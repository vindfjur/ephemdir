"""Core API: create, track and clean up ephemeral directories."""

from __future__ import annotations

import errno
import hashlib
import logging
import math
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from ._backends import posix as _posix_backend
from ._backends.base import CreatedDirectory
from ._backends.posix import PosixBackend, StagingIdentityError
from ._config import load_config
from ._errors import SafetyError, TargetError, UnsupportedPlatformError
from ._inuse import is_in_use
from ._mounts import (
    linux_mount_id_fdinfo as _shared_linux_mount_id_fdinfo,
)
from ._mounts import (
    linux_mount_id_mountinfo as _shared_linux_mount_id_mountinfo,
)
from ._mounts import (
    linux_mount_id_statx as _shared_linux_mount_id_statx,
)
from ._naming import clean_name, funny_name
from ._platform import (
    _canonical_private_dir_path,
    boot_session_id,
    boot_time,
    same_boot,
    user_config_dir,
    user_data_dir,
)
from ._policy import CleanupDecision, CleanupPolicy, SweepMode, decide_cleanup
from ._registry import (
    _MAX_DESCRIPTION_BYTES,
    _MAX_TAGS_PER_ENTRY,
    Entry,
    Registry,
    _has_control_character,
    _valid_tag,
)
from ._security import (
    open_or_create_trusted_directory as _security_open_or_create_trusted_directory,
)
from ._security import (
    open_trusted_directory as _security_open_trusted_directory,
)
from ._security import (
    trusted_component_error as _security_trusted_component_error,
)
from ._security import (
    trusted_final_parent_error as _security_trusted_final_parent_error,
)
from ._security import (
    walk_trusted_directory as _security_walk_trusted_directory,
)
from ._size import measure_tree, parse_size
from ._stats import StatsLedger

__all__ = [
    "EphemeralDirectory",
    "tempdir",
    "sweep",
    "registered",
    "keep",
    "extend",
    "remove",
    "resolve",
    "prune",
    "recover",
    "dir_status",
    "explain",
    "plan_sweep",
    "CleanupDecision",
    "CleanupPolicy",
    "SweepMode",
    "parse_size",
]

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
    "cleanup": "auto",
    "max_size": None,
    "name_style": "secure",
}

# A lifetime may be given as seconds (int/float), a timedelta, a human string
# like "2h", "1h30m", "90s", or None for "no time limit" (restart-only).
Lifetime = int | float | str | timedelta | None

# Maximum attempts to find a free, unique directory name before giving up.
_MAX_NAME_ATTEMPTS = 100
_MAX_PREFIX_CODEPOINTS = 64

# A directory expiring within this window is reported as "expiring" by
# :func:`dir_status`, so UIs can highlight it before it disappears.
_EXPIRING_SOON_SECONDS = 15 * 60

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
        seconds = lifetime.total_seconds()
        if seconds < 0:
            raise ValueError("lifetime cannot be negative")
        return seconds
    if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool):
        value = float(lifetime)
        if not math.isfinite(value) or value < 0:
            raise ValueError("lifetime must be a finite, non-negative number")
        return value
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
    if not math.isfinite(total):
        raise ValueError(f"lifetime is too large: {text!r}")
    return total


# --- Ownership markers and deletion guards ---------------------------------
#
# ephemdir must never delete anything it did not create. Each created
# directory contains a hidden marker file with a random id that is also stored
# in the registry; before any deletion the two are compared. If the directory
# was deleted manually and something else later appeared at the same path, the
# marker will not match and ephemdir leaves the newcomer alone.

# The single active POSIX backend instance driving the FS-side operations.
_BACKEND = PosixBackend()

_MARKER_NAME = _posix_backend.MARKER_NAME

# The marker, ownership-probe and fd-bound deletion implementations live in
# the POSIX backend (APA-BE-002). The module-level names below stay bound in
# core because they are the long-standing test seams — the frozen golden
# fixtures and the hardening suite monkeypatch and call them on this module.
_parse_marker_payload = _posix_backend.parse_marker_payload
_write_marker = _posix_backend.write_marker
_read_marker = _posix_backend.read_marker


def _remove_marker_if_ours(path: Path, marker_id: object) -> None:
    """Remove the marker file only when it is provably ours.

    A replaced directory may contain the user's own ``.ephemdir`` file, and a
    legacy directory never had one — in both cases keeping a directory must
    not modify its contents. The verification read and the unlink both use the
    same held directory descriptor, so the directory cannot be swapped for
    another one between the check and the removal.
    """
    if not isinstance(marker_id, str):
        return
    try:
        dir_fd = _open_directory_nofollow(path)
    except (OSError, NotImplementedError, TypeError):
        return
    try:
        if _read_marker_at(dir_fd) == marker_id:
            try:
                os.unlink(_MARKER_NAME, dir_fd=dir_fd)
            except OSError:
                pass
    finally:
        os.close(dir_fd)


# Shape of the random suffix in private staging names.  The full path is
# validated relative to the original directory, not with a broad regex, so a
# crafted path elsewhere cannot masquerade as ephemdir's staging tree.
_STAGING_SUFFIX_PATTERN = re.compile(r"^\d+-[0-9a-f]{8}\.deleting$")
_CONTENTS_CLEARED_CALLBACK_KEY = "_ephemdir_contents_cleared_callback"

_StagingIdentityError = StagingIdentityError


def _valid_staging_path(original: Path, staging: Path) -> bool:
    """Return whether ``staging`` is a private sibling derived from ``original``."""
    if staging.parent != original.parent:
        return False
    prefix = f".{original.name}."
    if not staging.name.startswith(prefix):
        return False
    return _STAGING_SUFFIX_PATTERN.fullmatch(staging.name[len(prefix):]) is not None


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync after a critical rename (POSIX durability)."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_inode_matches = _posix_backend.inode_matches
_fd_inode_matches = _posix_backend.fd_inode_matches
_is_real_directory = _posix_backend.is_real_directory
_read_marker_at = _posix_backend.read_marker_at


def _ownership(path: Path, entry: Entry) -> str:
    """Classify whether ``path`` is still the directory we registered.

    Delegates to the POSIX backend's ownership probe; see
    :func:`ephemdir._backends.posix.ownership` for the classification rules.
    """
    return _BACKEND.probe_ownership(path, entry).status


def _staging_ownership(original: Path, staging: Path, entry: Entry) -> str:
    """Classify a staging tree mid-deletion.

    A journaled state alone proves nothing — the staging path must be the
    private sibling derived from the original path AND be confirmed by the
    marker. The inode recorded at claim time is a necessary cross-check but
    never sufficient on its own: filesystems such as ext4 and tmpfs reuse an
    inode number the instant the original tree is removed, so a newcomer
    created at the same private path can inherit the recorded inode number.
    When the marker is recorded but gone, the result is therefore ambiguous
    (``"unverified"``) and recovery parks it rather than deleting a possible
    replacement.
    """
    if not _valid_staging_path(original, staging):
        return "foreign"
    inode_ok = _inode_matches(staging, entry)
    if inode_ok is False:
        return "foreign"
    marker_id = entry.get("marker_id")
    marker = _read_marker(staging)
    if isinstance(marker_id, str):
        if marker == marker_id:
            return "ours"
        if marker is not None:
            return "foreign"
        # Marker recorded but absent: inode-match alone is not proof (inode
        # reuse), so refuse to claim ownership and let recovery handle it.
        return "unverified"
    if inode_ok is None:
        return "unverified"
    return "ours"


def _recovery_staging_ownership(original: Path, staging: Path, entry: Entry) -> str:
    """Probe staging ownership while honoring the durable root-only phase."""
    verdict = _staging_ownership(original, staging, entry)
    if (
        verdict == "unverified"
        and entry.get("delete_phase") == "root-only"
        and _is_real_directory(staging)
        and _inode_matches(staging, entry) is True
    ):
        # The marker was intentionally removed only after all managed payload
        # entries were durably recorded as gone. The backend will still remove
        # only an empty root and reject any replacement that gained content.
        return "ours"
    return verdict


def _deletion_lock_key(key: str, entry: Entry) -> str:
    """Stable per-directory key for :meth:`Registry.deletion_lock`."""
    marker_id = entry.get("marker_id")
    if isinstance(marker_id, str):
        return marker_id
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _path_state(path: Path) -> str:
    """Probe a path: ``"present"``, ``"missing"`` or ``"unknown"``.

    ``Path.exists()`` conflates "really gone" with "temporarily unreachable"
    (permission error, detached network mount, transient I/O failure); an
    entry must never be dropped by implicit cleanup just because the path is
    provably missing. Missing active entries stay tracked until an explicit
    user command such as ``prune`` or ``keep`` forgets them.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "unknown"
    return "present"


def _deletion_guard(path: Path) -> str | None:
    """Refuse obviously catastrophic deletion targets; return the reason.

    The marker check already protects against replaced directories; this is a
    last-resort safety net against a poisoned or hand-edited registry pointing
    at the filesystem root, the home directory or ephemdir's own state. The
    path is canonicalized first so ``..`` segments and symlink aliases cannot
    smuggle a critical directory past the comparison.
    """
    if not path.is_absolute():
        return "path is not absolute"
    try:
        normalized = path.resolve(strict=False)
    except OSError:
        return "path could not be canonicalized"
    if normalized.parent == normalized:
        return "filesystem root"
    try:
        home: Path | None = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        if normalized == home:
            return "home directory"
        if normalized in home.parents:
            return "ancestor of the home directory"
    for critical, label in (
        (user_data_dir(create=False), "ephemdir data directory"),
        (user_config_dir(create=False), "ephemdir config directory"),
    ):
        resolved = critical.resolve(strict=False)
        if normalized == resolved:
            return label
        if normalized in resolved.parents:
            return f"ancestor of the {label}"
    return None


def _destructive_path_error(path: Path) -> str | None:
    """Return any critical-target or parent-trust blocker for deletion."""
    return _deletion_guard(path) or _parent_trust_error(path)


def _trusted_component_error(path: Path, info: os.stat_result) -> str | None:
    """Return why one parent-chain component is unsafe, or ``None``."""
    return _security_trusted_component_error(path, info)


def _trusted_final_parent_error(path: Path, info: os.stat_result) -> str | None:
    """Return why the final parent cannot safely host managed entries."""
    return _security_trusted_final_parent_error(path, info)


def _walk_trusted_directory(
    directory: Path,
    *,
    create_missing: bool,
    allow_missing_tail: bool = False,
) -> int | None:
    """Walk a directory path component-by-component without following symlinks."""
    return _security_walk_trusted_directory(
        directory,
        create_missing=create_missing,
        allow_missing_tail=allow_missing_tail,
    )


def _trusted_parent_error(parent: Path) -> str | None:
    """Return why ``parent`` is unsafe for claim-by-rename, or ``None``."""
    try:
        fd = _walk_trusted_directory(
            parent,
            create_missing=False,
            allow_missing_tail=False,
        )
    except PermissionError as exc:
        return str(exc)
    except OSError as exc:
        return f"parent directory cannot be inspected: {exc}"
    if fd is not None:
        os.close(fd)
    return None


def _parent_trust_error(path: Path) -> str | None:
    """Return why ``path`` cannot be safely claimed under its parent."""
    return _trusted_parent_error(path.parent)


def _open_trusted_directory(directory: Path) -> int:
    """Open and re-verify a trusted directory for fd-relative operations."""
    try:
        fd = _security_open_trusted_directory(directory)
    except PermissionError as exc:
        raise PermissionError(f"refusing to operate in {directory}: {exc}") from exc
    return fd


def _open_or_create_trusted_directory(directory: Path) -> int:
    """Create any missing parent tail and return a verified final directory fd."""
    try:
        fd = _security_open_or_create_trusted_directory(directory)
    except PermissionError as exc:
        raise PermissionError(f"refusing to operate in {directory}: {exc}") from exc
    return fd


def _open_trusted_parent(path: Path) -> int:
    """Open and re-verify a parent directory used for an atomic rename."""
    reason = _parent_trust_error(path)
    if reason is not None:
        raise PermissionError(f"refusing to claim {path}: {reason}")
    return _open_trusted_directory(path.parent)


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
        marker_id: str | None = None,
    ) -> None:
        self._path = path
        self._created_at = created_at
        self._expires_at = expires_at
        self._remove_on_restart = remove_on_restart
        self._registry = registry
        self._marker_id = marker_id
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
        """Delete the directory now and stop tracking it. Idempotent.

        Deletes only while the directory is still tracked: if the registry
        entry is gone — most importantly because :func:`ephemdir.keep` released
        the directory — nothing is deleted, so ``keep()`` inside a ``with``
        block is honoured. Raises :class:`OSError` when the directory cannot
        be removed safely (for example because an object is locked) or is no longer the
        directory that was created (replaced after a manual delete) — nothing
        is half-deleted in either case, and the operation can be retried.
        """
        if self._removed:
            return
        entry = self._registry.load(read_only=True).get(str(self._path))
        if entry is None:
            # Released via keep() or already cleaned up elsewhere. Deleting an
            # untracked path would betray the keep() promise, so leave it be.
            self._removed = True
            if self._path.exists():
                logger.info("%s is no longer tracked; leaving it on disk", self._path)
            return
        if (
            self._marker_id is not None
            and isinstance(entry.get("marker_id"), str)
            and entry["marker_id"] != self._marker_id
        ):
            # The registry entry does not belong to this handle's directory.
            self._removed = True
            raise OSError(
                f"refusing to delete {self._path}: the registry entry does not "
                "match this handle (the directory was replaced?)"
            )
        if _execute_remove(self._path, self._registry, missing_ok=True, expected=entry):
            logger.info("removed ephemeral directory %s", self._path)
        self._removed = True

    def keep(self) -> None:
        """Stop tracking the directory without deleting it.

        After this call the directory is permanent as far as ephemdir is
        concerned: it will never be auto-removed (leaving a ``with`` block no
        longer deletes it either), and its ownership marker file is removed.

        Raises :class:`LookupError` when the directory is no longer tracked —
        e.g. a sweep already claimed it — so a lost race is reported instead
        of pretending the directory was saved.
        """
        with self._registry.transaction() as state:
            entry = state.get(str(self._path))
            if entry is None or entry.get("state", "active") != "active":
                raise LookupError(
                    f"{self._path} is no longer tracked; it cannot be kept "
                    "(a sweep may have claimed it already)"
                )
            self._assert_entry_is_ours(entry)
            del state[str(self._path)]
        _remove_marker_if_ours(self._path, entry.get("marker_id"))
        # A kept directory must survive the context-manager exit too.
        self._removed = True
        logger.info("released ephemeral directory %s (kept on disk)", self._path)

    def extend(self, lifetime: Lifetime = None) -> None:
        """Give the directory a fresh lifetime counted from now.

        ``None`` removes the time limit; the restart policy still applies.
        Raises :class:`LookupError` when the directory is no longer tracked,
        so a lost race against a sweep is never reported as success.
        """
        seconds = parse_lifetime(lifetime)
        expires_at = time.time() + seconds if seconds is not None else None
        with self._registry.transaction() as state:
            entry = state.get(str(self._path))
            if entry is None or entry.get("state", "active") != "active":
                raise LookupError(
                    f"{self._path} is no longer tracked; it cannot be extended"
                )
            self._assert_entry_is_ours(entry)
            entry["expires_at"] = expires_at
            entry["cleanup_policy"] = CleanupPolicy.AUTO.value
        self._expires_at = expires_at
        logger.info("extended ephemeral directory %s", self._path)

    def _assert_entry_is_ours(self, entry: Entry) -> None:
        """Refuse to act on a registry entry that no longer belongs to us.

        Without this, a stale handle whose directory was deleted and replaced
        could keep/extend the *replacement* directory's entry.
        """
        if (
            self._marker_id is not None
            and isinstance(entry.get("marker_id"), str)
            and entry["marker_id"] != self._marker_id
        ):
            raise OSError(f"registry entry for {self._path} does not match this handle")
        if _ownership(self._path, entry) == "foreign":
            raise OSError(
                f"{self._path} is not the directory this handle created "
                "(it was replaced after a manual delete)"
            )


def tempdir(
    lifetime: Lifetime = _UNSET,  # type: ignore[assignment]
    *,
    cleanup: str = _UNSET,  # type: ignore[assignment]
    remove_on_restart: bool = _UNSET,  # type: ignore[assignment]
    keep_while_in_use: bool = _UNSET,  # type: ignore[assignment]
    max_size: str | int | None = _UNSET,  # type: ignore[assignment]
    name_style: str = _UNSET,  # type: ignore[assignment]
    parent: str | os.PathLike[str] | None = _UNSET,  # type: ignore[assignment]
    prefix: str = _UNSET,  # type: ignore[assignment]
    words: int = _UNSET,  # type: ignore[assignment]
    tags: Sequence[str] | None = None,
    description: str | None = None,
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
        to ``False``. On supported POSIX systems this is detected with ``lsof``.
        If the probe is unavailable, deletion is deferred rather than guessed.
    cleanup:
        ``"auto"`` keeps the normal lifetime/restart policy. ``"next-sweep"``
        keeps the directory until an explicit full sweep, watcher or scheduler
        run.
    max_size:
        Optional byte limit. A full or maintenance sweep removes the directory
        once its measured tree size is above this limit.
    name_style:
        ``"secure"`` (or legacy alias ``"funny"``) appends a random suffix.
        ``"clean"`` omits the suffix only in private parent directories.
        ``"auto"`` uses clean names in private parents and secure names
        elsewhere.
    parent:
        Where to create the directory. Defaults to the current working
        directory.
    prefix:
        Optional prefix prepended to the generated playful name.
    words:
        Number of words in the generated name (``2`` -> ``brave-otter``).
    tags:
        Optional labels for grouping and filtering (e.g. ``["rust", "build"]``).
        Each tag is lowercase, starts with a letter or digit, uses only
        ``a-z 0-9 . _ -`` and is at most 32 characters; at most 16 per directory.
    description:
        Optional one-line, human-readable note (≤ 256 bytes, no control
        characters) shown in ``list`` and ``explain``.
    registry:
        Custom :class:`Registry` (mainly for testing).

    Returns
    -------
    EphemeralDirectory
        A path-like handle to the created directory.
    """
    settings, sources = _resolve_settings_with_sources(
        lifetime=lifetime,
        cleanup=cleanup,
        remove_on_restart=remove_on_restart,
        keep_while_in_use=keep_while_in_use,
        max_size=max_size,
        name_style=name_style,
        parent=parent,
        prefix=prefix,
        words=words,
    )

    # Validate every input before any side effect (including the lazy sweep
    # below), so a bad argument can never half-run a cleanup first.
    expires_seconds = parse_lifetime(settings["lifetime"])
    cleanup_policy = _parse_cleanup_policy(settings["cleanup"])
    max_size_bytes = parse_size(settings["max_size"])
    name_style_value = _parse_name_style(settings["name_style"])
    remove_on_restart_value = _require_bool(
        settings["remove_on_restart"],
        "remove_on_restart",
    )
    keep_while_in_use_value = _require_bool(
        settings["keep_while_in_use"],
        "keep_while_in_use",
    )
    if cleanup_policy is CleanupPolicy.NEXT_SWEEP:
        cleanup_source = sources["cleanup"]
        explicit_lifetime_conflict = (
            sources["lifetime"] == "explicit" and expires_seconds is not None
        )
        explicit_restart_conflict = (
            sources["remove_on_restart"] == "explicit" and remove_on_restart_value
        )
        if cleanup_source != "explicit" and (
            explicit_lifetime_conflict or explicit_restart_conflict
        ):
            cleanup_policy = CleanupPolicy.AUTO
            settings["cleanup"] = cleanup_policy.value
        elif (
            sources["lifetime"] == cleanup_source
            and expires_seconds is not None
            or sources["remove_on_restart"] == cleanup_source
            and remove_on_restart_value
        ):
            raise ValueError(
                "--until-sweep cannot be combined with --lifetime or restart cleanup"
            )
        else:
            settings["lifetime"] = None
            settings["remove_on_restart"] = False
            expires_seconds = None
            remove_on_restart_value = False
    if not isinstance(settings["prefix"], str):
        raise TypeError("prefix must be a string")
    _validate_prefix(settings["prefix"])
    _validate_words(settings["words"])
    tags_value = _normalize_tags(tags)
    description_value = _normalize_description(description)
    parent_path = _normalize_parent_value(settings["parent"])
    # Reject a parent whose path carries control/format/surrogate characters
    # before any side effect (preflight, lazy sweep, or directory creation), so
    # an unstorable registry key can never leave an unregistered directory
    # behind on disk (RC7-1). The registry would refuse such a key on save.
    if _has_control_character(str(parent_path)):
        raise ValueError("parent path must not contain control or formatting characters")

    backend_error = _safe_delete_backend_error()
    if backend_error is not None:
        raise UnsupportedPlatformError(
            errno.ENOTSUP,
            f"cannot create an ephemeral directory safely: {backend_error}",
        )

    # Normalize and verify the parent before the lazy sweep: predictable parent
    # failures must not be preceded by unrelated cleanup side effects.  Missing
    # tails are allowed here because the later create step makes them owner-only.
    parent_fd = _preflight_existing_parent(parent_path)
    try:
        effective_name_style = _effective_name_style_for_preflight(
            parent_path,
            name_style_value,
            parent_fd,
        )
    except BaseException:
        if parent_fd is not None:
            os.close(parent_fd)
        raise

    reg = registry or Registry()
    path: Path | None = None
    marker_id: str | None = None
    # Opportunistically clean up anything already due before creating more.
    try:
        sweep(registry=reg, mode=SweepMode.MAINTENANCE)
        if parent_fd is None:
            parent_fd = _open_or_create_trusted_directory(parent_path)

        now = time.time()
        expires_at = now + expires_seconds if expires_seconds is not None else None
        created_boot_time = boot_time()
        created_boot_id = boot_session_id()

        # Reserve the ordinary pathname against every lifecycle state while the
        # registry lock is held.  A recovery entry may have no object at its
        # original path, so filesystem exclusivity alone is not sufficient.
        # Creation, the marker write and the inode snapshot are all relative to
        # descriptors of the verified parent/new directory, so no ancestor
        # rename between these steps can redirect them elsewhere.
        try:
            with reg.transaction() as state:
                created = _create_unique_dir(
                    parent_path,
                    settings["prefix"],
                    settings["words"],
                    name_style=effective_name_style,
                    reserved=frozenset(state),
                    parent_fd=parent_fd,
                )
                path = created.path
                marker_id = created.marker_id
                entry: Entry = {
                    "created_at": now,
                    "expires_at": expires_at,
                    "remove_on_restart": remove_on_restart_value,
                    "keep_while_in_use": keep_while_in_use_value,
                    "cleanup_policy": cleanup_policy.value,
                    "max_size": max_size_bytes,
                    "name_style": effective_name_style,
                    "registry_version": 3,
                    "backend": "posix" if os.name == "posix" else sys.platform,
                    "platform": sys.platform,
                    "boot_time": created_boot_time,
                    "boot_id": created_boot_id,
                    "marker_id": marker_id,
                    "state": "active",
                    "claim_id": None,
                    "staging_path": None,
                }
                if created.ino:  # 0 means the filesystem does not report inodes
                    entry["dev"] = created.dev
                    entry["ino"] = created.ino
                # Only store the optional v3 metadata when set, so an untagged
                # directory's entry stays byte-identical to earlier versions.
                if tags_value:
                    entry["tags"] = tags_value
                if description_value is not None:
                    entry["description"] = description_value
                if str(path) in state:
                    # Defensive assertion against future refactors that weaken the
                    # reservation check. Never overwrite a deletion journal.
                    raise FileExistsError(f"registry path became reserved: {path}")
                state[str(path)] = entry
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
    except BaseException:
        # Never recursively delete by pathname after a failed registry commit:
        # another process could have replaced the directory in the meantime.
        # Leaving our owner-only, marker-bearing directory behind is a bounded
        # leak and is strictly safer than deleting a replacement we did not create.
        if path is not None:
            logger.error(
                "could not register newly created directory %s; leaving it on disk "
                "rather than risking deletion of a replacement",
                path,
            )
        raise

    if path is None or marker_id is None:
        raise RuntimeError("internal error: directory registration produced no path or marker")
    logger.info("created ephemeral directory %s", path)
    _record_stats(created=1)
    return EphemeralDirectory(
        path,
        created_at=now,
        expires_at=expires_at,
        remove_on_restart=remove_on_restart_value,
        registry=reg,
        marker_id=marker_id,
    )


def _resolve_settings(**overrides: object) -> dict[str, Any]:
    """Merge explicit arguments over user config over built-in defaults.

    Only arguments that differ from the ``_UNSET`` sentinel override the lower
    layers, so callers can selectively set just the options they care about.
    """
    resolved, _ = _resolve_settings_with_sources(**overrides)
    return resolved


def _resolve_settings_with_sources(**overrides: object) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge settings and report whether each value came from call/config/default."""
    config = load_config()
    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, default in _DEFAULTS.items():
        if overrides.get(key, _UNSET) is not _UNSET:
            resolved[key] = overrides[key]
            sources[key] = "explicit"
        elif key in config:
            resolved[key] = config[key]
            sources[key] = "config"
        else:
            resolved[key] = default
            sources[key] = "default"
    return resolved, sources


def _parse_cleanup_policy(value: object) -> CleanupPolicy:
    if not isinstance(value, str):
        raise TypeError("cleanup must be a string")
    try:
        return CleanupPolicy(value)
    except ValueError as error:
        raise ValueError("cleanup must be 'auto' or 'next-sweep'") from error


def _parse_name_style(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("name_style must be a string")
    text = value
    if text == "funny":
        return "secure"
    if text not in {"auto", "clean", "secure"}:
        raise ValueError("name_style must be 'auto', 'clean', or 'secure'")
    return text


def _require_bool(value: object, name: str) -> bool:
    """Reject truthy/falsy coercion for public boolean API values."""
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")
    return value


def _normalize_parent_value(value: object) -> Path:
    """Normalize a parent argument without accepting unrelated object types."""
    if value is None:
        return Path.cwd()
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError("parent must be str, os.PathLike, or None")
    return _canonical_private_dir_path(Path(value).expanduser())


def _preflight_existing_parent(parent: Path) -> int | None:
    """Reject existing unusable parents before maintenance sweep side effects."""
    try:
        fd = _walk_trusted_directory(
            parent,
            create_missing=False,
            allow_missing_tail=True,
        )
    except FileNotFoundError:
        return None
    except NotADirectoryError as exc:
        raise FileExistsError(f"{parent} is not a directory") from exc
    except PermissionError as exc:
        raise PermissionError(f"refusing to create ephemdir in {parent}: {exc}") from exc
    except OSError as exc:
        raise PermissionError(f"refusing to create ephemdir in {parent}: {exc}") from exc
    if fd is not None:
        return fd
    return None


def _effective_name_style_for_preflight(
    parent: Path,
    name_style: str,
    parent_fd: int | None,
) -> str:
    """Choose naming style using pre-sweep parent verification results."""
    if name_style == "secure":
        return "secure"
    private = True if parent_fd is None else _is_private_parent_stat(os.fstat(parent_fd))
    if name_style == "clean":
        if not private:
            raise PermissionError(
                f"refusing clean ephemdir names in {parent}: parent is not private"
            )
        return "clean"
    if name_style == "auto":
        return "clean" if private else "secure"
    raise ValueError("name_style must be 'auto', 'clean', or 'secure'")


def sweep(
    *,
    registry: Registry | None = None,
    force: bool = False,
    dry_run: bool = False,
    mode: SweepMode | str | None = None,
    tags: Sequence[str] | None = None,
) -> int:
    """Remove every tracked directory that is due for cleanup.

    Interrupted journal entries are reconciled under the same per-directory
    OS lock used by claim/delete.  This prevents a live claim from being
    mistaken for a crashed one by a concurrent sweep.

    ``tags`` restricts the sweep to directories carrying every given tag. It can
    only *narrow* the candidate set — it never changes which directories are due
    or how they are deleted — so it is safe with respect to the deletion path.
    """
    reg = registry or Registry()
    wanted = _normalize_tags(tags)
    sweep_mode = SweepMode.FORCE if force else (SweepMode(mode) if mode else SweepMode.FULL)
    is_force = sweep_mode is SweepMode.FORCE
    if dry_run:
        return sum(
            1
            for decision in plan_sweep(registry=reg, force=is_force, mode=sweep_mode, tags=wanted)
            if decision.destructive_allowed
        )
    now = time.time()
    current_boot = boot_time()
    current_boot_id = boot_session_id()
    journal: list[tuple[Path, Entry]] = []
    candidates: list[tuple[Path, Entry]] = []

    # Phase 1: take snapshots only.  Journal recovery must not run while merely
    # holding the registry lock: the deletion lock is the authority that tells
    # us whether the owner of a moving/deleting transaction is still alive.
    with reg.transaction() as state:
        for key in list(state.keys()):
            entry = state[key]
            path = Path(key)
            if wanted and not _entry_has_tags(entry, wanted):
                continue  # tag filter only narrows; never touches non-matching entries
            blockers = _entry_compatibility_blockers(entry)
            if blockers:
                logger.warning("deferring incompatible entry for %s: %s", path, ", ".join(blockers))
                continue
            entry_state = entry.get("state", "active")
            if entry_state != "active":
                journal.append((path, dict(entry)))
                continue

            probe = _path_state(path)
            if probe == "missing":
                logger.warning(
                    "%s is missing; keeping its entry until deletion is verified "
                    "or it is explicitly pruned/kept",
                    path,
                )
                continue
            if probe == "unknown":
                logger.warning("cannot probe %s; keeping its entry untouched", path)
                continue

            reason = _deletion_guard(path)
            if reason is not None:
                logger.warning("deferring %s: %s", path, reason)
                continue

            max_size_value = entry.get("max_size")
            size_limited = isinstance(max_size_value, int) and max_size_value >= 0
            if not (
                is_force
                or _is_due(entry, now, current_boot, current_boot_id, mode=sweep_mode)
                or size_limited
            ):
                continue

            ownership = _ownership(path, entry)
            if ownership == "foreign":
                logger.warning(
                    "%s is no longer the directory ephemdir created; leaving it "
                    "alone and keeping the entry blocked until explicit keep",
                    path,
                )
                continue
            if ownership == "unverified":
                logger.warning(
                    "%s was registered by an older ephemdir and cannot be verified; "
                    "not removing it automatically -- run `ephemdir rm %s` or "
                    "`ephemdir keep %s`",
                    path,
                    path.name,
                    path.name,
                )
                continue
            candidates.append((path, dict(entry)))

    removed = 0

    # Reconcile crashes first.  The lock order is always deletion lock then
    # registry lock, matching claim/delete and avoiding deadlocks.
    for path, snapshot in journal:
        with reg.deletion_lock(_deletion_lock_key(str(path), snapshot)) as acquired:
            if not acquired:
                logger.info("skipping recovery for %s: another process owns the claim", path)
                continue
            status, staging, claimed = _recover_entry(reg, str(path), expected=snapshot)
            if status == "resume":
                if staging is None or claimed is None:
                    logger.error("invalid recovery payload for %s; leaving it tracked", path)
                    continue
                if _finish_deletion(reg, str(path), staging, claimed):
                    removed += 1
                    logger.info("finished interrupted deletion of %s", path)
            elif status == "completed":
                removed += 1
                logger.info("finalized interrupted deletion record for %s", path)
            elif status == "recovery":
                logger.warning(
                    "%s needs manual attention; run `ephemdir recover %s --retry` "
                    "after resolving the conflicting paths, or `--forget` to stop tracking",
                    path,
                    path.name,
                )
            elif status == "blocked":
                logger.warning("deferring recovery for %s: incompatible registry entry", path)

    # Process active due entries.  Slow lsof/rmtree work stays outside the
    # registry lock; the per-directory lock prevents overlapping sweep calls.
    for path, snapshot in candidates:
        decision = _decision_for_entry(
            path,
            snapshot,
            now=now,
            current_boot=current_boot,
            current_boot_id=current_boot_id,
            mode=sweep_mode,
        )
        if not decision.due:
            continue
        if decision.blockers:
            logger.warning("deferring %s: %s", path, ", ".join(decision.blockers))
            continue

        if snapshot.get("keep_while_in_use"):
            in_use = is_in_use(path)
            if in_use:
                logger.info("deferring in-use ephemeral directory %s", path)
                continue
            if in_use is None and sys.platform != "win32":
                logger.warning("cannot tell whether %s is in use; deferring", path)
                continue

        with reg.deletion_lock(_deletion_lock_key(str(path), snapshot)) as acquired:
            if not acquired:
                logger.info("skipping %s: another process is already deleting it", path)
                continue
            status, staging, claimed = _try_claim(reg, path, snapshot, allow_unverified=False)
            if status == "claimed":
                if staging is None or claimed is None:
                    logger.error("invalid claim payload for %s; leaving it tracked", path)
                    continue
                if _finish_deletion(reg, str(path), staging, claimed):
                    removed += 1
                    logger.info("swept ephemeral directory %s", path)
            elif status == "changed":
                logger.info("skipping %s: its entry changed during the sweep", path)
            elif status == "foreign":
                logger.warning("%s changed under us; leaving it alone", path)
            elif status == "locked":
                logger.info("deferring locked ephemeral directory %s", path)
            elif status == "unsafe_parent":
                logger.warning("deferring %s: parent directory is not safe for deletion", path)
            elif status == "unsupported":
                logger.warning(
                    "deferring %s: safe recursive deletion is unavailable; "
                    "original remains in place",
                    path,
                )

    if removed:
        _record_stats(swept=removed)
    return removed


def plan_sweep(
    *,
    registry: Registry | None = None,
    force: bool = False,
    mode: SweepMode | str | None = None,
    tags: Sequence[str] | None = None,
) -> list[CleanupDecision]:
    """Return the read-only cleanup plan for the current registry.

    ``tags`` narrows the plan to directories carrying every given tag.
    """
    reg = registry or Registry()
    wanted = _normalize_tags(tags)
    sweep_mode = SweepMode.FORCE if force else (SweepMode(mode) if mode else SweepMode.FULL)
    now = time.time()
    current_boot = boot_time()
    current_boot_id = boot_session_id()
    decisions: list[CleanupDecision] = []
    for key, entry in reg.load(read_only=True).items():
        path = Path(key)
        if wanted and not _entry_has_tags(entry, wanted):
            continue
        if entry.get("state", "active") != "active":
            decisions.append(_recovery_plan_decision(path, entry))
            continue
        decisions.append(
            _decision_for_entry(
                path,
                entry,
                now=now,
                current_boot=current_boot,
                current_boot_id=current_boot_id,
                mode=sweep_mode,
            )
        )
    return decisions


def _recovery_plan_decision(path: Path, entry: Entry) -> CleanupDecision:
    """Plan crash recovery without taking locks or mutating state."""
    compatibility_blockers = _entry_compatibility_blockers(entry)
    if compatibility_blockers:
        return _blocked_decision(
            path,
            entry,
            ("recovery-required",) + compatibility_blockers,
            reasons=("recovery-resume",),
            due=True,
        )
    lifecycle = entry.get("state", "active")
    if lifecycle == "recovery":
        return _blocked_decision(
            path,
            entry,
            ("recovery-required",),
            reasons=("recovery-resume",),
            due=True,
        )
    if lifecycle not in {"moving", "deleting"}:
        return _blocked_decision(
            path,
            entry,
            ("recovery-required",),
            reasons=("recovery-resume",),
            due=True,
        )
    reason = _destructive_path_error(path)
    if reason is not None:
        return _blocked_decision(
            path,
            entry,
            ("unsafe-parent",),
            reasons=("recovery-resume",),
            due=True,
        )
    staging_value = entry.get("staging_path")
    staging = Path(staging_value) if isinstance(staging_value, str) else None
    if staging is None or not _valid_staging_path(path, staging):
        return _blocked_decision(
            path,
            entry,
            ("recovery-required",),
            reasons=("recovery-resume",),
            due=True,
        )
    original_probe = _path_state(path)
    staging_probe = _path_state(staging)
    if original_probe == "unknown" or staging_probe == "unknown":
        return _blocked_decision(
            path,
            entry,
            ("ownership-unverified",),
            reasons=("recovery-resume",),
            due=True,
        )
    original_here = original_probe == "present"
    staging_here = staging_probe == "present"
    if not staging_here:
        return _blocked_decision(
            path,
            entry,
            ("recovery-required",),
            reasons=("recovery-resume",),
            due=True,
        )
    staging_verdict = _recovery_staging_ownership(path, staging, entry)
    original_verdict = _ownership(path, entry) if original_here else "missing"
    if staging_verdict == "ours" and (not original_here or original_verdict != "ours"):
        return CleanupDecision(
            path=path,
            due=True,
            status="due",
            reasons=("recovery-resume",),
            blockers=(),
            destructive_allowed=True,
            measured_size_bytes=None,
            max_size_bytes=None,
        )
    return _blocked_decision(
        path,
        entry,
        ("recovery-required",),
        reasons=("recovery-resume",),
        due=True,
    )


def registered(*, registry: Registry | None = None) -> dict[str, Entry]:
    """Return a snapshot of all currently tracked directories."""
    reg = registry or Registry()
    return reg.load(read_only=True)


def _match_target(
    target: str | os.PathLike[str],
    state: dict[str, Entry],
) -> Path:
    """Resolve a path/name/prefix against an already-filtered registry state."""
    text = os.fspath(target)
    candidate = Path(os.path.abspath(Path(text).expanduser()))
    if str(candidate) in state:
        matches = [candidate]
    else:
        tracked = [Path(key) for key in state]
        matches = [path for path in tracked if path.name == text]
        if not matches:
            matches = [path for path in tracked if path.name.startswith(text)]
    if not matches:
        raise TargetError(f"no tracked directory matches {text!r}")
    if len(matches) > 1:
        names = ", ".join(sorted(path.name for path in matches))
        raise TargetError(f"{text!r} is ambiguous; it matches: {names}")
    return matches[0]


def _resolve_active(
    target: str | os.PathLike[str],
    *,
    registry: Registry,
) -> tuple[Path, Entry]:
    """Resolve ``target`` to an active path and the exact entry snapshot."""
    reg = registry or Registry()
    state = {
        key: entry
        for key, entry in reg.load(read_only=True).items()
        if entry.get("state", "active") == "active"
    }
    path = _match_target(target, state)
    snapshot = dict(state[str(path)])
    blockers = _entry_compatibility_blockers(snapshot)
    if blockers:
        raise LookupError(
            f"{path} is tracked for an incompatible runtime: {', '.join(blockers)}"
        )
    probe = _path_state(path)
    if probe == "missing":
        raise LookupError(
            f"{path} is tracked but currently missing; the entry was preserved. "
            "Run `ephemdir prune` to forget missing entries or "
            f"`ephemdir keep {path.name}` to forget this one."
        )
    if probe == "unknown":
        raise LookupError(
            f"{path} is temporarily inaccessible; its registry entry was preserved"
        )
    return path, snapshot


def resolve(target: str | os.PathLike[str], *, registry: Registry | None = None) -> Path:
    """Resolve ``target`` to an active tracked directory.

    Missing or temporarily inaccessible paths are preserved in the registry and
    reported as unavailable; only explicit commands such as ``prune`` or
    ``keep`` forget missing active entries.
    """
    reg = registry or Registry()
    path, _ = _resolve_active(target, registry=reg)
    return path


# --- Current-directory target resolution -----------------------------------
#
# Commands like `keep`, `rm`, `explain` and `extend` can act on the ephemdir
# directory the user is standing in, with no name argument. We find the nearest
# `.ephemdir` marker at or above the current directory and act on it only when
# it matches an active registry entry by marker id and inode -- never guessing.

_CURRENT_NOTFOUND_MESSAGE = (
    "no target provided and current directory is not inside a tracked ephemdir "
    "directory; pass a name/path or cd into one"
)
_CURRENT_MISMATCH_MESSAGE = (
    "current directory contains an .ephemdir marker, but it does not match an "
    "active tracked ephemdir entry; refusing to guess"
)


class _CurrentTargetNotFound(TargetError):
    """No ``.ephemdir`` marker was found at or above the start directory."""


class _CurrentTargetMismatch(TargetError):
    """A marker was found but does not match an active tracked entry."""


def _marker_present(directory: Path) -> bool:
    """Whether ``directory`` holds an ``.ephemdir`` entry (even a broken link)."""
    return os.path.lexists(directory / _MARKER_NAME)


def _resolve_current_target(
    *, registry: Registry, start: Path | None = None
) -> tuple[Path, Entry]:
    """Resolve the active tracked ephemdir root containing ``start`` (or cwd).

    Walks upward from ``start`` and stops at the **nearest** directory holding
    an ``.ephemdir`` marker. That directory is returned only when it is an
    active registry entry whose marker id and inode match (``_ownership`` is
    ``"ours"``) and whose runtime is compatible. A present-but-unmatched marker
    raises :class:`_CurrentTargetMismatch` — it never falls through to a marker
    higher up, and never guesses. No marker anywhere raises
    :class:`_CurrentTargetNotFound`. The resolver performs no registry
    mutations.
    """
    reg = registry or Registry()
    try:
        base = Path.cwd() if start is None else Path(start)
    except OSError as error:
        raise _CurrentTargetNotFound(_CURRENT_NOTFOUND_MESSAGE) from error
    base = _canonical_private_dir_path(base)
    state = {
        key: entry
        for key, entry in reg.load(read_only=True).items()
        if entry.get("state", "active") == "active"
    }
    for directory in (base, *base.parents):
        if not _marker_present(directory):
            continue
        entry = state.get(str(directory))
        if (
            entry is not None
            and _ownership(directory, entry) == "ours"
            and not _entry_compatibility_blockers(entry)
        ):
            return directory, dict(entry)
        # A marker is here but it does not match an active, owned entry. Refuse
        # to guess and refuse to walk past it to a marker further up.
        raise _CurrentTargetMismatch(_CURRENT_MISMATCH_MESSAGE)
    raise _CurrentTargetNotFound(_CURRENT_NOTFOUND_MESSAGE)


def _current_target_path(
    *, registry: Registry | None = None, start: Path | None = None
) -> Path:
    """Return the path of the active ephemdir directory containing the cwd."""
    reg = registry or Registry()
    path, _ = _resolve_current_target(registry=reg, start=start)
    return path


def keep(target: str | os.PathLike[str], *, registry: Registry | None = None) -> Path:
    """Stop tracking a directory without deleting it; return its path.

    The directory becomes permanent as far as ephemdir is concerned — it will
    never be auto-removed. ``target`` accepts anything :func:`resolve` does.
    """
    reg = registry or Registry()
    state = {
        key: entry
        for key, entry in reg.load(read_only=True).items()
        if entry.get("state", "active") == "active"
    }
    path = _match_target(target, state)
    snapshot = dict(state[str(path)])
    blockers = _entry_compatibility_blockers(snapshot)
    if blockers:
        raise LookupError(
            f"{path} is tracked for an incompatible runtime: {', '.join(blockers)}"
        )
    probe = _path_state(path)
    if probe == "unknown":
        raise LookupError(
            f"{path} is temporarily inaccessible; its registry entry was preserved"
        )
    with reg.transaction() as state:
        entry = state.get(str(path))
        if entry != snapshot:
            # The entry vanished (or was claimed) between resolve() and here —
            # a sweep won the race, so the directory can no longer be saved.
            raise LookupError(
                f"{path} is no longer tracked (a concurrent sweep claimed it first)"
            )
        if probe == "missing" and _path_state(path) != "missing":
            raise LookupError(f"{path} reappeared while keeping; retry the command")
        del state[str(path)]
    if probe == "present":
        # Only delete the marker when it is verifiably ours: a replaced directory
        # may contain the user's own .ephemdir file, a legacy one never had any.
        _remove_marker_if_ours(path, entry.get("marker_id"))
        logger.info("kept %s (no longer tracked)", path)
        _record_stats(kept=1)
    else:
        logger.info("forgot missing tracked directory: %s", path)
    return path


def extend(
    target: str | os.PathLike[str],
    lifetime: Lifetime = None,
    *,
    registry: Registry | None = None,
) -> Path:
    """Give a tracked directory a fresh lifetime counted from now.

    ``None`` removes the time limit entirely (the restart policy still
    applies). Returns the directory's path.

    Raises :class:`OSError` when the path no longer holds the directory
    ephemdir created: such an entry can never be cleaned up automatically, so
    a new deadline would be a promise nothing can keep. The recorded lifetime
    is left untouched in that case.
    """
    reg = registry or Registry()
    path, snapshot = _resolve_active(target, registry=reg)
    seconds = parse_lifetime(lifetime)
    expires_at = time.time() + seconds if seconds is not None else None
    with reg.transaction() as state:
        entry = state.get(str(path))
        if entry != snapshot:
            # The entry vanished or was claimed between resolve() and here
            # (e.g. a concurrent sweep); surface it instead of false success.
            raise LookupError(f"no tracked directory matches {os.fspath(target)!r}")
        if _ownership(path, entry) == "foreign":
            # The backend answers "foreign" both when someone else's directory
            # occupies the path and when the path simply vanished. Name the
            # actual case instead of accusing a deleted directory of being an
            # impostor; the refusal itself is right either way.
            reason = (
                "it no longer exists"
                if _path_state(path) == "missing"
                else "it is not the directory ephemdir created"
            )
            raise OSError(f"refusing to extend {path}: {reason}")
        entry["expires_at"] = expires_at
        entry["cleanup_policy"] = CleanupPolicy.AUTO.value
    logger.info("extended %s", path)
    return path


def remove(target: str | os.PathLike[str], *, registry: Registry | None = None) -> Path:
    """Delete a tracked directory now and stop tracking it; return its path.

    Raises :class:`OSError` when the directory cannot be removed safely (a
    locked file, or the directory was replaced after a manual delete); nothing
    is half-deleted in that case and the entry stays tracked for retry.
    """
    reg = registry or Registry()
    path, snapshot = _resolve_active(target, registry=reg)
    _execute_remove(path, reg, expected=snapshot)
    logger.info("removed %s", path)
    _record_stats(removed=1)
    return path


def recover(
    target: str | os.PathLike[str],
    *,
    action: str = "retry",
    registry: Registry | None = None,
) -> Path:
    """Reconcile or forget an interrupted deletion journal entry.

    ``action="retry"`` performs the same safe, lock-protected reconciliation
    used by :func:`sweep`.  ``action="forget"`` removes only the registry
    entry and never touches either filesystem path, providing a safe escape
    hatch for genuinely ambiguous ``recovery`` entries.
    """
    if action not in {"retry", "forget"}:
        raise ValueError("action must be 'retry' or 'forget'")
    reg = registry or Registry()
    all_state = reg.load(read_only=True)
    journal_state = {
        key: entry
        for key, entry in all_state.items()
        if entry.get("state", "active") != "active"
    }
    path = _match_target(target, journal_state)
    key = str(path)
    snapshot = journal_state[key]

    with reg.deletion_lock(_deletion_lock_key(key, snapshot)) as acquired:
        if not acquired:
            raise OSError(f"{path} is currently being processed by another ephemdir process")
        if action == "forget":
            with reg.transaction() as state:
                live = state.get(key)
                if live is None or live.get("state", "active") == "active":
                    raise LookupError(f"{path} no longer has a recovery entry")
                if live != snapshot:
                    raise LookupError(
                        f"{path} recovery entry changed concurrently; reload and retry"
                    )
                del state[key]
            logger.warning("forgot recovery entry for %s; no files were deleted", path)
            return path

        status, staging, claimed = _recover_entry(
            reg, key, retry_recovery=True, expected=snapshot
        )
        if status == "changed":
            raise LookupError(f"{path} recovery entry changed concurrently; reload and retry")
        if status == "blocked":
            raise OSError(f"{path} recovery entry is not compatible with this backend")
        if status == "resume":
            if staging is None or claimed is None:
                raise OSError(f"invalid recovery payload for {path}; entry remains tracked")
            if not _finish_deletion(reg, key, staging, claimed):
                raise OSError(
                    f"could not fully remove {path}; the staging remainder stays tracked"
                )
        elif status == "completed":
            return path
        elif status == "recovery":
            raise OSError(
                f"{path} is still ambiguous; resolve the conflicting original/staging "
                "paths and retry, or use --forget to stop tracking without deleting"
            )
    return path


def prune(*, registry: Registry | None = None) -> int:
    """Drop missing active entries by explicit user request.

    Returns the number of entries forgotten. ``prune`` only tidies the registry
    and never deletes anything from disk; sweeps preserve missing entries.
    """
    reg = registry or Registry()
    pruned = 0
    with reg.transaction() as state:
        for key in list(state.keys()):
            entry = state[key]
            # Mid-deletion/recovery entries reference a staging tree that is
            # deliberately not at the original path; never prune those.
            if entry.get("state", "active") != "active":
                continue
            if _entry_compatibility_blockers(entry):
                continue
            if _path_state(Path(key)) == "missing":
                del state[key]
                pruned += 1
                logger.warning("forgot missing tracked directory: %s", key)
    if pruned:
        logger.info("pruned %d missing entr%s", pruned, "y" if pruned == 1 else "ies")
    return pruned


def dir_status(
    entry: Entry,
    path: Path,
    now: float,
    current_boot: float | None,
    current_boot_id: str | None = None,
) -> str:
    """Classify a registry entry for display purposes.

    Returns one of ``"missing"`` (path absent but still tracked), ``"deleting"``
    (mid-deletion, finished or retried by sweeps), ``"recovery"`` (an
    interrupted deletion that needs manual attention), ``"replaced"``
    (something else now occupies the path), ``"legacy"`` (created by
    ephemdir <= 0.3, cannot be verified, never auto-removed), ``"expired"``
    (due for cleanup on the next sweep), ``"expiring"`` (less than 15 minutes
    left), ``"active"`` (counting down), ``"until-restart"`` (no time limit,
    removed on reboot) or ``"kept"`` (no time limit and survives reboots).
    """
    entry_state = entry.get("state", "active")
    if entry_state in ("moving", "deleting"):
        return "deleting"
    if entry_state == "recovery":
        return "recovery"
    if _entry_compatibility_blockers(entry):
        return "blocked"
    probe = _path_state(path)
    if probe == "missing":
        return "missing"
    if probe == "unknown":
        return "unavailable"
    ownership = _ownership(path, entry)
    if ownership == "foreign":
        # The backend reports a vanished path as "foreign" too, so a sweep
        # that renames the directory to its staging name between the probe
        # above and this call would surface as "replaced". That word is a
        # safety signal ("someone else's directory occupies the path"), so
        # re-probe and keep it meaningful: a path that simply disappeared is
        # reported as missing, exactly as the next sweep will see it.
        return "replaced" if _path_state(path) == "present" else "missing"
    if ownership == "unverified":
        return "legacy"
    if entry.get("cleanup_policy") == CleanupPolicy.NEXT_SWEEP.value:
        return "until-sweep"
    if _is_due(entry, now, current_boot, current_boot_id):
        return "expired"
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, (int, float)):
        remaining = float(expires_at) - now
        return "expiring" if remaining <= _EXPIRING_SOON_SECONDS else "active"
    return "until-restart" if entry.get("remove_on_restart") else "kept"


def explain(
    target: str | os.PathLike[str],
    *,
    registry: Registry | None = None,
    mode: SweepMode | str = SweepMode.FULL,
) -> CleanupDecision:
    """Explain cleanup state for one active tracked directory."""
    decision, _, _ = _explain_with_entry(target, registry=registry, mode=mode)
    return decision


def _explain_with_entry(
    target: str | os.PathLike[str],
    *,
    registry: Registry | None = None,
    mode: SweepMode | str = SweepMode.FULL,
) -> tuple[CleanupDecision, Entry, float]:
    """Return one decision and the exact entry/time snapshot that produced it."""
    reg = registry or Registry()
    state = reg.load(read_only=True)
    path = _match_target(target, state)
    entry = dict(state[str(path)])
    evaluated_at = time.time()
    if entry.get("state", "active") != "active":
        return _recovery_plan_decision(path, entry), entry, evaluated_at
    decision = _decision_for_entry(
        path,
        entry,
        now=evaluated_at,
        current_boot=boot_time(),
        current_boot_id=boot_session_id(),
        mode=SweepMode(mode),
    )
    return decision, entry, evaluated_at


def _decision_for_entry(
    path: Path,
    entry: Entry,
    *,
    now: float,
    current_boot: float | None,
    current_boot_id: str | None,
    mode: SweepMode,
    in_use: bool | None = False,
) -> CleanupDecision:
    compatibility_blockers = _entry_compatibility_blockers(entry)
    if compatibility_blockers:
        return _blocked_decision(path, entry, compatibility_blockers)
    path_probe = _path_state(path)
    ownership = "unverified"
    parent_error = None
    measured: int | None = None
    complete = True
    max_size_for_status = entry.get("max_size")
    if path_probe == "present":
        parent_error = _destructive_path_error(path)
        ownership = _ownership(path, entry)
        if entry.get("keep_while_in_use") and in_use is False:
            in_use = is_in_use(path)
        max_size_value = entry.get("max_size")
        if isinstance(max_size_value, int) and max_size_value >= 0:
            result = measure_tree(path, limit=max_size_value)
            measured = result.bytes
            complete = result.complete
    elif path_probe == "missing":
        present_policy = decide_cleanup(
            path,
            entry,
            now=now,
            current_boot=current_boot,
            current_boot_id=current_boot_id,
            mode=mode,
            path_state="present",
            ownership="ours",
            parent_error=None,
            in_use=False,
            measured_size_bytes=None,
            size_complete=True,
            same_boot_func=same_boot,
        )
        blockers = tuple(dict.fromkeys(("path-missing",) + present_policy.blockers))
        return CleanupDecision(
            path=path,
            due=present_policy.due,
            status="missing",
            reasons=present_policy.reasons,
            blockers=blockers,
            destructive_allowed=False,
            measured_size_bytes=None,
            max_size_bytes=(
                max_size_for_status
                if isinstance(max_size_for_status, int) and max_size_for_status >= 0
                else None
            ),
        )
    return decide_cleanup(
        path,
        entry,
        now=now,
        current_boot=current_boot,
        current_boot_id=current_boot_id,
        mode=mode,
        path_state=path_probe,
        ownership=ownership,
        parent_error=parent_error,
        in_use=in_use,
        measured_size_bytes=measured,
        size_complete=complete,
        same_boot_func=same_boot,
    )


def _blocked_decision(
    path: Path,
    entry: Entry,
    blockers: tuple[str, ...],
    *,
    reasons: tuple[str, ...] = (),
    due: bool = False,
    destructive_allowed: bool = False,
) -> CleanupDecision:
    max_size_value = entry.get("max_size")
    return CleanupDecision(
        path=path,
        due=due,
        status="blocked",
        reasons=reasons,
        blockers=blockers,
        destructive_allowed=destructive_allowed,
        measured_size_bytes=None,
        max_size_bytes=(
            max_size_value
            if isinstance(max_size_value, int) and max_size_value >= 0
            else None
        ),
    )


def _entry_compatibility_blockers(entry: Entry) -> tuple[str, ...]:
    """Return platform/backend blockers for entries unsafe on this runtime."""
    blockers: list[str] = []
    backend = entry.get("backend")
    if isinstance(backend, str) and backend != "posix":
        blockers.append("unsupported-backend")
    platform = entry.get("platform")
    if isinstance(platform, str) and platform != sys.platform:
        blockers.append("foreign-platform")
    return tuple(blockers)


def _is_due(
    entry: Entry,
    now: float,
    current_boot: float | None,
    current_boot_id: str | None = None,
    *,
    mode: SweepMode = SweepMode.FULL,
) -> bool:
    """Decide whether a registry entry should be cleaned up now."""
    decision = decide_cleanup(
        Path("."),
        entry,
        now=now,
        current_boot=current_boot,
        current_boot_id=current_boot_id,
        mode=mode,
        path_state="present",
        ownership="owned",
        parent_error=None,
        same_boot_func=same_boot,
    )
    return decision.due


def _validate_prefix(prefix: str) -> None:
    """Reject path separators and control characters in generated names."""
    if not prefix:
        return
    if len(prefix) > _MAX_PREFIX_CODEPOINTS:
        raise ValueError(f"prefix must be at most {_MAX_PREFIX_CODEPOINTS} characters")
    separators = {os.sep} | ({os.altsep} if os.altsep else set())
    if any(sep in prefix for sep in separators) or os.path.isabs(prefix):
        raise ValueError(
            f"prefix {prefix!r} must not contain path separators; the directory "
            "is always created directly under parent"
        )
    if any(
        ord(char) < 32 or ord(char) == 127
        or unicodedata.category(char) in ("Cc", "Cf", "Cs")
        for char in prefix
    ):
        raise ValueError("prefix must not contain control or formatting characters")


def _validate_words(words: object) -> None:
    """Reject word counts outside what name generation supports (1-4)."""
    if not isinstance(words, int) or isinstance(words, bool) or not 1 <= words <= 4:
        raise ValueError("words must be an integer between 1 and 4")


def _record_stats(**deltas: int) -> None:
    """Best-effort lifetime-counter update; never raises into the caller."""
    StatsLedger().record(**deltas)


def _entry_has_tags(entry: Entry, wanted: Sequence[str]) -> bool:
    """True when ``entry`` carries every tag in ``wanted`` (AND semantics)."""
    raw = entry.get("tags")
    have = {tag for tag in raw if isinstance(tag, str)} if isinstance(raw, list) else set()
    return all(tag in have for tag in wanted)


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    """Validate user-supplied tags, preserving order and dropping duplicates."""
    if tags is None:
        return []
    if isinstance(tags, str):
        raise TypeError("tags must be a sequence of strings, not a single string")
    normalized: list[str] = []
    for tag in tags:
        if not _valid_tag(tag):
            raise ValueError(
                f"invalid tag {tag!r}: tags are lowercase, start with a letter or "
                "digit, use only a-z 0-9 . _ -, and are at most 32 characters"
            )
        if tag not in normalized:
            normalized.append(tag)
    if len(normalized) > _MAX_TAGS_PER_ENTRY:
        raise ValueError(f"at most {_MAX_TAGS_PER_ENTRY} tags are allowed per directory")
    return normalized


def _normalize_description(description: str | None) -> str | None:
    """Validate a user-supplied description (short, single-line, no controls)."""
    if description is None:
        return None
    if not isinstance(description, str):
        raise TypeError("description must be a string or None")
    if len(description.encode("utf-8", errors="surrogatepass")) > _MAX_DESCRIPTION_BYTES:
        raise ValueError(f"description must be at most {_MAX_DESCRIPTION_BYTES} bytes")
    if _has_control_character(description):
        raise ValueError("description must not contain control or formatting characters")
    return description


def _create_unique_dir(
    parent: Path,
    prefix: str,
    words: int,
    *,
    name_style: str = "secure",
    reserved: set[str] | frozenset[str] = frozenset(),
    parent_fd: int | None = None,
) -> CreatedDirectory:
    """Create a new owner-only marked directory absent from disk and the registry.

    ``reserved`` is read while the caller holds the registry lock.  This is
    essential for journal entries whose original pathname is temporarily free
    while their owned tree lives at a private ``.deleting`` path.  The name
    loop (naming policy, registry reservations) stays here; each single
    attempt — mkdir, marker, identity snapshot, all relative to the verified
    ``parent_fd`` when given — is the backend's create operation.
    """
    _validate_prefix(prefix)
    if name_style not in {"clean", "secure"}:
        raise ValueError("name_style must be an effective 'clean' or 'secure' value")
    style = name_style
    for _ in range(_MAX_NAME_ATTEMPTS):
        generated = clean_name(words) if style == "clean" else funny_name(words)
        name = f"{prefix}{generated}"
        candidate = parent / name
        if str(candidate) in reserved:
            continue
        try:
            return _BACKEND.create_owned_directory(parent, name, parent_fd=parent_fd)
        except FileExistsError:
            continue  # Name collision: try another playful name.
    raise RuntimeError(
        f"could not create a unique directory in {parent} after "
        f"{_MAX_NAME_ATTEMPTS} attempts"
    )


def _effective_name_style(parent: Path, name_style: str) -> str:
    """Choose the actual naming strategy for a verified parent directory."""
    if name_style == "secure":
        return "secure"
    private = _is_private_parent(parent)
    if name_style == "clean":
        if not private:
            raise PermissionError(
                f"refusing clean ephemdir names in {parent}: parent is not private"
            )
        return "clean"
    if name_style == "auto":
        return "clean" if private else "secure"
    raise ValueError("name_style must be 'auto', 'clean', or 'secure'")


def _is_private_parent(parent: Path) -> bool:
    """Return True only for owner-only local parent directories."""
    if os.name != "posix" or not hasattr(os, "geteuid"):
        return False
    try:
        info = os.stat(parent, follow_symlinks=False)
    except OSError:
        return False
    return _is_private_parent_stat(info)


def _is_private_parent_stat(info: os.stat_result) -> bool:
    """Return True only for an owner-only local parent stat result."""
    if os.name != "posix" or not hasattr(os, "geteuid"):
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) & 0o077 == 0
    )


def _staging_name(path: Path) -> Path:
    """Unique sibling name so a previous crashed attempt never clashes."""
    return path.parent / f".{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.deleting"


_open_directory_nofollow = _posix_backend.open_directory_nofollow


def _open_verified_staging(staging: Path, entry: Entry) -> int:
    """Open ``staging`` and prove the fd still names the claimed directory.

    Fault-injection seam for the recovery fixtures; the implementation lives
    in the POSIX backend.
    """
    return _posix_backend.open_verified_staging(staging, entry)


_rmdir_verified_child = _posix_backend.rmdir_verified_child


def _linux_mount_id_statx(fd: int) -> int | None:
    """Compatibility wrapper for tests and private callers."""
    return _shared_linux_mount_id_statx(fd)


def _linux_mount_id_fdinfo(fd: int) -> int | None:
    """Compatibility wrapper for tests and private callers."""
    return _shared_linux_mount_id_fdinfo(fd)


def _linux_mount_id_mountinfo(fd: int) -> int | None:
    """Compatibility wrapper for tests and private callers."""
    return _shared_linux_mount_id_mountinfo(fd)


def _linux_mount_id(fd: int) -> int | None:
    """Return a stable Linux mount id using the shared probe implementations."""
    for probe in (
        _linux_mount_id_statx,
        _linux_mount_id_fdinfo,
        _linux_mount_id_mountinfo,
    ):
        mount_id = probe(fd)
        if mount_id is not None:
            return mount_id
    return None


def _mount_id_for_fd(fd: int) -> int | None:
    """Return a stable mount id for fd when the platform exposes one."""
    return _linux_mount_id(fd)


_check_child_mount_boundary = _posix_backend.check_child_mount_boundary
_scan_fd_names = _posix_backend.scan_fd_names


_delete_directory_contents_fd = _posix_backend.delete_directory_contents_fd
_rmdir_verified_staging = _posix_backend.rmdir_verified_staging


def _safe_delete_backend_error() -> str | None:
    """Return why fd-bound recursive deletion is unavailable, if applicable.

    Capability seam for the platform fixtures; the probe lives in the POSIX
    backend.
    """
    return _posix_backend.safe_delete_backend_error()


def _preflight_claim_source(
    path: Path,
    parent_fd: int,
    entry: Entry,
    *,
    allow_unverified: bool,
) -> tuple[str, int | None, os.stat_result | None, int | None]:
    """Open and verify the source before any journal or pathname mutation."""
    return _BACKEND.preflight_claim_source(
        path, parent_fd, entry, allow_unverified=allow_unverified
    )


def _delete_staging_tree(staging: Path, entry: Entry) -> None:
    """Delete a verified staging tree, binding recursion to an opened directory fd.

    Fault-injection seam for the crash-window fixtures; the fd-bound deletion
    engine lives in the POSIX backend.
    """
    _BACKEND.delete_claimed_tree(staging, entry)


def _try_claim(
    registry: Registry,
    path: Path,
    expected: Entry | None,
    *,
    allow_unverified: bool,
) -> tuple[str, Path | None, Entry | None]:
    """Claim an active directory only after a non-destructive safety preflight.

    The parent and source are opened and verified before the registry enters
    ``moving``. Unsupported platforms therefore leave both the original path
    and active registry entry untouched. The actual rename is relative to the
    already-verified parent fd and has no pathname fallback.
    """
    key = str(path)
    claim_id = uuid.uuid4().hex
    parent_fd: int | None = None
    source_fd: int | None = None
    staging: Path | None = None
    entry_snapshot: Entry | None = None

    try:
        with registry.transaction() as state:
            entry = state.get(key)
            if entry is None:
                return "missing", None, None
            if expected is not None and entry != expected:
                return "changed", None, None
            if entry.get("state", "active") != "active":
                return "busy", None, None
            if _parent_trust_error(path) is not None:
                return "unsafe_parent", None, None
            ownership = _ownership(path, entry)
            if ownership == "unverified" and not allow_unverified:
                return "unverified", None, None
            if ownership == "foreign":
                return "foreign", None, None

            backend_error = _safe_delete_backend_error()
            if backend_error is not None:
                return "unsupported", None, None
            try:
                parent_fd = _open_trusted_parent(path)
            except (NotImplementedError, TypeError):
                return "unsupported", None, None
            except OSError:
                return "locked", None, None

            preflight, opened_fd, source_stat, root_mount_id = _preflight_claim_source(
                path, parent_fd, entry, allow_unverified=allow_unverified
            )
            if preflight != "ready":
                return preflight, None, None
            if opened_fd is None or source_stat is None:
                return "unsupported", None, None
            source_fd = opened_fd

            staging = _staging_name(path)
            entry.update(
                {
                    "state": "moving",
                    "claim_id": claim_id,
                    "staging_path": str(staging),
                    "dev": source_stat.st_dev,
                    "ino": source_stat.st_ino,
                }
            )
            if root_mount_id is not None:
                entry["mount_id"] = root_mount_id
            entry_snapshot = dict(entry)

        if (
            parent_fd is None
            or source_fd is None
            or staging is None
            or entry_snapshot is None
        ):
            with registry.transaction() as state:
                live = state.get(key)
                if live is not None and live.get("claim_id") == claim_id:
                    live.update({"state": "active", "claim_id": None, "staging_path": None})
            return "unsupported", None, None
        try:
            _BACKEND.rename_to_staging(path, staging, parent_fd, source_fd)
        except (NotImplementedError, TypeError):
            result = "unsupported"
        except OSError:
            result = "locked"
        else:
            result = "renamed"

        if result != "renamed":
            # Reconcile only when the filesystem proves no rename took place.
            original_probe = _path_state(path)
            staging_probe = _path_state(staging)
            with registry.transaction() as state:
                live = state.get(key)
                if live is not None and live.get("claim_id") == claim_id:
                    if original_probe == "present" and staging_probe == "missing":
                        live.update({"state": "active", "claim_id": None, "staging_path": None})
            return result, None, None

        verdict = _staging_ownership(path, staging, entry_snapshot)
        if verdict == "foreign" or (verdict == "unverified" and not allow_unverified):
            with registry.transaction() as state:
                live = state.get(key)
                if live is not None and live.get("claim_id") == claim_id:
                    live.update({"state": "recovery", "claim_id": None})
            logger.warning("%s failed verification after rename; staging left for recovery", path)
            return "busy", None, None

        with registry.transaction() as state:
            live = state.get(key)
            if live is None or live.get("claim_id") != claim_id:
                return "busy", None, None
            live["state"] = "deleting"
            claimed: Entry = dict(live)
        return "claimed", staging, claimed
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _finish_deletion(registry: Registry, key: str, staging: Path, entry: Entry) -> bool:
    """Delete a claimed staging tree without ever replacing ``original``.

    A partial failure leaves the remainder at its private staging path and in
    state ``deleting`` for a later retry.  This deliberately trades temporary
    disk leakage for the stronger invariant that failure recovery can never
    clobber a new directory created at the original pathname.
    """
    claim_id = entry.get("claim_id")
    error: Exception | None = None
    identity_lost = False
    completed = False
    delete_entry = dict(entry)

    def mark_contents_cleared() -> None:
        """Persist root-only phase before unlinking the ownership marker."""
        with registry.transaction() as state:
            live = state.get(key)
            if live is None or live.get("claim_id") != claim_id:
                raise _StagingIdentityError(
                    errno.ESTALE,
                    f"registry claim for {key} changed during deletion",
                )
            live["delete_phase"] = "root-only"
        delete_entry["delete_phase"] = "root-only"

    # Preserve the long-standing two-argument test seam. The backend consumes
    # this in-memory-only callback after all payload entries are gone; it is
    # never serialized into the registry.
    delete_entry[_CONTENTS_CLEARED_CALLBACK_KEY] = mark_contents_cleared
    try:
        _delete_staging_tree(staging, delete_entry)
        _fsync_directory(staging.parent)
        completed = True
    except _StagingIdentityError as exc:
        identity_lost = True
        error = exc
    except OSError as exc:
        error = exc
    except Exception as exc:
        error = exc

    with registry.transaction() as state:
        live = state.get(key)
        if live is None or live.get("claim_id") != claim_id:
            return False
        if completed:
            del state[key]
            return True
        # Present or temporarily inaccessible: retain the exact staging path
        # and clear the process claim so a later lock holder can retry.
        live.update({"state": "recovery" if identity_lost else "deleting", "claim_id": None})
        if error is not None:
            live["last_error"] = _bounded_error_message(error)
    if identity_lost:
        logger.warning(
            "staging path for %s changed during deletion; entry parked for recovery",
            key,
        )
    else:
        logger.warning("partial deletion of %s; leftover %s stays tracked for retry", key, staging)
    return False


def _bounded_error_message(error: BaseException, *, limit: int = 1024) -> str:
    """Return a registry-safe bounded error message."""
    message = str(error)
    encoded = message.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return message
    return encoded[:limit].decode("utf-8", errors="ignore")


def _recover_entry(
    registry: Registry,
    key: str,
    *,
    retry_recovery: bool = False,
    expected: Entry | None = None,
) -> tuple[str, Path | None, Entry | None]:
    """Reconcile one journal entry while its deletion lock is held.

    Returns ``("resume", staging, claimed_entry)`` when a verified staging
    tree should be deleted now, or a non-destructive status otherwise.  The
    function may also repair an entry back to ``active``. Missing or ambiguous
    journal paths stay in recovery until the user explicitly retries or
    forgets them.
    """
    original = Path(key)
    with registry.transaction() as state:
        entry = state.get(key)
        if entry is None:
            return "missing", None, None
        if expected is not None and entry != expected:
            return "changed", None, dict(entry)
        lifecycle = entry.get("state", "active")
        if lifecycle == "active":
            return "active", None, dict(entry)
        if _entry_compatibility_blockers(entry):
            return "blocked", None, dict(entry)
        if lifecycle == "recovery" and not retry_recovery:
            return "recovery", None, dict(entry)
        reason = _destructive_path_error(original)
        if reason is not None:
            entry.update({"state": "recovery", "claim_id": None})
            logger.warning("refusing journal recovery for %s: %s", original, reason)
            return "recovery", None, dict(entry)

        staging_value = entry.get("staging_path")
        staging = Path(staging_value) if isinstance(staging_value, str) else None
        if staging is not None and not _valid_staging_path(original, staging):
            entry.update({"state": "recovery", "claim_id": None})
            return "recovery", None, dict(entry)

        original_probe = _path_state(original)
        staging_probe = _path_state(staging) if staging is not None else "missing"
        if original_probe == "unknown" or staging_probe == "unknown":
            return "busy", None, dict(entry)
        original_here = original_probe == "present"
        staging_here = staging_probe == "present"
        root_only = entry.get("delete_phase") == "root-only"

        if not original_here and not staging_here and root_only:
            del state[key]
            return "completed", None, dict(entry)
        if not original_here and not staging_here:
            entry.update({"state": "recovery", "claim_id": None})
            return "recovery", None, dict(entry)

        original_verdict = _ownership(original, entry) if original_here else "missing"
        staging_verdict = (
            _recovery_staging_ownership(original, staging, entry)
            if staging_here and staging is not None
            else "missing"
        )

        if root_only and not staging_here and original_verdict == "foreign":
            del state[key]
            return "completed", None, dict(entry)

        if original_here and staging_here:
            if staging_verdict == "ours" and original_verdict != "ours":
                # Our staging tree plus a foreign replacement at the original
                # path is unambiguous: leave the replacement alone and finish
                # deleting only the verified staging tree.
                claim_id = uuid.uuid4().hex
                entry.update({"state": "deleting", "claim_id": claim_id})
                return "resume", staging, dict(entry)
            # Any other two-path situation is ambiguous.  In particular, a
            # foreign staging collision must not cause us to reactivate and
            # then delete the original in the same or a later forced sweep.
            entry.update({"state": "recovery", "claim_id": None})
            return "recovery", None, dict(entry)

        if staging_here and staging_verdict == "ours":
            claim_id = uuid.uuid4().hex
            entry.update({"state": "deleting", "claim_id": claim_id})
            return "resume", staging, dict(entry)

        if original_here and original_verdict == "ours":
            # Intent was saved but rename never happened, or a legacy build
            # already moved the tree back. A foreign staging collision is untouched.
            entry.update({"state": "active", "claim_id": None, "staging_path": None})
            return "active", None, dict(entry)

        if staging_here and staging_verdict == "foreign":
            # The private path was reused by something else; no owned tree is
            # safely reachable by pathname.  Never touch the replacement and do
            # not silently forget the claim; the owned tree may have been moved
            # elsewhere by the same race.
            entry.update({"state": "recovery", "claim_id": None})
            return "recovery", None, dict(entry)

        if original_here and original_verdict == "foreign" and not staging_here:
            entry.update({"state": "recovery", "claim_id": None})
            return "recovery", None, dict(entry)

        entry.update({"state": "recovery", "claim_id": None})
        return "recovery", None, dict(entry)


def _execute_remove(
    path: Path,
    registry: Registry,
    *,
    missing_ok: bool = False,
    expected: Entry | None = None,
) -> bool:
    """Safely delete a tracked directory for an explicit removal request.

    Runs the full safety battery: the deletion guard, the per-directory
    deletion lock, the journaled claim (which re-verifies the entry and the
    ownership marker) and the retryable staging delete. There is deliberately
    no rollback over the original pathname and no recursive fallback — failing
    loudly is safer than deleting whatever currently occupies the path. Returns
    ``False`` when the
    entry is gone and ``missing_ok`` is true; raises :class:`OSError` for
    every unsafe outcome.
    """
    reason = _deletion_guard(path)
    if reason is not None:
        raise SafetyError(f"refusing to delete {path}: {reason}")
    hint = expected or registry.load(read_only=True).get(str(path)) or {}
    with registry.deletion_lock(_deletion_lock_key(str(path), hint)) as acquired:
        if not acquired:
            raise OSError(
                f"{path} is currently being processed by another ephemdir process; "
                "try again shortly"
            )
        # Explicitly naming a directory counts as user confirmation, so legacy
        # entries without a marker may be removed here (unlike in sweeps).
        status, staging, entry = _try_claim(registry, path, expected, allow_unverified=True)
        if status == "missing":
            if missing_ok:
                return False
            raise OSError(
                f"{path} is not tracked by ephemdir (already kept or removed); "
                "refusing to delete"
            )
        if status == "foreign":
            raise OSError(
                f"refusing to delete {path}: it is not the directory ephemdir created "
                "(the original was deleted and something else took its place)"
            )
        if status == "locked":
            raise OSError(f"could not remove {path}: a file inside is locked or in use")
        if status == "changed":
            raise OSError(f"refusing to delete {path}: its registry entry changed concurrently")
        if status == "unsupported":
            raise UnsupportedPlatformError(
                errno.ENOTSUP,
                f"safe recursive deletion is unavailable for {path}; original remains in place",
            )
        if status == "unsafe_parent":
            reason = _parent_trust_error(path) or "parent directory is not trusted"
            raise OSError(f"refusing to delete {path}: {reason}")
        if status == "busy":
            raise OSError(
                f"{path} is mid-deletion or needs recovery; a sweep will reconcile it"
            )
        if staging is None or entry is None:
            raise OSError(f"invalid claim payload for {path}; entry remains tracked")
        if not _finish_deletion(registry, str(path), staging, entry):
            raise OSError(
                f"could not fully remove {path}; the remainder stays tracked and will "
                "be retried by the next sweep"
            )
    return True
