"""Tests for installation diagnostics."""

from __future__ import annotations

import os

import pytest

from ephemdir._doctor import DoctorCheck, run_doctor
from ephemdir._registry import Registry


def _named(checks: list[DoctorCheck], name: str) -> DoctorCheck:
    return next(check for check in checks if check.name == name)


def test_doctor_reports_corrupt_registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{broken", encoding="utf-8")
    path.chmod(0o600)

    checks = run_doctor(registry=Registry(path=path))

    assert _named(checks, "registry").ok is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_doctor_rejects_world_readable_registry(tmp_path):
    path = tmp_path / "registry.json"
    Registry(path=path).save({})
    path.chmod(0o644)

    checks = run_doctor(registry=Registry(path=path))

    registry = _named(checks, "registry")
    assert registry.ok is False
    assert "readable by other users" in registry.message


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_doctor_rejects_symlink_data_dir(tmp_path, monkeypatch):
    real = tmp_path / "real-data"
    real.mkdir(mode=0o700)
    link = tmp_path / "data-link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("EPHEMDIR_DATA_DIR", str(link))

    checks = run_doctor(registry=Registry(path=tmp_path / "registry.json"))

    data_dir = _named(checks, "data-dir")
    assert data_dir.ok is False
    assert "is a symlink" in data_dir.message


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_doctor_rejects_symlink_data_dir_ancestor(tmp_path, monkeypatch):
    real = tmp_path / "real"
    nested = real / "ephemdir"
    nested.mkdir(parents=True)
    safe = tmp_path / "safe"
    safe.mkdir()
    link = safe / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("EPHEMDIR_DATA_DIR", str(link / "ephemdir"))

    checks = run_doctor(registry=Registry(path=tmp_path / "registry.json"))

    data_dir = _named(checks, "data-dir")
    assert data_dir.ok is False
    assert "is a symlink" in data_dir.message
