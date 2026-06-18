"""Tests for the 0.4.0 hardening work.

The cases cover directory replacement, partial recursive deletion, stale or
contended registry locks, poisoned registry data, unanswerable in-use probes and
stepped system clocks.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

import ephemdir._registry as registry_module
import ephemdir.core as core
from ephemdir._naming import funny_name
from ephemdir._platform import user_data_dir
from ephemdir._registry import CorruptRegistryError, Registry
from ephemdir.core import (
    _MARKER_NAME,
    _deletion_guard,
    _read_marker,
    _write_marker,
    extend,
    keep,
    registered,
    remove,
    sweep,
    tempdir,
)


def _expire(registry, path):
    with registry.transaction() as state:
        state[str(path)]["expires_at"] = time.time() - 1


# --- Ownership marker --------------------------------------------------------

def test_marker_is_created_and_registered(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    marker = d.path / _MARKER_NAME
    assert marker.is_file()
    entry = registered(registry=registry)[str(d.path)]
    assert entry["marker_id"] == marker.read_text().strip()


def test_marker_reader_accepts_only_canonical_payload(tmp_path):
    directory = tmp_path / "owned"
    directory.mkdir()
    marker = directory / _MARKER_NAME
    marker.write_bytes(b"a" * 32 + b"\n")
    assert _read_marker(directory) == "a" * 32

    marker.write_bytes(b"A" * 32 + b"\n")
    assert _read_marker(directory) is None
    marker.write_bytes(b"a" * 31 + b"\n")
    assert _read_marker(directory) is None
    marker.write_bytes(b"a" * 32 + b"\r\n")
    assert _read_marker(directory) is None


def test_marker_reader_rejects_oversized_regular_file(tmp_path):
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / _MARKER_NAME).write_bytes(b"a" * 34)
    assert _read_marker(directory) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support required")
def test_marker_fifo_cannot_block_sweep(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    marker = d.path / _MARKER_NAME
    marker.unlink()
    os.mkfifo(marker)
    _expire(registry, d.path)

    started = time.monotonic()
    assert sweep(registry=registry) == 0
    assert time.monotonic() - started < 1.0
    assert d.path.is_dir()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_marker_symlink_is_never_followed(tmp_path):
    directory = tmp_path / "owned"
    directory.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"a" * 32 + b"\n")
    (directory / _MARKER_NAME).symlink_to(victim)
    assert _read_marker(directory) is None


def test_write_marker_rejects_noncanonical_explicit_id(tmp_path):
    directory = tmp_path / "owned"
    directory.mkdir()
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        _write_marker(directory, "not-a-marker")
    assert not (directory / _MARKER_NAME).exists()


def test_replaced_directory_is_never_deleted(tmp_path, registry):
    # A directory deleted manually can be replaced at the same path before the
    # sweep runs; the replacement must never be removed.
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    shutil.rmtree(d.path)
    impostor = d.path
    impostor.mkdir()
    (impostor / "precious.txt").write_text("user data")
    _expire(registry, d.path)

    assert sweep(registry=registry) == 0
    assert (impostor / "precious.txt").read_text() == "user data"
    assert str(d.path) not in registered(registry=registry)  # stale entry dropped


def test_tampered_marker_blocks_deletion(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / _MARKER_NAME).write_text("someone-elses-id\n")
    _expire(registry, d.path)

    assert sweep(registry=registry) == 0
    assert d.path.is_dir()


def test_legacy_entry_is_never_swept_but_explicit_rm_works(tmp_path, registry):
    # Entries written by ephemdir <= 0.3 have no marker_id, so they cannot be
    # verified. They must never be deleted automatically (a crafted markerless
    # entry would otherwise bypass the whole marker protection); explicitly
    # naming the directory in `rm` counts as the user's confirmation.
    legacy = tmp_path / "old-school"
    legacy.mkdir()
    with registry.transaction() as state:
        state[str(legacy)] = {
            "created_at": time.time(),
            "expires_at": time.time() - 1,
            "remove_on_restart": True,
            "keep_while_in_use": False,
            "boot_time": None,
        }
    assert sweep(registry=registry, force=True) == 0
    assert legacy.is_dir()

    remove("old-school", registry=registry)
    assert not legacy.exists()


def test_remove_refuses_replaced_directory(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    shutil.rmtree(d.path)
    d.path.mkdir()
    (d.path / "data.txt").write_text("not ephemdir's")

    with pytest.raises(OSError, match="refusing to delete"):
        remove(d.path.name, registry=registry)
    assert (d.path / "data.txt").read_text() == "not ephemdir's"


def test_keep_inside_context_manager_is_honoured(tmp_path, registry):
    # Audit scenario: the global keep() must win even though the with-block
    # exit calls remove() on the handle afterwards.
    with tempdir(parent=tmp_path, registry=registry) as d:
        kept = keep(d.path.name, registry=registry)
        assert kept == d.path
        assert not (d.path / _MARKER_NAME).exists()
    assert d.path.is_dir()  # leaving the block did not delete the kept dir


def test_remove_refuses_untracked_path(tmp_path, registry):
    # A path without a registry entry must never be deleted by name.
    d = tempdir(parent=tmp_path, registry=registry)
    keep(d.path.name, registry=registry)
    with pytest.raises(LookupError):
        remove(d.path.name, registry=registry)
    assert d.path.is_dir()


# --- No blind rmtree fallback ------------------------------------------------

def test_explicit_remove_raises_when_locked(tmp_path, registry, monkeypatch):
    d = tempdir(parent=tmp_path, registry=registry)
    real_rename = os.rename

    def _raise(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if ".deleting" in str(dst):
            raise OSError("file is in use")
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr("ephemdir.core.os.rename", _raise)
    monkeypatch.setattr(
        "ephemdir.core.os.supports_dir_fd",
        frozenset(set(os.supports_dir_fd) | {_raise}),
    )
    with pytest.raises(OSError, match="locked or in use"):
        d.remove()
    assert d.path.is_dir()
    assert str(d.path) in registered(registry=registry)  # still tracked for retry


# --- Recovery from partial deletion -----------------------------------------

def test_partial_rmtree_failure_keeps_staging_tracked(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "keep.txt").write_text("data")
    _expire(registry, d.path)

    def _fail(path, *args, **kwargs):
        raise OSError("backend hiccup")

    monkeypatch.setattr("ephemdir.core._delete_staging_tree", _fail)
    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    staging = Path(entry["staging_path"])
    assert entry["state"] == "deleting"
    assert not d.path.exists()
    assert (staging / "keep.txt").read_text() == "data"

def test_unrestorable_leftover_stays_tracked_for_retry(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    real_replace = os.replace

    def _replace(src, dst):
        if ".deleting" in str(dst):
            return real_replace(src, dst)  # path -> staging succeeds
        if ".deleting" in str(src):
            raise OSError("cannot restore")  # staging -> path fails
        return real_replace(src, dst)

    def _fail_rmtree(path, *args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr("ephemdir.core.os.replace", _replace)
    monkeypatch.setattr("ephemdir.core._delete_staging_tree", _fail_rmtree)

    assert sweep(registry=registry) == 0
    # The journal keeps the entry under the original key, pointing at the
    # staging leftover -- the data is still tracked, not forgotten.
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "deleting"
    assert Path(entry["staging_path"]).exists()


# --- Sweep vs keep()/extend() races -------------------------------------------

def test_sweep_decision_loses_to_concurrent_extend(tmp_path, registry):
    from ephemdir.core import _try_claim

    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    snapshot = dict(registered(registry=registry)[str(d.path)])
    # extend() races in after the sweep made its decision but before the claim.
    extend(d.path.name, "5h", registry=registry)

    status, staging, _ = _try_claim(registry, d.path, snapshot, allow_unverified=False)
    assert status == "changed"
    assert d.path.is_dir()


def test_sweep_decision_loses_to_concurrent_keep(tmp_path, registry):
    from ephemdir.core import _try_claim

    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    snapshot = dict(registered(registry=registry)[str(d.path)])
    keep(d.path.name, registry=registry)

    status, staging, _ = _try_claim(registry, d.path, snapshot, allow_unverified=False)
    assert status == "missing"
    assert d.path.is_dir()


def test_keep_raises_when_sweep_already_claimed(tmp_path, registry):
    # Once a sweep has claimed (renamed) the directory, keep() must report the
    # loss instead of pretending the directory was saved.
    from ephemdir.core import _try_claim

    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    snapshot = dict(registered(registry=registry)[str(d.path)])
    status, staging, _ = _try_claim(registry, d.path, snapshot, allow_unverified=False)
    assert status == "claimed"

    with pytest.raises(LookupError):
        keep(d.path.name, registry=registry)


# --- Registry locking --------------------------------------------------------

def test_contended_lock_times_out_instead_of_proceeding(tmp_path, monkeypatch):
    registry = Registry(path=tmp_path / "registry.json")
    monkeypatch.setattr(registry_module, "_LOCK_TIMEOUT_SECONDS", 0.3)

    fd = os.open(registry._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    registry_module._try_lock(fd)
    try:
        with pytest.raises(TimeoutError):
            with registry.transaction():
                pass
    finally:
        registry_module._unlock(fd)
        os.close(fd)


def test_stale_lock_file_does_not_block(tmp_path):
    # A leftover lock *file* from a crashed process must cost nothing: the OS
    # lock died with the process, only the inert file remains.
    registry = Registry(path=tmp_path / "registry.json")
    registry._lock_path.touch()

    start = time.monotonic()
    with registry.transaction() as state:
        state[str(tmp_path / "probe")] = {
            "created_at": 1.0,
            "expires_at": None,
            "remove_on_restart": True,
        }
    assert time.monotonic() - start < 1.0


# --- Registry validation -----------------------------------------------------

def test_corrupt_registry_is_quarantined(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{ this is not json", encoding="utf-8")
    path.chmod(0o600)
    reg = Registry(path=path)

    # Unlocked reads fail closed without creating a new empty registry.
    with pytest.raises(CorruptRegistryError):
        reg.load()
    assert path.exists()
    with pytest.raises(CorruptRegistryError):
        with reg.transaction():
            pass
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "{ this is not json"
    assert list(tmp_path.glob("registry.json.corrupt-*"))


def test_nan_in_registry_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "registry.json"
    payload = '{"/x": {"created_at": NaN, "expires_at": null, "remove_on_restart": true}}'
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    reg = Registry(path=path)

    with pytest.raises(CorruptRegistryError):
        reg.load()
    with pytest.raises(CorruptRegistryError):
        with reg.transaction():
            pass
    assert path.exists()
    assert list(tmp_path.glob("registry.json.corrupt-*"))


def test_corrupt_registry_blocks_repeated_sweeps_without_losing_tracking(tmp_path):
    registry = Registry(path=tmp_path / "registry.json")
    directory = tempdir(parent=tmp_path / "work", registry=registry)
    registry.path.write_text("{broken", encoding="utf-8")
    registry.path.chmod(0o600)

    for _ in range(2):
        with pytest.raises(CorruptRegistryError):
            sweep(registry=registry)

    assert registry.path.read_text(encoding="utf-8") == "{broken"
    assert directory.path.exists()
    assert list(tmp_path.glob("registry.json.corrupt-*"))


def test_malformed_entry_blocks_repeated_sweeps_without_orphaning_valid_entries(
    tmp_path,
):
    registry = Registry(path=tmp_path / "registry.json")
    first = tempdir(parent=tmp_path / "work", registry=registry)
    second = tempdir(parent=tmp_path / "work", registry=registry)
    state = registry.load(read_only=True)
    state[str(first.path)]["marker_id"] = "broken"
    registry.path.write_text(json.dumps(state), encoding="utf-8")
    registry.path.chmod(0o600)
    original = registry.path.read_bytes()

    for _ in range(2):
        with pytest.raises(CorruptRegistryError):
            sweep(registry=registry)

    assert registry.path.read_bytes() == original
    assert first.path.exists()
    assert second.path.exists()
    quarantines = list(tmp_path.glob("registry.json.corrupt-*"))
    assert quarantines
    assert quarantines[0].read_bytes() == original


def test_tempdir_after_corrupt_registry_does_not_create_directory(tmp_path):
    registry = Registry(path=tmp_path / "registry.json")
    parent = tmp_path / "work"
    parent.mkdir()
    registry.path.parent.mkdir(exist_ok=True)
    registry.path.write_text("{broken", encoding="utf-8")
    registry.path.chmod(0o600)

    with pytest.raises(CorruptRegistryError):
        tempdir(parent=parent, registry=registry)

    assert list(parent.iterdir()) == []
    assert registry.path.read_text(encoding="utf-8") == "{broken"


def test_malformed_entry_blocks_entire_registry(tmp_path):
    path = tmp_path / "registry.json"
    good = str(tmp_path / "good")
    payload = {
        good: {"created_at": 1.0, "expires_at": None, "remove_on_restart": True},
        "relative/path": {"created_at": 1.0},
        str(tmp_path / "bad-types"): {"expires_at": "tomorrow"},
        str(tmp_path / "not-a-dict"): "zap",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CorruptRegistryError, match="relative/path"):
        Registry(path=path).load(read_only=True)

    original = path.read_bytes()
    with pytest.raises(CorruptRegistryError):
        with Registry(path=path).transaction():
            pass
    quarantines = list(tmp_path.glob("registry.json.corrupt-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == original
    assert path.read_bytes() == original


def test_empty_registry_entry_is_rejected(tmp_path):
    # Audit scenario: {"~/Documents/important": {}} must never reach the
    # sweeper as a trusted markerless entry.
    path = tmp_path / "registry.json"
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "doc.txt").write_text("data")
    path.write_text(json.dumps({str(victim): {}}), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CorruptRegistryError):
        Registry(path=path).load(read_only=True)
    assert (victim / "doc.txt").read_text() == "data"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_registry_save_does_not_follow_predictable_tmp_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite", encoding="utf-8")
    legacy_tmp = tmp_path / "registry.json.tmp"
    legacy_tmp.symlink_to(victim)

    reg = Registry(path=tmp_path / "registry.json")
    payload = {
        str(tmp_path / "owned"): {
            "created_at": 1.0,
            "expires_at": None,
            "remove_on_restart": True,
        }
    }
    reg.save(payload)

    assert victim.read_text(encoding="utf-8") == "do not overwrite"
    assert legacy_tmp.is_symlink()
    assert reg.load() == payload


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_registry_save_rejects_symlinked_data_directory(tmp_path):
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    linked_data = tmp_path / "linked-data"
    linked_data.symlink_to(real_data, target_is_directory=True)

    reg = Registry(path=linked_data / "registry.json")
    with pytest.raises((OSError, NotADirectoryError, PermissionError)):
        reg.save({})


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_registry_load_rejects_symlinked_state_ancestor(tmp_path):
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    linked_data = tmp_path / "linked-data"
    linked_data.symlink_to(real_data, target_is_directory=True)

    reg = Registry(path=linked_data / "state" / "registry.json")
    with pytest.raises(registry_module.RegistryUnavailableError):
        reg.load(read_only=True)
    assert not (real_data / "state" / "registry.json").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_registry_transaction_rejects_symlinked_state_ancestor(tmp_path):
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    linked_data = tmp_path / "linked-data"
    linked_data.symlink_to(real_data, target_is_directory=True)

    reg = Registry(path=linked_data / "state" / "registry.json")
    with pytest.raises(PermissionError):
        with reg.transaction():
            pass
    assert not (real_data / "state" / "registry.json").exists()


def test_tempdir_leaves_owned_orphan_when_registration_fails(tmp_path, registry, monkeypatch):
    parent = tmp_path / "work"
    real_save = Registry.save

    def failing_save(self, state, **kwargs):
        if state:  # let the initial empty sweep through, fail the registration
            raise OSError("disk full")
        return real_save(self, state, **kwargs)

    monkeypatch.setattr(Registry, "save", failing_save)
    with pytest.raises(OSError, match="disk full"):
        tempdir(parent=parent, registry=registry)
    leftovers = list(parent.iterdir())
    assert len(leftovers) == 1
    assert (leftovers[0] / _MARKER_NAME).is_file()

def test_deletion_guard_blocks_catastrophic_paths():
    assert _deletion_guard(Path(Path.home().anchor)) is not None  # filesystem root
    assert _deletion_guard(Path.home()) is not None
    assert _deletion_guard(Path.home().parent) is not None
    assert _deletion_guard(Path("relative")) is not None
    assert _deletion_guard(Path.home() / "projects" / "scratch-dir") is None


def test_deletion_guard_normalizes_traversal():
    # `..` segments must not smuggle a critical directory past the guard.
    assert _deletion_guard(Path.home() / "child" / "..") is not None
    assert _deletion_guard(Path.home() / "a" / ".." / "b" / "..") is not None
    assert _deletion_guard(user_data_dir() / "sub" / "..") is not None


def test_infinite_number_in_registry_entry_is_rejected(tmp_path):
    # JSON `1e999` parses to float infinity without triggering parse_constant;
    # such an entry must be dropped, or every later save would crash on
    # allow_nan=False and brick the registry.
    path = tmp_path / "registry.json"
    payload = '{"%s": {"created_at": 1e999, "expires_at": null, "remove_on_restart": true}}' % (
        tmp_path / "x"
    )
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    reg = Registry(path=path)

    with pytest.raises(CorruptRegistryError):
        reg.load(read_only=True)
    original = path.read_bytes()
    with pytest.raises(CorruptRegistryError):
        with reg.transaction():
            pass
    quarantines = list(tmp_path.glob("registry.json.corrupt-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == original
    assert path.read_bytes() == original


def test_stale_handle_cannot_extend_replacement_directory(tmp_path, registry):
    from ephemdir.core import _write_marker

    d1 = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    # The directory is deleted and "d2" appears at the same path with its own
    # marker and registry entry.
    shutil.rmtree(d1.path)
    d1.path.mkdir()
    new_marker = _write_marker(d1.path)
    with registry.transaction() as state:
        state[str(d1.path)]["marker_id"] = new_marker

    with pytest.raises(OSError):
        d1.extend("9h")
    with pytest.raises(OSError):
        d1.keep()


def test_transient_fs_error_does_not_drop_entry(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    real_lstat = os.lstat

    def flaky(target, *args, **kwargs):
        if str(target) == str(d.path):
            raise PermissionError("EACCES")  # e.g. an unreachable mount
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr("ephemdir.core.os.lstat", flaky)
    assert sweep(registry=registry) == 0
    # The entry survives the transient error instead of being dropped as stale.
    assert str(d.path) in registered(registry=registry)
    monkeypatch.undo()
    assert sweep(registry=registry) == 1  # once reachable again, swept normally


def test_resolve_stale_snapshot_does_not_delete_new_entry(tmp_path, registry, monkeypatch):
    path = tmp_path / "reused-name"
    old_entry = {
        "created_at": 1.0,
        "expires_at": None,
        "remove_on_restart": False,
        "keep_while_in_use": False,
        "marker_id": "a" * 32,
        "state": "active",
        "claim_id": None,
        "staging_path": None,
    }
    new_entry = {**old_entry, "marker_id": "b" * 32, "created_at": 2.0}
    with registry.transaction() as state:
        state[str(path)] = dict(old_entry)

    real_path_state = core._path_state
    first_probe = True

    def racing_path_state(probed):
        nonlocal first_probe
        if probed == path and first_probe:
            first_probe = False
            path.mkdir()
            with registry.transaction() as state:
                state[str(path)] = dict(new_entry)
            return "missing"
        return real_path_state(probed)

    monkeypatch.setattr(core, "_path_state", racing_path_state)
    with pytest.raises(LookupError, match="changed while resolving"):
        core.resolve(path.name, registry=registry)

    assert registered(registry=registry)[str(path)] == new_entry
    assert path.is_dir()


def test_keep_extend_remove_refuse_changed_entry_after_resolve(tmp_path, registry, monkeypatch):
    path = tmp_path / "same-name"
    path.mkdir()
    old_entry = {
        "created_at": 1.0,
        "expires_at": None,
        "remove_on_restart": False,
        "keep_while_in_use": False,
        "marker_id": "c" * 32,
        "state": "active",
        "claim_id": None,
        "staging_path": None,
    }
    new_entry = {**old_entry, "marker_id": "d" * 32, "created_at": 2.0}
    with registry.transaction() as state:
        state[str(path)] = dict(new_entry)

    monkeypatch.setattr(
        core,
        "_resolve_active",
        lambda target, *, registry: (path, dict(old_entry)),
    )

    with pytest.raises(LookupError):
        keep(path.name, registry=registry)
    with pytest.raises(LookupError):
        extend(path.name, "2h", registry=registry)
    with pytest.raises(OSError, match="changed concurrently"):
        remove(path.name, registry=registry)
    assert registered(registry=registry)[str(path)] == new_entry


def test_sweep_refuses_poisoned_entry_for_data_dir(registry):
    # A hand-edited registry pointing at ephemdir's own data directory must
    # never be honoured, even with --force.
    target = user_data_dir()  # isolated by the autouse env fixture
    with registry.transaction() as state:
        state[str(target)] = {
            "created_at": time.time(),
            "expires_at": time.time() - 1,
            "remove_on_restart": True,
            "keep_while_in_use": False,
        }
    assert sweep(registry=registry, force=True) == 0
    assert target.exists()
    assert str(target) in registered(registry=registry)  # unsafe entry stays blocked


# --- The deleting flag must never bypass ownership ----------------------------

def test_deleting_state_does_not_bypass_ownership(tmp_path, registry):
    # Audit scenario: a crafted mid-deletion entry pointing at a normal user
    # directory must not be treated as "ours" and recursively deleted.
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "doc.txt").write_text("data")

    # Variant A: the entry's key IS the victim (original and "staging" both
    # exist) -> ambiguous, parked as recovery, nothing deleted.
    with pytest.raises(ValueError):
        with registry.transaction() as state:
            state[str(victim)] = {
                "created_at": 0.0,
                "expires_at": None,
                "remove_on_restart": False,
                "state": "deleting",
                "claim_id": None,
                "staging_path": str(victim),
            }
    assert (victim / "doc.txt").read_text() == "data"

    # Variant B: staging_path points at the victim from a bogus key -> the
    # victim's name does not match ephemdir's staging shape -> left alone.
    with pytest.raises(ValueError):
        with registry.transaction() as state:
            state.clear()
            state[str(tmp_path / "nonexistent")] = {
                "created_at": 0.0,
                "expires_at": None,
                "remove_on_restart": False,
                "state": "deleting",
                "claim_id": None,
                "staging_path": str(victim),
            }
    assert (victim / "doc.txt").read_text() == "data"


def test_replaced_staging_leftover_is_not_deleted(tmp_path, registry, monkeypatch):
    # Create a genuine leftover, then replace it manually: the recorded inode
    # no longer matches, so the retry must leave the newcomer alone.
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    real_replace = os.replace

    def _replace(src, dst):
        if ".deleting" in str(dst):
            return real_replace(src, dst)
        if ".deleting" in str(src):
            raise OSError("cannot restore")
        return real_replace(src, dst)

    def _fail_rmtree(path, *args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr("ephemdir.core.os.replace", _replace)
    monkeypatch.setattr("ephemdir.core._delete_staging_tree", _fail_rmtree)
    assert sweep(registry=registry) == 0
    monkeypatch.undo()

    entry = registered(registry=registry)[str(d.path)]
    leftover = Path(entry["staging_path"])
    shutil.rmtree(leftover)
    leftover.mkdir()  # someone else reuses the staging path
    (leftover / "innocent.txt").write_text("bystander")

    assert sweep(registry=registry, force=True) == 0
    assert (leftover / "innocent.txt").read_text() == "bystander"
    assert registered(registry=registry)[str(d.path)]["state"] == "recovery"


def test_copied_marker_cannot_override_staging_inode_mismatch(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    entry = registered(registry=registry)[str(d.path)]
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    os.replace(d.path, staging)
    marker = (staging / _MARKER_NAME).read_text(encoding="utf-8")
    shutil.rmtree(staging)
    staging.mkdir()
    (staging / _MARKER_NAME).write_text(marker, encoding="utf-8")
    (staging / "innocent.txt").write_text("foreign", encoding="utf-8")

    assert core._staging_ownership(d.path, staging, entry) == "foreign"
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "deleting", "claim_id": None, "staging_path": str(staging)}
        )

    assert sweep(registry=registry, force=True) == 0
    assert (staging / "innocent.txt").read_text(encoding="utf-8") == "foreign"
    assert registered(registry=registry)[str(d.path)]["state"] == "recovery"


@pytest.mark.skipif(os.name != "posix", reason="POSIX dir_fd deletion semantics")
def test_staging_replacement_during_fd_bound_delete_is_not_deleted(
    tmp_path,
    registry,
    monkeypatch,
):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.txt").write_text("owned", encoding="utf-8")
    _expire(registry, d.path)
    real_open_verified = core._open_verified_staging
    moved_owned_tree = tmp_path / "owned-moved-away"

    def open_then_replace_staging(staging, entry):
        fd = real_open_verified(staging, entry)
        os.replace(staging, moved_owned_tree)
        staging.mkdir()
        (staging / "innocent.txt").write_text("foreign", encoding="utf-8")
        return fd

    monkeypatch.setattr(core, "_open_verified_staging", open_then_replace_staging)

    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    staging = Path(entry["staging_path"])
    assert entry["state"] == "recovery"
    assert (staging / "innocent.txt").read_text(encoding="utf-8") == "foreign"
    assert moved_owned_tree.is_dir()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mount boundary semantics")
def test_mount_boundary_mismatch_is_not_traversed(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    mounted = d.path / "mounted"
    mounted.mkdir()
    (mounted / "sentinel.txt").write_text("external", encoding="utf-8")
    _expire(registry, d.path)

    mount_ids = iter([100, 100, 200])
    monkeypatch.setattr(core.sys, "platform", "linux")
    with registry.transaction() as state:
        state[str(d.path)]["platform"] = "linux"
    monkeypatch.setattr(core, "_mount_id_for_fd", lambda fd: next(mount_ids, 200))

    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "deleting"
    assert (Path(entry["staging_path"]) / "mounted" / "sentinel.txt").read_text(
        encoding="utf-8"
    ) == "external"


def test_foreign_platform_journal_is_not_recovered(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    os.rename(d.path, staging)
    with registry.transaction() as state:
        entry = state[str(d.path)]
        entry.update(
            {
                "state": "deleting",
                "claim_id": None,
                "staging_path": str(staging),
                "platform": "win32",
                "backend": "windows",
            }
        )

    assert sweep(registry=registry) == 0

    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "deleting"
    assert entry["platform"] == "win32"
    assert staging.exists()


def test_non_posix_delete_path_fails_closed(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "sentinel.txt").write_text("keep", encoding="utf-8")
    _expire(registry, d.path)
    monkeypatch.setattr(core.os, "supports_dir_fd", frozenset())

    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "active"
    assert entry["staging_path"] is None
    assert (d.path / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(f".{d.path.name}.*.deleting"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_untrusted_non_sticky_parent_is_rejected(tmp_path, registry):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o777)

    with pytest.raises(PermissionError, match="without sticky bit"):
        tempdir(parent=parent, registry=registry)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_foreign_owned_sticky_parent_is_rejected_by_policy(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    real = parent.stat()
    values = list(real)
    values[0] = stat.S_IFDIR | 0o1777
    values[4] = os.geteuid() + 1
    synthetic = os.stat_result(values)

    assert core._trusted_component_error(parent, synthetic) is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_root_owned_sticky_parent_is_accepted_by_policy(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    real = parent.stat()
    values = list(real)
    values[0] = stat.S_IFDIR | 0o1777
    values[4] = 0
    synthetic = os.stat_result(values)

    assert core._trusted_component_error(parent, synthetic) is None
    assert core._trusted_final_parent_error(parent, synthetic) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_claim_refuses_parent_that_became_untrusted(tmp_path, registry):
    parent = tmp_path / "work"
    parent.mkdir()
    separate_registry = Registry(path=tmp_path / "data" / "registry.json")
    d = tempdir(lifetime="1h", parent=parent, registry=separate_registry)
    _expire(separate_registry, d.path)
    parent.chmod(0o777)

    assert sweep(registry=separate_registry) == 0
    assert d.path.is_dir()
    assert registered(registry=separate_registry)[str(d.path)]["state"] == "active"


def test_non_oserror_delete_failure_stays_retryable(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    def fail_with_recursion_error(staging, entry):
        raise RecursionError("too deep")

    monkeypatch.setattr(core, "_delete_staging_tree", fail_with_recursion_error)
    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "deleting"
    assert "too deep" in entry["last_error"]


# --- Journaled two-phase claim: crash recovery ---------------------------------

def _journal_intent(registry, path, staging):
    """Simulate the durable 'moving' intent as written by phase 1 of a claim."""
    with registry.transaction() as state:
        state[str(path)].update(
            {"state": "moving", "claim_id": "5" * 32, "staging_path": str(staging)}
        )


def test_crash_between_rename_and_commit_is_recovered(tmp_path, registry):
    # Audit blocker: the process dies after os.replace() but before the
    # registry commit. The journal must let the next sweep finish the job.
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.bin").write_bytes(b"x" * 64)
    _expire(registry, d.path)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    _journal_intent(registry, d.path, staging)
    os.replace(d.path, staging)  # ...and "crash" here

    assert sweep(registry=registry) == 1
    assert not staging.exists()
    assert not d.path.exists()
    assert registered(registry=registry) == {}


def test_crash_before_rename_recovers_to_active(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    _journal_intent(registry, d.path, staging)  # intent journaled, no rename, "crash"

    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "active"
    assert entry["staging_path"] is None
    assert d.path.is_dir()


def test_recovery_refuses_when_both_paths_exist(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    _journal_intent(registry, d.path, staging)
    staging.mkdir()  # ambiguous: both original and staging exist
    (staging / "mystery.txt").write_text("?")

    for _ in range(2):  # repeated sweeps must stay stable
        assert sweep(registry=registry, force=True) == 0
    assert d.path.is_dir()
    assert (staging / "mystery.txt").read_text() == "?"
    assert registered(registry=registry)[str(d.path)]["state"] == "recovery"


def test_recovery_accepts_legacy_tree_returned_to_original(tmp_path, registry):
    # A legacy build may have returned a deleting tree to the original path
    # before crashing; current recovery must recognize the still-owned tree.
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "deleting", "claim_id": "6" * 32, "staging_path": str(staging)}
        )

    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "active"
    assert d.path.is_dir()


def test_recovery_drops_entry_when_nothing_left_on_disk(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    _journal_intent(registry, d.path, staging)
    shutil.rmtree(d.path)  # both original and staging are gone

    sweep(registry=registry)
    assert registered(registry=registry) == {}


# --- Per-directory deletion lock ------------------------------------------------

def test_deletion_lock_prevents_concurrent_delete(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    entry = registered(registry=registry)[str(d.path)]

    from ephemdir.core import _deletion_lock_key

    lock_key = _deletion_lock_key(str(d.path), entry)
    with registry.deletion_lock(lock_key) as acquired:
        assert acquired
        # "Another process" already deleting this directory: the sweep must
        # skip it instead of racing into the same rmtree.
        assert sweep(registry=registry) == 0
        assert d.path.is_dir()
    # Lock released: now the sweep may proceed.
    assert sweep(registry=registry) == 1
    assert not d.path.exists()


# --- Handle methods must not report false success ------------------------------

def test_handle_keep_raises_when_sweep_already_claimed(tmp_path, registry):
    from ephemdir.core import _try_claim

    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    snapshot = dict(registered(registry=registry)[str(d.path)])
    status, _, _ = _try_claim(registry, d.path, snapshot, allow_unverified=False)
    assert status == "claimed"

    with pytest.raises(LookupError):
        d.keep()


def test_handle_extend_raises_when_untracked(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    keep(d.path.name, registry=registry)

    with pytest.raises(LookupError):
        d.extend("2h")


# --- keep() must not modify directories it does not own -------------------------

def test_keep_does_not_touch_foreign_marker(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    shutil.rmtree(d.path)
    d.path.mkdir()
    (d.path / _MARKER_NAME).write_text("the user's own file")

    keep(d.path.name, registry=registry)
    assert (d.path / _MARKER_NAME).read_text() == "the user's own file"


def test_keep_does_not_modify_legacy_directory(tmp_path, registry):
    legacy = tmp_path / "old-school"
    legacy.mkdir()
    (legacy / _MARKER_NAME).write_text("preexisting user file")
    with registry.transaction() as state:
        state[str(legacy)] = {
            "created_at": time.time(),
            "expires_at": None,
            "remove_on_restart": False,
            "keep_while_in_use": False,
            "boot_time": None,
        }

    keep("old-school", registry=registry)
    assert (legacy / _MARKER_NAME).read_text() == "preexisting user file"


# --- Fail-safe in-use protection ---------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX in-use semantics")
def test_unknown_in_use_state_defers_deletion(tmp_path, registry, monkeypatch):
    # lsof missing/broken: with protection requested, err on the keeping side.
    monkeypatch.setattr("ephemdir.core.is_in_use", lambda path: None)
    d = tempdir(lifetime="1h", keep_while_in_use=True, parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    assert sweep(registry=registry) == 0
    assert d.path.is_dir()


# --- Boot session identity ---------------------------------------------------

def test_boot_id_change_means_reboot(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("ephemdir.core.boot_session_id", lambda: "boot-AAA")
    d = tempdir(parent=tmp_path, registry=registry)

    monkeypatch.setattr("ephemdir.core.boot_session_id", lambda: "boot-BBB")
    assert sweep(registry=registry) == 1
    assert not d.path.exists()


def test_clock_step_with_same_boot_id_is_not_a_reboot(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("ephemdir.core.boot_session_id", lambda: "boot-AAA")
    d = tempdir(parent=tmp_path, registry=registry)
    # The wall clock was stepped, so the derived boot time looks ancient -- but
    # the stable boot id proves the machine never rebooted.
    with registry.transaction() as state:
        state[str(d.path)]["boot_time"] = time.time() - 10_000_000

    assert sweep(registry=registry) == 0
    assert d.path.is_dir()


# --- Permissions and input validation ----------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_directories_are_owner_only(tmp_path, registry):
    old_umask = os.umask(0o022)
    try:
        d = tempdir(parent=tmp_path, registry=registry)
    finally:
        os.umask(old_umask)
    assert os.stat(d.path).st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_registry_file_is_owner_only(tmp_path):
    registry = Registry(path=tmp_path / "registry.json")
    with registry.transaction() as state:
        state[str(tmp_path / "x")] = {
            "created_at": 1.0,
            "expires_at": None,
            "remove_on_restart": True,
        }
    assert os.stat(registry.path).st_mode & 0o777 == 0o600


def test_prefix_with_path_separator_is_rejected(tmp_path, registry):
    with pytest.raises(ValueError, match="separator"):
        tempdir(prefix="../evil-", parent=tmp_path, registry=registry)


def test_words_out_of_range_rejected():
    with pytest.raises(ValueError):
        funny_name(0)
    with pytest.raises(ValueError):
        funny_name(5)
    # Single word works instead of KeyError: one word plus the random suffix.
    assert funny_name(1).count("-") == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits required")
def test_sweep_refuses_writable_registry_and_keeps_real_directory(tmp_path):
    # MED-01: a registry another local user could write to may carry a forged
    # policy. Here the victim's own directory is genuine (real marker + inode),
    # so marker/inode checks pass -- the attack is purely a policy edit (forced
    # expiry). A sweep must refuse to act on such a registry rather than delete
    # a directory the owner really created.
    registry = Registry(path=tmp_path / "registry.json")
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)

    # "Attacker" with write access edits only expires_at and leaves the file
    # group/world-writable, as an older umask or a shared data dir would.
    state = registry.load()
    state[str(d.path)]["expires_at"] = 0.0
    registry.path.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(registry.path, 0o664)

    from ephemdir._registry import UnsafeRegistryError

    with pytest.raises(UnsafeRegistryError):
        core.sweep(registry=registry)

    # The real directory survived and the registry was neither emptied nor
    # quarantined: the owner can recover it by tightening the permissions.
    assert d.path.exists()
    assert registry.path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))
