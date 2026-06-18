"""Tests for the core API: creation, lifetime parsing and sweeping."""

from __future__ import annotations

import os
import re
import stat
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from ephemdir import _size as size_module
from ephemdir import core as core_module
from ephemdir._registry import RegistryUnavailableError
from ephemdir.core import (
    EphemeralDirectory,
    SweepMode,
    explain,
    parse_lifetime,
    parse_size,
    plan_sweep,
    prune,
    recover,
    registered,
    resolve,
    sweep,
    tempdir,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (3600, 3600.0),
        (90.5, 90.5),
        ("90s", 90.0),
        ("2h", 7200.0),
        ("1h30m", 5400.0),
        ("1 day", 86400.0),
        (timedelta(minutes=5), 300.0),
    ],
)
def test_parse_lifetime(value, expected):
    assert parse_lifetime(value) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "10x",
        "abc",
        "-5",
        -1,
        float("nan"),
        float("inf"),
        timedelta(seconds=-5),
        True,
        "9" * 400 + "h",  # overflows float to infinity
    ],
)
def test_parse_lifetime_rejects_bad_input(bad):
    with pytest.raises((ValueError, TypeError)):
        parse_lifetime(bad)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1K", 1000),
        ("1MiB", 1024**2),
        ("1.5G", 1_500_000_000),
        (42, 42),
        (None, None),
    ],
)
def test_parse_size(value, expected):
    assert parse_size(value) == expected


def test_core_path_state_and_deletion_guard_edges(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    assert core_module._path_state(missing) == "missing"

    monkeypatch.setattr(
        core_module.os,
        "lstat",
        lambda path: (_ for _ in ()).throw(OSError("unreachable")),
    )
    assert core_module._path_state(tmp_path / "unknown") == "unknown"
    monkeypatch.setattr(core_module.os, "lstat", os.lstat)

    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "state" / "data"
    config = tmp_path / "state" / "config"
    data.mkdir(parents=True)
    config.mkdir()
    monkeypatch.setattr(core_module.Path, "home", lambda: home)
    monkeypatch.setattr(core_module, "user_data_dir", lambda create=False: data)
    monkeypatch.setattr(core_module, "user_config_dir", lambda create=False: config)

    assert core_module._deletion_guard(Path("relative")) == "path is not absolute"
    assert core_module._deletion_guard(Path("/")) == "filesystem root"
    assert core_module._deletion_guard(home) == "home directory"
    assert core_module._deletion_guard(home.parent) == "ancestor of the home directory"
    assert core_module._deletion_guard(data) == "ephemdir data directory"
    assert core_module._deletion_guard(data.parent) == "ancestor of the ephemdir data directory"


def test_core_marker_write_read_and_remove_edges(tmp_path, monkeypatch):
    directory = tmp_path / "owned"
    directory.mkdir()

    with pytest.raises(ValueError, match="32 lowercase"):
        core_module._write_marker(directory, "é" * 32)

    fd = os.open(directory, os.O_RDONLY)
    try:
        monkeypatch.setattr(core_module.os, "write", lambda fd, payload: 0)
        with pytest.raises(OSError, match="short write"):
            core_module._write_marker(directory, "0" * 32, dir_fd=fd)
    finally:
        os.close(fd)

    monkeypatch.setattr(
        core_module,
        "_open_directory_nofollow",
        lambda path: (_ for _ in ()).throw(OSError("cannot open")),
    )
    assert core_module._read_marker(directory) is None
    core_module._remove_marker_if_ours(directory, "0" * 32)
    core_module._remove_marker_if_ours(directory, None)


def test_core_ownership_classifiers(tmp_path, monkeypatch):
    path = tmp_path / "owned"
    staging = tmp_path / f".{path.name}.123-abcdef12.deleting"
    entry = {"marker_id": "a" * 32, "dev": 1, "ino": 2}

    monkeypatch.setattr(core_module, "_is_real_directory", lambda path: False)
    assert core_module._ownership(path, entry) == "foreign"

    monkeypatch.setattr(core_module, "_is_real_directory", lambda path: True)
    assert core_module._ownership(path, {}) == "unverified"

    monkeypatch.setattr(core_module, "_read_marker", lambda path: "b" * 32)
    assert core_module._ownership(path, entry) == "foreign"

    monkeypatch.setattr(core_module, "_read_marker", lambda path: "a" * 32)
    monkeypatch.setattr(core_module, "_inode_matches", lambda path, entry: False)
    assert core_module._ownership(path, entry) == "foreign"

    monkeypatch.setattr(core_module, "_inode_matches", lambda path, entry: True)
    assert core_module._ownership(path, entry) == "ours"

    assert core_module._staging_ownership(path, tmp_path / "bad", entry) == "foreign"
    monkeypatch.setattr(core_module, "_inode_matches", lambda path, entry: False)
    assert core_module._staging_ownership(path, staging, entry) == "foreign"

    monkeypatch.setattr(core_module, "_inode_matches", lambda path, entry: True)
    monkeypatch.setattr(core_module, "_read_marker", lambda path: "a" * 32)
    assert core_module._staging_ownership(path, staging, entry) == "ours"

    monkeypatch.setattr(core_module, "_read_marker", lambda path: "b" * 32)
    assert core_module._staging_ownership(path, staging, entry) == "foreign"

    monkeypatch.setattr(core_module, "_read_marker", lambda path: None)
    assert core_module._staging_ownership(path, staging, entry) == "unverified"

    monkeypatch.setattr(core_module, "_inode_matches", lambda path, entry: None)
    assert core_module._staging_ownership(path, staging, {}) == "unverified"
    monkeypatch.setattr(core_module, "_inode_matches", lambda path, entry: True)
    assert core_module._staging_ownership(path, staging, {}) == "ours"

    assert core_module._deletion_lock_key(str(path), entry) == "a" * 32
    assert len(core_module._deletion_lock_key(str(path), {})) == 32


def test_core_inode_and_directory_helpers(tmp_path, monkeypatch):
    path = tmp_path / "dir"
    path.mkdir()
    info = path.stat()

    assert core_module._inode_matches(path, {}) is None
    assert core_module._inode_matches(path, {"dev": info.st_dev, "ino": info.st_ino}) is True
    assert core_module._inode_matches(path, {"dev": info.st_dev, "ino": info.st_ino + 1}) is False
    assert core_module._inode_matches(tmp_path / "missing", {"dev": 1, "ino": 2}) is False

    fd = os.open(path, os.O_RDONLY)
    try:
        assert core_module._fd_inode_matches(fd, {}) is None
        assert core_module._fd_inode_matches(fd, {"dev": info.st_dev, "ino": info.st_ino}) is True
    finally:
        os.close(fd)

    assert core_module._is_real_directory(path) is True
    assert core_module._is_real_directory(tmp_path / "missing") is False


def test_core_parse_and_name_style_helpers(tmp_path, monkeypatch):
    with pytest.raises(TypeError, match="cleanup"):
        core_module._parse_cleanup_policy(None)
    with pytest.raises(ValueError, match="cleanup"):
        core_module._parse_cleanup_policy("later")
    assert core_module._parse_name_style("funny") == "secure"
    with pytest.raises(TypeError, match="name_style"):
        core_module._parse_name_style(None)
    with pytest.raises(TypeError, match="remove_on_restart"):
        core_module._require_bool("true", "remove_on_restart")
    assert core_module._normalize_parent_value(None) == Path.cwd()
    with pytest.raises(TypeError, match="parent"):
        core_module._normalize_parent_value(object())

    non_private = os.stat_result(
        (
            stat.S_IFDIR | 0o755,
            1,
            1,
            1,
            os.geteuid() if hasattr(os, "geteuid") else 0,
            0,
            0,
            0,
            0,
            0,
        )
    )
    monkeypatch.setattr(core_module.os, "fstat", lambda fd: non_private)
    assert core_module._effective_name_style_for_preflight(tmp_path, "secure", 99) == "secure"
    assert core_module._effective_name_style_for_preflight(tmp_path, "auto", 99) == "secure"
    with pytest.raises(PermissionError, match="clean"):
        core_module._effective_name_style_for_preflight(tmp_path, "clean", 99)
    with pytest.raises(ValueError, match="name_style"):
        core_module._effective_name_style_for_preflight(tmp_path, "bad", 99)

    monkeypatch.setattr(core_module, "_is_private_parent", lambda parent: False)
    assert core_module._effective_name_style(tmp_path, "secure") == "secure"
    assert core_module._effective_name_style(tmp_path, "auto") == "secure"
    with pytest.raises(PermissionError, match="clean"):
        core_module._effective_name_style(tmp_path, "clean")


def test_core_private_parent_and_delete_backend_checks(tmp_path, monkeypatch):
    private = os.stat_result(
        (
            stat.S_IFDIR | 0o700,
            1,
            1,
            1,
            os.geteuid() if hasattr(os, "geteuid") else 0,
            0,
            0,
            0,
            0,
            0,
        )
    )
    assert core_module._is_private_parent_stat(private) is True

    monkeypatch.setattr(core_module.os, "name", "nt")
    assert core_module._is_private_parent_stat(private) is False
    assert "unavailable" in str(core_module._safe_delete_backend_error())

    monkeypatch.setattr(core_module.os, "name", "posix")
    monkeypatch.setattr(core_module.os, "supports_dir_fd", frozenset())
    assert "dir_fd" in str(core_module._safe_delete_backend_error())

    monkeypatch.setattr(
        core_module.os,
        "supports_dir_fd",
        frozenset({os.open, os.stat, os.unlink, os.rmdir, os.rename, os.mkdir}),
    )
    monkeypatch.setattr(core_module.os, "supports_fd", frozenset())
    assert "scandir" in str(core_module._safe_delete_backend_error())


def test_core_verified_child_removal_detects_replacement(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    parent_fd = os.open(parent, os.O_RDONLY)
    child_fd = os.open(child, os.O_RDONLY)
    try:
        child.rmdir()
        with pytest.raises(core_module._StagingIdentityError, match="disappeared"):
            core_module._rmdir_verified_child(parent_fd, "child", child_fd)
    finally:
        os.close(child_fd)
        os.close(parent_fd)


def test_tempdir_creates_directory(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    assert isinstance(d, EphemeralDirectory)
    assert d.path.is_dir()
    assert d.path.parent == tmp_path
    # The generated name is a two-word slug by default.
    assert "-" in d.path.name


def test_tempdir_is_path_like(tmp_path, registry):
    import os

    d = tempdir(parent=tmp_path, registry=registry)
    assert os.fspath(d) == str(d.path)
    (d / "file.txt").write_text("hi")
    assert (d.path / "file.txt").read_text() == "hi"


def test_context_manager_removes_directory(tmp_path, registry):
    with tempdir(parent=tmp_path, registry=registry) as d:
        path = d.path
        assert path.is_dir()
    assert not path.exists()


def test_remove_is_idempotent(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    d.remove()
    d.remove()  # second call must not raise
    assert not d.path.exists()


def test_keep_stops_tracking_without_deleting(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    d.keep()
    assert d.path.is_dir()
    assert str(d.path) not in registered(registry=registry)


def test_sweep_removes_expired(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    # Force the entry to look expired.
    with registry.transaction() as state:
        state[str(d.path)]["expires_at"] = time.time() - 1
    removed = sweep(registry=registry)
    assert removed == 1
    assert not d.path.exists()


def test_sweep_keeps_unexpired(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    removed = sweep(registry=registry)
    assert removed == 0
    assert d.path.is_dir()


def test_sweep_removes_on_restart(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("ephemdir.core.boot_session_id", lambda: "current-boot")
    d = tempdir(parent=tmp_path, registry=registry)
    # A different stable boot-session id is authoritative and independent of
    # wall-clock corrections.
    with registry.transaction() as state:
        state[str(d.path)]["boot_id"] = "definitely-an-older-boot"
    removed = sweep(registry=registry)
    assert removed == 1
    assert not d.path.exists()


def test_keep_on_restart_survives(tmp_path, registry):
    d = tempdir(remove_on_restart=False, parent=tmp_path, registry=registry)
    with registry.transaction() as state:
        state[str(d.path)]["boot_time"] = time.time() - 10_000_000
    removed = sweep(registry=registry)
    assert removed == 0
    assert d.path.is_dir()


def test_sweep_force_removes_everything(tmp_path, registry):
    d = tempdir(lifetime="100h", parent=tmp_path, registry=registry)
    removed = sweep(registry=registry, force=True)
    assert removed == 1
    assert not d.path.exists()


def test_sweep_mode_force_removes_everything(tmp_path, registry):
    d = tempdir(lifetime="100h", parent=tmp_path, registry=registry)
    removed = sweep(registry=registry, mode=SweepMode.FORCE)
    assert removed == 1
    assert not d.path.exists()


def test_sweep_preserves_vanished_entries(tmp_path, registry):
    import shutil

    d = tempdir(parent=tmp_path, registry=registry)
    # Remove the directory behind ephemdir's back.
    shutil.rmtree(d.path)
    assert sweep(registry=registry) == 0
    assert str(d.path) in registered(registry=registry)


def test_restart_due_missing_entry_is_removed_after_reappearing(
    tmp_path,
    registry,
    monkeypatch,
):
    import shutil

    monkeypatch.setattr("ephemdir.core.boot_session_id", lambda: "current-boot")
    d = tempdir(parent=tmp_path, registry=registry)
    marker_id = registered(registry=registry)[str(d.path)]["marker_id"]
    with registry.transaction() as state:
        state[str(d.path)]["boot_id"] = "older-boot"
    shutil.rmtree(d.path)

    assert sweep(registry=registry) == 0
    assert str(d.path) in registered(registry=registry)

    d.path.mkdir(mode=0o700)
    core_module._write_marker(d.path, marker_id)
    path_stat = d.path.stat()
    with registry.transaction() as state:
        state[str(d.path)]["dev"] = path_stat.st_dev
        state[str(d.path)]["ino"] = path_stat.st_ino

    assert sweep(registry=registry) == 1
    assert not d.path.exists()
    assert str(d.path) not in registered(registry=registry)


def test_next_sweep_survives_maintenance_but_full_sweep_removes(tmp_path, registry):
    d = tempdir(
        cleanup="next-sweep",
        parent=tmp_path,
        registry=registry,
    )

    assert sweep(registry=registry, mode="maintenance") == 0
    assert d.path.is_dir()

    decision = explain(d.path.name, registry=registry)
    assert decision.due is True
    assert decision.reasons == ("next-sweep",)

    assert sweep(registry=registry) == 1
    assert not d.path.exists()


def test_next_sweep_rejects_lifetime_or_restart_cleanup(tmp_path, registry):
    with pytest.raises(ValueError, match="until-sweep"):
        tempdir(
            lifetime="1h",
            cleanup="next-sweep",
            remove_on_restart=False,
            parent=tmp_path,
            registry=registry,
        )
    with pytest.raises(ValueError, match="until-sweep"):
        tempdir(
            cleanup="next-sweep",
            remove_on_restart=True,
            parent=tmp_path,
            registry=registry,
        )


def test_clean_name_validation_happens_before_maintenance_sweep(tmp_path, registry):
    private_parent = tmp_path / "private"
    shared_parent = tmp_path / "shared"
    private_parent.mkdir(mode=0o700)
    shared_parent.mkdir(mode=0o755)
    d = tempdir(lifetime="1h", parent=private_parent, registry=registry)
    with registry.transaction() as state:
        state[str(d.path)]["expires_at"] = time.time() - 1

    with pytest.raises(PermissionError, match="clean"):
        tempdir(name_style="clean", parent=shared_parent, registry=registry)

    assert d.path.exists()
    assert str(d.path) in registered(registry=registry)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"remove_on_restart": "false"},
        {"keep_while_in_use": "false"},
        {"prefix": None},
        {"parent": 123},
        {"cleanup": None},
        {"name_style": None},
        {"max_size": True},
    ],
)
def test_invalid_public_api_types_have_no_maintenance_side_effects(
    tmp_path,
    registry,
    kwargs,
):
    d = tempdir(lifetime="1h", parent=tmp_path / "private", registry=registry)
    with registry.transaction() as state:
        state[str(d.path)]["expires_at"] = time.time() - 1

    with pytest.raises((TypeError, ValueError)):
        tempdir(parent=tmp_path / "new", registry=registry, **kwargs)

    assert d.path.exists()
    assert str(d.path) in registered(registry=registry)


def test_extend_next_sweep_switches_back_to_auto(tmp_path, registry):
    d = tempdir(cleanup="next-sweep", parent=tmp_path, registry=registry)

    d.extend("2h")
    decision = explain(d.path.name, registry=registry)

    assert "next-sweep" not in decision.reasons
    assert sweep(registry=registry) == 0
    assert d.path.is_dir()


def test_max_size_marks_oversize_due(tmp_path, registry):
    d = tempdir(max_size=1, parent=tmp_path, registry=registry)
    (d.path / "big.txt").write_text("too much")

    plan = plan_sweep(registry=registry)
    decision = next(item for item in plan if item.path == d.path)
    assert decision.due is True
    assert "oversize" in decision.reasons

    assert sweep(registry=registry) == 1
    assert not d.path.exists()


def test_empty_directory_is_not_oversize_due_to_marker(tmp_path, registry):
    d = tempdir(max_size=1, parent=tmp_path, registry=registry)

    decision = explain(d.path.name, registry=registry)

    assert "oversize" not in decision.reasons
    assert decision.measured_size_bytes == 0


def test_size_scan_refuses_linux_mount_boundary(tmp_path, monkeypatch):
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    monkeypatch.setattr(size_module.sys, "platform", "linux")
    mount_ids = iter([10, 11])
    monkeypatch.setattr(size_module, "_mount_id_for_fd", lambda fd: next(mount_ids))

    result = size_module.measure_tree(root)

    assert result.bytes is None
    assert result.error == "unsupported-filesystem"


def test_size_scan_checks_directory_mount_before_early_limit(tmp_path, monkeypatch):
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    monkeypatch.setattr(size_module.sys, "platform", "linux")
    mount_ids = iter([10, 11])
    monkeypatch.setattr(size_module, "_mount_id_for_fd", lambda fd: next(mount_ids))

    result = size_module.measure_tree(root, limit=0)

    assert result.bytes is None
    assert result.error == "unsupported-filesystem"


def test_size_scan_checks_file_mount_boundary(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "mounted-file").write_text("external", encoding="utf-8")
    monkeypatch.setattr(size_module.sys, "platform", "linux")
    mount_ids = iter([10, 11])
    monkeypatch.setattr(size_module, "_mount_id_for_fd", lambda fd: next(mount_ids))

    result = size_module.measure_tree(root, limit=0)

    assert result.bytes is None
    assert result.error == "unsupported-filesystem"


def test_size_scan_allows_same_linux_mount_with_different_st_dev(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "big.txt"
    child.write_text("external", encoding="utf-8")
    real_stat = os.stat
    real_fstat = os.fstat
    root_real = real_stat(root, follow_symlinks=False)
    child_real = real_stat(child, follow_symlinks=False)

    def with_dev(info: os.stat_result, dev: int) -> os.stat_result:
        values = list(info)
        values[2] = dev
        return os.stat_result(values)

    def fake_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if (info.st_dev, info.st_ino) == (root_real.st_dev, root_real.st_ino):
            return with_dev(info, 19)
        if (info.st_dev, info.st_ino) == (child_real.st_dev, child_real.st_ino):
            return with_dev(info, 17)
        return info

    def fake_fstat(fd):
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == (root_real.st_dev, root_real.st_ino):
            return with_dev(info, 19)
        if (info.st_dev, info.st_ino) == (child_real.st_dev, child_real.st_ino):
            return with_dev(info, 17)
        return info

    monkeypatch.setattr(size_module.sys, "platform", "linux")
    monkeypatch.setattr(size_module.os, "stat", fake_stat)
    monkeypatch.setattr(size_module.os, "fstat", fake_fstat)
    mount_ids = iter([10, 10])
    monkeypatch.setattr(size_module, "_mount_id_for_fd", lambda fd: next(mount_ids))

    result = size_module.measure_tree(root, limit=1)

    assert result.bytes is not None
    assert result.bytes > 1
    assert result.error is None


def test_clean_name_style_omits_suffix_in_private_parent(tmp_path, registry):
    if os.name == "posix":
        tmp_path.chmod(0o700)
    d = tempdir(name_style="clean", parent=tmp_path, registry=registry)
    assert re.fullmatch(r"[a-z]+-[a-z]+", d.path.name)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions required")
def test_clean_name_style_rejects_shared_parent(tmp_path, registry):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    with pytest.raises(PermissionError, match="clean"):
        tempdir(name_style="clean", parent=parent, registry=registry)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions required")
def test_plan_sweep_does_not_tighten_registry_permissions(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    registry.path.chmod(0o644)

    plan_sweep(registry=registry)

    assert oct(registry.path.stat().st_mode & 0o777) == "0o644"
    assert d.path.is_dir()


def test_plan_sweep_marks_missing_entry_as_blocked(tmp_path, registry):
    d = tempdir(cleanup="next-sweep", parent=tmp_path, registry=registry)
    import shutil

    shutil.rmtree(d.path)

    decision = next(item for item in plan_sweep(registry=registry) if item.path == d.path)
    assert decision.status == "missing"
    assert decision.due is True
    assert decision.reasons == ("next-sweep",)
    assert "path-missing" in decision.blockers
    assert decision.destructive_allowed is False


def _foreign_active_entry(**overrides):
    entry = {
        "created_at": 1.0,
        "expires_at": None,
        "remove_on_restart": True,
        "state": "active",
        "claim_id": None,
        "staging_path": None,
        "backend": "posix",
        "platform": f"foreign-{sys.platform}",
    }
    entry.update(overrides)
    return entry


def test_plan_sweep_reports_foreign_platform_before_stale(tmp_path, registry, monkeypatch):
    missing = tmp_path / "missing-foreign"
    registry.save({str(missing): _foreign_active_entry()})

    def fail_probe(path):
        pytest.fail(f"foreign entry probed before compatibility check: {path}")

    monkeypatch.setattr(core_module, "_path_state", fail_probe)

    decision = next(item for item in plan_sweep(registry=registry) if item.path == missing)

    assert decision.status == "blocked"
    assert "foreign-platform" in decision.blockers


def test_foreign_active_missing_entry_is_preserved_by_sweep(tmp_path, registry, monkeypatch):
    missing = tmp_path / "missing-foreign"
    registry.save({str(missing): _foreign_active_entry()})

    def fail_probe(path):
        pytest.fail(f"foreign entry probed before compatibility check: {path}")

    monkeypatch.setattr(core_module, "_path_state", fail_probe)

    assert sweep(registry=registry) == 0
    assert str(missing) in registered(registry=registry)


def test_foreign_active_missing_entry_is_preserved_by_prune(tmp_path, registry):
    missing = tmp_path / "missing-foreign"
    registry.save({str(missing): _foreign_active_entry()})

    assert prune(registry=registry) == 0
    assert str(missing) in registered(registry=registry)


def test_resolve_does_not_drop_foreign_active_entry(tmp_path, registry):
    missing = tmp_path / "missing-foreign"
    registry.save({str(missing): _foreign_active_entry()})

    with pytest.raises(LookupError, match="incompatible runtime"):
        resolve(missing.name, registry=registry)

    assert str(missing) in registered(registry=registry)


def test_plan_sweep_reports_in_use_unknown(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("ephemdir.core.is_in_use", lambda path: None)
    d = tempdir(lifetime="1h", keep_while_in_use=True, parent=tmp_path, registry=registry)
    with registry.transaction() as state:
        state[str(d.path)]["expires_at"] = time.time() - 1

    decision = next(item for item in plan_sweep(registry=registry) if item.path == d.path)
    assert "in-use-unknown" in decision.blockers
    assert decision.destructive_allowed is False



def test_tempdir_refuses_unsupported_backend_before_side_effects(
    tmp_path, registry, monkeypatch
):
    parent = tmp_path / "work"
    monkeypatch.setattr(
        "ephemdir.core._safe_delete_backend_error",
        lambda: "safe deletion backend unavailable",
    )

    with pytest.raises(OSError, match="cannot create an ephemeral directory safely"):
        tempdir(parent=parent, registry=registry)

    assert not parent.exists()
    assert not registry.path.exists()


def test_tempdir_invalid_parent_does_not_run_maintenance_sweep(tmp_path, registry):
    expired = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    with registry.transaction() as state:
        state[str(expired.path)]["expires_at"] = 0.0
    invalid_parent = tmp_path / "not-a-directory"
    invalid_parent.write_text("file", encoding="utf-8")

    with pytest.raises(FileExistsError):
        tempdir(parent=invalid_parent, registry=registry)

    assert expired.path.exists()
    assert str(expired.path) in registered(registry=registry)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_tempdir_rejects_symlink_ancestor_before_maintenance(tmp_path, registry):
    expired = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    with registry.transaction() as state:
        state[str(expired.path)]["expires_at"] = 0.0
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(PermissionError, match="symlink"):
        tempdir(parent=alias / "child", registry=registry)

    assert expired.path.exists()
    assert str(expired.path) in registered(registry=registry)
    assert not (target / "child").exists()


def test_normalize_parent_accepts_macos_var_alias(monkeypatch):
    monkeypatch.setattr(core_module.sys, "platform", "darwin")

    assert core_module._normalize_parent_value("/var/folders/x") == Path("/private/var/folders/x")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_tempdir_rejects_final_parent_symlink_before_maintenance(tmp_path, registry):
    expired = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    with registry.transaction() as state:
        state[str(expired.path)]["expires_at"] = 0.0
    target = tmp_path / "target"
    target.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(PermissionError, match="symlink"):
        tempdir(parent=linked_parent, registry=registry)

    assert expired.path.exists()
    assert str(expired.path) in registered(registry=registry)
    assert not any(target.iterdir())


def test_overlong_prefix_does_not_run_maintenance_sweep(tmp_path, registry):
    expired = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    with registry.transaction() as state:
        state[str(expired.path)]["expires_at"] = 0.0

    with pytest.raises(ValueError, match="prefix"):
        tempdir(prefix="x" * 65, parent=tmp_path, registry=registry)

    assert expired.path.exists()
    assert str(expired.path) in registered(registry=registry)


def test_explicit_next_sweep_overrides_config_lifetime(tmp_path, registry, monkeypatch):
    monkeypatch.setattr(
        "ephemdir.core.load_config",
        lambda: {"lifetime": "6h", "remove_on_restart": True},
    )

    d = tempdir(cleanup="next-sweep", parent=tmp_path, registry=registry)
    entry = registered(registry=registry)[str(d.path)]

    assert entry["expires_at"] is None
    assert entry["remove_on_restart"] is False
    assert entry["cleanup_policy"] == "next-sweep"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_handle_remove_unsafe_registry_raises_without_marking_removed(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    real_registry = tmp_path / "real-registry.json"
    registry.path.rename(real_registry)
    registry.path.symlink_to(real_registry)

    with pytest.raises(RegistryUnavailableError):
        d.remove()

    assert d.path.exists()
    assert d._removed is False


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_explain_unsafe_registry_raises_typed_error(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    real_registry = tmp_path / "real-registry.json"
    registry.path.rename(real_registry)
    registry.path.symlink_to(real_registry)

    with pytest.raises(RegistryUnavailableError):
        explain(d.path.name, registry=registry)

    assert d.path.exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_recover_unsafe_registry_raises_typed_error(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "moving", "claim_id": "1" * 32, "staging_path": str(staging)}
        )
    real_registry = tmp_path / "real-registry.json"
    registry.path.rename(real_registry)
    registry.path.symlink_to(real_registry)

    with pytest.raises(RegistryUnavailableError):
        recover(d.path.name, registry=registry)

    assert d.path.exists()
