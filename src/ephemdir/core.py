"""Core API: create, track and clean up ephemeral directories."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import math
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ._config import load_config
from ._inuse import is_in_use
from ._naming import funny_name
from ._platform import boot_session_id, boot_time, same_boot, user_config_dir, user_data_dir
from ._registry import Entry, Registry

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
}

# A lifetime may be given as seconds (int/float), a timedelta, a human string
# like "2h", "1h30m", "90s", or None for "no time limit" (restart-only).
Lifetime = int | float | str | timedelta | None

# Maximum attempts to find a free, unique directory name before giving up.
_MAX_NAME_ATTEMPTS = 100

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

_MARKER_NAME = ".ephemdir"
_MARKER_ID_PATTERN = re.compile(rb"[0-9a-f]{32}")
_MARKER_PAYLOAD_MAX_BYTES = 33  # 32 lowercase hex characters plus one LF.


def _parse_marker_payload(payload: bytes) -> str | None:
    """Validate and decode one bounded ownership-marker payload."""
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    if _MARKER_ID_PATTERN.fullmatch(payload) is None:
        return None
    return payload.decode("ascii")


def _write_marker(path: Path, marker_id: str | None = None, *, dir_fd: int | None = None) -> str:
    """Create the ownership marker inside ``path``; return its id.

    Passing an explicit ``marker_id`` recreates a known marker during safe
    recovery or test setup without changing the ownership identity. When
    ``dir_fd`` names the already-opened directory, the marker is created
    relative to that descriptor so a concurrent path swap cannot redirect it.
    """
    marker_id = marker_id or uuid.uuid4().hex
    try:
        encoded = marker_id.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("marker_id must be 32 lowercase hexadecimal characters") from error
    if _MARKER_ID_PATTERN.fullmatch(encoded) is None:
        raise ValueError("marker_id must be 32 lowercase hexadecimal characters")

    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if dir_fd is not None:
        fd = os.open(_MARKER_NAME, flags, 0o600, dir_fd=dir_fd)
    else:
        fd = os.open(path / _MARKER_NAME, flags, 0o600)
    try:
        payload = encoded + b"\n"
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count == 0:
                raise OSError(errno.EIO, "short write while creating ownership marker")
            written += count
    finally:
        os.close(fd)
    return marker_id


def _read_marker(path: Path) -> str | None:
    """Read a marker through a no-follow directory descriptor."""
    try:
        dir_fd = _open_directory_nofollow(path)
    except (OSError, NotImplementedError, TypeError):
        return None
    try:
        return _read_marker_at(dir_fd)
    finally:
        os.close(dir_fd)


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


class _StagingIdentityError(OSError):
    """Raised when a staging pathname no longer names the verified directory."""


@dataclass
class _DeleteFrame:
    fd: int
    names: Iterator[str]
    parent_fd: int | None
    name: str | None
    inode: tuple[int, int]
    close: bool


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("__spare2", ctypes.c_uint64 * 13),
    ]


_AT_EMPTY_PATH = 0x1000
_AT_NO_AUTOMOUNT = 0x800
_STATX_BASIC_STATS = 0x000007FF
_STATX_MNT_ID = 0x00001000
_LIBC: ctypes.CDLL | None = None
_STATX_UNAVAILABLE = False


def _linux_mount_id_statx(fd: int) -> int | None:
    """Return Linux mount id for an fd via ``statx``, when supported."""
    global _LIBC, _STATX_UNAVAILABLE
    if not sys.platform.startswith("linux") or _STATX_UNAVAILABLE:
        return None
    if _LIBC is None:
        try:
            _LIBC = ctypes.CDLL(None, use_errno=True)
        except OSError:
            _STATX_UNAVAILABLE = True
            return None
    statx_func = getattr(_LIBC, "statx", None)
    if statx_func is None:
        _STATX_UNAVAILABLE = True
        return None
    buffer = _Statx()
    result = statx_func(
        fd,
        ctypes.c_char_p(b""),
        _AT_EMPTY_PATH | _AT_NO_AUTOMOUNT,
        _STATX_BASIC_STATS | _STATX_MNT_ID,
        ctypes.byref(buffer),
    )
    if result != 0 or not (buffer.stx_mask & _STATX_MNT_ID):
        return None
    return int(buffer.stx_mnt_id)


def _linux_mount_id_fdinfo(fd: int) -> int | None:
    """Return Linux mount id from ``/proc/self/fdinfo`` as a statx fallback.

    ``mnt_id`` has been exposed by procfs since Linux 3.15, substantially older
    than ``STATX_MNT_ID``.  The file belongs to this process and identifies the
    already-open descriptor, so no user-controlled pathname is followed.
    """
    if not sys.platform.startswith("linux"):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        info_fd = os.open(f"/proc/self/fdinfo/{fd}", flags)
        with os.fdopen(info_fd, "r", encoding="ascii") as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if separator and key == "mnt_id":
                    parsed = value.strip()
                    return int(parsed) if parsed.isdecimal() else None
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return None


def _unescape_mountinfo_path(value: str) -> str:
    """Decode the octal escapes used for paths in ``/proc/*/mountinfo``."""
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _linux_mount_id_mountinfo(fd: int) -> int | None:
    """Map an opened descriptor to the deepest mountpoint in mountinfo.

    This fallback works on older kernels that expose neither ``STATX_MNT_ID``
    nor ``mnt_id`` in fdinfo. Selecting the longest matching mountpoint detects
    nested bind mounts even when they share the same device number.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        fd_path = os.readlink(f"/proc/self/fd/{fd}")
        if fd_path.endswith(" (deleted)"):
            fd_path = fd_path[: -len(" (deleted)")]
        fd_path = os.path.normpath(fd_path)
        best: tuple[int, int] | None = None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        mountinfo_fd = os.open("/proc/self/mountinfo", flags)
        with os.fdopen(mountinfo_fd, "r", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 6 or not fields[0].isdecimal():
                    continue
                mountpoint = os.path.normpath(_unescape_mountinfo_path(fields[4]))
                if fd_path == mountpoint or fd_path.startswith(mountpoint.rstrip("/") + "/"):
                    candidate = (len(mountpoint), int(fields[0]))
                    if best is None or candidate[0] >= best[0]:
                        best = candidate
        return None if best is None else best[1]
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _linux_mount_id(fd: int) -> int | None:
    """Return a stable Linux mount id using all available kernel interfaces."""
    for probe in (
        _linux_mount_id_statx,
        _linux_mount_id_fdinfo,
        _linux_mount_id_mountinfo,
    ):
        mount_id = probe(fd)
        if mount_id is not None:
            return mount_id
    return None


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


def _inode_matches(path: Path, entry: Entry) -> bool | None:
    """Compare ``path`` against the entry's stored inode.

    Returns ``True``/``False`` for a definite answer and ``None`` when the
    entry carries no inode information to compare against.
    """
    dev, ino = entry.get("dev"), entry.get("ino")
    if not (isinstance(dev, int) and isinstance(ino, int)):
        return None
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (path_stat.st_dev, path_stat.st_ino) == (dev, ino)


def _fd_inode_matches(fd: int, entry: Entry) -> bool | None:
    """Compare an already-open directory fd against the entry's stored inode."""
    dev, ino = entry.get("dev"), entry.get("ino")
    if not (isinstance(dev, int) and isinstance(ino, int)):
        return None
    fd_stat = os.fstat(fd)
    return (fd_stat.st_dev, fd_stat.st_ino) == (dev, ino)


def _is_real_directory(path: Path) -> bool:
    """Return whether ``path`` itself is a directory, not a symlink to one."""
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(path_stat.st_mode)


def _read_marker_at(dir_fd: int) -> str | None:
    """Read a small regular marker relative to an open directory descriptor.

    The marker is attacker-controlled once untrusted code can write inside an
    ephemeral directory.  Fail closed unless no-follow and non-blocking opens
    are available, reject special files, and never read more than the exact
    marker envelope.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        return None
    flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(_MARKER_NAME, flags, dir_fd=dir_fd)
    except (OSError, NotImplementedError, TypeError):
        return None
    try:
        marker_stat = os.fstat(fd)
        if not stat.S_ISREG(marker_stat.st_mode):
            return None
        if marker_stat.st_size > _MARKER_PAYLOAD_MAX_BYTES:
            return None
        payload = os.read(fd, _MARKER_PAYLOAD_MAX_BYTES + 1)
        if len(payload) > _MARKER_PAYLOAD_MAX_BYTES:
            return None
        return _parse_marker_payload(payload)
    except OSError:
        return None
    finally:
        os.close(fd)


def _ownership(path: Path, entry: Entry) -> str:
    """Classify whether ``path`` is still the directory we registered.

    Returns ``"ours"`` (marker and inode verified), ``"foreign"`` (something
    else occupies the path now) or ``"unverified"`` (cannot be proven either
    way — such directories are never deleted automatically, only via an
    explicit ``rm``).
    """
    if not _is_real_directory(path):
        return "foreign"
    marker_id = entry.get("marker_id")
    if not isinstance(marker_id, str):
        return "unverified"
    if _read_marker(path) != marker_id:
        return "foreign"
    if _inode_matches(path, entry) is False:
        return "foreign"
    return "ours"


def _staging_ownership(original: Path, staging: Path, entry: Entry) -> str:
    """Classify a staging tree mid-deletion.

    A journaled state alone proves nothing — the staging path must be the
    private sibling derived from the original path AND be confirmed by the
    marker (while it still exists) or by the inode recorded at claim time. The
    marker may legitimately be gone after a partial ``rmtree``, which is why
    the inode is the fallback proof.
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
        if marker is None and inode_ok is True:
            return "ours"
        return "foreign"
    if inode_ok is None:
        return "unverified"
    return "ours"


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
    entry must only be dropped as stale when the path is provably missing.
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
        (user_data_dir(), "ephemdir data directory"),
        (user_config_dir(), "ephemdir config directory"),
    ):
        resolved = critical.resolve()
        if normalized == resolved:
            return label
        if normalized in resolved.parents:
            return f"ancestor of the {label}"
    return None


def _trusted_parent_error(parent: Path) -> str | None:
    """Return why ``parent`` is unsafe for claim-by-rename, or ``None``."""
    if os.name != "posix":
        return None
    try:
        parent_stat = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        return f"parent directory cannot be inspected: {exc}"
    if not stat.S_ISDIR(parent_stat.st_mode):
        return "parent path is not a real directory"
    mode = stat.S_IMODE(parent_stat.st_mode)
    writable_by_others = bool(mode & 0o022)
    if writable_by_others and not (mode & stat.S_ISVTX):
        return "parent directory is group/world-writable without sticky bit"
    if not writable_by_others and hasattr(os, "geteuid") and parent_stat.st_uid != os.geteuid():
        return "parent directory is not owned by the current user"
    return None


def _parent_trust_error(path: Path) -> str | None:
    """Return why ``path`` cannot be safely claimed under its parent."""
    return _trusted_parent_error(path.parent)


def _open_trusted_directory(directory: Path) -> int:
    """Open and re-verify a trusted directory for fd-relative operations."""
    reason = _trusted_parent_error(directory)
    if reason is not None:
        raise PermissionError(f"refusing to operate in {directory}: {reason}")
    fd = _open_directory_nofollow(directory)
    try:
        fd_stat = os.fstat(fd)
        live_stat = os.stat(directory, follow_symlinks=False)
        if (fd_stat.st_dev, fd_stat.st_ino) != (live_stat.st_dev, live_stat.st_ino):
            raise _StagingIdentityError(
                errno.ESTALE,
                f"directory {directory} changed before it could be used",
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


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
        entry = self._registry.load().get(str(self._path))
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
        to ``False``. On supported POSIX systems this is detected with ``lsof``.
        If the probe is unavailable, deletion is deferred rather than guessed.
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

    # Validate every input before any side effect (including the lazy sweep
    # below), so a bad argument can never half-run a cleanup first.
    expires_seconds = parse_lifetime(settings["lifetime"])
    _validate_prefix(str(settings["prefix"]))
    _validate_words(settings["words"])

    backend_error = _safe_delete_backend_error()
    if backend_error is not None:
        raise OSError(
            errno.ENOTSUP,
            f"cannot create an ephemeral directory safely: {backend_error}",
        )

    reg = registry or Registry()
    # Opportunistically clean up anything already due before creating more.
    sweep(registry=reg)

    parent_value = settings["parent"]
    # Normalize to an absolute path: the registry key must stay valid no matter
    # which working directory a later sweep runs from.
    if parent_value is not None:
        parent_path = Path(os.path.abspath(Path(parent_value).expanduser()))
    else:
        parent_path = Path.cwd()
    parent_path.mkdir(parents=True, exist_ok=True)
    trust_error = _trusted_parent_error(parent_path)
    if trust_error is not None:
        raise PermissionError(f"refusing to create ephemdir in {parent_path}: {trust_error}")

    now = time.time()
    expires_at = now + expires_seconds if expires_seconds is not None else None
    remove_on_restart_value = bool(settings["remove_on_restart"])
    created_boot_time = boot_time()
    created_boot_id = boot_session_id()
    path: Path | None = None
    marker_id: str | None = None

    try:
        # Reserve the ordinary pathname against every lifecycle state while the
        # registry lock is held.  A recovery entry may have no object at its
        # original path, so filesystem exclusivity alone is not sufficient.
        # Creation, the marker write and the inode snapshot are all relative to
        # descriptors of the verified parent/new directory, so no ancestor
        # rename between these steps can redirect them elsewhere.
        with reg.transaction() as state:
            parent_fd = _open_trusted_directory(parent_path)
            try:
                path = _create_unique_dir(
                    parent_path,
                    settings["prefix"],
                    settings["words"],
                    reserved=frozenset(state),
                    parent_fd=parent_fd,
                )
                dir_fd = _open_directory_nofollow(path.name, dir_fd=parent_fd)
                try:
                    marker_id = _write_marker(path, dir_fd=dir_fd)
                    path_stat = os.fstat(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                os.close(parent_fd)
            entry: Entry = {
                "created_at": now,
                "expires_at": expires_at,
                "remove_on_restart": remove_on_restart_value,
                "keep_while_in_use": bool(settings["keep_while_in_use"]),
                "boot_time": created_boot_time,
                "boot_id": created_boot_id,
                "marker_id": marker_id,
                "state": "active",
                "claim_id": None,
                "staging_path": None,
            }
            if path_stat.st_ino:  # 0 means the filesystem does not report inodes
                entry["dev"] = path_stat.st_dev
                entry["ino"] = path_stat.st_ino
            if str(path) in state:
                # Defensive assertion against future refactors that weaken the
                # reservation check. Never overwrite a deletion journal.
                raise FileExistsError(f"registry path became reserved: {path}")
            state[str(path)] = entry
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

    Interrupted journal entries are reconciled under the same per-directory
    OS lock used by claim/delete.  This prevents a live claim from being
    mistaken for a crashed one by a concurrent sweep.
    """
    reg = registry or Registry()
    now = time.time()
    current_boot = boot_time()
    current_boot_id = boot_session_id()
    stale = 0
    journal: list[tuple[Path, Entry]] = []
    candidates: list[tuple[Path, Entry]] = []

    # Phase 1: take snapshots only.  Journal recovery must not run while merely
    # holding the registry lock: the deletion lock is the authority that tells
    # us whether the owner of a moving/deleting transaction is still alive.
    with reg.transaction() as state:
        for key in list(state.keys()):
            entry = state[key]
            path = Path(key)
            entry_state = entry.get("state", "active")
            if entry_state != "active":
                journal.append((path, dict(entry)))
                continue

            probe = _path_state(path)
            if probe == "missing":
                del state[key]
                stale += 1
                continue
            if probe == "unknown":
                logger.warning("cannot probe %s; keeping its entry untouched", path)
                continue

            reason = _deletion_guard(path)
            if reason is not None:
                logger.warning("refusing to manage %s (%s); dropping the entry", path, reason)
                del state[key]
                continue

            if not (force or _is_due(entry, now, current_boot, current_boot_id)):
                continue

            ownership = _ownership(path, entry)
            if ownership == "foreign":
                logger.warning(
                    "%s is no longer the directory ephemdir created; leaving it "
                    "alone and dropping the entry",
                    path,
                )
                del state[key]
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
            elif status == "recovery":
                logger.warning(
                    "%s needs manual attention; run `ephemdir recover %s --retry` "
                    "after resolving the conflicting paths, or `--forget` to stop tracking",
                    path,
                    path.name,
                )

    # Process active due entries.  Slow lsof/rmtree work stays outside the
    # registry lock; the per-directory lock prevents overlapping sweep calls.
    for path, snapshot in candidates:
        if not force and snapshot.get("keep_while_in_use"):
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

    if stale:
        logger.info(
            "dropped %d stale entr%s (directories deleted outside ephemdir)",
            stale,
            "y" if stale == 1 else "ies",
        )
    return removed


def registered(*, registry: Registry | None = None) -> dict[str, Entry]:
    """Return a snapshot of all currently tracked directories."""
    reg = registry or Registry()
    return reg.load()


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
        raise LookupError(f"no tracked directory matches {text!r}")
    if len(matches) > 1:
        names = ", ".join(sorted(path.name for path in matches))
        raise LookupError(f"{text!r} is ambiguous; it matches: {names}")
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
        for key, entry in reg.load().items()
        if entry.get("state", "active") == "active"
    }
    path = _match_target(target, state)
    snapshot = dict(state[str(path)])
    probe = _path_state(path)
    if probe == "missing":
        with reg.transaction() as live:
            current = live.get(str(path))
            if current == snapshot and _path_state(path) == "missing":
                del live[str(path)]
            elif current is not None:
                raise LookupError(
                    f"{path} changed while resolving; retry the command"
                ) from None
        raise LookupError(
            f"{path} was tracked but no longer exists "
            "(deleted outside ephemdir); the stale entry has been removed"
        )
    if probe == "unknown":
        raise LookupError(
            f"{path} is temporarily inaccessible; its registry entry was preserved"
        )
    return path, snapshot


def resolve(target: str | os.PathLike[str], *, registry: Registry | None = None) -> Path:
    """Resolve ``target`` to an active tracked directory.

    A temporarily inaccessible path is preserved in the registry and reported
    as unavailable; only a definite ``ENOENT`` removes the stale entry.
    """
    reg = registry or Registry()
    path, _ = _resolve_active(target, registry=reg)
    return path


def keep(target: str | os.PathLike[str], *, registry: Registry | None = None) -> Path:
    """Stop tracking a directory without deleting it; return its path.

    The directory becomes permanent as far as ephemdir is concerned — it will
    never be auto-removed. ``target`` accepts anything :func:`resolve` does.
    """
    reg = registry or Registry()
    path, snapshot = _resolve_active(target, registry=reg)
    with reg.transaction() as state:
        entry = state.get(str(path))
        if entry != snapshot:
            # The entry vanished (or was claimed) between resolve() and here —
            # a sweep won the race, so the directory can no longer be saved.
            raise LookupError(
                f"{path} is no longer tracked (a concurrent sweep claimed it first)"
            )
        del state[str(path)]
    # Only delete the marker when it is verifiably ours: a replaced directory
    # may contain the user's own .ephemdir file, a legacy one never had any.
    _remove_marker_if_ours(path, entry.get("marker_id"))
    logger.info("kept %s (no longer tracked)", path)
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
        entry["expires_at"] = expires_at
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
    all_state = reg.load()
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
        if status == "resume":
            if staging is None or claimed is None:
                raise OSError(f"invalid recovery payload for {path}; entry remains tracked")
            if not _finish_deletion(reg, key, staging, claimed):
                raise OSError(
                    f"could not fully remove {path}; the staging remainder stays tracked"
                )
        elif status == "recovery":
            raise OSError(
                f"{path} is still ambiguous; resolve the conflicting original/staging "
                "paths and retry, or use --forget to stop tracking without deleting"
            )
    return path


def prune(*, registry: Registry | None = None) -> int:
    """Drop registry entries whose directories were deleted outside ephemdir.

    Returns the number of stale entries removed. Sweeps do this automatically;
    ``prune`` only tidies the registry without deleting anything from disk.
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
            if _path_state(Path(key)) == "missing":
                del state[key]
                pruned += 1
    if pruned:
        logger.info("pruned %d stale entr%s", pruned, "y" if pruned == 1 else "ies")
    return pruned


def dir_status(
    entry: Entry,
    path: Path,
    now: float,
    current_boot: float | None,
    current_boot_id: str | None = None,
) -> str:
    """Classify a registry entry for display purposes.

    Returns one of ``"missing"`` (deleted outside ephemdir), ``"deleting"``
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
    probe = _path_state(path)
    if probe == "missing":
        return "missing"
    if probe == "unknown":
        return "unavailable"
    ownership = _ownership(path, entry)
    if ownership == "foreign":
        return "replaced"
    if ownership == "unverified":
        return "legacy"
    if _is_due(entry, now, current_boot, current_boot_id):
        return "expired"
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, (int, float)):
        remaining = float(expires_at) - now
        return "expiring" if remaining <= _EXPIRING_SOON_SECONDS else "active"
    return "until-restart" if entry.get("remove_on_restart") else "kept"


def _is_due(
    entry: Entry,
    now: float,
    current_boot: float | None,
    current_boot_id: str | None = None,
) -> bool:
    """Decide whether a registry entry should be cleaned up now."""
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, (int, float)) and now >= float(expires_at):
        return True
    if entry.get("remove_on_restart"):
        created_id = entry.get("boot_id")
        if current_boot_id is not None:
            # A stable session id is authoritative.  If an older entry lacks
            # one, a reboot cannot be proven safely, so keep the directory.
            return isinstance(created_id, str) and created_id != current_boot_id
        if isinstance(created_id, str):
            # We recorded a stable id but cannot read the current one now.
            return False
        if sys.platform == "darwin":
            # macOS kern.boottime is a stored kernel timestamp, not wall-clock
            # minus uptime, so it is safe as the only timestamp fallback.
            created_boot = entry.get("boot_time")
            created_boot = created_boot if isinstance(created_boot, (int, float)) else None
            return not same_boot(created_boot, current_boot)
        # Linux/Windows uptime-derived timestamps move when the wall clock is
        # stepped; absence of a stable id must fail safe rather than look like
        # a reboot.
        return False
    return False


def _validate_prefix(prefix: str) -> None:
    """Reject path separators and control characters in generated names."""
    if not prefix:
        return
    separators = {os.sep} | ({os.altsep} if os.altsep else set())
    if any(sep in prefix for sep in separators) or os.path.isabs(prefix):
        raise ValueError(
            f"prefix {prefix!r} must not contain path separators; the directory "
            "is always created directly under parent"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in prefix):
        raise ValueError("prefix must not contain control characters")


def _validate_words(words: object) -> None:
    """Reject word counts outside what name generation supports (1-4)."""
    if not isinstance(words, int) or isinstance(words, bool) or not 1 <= words <= 4:
        raise ValueError("words must be an integer between 1 and 4")


def _create_unique_dir(
    parent: Path,
    prefix: str,
    words: int,
    *,
    reserved: set[str] | frozenset[str] = frozenset(),
    parent_fd: int | None = None,
) -> Path:
    """Create a new owner-only directory absent from disk and the registry.

    ``reserved`` is read while the caller holds the registry lock.  This is
    essential for journal entries whose original pathname is temporarily free
    while their owned tree lives at a private ``.deleting`` path.  When
    ``parent_fd`` is given, the directory is created relative to that already
    verified descriptor so an ancestor swapped mid-operation cannot redirect
    the creation.
    """
    _validate_prefix(prefix)
    for _ in range(_MAX_NAME_ATTEMPTS):
        name = f"{prefix}{funny_name(words)}"
        candidate = parent / name
        if str(candidate) in reserved:
            continue
        try:
            if parent_fd is not None:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            else:
                candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue  # Name collision: try another playful name.
    raise RuntimeError(
        f"could not create a unique directory in {parent} after "
        f"{_MAX_NAME_ATTEMPTS} attempts"
    )


def _staging_name(path: Path) -> Path:
    """Unique sibling name so a previous crashed attempt never clashes."""
    return path.parent / f".{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.deleting"


def _open_directory_nofollow(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
    """Open a directory itself, refusing symlinks when the platform supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if dir_fd is None:
        return os.open(path, flags)
    return os.open(path, flags, dir_fd=dir_fd)


def _open_verified_staging(staging: Path, entry: Entry) -> int:
    """Open ``staging`` and prove the fd still names the claimed directory."""
    fd = _open_directory_nofollow(staging)
    try:
        fd_stat = os.fstat(fd)
        if not stat.S_ISDIR(fd_stat.st_mode):
            raise _StagingIdentityError(errno.ENOTDIR, "staging path is not a directory")

        inode_ok = _fd_inode_matches(fd, entry)
        if inode_ok is False:
            raise _StagingIdentityError(
                errno.ESTALE,
                f"{staging} no longer matches the claimed directory inode",
            )

        marker_id = entry.get("marker_id")
        marker = _read_marker_at(fd)
        if isinstance(marker_id, str):
            if marker is not None and marker != marker_id:
                raise _StagingIdentityError(
                    errno.ESTALE,
                    f"{staging} no longer carries ephemdir's ownership marker",
                )
            if marker is None and inode_ok is None:
                raise _StagingIdentityError(
                    errno.ESTALE,
                    f"{staging} cannot be verified without marker or inode",
                )
        elif inode_ok is None:
            raise _StagingIdentityError(
                errno.ESTALE,
                f"{staging} cannot be verified without marker or inode",
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rmdir_verified_child(parent_fd: int, name: str, child_fd: int) -> None:
    """Remove an empty child directory only while its name still matches ``child_fd``."""
    child_stat = os.fstat(child_fd)
    try:
        live_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _StagingIdentityError(
            errno.ESTALE,
            f"directory entry {name!r} disappeared before removal",
        ) from exc
    if (live_stat.st_dev, live_stat.st_ino) != (child_stat.st_dev, child_stat.st_ino):
        raise _StagingIdentityError(
            errno.ESTALE,
            f"directory entry {name!r} was replaced before removal",
        )
    os.rmdir(name, dir_fd=parent_fd)


def _mount_id_for_fd(fd: int) -> int | None:
    """Return a stable mount id for fd when the platform exposes one."""
    return _linux_mount_id(fd)


def _check_child_mount_boundary(child_fd: int, *, root_dev: int, root_mount_id: int | None) -> None:
    """Fail closed when a child directory crosses a filesystem or mount boundary."""
    child_stat = os.fstat(child_fd)
    if child_stat.st_dev != root_dev:
        raise OSError(
            errno.EXDEV,
            "refusing to cross filesystem boundary while deleting staging tree",
        )
    if sys.platform.startswith("linux"):
        child_mount_id = _mount_id_for_fd(child_fd)
        if root_mount_id is None or child_mount_id is None:
            raise OSError(
                errno.ENOTSUP,
                "cannot verify Linux mount boundary while deleting staging tree",
            )
        if child_mount_id != root_mount_id:
            raise OSError(
                errno.EXDEV,
                "refusing to cross mount boundary while deleting staging tree",
            )


def _scan_fd_names(dir_fd: int) -> Iterator[str]:
    """Yield directory entry names from an fd without materializing the listing."""
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            yield entry.name


def _delete_directory_contents_fd(
    dir_fd: int,
    *,
    root_dev: int,
    root_mount_id: int | None,
) -> None:
    """Remove directory contents relative to an fd using an explicit stack."""
    root_stat = os.fstat(dir_fd)
    root_id = (root_stat.st_dev, root_stat.st_ino)
    seen = {root_id}
    stack = [
        _DeleteFrame(
            fd=dir_fd,
            names=_scan_fd_names(dir_fd),
            parent_fd=None,
            name=None,
            inode=root_id,
            close=False,
        )
    ]
    try:
        while stack:
            frame = stack[-1]
            current_fd = frame.fd
            try:
                name = next(frame.names)
            except StopIteration:
                stack.pop()
                seen.discard(frame.inode)
                try:
                    close_method = getattr(frame.names, "close", None)
                    if close_method is not None:
                        close_method()
                    if frame.parent_fd is not None and frame.name is not None:
                        _rmdir_verified_child(frame.parent_fd, frame.name, current_fd)
                finally:
                    if frame.close:
                        os.close(current_fd)
                continue

            if name in {".", ".."}:
                continue
            try:
                child_stat = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue

            if stat.S_ISDIR(child_stat.st_mode):
                child_fd = _open_directory_nofollow(name, dir_fd=current_fd)
                pushed = False
                try:
                    opened_stat = os.fstat(child_fd)
                    if (opened_stat.st_dev, opened_stat.st_ino) != (
                        child_stat.st_dev,
                        child_stat.st_ino,
                    ):
                        raise _StagingIdentityError(
                            errno.ESTALE,
                            f"directory entry {name!r} changed before traversal",
                        )
                    _check_child_mount_boundary(
                        child_fd,
                        root_dev=root_dev,
                        root_mount_id=root_mount_id,
                    )
                    child_id = (opened_stat.st_dev, opened_stat.st_ino)
                    if child_id in seen:
                        raise OSError(
                            errno.ELOOP,
                            "refusing to recurse into a repeated directory inode",
                        )
                    seen.add(child_id)
                    stack.append(
                        _DeleteFrame(
                            fd=child_fd,
                            names=_scan_fd_names(child_fd),
                            parent_fd=current_fd,
                            name=name,
                            inode=child_id,
                            close=True,
                        )
                    )
                    pushed = True
                finally:
                    if not pushed:
                        os.close(child_fd)
            else:
                try:
                    os.unlink(name, dir_fd=current_fd)
                except FileNotFoundError:
                    continue
    finally:
        while len(stack) > 1:
            frame = stack.pop()
            close_method = getattr(frame.names, "close", None)
            if close_method is not None:
                close_method()
            if frame.close:
                os.close(frame.fd)


def _rmdir_verified_staging(staging: Path, staging_fd: int) -> None:
    """Remove the empty staging directory if the pathname still names ``staging_fd``."""
    fd_stat = os.fstat(staging_fd)
    try:
        live_stat = os.stat(staging, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _StagingIdentityError(
            errno.ESTALE,
            f"{staging} disappeared before final directory removal",
        ) from exc
    if (live_stat.st_dev, live_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
        raise _StagingIdentityError(
            errno.ESTALE,
            f"{staging} was replaced before final directory removal",
        )
    os.rmdir(staging)


def _safe_delete_backend_error() -> str | None:
    """Return why fd-bound recursive deletion is unavailable, if applicable."""
    if os.name != "posix":
        return "safe handle-bound recursive deletion is unavailable on this platform"
    required_dir_fd = (os.open, os.stat, os.unlink, os.rmdir, os.rename, os.mkdir)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        return "required dir_fd filesystem operations are unavailable"
    if os.scandir not in os.supports_fd:
        return "scandir cannot enumerate an opened directory descriptor"
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        return "safe no-follow directory opens are unavailable"
    return None


def _preflight_claim_source(
    path: Path,
    parent_fd: int,
    entry: Entry,
    *,
    allow_unverified: bool,
) -> tuple[str, int | None, os.stat_result | None, int | None]:
    """Open and verify the source before any journal or pathname mutation."""
    try:
        source_fd = _open_directory_nofollow(path.name, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError):
        return "foreign", None, None, None
    except (NotImplementedError, TypeError):
        return "unsupported", None, None, None
    keep_open = False
    try:
        source_stat = os.fstat(source_fd)
        live_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(source_stat.st_mode):
            return "foreign", None, None, None
        if (source_stat.st_dev, source_stat.st_ino) != (live_stat.st_dev, live_stat.st_ino):
            return "changed", None, None, None

        inode_ok = _fd_inode_matches(source_fd, entry)
        marker_id = entry.get("marker_id")
        marker = _read_marker_at(source_fd)
        if isinstance(marker_id, str):
            if marker != marker_id or inode_ok is False:
                return "foreign", None, None, None
            verified = True
        else:
            verified = inode_ok is True
        if not verified and not allow_unverified:
            return "unverified", None, None, None

        root_mount_id = _mount_id_for_fd(source_fd)
        if sys.platform.startswith("linux") and root_mount_id is None:
            return "unsupported", None, None, None
        keep_open = True
        return "ready", source_fd, source_stat, root_mount_id
    except (NotImplementedError, TypeError):
        return "unsupported", None, None, None
    except OSError:
        return "locked", None, None, None
    finally:
        if not keep_open:
            os.close(source_fd)


def _delete_staging_tree(staging: Path, entry: Entry) -> None:
    """Delete a verified staging tree, binding recursion to an opened directory fd."""
    backend_error = _safe_delete_backend_error()
    if backend_error is not None:
        raise OSError(errno.ENOTSUP, backend_error)

    staging_fd = _open_verified_staging(staging, entry)
    try:
        root_stat = os.fstat(staging_fd)
        stored_mount_id = entry.get("mount_id")
        root_mount_id = _mount_id_for_fd(staging_fd)
        if sys.platform.startswith("linux") and root_mount_id is None:
            raise OSError(
                errno.ENOTSUP,
                "cannot verify Linux mount boundary while deleting staging tree",
            )
        if (
            isinstance(stored_mount_id, int)
            and root_mount_id is not None
            and stored_mount_id != root_mount_id
        ):
            raise _StagingIdentityError(
                errno.ESTALE,
                "staging directory moved to a different mount after claim",
            )
        _delete_directory_contents_fd(
            staging_fd,
            root_dev=root_stat.st_dev,
            root_mount_id=root_mount_id,
        )
        _rmdir_verified_staging(staging, staging_fd)
    finally:
        os.close(staging_fd)


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
                del state[key]
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
                if preflight == "foreign":
                    del state[key]
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
            os.rename(path.name, staging.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            moved_stat = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
            source_stat = os.fstat(source_fd)
            if (moved_stat.st_dev, moved_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino):
                raise _StagingIdentityError(
                    errno.ESTALE, f"{staging} does not name the preflighted source directory"
                )
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
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
    try:
        _delete_staging_tree(staging, entry)
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
            live["last_error"] = str(error)
    if identity_lost:
        logger.warning(
            "staging path for %s changed during deletion; entry parked for recovery",
            key,
        )
    else:
        logger.warning("partial deletion of %s; leftover %s stays tracked for retry", key, staging)
    return False


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
    function may also repair an entry back to ``active`` or drop it when both
    paths are provably gone.
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
        if lifecycle == "recovery" and not retry_recovery:
            return "recovery", None, dict(entry)
        reason = _deletion_guard(original)
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

        if not original_here and not staging_here:
            del state[key]
            return "dropped", None, None

        original_verdict = _ownership(original, entry) if original_here else "missing"
        staging_verdict = (
            _staging_ownership(original, staging, entry)
            if staging_here and staging is not None
            else "missing"
        )

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
            del state[key]
            return "foreign", None, None

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
        raise OSError(f"refusing to delete {path}: {reason}")
    hint = expected or registry.load().get(str(path)) or {}
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
            raise OSError(
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
