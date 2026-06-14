"""Resolve optional system tools without consulting attacker-controlled paths."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

_TRUSTED_POSIX_DIRS = (Path("/usr/bin"), Path("/usr/sbin"), Path("/bin"), Path("/sbin"))


def _windows_system_directory() -> Path | None:
    """Return the real Windows system directory from the kernel, not the environment."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_system_directory = kernel32.GetSystemDirectoryW
        get_system_directory.restype = ctypes.c_uint
        size = 260
        while size <= 32768:
            buffer = ctypes.create_unicode_buffer(size)
            length = int(get_system_directory(buffer, size))
            if length == 0:
                return None
            if length < size:
                value = buffer.value
                return Path(value) if value else None
            size = length + 1
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


def trusted_system_dirs() -> tuple[Path, ...]:
    """Return fixed system directories used for optional helper programs."""
    if sys.platform == "win32":
        system_directory = _windows_system_directory()
        return () if system_directory is None else (system_directory,)
    return _TRUSTED_POSIX_DIRS


def _component_is_trusted(info: os.stat_result, current_uid: int | None) -> bool:
    """Whether one resolved path component resists replacement by other users."""
    if not stat.S_ISDIR(info.st_mode):
        return False
    if stat.S_IMODE(info.st_mode) & 0o022:
        return False
    if current_uid is not None and info.st_uid not in {0, current_uid}:
        return False
    return True


def _directory_chain_is_trusted(directory: Path, current_uid: int | None) -> bool:
    """Verify ``directory`` and every ancestor resist replacement by other users.

    Checking only the immediate directory is not enough: whoever owns or can
    write to *any* parent of a trusted directory can swap the directory itself
    (and thus the executable inside it). Both the lexical path and the
    ``realpath``-resolved path are walked; a symlink component is tolerated
    only because the resolved chain re-checks what it points to.
    """
    if os.name != "posix":
        return True
    absolute = Path(os.path.abspath(directory))
    try:
        resolved = Path(os.path.realpath(absolute))
    except OSError:
        return False
    chains = [absolute] if resolved == absolute else [absolute, resolved]
    for chain in chains:
        current = Path(chain.parts[0])
        for part in chain.parts[1:]:
            current = current / part
            try:
                info = os.lstat(current)
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode):
                continue  # The resolved chain re-checks the link target.
            if not _component_is_trusted(info, current_uid):
                return False
    return True


def resolve_executable_in_dirs(name: str, directories: Iterable[Path]) -> str | None:
    """Resolve a regular executable from explicitly trusted directories.

    On POSIX the directory and executable must not be writable by group/other
    users, and both must be owned by root or the current effective user.
    The directory owner matters as much as the file owner: whoever owns the
    directory -- or any directory above it -- can replace its entries
    regardless of the file's own ownership or mode, so the whole ancestor
    chain is validated. Final symlinks are rejected on every platform where
    lstat semantics are available.
    """
    executable = f"{name}.exe" if sys.platform == "win32" and not name.endswith(".exe") else name
    current_uid = os.geteuid() if hasattr(os, "geteuid") else None
    for directory in directories:
        candidate = directory / executable
        try:
            candidate_stat = os.stat(candidate, follow_symlinks=False)
            directory_stat = os.stat(directory, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(directory_stat.st_mode) or not stat.S_ISREG(candidate_stat.st_mode):
            continue
        if os.name == "posix":
            if stat.S_IMODE(directory_stat.st_mode) & 0o022:
                continue
            if current_uid is not None and directory_stat.st_uid not in {0, current_uid}:
                continue
            if stat.S_IMODE(candidate_stat.st_mode) & 0o022:
                continue
            if current_uid is not None and candidate_stat.st_uid not in {0, current_uid}:
                continue
            if not _directory_chain_is_trusted(directory, current_uid):
                continue
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def resolve_system_executable(name: str) -> str | None:
    """Resolve an executable from kernel-derived/fixed system directories only."""
    return resolve_executable_in_dirs(name, trusted_system_dirs())


def minimal_subprocess_env() -> dict[str, str]:
    """Return a small environment with a deterministic trusted PATH."""
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "TMP",
        "TEMP",
        "USER",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    directories = trusted_system_dirs()
    env["PATH"] = os.pathsep.join(str(path) for path in directories)
    if sys.platform == "win32" and directories:
        windows_root = str(directories[0].parent)
        env["SystemRoot"] = windows_root
        env["WINDIR"] = windows_root
    return env


def stable_subprocess_cwd() -> str:
    """Return a predictable working directory for optional helper programs."""
    try:
        home = Path.home()
        if home.is_dir():
            return str(home)
    except (OSError, RuntimeError):
        pass
    return os.sep
