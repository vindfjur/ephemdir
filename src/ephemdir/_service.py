"""Install a recurring ``ephemdir sweep`` as a per-user OS service.

Each platform uses its native scheduler:

* macOS:   a LaunchAgent (``launchctl``)
* Linux:   a systemd user service + timer (``systemctl --user``)
* Windows: a Scheduled Task (``schtasks``)

The rendering of unit files is kept separate from the side effects so it can be
tested without touching the real system.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from ._platform import user_config_dir

__all__ = ["install_service", "uninstall_service", "sweep_command"]

logger = logging.getLogger("ephemdir")

# Identifiers reused across platforms.
LAUNCHD_LABEL = "com.vindfjur.ephemdir.sweep"
SYSTEMD_UNIT = "ephemdir-sweep"
WINDOWS_TASK = "ephemdir-sweep"


def sweep_command() -> list[str]:
    """Return the argv that runs ``ephemdir sweep`` reliably from a service.

    Prefers the installed console script; otherwise falls back to
    ``python -m ephemdir`` so it works even when the script is not on PATH.
    """
    script = shutil.which("ephemdir")
    base = [script] if script else [sys.executable, "-m", "ephemdir"]
    return base + ["sweep"]


# --- macOS (launchd) -------------------------------------------------------

def render_launchd_plist(interval: int, command: list[str]) -> str:
    """Render a LaunchAgent plist that runs ``command`` every ``interval`` s."""
    args = "\n".join(f"        <string>{arg}</string>" for arg in command)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>StartInterval</key>\n"
        f"    <integer>{interval}</integer>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _install_launchd(interval: int) -> str:
    path = _launchd_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_launchd_plist(interval, sweep_command()), encoding="utf-8")
    # Reload so an existing agent picks up the new definition.
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
    subprocess.run(["launchctl", "load", str(path)], capture_output=True, check=False)
    return f"installed LaunchAgent at {path} (sweeps every {interval}s)"


def _uninstall_launchd() -> str:
    path = _launchd_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
        path.unlink()
        return f"removed LaunchAgent {path}"
    return "no LaunchAgent installed"


# --- Linux (systemd user units) -------------------------------------------

def render_systemd_units(interval: int, command: list[str]) -> dict[str, str]:
    """Render the systemd ``.service`` and ``.timer`` unit file contents."""
    exec_start = " ".join(command)
    service = (
        "[Unit]\n"
        "Description=ephemdir scheduled cleanup\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run ephemdir cleanup periodically\n\n"
        "[Timer]\n"
        f"OnBootSec={interval}\n"
        f"OnUnitActiveSec={interval}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{SYSTEMD_UNIT}.service": service, f"{SYSTEMD_UNIT}.timer": timer}


def _systemd_dir() -> Path:
    return user_config_dir().parent / "systemd" / "user"


def _install_systemd(interval: int) -> str:
    units_dir = _systemd_dir()
    units_dir.mkdir(parents=True, exist_ok=True)
    for name, content in render_systemd_units(interval, sweep_command()).items():
        (units_dir / name).write_text(content, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"],
        capture_output=True,
        check=False,
    )
    return f"installed systemd user timer in {units_dir} (sweeps every {interval}s)"


def _uninstall_systemd() -> str:
    units_dir = _systemd_dir()
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"],
        capture_output=True,
        check=False,
    )
    removed = False
    for name in (f"{SYSTEMD_UNIT}.service", f"{SYSTEMD_UNIT}.timer"):
        unit = units_dir / name
        if unit.exists():
            unit.unlink()
            removed = True
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    if removed:
        return f"removed systemd user timer from {units_dir}"
    return "no systemd timer installed"


# --- Windows (Task Scheduler) ---------------------------------------------

def _install_windows(interval: int) -> str:
    minutes = max(1, interval // 60)
    command = " ".join(sweep_command())
    subprocess.run(
        [
            "schtasks", "/Create", "/F",
            "/TN", WINDOWS_TASK,
            "/TR", command,
            "/SC", "MINUTE",
            "/MO", str(minutes),
        ],
        capture_output=True,
        check=False,
    )
    return f"installed scheduled task {WINDOWS_TASK!r} (sweeps every {minutes} min)"


def _uninstall_windows() -> str:
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"],
        capture_output=True,
        check=False,
    )
    return (
        f"removed scheduled task {WINDOWS_TASK!r}"
        if result.returncode == 0
        else "no scheduled task installed"
    )


# --- Public dispatch -------------------------------------------------------

def install_service(interval: int = 600) -> str:
    """Install the periodic sweep service for the current platform."""
    if interval < 1:
        raise ValueError("interval must be >= 1 second")
    if sys.platform == "darwin":
        return _install_launchd(interval)
    if sys.platform == "win32":
        return _install_windows(interval)
    return _install_systemd(interval)


def uninstall_service() -> str:
    """Remove the periodic sweep service for the current platform."""
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform == "win32":
        return _uninstall_windows()
    return _uninstall_systemd()
