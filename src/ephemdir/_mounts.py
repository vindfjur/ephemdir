"""Mount identity helpers shared by traversal and deletion code."""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "MountBoundary",
    "MountBoundaryError",
    "linux_mount_id_fdinfo",
    "linux_mount_id_mountinfo",
    "linux_mount_id_statx",
    "mount_id_for_fd",
    "verify_same_mount",
]


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


@dataclass(frozen=True)
class MountBoundary:
    root_dev: int
    root_mount_id: int | None
    mount_id_required: bool


class MountBoundaryError(RuntimeError):
    """Raised when an fd cannot be proven to stay within a mount boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def linux_mount_id_statx(fd: int) -> int | None:
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


def linux_mount_id_fdinfo(fd: int) -> int | None:
    """Return Linux mount id from ``/proc/self/fdinfo``."""
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


def linux_mount_id_mountinfo(fd: int) -> int | None:
    """Map an opened descriptor to the deepest mountpoint in mountinfo."""
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


def mount_id_for_fd(fd: int) -> int | None:
    """Return a stable Linux mount id using all available kernel interfaces."""
    if not sys.platform.startswith("linux"):
        return None
    for probe in (
        linux_mount_id_statx,
        linux_mount_id_fdinfo,
        linux_mount_id_mountinfo,
    ):
        mount_id = probe(fd)
        if mount_id is not None:
            return mount_id
    return None


def verify_same_mount(
    fd: int,
    boundary: MountBoundary,
    *,
    mount_id_func: Callable[[int], int | None] = mount_id_for_fd,
) -> None:
    """Verify that ``fd`` belongs to ``boundary`` without crossing mounts.

    Linux mount IDs are authoritative when required. ``st_dev`` remains the
    fallback for platforms that do not expose a stable mount identifier.
    """
    info = os.fstat(fd)
    if boundary.mount_id_required:
        child_mount_id = mount_id_func(fd)
        if boundary.root_mount_id is None or child_mount_id is None:
            raise MountBoundaryError("mount-id-unavailable")
        if child_mount_id != boundary.root_mount_id:
            raise MountBoundaryError("unsupported-filesystem")
        return
    if info.st_dev != boundary.root_dev:
        raise MountBoundaryError("unsupported-filesystem")
