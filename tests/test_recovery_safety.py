"""Regression tests for deletion journaling and recovery safety."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from ephemdir import core, recover, registered, resolve, sweep, tempdir
from ephemdir._registry import Registry
from ephemdir._service import SYSTEMD_UNIT, render_systemd_units


def _expire(registry: Registry, path: Path) -> None:
    with registry.transaction() as state:
        state[str(path)]["expires_at"] = time.time() - 1


def test_live_moving_claim_is_not_recovered_while_deletion_lock_is_held(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    entry = registered(registry=registry)[str(d.path)]
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    lock_key = core._deletion_lock_key(str(d.path), entry)

    with registry.deletion_lock(lock_key) as acquired:
        assert acquired
        # This models a live claimant after it durably records intent and just
        # before it renames the directory.
        with registry.transaction() as state:
            state[str(d.path)].update(
                {"state": "moving", "claim_id": "live-claim", "staging_path": str(staging)}
            )
        assert sweep(registry=registry) == 0
        live = registered(registry=registry)[str(d.path)]
        assert live["state"] == "moving"
        assert live["claim_id"] == "live-claim"

    # Once the claimant is gone, recovery may safely decide that rename never
    # happened and reactivate the entry.  It is not deleted in that same pass.
    assert sweep(registry=registry) == 0
    assert registered(registry=registry)[str(d.path)]["state"] == "active"
    assert d.path.is_dir()


def test_partial_delete_never_replaces_new_original_directory(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.txt").write_text("owned")
    _expire(registry, d.path)
    replacement_inode = None

    def fail_after_replacement(staging, *args, **kwargs):
        nonlocal replacement_inode
        d.path.mkdir()
        (d.path / "innocent.txt").write_text("foreign")
        replacement_inode = d.path.stat().st_ino
        raise OSError("simulated partial delete")

    monkeypatch.setattr(core, "_delete_staging_tree", fail_after_replacement)
    assert sweep(registry=registry) == 0

    assert d.path.stat().st_ino == replacement_inode
    assert (d.path / "innocent.txt").read_text() == "foreign"
    entry = registered(registry=registry)[str(d.path)]
    staging = Path(entry["staging_path"])
    assert entry["state"] == "deleting"
    assert (staging / "payload.txt").read_text() == "owned"


def test_registration_failure_does_not_delete_replacement(tmp_path, registry, monkeypatch):
    parent = tmp_path / "work"
    real_save = Registry.save
    replacement: Path | None = None

    def fail_registration(self, state):
        nonlocal replacement
        if state:
            path = Path(next(iter(state)))
            shutil.rmtree(path)
            path.mkdir()
            (path / "innocent.txt").write_text("foreign")
            replacement = path
            raise OSError("disk full")
        return real_save(self, state)

    monkeypatch.setattr(Registry, "save", fail_registration)
    with pytest.raises(OSError, match="disk full"):
        tempdir(parent=parent, registry=registry)
    assert replacement is not None
    assert (replacement / "innocent.txt").read_text() == "foreign"


def test_recovery_deletes_owned_staging_but_leaves_foreign_original(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.txt").write_text("owned")
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "moving", "claim_id": "dead", "staging_path": str(staging)}
        )
    os.replace(d.path, staging)
    d.path.mkdir()
    (d.path / "innocent.txt").write_text("foreign")

    assert sweep(registry=registry) == 1
    assert (d.path / "innocent.txt").read_text() == "foreign"
    assert not staging.exists()
    assert str(d.path) not in registered(registry=registry)


def test_recover_forget_never_touches_either_path(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    staging.mkdir()
    (staging / "mystery.txt").write_text("keep")
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "recovery", "claim_id": None, "staging_path": str(staging)}
        )

    assert recover(d.path.name, action="forget", registry=registry) == d.path
    assert d.path.is_dir()
    assert (staging / "mystery.txt").read_text() == "keep"
    assert str(d.path) not in registered(registry=registry)


def test_prefix_rejects_control_characters(tmp_path, registry):
    with pytest.raises(ValueError, match="control"):
        tempdir(parent=tmp_path, prefix="line\n", registry=registry)


def test_linux_without_boot_id_does_not_trust_wall_clock(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "linux")
    entry = {
        "created_at": 1.0,
        "expires_at": None,
        "remove_on_restart": True,
        "boot_id": None,
        "boot_time": 1.0,
    }
    assert not core._is_due(entry, now=10_000_000.0, current_boot=9_000_000.0, current_boot_id=None)


def test_staging_path_must_be_derived_from_original(tmp_path):
    original = tmp_path / "brave-otter"
    elsewhere = tmp_path / ".other.123-abcdef12.deleting"
    assert not core._valid_staging_path(original, elsewhere)
    valid = tmp_path / ".brave-otter.123-abcdef12.deleting"
    assert core._valid_staging_path(original, valid)


def test_resolve_unknown_probe_preserves_entry(tmp_path, registry, monkeypatch):
    d = tempdir(parent=tmp_path, registry=registry)
    monkeypatch.setattr(core, "_path_state", lambda path: "unknown")
    with pytest.raises(LookupError, match="temporarily inaccessible"):
        resolve(d.path.name, registry=registry)
    assert str(d.path) in registered(registry=registry)


def test_deletion_locks_use_fixed_stripes(tmp_path):
    registry = Registry(path=tmp_path / "data" / "registry.json")
    for index in range(400):
        with registry.deletion_lock(f"marker-{index}") as acquired:
            assert acquired
    lock_files = list((registry.path.parent / "locks").glob("delete-*.lock"))
    assert len(lock_files) <= 256


def test_systemd_escaping_handles_percent_and_quotes():
    units = render_systemd_units(60, ['/opt/100% ready/ephem"dir', "sweep"])
    service = units[f"{SYSTEMD_UNIT}.service"]
    assert "100%% ready" in service
    assert '\\"dir' in service


def test_recover_forget_cannot_race_live_claim(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    entry = registered(registry=registry)[str(d.path)]
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "moving", "claim_id": "live", "staging_path": str(staging)}
        )
    lock_key = core._deletion_lock_key(str(d.path), entry)
    with registry.deletion_lock(lock_key) as acquired:
        assert acquired
        with pytest.raises(OSError, match="currently being processed"):
            recover(d.path.name, action="forget", registry=registry)
    assert str(d.path) in registered(registry=registry)
    assert d.path.is_dir()


def test_journal_recovery_applies_deletion_guard(tmp_path, registry, monkeypatch):
    d = tempdir(parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "moving", "claim_id": "dead", "staging_path": str(staging)}
        )
    monkeypatch.setattr(core, "_deletion_guard", lambda path: "test critical path")
    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "recovery"
    assert d.path.is_dir()
