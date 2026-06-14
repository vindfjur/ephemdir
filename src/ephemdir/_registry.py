"""Persistent registry of tracked ephemeral directories.

The registry is a single JSON file stored in the per-user data directory. It
maps each directory's absolute path to its metadata (creation time, optional
expiry time, restart policy, the boot session it was created in and the
ownership marker id).

Writes are atomic (write-to-temp then ``os.replace``) and guarded by a real
OS-level file lock (``flock`` on POSIX, ``msvcrt.locking`` on Windows), so a
background ``sweep`` and a foreground ``tempdir()`` call cannot corrupt the
file when they run concurrently. OS locks die with their process, so a crashed
holder can never leave a stale lock behind. If the lock cannot be acquired
within the timeout a :class:`TimeoutError` is raised — the registry is never
touched without the lock held.

Loading is defensive: a corrupt registry file is quarantined (renamed to
``registry.json.corrupt-<timestamp>``) instead of being silently replaced, and
entries that do not match the expected schema are dropped with a warning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ._platform import user_data_dir

__all__ = ["Registry", "Entry", "UnsafeRegistryError"]

logger = logging.getLogger("ephemdir")


class UnsafeRegistryError(RuntimeError):
    """Raised when the registry cannot be trusted and must not be acted upon.

    Unlike a *corrupt* registry (which is quarantined and treated as empty), an
    *untrusted* registry -- one that other local users could have written to
    before this process ran -- must abort the whole operation: its contents are
    neither parsed nor swept, and the file is left in place for the user to
    inspect rather than being overwritten with an empty state.
    """

# One registry entry. Kept as a plain dict for trivial JSON serialization.
Entry = dict[str, object]

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05
_MAX_REGISTRY_BYTES = 1_048_576

# Expected types for known entry fields; anything violating these marks the
# whole entry as invalid (a poisoned entry must never reach the sweeper).
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "created_at": (int, float),
    "expires_at": (int, float, type(None)),
    "remove_on_restart": (bool,),
    "keep_while_in_use": (bool,),
    "boot_time": (int, float, type(None)),
    "boot_id": (str, type(None)),
    "marker_id": (str,),
    "dev": (int,),
    "ino": (int,),
    "state": (str,),
    "claim_id": (str, type(None)),
    "staging_path": (str, type(None)),
    "last_error": (str,),
    "mount_id": (int,),
}

# Numeric fields must be finite: JSON like ``1e999`` parses to infinity and
# would otherwise poison every later save (which refuses non-finite numbers).
_NUMERIC_FIELDS = ("created_at", "expires_at", "boot_time")

# Lifecycle states of an entry (see ephemdir.core for the deletion journal).
_VALID_STATES = frozenset({"active", "moving", "deleting", "recovery"})

# Fields every entry must carry; a bare ``{}`` (or anything hand-crafted
# without the basics) is rejected outright.
_REQUIRED_FIELDS = ("created_at", "expires_at", "remove_on_restart")

_OWNER_ONLY_DIR_MODE = 0o700
_OWNER_ONLY_FILE_MODE = 0o600
_TEMP_OPEN_ATTEMPTS = 100

if sys.platform == "win32":  # pragma: no cover - legacy compatibility path
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # released on close anyway
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    """Best-effort fsync of a directory after replacing a durable file."""
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


def _nofollow_flag() -> int:
    """Return ``O_NOFOLLOW`` when available, otherwise a no-op flag."""
    return getattr(os, "O_NOFOLLOW", 0)


def _open_owner_file(path: Path, flags: int, mode: int = _OWNER_ONLY_FILE_MODE) -> int:
    """Open a regular support file without following a final symlink."""
    fd = os.open(path, flags | _nofollow_flag(), mode)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
    except OSError:
        os.close(fd)
        raise
    return fd


def _open_nofollow(path: Path, flags: int) -> int:
    """Open an existing file without following a final symlink."""
    return os.open(path, flags | _nofollow_flag())


def _ensure_private_directory(path: Path) -> None:
    """Create and verify an owner-only directory, rejecting symlinked paths."""
    path.mkdir(mode=_OWNER_ONLY_DIR_MODE, parents=True, exist_ok=True)
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except TypeError:  # pragma: no cover - old/non-POSIX Python fallback
        path_stat = os.lstat(path)
    if not stat.S_ISDIR(path_stat.st_mode):
        raise NotADirectoryError(f"{path} is not a directory")
    if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
        raise PermissionError(f"{path} is not owned by the current user")
    if os.name == "posix" and stat.S_IMODE(path_stat.st_mode) & 0o077:
        os.chmod(path, _OWNER_ONLY_DIR_MODE)
        path_stat = os.stat(path, follow_symlinks=False)
        if stat.S_IMODE(path_stat.st_mode) & 0o077:
            raise PermissionError(f"{path} is not private to the current user")


def _open_random_temp(target: Path) -> tuple[Path, int]:
    """Create a random sibling temp file for an atomic replace."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for _ in range(_TEMP_OPEN_ATTEMPTS):
        tmp_path = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            fd = _open_owner_file(tmp_path, flags)
        except FileExistsError:
            continue
        return tmp_path, fd
    raise FileExistsError(f"could not create a unique temporary file next to {target}")


def _reject_json_constant(name: str) -> None:
    raise ValueError(f"refusing non-finite JSON constant {name!r} in the registry")


def _tighten_owner_only(fd: int, info: os.stat_result) -> None:
    """Restore ``0600`` on an owner-owned registry written by an older ephemdir.

    ephemdir <= 0.3 saved the registry with the process umask (commonly
    ``0644``, or ``0664`` under a group-writable umask), so a perfectly valid
    registry can be group/world-accessible after an upgrade. The file is
    already proven to be owned by the current user, so tighten its mode in
    place instead of quarantining real tracking data and orphaning every
    directory it records. Only a file that genuinely cannot be made private is
    treated as untrusted.
    """
    if not hasattr(os, "fchmod"):  # pragma: no cover - non-POSIX safety net
        raise ValueError("registry file is accessible to other users and cannot be secured")
    try:
        os.fchmod(fd, _OWNER_ONLY_FILE_MODE)
        secured = os.fstat(fd)
    except OSError as error:
        raise ValueError(f"registry file could not be made private: {error}") from error
    if stat.S_IMODE(secured.st_mode) & 0o077:
        raise ValueError("registry file is still accessible to other users after tightening")


def _valid_entry(key: str, entry: object) -> bool:
    """Check one registry item against the expected schema."""
    if not isinstance(entry, dict):
        return False
    if not os.path.isabs(key):
        return False
    # Reject sneaky traversal in keys: a stored path is always normalized.
    if os.path.normpath(key) != key:
        return False
    if any(field not in entry for field in _REQUIRED_FIELDS):
        return False
    for field, value in entry.items():
        expected = _FIELD_TYPES.get(field)
        if expected is None:
            continue  # unknown fields are tolerated for forward compatibility
        # bool is a subclass of int: reject True where a number is expected.
        if isinstance(value, bool) and bool not in expected:
            return False
        if not isinstance(value, expected):
            return False
        if (
            field in _NUMERIC_FIELDS
            and isinstance(value, (int, float))
            and not math.isfinite(float(value))
        ):
            return False
        if field in ("dev", "ino") and isinstance(value, int) and value < 0:
            return False
    state = entry.get("state", "active")
    if state not in _VALID_STATES:
        return False
    return True


class Registry:
    """Read/modify/write access to the persisted directory registry."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (user_data_dir() / "registry.json")
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self, *, _locked: bool = False) -> dict[str, Entry]:
        """Return the registry contents, dropping anything malformed.

        The path is opened non-blocking and without following its final
        symlink. Only a small owner-private regular file is accepted, so a FIFO,
        socket, device, oversized file or permission-poisoned replacement can
        never stall a foreground command or the scheduled sweeper.

        Invalid content is quarantined only while the registry lock is held,
        and only when the pathname still identifies the exact object that was
        inspected. An unlocked read simply reports an empty registry.
        """
        read_identity: tuple[int, int] | None = None
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = _open_nofollow(self.path, flags)
            with os.fdopen(fd, "rb") as handle:
                registry_stat = os.fstat(handle.fileno())
                read_identity = (registry_stat.st_dev, registry_stat.st_ino)
                if not stat.S_ISREG(registry_stat.st_mode):
                    raise ValueError("registry path is not a regular file")
                if hasattr(os, "getuid") and registry_stat.st_uid != os.getuid():
                    raise ValueError("registry file is not owned by the current user")
                if os.name == "posix":
                    mode = stat.S_IMODE(registry_stat.st_mode)
                    # Writable by group/other means another local user could
                    # have tampered with the policy (e.g. forced expiry) before
                    # this run. Marker/inode checks do not help: the attacker is
                    # not swapping a path, just making us delete a directory the
                    # owner really created. Refuse to trust it -- abort without
                    # parsing, sweeping or overwriting the file.
                    if mode & 0o022:
                        raise UnsafeRegistryError(
                            f"registry {self.path} is writable by other users; "
                            "refusing to trust possibly-tampered contents. "
                            "Inspect it, then `chmod 600` it once you are sure "
                            "it is intact."
                        )
                    # Owner-owned but world/group *readable* only (an older
                    # umask): valid data, so tighten it in place rather than
                    # treating it as corrupt and quarantining it.
                    if mode & 0o044:
                        _tighten_owner_only(handle.fileno(), registry_stat)
                if registry_stat.st_size > _MAX_REGISTRY_BYTES:
                    raise ValueError("registry file is larger than 1 MiB")
                raw = handle.read(_MAX_REGISTRY_BYTES + 1)
                if len(raw) > _MAX_REGISTRY_BYTES:
                    raise ValueError("registry file is larger than 1 MiB")
                # NaN/Infinity are not valid timestamps; a registry containing
                # them is treated as corrupt rather than parsed permissively.
                data = json.loads(
                    raw.decode("utf-8"),
                    parse_constant=_reject_json_constant,
                )
        except FileNotFoundError:
            return {}
        except OSError as error:
            logger.warning("could not safely open registry %s: %s", self.path, error)
            return {}
        except (ValueError, UnicodeDecodeError) as error:  # includes JSONDecodeError
            logger.warning("could not safely read registry %s: %s", self.path, error)
            if _locked and read_identity is not None:
                self._quarantine_corrupt(read_identity)
            return {}
        if not isinstance(data, dict):
            if _locked and read_identity is not None:
                self._quarantine_corrupt(read_identity)
            return {}
        valid: dict[str, Entry] = {}
        for key, entry in data.items():
            if _valid_entry(key, entry):
                valid[key] = entry
            else:
                logger.warning("ignoring malformed registry entry for %r", key)
        return valid

    def _quarantine_corrupt(self, read_identity: tuple[int, int]) -> None:
        """Move an invalid registry aside so it is kept for inspection.

        Skipped when the file on disk is no longer the one we inspected:
        another process may already have replaced it with a healthy registry,
        and that newer object must never be quarantined.
        """
        try:
            live_stat = os.stat(self.path, follow_symlinks=False)
            if (live_stat.st_dev, live_stat.st_ino) != read_identity:
                return
        except OSError:
            return
        quarantine = self.path.with_suffix(
            self.path.suffix + f".corrupt-{int(time.time())}-{uuid.uuid4().hex}"
        )
        try:
            os.replace(self.path, quarantine)
        except OSError:
            return
        logger.warning("registry %s was invalid; moved to %s", self.path, quarantine)

    def save(self, state: dict[str, Entry]) -> None:
        """Atomically persist ``state`` to disk (owner-only permissions)."""
        _ensure_private_directory(self.path.parent)
        tmp_path_value, fd = _open_random_temp(self.path)
        tmp_path: Path | None = tmp_path_value
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path_value, self.path)
            tmp_path = None
            _fsync_directory(self.path.parent)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Entry]]:
        """Lock, yield a mutable copy of the state, then save it on exit.

        Usage::

            with registry.transaction() as state:
                state[path] = entry

        Raises :class:`TimeoutError` when the lock cannot be acquired; the
        registry is never read or written without holding it.
        """
        with self._lock():
            state = self.load(_locked=True)
            yield state
            self.save(state)

    @contextmanager
    def deletion_lock(self, lock_key: str) -> Iterator[bool]:
        """Non-blocking exclusive OS lock for deleting a single directory.

        Yields ``True`` when this process holds the lock and may proceed with
        claim/recovery/delete, ``False`` when another process is already
        working on the same lock stripe — the caller must then skip it instead
        of racing into a concurrent ``rmtree``.  There are a fixed 256 stripe
        files, so long-running installations do not accumulate one lock file
        per historical directory.
        """
        _ensure_private_directory(self.path.parent)
        locks_dir = self.path.parent / "locks"
        _ensure_private_directory(locks_dir)
        # Fixed striped locks avoid leaking one permanent lock file for every
        # directory ever created.  A collision only serializes unrelated
        # deletions briefly; it cannot weaken safety.
        digest = hashlib.sha256(lock_key.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:2], "big") % 256
        lock_path = locks_dir / f"delete-{slot:03d}.lock"
        fd = _open_owner_file(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            try:
                _try_lock(fd)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                _unlock(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Acquire an exclusive OS-level lock on the registry's lock file.

        The lock file itself is permanent and never deleted: removing a file
        other processes may be flock-ing reintroduces the classic unlock race.
        The lock is tied to the file descriptor, so it disappears with the
        process — a crash cannot leave the registry permanently locked.
        """
        _ensure_private_directory(self.path.parent)
        fd = _open_owner_file(self._lock_path, os.O_CREAT | os.O_RDWR)
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"could not lock registry {self.path} within "
                            f"{_LOCK_TIMEOUT_SECONDS:.0f}s; is another ephemdir "
                            "process stuck?"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
