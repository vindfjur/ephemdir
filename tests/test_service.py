"""Tests for service file rendering (no real install side effects)."""

from __future__ import annotations

from ephemdir import _service
from ephemdir._service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT,
    render_launchd_plist,
    render_systemd_units,
    sweep_command,
)


def test_sweep_command_ends_with_sweep():
    assert sweep_command()[-1] == "sweep"


def test_launchd_plist_contains_label_and_interval():
    plist = render_launchd_plist(900, ["/usr/local/bin/ephemdir", "sweep"])
    assert LAUNCHD_LABEL in plist
    assert "<integer>900</integer>" in plist
    assert "<string>sweep</string>" in plist


def test_systemd_units_have_service_and_timer():
    units = render_systemd_units(300, ["ephemdir", "sweep"])
    assert f"{SYSTEMD_UNIT}.service" in units
    assert f"{SYSTEMD_UNIT}.timer" in units
    assert "ExecStart=ephemdir sweep" in units[f"{SYSTEMD_UNIT}.service"]
    assert "OnUnitActiveSec=300" in units[f"{SYSTEMD_UNIT}.timer"]


def test_install_service_rejects_bad_interval():
    import pytest

    with pytest.raises(ValueError):
        _service.install_service(interval=0)
