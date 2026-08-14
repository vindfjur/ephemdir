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
                {"state": "moving", "claim_id": "1" * 32, "staging_path": str(staging)}
            )
        assert sweep(registry=registry) == 0
        live = registered(registry=registry)[str(d.path)]
        assert live["state"] == "moving"
        assert live["claim_id"] == "1" * 32

    # Once the claimant is gone, recovery may safely decide that rename never
    # happened and reactivate the entry.  It is not deleted in that same pass.
    assert sweep(registry=registry) == 0
    assert registered(registry=registry)[str(d.path)]["state"] == "active"
    assert d.path.is_dir()


def test_dry_run_counts_auto_resumable_deleting_entry(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    os.rename(d.path, staging)
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "deleting", "claim_id": None, "staging_path": str(staging)}
        )

    decision = next(item for item in core.plan_sweep(registry=registry) if item.path == d.path)

    assert decision.destructive_allowed is True
    assert sweep(registry=registry, dry_run=True) == 1
    assert staging.exists()
    assert registered(registry=registry)[str(d.path)]["state"] == "deleting"


def test_dry_run_blocks_ambiguous_recovery_entry(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    os.rename(d.path, staging)
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "recovery", "claim_id": None, "staging_path": str(staging)}
        )

    decision = next(item for item in core.plan_sweep(registry=registry) if item.path == d.path)

    assert decision.destructive_allowed is False
    assert "recovery-required" in decision.blockers
    assert sweep(registry=registry, dry_run=True) == 0
    assert staging.exists()


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

    def fail_registration(self, state, **kwargs):
        nonlocal replacement
        if state:
            path = Path(next(iter(state)))
            shutil.rmtree(path)
            path.mkdir()
            (path / "innocent.txt").write_text("foreign")
            replacement = path
            raise OSError("disk full")
        return real_save(self, state, **kwargs)

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
            {"state": "moving", "claim_id": "2" * 32, "staging_path": str(staging)}
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


def test_macos_legacy_entry_detects_fast_reboot_even_with_current_boot_uuid(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "darwin")
    entry = {
        "created_at": 1.0,
        "expires_at": None,
        "remove_on_restart": True,
        "boot_id": None,
        "boot_time": 100.0,
    }

    decision = core.decide_cleanup(
        Path("/tmp/legacy"),
        entry,
        now=200.0,
        current_boot=160.0,
        current_boot_id="darwin-boot-11111111-2222-3333-4444-555555555555",
        mode=core.SweepMode.FULL,
        path_state="present",
        ownership="owned",
        parent_error=None,
        same_boot_func=core.same_boot,
    )

    assert decision.reasons == ("restarted",)


def test_macos_legacy_restart_entry_is_deleted_by_real_sweep(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime=None, parent=tmp_path, registry=registry)
    with registry.transaction() as state:
        state[str(d.path)]["boot_id"] = None
        state[str(d.path)]["boot_time"] = 100.0
        state[str(d.path)]["platform"] = "darwin"
    monkeypatch.setattr(core.sys, "platform", "darwin")
    monkeypatch.setattr(core, "boot_time", lambda: 160.0)
    monkeypatch.setattr(
        core,
        "boot_session_id",
        lambda: "darwin-boot-11111111-2222-3333-4444-555555555555",
    )

    assert sweep(registry=registry) == 1
    assert not d.path.exists()
    assert str(d.path) not in registered(registry=registry)


def test_root_only_deletion_resumes_after_marker_was_unlinked(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.txt").write_text("owned", encoding="utf-8")
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    os.rename(d.path, staging)
    (staging / "payload.txt").unlink()
    (staging / core._MARKER_NAME).unlink()
    with registry.transaction() as state:
        state[str(d.path)].update(
            {
                "state": "deleting",
                "claim_id": None,
                "staging_path": str(staging),
                "delete_phase": "root-only",
            }
        )

    assert sweep(registry=registry) == 1
    assert not staging.exists()
    assert str(d.path) not in registered(registry=registry)


def test_root_only_missing_staging_retires_completed_journal(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    shutil.rmtree(d.path)
    with registry.transaction() as state:
        state[str(d.path)].update(
            {
                "state": "deleting",
                "claim_id": None,
                "staging_path": str(staging),
                "delete_phase": "root-only",
            }
        )

    assert sweep(registry=registry) == 1
    assert str(d.path) not in registered(registry=registry)


def test_root_only_backend_requires_recorded_inode_identity(tmp_path):
    from ephemdir._backends import posix

    staging = tmp_path / ".victim.1-aabbccdd.deleting"
    staging.mkdir()
    entry = {"marker_id": "a" * 32, "delete_phase": "root-only"}

    with pytest.raises(posix.StagingIdentityError):
        posix.delete_claimed_tree(staging, entry)

    assert staging.is_dir()


def test_root_only_recovery_refuses_unexpected_new_payload(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    os.rename(d.path, staging)
    (staging / core._MARKER_NAME).unlink()
    (staging / "foreign.txt").write_text("preserve", encoding="utf-8")
    with registry.transaction() as state:
        state[str(d.path)].update(
            {
                "state": "deleting",
                "claim_id": None,
                "staging_path": str(staging),
                "delete_phase": "root-only",
            }
        )

    assert sweep(registry=registry) == 0
    assert (staging / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert registered(registry=registry)[str(d.path)]["state"] == "recovery"


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
            {"state": "moving", "claim_id": "3" * 32, "staging_path": str(staging)}
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
            {"state": "moving", "claim_id": "4" * 32, "staging_path": str(staging)}
        )
    monkeypatch.setattr(core, "_deletion_guard", lambda path: "test critical path")
    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "recovery"
    assert d.path.is_dir()


def test_legacy_recovery_entry_without_identity_fails_before_backend(
    tmp_path, registry, monkeypatch
):
    # Auditor-requested sequencing contract (rc6-hardened re-audit): a legacy
    # recovery entry lacking recorded dev/ino must be refused by write-time
    # registry validation BEFORE the deletion backend runs, leaving payload,
    # marker, staging and tracking byte-for-byte intact.
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.txt").write_text("owned", encoding="utf-8")
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    os.rename(d.path, staging)
    with registry.transaction() as state:
        entry = state[str(d.path)]
        entry.update({"state": "recovery", "claim_id": None, "staging_path": str(staging)})
        entry.pop("dev", None)
        entry.pop("ino", None)
    before = dict(registered(registry=registry)[str(d.path)])

    backend_calls: list[Path] = []
    monkeypatch.setattr(
        core, "_delete_staging_tree", lambda staging, entry: backend_calls.append(staging)
    )

    with pytest.raises(Exception, match="invalid|identity|dev"):
        recover(str(d.path), action="retry", registry=registry)

    assert backend_calls == []
    assert (staging / "payload.txt").read_text(encoding="utf-8") == "owned"
    assert (staging / core._MARKER_NAME).is_file()
    assert registered(registry=registry)[str(d.path)] == before
