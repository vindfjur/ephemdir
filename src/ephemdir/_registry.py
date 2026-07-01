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

Loading is defensive: a corrupt registry file blocks the command and, under the
registry lock, has its bytes copied to ``registry.json.corrupt-<timestamp>`` for
inspection. The active registry path stays in place as the blocking object. A
single malformed entry invalidates the whole registry, because filtering it out
would permanently orphan the directory it used to track.
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
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ._platform import user_data_dir
from ._security import ensure_private_directory, open_private_directory

__all__ = [
    "Registry",
    "Entry",
    "UnsafeRegistryError",
    "RegistryFormatError",
    "CorruptRegistryError",
    "RegistryTooLargeError",
    "RegistryUnavailableError",
]

logger = logging.getLogger("ephemdir")
_OS_REPLACE = os.replace


class UnsafeRegistryError(RuntimeError):
    """Raised when the registry cannot be trusted and must not be acted upon.

    An untrusted registry -- one that other local users could have written to
    before this process ran -- must abort the whole operation: its contents are
    neither parsed nor swept, and the file is left in place for the user to
    inspect rather than being overwritten with an empty state.
    """


class RegistryFormatError(RuntimeError):
    """Raised when registry content is syntactically valid but unsupported."""


class CorruptRegistryError(RegistryFormatError):
    """Raised when read-only registry content is invalid and must not be hidden."""


class RegistryTooLargeError(RegistryFormatError):
    """Raised when a registry exceeds ephemdir's bounded read/write limits."""


class RegistryUnavailableError(RuntimeError):
    """Raised when the registry object cannot be read safely right now."""


# One registry entry. Kept as a plain dict for trivial JSON serialization.
Entry = dict[str, object]

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05
_MAX_REGISTRY_BYTES = 1_048_576
_REGISTRY_SCHEMA_VERSION = 3
# Oldest on-disk envelope this ephemdir can read and migrate forward. A flat v1
# registry has no ``schema_version`` key at all and is handled separately.
_MIN_SUPPORTED_SCHEMA_VERSION = 2
_MAX_REGISTRY_ENTRIES = 10_000
_MAX_ENTRY_BYTES = 8_192
_MAX_PATH_BYTES = 4_096
_MAX_STRING_BYTES = 4_096
_MAX_LAST_ERROR_BYTES = 1_024

# User-supplied labels (schema v3). Both are untrusted input and are validated
# as strictly as every other registry field.
_MAX_TAGS_PER_ENTRY = 16
_MAX_TAG_CHARS = 32
_MAX_DESCRIPTION_BYTES = 256
_TAG_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_TAG_ALLOWED_START = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

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
    "registry_version": (int,),
    "cleanup_policy": (str,),
    "max_size": (int, type(None)),
    "name_style": (str,),
    "backend": (str,),
    "platform": (str,),
    "tags": (list,),
    "description": (str, type(None)),
}

# Numeric fields must be finite: JSON like ``1e999`` parses to infinity and
# would otherwise poison every later save (which refuses non-finite numbers).
_NUMERIC_FIELDS = ("created_at", "expires_at", "boot_time")

# Lifecycle states of an entry (see ephemdir.core for the deletion journal).
_VALID_STATES = frozenset({"active", "moving", "deleting", "recovery"})
_VALID_CLEANUP_POLICIES = frozenset({"auto", "next-sweep"})
_VALID_NAME_STYLES = frozenset({"auto", "clean", "secure"})

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


def _fsync_dir_fd(fd: int) -> None:
    """Best-effort fsync of an already verified directory descriptor."""
    try:
        os.fsync(fd)
    except OSError:
        pass


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


def _open_owner_file_at(
    dir_fd: int,
    name: str,
    flags: int,
    mode: int = _OWNER_ONLY_FILE_MODE,
) -> int:
    """Open a regular support file relative to a verified directory fd."""
    fd = os.open(name, flags | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=dir_fd)
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


def _open_nofollow_at(dir_fd: int, name: str, flags: int) -> int:
    """Open an existing file relative to a verified directory fd."""
    return os.open(name, flags | _nofollow_flag() | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)


def _ensure_private_directory(path: Path) -> None:
    """Create and verify an owner-only directory, rejecting symlinked paths."""
    ensure_private_directory(path)


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


def _open_random_temp_at(dir_fd: int, target_name: str) -> tuple[str, int]:
    """Create a random sibling temp file relative to a verified dirfd."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for _ in range(_TEMP_OPEN_ATTEMPTS):
        tmp_name = f".{target_name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            fd = _open_owner_file_at(dir_fd, tmp_name, flags)
        except FileExistsError:
            continue
        return tmp_name, fd
    raise FileExistsError(f"could not create a unique temporary file next to {target_name}")


def _safe_child_name(path: Path) -> str:
    """Return a basename safe for fd-relative registry operations."""
    name = path.name
    if name in {"", ".", ".."} or Path(name).parts != (name,):
        raise ValueError(f"unsafe registry filename: {name!r}")
    return name


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
    if not _valid_registry_path(key):
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
        if field in ("dev", "ino", "mount_id") and isinstance(value, int) and value < 0:
            return False
        if isinstance(value, str):
            limit = _MAX_LAST_ERROR_BYTES if field == "last_error" else _MAX_STRING_BYTES
            if len(value.encode("utf-8", errors="surrogatepass")) > limit:
                return False
            if field in {"backend", "platform"} and not value:
                return False
            if field in {"backend", "platform", "name_style", "cleanup_policy"}:
                if _has_control_character(value):
                    return False
            if field == "staging_path" and value and not _valid_registry_path(value):
                return False
        if field == "max_size" and isinstance(value, int) and value < 0:
            return False
        if field == "cleanup_policy" and value not in _VALID_CLEANUP_POLICIES:
            return False
        if field == "name_style" and value not in _VALID_NAME_STYLES:
            return False
        if field == "tags" and isinstance(value, list):
            if len(value) > _MAX_TAGS_PER_ENTRY:
                return False
            if not all(_valid_tag(tag) for tag in value):
                return False
        if field == "description" and isinstance(value, str):
            if len(value.encode("utf-8", errors="surrogatepass")) > _MAX_DESCRIPTION_BYTES:
                return False
            if _has_control_character(value):
                return False
    state = entry.get("state", "active")
    if state not in _VALID_STATES:
        return False
    marker_id = entry.get("marker_id")
    if marker_id is not None and not _valid_hex_id(marker_id):
        return False
    claim_id = entry.get("claim_id")
    if claim_id is not None and not _valid_hex_id(claim_id):
        return False
    staging_path = entry.get("staging_path")
    if state == "active":
        if claim_id is not None or staging_path is not None:
            return False
    elif state == "moving":
        if claim_id is None or not _valid_staging_reference(key, staging_path):
            return False
    elif state == "deleting":
        if not _valid_staging_reference(key, staging_path):
            return False
        if not isinstance(entry.get("dev"), int) or not isinstance(entry.get("ino"), int):
            return False
    elif state == "recovery" and claim_id is not None:
        return False
    return True


def _valid_hex_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _has_control_character(value: str) -> bool:
    """True if ``value`` has any C0/DEL, C1, Unicode format (Cf) or surrogate (Cs).

    Covers ASCII controls plus Unicode control/format characters such as
    U+202E RIGHT-TO-LEFT OVERRIDE and zero-width joiners, and surrogate code
    points (U+D800-U+DFFF) — the form raw undecodable bytes take under POSIX
    ``surrogateescape``, which would otherwise round-trip back to the original
    control byte on output. Matches the prefix and separator validators so
    stored fields are as strict as generated names.
    """
    return any(
        ord(char) < 32 or ord(char) == 127
        or unicodedata.category(char) in ("Cc", "Cf", "Cs")
        for char in value
    )


def _valid_tag(value: object) -> bool:
    """A tag is a short, lowercase, filesystem- and shell-safe label."""
    if not isinstance(value, str):
        return False
    if not 1 <= len(value) <= _MAX_TAG_CHARS:
        return False
    if value[0] not in _TAG_ALLOWED_START:
        return False
    return all(char in _TAG_ALLOWED_CHARS for char in value)


def _valid_registry_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if _has_control_character(value):
        return False
    if len(value.encode("utf-8", errors="surrogatepass")) > _MAX_PATH_BYTES:
        return False
    if not os.path.isabs(value):
        return False
    # Reject sneaky traversal in keys: a stored path is always normalized.
    return os.path.normpath(value) == value


def _valid_staging_reference(key: str, value: object) -> bool:
    if not _valid_registry_path(value):
        return False
    original = Path(key)
    staging = Path(str(value))
    return (
        staging.parent == original.parent
        and staging.name.startswith(f".{original.name}.")
        and staging.name.endswith(".deleting")
    )


def _registry_envelope(state: dict[str, Entry]) -> dict[str, object]:
    return {
        "schema_version": _REGISTRY_SCHEMA_VERSION,
        "writer": {
            "name": "ephemdir",
            "format": f"registry-v{_REGISTRY_SCHEMA_VERSION}",
        },
        "entries": state,
    }


def _extract_entries(data: object) -> dict[str, object]:
    """Extract the entries mapping from a flat v1, or a v2/v3 envelope.

    v2 and v3 share the same envelope and entries shape; v3 only adds the
    optional ``tags``/``description`` fields, so a v2 file reads cleanly and is
    migrated forward on the next save.
    """
    if not isinstance(data, dict):
        raise ValueError("registry top level must be an object")
    schema = data.get("schema_version")
    if schema is None:
        return data
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise RegistryFormatError("registry schema_version must be an integer")
    if schema > _REGISTRY_SCHEMA_VERSION:
        raise RegistryFormatError(
            f"registry schema {schema} is newer than this ephemdir supports"
        )
    if schema < _MIN_SUPPORTED_SCHEMA_VERSION:
        raise RegistryFormatError(f"unsupported registry schema {schema}")
    entries = data.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("registry entries must be an object")
    return entries


def _encoded_json(payload: object) -> bytes:
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    return text.encode("utf-8")


def _validate_state_bounds(state: dict[str, Entry]) -> None:
    if len(state) > _MAX_REGISTRY_ENTRIES:
        raise RegistryTooLargeError(
            f"registry has {len(state)} entries; limit is {_MAX_REGISTRY_ENTRIES}"
        )
    for key, entry in state.items():
        if not _valid_entry(key, entry):
            raise ValueError(f"registry entry for {key!r} is invalid")
        encoded = _encoded_json({key: entry})
        if len(encoded) > _MAX_ENTRY_BYTES:
            raise RegistryTooLargeError(
                f"registry entry for {key!r} is larger than {_MAX_ENTRY_BYTES} bytes"
            )


class Registry:
    """Read/modify/write access to the persisted directory registry."""

    def __init__(self, path: Path | None = None) -> None:
        raw_path = path or (user_data_dir(create=False) / "registry.json")
        self.path = Path(os.path.abspath(raw_path))
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._name = _safe_child_name(self.path)
        self._lock_name = _safe_child_name(self._lock_path)
        # Set to the on-disk format label ("v1"/"v2") when a load under the lock
        # sees an older schema, so the next save backs it up before migrating.
        self._pending_backup_label: str | None = None

    def _open_state_dir(self, *, create: bool) -> int:
        """Open the state directory that owns registry support files."""
        return open_private_directory(self.path.parent, create=create)

    def load(
        self,
        *,
        _locked: bool = False,
        read_only: bool = False,
        _dir_fd: int | None = None,
    ) -> dict[str, Entry]:
        """Return the registry contents, raising when any entry is malformed.

        The path is opened non-blocking and without following its final
        symlink. Only a small owner-private regular file is accepted, so a FIFO,
        socket, device, oversized file or permission-poisoned replacement can
        never stall a foreground command or the scheduled sweeper.

        Invalid JSON is quarantined only while the registry lock is held, and
        only when the pathname still identifies the exact object that was
        inspected. Oversized, unsupported or temporarily unavailable registries
        raise under the lock so a transaction never overwrites them with `{}`.
        """
        read_identity: tuple[int, int] | None = None
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        close_dir_fd = False
        if _dir_fd is None:
            try:
                _dir_fd = self._open_state_dir(create=False)
                close_dir_fd = True
            except FileNotFoundError:
                return {}
            except OSError as error:
                logger.warning(
                    "could not safely open registry directory %s: %s",
                    self.path.parent,
                    error,
                )
                raise RegistryUnavailableError(
                    f"could not safely open registry directory {self.path.parent}: {error}"
                ) from error
        try:
            try:
                fd = _open_nofollow_at(_dir_fd, self._name, flags)
                with os.fdopen(fd, "rb") as handle:
                    registry_stat = os.fstat(handle.fileno())
                    read_identity = (registry_stat.st_dev, registry_stat.st_ino)
                    if not stat.S_ISREG(registry_stat.st_mode):
                        raise RegistryUnavailableError(
                            f"registry {self.path} is not a regular file"
                        )
                    if hasattr(os, "getuid") and registry_stat.st_uid != os.getuid():
                        raise UnsafeRegistryError(
                            f"registry {self.path} is not owned by the current user"
                        )
                    if os.name == "posix":
                        mode = stat.S_IMODE(registry_stat.st_mode)
                        if mode & 0o022:
                            raise UnsafeRegistryError(
                                f"registry {self.path} is writable by other users; "
                                "refusing to trust possibly-tampered contents. "
                                "Inspect it, then `chmod 600` it once you are sure "
                                "it is intact."
                            )
                        if mode & 0o044 and not read_only:
                            _tighten_owner_only(handle.fileno(), registry_stat)
                        elif mode & 0o044:
                            logger.warning(
                                "registry %s is readable by other users; read-only load "
                                "will not tighten permissions",
                                self.path,
                            )
                    if registry_stat.st_size > _MAX_REGISTRY_BYTES:
                        raise RegistryTooLargeError("registry file is larger than 1 MiB")
                    raw = handle.read(_MAX_REGISTRY_BYTES + 1)
                    if len(raw) > _MAX_REGISTRY_BYTES:
                        raise RegistryTooLargeError("registry file is larger than 1 MiB")
                    data = json.loads(
                        raw.decode("utf-8"),
                        parse_constant=_reject_json_constant,
                    )
            except FileNotFoundError:
                return {}
            except RegistryFormatError as error:
                logger.warning("could not safely use registry %s: %s", self.path, error)
                raise
            except OSError as error:
                logger.warning("could not safely open registry %s: %s", self.path, error)
                raise RegistryUnavailableError(
                    f"could not safely open registry {self.path}: {error}"
                ) from error
            except (ValueError, UnicodeDecodeError) as error:
                logger.warning("could not safely read registry %s: %s", self.path, error)
                if _locked and read_identity is not None:
                    self._quarantine_corrupt(read_identity, _dir_fd)
                raise CorruptRegistryError(
                    f"registry {self.path} is corrupt: {error}"
                ) from error

            backup_label: str | None = None
            try:
                entries = _extract_entries(data)
                if isinstance(data, dict):
                    on_disk_schema = data.get("schema_version")
                    if on_disk_schema is None:
                        backup_label = "v1"
                    elif on_disk_schema < _REGISTRY_SCHEMA_VERSION:
                        backup_label = f"v{on_disk_schema}"
            except RegistryFormatError as error:
                logger.warning("could not safely use registry %s: %s", self.path, error)
                raise
            except ValueError as error:
                logger.warning("could not safely read registry %s: %s", self.path, error)
                if _locked and read_identity is not None:
                    self._quarantine_corrupt(read_identity, _dir_fd)
                raise CorruptRegistryError(
                    f"registry {self.path} is corrupt: {error}"
                ) from error

            valid: dict[str, Entry] = {}
            for key, entry in entries.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(entry, dict)
                    or not _valid_entry(key, entry)
                ):
                    reason = ValueError(f"registry entry for {key!r} is invalid")
                    logger.warning("could not safely read registry %s: %s", self.path, reason)
                    if _locked and read_identity is not None:
                        self._quarantine_corrupt(read_identity, _dir_fd)
                    raise CorruptRegistryError(
                        f"registry {self.path} is corrupt: {reason}"
                    ) from reason
                valid[key] = dict(entry)
            if _locked and backup_label is not None:
                self._pending_backup_label = backup_label
            return valid
        finally:
            if close_dir_fd and _dir_fd is not None:
                os.close(_dir_fd)

    def _quarantine_corrupt(self, read_identity: tuple[int, int], dir_fd: int) -> None:
        """Copy an invalid registry aside while leaving active path blocking.

        Skipped when the file on disk is no longer the one we inspected:
        another process may already have replaced it with a healthy registry,
        and that newer object must never be quarantined.
        """
        try:
            live_stat = os.stat(self._name, dir_fd=dir_fd, follow_symlinks=False)
            if (live_stat.st_dev, live_stat.st_ino) != read_identity:
                return
        except OSError:
            return
        quarantine_name = f"{self._name}.corrupt-{int(time.time())}-{uuid.uuid4().hex}"
        try:
            source_fd = _open_nofollow_at(
                dir_fd,
                self._name,
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                source_stat = os.fstat(source_fd)
                if (source_stat.st_dev, source_stat.st_ino) != read_identity:
                    return
                target_fd = _open_owner_file_at(
                    dir_fd,
                    quarantine_name,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                try:
                    while True:
                        chunk = os.read(source_fd, 64 * 1024)
                        if not chunk:
                            break
                        os.write(target_fd, chunk)
                    os.fsync(target_fd)
                finally:
                    os.close(target_fd)
            finally:
                os.close(source_fd)
            _fsync_dir_fd(dir_fd)
        except OSError:
            return
        logger.warning(
            "registry %s is invalid; copied bytes to %s and left active path blocked",
            self.path,
            self.path.with_name(quarantine_name),
        )

    def save(self, state: dict[str, Entry], *, _dir_fd: int | None = None) -> None:
        """Atomically persist ``state`` to disk (owner-only permissions)."""
        _validate_state_bounds(state)
        payload = _encoded_json(_registry_envelope(state))
        if len(payload) > _MAX_REGISTRY_BYTES:
            raise RegistryTooLargeError(
                f"registry would be {len(payload)} bytes; limit is {_MAX_REGISTRY_BYTES}"
            )
        close_dir_fd = False
        if _dir_fd is None:
            _dir_fd = self._open_state_dir(create=True)
            close_dir_fd = True
        tmp_name_value, fd = _open_random_temp_at(_dir_fd, self._name)
        tmp_name: str | None = tmp_name_value
        try:
            if self._pending_backup_label is not None:
                self._write_format_backup(_dir_fd, self._pending_backup_label)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _OS_REPLACE(tmp_name_value, self._name, src_dir_fd=_dir_fd, dst_dir_fd=_dir_fd)
            tmp_name = None
            _fsync_dir_fd(_dir_fd)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name, dir_fd=_dir_fd)
                except OSError:
                    pass
            if close_dir_fd and _dir_fd is not None:
                os.close(_dir_fd)
            self._pending_backup_label = None

    def _write_format_backup(self, dir_fd: int, label: str) -> None:
        """Copy the current registry aside before migrating it to a newer schema.

        ``label`` is the on-disk format being superseded ("v1", "v2", ...). The
        backup is owner-only and is created without ever replacing an existing
        backup, so a migration can never silently overwrite an earlier one.
        """
        try:
            source_fd = _open_nofollow_at(
                dir_fd,
                self._name,
                os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return
        backup_name: str | None = None
        try:
            with os.fdopen(source_fd, "rb") as source:
                backup, backup_fd = self._open_format_backup_destination(dir_fd, label)
                backup_name = backup
                with os.fdopen(backup_fd, "wb") as target:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            backup_name = None
            _fsync_dir_fd(dir_fd)
        finally:
            if backup_name is not None:
                try:
                    os.unlink(backup_name, dir_fd=dir_fd)
                except OSError:
                    pass

    def _open_format_backup_destination(self, dir_fd: int, label: str) -> tuple[str, int]:
        """Create a new backup file without replacing an existing backup."""
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        primary = f"{self._name}.{label}.bak"
        try:
            return primary, _open_owner_file_at(dir_fd, primary, flags)
        except FileExistsError:
            pass
        for _ in range(_TEMP_OPEN_ATTEMPTS):
            suffix = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            backup = f"{self._name}.{label}.{suffix}.bak"
            try:
                return backup, _open_owner_file_at(dir_fd, backup, flags)
            except FileExistsError:
                continue
        raise FileExistsError(f"could not create a unique backup next to {self.path}")

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Entry]]:
        """Lock, yield a mutable copy of the state, then save it on exit.

        Usage::

            with registry.transaction() as state:
                state[path] = entry

        Raises :class:`TimeoutError` when the lock cannot be acquired; the
        registry is never read or written without holding it.
        """
        with self._lock() as dir_fd:
            state = self.load(_locked=True, _dir_fd=dir_fd)
            yield state
            self.save(state, _dir_fd=dir_fd)

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
        state_fd = self._open_state_dir(create=True)
        locks_fd: int | None = None
        # Fixed striped locks avoid leaking one permanent lock file for every
        # directory ever created.  A collision only serializes unrelated
        # deletions briefly; it cannot weaken safety.
        digest = hashlib.sha256(lock_key.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:2], "big") % 256
        lock_name = f"delete-{slot:03d}.lock"
        try:
            locks_fd = self._open_private_child_dir(state_fd, "locks")
            fd = _open_owner_file_at(locks_fd, lock_name, os.O_CREAT | os.O_RDWR)
        except BaseException:
            if locks_fd is not None:
                os.close(locks_fd)
            os.close(state_fd)
            raise
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
            if locks_fd is not None:
                os.close(locks_fd)
            os.close(state_fd)

    @contextmanager
    def _lock(self) -> Iterator[int]:
        """Acquire an exclusive OS-level lock on the registry's lock file.

        The lock file itself is permanent and never deleted: removing a file
        other processes may be flock-ing reintroduces the classic unlock race.
        The lock is tied to the file descriptor, so it disappears with the
        process — a crash cannot leave the registry permanently locked.
        """
        state_fd = self._open_state_dir(create=True)
        try:
            fd = _open_owner_file_at(state_fd, self._lock_name, os.O_CREAT | os.O_RDWR)
        except BaseException:
            os.close(state_fd)
            raise
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
                yield state_fd
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
            os.close(state_fd)

    def _open_private_child_dir(self, parent_fd: int, name: str) -> int:
        """Open/create an owner-only direct child directory under state fd."""
        if name in {"", ".", ".."} or Path(name).parts != (name,):
            raise ValueError(f"unsafe state directory child: {name!r}")
        try:
            os.mkdir(name, mode=_OWNER_ONLY_DIR_MODE, dir_fd=parent_fd)
            _fsync_dir_fd(parent_fd)
        except FileExistsError:
            pass
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(f"{self.path.parent / name} is not a directory")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise OSError(f"{self.path.parent / name} changed before it could be used")
            if hasattr(os, "getuid") and opened.st_uid != os.getuid():
                raise PermissionError(f"{self.path.parent / name} is not owned by current user")
            if hasattr(os, "fchmod"):
                os.fchmod(fd, _OWNER_ONLY_DIR_MODE)
            opened = os.fstat(fd)
            if os.name == "posix" and stat.S_IMODE(opened.st_mode) & 0o077:
                raise PermissionError(f"{self.path.parent / name} is not private")
            return fd
        except BaseException:
            os.close(fd)
            raise
