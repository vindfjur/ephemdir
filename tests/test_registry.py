"""Tests for the persistent registry."""

from __future__ import annotations

from ephemdir._registry import Registry


def test_load_missing_returns_empty(tmp_path):
    reg = Registry(path=tmp_path / "absent.json")
    assert reg.load() == {}


def test_load_corrupt_returns_empty(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{ this is not valid json")
    reg = Registry(path=path)
    assert reg.load() == {}


def test_save_and_load_roundtrip(registry):
    payload = {"/tmp/brave-otter": {"created_at": 1.0, "expires_at": None}}
    registry.save(payload)
    assert registry.load() == payload


def test_transaction_persists_changes(registry):
    with registry.transaction() as state:
        state["/tmp/x"] = {"created_at": 2.0}
    assert registry.load() == {"/tmp/x": {"created_at": 2.0}}


def test_transaction_releases_lock(registry):
    # Two sequential transactions must both succeed (lock is released).
    with registry.transaction() as state:
        state["a"] = {"v": 1}
    with registry.transaction() as state:
        state["b"] = {"v": 2}
    assert set(registry.load()) == {"a", "b"}
