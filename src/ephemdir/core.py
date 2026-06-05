"""Core API: create, track and clean up ephemeral directories."""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional, Union

from ._naming import funny_name
from ._platform import boot_time, same_boot
from ._registry import Registry

__all__ = ["EphemeralDirectory", "tempdir", "sweep", "registered"]

logger = logging.getLogger("ephemdir")

# A lifetime may be given as seconds (int/float), a timedelta, a human string
# like "2h", "1h30m", "90s", or None for "no time limit" (restart-only).
Lifetime = Union[int, float, str, timedelta, None]

# Maximum attempts to find a free, unique directory name before giving up.
_MAX_NAME_ATTEMPTS = 100

_DURATION_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-zµ]+)", re.IGNORECASE)
_DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_lifetime(lifetime: Lifetime) -> Optional[float]:
    """Normalize a lifetime value into seconds (float) or ``None``.

    ``None`` means the directory has no time limit and is only removed on
    restart (subject to its restart policy).
    """
    if lifetime is None:
        return None
    if isinstance(lifetime, timedelta):
        return lifetime.total_seconds()
    if isinstance(lifetime, (int, float)):
        if lifetime < 0:
            raise ValueError("lifetime cannot be negative")
        return float(lifetime)
    if isinstance(lifetime, str):
        return _parse_duration_string(lifetime)
    raise TypeError(f"unsupported lifetime type: {type(lifetime).__name__!r}")


def _parse_duration_string(text: str) -> float:
    """Parse strings like ``"2h"``, ``"1h30m"`` or ``"90s"`` into seconds."""
    cleaned = text.strip().lower()
    if not cleaned:
        raise ValueError("empty lifetime string")
    total = 0.0
    matched_span = 0
    for match in _DURATION_PATTERN.finditer(cleaned):
        unit = match.group("unit")
        if unit not in _DURATION_UNITS:
            raise ValueError(f"unknown duration unit: {unit!r}")
        total += float(match.group("value")) * _DURATION_UNITS[unit]
        # Count consumed characters ignoring the optional space between number
        # and unit, so "1 day" validates the same as "1day".
        matched_span += len(match.group("value")) + len(match.group("unit"))
    # Reject input that contained stray characters we did not understand.
    if matched_span != len(cleaned.replace(" ", "")):
        raise ValueError(f"could not parse lifetime: {text!r}")
    return total


class EphemeralDirectory:
    """A temporary directory that is tracked for automatic cleanup.

    The instance is path-like: it can be passed anywhere a path is expected
    (``os.fspath``), joined with ``/``, and used as a context manager that
    removes the directory on exit.
    """

    def __init__(
        self,
        path: Path,
        *,
        created_at: float,
        expires_at: Optional[float],
        remove_on_restart: bool,
        registry: Registry,
    ) -> None:
        self._path = path
        self._created_at = created_at
        self._expires_at = expires_at
        self._remove_on_restart = remove_on_restart
        self._registry = registry
        self._removed = False

    @property
    def path(self) -> Path:
        """The directory location as a :class:`pathlib.Path`."""
        return self._path

    @property
    def created_at(self) -> float:
        """Unix timestamp of when the directory was created."""
        return self._created_at

    @property
    def expires_at(self) -> Optional[float]:
        """Unix timestamp when the directory expires, or ``None`` if never."""
        return self._expires_at

    @property
    def remove_on_restart(self) -> bool:
        """Whether the directory is removed after a system restart."""
        return self._remove_on_restart

    def __fspath__(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return f"EphemeralDirectory({str(self._path)!r})"

    def __truediv__(self, other: Union[str, os.PathLike]) -> Path:
        # Allow ``ephemeral_dir / "sub" / "file.txt"`` like a normal Path.
        return self._path / other

    def __enter__(self) -> "EphemeralDirectory":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.remove()

    def remove(self) -> None:
        """Delete the directory now and stop tracking it. Idempotent."""
        if self._removed:
            return
        _delete_tree(self._path)
        self._unregister()
        self._removed = True
        logger.info("removed ephemeral directory %s", self._path)

    def keep(self) -> None:
        """Stop tracking the directory without deleting it.

        After this call the directory is permanent as far as ephemdir is
        concerned: it will never be auto-removed.
        """
        self._unregister()
        logger.info("released ephemeral directory %s (kept on disk)", self._path)

    def _unregister(self) -> None:
        key = str(self._path)
        with self._registry.transaction() as state:
            state.pop(key, None)


def tempdir(
    lifetime: Lifetime = None,
    *,
    remove_on_restart: bool = True,
    parent: Union[str, os.PathLike, None] = None,
    prefix: str = "",
    words: int = 2,
    registry: Optional[Registry] = None,
) -> EphemeralDirectory:
    """Create and register a new ephemeral directory.

    Parameters
    ----------
    lifetime:
        How long the directory should live. Accepts seconds (``3600``), a
        :class:`datetime.timedelta`, or a human string (``"2h"``, ``"1h30m"``).
        ``None`` (default) means no time limit — the directory lives until the
        next system restart.
    remove_on_restart:
        If ``True`` (default) the directory is removed after the machine
        reboots. Set to ``False`` to survive restarts and rely on ``lifetime``.
    parent:
        Where to create the directory. Defaults to the current working
        directory.
    prefix:
        Optional prefix prepended to the generated playful name.
    words:
        Number of words in the generated name (``2`` -> ``brave-otter``).
    registry:
        Custom :class:`Registry` (mainly for testing).

    Returns
    -------
    EphemeralDirectory
        A path-like handle to the created directory.
    """
    reg = registry or Registry()
    # Opportunistically clean up anything already due before creating more.
    sweep(registry=reg)

    expires_seconds = parse_lifetime(lifetime)
    parent_path = Path(parent) if parent is not None else Path.cwd()
    parent_path.mkdir(parents=True, exist_ok=True)

    path = _create_unique_dir(parent_path, prefix, words)
    now = time.time()
    expires_at = now + expires_seconds if expires_seconds is not None else None

    entry = {
        "created_at": now,
        "expires_at": expires_at,
        "remove_on_restart": bool(remove_on_restart),
        "boot_time": boot_time(),
    }
    with reg.transaction() as state:
        state[str(path)] = entry

    logger.info("created ephemeral directory %s", path)
    return EphemeralDirectory(
        path,
        created_at=now,
        expires_at=expires_at,
        remove_on_restart=bool(remove_on_restart),
        registry=reg,
    )


def sweep(*, registry: Optional[Registry] = None, force: bool = False) -> int:
    """Remove every tracked directory that is due for cleanup.

    A directory is removed when any of these is true:

    * its ``expires_at`` time has passed,
    * ``remove_on_restart`` is set and the machine rebooted since creation,
    * ``force`` is ``True`` (removes all tracked directories),
    * it no longer exists on disk (the entry is simply dropped).

    Returns the number of directories removed.
    """
    reg = registry or Registry()
    now = time.time()
    current_boot = boot_time()
    removed = 0

    with reg.transaction() as state:
        for key in list(state.keys()):
            entry = state[key]
            path = Path(key)

            # Drop entries whose directory has already disappeared.
            if not path.exists():
                del state[key]
                continue

            if force or _is_due(entry, now, current_boot):
                _delete_tree(path)
                if not path.exists():
                    del state[key]
                    removed += 1
                    logger.info("swept ephemeral directory %s", path)

    return removed


def registered(*, registry: Optional[Registry] = None) -> dict:
    """Return a snapshot of all currently tracked directories."""
    reg = registry or Registry()
    return reg.load()


def _is_due(entry: dict, now: float, current_boot: Optional[float]) -> bool:
    """Decide whether a registry entry should be cleaned up now."""
    expires_at = entry.get("expires_at")
    if expires_at is not None and now >= float(expires_at):
        return True
    if entry.get("remove_on_restart"):
        created_boot = entry.get("boot_time")
        # A different boot session means the machine has been restarted.
        if not same_boot(created_boot, current_boot):
            return True
    return False


def _create_unique_dir(parent: Path, prefix: str, words: int) -> Path:
    """Create a new directory with a unique playful name under ``parent``."""
    for _ in range(_MAX_NAME_ATTEMPTS):
        name = f"{prefix}{funny_name(words)}"
        candidate = parent / name
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue  # Name collision: try another playful name.
    raise RuntimeError(
        f"could not create a unique directory in {parent} after "
        f"{_MAX_NAME_ATTEMPTS} attempts"
    )


def _delete_tree(path: Path) -> None:
    """Best-effort recursive deletion that never raises on cleanup."""
    shutil.rmtree(path, ignore_errors=True)
