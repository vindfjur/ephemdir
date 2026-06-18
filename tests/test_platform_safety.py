"""Regression tests for platform and trusted-executable safety."""

from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from ephemdir import _inuse, _platform, _trusted_exec, core
from ephemdir.core import recover, registered, sweep, tempdir


def _expire(registry, path: Path) -> None:
    with registry.transaction() as state:
        state[str(path)]["expires_at"] = 0.0


def test_mount_id_unavailable_is_non_destructive(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "sentinel.txt").write_text("keep", encoding="utf-8")
    _expire(registry, d.path)
    monkeypatch.setattr(core.sys, "platform", "linux")
    monkeypatch.setattr(core, "_mount_id_for_fd", lambda fd: None)

    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "active"
    assert entry["staging_path"] is None
    assert (d.path / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(f".{d.path.name}.*.deleting"))


def test_parent_fd_backend_failure_is_non_destructive(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    def unsupported(path):
        raise NotImplementedError("dir_fd unavailable")

    monkeypatch.setattr(core, "_open_trusted_parent", unsupported)
    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "active"
    assert d.path.is_dir()


def test_mount_id_uses_mountinfo_after_older_interfaces(monkeypatch):
    monkeypatch.setattr(core, "_linux_mount_id_statx", lambda fd: None)
    monkeypatch.setattr(core, "_linux_mount_id_fdinfo", lambda fd: None)
    monkeypatch.setattr(core, "_linux_mount_id_mountinfo", lambda fd: 42)
    assert core._linux_mount_id(123) == 42


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd semantics required")
def test_delete_boundary_allows_same_linux_mount_with_different_st_dev(tmp_path, monkeypatch):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(tmp_path, flags)
    try:
        monkeypatch.setattr(core.sys, "platform", "linux")
        monkeypatch.setattr(core, "_mount_id_for_fd", lambda fd: 10)

        core._check_child_mount_boundary(
            fd,
            root_dev=os.fstat(fd).st_dev + 1,
            root_mount_id=10,
        )
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd semantics required")
def test_delete_boundary_blocks_different_linux_mount_with_same_st_dev(tmp_path, monkeypatch):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(tmp_path, flags)
    try:
        monkeypatch.setattr(core.sys, "platform", "linux")
        monkeypatch.setattr(core, "_mount_id_for_fd", lambda fd: 11)

        with pytest.raises(OSError) as error:
            core._check_child_mount_boundary(
                fd,
                root_dev=os.fstat(fd).st_dev,
                root_mount_id=10,
            )
    finally:
        os.close(fd)
    assert error.value.errno == errno.EXDEV


def test_recovery_name_is_reserved_during_new_creation(tmp_path, registry, monkeypatch):
    names = iter(["reserved-name", "reserved-name", "fresh-name"])
    monkeypatch.setattr(core, "funny_name", lambda words: next(names))

    old = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = tmp_path / f".{old.path.name}.123-abcdef12.deleting"
    os.rename(old.path, staging)
    with registry.transaction() as state:
        state[str(old.path)].update(
            {"state": "recovery", "claim_id": None, "staging_path": str(staging)}
        )
    old_snapshot = dict(registered(registry=registry)[str(old.path)])

    new = tempdir(lifetime="1h", parent=tmp_path, registry=registry)

    assert new.path.name == "fresh-name"
    state = registered(registry=registry)
    assert state[str(old.path)] == old_snapshot
    assert str(new.path) in state
    assert staging.is_dir()


@pytest.mark.parametrize("action", ["forget", "retry"])
def test_recover_refuses_a_changed_snapshot(tmp_path, registry, monkeypatch, action):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = tmp_path / f".{d.path.name}.123-abcdef12.deleting"
    os.rename(d.path, staging)
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "recovery", "claim_id": None, "staging_path": str(staging)}
        )
    changed = dict(registered(registry=registry)[str(d.path)])
    changed["marker_id"] = "e" * 32
    changed["staging_path"] = str(tmp_path / f".{d.path.name}.456-fedcba98.deleting")

    @contextmanager
    def change_before_lock(lock_key):
        with registry.transaction() as state:
            state[str(d.path)] = dict(changed)
        yield True

    monkeypatch.setattr(registry, "deletion_lock", change_before_lock)
    with pytest.raises(LookupError, match="changed concurrently"):
        recover(d.path.name, action=action, registry=registry)
    assert registered(registry=registry)[str(d.path)] == changed
    assert staging.is_dir()


@pytest.mark.skipif(os.name != "posix", reason="executable PATH semantics")
def test_lsof_probe_ignores_attacker_path(tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    fake = tmp_path / "lsof"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    _inuse._lsof_in_use(str(tmp_path))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="executable PATH semantics")
def test_sysctl_probe_ignores_attacker_path(tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    fake = tmp_path / "sysctl"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    _platform._boot_time_macos()
    assert not marker.exists()


def test_trusted_resolver_rejects_final_symlink(tmp_path, monkeypatch):
    target = tmp_path / "real-tool"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    link = tmp_path / "tool"
    link.symlink_to(target)
    tmp_path.chmod(0o700)
    monkeypatch.setattr(_trusted_exec, "trusted_system_dirs", lambda: (tmp_path,))

    assert _trusted_exec.resolve_system_executable("tool") is None


def test_trusted_resolver_rejects_writable_binary(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(stat.S_IRWXU | stat.S_IWGRP | stat.S_IXGRP)
    tmp_path.chmod(0o700)
    monkeypatch.setattr(_trusted_exec, "trusted_system_dirs", lambda: (tmp_path,))

    assert _trusted_exec.resolve_system_executable("tool") is None


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership semantics required")
def test_trusted_resolver_rejects_foreign_owned_directory(tmp_path, monkeypatch):
    # A 0755 directory owned by another user holding a root-owned executable:
    # the directory owner can replace the file at will, so the executable must
    # not be trusted even though the file itself passes every check. The
    # foreign owner is synthetic (euid + 1, never root), so the test stays
    # deterministic whether the suite runs as a regular user or as uid 0.
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    tmp_path.chmod(0o755)

    real_stat = os.stat

    def synthetic_owners(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        values = list(result)
        if str(path) == str(tool):
            values[4] = 0  # st_uid: the executable itself looks root-owned
        elif str(path) == str(tmp_path):
            values[4] = os.geteuid() + 1  # st_uid: directory owner is foreign
        return os.stat_result(values)

    monkeypatch.setattr(os, "stat", synthetic_owners)
    monkeypatch.setattr(_trusted_exec, "trusted_system_dirs", lambda: (tmp_path,))

    assert _trusted_exec.resolve_system_executable("tool") is None


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership semantics required")
def test_trusted_resolver_rejects_foreign_owned_ancestor(tmp_path, monkeypatch):
    # LOW-03: the trusted directory and the executable inside it are both
    # clean, but a *parent* directory is owned by another user. That parent's
    # owner can replace the trusted directory wholesale, so the executable must
    # not be trusted. Every other component is forced clean (synthetic owners)
    # so the result depends only on the foreign ancestor, regardless of the
    # real perms of tmp_path's ancestors or the uid running the suite.
    foreign_parent = tmp_path / "outer"
    trusted_dir = foreign_parent / "bin"
    trusted_dir.mkdir(parents=True, mode=0o755)
    foreign_parent.chmod(0o755)
    tool = trusted_dir / "tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)

    real_stat, real_lstat = os.stat, os.lstat

    def synthetic(real, foreign):
        foreign_paths = {str(path) for path in foreign}

        def wrapper(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            values = list(result)
            if str(path) in foreign_paths:
                values[4] = os.geteuid() + 1  # st_uid: foreign ancestor owner
            else:
                values[0] = result.st_mode & ~0o022  # clear group/world write
                values[4] = os.geteuid()  # owned by us
            return os.stat_result(values)

        return wrapper

    monkeypatch.setattr(_trusted_exec, "trusted_system_dirs", lambda: (trusted_dir,))

    monkeypatch.setattr(os, "stat", synthetic(real_stat, {foreign_parent}))
    monkeypatch.setattr(os, "lstat", synthetic(real_lstat, {foreign_parent}))
    assert _trusted_exec.resolve_system_executable("tool") is None

    # Positive control: the very same tree without the foreign ancestor
    # resolves, proving the rejection above was caused by that ancestor alone.
    monkeypatch.setattr(os, "stat", synthetic(real_stat, set()))
    monkeypatch.setattr(os, "lstat", synthetic(real_lstat, set()))
    assert _trusted_exec.resolve_system_executable("tool") == str(tool)


def test_windows_system_dir_ignores_environment(tmp_path, monkeypatch):
    fake_root = tmp_path / "attacker-windows"
    real_system = tmp_path / "real-windows" / "System32"
    monkeypatch.setattr(_trusted_exec.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(fake_root))
    monkeypatch.setenv("WINDIR", str(fake_root))
    monkeypatch.setattr(
        _trusted_exec, "_windows_system_directory", lambda: real_system
    )

    assert _trusted_exec.trusted_system_dirs() == (real_system,)
    env = _trusted_exec.minimal_subprocess_env()
    assert env["SystemRoot"] == str(real_system.parent)
    assert env["WINDIR"] == str(real_system.parent)
    assert str(fake_root) not in env["PATH"]
