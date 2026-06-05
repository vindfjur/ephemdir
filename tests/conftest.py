"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from ephemdir._registry import Registry


@pytest.fixture()
def registry(tmp_path):
    """An isolated Registry backed by a temporary file."""
    return Registry(path=tmp_path / "registry.json")
