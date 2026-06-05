"""Cross-platform helpers: user data directory and system boot time.

This module isolates every OS-specific branch so the rest of the package can
stay platform-agnostic. It supports Linux, macOS and Windows without any
third-party dependency.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

__all__ = ["user_data_dir", "user_config_dir", "boot_time", "same_boot"]

# Two boot-time readings within this many seconds are treated as the same boot.
# Boot time derived from uptime can jitter slightly between reads, so we never
# compare it for exact equality.
_BOOT_TOLERANCE_SECONDS = 10.0


def user_data_dir(app_name: str = "ephemdir") -> Path:
    """Return the per-user data directory for ``app_name``.

    Follows the platform conventions:

    * Windows: ``%LOCALAPPDATA%\\<app_name>``
    * macOS:   ``~/Library/Application Support/<app_name>``
    * Linux:   ``$XDG_DATA_HOME/<app_name>`` or ``~/.local/share/<app_name>``

    The directory is created if it does not exist.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"

    path = root / app_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_config_dir(app_name: str = "ephemdir") -> Path:
    """Return the per-user configuration directory for ``app_name``.

    Follows the platform conventions:

    * Windows: ``%APPDATA%\\<app_name>`` (roaming)
    * macOS:   ``~/Library/Application Support/<app_name>``
    * Linux:   ``$XDG_CONFIG_HOME/<app_name>`` or ``~/.config/<app_name>``

    The directory is created if it does not exist.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"

    path = root / app_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def boot_time() -> float | None:
    """Return the system boot time as a Unix timestamp, or ``None`` if unknown.

    The value is used only to detect whether the machine has been rebooted
    since a directory was registered, so a best-effort estimate is enough.
    """
    if sys.platform == "win32":
        return _boot_time_windows()
    if sys.platform == "darwin":
        return _boot_time_macos()
    return _boot_time_linux()


def same_boot(a: float | None, b: float | None) -> bool:
    """Return ``True`` if two boot timestamps refer to the same boot session.

    If either value is unknown we conservatively assume the same boot, so that
    a directory is never wiped just because boot time could not be read.
    """
    if a is None or b is None:
        return True
    return abs(a - b) <= _BOOT_TOLERANCE_SECONDS


def _boot_time_linux() -> float | None:
    """Read boot time from ``/proc/uptime`` (Linux and most Unixes)."""
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            uptime_seconds = float(handle.readline().split()[0])
        return time.time() - uptime_seconds
    except (OSError, ValueError, IndexError):
        return None


def _boot_time_macos() -> float | None:
    """Read the exact boot time from ``sysctl kern.boottime`` on macOS/BSD."""
    import subprocess

    try:
        output = subprocess.check_output(
            ["sysctl", "-n", "kern.boottime"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Output looks like: "{ sec = 1700000000, usec = 123456 } ..."
        marker = "sec = "
        start = output.index(marker) + len(marker)
        end = output.index(",", start)
        return float(output[start:end].strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _boot_time_windows() -> float | None:
    """Estimate boot time from the milliseconds-since-boot tick counter."""
    try:
        import ctypes

        # GetTickCount64 returns milliseconds since the system started.
        millis = int(ctypes.windll.kernel32.GetTickCount64())  # type: ignore[attr-defined]
        return time.time() - millis / 1000.0
    except (OSError, AttributeError):
        return None
