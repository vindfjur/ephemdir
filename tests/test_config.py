"""Tests for user config loading and its effect on tempdir defaults."""

from __future__ import annotations

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
