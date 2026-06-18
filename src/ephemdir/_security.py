"""Shared filesystem trust helpers for ephemdir.

The functions in this module deliberately avoid pathname shortcuts.  They walk
directory chains one component at a time, reject symlink components, and return
open directory descriptors that callers can keep for fd-relative work.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path

OWNER_ONLY_DIR_MODE = 0o700
DirectoryValidator = Callable[[Path, os.stat_result], str | None]


class DirectoryIdentityError(OSError):
    """Raised when a verified directory changes before it can be used."""


def trusted_component_error(path: Path, info: os.stat_result) -> str | None:
    """Return why one path component is unsafe, or ``None``."""
    if not stat.S_ISDIR(info.st_mode):
        return f"{path} is not a real directory"
    if not hasattr(os, "geteuid"):
        return None
    euid = os.geteuid()
    if info.st_uid not in {0, euid}:
        return f"{path} is not owned by the current user or root"
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 and not (mode & stat.S_ISVTX):
        return f"{path} is group/world-writable without sticky bit"
    return None


def trusted_final_parent_error(path: Path, info: os.stat_result) -> str | None:
    """Return why a final managed-directory parent is unsafe, or ``None``."""
    if not hasattr(os, "geteuid"):
        return None
    euid = os.geteuid()
    if info.st_uid == euid:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == 0 and mode & 0o022 and mode & stat.S_ISVTX:
        return None
    return f"{path} is not owned by the current user or a root-owned sticky shared directory"


def private_directory_error(path: Path, info: os.stat_result) -> str | None:
    """Return why a private application-state directory is unsafe."""
    if not stat.S_ISDIR(info.st_mode):
        return f"{path} is not a real directory"
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return f"{path} is not owned by the current user"
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        return f"{path} is not private to the current user"
    return None


def private_directory_owner_error(path: Path, info: os.stat_result) -> str | None:
    """Return why a private state dir cannot even be opened for tightening."""
    if not stat.S_ISDIR(info.st_mode):
        return f"{path} is not a real directory"
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return f"{path} is not owned by the current user"
    return None


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _fsync_fd(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


def walk_trusted_directory(
    directory: Path,
    *,
    create_missing: bool,
    allow_missing_tail: bool = False,
    final_validator: DirectoryValidator = trusted_final_parent_error,
) -> int | None:
    """Walk ``directory`` without following symlink components.

    Returns an open descriptor for the final directory.  When
    ``allow_missing_tail`` is true and a missing suffix is found, returns
    ``None`` after verifying that the existing ancestor may safely host the
    owner-only tail that a later create step would make.
    """
    if os.name != "posix":
        if create_missing:
            directory.mkdir(mode=OWNER_ONLY_DIR_MODE, parents=True, exist_ok=True)
        if allow_missing_tail and not directory.exists():
            return None
        return os.open(directory, _directory_flags())

    if not directory.is_absolute():
        raise PermissionError("directory path must be absolute")

    flags = _directory_flags()
    current_path = Path(directory.anchor)
    current_fd = os.open(directory.anchor, flags)
    try:
        root_error = trusted_component_error(current_path, os.fstat(current_fd))
        if root_error is not None:
            raise PermissionError(root_error)

        parts = directory.parts[1:]
        for index, name in enumerate(parts):
            if name in {"", ".", ".."}:
                raise PermissionError(f"unsafe directory component {name!r}")
            next_path = current_path / name
            is_final = index == len(parts) - 1
            try:
                link_info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                if allow_missing_tail and not create_missing:
                    host_error = trusted_final_parent_error(current_path, os.fstat(current_fd))
                    if host_error is not None:
                        raise PermissionError(host_error) from exc
                    os.close(current_fd)
                    return None
                if not create_missing:
                    raise
                os.mkdir(name, mode=OWNER_ONLY_DIR_MODE, dir_fd=current_fd)
                _fsync_fd(current_fd)
                link_info = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            if stat.S_ISLNK(link_info.st_mode):
                raise PermissionError(f"{next_path} is a symlink")
            if not stat.S_ISDIR(link_info.st_mode):
                raise NotADirectoryError(f"{next_path} is not a directory")

            next_fd = os.open(name, flags, dir_fd=current_fd)
            try:
                opened_info = os.fstat(next_fd)
                if (opened_info.st_dev, opened_info.st_ino) != (
                    link_info.st_dev,
                    link_info.st_ino,
                ):
                    raise DirectoryIdentityError(
                        errno.ESTALE,
                        f"{next_path} changed before it could be used",
                    )
                trust_error = trusted_component_error(next_path, opened_info)
                if trust_error is not None:
                    raise PermissionError(trust_error)
                if is_final:
                    final_error = final_validator(next_path, opened_info)
                    if final_error is not None:
                        raise PermissionError(final_error)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
            current_path = next_path
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def open_trusted_directory(directory: Path) -> int:
    """Open an existing managed-directory parent after a no-follow walk."""
    fd = walk_trusted_directory(
        directory,
        create_missing=False,
        allow_missing_tail=False,
        final_validator=trusted_final_parent_error,
    )
    if fd is None:
        raise FileNotFoundError(directory)
    return fd


def open_or_create_trusted_directory(directory: Path) -> int:
    """Create any missing tail and open a managed-directory parent safely."""
    fd = walk_trusted_directory(
        directory,
        create_missing=True,
        allow_missing_tail=False,
        final_validator=trusted_final_parent_error,
    )
    if fd is None:
        raise FileNotFoundError(directory)
    return fd


def open_private_directory(directory: Path, *, create: bool) -> int:
    """Open an owner-only application-state directory safely."""
    fd = walk_trusted_directory(
        directory,
        create_missing=create,
        allow_missing_tail=not create,
        final_validator=private_directory_owner_error,
    )
    if fd is None:
        raise FileNotFoundError(directory)
    if os.name == "posix":
        if hasattr(os, "fchmod"):
            os.fchmod(fd, OWNER_ONLY_DIR_MODE)
        final_error = private_directory_error(directory, os.fstat(fd))
        if final_error is not None:
            os.close(fd)
            raise PermissionError(final_error)
    return fd


def ensure_private_directory(directory: Path) -> None:
    """Create and verify an owner-only application-state directory."""
    fd = open_private_directory(directory, create=True)
    os.close(fd)
