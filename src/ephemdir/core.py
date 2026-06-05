"""Core API: create, track and clean up ephemeral directories."""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Union

from ._config import load_config
from ._inuse import is_in_use
from ._naming import funny_name
from ._platform import boot_time, same_boot
from ._registry import Entry, Registry

__all__ = ["EphemeralDirectory", "tempdir", "sweep", "registered"]

logger = logging.getLogger("ephemdir")

# Sentinel marking "argument not provided" so we can tell an explicit value
# apart from a missing one and fall back to the user config / built-in default.
_UNSET = object()

# Built-in defaults, used when neither the call nor the user config specifies a
# value. Keys mirror the recognized config keys.
_DEFAULTS = {
    "lifetime": None,
    "remove_on_restart": True,
    "keep_while_in_use": False,
    "parent": None,
    "prefix": "",
    "words": 2,
}

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


def parse_lifetime(lifetime: Lifetime) -> float | None:
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
        expires_at: float | None,
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
    def expires_at(self) -> float | None:
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

    def __truediv__(self, other: str | os.PathLike[str]) -> Path:
        # Allow ``ephemeral_dir / "sub" / "file.txt"`` like a normal Path.
        return self._path / other

    def __enter__(self) -> EphemeralDirectory:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.remove()

    def remove(self) -> None:
        """Delete the directory now and stop tracking it. Idempotent."""
        if self._removed:
            return
        if not _delete_tree(self._path):
            # Explicit removal: fall back to a best-effort delete even when the
            # atomic rename failed (e.g. a locked file on Windows).
            shutil.rmtree(self._path, ignore_errors=True)
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
    lifetime: Lifetime = _UNSET,  # type: ignore[assignment]
    *,
    remove_on_restart: bool = _UNSET,  # type: ignore[assignment]
    keep_while_in_use: bool = _UNSET,  # type: ignore[assignment]
    parent: str | os.PathLike[str] | None = _UNSET,  # type: ignore[assignment]
    prefix: str = _UNSET,  # type: ignore[assignment]
    words: int = _UNSET,  # type: ignore[assignment]
    registry: Registry | None = None,
) -> EphemeralDirectory:
    """Create and register a new ephemeral directory.

    Any argument left unset falls back to the user config file (if present) and
    then to the built-in default, so per-user defaults can be configured once in
    ``config.toml`` (see :mod:`ephemdir._config`).

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
    keep_while_in_use:
        If ``True``, a sweep will not delete the directory while a process still
        has files open inside it; removal is deferred to a later sweep. Defaults
        to ``False``. On POSIX this is detected with ``lsof``; on Windows the
        atomic delete naturally fails on a locked file, so the sweep defers there
        too instead of leaving a half-deleted directory.
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
    settings = _resolve_settings(
        lifetime=lifetime,
        remove_on_restart=remove_on_restart,
        keep_while_in_use=keep_while_in_use,
        parent=parent,
        prefix=prefix,
        words=words,
    )

    reg = registry or Registry()
    # Opportunistically clean up anything already due before creating more.
    sweep(registry=reg)

    expires_seconds = parse_lifetime(settings["lifetime"])
    parent_value = settings["parent"]
    parent_path = Path(parent_value) if parent_value is not None else Path.cwd()
    parent_path.mkdir(parents=True, exist_ok=True)

    path = _create_unique_dir(parent_path, settings["prefix"], settings["words"])
    now = time.time()
    expires_at = now + expires_seconds if expires_seconds is not None else None
    remove_on_restart_value = bool(settings["remove_on_restart"])

    entry: Entry = {
        "created_at": now,
        "expires_at": expires_at,
        "remove_on_restart": remove_on_restart_value,
        "keep_while_in_use": bool(settings["keep_while_in_use"]),
        "boot_time": boot_time(),
    }
    with reg.transaction() as state:
        state[str(path)] = entry

    logger.info("created ephemeral directory %s", path)
    return EphemeralDirectory(
        path,
        created_at=now,
        expires_at=expires_at,
        remove_on_restart=remove_on_restart_value,
        registry=reg,
    )


def _resolve_settings(**overrides: object) -> dict[str, Any]:
    """Merge explicit arguments over user config over built-in defaults.

    Only arguments that differ from the ``_UNSET`` sentinel override the lower
    layers, so callers can selectively set just the options they care about.
    """
    config = load_config()
    resolved: dict[str, Any] = {}
    for key, default in _DEFAULTS.items():
        if overrides.get(key, _UNSET) is not _UNSET:
            resolved[key] = overrides[key]
        elif key in config:
            resolved[key] = config[key]
        else:
            resolved[key] = default
    return resolved


def sweep(*, registry: Registry | None = None, force: bool = False) -> int:
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

            if not (force or _is_due(entry, now, current_boot)):
                continue

            # Defer removal of an in-use directory when asked to protect it; a
            # later sweep retries once the files are released. POSIX uses lsof;
            # Windows relies on the transactional delete below to detect locks.
            if not force and entry.get("keep_while_in_use") and is_in_use(path):
                logger.info("deferring in-use ephemeral directory %s", path)
                continue

            if _delete_tree(path):
                del state[key]
                removed += 1
                logger.info("swept ephemeral directory %s", path)
            else:
                # Locked (e.g. an open file on Windows): keep tracking and retry.
                logger.info("deferring locked ephemeral directory %s", path)

    return removed


def registered(*, registry: Registry | None = None) -> dict[str, Entry]:
    """Return a snapshot of all currently tracked directories."""
    reg = registry or Registry()
    return reg.load()


def _is_due(entry: Entry, now: float, current_boot: float | None) -> bool:
    """Decide whether a registry entry should be cleaned up now."""
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, (int, float)) and now >= float(expires_at):
        return True
    if entry.get("remove_on_restart"):
        created_boot = entry.get("boot_time")
        created_boot = created_boot if isinstance(created_boot, (int, float)) else None
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


def _delete_tree(path: Path) -> bool:
    """Atomically delete a directory tree; return ``True`` if it was removed.

    The directory is first renamed to a temporary sibling and only then deleted.
    Renaming makes the operation all-or-nothing: on Windows an open file inside
    the tree makes the rename fail with a sharing violation, so we report
    failure instead of leaving a half-deleted directory behind. On POSIX the
    rename always succeeds, so in-use protection there relies on
    :func:`is_in_use` (``lsof``) checked before calling this.
    """
    if not path.exists():
        return True
    # Unique sibling name so a previous crashed attempt never clashes.
    staging = path.parent / f".{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.deleting"
    try:
        os.replace(path, staging)
    except OSError:
        # Could not take exclusive ownership (e.g. a file is open on Windows).
        return False
    shutil.rmtree(staging, ignore_errors=True)
    return not staging.exists()
