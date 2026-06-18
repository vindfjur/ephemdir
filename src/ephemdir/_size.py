"""Size parsing and bounded directory measurement."""

from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ._mounts import (
    MountBoundary,
    MountBoundaryError,
    verify_same_mount,
)
from ._mounts import (
    mount_id_for_fd as _shared_mount_id_for_fd,
)

__all__ = ["SizeResult", "parse_size", "measure_tree"]


@dataclass(frozen=True)
class SizeResult:
    bytes: int | None
    complete: bool
    error: str | None = None


_SIZE_UNITS = {
    "b": 1,
    "": 1,
    "k": 1000,
    "kb": 1000,
    "m": 1000**2,
    "mb": 1000**2,
    "g": 1000**3,
    "gb": 1000**3,
    "t": 1000**4,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}
_MAX_SIZE_BYTES = 2**63 - 1


def parse_size(value: str | int | None) -> int | None:
    """Normalize a size limit to bytes.

    Integers are byte counts. Strings accept decimal units (MB/GB) and binary
    units (MiB/GiB). ``None`` disables the limit.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("max_size must be bytes, a size string, or None")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("max_size cannot be negative")
        if value > _MAX_SIZE_BYTES:
            raise ValueError("max_size is too large")
        return value
    if not isinstance(value, str):
        raise TypeError(f"unsupported max_size type: {type(value).__name__!r}")
    text = value.strip()
    if not text:
        raise ValueError("empty max_size string")
    number = []
    unit = []
    seen_unit = False
    for char in text:
        if char.isspace():
            seen_unit = True
            continue
        if not seen_unit and (char.isdigit() or char == "."):
            number.append(char)
        else:
            seen_unit = True
            unit.append(char)
    try:
        amount = Decimal("".join(number))
    except InvalidOperation as error:
        raise ValueError(f"could not parse max_size: {value!r}") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("max_size must be finite and non-negative")
    suffix = "".join(unit).lower()
    if suffix not in _SIZE_UNITS:
        raise ValueError(f"unknown size unit: {''.join(unit)!r}")
    result = int(amount * _SIZE_UNITS[suffix])
    if result > _MAX_SIZE_BYTES:
        raise ValueError("max_size is too large")
    return result


def measure_tree(
    path: Path,
    *,
    limit: int | None = None,
    max_entries: int = 100_000,
    max_depth: int = 256,
    max_seconds: float = 2.0,
) -> SizeResult:
    """Measure a tree using fd-relative no-follow traversal.

    When ``limit`` is provided, scanning may stop once the accumulated size is
    greater than the limit. The returned size is then a lower bound and
    ``complete`` is ``False``.
    """
    if os.name != "posix":
        return SizeResult(None, complete=False, error="unsupported-backend")
    if os.scandir not in os.supports_fd:
        return SizeResult(None, complete=False, error="unsupported-filesystem")
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        return SizeResult(None, complete=False, error="unsupported-filesystem")

    total = 0
    entries_seen = 0
    seen: set[tuple[int, int]] = set()
    deadline = time.monotonic() + max_seconds
    stack: list[tuple[int, int, bool]] = []
    try:
        root_fd = os.open(path, dir_flags)
    except OSError as error:
        return SizeResult(None, complete=False, error=str(error))
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            return SizeResult(None, complete=False, error="not-directory")
        root_mount_id = _mount_id_for_fd(root_fd)
        if sys.platform.startswith("linux") and root_mount_id is None:
            return SizeResult(None, complete=False, error="mount-id-unavailable")
        boundary = MountBoundary(
            root_dev=root_stat.st_dev,
            root_mount_id=root_mount_id,
            mount_id_required=sys.platform.startswith("linux"),
        )
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        seen.add(root_identity)
        stack = [(root_fd, 0, False)]
        while stack:
            if time.monotonic() > deadline:
                return SizeResult(total, complete=False, error="scan-budget-exceeded")
            current_fd, depth, close_fd = stack.pop()
            try:
                with os.scandir(current_fd) as entries:
                    for entry in entries:
                        if depth == 0 and entry.name == ".ephemdir":
                            continue
                        entries_seen += 1
                        if entries_seen > max_entries:
                            return SizeResult(
                                total,
                                complete=False,
                                error="scan-budget-exceeded",
                            )
                        try:
                            info = os.stat(
                                entry.name,
                                dir_fd=current_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            continue
                        identity = (info.st_dev, info.st_ino)
                        is_dir = stat.S_ISDIR(info.st_mode)
                        entry_fd: int | None = None
                        pushed = False
                        try:
                            if is_dir or sys.platform.startswith("linux"):
                                entry_fd = _open_entry_fd(
                                    entry.name,
                                    current_fd,
                                    is_dir=is_dir,
                                    dir_flags=dir_flags,
                                )
                                opened_stat = os.fstat(entry_fd)
                                if (opened_stat.st_dev, opened_stat.st_ino) != identity:
                                    return SizeResult(
                                        None,
                                        complete=False,
                                        error="identity-changed",
                                    )
                                try:
                                    verify_same_mount(
                                        entry_fd,
                                        boundary,
                                        mount_id_func=_mount_id_for_fd,
                                    )
                                except MountBoundaryError as error:
                                    return SizeResult(
                                        None,
                                        complete=False,
                                        error=error.code,
                                    )
                                info = opened_stat
                            if identity in seen:
                                continue
                            seen.add(identity)
                            total += _allocated_size(info)
                            if limit is not None and total > limit:
                                return SizeResult(total, complete=False)
                            if not is_dir:
                                continue
                            if depth >= max_depth:
                                return SizeResult(
                                    total,
                                    complete=False,
                                    error="scan-budget-exceeded",
                                )
                            if entry_fd is None:
                                entry_fd = os.open(entry.name, dir_flags, dir_fd=current_fd)
                            stack.append((entry_fd, depth + 1, True))
                            pushed = True
                        finally:
                            if entry_fd is not None and not pushed:
                                os.close(entry_fd)
            finally:
                if close_fd:
                    os.close(current_fd)
    except OSError as error:
        return SizeResult(None, complete=False, error=str(error))
    finally:
        for leftover_fd, _, close_fd in stack:
            if close_fd:
                try:
                    os.close(leftover_fd)
                except OSError:
                    pass
        os.close(root_fd)
    return SizeResult(total, complete=True)


def _allocated_size(info: os.stat_result) -> int:
    blocks = getattr(info, "st_blocks", None)
    if isinstance(blocks, int) and blocks > 0:
        return blocks * 512
    return int(getattr(info, "st_size", 0))


def _mount_id_for_fd(fd: int) -> int | None:
    """Return Linux mount ID for an open fd using the shared probe chain."""
    return _shared_mount_id_for_fd(fd)


def _open_entry_fd(name: str, parent_fd: int, *, is_dir: bool, dir_flags: int) -> int:
    """Open one child entry without following a final symlink."""
    if is_dir:
        return os.open(name, dir_flags, dir_fd=parent_fd)
    flags = (
        getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_PATH", 0)
    )
    if not getattr(os, "O_PATH", 0):
        flags |= os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    return os.open(name, flags, dir_fd=parent_fd)
