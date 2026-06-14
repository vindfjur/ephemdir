"""Tests for user config loading and its effect on tempdir defaults."""

from __future__ import annotations

import os

import pytest

import ephemdir._config as config_module
from ephemdir._config import load_config
from ephemdir.core import _resolve_settings


def _write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_missing_config(tmp_path):
    assert load_config(tmp_path / "absent.toml") == {}


def test_load_recognized_keys(tmp_path):
    path = _write_config(tmp_path, 'lifetime = "6h"\nwords = 3\n')
    assert load_config(path) == {"lifetime": "6h", "words": 3}


def test_unknown_keys_are_dropped(tmp_path):
    path = _write_config(tmp_path, 'lifetime = "1h"\nbogus = 5\n')
    assert load_config(path) == {"lifetime": "1h"}


def test_corrupt_config_is_ignored(tmp_path):
    path = _write_config(tmp_path, "this is = not valid = toml")
    assert load_config(path) == {}


def test_oversized_config_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_MAX_CONFIG_BYTES", 16)
    path = _write_config(tmp_path, 'lifetime = "6h"\nwords = 2\n')
    assert load_config(path) == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_group_writable_config_is_ignored(tmp_path):
    path = _write_config(tmp_path, 'lifetime = "6h"\n')
    path.chmod(0o664)
    assert load_config(path) == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_world_writable_config_is_ignored(tmp_path):
    path = _write_config(tmp_path, 'lifetime = "6h"\n')
    path.chmod(0o646)
    assert load_config(path) == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_owner_only_config_is_accepted(tmp_path):
    path = _write_config(tmp_path, 'lifetime = "6h"\n')
    path.chmod(0o600)
    assert load_config(path) == {"lifetime": "6h"}


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_symlinked_config_is_ignored(tmp_path):
    target = _write_config(tmp_path, 'lifetime = "6h"\n')
    linked = tmp_path / "linked.toml"
    linked.symlink_to(target)
    assert load_config(linked) == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support required")
def test_fifo_config_is_rejected_without_blocking(tmp_path):
    # A FIFO with no writer blocks a plain read-only open() forever; the
    # loader must reject it via O_NONBLOCK + fstat() instead of hanging.
    fifo = tmp_path / "config.toml"
    os.mkfifo(fifo)

    import threading

    result: dict[str, object] = {}

    def load():
        result["value"] = load_config(fifo)

    worker = threading.Thread(target=load, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "load_config() blocked on a FIFO config file"
    assert result["value"] == {}


def test_wrongly_typed_keys_are_dropped(tmp_path):
    # The string "false" must never silently act as a truthy boolean.
    path = _write_config(
        tmp_path,
        'remove_on_restart = "false"\nwords = "three"\nlifetime = "2h"\n',
    )
    assert load_config(path) == {"lifetime": "2h"}


def test_explicit_argument_wins_over_config(monkeypatch):
    monkeypatch.setattr("ephemdir.core.load_config", lambda: {"prefix": "cfg-"})
    settings = _resolve_settings(prefix="explicit-")
    assert settings["prefix"] == "explicit-"


def test_config_used_when_argument_unset(monkeypatch):
    monkeypatch.setattr("ephemdir.core.load_config", lambda: {"prefix": "cfg-"})
    settings = _resolve_settings()
    assert settings["prefix"] == "cfg-"


def test_builtin_default_when_nothing_set(monkeypatch):
    monkeypatch.setattr("ephemdir.core.load_config", lambda: {})
    settings = _resolve_settings()
    assert settings["prefix"] == ""
    assert settings["remove_on_restart"] is True
