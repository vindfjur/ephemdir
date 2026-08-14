"""Golden behaviour fixtures for the backend extraction (0.8.0, APA-BE-010).

Captured from the CURRENT (pre-extraction) code. Each scenario pins the exact
observable outcome of the claim/delete/recovery machinery: the sweep result,
the filesystem state, and the registry entry state. The backend-extraction rc
must replay this suite unchanged — any behavioural difference is a NO-GO.

Scenario numbering follows the auditor's rc2 fixture list.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from ephemdir import core, explain, plan_sweep, registered, sweep, tempdir
from ephemdir.core import _MARKER_NAME

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX backend fixtures")


def _expire(registry, path):
    with registry.transaction() as state:
        state[str(path)]["expires_at"] = time.time() - 1


def _entry(registry, path):
    return registered(registry=registry).get(str(path))


def _verdict(registry, path):
    """Normalized read-only decision: (status, reasons, blockers, allowed)."""
    decision = explain(str(path), registry=registry)
    return (
        decision.status,
        decision.reasons,
        decision.blockers,
        decision.destructive_allowed,
    )


def _entry_transcript(registry, path):
    """Normalized registry-entry state; volatile staging basenames reduce to
    presence/existence so the transcript is stable across runs."""
    entry = registered(registry=registry).get(str(path))
    if entry is None:
        return None
    staging = entry.get("staging_path")
    return {
        "state": entry.get("state", "active"),
        "staging_recorded": staging is not None,
        "staging_exists": bool(staging) and os.path.lexists(staging),
    }


# A refused claim rolls its intent back completely: state returns to
# "active" and no staging path stays journaled. The transcript pins that
# shape so the extraction cannot silently change it.
_REFUSED_ACTIVE = {"state": "active", "staging_recorded": False, "staging_exists": False}


# --- 1. happy claim -> delete transcript ------------------------------------


def test_golden_01_expired_directory_is_claimed_and_deleted(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.bin").write_bytes(b"x" * 512)
    _expire(registry, d.path)

    assert sweep(registry=registry) == 1
    assert not d.path.exists()
    assert _entry(registry, d.path) is None                 # entry fully retired
    assert not list(tmp_path.glob(".*.deleting"))           # no staging leftovers


# --- 2. symlink parent refusal ------------------------------------------------


def test_golden_02_symlinked_parent_is_refused(tmp_path, registry):
    nest = tmp_path / "nest"
    nest.mkdir(mode=0o700)
    d = tempdir(lifetime="1h", parent=nest, registry=registry)
    _expire(registry, d.path)

    os.rename(nest, tmp_path / "moved")
    os.symlink(tmp_path / "moved", nest)

    assert sweep(registry=registry) == 0
    assert (tmp_path / "moved" / d.path.name).is_dir()  # data survived
    assert not list((tmp_path / "moved").glob(".*.deleting"))
    assert not list(tmp_path.glob(".*.deleting"))
    assert _entry_transcript(registry, d.path) == _REFUSED_ACTIVE
    # METHOD-STABILIZATION CHANGE: parent trust is part of the shared
    # read-only decision, so explain/dry-run no longer promise a deletion that
    # the claim path will refuse. Data and tracking remain intact.
    assert _verdict(registry, d.path) == (
        "blocked",
        ("expired",),
        ("unsafe-parent",),
        False,
    )
    assert sweep(registry=registry, dry_run=True) == 0


# --- 3. symlink final path refusal ---------------------------------------------


def test_golden_03_symlinked_final_path_is_refused(tmp_path, registry):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("bystander", encoding="utf-8")
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    shutil.rmtree(d.path)
    os.symlink(victim, d.path)

    assert sweep(registry=registry) == 0
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "bystander"
    assert os.path.islink(d.path)  # the symlink itself is left in place
    assert not list(tmp_path.glob(".*.deleting"))
    assert _entry_transcript(registry, d.path) == _REFUSED_ACTIVE
    # A symlink where the directory used to be reads as identity-changed
    # (foreign): the recorded inode/marker no longer matches.
    assert _verdict(registry, d.path) == (
        "blocked",
        ("expired",),
        ("identity-changed",),
        False,
    )


# --- 4. marker missing -> identity-changed, preserved ---------------------------


def test_golden_04_missing_marker_is_never_auto_deleted(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "data.txt").write_text("keep me", encoding="utf-8")
    _expire(registry, d.path)
    (d.path / _MARKER_NAME).unlink()

    assert sweep(registry=registry) == 0
    assert (d.path / "data.txt").read_text(encoding="utf-8") == "keep me"
    assert _entry_transcript(registry, d.path) == _REFUSED_ACTIVE
    # GOLDEN FACT: a recorded marker_id whose marker file is gone reads as
    # identity-changed (foreign) — the stricter blocker — not merely
    # ownership-unverified (reserved for legacy entries without a marker_id).
    assert _verdict(registry, d.path) == (
        "blocked",
        ("expired",),
        ("identity-changed",),
        False,
    )


# --- 5. marker mismatch -> foreign, preserved -----------------------------------


def test_golden_05_marker_mismatch_is_foreign(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    (d.path / _MARKER_NAME).write_text("f" * 32, encoding="utf-8")

    assert sweep(registry=registry) == 0
    assert d.path.is_dir()
    assert (d.path / _MARKER_NAME).read_text(encoding="utf-8") == "f" * 32
    assert _entry_transcript(registry, d.path) == _REFUSED_ACTIVE
    assert _verdict(registry, d.path) == (
        "blocked",
        ("expired",),
        ("identity-changed",),
        False,
    )


# --- 6. replaced original (stale inode) -> preserved -----------------------------


def test_golden_06_replaced_original_is_left_alone(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    shutil.rmtree(d.path)
    d.path.mkdir()
    (d.path / "innocent.txt").write_text("newcomer", encoding="utf-8")

    assert sweep(registry=registry) == 0
    assert (d.path / "innocent.txt").read_text(encoding="utf-8") == "newcomer"
    assert _entry_transcript(registry, d.path) == _REFUSED_ACTIVE
    assert _verdict(registry, d.path) == (
        "blocked",
        ("expired",),
        ("identity-changed",),
        False,
    )


# --- 7. mount boundary crossing -> refused, parked ------------------------------


def test_golden_07_mount_boundary_is_not_traversed(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    mounted = d.path / "mounted"
    mounted.mkdir()
    (mounted / "sentinel.txt").write_text("external", encoding="utf-8")
    _expire(registry, d.path)

    mount_ids = iter([100, 100, 200])
    monkeypatch.setattr(core.sys, "platform", "linux")
    with registry.transaction() as state:
        state[str(d.path)]["platform"] = "linux"  # keep the entry compatible
    monkeypatch.setattr(core, "_mount_id_for_fd", lambda fd: next(mount_ids, 200))

    assert sweep(registry=registry) == 0
    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "deleting"  # journal preserved for retry
    assert not os.path.lexists(d.path)  # original name gone (renamed away)
    staging = Path(entry["staging_path"])
    assert staging.is_dir()  # remainder parked at staging
    sentinel = staging / "mounted" / "sentinel.txt"
    assert sentinel.read_text(encoding="utf-8") == "external"


# --- 8. mount-id unavailable on Linux -> defer ----------------------------------


def test_golden_08_mount_id_unavailable_defers(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    monkeypatch.setattr(core.sys, "platform", "linux")
    with registry.transaction() as state:
        state[str(d.path)]["platform"] = "linux"  # keep the entry compatible
    monkeypatch.setattr(core, "_mount_id_for_fd", lambda fd: None)

    assert sweep(registry=registry) == 0
    assert d.path.is_dir()
    assert (d.path / _MARKER_NAME).is_file()  # directory untouched
    assert not list(tmp_path.glob(".*.deleting"))
    # Refused before the rename: intent rolled back, nothing at staging.
    assert _entry_transcript(registry, d.path) == _REFUSED_ACTIVE


# --- 9. crash window: rename fails before staging exists -> reconciled active ---


def test_golden_09_failed_rename_reconciles_to_active(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    real_rename = os.rename

    def deny_rename(*args, **kwargs):
        if kwargs.get("src_dir_fd") is not None:
            raise OSError("simulated rename failure")
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(core.os, "rename", deny_rename)
    assert sweep(registry=registry) == 0
    monkeypatch.undo()

    entry = registered(registry=registry)[str(d.path)]
    assert entry.get("state", "active") == "active"  # intent rolled back
    assert d.path.is_dir()
    # A later sweep completes normally.
    assert sweep(registry=registry) == 1
    assert not d.path.exists()


# --- 10. crash window: partial delete after rename -> deleting, retried ---------


def test_golden_10_partial_delete_stays_journaled_and_retries(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "payload.bin").write_bytes(b"x" * 128)
    _expire(registry, d.path)

    def fail_delete(staging, entry):
        raise OSError("simulated disk error")

    monkeypatch.setattr(core, "_delete_staging_tree", fail_delete)
    assert sweep(registry=registry) == 0
    monkeypatch.undo()

    entry = registered(registry=registry)[str(d.path)]
    assert entry["state"] == "deleting"
    staging = Path(entry["staging_path"])
    assert staging.is_dir()                      # remainder parked at staging
    assert not d.path.exists()                   # original name not restored over
    # Retry finishes the interrupted deletion.
    assert sweep(registry=registry) == 1
    assert not staging.exists()
    assert _entry(registry, d.path) is None


# --- 11. deletion lock contention -> skipped, untouched --------------------------


def test_golden_11_lock_contention_skips_directory(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)
    entry = registered(registry=registry)[str(d.path)]

    with registry.deletion_lock(core._deletion_lock_key(str(d.path), entry)) as held:
        assert held is True
        assert sweep(registry=registry) == 0

    assert d.path.is_dir()
    assert _entry(registry, d.path) is not None
    assert sweep(registry=registry) == 1  # deleted once the lock is free


# --- 12. stale staging with our marker resumes; foreign staging parks ------------


def test_golden_12a_stale_staging_with_our_marker_is_finished(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    def fail_delete(staging, entry):
        raise OSError("simulated disk error")

    monkeypatch.setattr(core, "_delete_staging_tree", fail_delete)
    assert sweep(registry=registry) == 0
    monkeypatch.undo()

    # Staging still carries our marker: the retry may finish the deletion.
    entry = registered(registry=registry)[str(d.path)]
    staging = Path(entry["staging_path"])
    assert (staging / _MARKER_NAME).is_file()
    assert sweep(registry=registry) == 1
    assert not staging.exists()


def test_golden_12b_foreign_staging_is_parked_for_recovery(tmp_path, registry, monkeypatch):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    def fail_delete(staging, entry):
        raise OSError("simulated disk error")

    monkeypatch.setattr(core, "_delete_staging_tree", fail_delete)
    assert sweep(registry=registry) == 0
    monkeypatch.undo()

    entry = registered(registry=registry)[str(d.path)]
    staging = Path(entry["staging_path"])
    shutil.rmtree(staging)
    staging.mkdir()
    (staging / "innocent.txt").write_text("bystander", encoding="utf-8")

    assert sweep(registry=registry, force=True) == 0
    assert (staging / "innocent.txt").read_text(encoding="utf-8") == "bystander"
    assert _entry_transcript(registry, d.path) == {
        "state": "recovery",
        "staging_recorded": True,
        "staging_exists": True,
    }
    # Once parked in recovery the entry is out of the sweep's reach: a later
    # plain sweep never force-deletes it and the bystander stays intact.
    assert sweep(registry=registry) == 0
    assert (staging / "innocent.txt").read_text(encoding="utf-8") == "bystander"
    assert registered(registry=registry)[str(d.path)]["state"] == "recovery"


# --- 13. plan / dry-run / explain equivalence ------------------------------------


def test_golden_13_plan_dry_run_explain_equivalence(tmp_path, registry):
    # Create everything BEFORE expiring anything: tempdir() runs a lazy
    # maintenance sweep, which would otherwise remove an already-expired entry.
    due = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    alive = tempdir(lifetime="9h", parent=tmp_path, registry=registry)
    blocked = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    _expire(registry, due.path)
    _expire(registry, blocked.path)
    # A recorded marker_id whose marker is GONE reads as identity-changed
    # (foreign), not merely unverified — that is the stricter outcome.
    (blocked.path / _MARKER_NAME).unlink()

    decisions = {d.path: d for d in plan_sweep(registry=registry)}
    assert decisions[due.path].destructive_allowed is True
    assert decisions[alive.path].due is False
    assert decisions[blocked.path].due is True
    assert decisions[blocked.path].destructive_allowed is False
    assert "identity-changed" in decisions[blocked.path].blockers

    # dry-run count == number of destructive_allowed decisions
    allowed = sum(1 for d in decisions.values() if d.destructive_allowed)
    assert sweep(registry=registry, dry_run=True) == allowed == 1

    # explain() reproduces the same reasons/blockers per entry
    for path, decision in decisions.items():
        verdict = explain(str(path), registry=registry)
        assert verdict.reasons == decision.reasons
        assert verdict.blockers == decision.blockers

    # dry-run had no side effects: everything still on disk and tracked
    for d in (due, alive, blocked):
        assert d.path.is_dir()
        assert _entry(registry, d.path) is not None


# --- 14. untrusted parents: sticky /tmp-like vs group-writable -------------------


def test_golden_14_sticky_vs_group_writable_parent(tmp_path, registry):
    # GOLDEN FACT: an owner-owned parent that is sticky world-writable
    # (/tmp-like, 1777) stays claimable — the sticky bit prevents entry
    # swaps by others. An owner-owned NON-STICKY group-writable parent (0770)
    # is NOT trusted for fd-relative claims: sweep defers and preserves both
    # data and tracking until trust is restored. The refusal is mode-bit
    # policy, not an access check, so it holds under root as well (no root
    # skip). Foreign-owner refusals are pinned by the _security unit tests
    # and the root-container CI job.
    nest = tmp_path / "nest"
    nest.mkdir(mode=0o700)
    a = tempdir(lifetime="1h", parent=nest, registry=registry)
    _expire(registry, a.path)
    os.chmod(nest, 0o1777)  # sticky world-writable: claim proceeds
    assert sweep(registry=registry) == 1
    assert not a.path.exists()

    os.chmod(nest, 0o700)
    b = tempdir(lifetime="1h", parent=nest, registry=registry)
    _expire(registry, b.path)
    os.chmod(nest, 0o770)  # non-sticky group-writable: refused, deferred
    # METHOD-STABILIZATION CHANGE: the read-only decision and claim path now
    # share the same parent-trust blocker.
    assert _verdict(registry, b.path) == (
        "blocked",
        ("expired",),
        ("unsafe-parent",),
        False,
    )
    assert sweep(registry=registry, dry_run=True) == 0
    assert sweep(registry=registry) == 0
    assert b.path.is_dir()
    assert _entry_transcript(registry, b.path) == _REFUSED_ACTIVE

    os.chmod(nest, 0o700)  # trust restored: deletion completes
    assert sweep(registry=registry) == 1
    assert not b.path.exists()


# --- 15. missing parent tail is created owner-only by the create flow ------------


def test_golden_15_missing_parent_tail_is_created_owner_only(tmp_path, registry):
    # GOLDEN FACT: tempdir(parent=...) may point at a not-yet-existing parent
    # tail. The CREATE flow builds the missing components itself (via the
    # open-or-create trust walk) with owner-only 0700 modes; this is separate
    # from parent validation, which only ever accepts existing parents.
    parent = tmp_path / "new" / "missing" / "tail"

    d = tempdir(lifetime="1h", parent=parent, registry=registry)

    assert d.path.is_dir()
    assert (d.path / _MARKER_NAME).is_file()
    for component in (
        tmp_path / "new",
        tmp_path / "new" / "missing",
        parent,
    ):
        assert component.stat().st_mode & 0o7777 == 0o700
    assert _entry_transcript(registry, d.path) == {
        "state": "active",
        "staging_recorded": False,
        "staging_exists": False,
    }
    assert _verdict(registry, d.path) == ("active", (), (), False)
