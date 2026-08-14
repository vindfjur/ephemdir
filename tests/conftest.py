"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from ephemdir._registry import Registry


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path_factory, monkeypatch):
    """Point ephemdir's data and config dirs at fresh temp locations.

    This keeps every test away from the developer's real registry and
    config.toml, and lets CLI tests run against the default Registry().
    """
    base = tmp_path_factory.mktemp("ephemdir-home")
    monkeypatch.setenv("EPHEMDIR_DATA_DIR", str(base / "data"))
    monkeypatch.setenv("EPHEMDIR_CONFIG_DIR", str(base / "config"))
    # Completion installs resolve their targets through XDG; point them at
    # the sandbox so no test can ever write into the real home directory.
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg-config"))


@pytest.fixture()
def registry(tmp_path):
    """An isolated Registry backed by a temporary file."""
    return Registry(path=tmp_path / "registry.json")
