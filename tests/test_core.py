"""Tests for the core API: creation, lifetime parsing and sweeping."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from ephemdir.core import (
    EphemeralDirectory,
    parse_lifetime,
    registered,
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


def test_sweep_drops_vanished_entries(tmp_path, registry):
    import shutil

    d = tempdir(parent=tmp_path, registry=registry)
    # Remove the directory behind ephemdir's back.
    shutil.rmtree(d.path)
    sweep(registry=registry)
    assert str(d.path) not in registered(registry=registry)



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
