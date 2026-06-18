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

from ._security import ensure_private_directory
from ._trusted_exec import minimal_subprocess_env, resolve_system_executable, stable_subprocess_cwd

__all__ = ["user_data_dir", "user_config_dir", "boot_time", "boot_session_id", "same_boot"]

# Two boot-time readings within this many seconds are treated as the same boot.
# Boot time derived from uptime shifts when the wall clock is stepped (NTP,
# manual changes), so the tolerance is generous; on Linux the stable
# kernel boot_id is preferred and this fallback rarely decides anything.
_BOOT_TOLERANCE_SECONDS = 120.0


def _user_private_dir(path: Path, *, create: bool) -> Path:
    """Return an absolute private app directory, creating it safely if asked."""
    absolute = _canonical_private_dir_path(path)
    if create:
        ensure_private_directory(absolute)
    return absolute


def _canonical_private_dir_path(path: Path) -> Path:
    """Return a stable absolute app-state path without following user symlinks.

    macOS exposes ``/var`` as a root-owned system symlink to ``/private/var``.
    Shell helpers such as ``mktemp`` commonly return paths under ``/var``, but
    ephemdir's state-directory trust walk intentionally rejects symlink
    components.  Normalize that one platform alias lexically; do not call
    ``realpath`` on the whole path, because that would also resolve arbitrary
    user-controlled symlinks inside shared temporary directories.
    """
    absolute = Path(os.path.abspath(path))
    if sys.platform == "darwin":
        parts = absolute.parts
        if len(parts) >= 2 and parts[0] == "/" and parts[1] == "var":
            return Path("/private", *parts[1:])
    return absolute


def user_data_dir(app_name: str = "ephemdir", *, create: bool = True) -> Path:
    """Return the per-user data directory for ``app_name``.

    Follows the platform conventions:

    * Windows: ``%LOCALAPPDATA%\\<app_name>``
    * macOS:   ``~/Library/Application Support/<app_name>``
    * Linux:   ``$XDG_DATA_HOME/<app_name>`` or ``~/.local/share/<app_name>``

    ``EPHEMDIR_DATA_DIR`` overrides the platform location entirely (useful for
    tests and sandboxed setups). The directory is created if it does not exist.
    """
    override = os.environ.get("EPHEMDIR_DATA_DIR")
    if override:
        return _user_private_dir(Path(override), create=create)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"

    path = root / app_name
    # The registry may reference private paths, so keep the dir owner-only.
    return _user_private_dir(path, create=create)


def user_config_dir(app_name: str = "ephemdir", *, create: bool = True) -> Path:
    """Return the per-user configuration directory for ``app_name``.

    Follows the platform conventions:

    * Windows: ``%APPDATA%\\<app_name>`` (roaming)
    * macOS:   ``~/Library/Application Support/<app_name>``
    * Linux:   ``$XDG_CONFIG_HOME/<app_name>`` or ``~/.config/<app_name>``

    ``EPHEMDIR_CONFIG_DIR`` overrides the platform location entirely. The
    directory is created if it does not exist.
    """
    override = os.environ.get("EPHEMDIR_CONFIG_DIR")
    if override:
        return _user_private_dir(Path(override), create=create)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"

    path = root / app_name
    return _user_private_dir(path, create=create)


def boot_session_id() -> str | None:
    """Return a stable identifier of the current boot session, if available.

    Unlike :func:`boot_time` this does not depend on the wall clock, so
    stepping the system time (NTP, manual changes) can never make ephemdir
    believe the machine rebooted. Linux exposes
    ``/proc/sys/kernel/random/boot_id``; Windows keeps a per-boot counter in
    the registry. macOS returns ``None`` and falls back to the boot-time
    comparison (``kern.boottime`` is a stored value, not derived from
    uptime, so it is comparatively stable there).
    """
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
                return handle.read().strip() or None
        except OSError:
            return None
    if sys.platform == "win32":
        return _boot_session_id_windows()
    return None


def _boot_session_id_windows() -> str | None:  # pragma: no cover - Windows only
    """Read Windows' per-boot counter from the registry.

    ``GetTickCount64``-derived boot times shift whenever the wall clock is
    corrected, so they cannot identify a boot session reliably. The
    PrefetchParameters ``BootId`` value is incremented by the kernel on every
    boot and does not depend on the clock at all.
    """
    if sys.platform != "win32":  # also lets type checkers skip the win32 API
        return None
    try:
        import winreg

        key_path = (
            r"SYSTEM\CurrentControlSet\Control\Session Manager"
            r"\Memory Management\PrefetchParameters"
        )
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "BootId")
        return f"win-boot-{int(value)}"
    except (OSError, ValueError, TypeError, ImportError):
        return None


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
    # sysctl is resolved from trusted system directories and run without a shell.
    import subprocess  # nosec B404

    executable = resolve_system_executable("sysctl")
    if executable is None:
        return None
    try:
        output = subprocess.check_output(  # nosec B603
            [executable, "-n", "kern.boottime"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
            env=minimal_subprocess_env(),
            cwd=stable_subprocess_cwd(),
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
