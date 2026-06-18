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

import importlib.util
import logging
import os
import plistlib
import site
import stat
import subprocess  # nosec B404
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

from ._platform import user_config_dir, user_data_dir
from ._security import open_private_directory
from ._trusted_exec import resolve_executable_in_dirs, trusted_system_dirs

__all__ = ["ServiceError", "install_service", "uninstall_service", "sweep_command"]

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
        try:
            fd = open_private_directory(directory, create=True)
        except OSError as error:
            raise ServiceError(
                f"refusing to install service: unsafe {label} directory {directory}: {error}"
            ) from error
        else:
            os.close(fd)
    return {
        "EPHEMDIR_DATA_DIR": str(data_dir),
        "EPHEMDIR_CONFIG_DIR": str(config_dir),
    }


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


def _writable_by_other_users(mode: int) -> bool:
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


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


def _check_runtime_component(current: Path, info: os.stat_result, description: str) -> None:
    """Reject one runtime path component another local user could replace.

    Group/world-writability is not the only takeover vector: the owner of a
    component can always rewrite it regardless of its mode, so every
    component must belong to root or the installing user. A `0755` venv owned
    by a different local user must never become a scheduled service runtime.
    """
    if stat.S_ISLNK(info.st_mode):
        return  # The resolved chain re-checks what the link points to.
    if _writable_by_other_users(info.st_mode):
        raise ServiceError(
            f"refusing to install service: {description} {current} is "
            "group/world-writable (writable by other users); shared directories "
            "like /tmp cannot host a scheduled service runtime, sticky bit or not."
            f"{_safe_uv_runtime_hint()}"
        )
    if hasattr(os, "geteuid") and info.st_uid not in (0, os.geteuid()):
        raise ServiceError(
            f"refusing to install service: {description} {current} is "
            "owned by another user"
        )


def _validate_runtime_chain(absolute: Path, label: str) -> os.stat_result:
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
        _check_runtime_component(current, info, f"{label} path component")

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
            _check_runtime_component(current, info, f"resolved {label} path component")

    try:
        return os.stat(absolute)
    except OSError as error:
        raise ServiceError(f"cannot stat {label} path {absolute}: {error}") from error


def _validate_runtime_path(path: Path, label: str) -> None:
    """Reject service runtime paths another local user could swap or edit.

    A scheduled job persists beyond the current shell, so every existing path
    component leading to the interpreter or imported package must resist
    replacement: not writable by group members or arbitrary local users, and
    owned by root or the installing user. User-owned virtual environments
    remain supported.
    """
    absolute = Path(os.path.abspath(path))
    final_info = _validate_runtime_chain(absolute, label)
    if not stat.S_ISREG(final_info.st_mode):
        raise ServiceError(f"refusing to install service: {label} is not a regular file")
    if hasattr(os, "geteuid") and final_info.st_uid not in (0, os.geteuid()):
        raise ServiceError(f"refusing to install service: {label} is owned by another user")


def _validate_runtime_dir(path: Path, label: str) -> None:
    """Reject a runtime *directory* another local user could swap or populate.

    Unlike :func:`_validate_runtime_path`, the final component must be a real
    directory. This matters for an otherwise-empty package subdirectory (e.g.
    a freshly created, attacker-owned ``__pycache__``): it holds no module file
    to validate indirectly today, but its owner can drop an unchecked ``.pyc``
    into it after the service is installed, which the interpreter would then
    load. Validating the directory itself closes that gap.
    """
    absolute = Path(os.path.abspath(path))
    final_info = _validate_runtime_chain(absolute, label)
    if not stat.S_ISDIR(final_info.st_mode):
        raise ServiceError(f"refusing to install service: {label} is not a directory")
    if _writable_by_other_users(final_info.st_mode):
        raise ServiceError(
            f"refusing to install service: {label} {absolute} is "
            "group/world-writable (writable by other users)."
            f"{_safe_uv_runtime_hint()}"
        )
    if hasattr(os, "geteuid") and final_info.st_uid not in (0, os.geteuid()):
        raise ServiceError(f"refusing to install service: {label} is owned by another user")


# Files the interpreter can load and execute as the service user. A `.py`
# source, its cached bytecode or a native extension are all code the scheduled
# `python -I -m ephemdir sweep` may import, so each must resist local tampering.
_EXECUTABLE_MODULE_SUFFIXES = (".py", ".pyc", ".pyo", ".so", ".pyd", ".dylib")


def _validate_package_tree(package_dir: Path) -> None:
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
        _validate_runtime_dir(root_path, "ephemdir package directory")
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
            _validate_runtime_dir(sub, "ephemdir package directory")
        for name in files:
            file_path = root_path / name
            if file_path.suffix in _EXECUTABLE_MODULE_SUFFIXES:
                _validate_runtime_path(file_path, "ephemdir module")
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


def _validate_startup_environment() -> None:
    """Verify interpreter-startup hooks resist tampering by other local users."""
    for path, label in _iter_startup_files():
        _validate_runtime_path(path, label)

    # On Python 3.10 the sweep imports `tomli` for TOML config; the whole
    # package runs as the service user, so validate every module in it.
    if sys.version_info < (3, 11):
        try:
            tomli_spec = importlib.util.find_spec("tomli")
        except (ImportError, AttributeError, ValueError):  # pragma: no cover - defensive
            tomli_spec = None
        if tomli_spec is not None and tomli_spec.origin:
            _validate_package_tree(Path(tomli_spec.origin).resolve().parent)


def _validate_service_runtime() -> None:
    _validate_runtime_path(Path(sys.executable), "Python interpreter")
    _validate_package_tree(Path(__file__).resolve().parent)
    _validate_startup_environment()


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
    _write_service_file(
        path,
        render_launchd_plist(
            interval,
            sweep_command(),
            environment=_effective_service_environment(),
        ),
    )
    # Unload best-effort: it legitimately fails when the agent was not loaded.
    _run_scheduler("launchctl", ["unload", str(path)])
    _run_checked([_resolve_scheduler("launchctl"), "load", str(path)], "launchctl load")
    return f"installed LaunchAgent at {path} (sweeps every {interval}s)"


def _uninstall_launchd() -> str:
    path = _launchd_path()
    if not path.exists():
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
    path.unlink()
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
    return user_config_dir().parent / "systemd" / "user"


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
    _run_checked([systemctl, "--user", "daemon-reload"], "systemctl daemon-reload")
    _run_checked(
        [systemctl, "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"],
        "systemctl enable",
    )
    return f"installed systemd user timer in {units_dir} (sweeps every {interval}s)"


def _uninstall_systemd() -> str:
    units_dir = _systemd_dir()
    unit_files = [units_dir / f"{SYSTEMD_UNIT}.service", units_dir / f"{SYSTEMD_UNIT}.timer"]
    if not any(unit.exists() for unit in unit_files):
        return "no systemd timer installed"
    # `disable` fails when the timer was never enabled; the authoritative
    # check is the unit's ActiveState afterwards. `show` reports
    # "inactive" for unknown units and exits 0, so a failing command (as
    # opposed to a stopped timer) is distinguishable and treated as an error.
    systemctl = _resolve_scheduler("systemctl")
    _run([systemctl, "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"])
    show = _run(
        [systemctl, "--user", "show", "-p", "ActiveState", "--value", f"{SYSTEMD_UNIT}.timer"]
    )
    state = show.stdout.strip()
    # Removing the unit files is only safe when the timer is provably stopped;
    # "activating"/"deactivating"/"reloading" or an unreadable state are
    # treated as errors, not as absence.
    if state not in ("inactive", "failed"):
        detail = state or show.stderr.strip() or f"exit code {show.returncode}"
        raise ServiceError(
            f"could not confirm that {SYSTEMD_UNIT}.timer is stopped (state: {detail})"
        )
    for unit in unit_files:
        if unit.exists():
            unit.unlink()
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

def install_service(interval: int = 600) -> str:
    """Install the periodic sweep service for the current platform.

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
    _validate_service_runtime()
    _verify_isolated_import()
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
