"""Install a recurring ``ephemdir sweep`` as a per-user OS service.

Supported platforms use their native scheduler:

* macOS: a LaunchAgent (``launchctl``)
* Linux: a systemd user service + timer (``systemctl --user``)

Windows helpers are retained only so an older scheduled task can be removed.
New installation is refused until a handle-bound Windows deletion backend is
available.

The rendering of unit files is kept separate from the side effects so it can be
tested without touching the real system. Rendering uses proper escaping for
each format (``plistlib`` for launchd, systemd/C-style argument quoting,
``list2cmdline`` for Windows), so paths with spaces or special characters can
never corrupt a service definition. Scheduler commands are checked: a failed
installation raises :class:`ServiceError` instead of pretending it worked.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import logging
import os
import plistlib
import shlex
import site
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ._platform import _canonical_private_dir_path, user_config_dir, user_data_dir
from ._security import open_private_directory
from ._trusted_exec import resolve_executable_in_dirs, trusted_system_dirs

__all__ = [
    "ServiceError",
    "RuntimePolicy",
    "install_service",
    "uninstall_service",
    "service_status",
    "sweep_command",
]

# Scheduler/runtime subprocesses use fixed argv, trusted resolution and no shell.

logger = logging.getLogger("ephemdir")

# Identifiers reused across platforms.
LAUNCHD_LABEL = "com.vindfjur.ephemdir.sweep"
SYSTEMD_UNIT = "ephemdir-sweep"
WINDOWS_TASK = "ephemdir-sweep"


class ServiceError(RuntimeError):
    """Raised when the platform scheduler rejects an install/uninstall step."""


# Upper bound for any scheduler command so a hung launchctl/systemctl/schtasks
# can never stall an install or uninstall indefinitely.
_SCHEDULER_TIMEOUT_SECONDS = 30
_SERVICE_FILE_MODE = 0o600

# Installed-files manifest: the exact SHA-256 of every file ephemdir writes
# for the user (plists, systemd units, shell completions), kept owner-only in
# the config directory so uninstall can verify before it unlinks.
_MANIFEST_NAME = "installed-files.json"
_MANIFEST_LOCK_NAME = "installed-files.lock"
_MANIFEST_MAX_BYTES = 64 * 1024
_MANIFEST_LOCK_TIMEOUT_SECONDS = 10.0
_MANIFEST_LOCK_POLL_SECONDS = 0.05
_INSTALLED_FILE_MAX_BYTES = 1024 * 1024

# Environment override for the runtime-trust policy applied to install-service.
_RUNTIME_POLICY_ENV = "EPHEMDIR_SERVICE_RUNTIME_POLICY"
_REQUIRE_EXISTING_STATE_ENV = "EPHEMDIR_REQUIRE_EXISTING_STATE_DIR"
_REQUIRE_EXISTING_REGISTRY_ENV = "EPHEMDIR_REQUIRE_EXISTING_REGISTRY"


class RuntimePolicy(str, Enum):
    """How strictly ``install-service`` validates the interpreter/package runtime.

    Both policies hard-fail on the real takeover vectors in ephemdir's
    single-user POSIX threat model: a world-writable component, a component
    owned by another user, a symlinked package subdirectory, and any
    group/world-writable *executable* file or interpreter-startup hook. They
    differ on one point only.

    ``strict`` additionally rejects any group-writable *directory* ancestor.
    This is correct on a genuinely shared multi-user host, but on a normal
    single-user macOS box it rejects every default Homebrew interpreter, whose
    ``/opt/homebrew/Cellar`` ancestor is group-writable by ``admin`` (i.e. by
    the owner). That refusal silently prevents the scheduled sweep from ever
    being installed, so reboot/expiry cleanup never runs automatically.

    ``balanced`` allows a group-writable directory ancestor only as a narrow
    macOS carve-out — when :func:`_is_trusted_homebrew_group_writable_dir`
    passes: macOS, a directory owned by root or you, not world-writable, whose
    owning group is a local administrator group (``admin``) and whose resolved
    path is under ``/opt/homebrew`` or ``/usr/local``. It emits a warning. A
    group-writable directory owned by an ordinary shared group (which could
    contain another unprivileged user) is rejected, and every other vector
    stays a hard failure.
    """

    STRICT = "strict"
    BALANCED = "balanced"


@dataclass
class _RuntimeCheck:
    """Carries the active policy and de-duplicated warnings through validation."""

    policy: RuntimePolicy
    warnings: dict[str, None] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        self.warnings.setdefault(message, None)


def _resolve_runtime_policy(explicit: str | RuntimePolicy | None) -> RuntimePolicy:
    """Resolve the runtime policy from explicit arg, env, then platform default.

    Precedence: an explicit ``--runtime-policy`` value wins, then
    ``EPHEMDIR_SERVICE_RUNTIME_POLICY``, then the platform default (macOS is
    ``balanced`` so a stock Homebrew install works; every other platform is
    ``strict``).
    """
    if explicit is not None:
        return _coerce_runtime_policy(explicit, source="--runtime-policy")
    env_value = os.environ.get(_RUNTIME_POLICY_ENV)
    if env_value:
        return _coerce_runtime_policy(env_value, source=_RUNTIME_POLICY_ENV)
    if sys.platform == "darwin":
        return RuntimePolicy.BALANCED
    return RuntimePolicy.STRICT


def _coerce_runtime_policy(value: str | RuntimePolicy, *, source: str) -> RuntimePolicy:
    if isinstance(value, RuntimePolicy):
        return value
    try:
        return RuntimePolicy(value.strip().lower())
    except ValueError:
        raise ServiceError(
            f"invalid {source} value {value!r}: expected 'strict' or 'balanced'"
        ) from None


def _trusted_scheduler_dirs() -> tuple[Path, ...]:
    """Return fixed or kernel-derived directories trusted for schedulers."""
    return trusted_system_dirs()


def _resolve_scheduler(name: str) -> str:
    """Resolve a scheduler binary from trusted directories only."""
    resolved = resolve_executable_in_dirs(name, _trusted_scheduler_dirs())
    if resolved is not None:
        return resolved
    raise ServiceError(f"could not find trusted scheduler executable {name!r}")


def _scheduler_env() -> dict[str, str]:
    """Build a small environment for scheduler commands."""
    allowed = {
        "APPDATA",
        "DBUS_SESSION_BUS_ADDRESS",
        "EPHEMDIR_CONFIG_DIR",
        "EPHEMDIR_DATA_DIR",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    directories = _trusted_scheduler_dirs()
    env["PATH"] = os.pathsep.join(str(path) for path in directories)
    if sys.platform == "win32" and directories:
        windows_root = str(directories[0].parent)
        env["SystemRoot"] = windows_root
        env["WINDIR"] = windows_root
    return env


def _scheduler_cwd() -> str:
    """Return a stable cwd for scheduler subprocesses."""
    try:
        return str(Path.home())
    except RuntimeError:
        return os.sep


def _effective_service_environment() -> dict[str, str]:
    """Pin the data/config directories a scheduled sweep must use later."""
    data_dir = Path(os.path.abspath(user_data_dir(create=False)))
    config_dir = Path(os.path.abspath(user_config_dir(create=False)))
    for directory, label in ((data_dir, "data"), (config_dir, "config")):
        volatile_reason = _volatile_service_state_reason(directory)
        if volatile_reason is not None:
            raise ServiceError(
                f"refusing to install service: {label} directory {directory} "
                f"is not reboot-persistent ({volatile_reason})"
            )
        managed_ancestor = _managed_directory_ancestor(directory)
        if managed_ancestor is not None:
            raise ServiceError(
                f"refusing to install service: {label} directory {directory} is inside "
                f"tracked/marked ephemeral directory {managed_ancestor}"
            )
        try:
            fd = open_private_directory(directory, create=True)
        except OSError as error:
            raise ServiceError(
                f"refusing to install service: unsafe {label} directory {directory}: {error}"
            ) from error
        else:
            os.close(fd)

    # Establish and validate the exact registry file before the scheduler is
    # installed. The scheduled sweep then requires both the state root and this
    # file to remain present; losing only registry.json must not look like a
    # legitimate empty registry and silently orphan managed directories.
    try:
        from ._registry import Registry

        with Registry(path=data_dir / "registry.json").transaction():
            pass
    except Exception as error:
        raise ServiceError(
            f"refusing to install service: cannot establish registry in {data_dir}: {error}"
        ) from error
    return {
        "EPHEMDIR_DATA_DIR": str(data_dir),
        "EPHEMDIR_CONFIG_DIR": str(config_dir),
        # A scheduler must never recreate a vanished state root as an empty
        # registry after reboot. Interactive commands retain their historical
        # create-on-demand behaviour; the installed service fails closed.
        _REQUIRE_EXISTING_STATE_ENV: "1",
        _REQUIRE_EXISTING_REGISTRY_ENV: "1",
    }


def _is_within(path: Path, root: Path) -> bool:
    """Return whether ``path`` is ``root`` or one of its descendants."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _volatile_service_state_reason(directory: Path) -> str | None:
    """Explain why a scheduler state path may disappear across reboot.

    In particular, macOS' per-user ``TMPDIR`` normally lives under
    ``/private/var/folders/.../T`` and is routinely cleared. Pinning a registry
    there makes the LaunchAgent recreate an empty state directory after reboot,
    orphaning every still-existing managed directory.
    """
    absolute = _canonical_private_dir_path(Path(os.path.abspath(directory)))
    roots = {
        _canonical_private_dir_path(Path(os.path.abspath(tempfile.gettempdir()))),
        Path("/tmp"),  # nosec B108
        Path("/var/tmp"),  # nosec B108
        Path("/private/tmp"),
        Path("/private/var/tmp"),
    }
    if sys.platform == "darwin":
        roots.update({Path("/var/folders"), Path("/private/var/folders")})
    elif sys.platform.startswith("linux"):
        roots.update(
            {
                Path("/run"),
                Path("/var/run"),
                Path("/dev/shm"),  # nosec B108
            }
        )
    for root in roots:
        canonical_root = _canonical_private_dir_path(Path(os.path.abspath(root)))
        if _is_within(absolute, canonical_root):
            return f"path is under volatile root {canonical_root}"
    return None


def _managed_directory_ancestor(directory: Path) -> Path | None:
    """Return the nearest ancestor carrying an ephemdir ownership marker."""
    absolute = _canonical_private_dir_path(Path(os.path.abspath(directory)))
    for candidate in (absolute, *absolute.parents):
        try:
            if os.path.lexists(candidate / ".ephemdir"):
                return candidate
        except OSError:
            # The later private-directory trust walk will report inaccessible
            # components. Do not guess based on an inconclusive marker probe.
            return None
    return None


def _run_scheduler(name: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a trusted scheduler executable by basename."""
    return _run([_resolve_scheduler(name), *args])


def _check_service_dir_component(current: Path, info: os.stat_result, *, final: bool) -> None:
    """Reject one directory component another local user could swap or edit."""
    if not stat.S_ISDIR(info.st_mode):
        raise ServiceError(
            f"refusing to install service: {current} is not a real directory"
        )
    mode = stat.S_IMODE(info.st_mode)
    writable_by_others = bool(mode & 0o022)
    if final:
        # Anyone may create entries in a sticky world-writable directory, so
        # the directory that holds the unit/plist itself allows no exceptions.
        if writable_by_others:
            raise ServiceError(
                f"refusing to install service: {current} is writable by other users"
            )
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise ServiceError(
                f"refusing to install service: {current} is not owned by the current user"
            )
        return
    if writable_by_others and not (mode & stat.S_ISVTX):
        raise ServiceError(
            f"refusing to install service: path component {current} is "
            "writable by other users without sticky bit"
        )
    if hasattr(os, "geteuid") and info.st_uid not in (0, os.geteuid()):
        raise ServiceError(
            f"refusing to install service: path component {current} is owned "
            "by another user"
        )


def _validate_service_dir_chain(directory: Path) -> None:
    """Verify every component leading to the unit/plist directory is trusted.

    The scheduler re-reads these files long after installation, so each
    ancestor must resist replacement by other local users — otherwise the unit
    or plist could be swapped for one that runs arbitrary code as this user.
    Symlink components are allowed (e.g. ``/home`` on ostree systems) because
    the fully resolved chain is validated as well and a symlink can only be
    replaced through a parent directory that this walk already checks.
    """
    absolute = Path(os.path.abspath(directory))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise ServiceError(f"cannot resolve service directory {absolute}: {error}") from error
    chains = [absolute] if resolved == absolute else [absolute, resolved]
    for chain in chains:
        current = Path(chain.parts[0])
        components = chain.parts[1:]
        for index, part in enumerate(components):
            current = current / part
            try:
                info = os.lstat(current)
            except OSError as error:
                raise ServiceError(
                    f"cannot verify service directory component {current}: {error}"
                ) from error
            if stat.S_ISLNK(info.st_mode):
                continue  # The resolved chain re-checks what it points to.
            _check_service_dir_component(
                current, info, final=index == len(components) - 1
            )


def _open_verified_service_dir(directory: Path) -> int:
    """Open the unit/plist directory after proving the whole chain is trusted.

    Returns an ``O_DIRECTORY | O_NOFOLLOW`` descriptor whose identity and
    permissions are re-verified after opening, so every later write happens
    relative to the directory that was actually validated.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _validate_service_dir_chain(directory)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(directory, flags)
    except OSError as error:
        raise ServiceError(f"cannot open service directory {directory}: {error}") from error
    try:
        fd_stat = os.fstat(fd)
        _check_service_dir_component(directory, fd_stat, final=True)
        live_stat = os.lstat(directory)
        if (fd_stat.st_dev, fd_stat.st_ino) != (live_stat.st_dev, live_stat.st_ino):
            raise ServiceError(
                f"service directory {directory} changed while it was being verified"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_service_file(path: Path, content: str) -> None:
    """Atomically write a user service file relative to a verified dirfd.

    The temp file is created with ``O_EXCL | O_NOFOLLOW`` and both the write
    and the atomic replace use the descriptor of the already-validated parent
    directory, so neither a swapped ancestor nor a symlink planted at the
    target name can redirect the write.
    """
    dir_fd = _open_verified_service_dir(path.parent)
    tmp_name = f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    tmp_pending = True
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(tmp_name, flags, _SERVICE_FILE_MODE, dir_fd=dir_fd)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, _SERVICE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp_pending = False
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        if tmp_pending:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass
        os.close(dir_fd)


# --- Installed-files manifest and verified removal --------------------------
# ephemdir never unlinks an installed file by pathname alone (EPH-AUD-006).
# Every install records the exact content digest in an owner-only manifest;
# uninstall re-opens the target no-follow, proves it is still a regular,
# owner-owned, group/other-unwritable file whose bytes match that digest,
# re-checks the directory-entry identity against the open descriptor, and
# only then unlinks fd-relative. Anything modified, foreign or unrecorded is
# refused with the path so the user can resolve it manually.

_NOFOLLOW_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _manifest_path() -> Path:
    return Path(os.path.abspath(user_config_dir(create=False))) / _MANIFEST_NAME


def _fd_read_bounded(fd: int, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes from ``fd``; ``None`` when it holds more."""
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    return None if len(content) > limit else content


def _load_manifest() -> dict[str, str]:
    """Read the installed-files manifest; anything unusable reads as empty.

    An empty result is fail-closed downstream: files without a manifest
    record are refused by uninstall rather than deleted.
    """
    path = _manifest_path()
    try:
        fd = os.open(path, _NOFOLLOW_READ_FLAGS)
    except OSError:
        return {}
    try:
        try:
            _validate_service_dir_chain(path.parent)
        except ServiceError:
            return {}
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return {}
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            return {}
        # The manifest is the trust anchor for verified removal.  Treat any
        # group/other permission as unusable rather than trusting a digest that
        # another local user could read or replace through a misconfigured file.
        if stat.S_IMODE(info.st_mode) & 0o077:
            return {}
        raw = _fd_read_bounded(fd, _MANIFEST_MAX_BYTES)
        live = os.stat(path, follow_symlinks=False)
        if (live.st_dev, live.st_ino) != (info.st_dev, info.st_ino):
            return {}
    except OSError:
        return {}
    finally:
        os.close(fd)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    manifest: dict[str, str] = {}
    for key, value in data.items():
        if (
            isinstance(key, str)
            and isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        ):
            manifest[key] = value
    return manifest


def _save_manifest(manifest: dict[str, str]) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if len(payload.encode("utf-8")) > _MANIFEST_MAX_BYTES:
        raise ServiceError("installed-files manifest exceeds its 64 KiB safety bound")
    _write_service_file(_manifest_path(), payload)


@contextmanager
def _manifest_lock() -> Iterator[None]:
    """Serialize manifest read-modify-write operations across processes.

    The permanent lock file is opened no-follow inside the same verified,
    owner-controlled config directory as the manifest.  It is never deleted:
    unlinking a lock file while another process may hold it would recreate the
    classic split-lock race.
    """
    manifest = _manifest_path()
    dir_fd = _open_verified_service_dir(manifest.parent)
    flags = (
        os.O_CREAT
        | os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            fd = os.open(_MANIFEST_LOCK_NAME, flags, _SERVICE_FILE_MODE, dir_fd=dir_fd)
        except OSError as error:
            raise ServiceError(f"cannot open installed-files lock safely: {error}") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ServiceError("installed-files lock is not a regular file")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise ServiceError("installed-files lock is owned by another user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ServiceError("installed-files lock is not owner-only")

            # Reuse the registry's cross-platform fd-lock primitives.  The
            # service layer remains POSIX-only for installation, while this
            # keeps legacy Windows uninstall imports harmless.
            from ._registry import _try_lock, _unlock

            deadline = time.monotonic() + _MANIFEST_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ServiceError(
                            "could not lock installed-files manifest within "
                            f"{_MANIFEST_LOCK_TIMEOUT_SECONDS:.0f}s"
                        ) from None
                    time.sleep(_MANIFEST_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _record_installed_file(path: Path, content: str) -> None:
    with _manifest_lock():
        manifest = _load_manifest()
        manifest[str(path)] = _content_digest(content)
        _save_manifest(manifest)


def _forget_installed_file(path: Path) -> None:
    with _manifest_lock():
        manifest = _load_manifest()
        if manifest.pop(str(path), None) is not None:
            _save_manifest(manifest)


def _remove_verified_installed_file(path: Path, expected_digest: str) -> None:
    """Unlink ``path`` only after proving it is exactly the file we installed."""
    dir_fd = _open_verified_service_dir(path.parent)
    try:
        try:
            fd = os.open(path.name, _NOFOLLOW_READ_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError as error:
            raise ServiceError(f"{path} disappeared before verified removal") from error
        except OSError as error:
            raise ServiceError(
                f"refusing to remove {path}: cannot open it safely ({error}); "
                "inspect and remove it manually"
            ) from error
        try:
            info = os.fstat(fd)
            reason: str | None = None
            if not stat.S_ISREG(info.st_mode):
                reason = "it is not a regular file"
            elif hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                reason = "it is owned by another user"
            elif stat.S_IMODE(info.st_mode) & 0o022:
                reason = "it is writable by group or others"
            else:
                content = _fd_read_bounded(fd, _INSTALLED_FILE_MAX_BYTES)
                if content is None:
                    reason = "it is unexpectedly large"
                elif hashlib.sha256(content).hexdigest() != expected_digest:
                    reason = "its content no longer matches what ephemdir installed"
            if reason is not None:
                raise ServiceError(
                    f"refusing to remove {path}: {reason}; inspect and remove it "
                    "manually, or reinstall so removal can be verified again"
                )
            live = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
            if (live.st_dev, live.st_ino) != (info.st_dev, info.st_ino):
                raise ServiceError(
                    f"refusing to remove {path}: it was replaced during verification"
                )
            os.unlink(path.name, dir_fd=dir_fd)
        finally:
            os.close(fd)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        os.close(dir_fd)


def _remove_recorded_installed_file(path: Path) -> None:
    """Verified removal gated on one locked manifest transaction."""
    with _manifest_lock():
        manifest = _load_manifest()
        digest = manifest.get(str(path))
        if digest is None:
            raise ServiceError(
                f"refusing to remove {path}: it is not recorded in ephemdir's "
                "installed-files manifest (installed by hand or by an ephemdir "
                "older than 0.8.0); inspect and remove it manually, or reinstall "
                "first so removal can be verified"
            )
        _remove_verified_installed_file(path, digest)
        manifest.pop(str(path), None)
        _save_manifest(manifest)


def _installed_file_state(path: Path, expected_content: str | None) -> str:
    """Classify an installed file for read-only status reporting.

    Never creates or modifies anything. States: ``not-installed``,
    ``irregular`` (symlink/special), ``unreadable``, ``unsafe`` (untrusted
    parent or writable file), ``foreign`` (another user's file), ``unrecorded``
    (no manifest entry), ``modified`` (differs
    from the recorded digest), ``outdated`` (matches the record but not the
    current ``expected_content``) and ``installed``.
    """
    try:
        fd = os.open(path, _NOFOLLOW_READ_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return "not-installed"
    except OSError as error:
        return "irregular" if error.errno in (errno.ELOOP, errno.EMLINK) else "unreadable"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "irregular"
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            return "foreign"
        if stat.S_IMODE(info.st_mode) & 0o022:
            return "unsafe"
        try:
            _validate_service_dir_chain(path.parent)
        except ServiceError:
            return "unsafe"
        content = _fd_read_bounded(fd, _INSTALLED_FILE_MAX_BYTES)
        live = os.stat(path, follow_symlinks=False)
        if (live.st_dev, live.st_ino) != (info.st_dev, info.st_ino):
            return "unsafe"
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)
    if content is None:
        return "unreadable"
    recorded = _load_manifest().get(str(path))
    if recorded is None:
        return "unrecorded"
    if hashlib.sha256(content).hexdigest() != recorded:
        return "modified"
    if expected_content is not None and content != expected_content.encode("utf-8"):
        return "outdated"
    return "installed"


# The balanced policy tolerates a group-writable *directory* ancestor only as a
# narrow macOS carve-out: the resolved path lives under one of these package-
# manager prefixes AND its owning group is a local administrator group (admin).
# /opt/homebrew/Cellar is mode 0775, group admin — writable only by machine
# administrators, who on a personal Mac are the owner. A group-writable
# directory whose owning group might contain a *different* unprivileged user (a
# shared "project" group) is deliberately NOT covered: that user is in scope and
# could replace a directory on the import path, gaining code execution as the
# installing user when the scheduled sweep runs.
_GROUP_WRITABLE_PREFIX_ALLOWLIST: tuple[str, ...] = ("/opt/homebrew", "/usr/local")
_ADMIN_GROUP_NAMES: tuple[str, ...] = ("admin",)
_ADMIN_GROUP_FALLBACK_GIDS: frozenset[int] = frozenset({80})  # macOS `admin`


def _trusted_admin_gids() -> frozenset[int]:
    """Return gids of local administrator groups the carve-out may trust."""
    gids = set(_ADMIN_GROUP_FALLBACK_GIDS)
    try:
        import grp
    except ImportError:  # pragma: no cover - non-POSIX
        return frozenset(gids)
    for name in _ADMIN_GROUP_NAMES:
        try:
            gids.add(grp.getgrnam(name).gr_gid)
        except KeyError:  # pragma: no cover - admin group absent
            continue
    return frozenset(gids)


def _within_allowlisted_prefix(path: Path) -> bool:
    """Whether ``path`` resolves to within an allowlisted package-manager prefix."""
    try:
        real = path.resolve(strict=True)
    except OSError:  # pragma: no cover - component vanished mid-check
        return False
    for prefix in _GROUP_WRITABLE_PREFIX_ALLOWLIST:
        prefix_path = Path(prefix)
        if real == prefix_path or prefix_path in real.parents:
            return True
    return False


def _is_trusted_homebrew_group_writable_dir(current: Path, info: os.stat_result) -> bool:
    """Whether ``balanced`` may tolerate this group-writable directory.

    Only the macOS Homebrew / usr-local carve-out qualifies: macOS, a real
    directory, owning group is a local admin group, and the resolved path lives
    under an allowlisted prefix. Any other group-writable directory — including
    one whose owning group could contain another unprivileged local user — is
    rejected, so the relaxation never exceeds the documented threat model.
    """
    if sys.platform != "darwin":
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if info.st_gid not in _trusted_admin_gids():
        return False
    return _within_allowlisted_prefix(current)


def _balanced_policy_hint(check: _RuntimeCheck, current: Path, info: os.stat_result) -> str:
    """Mention balanced under strict only when the dir actually qualifies for it."""
    if check.policy is RuntimePolicy.STRICT and _is_trusted_homebrew_group_writable_dir(
        current, info
    ):
        return (
            " This is a Homebrew/usr-local prefix owned by a local admin group; "
            "--runtime-policy balanced (the macOS default) allows it."
        )
    return ""


def _safe_uv_runtime_hint() -> str:
    return (
        " Scheduled services run this interpreter later, so a "
        "group/world-writable component could be replaced by another local "
        "user. Install a safe uv-managed environment under your home directory "
        "instead:\n"
        "  uv python install 3.12\n"
        "  uv venv ~/.venvs/ephemdir-safe --python 3.12\n"
        "  uv pip install --python ~/.venvs/ephemdir-safe/bin/python ephemdir\n"
        "  ~/.venvs/ephemdir-safe/bin/python -I -m ephemdir install-service"
    )


def _check_runtime_component(
    current: Path, info: os.stat_result, description: str, check: _RuntimeCheck
) -> None:
    """Reject one runtime path component another local user could replace.

    Ownership is the primary gate: the owner of a component can always rewrite
    it regardless of mode, so every component must belong to root or the
    installing user. World-writable is always fatal. A group-writable
    *directory* ancestor is fatal under ``strict`` and is fatal under
    ``balanced`` too, except for the narrow macOS Homebrew/usr-local admin
    carve-out — when :func:`_is_trusted_homebrew_group_writable_dir` passes —
    where it is allowed with a warning. A group-writable *file* — an interpreter
    or importable module — stays fatal under both policies, because that is
    executable code.
    """
    if stat.S_ISLNK(info.st_mode):
        return  # The resolved chain re-checks what the link points to.
    if hasattr(os, "geteuid") and info.st_uid not in (0, os.geteuid()):
        raise ServiceError(
            f"refusing to install service: {description} {current} is "
            "owned by another user"
        )
    if info.st_mode & stat.S_IWOTH:
        raise ServiceError(
            f"refusing to install service: {description} {current} is "
            "world-writable (writable by any local user); shared directories "
            "like /tmp cannot host a scheduled service runtime, sticky bit or not."
            f"{_safe_uv_runtime_hint()}"
        )
    if info.st_mode & stat.S_IWGRP:
        if check.policy is RuntimePolicy.BALANCED and _is_trusted_homebrew_group_writable_dir(
            current, info
        ):
            check.warn(
                f"service runtime uses group-writable Homebrew/usr-local path "
                f"component {current} (owning group is a local admin group); "
                "allowed by --runtime-policy balanced. Use --runtime-policy "
                "strict to fail closed."
            )
            return
        raise ServiceError(
            f"refusing to install service: {description} {current} is "
            "group-writable (writable by other users in its group)."
            f"{_balanced_policy_hint(check, current, info)}"
            f"{_safe_uv_runtime_hint()}"
        )


def _validate_runtime_chain(absolute: Path, label: str, check: _RuntimeCheck) -> os.stat_result:
    """Validate every ancestor of ``absolute`` (lexical and resolved).

    Each component must resist replacement by other local users. Returns the
    final ``os.stat`` result (symlinks followed) so the caller can assert the
    expected file type. Raises :class:`ServiceError` on any untrusted link.
    """
    parts = absolute.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise ServiceError(f"cannot verify {label} path {current}: {error}") from error
        _check_runtime_component(current, info, f"{label} path component", check)

    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise ServiceError(f"cannot resolve {label} path {absolute}: {error}") from error
    if resolved != absolute:
        resolved_parts = resolved.parts
        current = Path(resolved_parts[0])
        for part in resolved_parts[1:]:
            current /= part
            try:
                info = os.lstat(current)
            except OSError as error:
                raise ServiceError(
                    f"cannot verify resolved {label} path {current}: {error}"
                ) from error
            _check_runtime_component(current, info, f"resolved {label} path component", check)

    try:
        return os.stat(absolute)
    except OSError as error:
        raise ServiceError(f"cannot stat {label} path {absolute}: {error}") from error


def _validate_runtime_path(path: Path, label: str, check: _RuntimeCheck) -> None:
    """Reject service runtime paths another local user could swap or edit.

    A scheduled job persists beyond the current shell, so every existing path
    component leading to the interpreter or imported package must resist
    replacement: not writable by group members or arbitrary local users, and
    owned by root or the installing user. User-owned virtual environments
    remain supported.
    """
    absolute = Path(os.path.abspath(path))
    final_info = _validate_runtime_chain(absolute, label, check)
    if not stat.S_ISREG(final_info.st_mode):
        raise ServiceError(f"refusing to install service: {label} is not a regular file")
    if hasattr(os, "geteuid") and final_info.st_uid not in (0, os.geteuid()):
        raise ServiceError(f"refusing to install service: {label} is owned by another user")


def _validate_runtime_dir(path: Path, label: str, check: _RuntimeCheck) -> None:
    """Reject a runtime *directory* another local user could swap or populate.

    Unlike :func:`_validate_runtime_path`, the final component must be a real
    directory. This matters for an otherwise-empty package subdirectory (e.g.
    a freshly created, attacker-owned ``__pycache__``): it holds no module file
    to validate indirectly today, but its owner can drop an unchecked ``.pyc``
    into it after the service is installed, which the interpreter would then
    load. Validating the directory itself closes that gap.
    """
    absolute = Path(os.path.abspath(path))
    final_info = _validate_runtime_chain(absolute, label, check)
    if not stat.S_ISDIR(final_info.st_mode):
        raise ServiceError(f"refusing to install service: {label} is not a directory")
    # Re-apply the per-component policy to the resolved directory itself (the
    # chain validated it through lstat; this covers the symlink-followed
    # target). Balanced relaxes a group-writable directory here only when it
    # passes the macOS Homebrew/usr-local admin carve-out
    # (_is_trusted_homebrew_group_writable_dir); otherwise it is rejected.
    _check_runtime_component(absolute, final_info, label, check)


# Files the interpreter can load and execute as the service user. A `.py`
# source, its cached bytecode or a native extension are all code the scheduled
# `python -I -m ephemdir sweep` may import, so each must resist local tampering.
_EXECUTABLE_MODULE_SUFFIXES = (".py", ".pyc", ".pyo", ".so", ".pyd", ".dylib")


def _validate_package_tree(package_dir: Path, check: _RuntimeCheck) -> None:
    """Verify every importable module under the package resists local tampering.

    ``python -I -m ephemdir sweep`` imports far more than one entry point:
    ``__main__.py``, ``cli.py``, ``core.py`` and every ``_*.py`` helper run as
    the service user when the timer fires. If any of those files -- or a
    directory on the way to them -- is writable by another local user, that
    user could swap the code the scheduler later executes. Validate the whole
    tree, not just a single file. Each file is checked through
    :func:`_validate_runtime_path`, which also re-verifies every ancestor
    directory, so a writable package directory is rejected too.
    """
    def _walk_error(error: OSError) -> None:
        # os.walk swallows traversal errors by default, so a subdirectory the
        # validator cannot even enter (e.g. a foreign `__pycache__` at mode
        # 0000) would be skipped silently and the install reported as safe.
        # Fail closed on any such error instead.
        raise ServiceError(
            f"refusing to install service: cannot inspect package directory: {error}"
        )

    validated = False
    for root, dirs, files in os.walk(package_dir, onerror=_walk_error):
        root_path = Path(root)
        # Validate the directory itself, not just the files inside it. An empty
        # (or unknown-files-only) subdirectory owned by another user -- e.g. a
        # foreign `__pycache__` -- has nothing to validate indirectly today,
        # but its owner can later drop an unchecked `.pyc` the interpreter would
        # load.
        _validate_runtime_dir(root_path, "ephemdir package directory", check)
        for name in list(dirs):
            sub = root_path / name
            # os.walk does not descend into a symlinked subdirectory, so a
            # swapped `__pycache__` (or any symlinked package subdir) would
            # never be validated -- yet Python follows it at import time and
            # would load whatever `.pyc` it points at. Refuse such trees.
            if sub.is_symlink():
                raise ServiceError(
                    "refusing to install service: package directory contains a "
                    f"symlinked subdirectory {sub}, which could redirect imported "
                    "code to an untrusted location"
                )
            # Validate each subdirectory *now*, before os.walk tries to descend.
            # An inaccessible or foreign-owned subdir is caught here (or by the
            # onerror handler above) rather than slipping through unvalidated.
            _validate_runtime_dir(sub, "ephemdir package directory", check)
        for name in files:
            file_path = root_path / name
            if file_path.suffix in _EXECUTABLE_MODULE_SUFFIXES:
                _validate_runtime_path(file_path, "ephemdir module", check)
                validated = True
    if not validated:
        raise ServiceError(
            "refusing to install service: found no ephemdir module files to "
            f"verify under {package_dir}"
        )


def _site_directories() -> list[Path]:
    """Return the site-packages directories ``python -I`` still processes.

    Isolated mode drops the *user* site and ``PYTHON*`` env vars but not
    ``site.py`` itself, so the system/venv site-packages are still scanned for
    ``.pth`` files at interpreter startup.
    """
    directories: list[Path] = []
    getsitepackages = getattr(site, "getsitepackages", None)
    if getsitepackages is not None:
        try:
            directories.extend(Path(entry) for entry in getsitepackages())
        except (AttributeError, OSError):  # pragma: no cover - exotic site setups
            pass
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for directory in directories:
        key = str(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def _iter_startup_files() -> Iterator[tuple[Path, str]]:
    """Yield every file Python may execute *before* ``-m ephemdir`` is imported.

    ``python -I -m ephemdir sweep`` does not use ``-S``, so any ``.pth`` line
    starting with ``import`` runs at interpreter startup, as can a
    ``sitecustomize`` module and -- on Python 3.10 -- the imported ``tomli``
    dependency. Each is code the scheduler would execute as the service user,
    so each must resist replacement by other local users.
    """
    for directory in _site_directories():
        if not directory.is_dir():
            continue
        for pth in sorted(directory.glob("*.pth")):
            yield pth, "site .pth file"

    # sitecustomize runs even under -I when present anywhere on sys.path.
    try:
        spec = importlib.util.find_spec("sitecustomize")
    except (ImportError, AttributeError, ValueError):  # pragma: no cover - defensive
        spec = None
    if spec is not None and spec.origin and spec.origin not in ("built-in", "frozen"):
        yield Path(spec.origin), "sitecustomize module"

    # pyvenv.cfg configures the interpreter's base prefix and site behaviour.
    pyvenv_cfg = Path(sys.prefix) / "pyvenv.cfg"
    if pyvenv_cfg.is_file():
        yield pyvenv_cfg, "pyvenv.cfg"


def _validate_startup_environment(check: _RuntimeCheck) -> None:
    """Verify interpreter-startup hooks resist tampering by other local users."""
    for path, label in _iter_startup_files():
        _validate_runtime_path(path, label, check)

    # On Python 3.10 the sweep imports `tomli` for TOML config; the whole
    # package runs as the service user, so validate every module in it.
    if sys.version_info < (3, 11):
        try:
            tomli_spec = importlib.util.find_spec("tomli")
        except (ImportError, AttributeError, ValueError):  # pragma: no cover - defensive
            tomli_spec = None
        if tomli_spec is not None and tomli_spec.origin:
            _validate_package_tree(Path(tomli_spec.origin).resolve().parent, check)


def _validate_service_runtime(check: _RuntimeCheck) -> None:
    _validate_runtime_path(Path(sys.executable), "Python interpreter", check)
    _validate_package_tree(Path(__file__).resolve().parent, check)
    _validate_startup_environment(check)


def _reject_elevated_user_install() -> None:
    """Refuse per-user service installation from elevated/root contexts."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ServiceError("install-service must be run as the target user, not root")
    if sys.platform == "win32":
        try:
            import ctypes

            shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
            if shell32.IsUserAnAdmin():
                raise ServiceError(
                    "install-service must be run as the target user, not Administrator"
                )
        except AttributeError:
            pass


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a scheduler command with a timeout; timeouts become ServiceError."""
    try:
        return subprocess.run(  # nosec B603
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_SCHEDULER_TIMEOUT_SECONDS,
            env=_scheduler_env(),
            cwd=_scheduler_cwd(),
        )
    except subprocess.TimeoutExpired as error:
        raise ServiceError(
            f"{command[0]} timed out after {_SCHEDULER_TIMEOUT_SECONDS}s"
        ) from error


def _run_checked(command: list[str], action: str) -> None:
    """Run a scheduler command and raise :class:`ServiceError` on failure."""
    result = _run(command)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise ServiceError(f"{action} failed: {detail}")


def sweep_command() -> list[str]:
    """Return the argv that runs ``ephemdir sweep`` reliably from a service.

    Always uses the current interpreter (``python -I -m ephemdir``) rather
    than whatever ``ephemdir`` happens to be first on PATH, so a polluted PATH
    can never pin a different executable into the scheduler. Isolated mode
    (``-I``) additionally ignores ``PYTHONPATH``/``PYTHONHOME``, the user
    site-packages and the current directory, so a file like ``~/ephemdir.py``
    or a poisoned environment can never be imported instead of the installed
    package when the scheduler runs the sweep later.
    """
    return [sys.executable, "-I", "-m", "ephemdir", "sweep"]


def _verify_isolated_import() -> None:
    """Prove that the scheduled command imports exactly this package.

    ``-I`` excludes the user site-packages, so a ``pip install --user`` copy of
    ephemdir would silently fail (or worse, resolve differently) once the
    scheduler runs it. Run the exact interpreter in isolated mode with the same
    minimal environment the service will see and require that it resolves
    ``ephemdir`` to this very file before anything is installed.
    """
    package_root = os.path.dirname(os.path.abspath(__file__))
    probe_code = (
        "import ephemdir, os; "
        "print(os.path.dirname(os.path.abspath(ephemdir.__file__)))"
    )
    try:
        # Probe the current interpreter in isolated mode, with no shell involved.
        probe = subprocess.run(  # nosec B603
            [sys.executable, "-I", "-c", probe_code],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SCHEDULER_TIMEOUT_SECONDS,
            env=_scheduler_env(),
            cwd=os.sep,
        )
    except subprocess.TimeoutExpired as error:
        raise ServiceError(
            f"isolated import check timed out after {_SCHEDULER_TIMEOUT_SECONDS}s"
        ) from error
    if probe.returncode != 0:
        detail = probe.stderr.strip() or probe.stdout.strip() or "unknown import error"
        raise ServiceError(
            "refusing to install service: ephemdir is not importable in isolated "
            f"mode (python -I); install it into a virtualenv or the system "
            f"site-packages, not with `pip install --user` ({detail})"
        )
    resolved = probe.stdout.strip()
    if os.path.realpath(resolved) != os.path.realpath(package_root):
        raise ServiceError(
            "refusing to install service: isolated mode resolves ephemdir to "
            f"{resolved!r} instead of the running package at {package_root!r}"
        )


# --- macOS (launchd) -------------------------------------------------------

def render_launchd_plist(
    interval: int,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> str:
    """Render a LaunchAgent plist that runs ``command`` every ``interval`` s.

    Built with :mod:`plistlib` so arbitrary characters in the command path are
    escaped correctly. The job runs from ``/`` with a fixed trusted ``PATH``,
    so a directory the user can write to is never the working directory of the
    scheduled interpreter and ``launchctl setenv`` cannot inject a search path.
    """
    env = {
        "PATH": os.pathsep.join(str(path) for path in trusted_system_dirs()),
    }
    if environment:
        env.update(environment)
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "StartInterval": interval,
        "WorkingDirectory": "/",
        "EnvironmentVariables": env,
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _install_launchd(interval: int) -> str:
    path = _launchd_path()
    content = render_launchd_plist(
        interval,
        sweep_command(),
        environment=_effective_service_environment(),
    )
    _write_service_file(path, content)
    _record_installed_file(path, content)
    # Unload best-effort: it legitimately fails when the agent was not loaded.
    _run_scheduler("launchctl", ["unload", str(path)])
    _run_checked([_resolve_scheduler("launchctl"), "load", str(path)], "launchctl load")
    return f"installed LaunchAgent at {path} (sweeps every {interval}s)"


def _uninstall_launchd() -> str:
    path = _launchd_path()
    if not os.path.lexists(path):
        return "no LaunchAgent installed"
    unload = _run_scheduler("launchctl", ["unload", str(path)])
    if unload.returncode != 0:
        still_loaded = _run_scheduler("launchctl", ["list", LAUNCHD_LABEL])
        if still_loaded.returncode == 0:
            raise ServiceError(
                f"could not unload LaunchAgent {LAUNCHD_LABEL}; it is still loaded"
            )
        detail = unload.stderr.strip() or unload.stdout.strip() or (
            f"launchctl unload exited {unload.returncode}"
        )
        # A second non-zero command does not prove absence (it may be a
        # permissions/bootstrap-domain error), so fail visibly and leave the
        # plist in place instead of pretending success.
        raise ServiceError(f"could not prove LaunchAgent is unloaded: {detail}")
    # The agent is provably unloaded; the file itself is removed only after
    # verifying it is exactly what ephemdir installed (EPH-AUD-006). A
    # modified/foreign/unrecorded plist stays on disk with instructions.
    _remove_recorded_installed_file(path)
    return f"removed LaunchAgent {path}"


# --- Linux (systemd user units) -------------------------------------------

def _quote_systemd_arg(argument: str) -> str:
    """Quote one ExecStart argument for systemd's command-line parser.

    This is deliberately not :func:`shlex.quote`: unit files are not parsed by
    a shell, and ``%`` is expanded as a systemd specifier even inside quotes.
    """
    escaped = (
        argument.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    if escaped and all(char.isalnum() or char in "_./:@+-" for char in escaped):
        return escaped
    return f'"{escaped}"'


def render_systemd_units(
    interval: int,
    command: list[str],
    *,
    env_executable: str = "/usr/bin/env",
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Render the systemd service and timer with systemd-native escaping.

    systemd applies extra restrictions to the executable token itself.  Run
    the absolute interpreter through ``env --`` so paths containing spaces,
    percent signs or quotes remain ordinary arguments rather than unit
    syntax.  ``sweep_command()`` supplies an absolute interpreter path.
    ``env_executable`` must come from the trusted resolver when the unit is
    actually installed — the scheduler will execute it unattended, so it gets
    the same ownership/writability scrutiny as every other helper binary.
    """
    argv = [env_executable, "--", *command]
    exec_start = " ".join(_quote_systemd_arg(arg) for arg in argv)
    environment_line = ""
    if environment:
        assignments = " ".join(
            _quote_systemd_arg(f"{key}={value}") for key, value in sorted(environment.items())
        )
        environment_line = f"Environment={assignments}\n"
    # WorkingDirectory=/ keeps a user-writable directory (the default is the
    # home directory) from ever being the interpreter's cwd, and the Python
    # search-path environment is dropped outright. Both complement the `-I`
    # flag in `sweep_command()`: even a unit edited to remove one layer keeps
    # the other.
    service = (
        "[Unit]\n"
        "Description=ephemdir scheduled cleanup\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "WorkingDirectory=/\n"
        "UnsetEnvironment=PYTHONPATH PYTHONHOME PYTHONSTARTUP\n"
        f"{environment_line}"
        f"ExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run ephemdir cleanup periodically\n\n"
        "[Timer]\n"
        f"OnBootSec={interval}\n"
        f"OnUnitActiveSec={interval}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{SYSTEMD_UNIT}.service": service, f"{SYSTEMD_UNIT}.timer": timer}


def _systemd_dir() -> Path:
    # Status must remain strictly read-only.  The atomic writer creates this
    # directory during install; merely computing the path must not create the
    # sibling ephemdir config directory.
    return user_config_dir(create=False).parent / "systemd" / "user"


def _install_systemd(interval: int) -> str:
    # Resolve and validate every executable the timer will run *before* any
    # unit file is written: both `systemctl` and the `env` trampoline in
    # ExecStart go through the trusted resolver (owner, writability, no final
    # symlink). A foreign-owned `env` must abort the install while the system
    # is still untouched, not end up inside an enabled unit.
    systemctl = _resolve_scheduler("systemctl")
    env_executable = _resolve_scheduler("env")
    units_dir = _systemd_dir()
    units = render_systemd_units(
        interval,
        sweep_command(),
        env_executable=env_executable,
        environment=_effective_service_environment(),
    )
    for name, content in units.items():
        _write_service_file(units_dir / name, content)
        _record_installed_file(units_dir / name, content)
    _run_checked([systemctl, "--user", "daemon-reload"], "systemctl daemon-reload")
    _run_checked(
        [systemctl, "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"],
        "systemctl enable",
    )
    return f"installed systemd user timer in {units_dir} (sweeps every {interval}s)"


def _uninstall_systemd() -> str:
    units_dir = _systemd_dir()
    unit_files = [units_dir / f"{SYSTEMD_UNIT}.service", units_dir / f"{SYSTEMD_UNIT}.timer"]
    if not any(os.path.lexists(unit) for unit in unit_files):
        return "no systemd timer installed"
    # `disable` fails when the timer was never enabled; the authoritative
    # check is the unit's ActiveState afterwards. `show` reports "inactive"
    # for unknown units and exits 0, so a failing command (as opposed to a
    # stopped timer) is distinguishable — and the exit code is checked below,
    # because a stale or partial "inactive" printed by a failing `show` must
    # not be mistaken for proof that the timer is stopped.
    systemctl = _resolve_scheduler("systemctl")
    _run([systemctl, "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"])
    show = _run(
        [systemctl, "--user", "show", "-p", "ActiveState", "--value", f"{SYSTEMD_UNIT}.timer"]
    )
    state = show.stdout.strip()
    # Removing the unit files is only safe when the timer is provably stopped;
    # "activating"/"deactivating"/"reloading" or an unreadable state are
    # treated as errors, not as absence.
    if show.returncode != 0 or state not in ("inactive", "failed"):
        detail = show.stderr.strip() or state or f"exit code {show.returncode}"
        raise ServiceError(
            f"could not confirm that {SYSTEMD_UNIT}.timer is stopped (state: {detail})"
        )
    # The timer is provably stopped; each unit file is removed only after
    # verifying it is exactly what ephemdir installed (EPH-AUD-006).
    for unit in unit_files:
        if os.path.lexists(unit):
            _remove_recorded_installed_file(unit)
    _run_checked([systemctl, "--user", "daemon-reload"], "systemctl daemon-reload")
    return f"removed systemd user timer from {units_dir}"


# --- Windows (Task Scheduler) ---------------------------------------------

def render_windows_command(command: list[str]) -> str:
    """Quote the task command with Windows argv rules (handles spaces)."""
    quoted: str = subprocess.list2cmdline(command)
    return quoted


def _install_windows(interval: int) -> str:
    minutes = max(1, interval // 60)
    _run_checked(
        [
            _resolve_scheduler("schtasks"), "/Create", "/F",
            "/TN", WINDOWS_TASK,
            "/TR", render_windows_command(sweep_command()),
            "/SC", "MINUTE",
            "/MO", str(minutes),
        ],
        "schtasks /Create",
    )
    return f"installed scheduled task {WINDOWS_TASK!r} (sweeps every {minutes} min)"


def _uninstall_windows() -> str:
    # Distinguish "not installed" from a real failure (e.g. access denied):
    # try the delete first; when it fails, only report "not installed" if the
    # task is provably absent, otherwise surface the delete error.
    schtasks = _resolve_scheduler("schtasks")
    delete = _run([schtasks, "/Delete", "/TN", WINDOWS_TASK, "/F"])
    if delete.returncode == 0:
        return f"removed scheduled task {WINDOWS_TASK!r}"
    query = _run([schtasks, "/Query", "/TN", WINDOWS_TASK])
    if query.returncode == 0:
        detail = delete.stderr.strip() or delete.stdout.strip() or "unknown error"
        raise ServiceError(f"schtasks /Delete failed: {detail}")
    # schtasks has no locale-independent status that distinguishes "missing"
    # from access denied or a broken scheduler.  Two failures are therefore
    # ambiguous and must not be reported as success/absence.
    detail = delete.stderr.strip() or delete.stdout.strip() or (
        query.stderr.strip() or query.stdout.strip() or "unknown scheduler error"
    )
    raise ServiceError(f"could not determine whether scheduled task exists: {detail}")


# --- Public dispatch -------------------------------------------------------

def install_service(
    interval: int = 600,
    *,
    runtime_policy: str | RuntimePolicy | None = None,
) -> str:
    """Install the periodic sweep service for the current platform.

    ``runtime_policy`` selects how strictly the interpreter/package runtime is
    validated (see :class:`RuntimePolicy`); ``None`` resolves it from the
    ``EPHEMDIR_SERVICE_RUNTIME_POLICY`` env var, then the platform default
    (``balanced`` on macOS, ``strict`` elsewhere). Any relaxation taken under
    ``balanced`` is logged as a warning.

    Raises :class:`ServiceError` when the platform scheduler reports failure,
    so a broken installation is never reported as success.
    """
    if interval < 1:
        raise ValueError("interval must be >= 1 second")
    _reject_elevated_user_install()
    if sys.platform == "win32":
        raise ServiceError(
            "Windows is unsupported because Python does not expose the "
            "handle-bound recursive deletion primitives ephemdir requires"
        )
    check = _RuntimeCheck(policy=_resolve_runtime_policy(runtime_policy))
    _validate_service_runtime(check)
    _verify_isolated_import()
    for warning in check.warnings:
        logger.warning("%s", warning)
    if sys.platform == "darwin":
        return _install_launchd(interval)
    return _install_systemd(interval)


def uninstall_service() -> str:
    """Remove the periodic sweep service for the current platform."""
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if sys.platform == "win32":
        return _uninstall_windows()
    return _uninstall_systemd()


def _pinned_service_definition(path: Path) -> dict[str, object]:
    """Read-only extraction of argv/cwd/environment from a verified service file."""
    try:
        fd = os.open(path, _NOFOLLOW_READ_FLAGS)
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return {}
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            return {}
        if stat.S_IMODE(info.st_mode) & 0o022:
            return {}
        try:
            _validate_service_dir_chain(path.parent)
        except ServiceError:
            return {}
        raw = _fd_read_bounded(fd, _INSTALLED_FILE_MAX_BYTES)
        live = os.stat(path, follow_symlinks=False)
        if (live.st_dev, live.st_ino) != (info.st_dev, info.st_ino):
            return {}
    except OSError:
        return {}
    finally:
        os.close(fd)
    if raw is None:
        return {}
    if path.suffix == ".plist":
        try:
            payload = plistlib.loads(raw)
        except Exception:  # noqa: BLE001 - any unparsable plist is untrusted
            return {}
        if not isinstance(payload, dict):
            return {}
        argv = payload.get("ProgramArguments")
        env = payload.get("EnvironmentVariables")
        return {
            "command": (
                list(argv)
                if isinstance(argv, list)
                and all(isinstance(value, str) for value in argv)
                else []
            ),
            "working_directory": payload.get("WorkingDirectory")
            if isinstance(payload.get("WorkingDirectory"), str)
            else None,
            "environment": {
                key: str(value)
                for key, value in env.items()
                if isinstance(env, dict)
                and isinstance(key, str)
                and key.startswith("EPHEMDIR_")
            }
            if isinstance(env, dict)
            else {},
        }
    command: list[str] = []
    working_directory: str | None = None
    pinned: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("WorkingDirectory="):
            working_directory = line.partition("=")[2]
        elif line.startswith("ExecStart="):
            try:
                command = shlex.split(line[len("ExecStart="):])
            except ValueError:
                command = []
            command = [token.replace("%%", "%") for token in command]
        elif line.startswith("Environment="):
            try:
                tokens = shlex.split(line[len("Environment="):])
            except ValueError:
                continue
            for token in tokens:
                name, separator, value = token.partition("=")
                if separator and name.startswith("EPHEMDIR_"):
                    pinned[name] = value.replace("%%", "%")
    return {
        "command": command,
        "working_directory": working_directory,
        "environment": pinned,
    }


def _pinned_service_environment(path: Path) -> dict[str, str]:
    """Compatibility wrapper returning only the pinned environment."""
    value = _pinned_service_definition(path).get("environment", {})
    return value if isinstance(value, dict) else {}


def _service_command_valid(manager: str, command: object) -> bool | None:
    if not isinstance(command, list) or not all(
        isinstance(item, str) for item in command
    ):
        return False
    expected = sweep_command()
    if manager == "launchd":
        return command == expected
    try:
        env_executable = _resolve_scheduler("env")
    except ServiceError:
        return None
    return command == [env_executable, "--", *expected]


def _registry_file_state(data_dir: str | None) -> str | None:
    if not data_dir:
        return None
    path = Path(data_dir) / "registry.json"
    try:
        info = os.stat(path, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(info.st_mode):
        return "unsafe"
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        return "unsafe"
    if stat.S_IMODE(info.st_mode) & 0o077:
        return "unsafe"
    return "present"


def service_status() -> dict[str, object]:
    """Read-only report of the scheduled sweep service installation.

    Never creates directories or files and never issues a state-changing
    scheduler command. ``files`` maps each managed file to its
    :func:`_installed_file_state`; ``active`` is the scheduler's own view
    (``None`` when the scheduler cannot be queried); ``environment`` is the
    pinned ``EPHEMDIR_*`` environment recovered from the installed file.
    """
    status: dict[str, object]
    if sys.platform == "darwin":
        path = _launchd_path()
        files = {str(path): _installed_file_state(path, None)}
        active: bool | None
        try:
            active = _run_scheduler("launchctl", ["list", LAUNCHD_LABEL]).returncode == 0
        except (ServiceError, OSError):
            active = None
        status = {
            "platform": "darwin",
            "manager": "launchd",
            "unit": LAUNCHD_LABEL,
            "files": files,
        }
        definition = (
            _pinned_service_definition(path)
            if files[str(path)] == "installed"
            else {}
        )
    elif sys.platform.startswith("linux"):
        units_dir = _systemd_dir()
        unit_files = (units_dir / f"{SYSTEMD_UNIT}.service", units_dir / f"{SYSTEMD_UNIT}.timer")
        files = {str(unit): _installed_file_state(unit, None) for unit in unit_files}
        try:
            systemctl = _resolve_scheduler("systemctl")
            show = _run(
                [systemctl, "--user", "show", "-p", "ActiveState", "--value",
                 f"{SYSTEMD_UNIT}.timer"]
            )
            active = show.stdout.strip() == "active" if show.returncode == 0 else None
        except (ServiceError, OSError):
            active = None
        status = {
            "platform": sys.platform,
            "manager": "systemd",
            "unit": f"{SYSTEMD_UNIT}.timer",
            "files": files,
        }
        definition = (
            _pinned_service_definition(unit_files[0])
            if files[str(unit_files[0])] == "installed"
            else {}
        )
    else:
        return {
            "platform": sys.platform,
            "manager": None,
            "unit": None,
            "files": {},
            "installed": False,
            "active": None,
            "environment": {},
            "command": [],
            "working_directory": None,
            "command_valid": None,
            "working_directory_valid": None,
            "registry_status": None,
            "registry_present": None,
        }
    status["installed"] = any(state != "not-installed" for state in files.values())
    status["active"] = active
    pinned = definition.get("environment", {}) if isinstance(definition, dict) else {}
    if not isinstance(pinned, dict):
        pinned = {}
    command = definition.get("command", []) if isinstance(definition, dict) else []
    working_directory = (
        definition.get("working_directory") if isinstance(definition, dict) else None
    )
    status["environment"] = pinned
    status["command"] = command
    status["working_directory"] = working_directory
    if definition:
        status["command_valid"] = _service_command_valid(
            str(status["manager"]), command
        )
        status["working_directory_valid"] = working_directory == "/"
    else:
        status["command_valid"] = None
        status["working_directory_valid"] = None
    data_dir = pinned.get("EPHEMDIR_DATA_DIR")
    registry_state = _registry_file_state(data_dir if isinstance(data_dir, str) else None)
    status["registry_status"] = registry_state
    status["registry_present"] = registry_state == "present" if registry_state else None
    return status
