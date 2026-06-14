"""Tests for managing tracked directories: resolve, keep, extend, remove, prune."""

from __future__ import annotations

import shutil
import time

import pytest

from ephemdir.core import (
    dir_status,
    extend,
    keep,
    prune,
    registered,
    remove,
    resolve,
    sweep,
    tempdir,
)


def test_resolve_by_full_path(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    assert resolve(d.path, registry=registry) == d.path


def test_resolve_by_exact_name(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    assert resolve(d.path.name, registry=registry) == d.path


def test_resolve_by_unique_prefix(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    assert resolve(d.path.name[:4], registry=registry) == d.path


def test_resolve_unknown_raises(registry):
    with pytest.raises(LookupError, match="no tracked directory"):
        resolve("does-not-exist", registry=registry)


def test_resolve_ambiguous_prefix_raises(tmp_path, registry, monkeypatch):
    names = iter(["alpha-one", "alpha-two"])
    monkeypatch.setattr("ephemdir.core.funny_name", lambda words: next(names))
    tempdir(parent=tmp_path, registry=registry)
    tempdir(parent=tmp_path, registry=registry)
    with pytest.raises(LookupError, match="ambiguous"):
        resolve("alpha", registry=registry)


def test_resolve_missing_dir_prunes_and_raises(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    shutil.rmtree(d.path)  # deleted behind ephemdir's back
    with pytest.raises(LookupError, match="no longer exists"):
        resolve(d.path.name, registry=registry)
    assert str(d.path) not in registered(registry=registry)


def test_keep_makes_directory_permanent(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    kept = keep(d.path.name, registry=registry)
    assert kept == d.path
    assert d.path.is_dir()
    assert str(d.path) not in registered(registry=registry)
    # Even a forced sweep must not touch it anymore.
    assert sweep(registry=registry, force=True) == 0
    assert d.path.is_dir()


def test_extend_sets_new_expiry(tmp_path, registry):
    d = tempdir(lifetime="1s", parent=tmp_path, registry=registry)
    extend(d.path.name, "2h", registry=registry)
    entry = registered(registry=registry)[str(d.path)]
    assert entry["expires_at"] > time.time() + 3600


def test_extend_without_lifetime_removes_limit(tmp_path, registry):
    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    extend(d.path.name, None, registry=registry)
    entry = registered(registry=registry)[str(d.path)]
    assert entry["expires_at"] is None


def test_extend_method_updates_handle(tmp_path, registry):
    d = tempdir(lifetime="1s", parent=tmp_path, registry=registry)
    d.extend("3h")
    assert d.expires_at > time.time() + 7200
    entry = registered(registry=registry)[str(d.path)]
    assert entry["expires_at"] == d.expires_at


def test_remove_deletes_now(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    (d.path / "f.txt").write_text("x")
    removed = remove(d.path.name, registry=registry)
    assert removed == d.path
    assert not d.path.exists()
    assert str(d.path) not in registered(registry=registry)


def test_prune_drops_only_stale_entries(tmp_path, registry):
    gone = tempdir(parent=tmp_path, registry=registry)
    alive = tempdir(parent=tmp_path, registry=registry)
    shutil.rmtree(gone.path)

    assert prune(registry=registry) == 1
    state = registered(registry=registry)
    assert str(gone.path) not in state
    assert str(alive.path) in state
    assert alive.path.is_dir()  # prune never touches the disk


def test_dir_status_classification(tmp_path, registry):
    # Use a real tracked directory so the ownership marker checks out, then
    # vary the entry fields to drive every status.
    d = tempdir(parent=tmp_path, registry=registry)
    entry = registered(registry=registry)[str(d.path)]
    now = time.time()

    assert dir_status(entry, tmp_path / "nope", now, None) == "missing"
    assert dir_status({**entry, "expires_at": now - 1}, d.path, now, None) == "expired"
    assert dir_status({**entry, "expires_at": now + 60}, d.path, now, None) == "expiring"
    assert dir_status({**entry, "expires_at": now + 7200}, d.path, now, None) == "active"
    assert dir_status(entry, d.path, now, None) == "until-restart"
    assert dir_status({**entry, "remove_on_restart": False}, d.path, now, None) == "kept"
    assert dir_status({**entry, "marker_id": "other-id"}, d.path, now, None) == "replaced"
    assert dir_status({**entry, "state": "deleting"}, d.path, now, None) == "deleting"
    assert dir_status({**entry, "state": "moving"}, d.path, now, None) == "deleting"
    assert dir_status({**entry, "state": "recovery"}, d.path, now, None) == "recovery"
    legacy = {key: value for key, value in entry.items() if key != "marker_id"}
    assert dir_status(legacy, d.path, now, None) == "legacy"


def test_dir_status_expired_after_reboot(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    entry = dict(registered(registry=registry)[str(d.path)])
    now = time.time()
    entry["boot_id"] = "older-boot"
    assert dir_status(entry, d.path, now, now, "current-boot") == "expired"
