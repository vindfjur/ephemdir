"""Diagnostics for ephemdir installations."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from ._platform import boot_session_id, boot_time, user_config_dir, user_data_dir
from ._registry import (
    _MAX_REGISTRY_BYTES,
    _REGISTRY_SCHEMA_VERSION,
    Registry,
    RegistryFormatError,
    RegistryTooLargeError,
    RegistryUnavailableError,
    UnsafeRegistryError,
    _extract_entries,
    _open_nofollow,
    _reject_json_constant,
    _valid_entry,
)

__all__ = ["DoctorCheck", "run_doctor"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    message: str
    hint: str | None = None


def run_doctor(*, registry: Registry | None = None) -> list[DoctorCheck]:
    reg = registry or Registry()
    data_dir = user_data_dir(create=False)
    config_dir = user_config_dir(create=False)
    checks: list[DoctorCheck] = []
    checks.append(
        DoctorCheck(
            "backend",
            os.name == "posix",
            "posix backend" if os.name == "posix" else f"unsupported backend on {sys.platform}",
            None if os.name == "posix" else "Use ephemdir only on supported local filesystems.",
        )
    )
    checks.append(DoctorCheck("registry-path", True, str(reg.path)))
    checks.append(DoctorCheck("data-dir-source", True, _data_dir_source()))
    checks.append(DoctorCheck("config-dir-source", True, _config_dir_source()))
    checks.append(DoctorCheck("xdg-data-home", True, _env_value("XDG_DATA_HOME")))
    checks.append(DoctorCheck("xdg-config-home", True, _env_value("XDG_CONFIG_HOME")))
    checks.append(
        DoctorCheck(
            "service-env",
            True,
            " ".join(
                (
                    f"EPHEMDIR_DATA_DIR={Path(os.path.abspath(data_dir))}",
                    f"EPHEMDIR_CONFIG_DIR={Path(os.path.abspath(config_dir))}",
                )
            ),
        )
    )
    checks.append(_directory_check("data-dir", data_dir))
    checks.append(_directory_check("config-dir", config_dir))
    checks.extend(_registry_checks(reg))
    boot_id = boot_session_id()
    checks.append(
        DoctorCheck(
            "boot-id",
            boot_id is not None or boot_time() is not None,
            "boot identity available" if boot_id is not None else "boot time fallback available",
            None,
        )
    )
    return checks


def _env_value(name: str) -> str:
    value = os.environ.get(name)
    return f"{name}={value}" if value else f"{name}=unset"


def _data_dir_source() -> str:
    override = os.environ.get("EPHEMDIR_DATA_DIR")
    if override:
        return f"EPHEMDIR_DATA_DIR={override}"
    if sys.platform not in {"win32", "darwin"}:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return f"XDG_DATA_HOME={xdg}"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return f"platform env={base}"
    return "platform default"


def _config_dir_source() -> str:
    override = os.environ.get("EPHEMDIR_CONFIG_DIR")
    if override:
        return f"EPHEMDIR_CONFIG_DIR={override}"
    if sys.platform not in {"win32", "darwin"}:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return f"XDG_CONFIG_HOME={xdg}"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return f"platform env={base}"
    return "platform default"


def _directory_check(name: str, path: Path) -> DoctorCheck:
    chain_problem = _directory_chain_problem(path)
    if chain_problem is not None:
        return DoctorCheck(name, False, chain_problem, f"Replace {path}.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return DoctorCheck(name, True, f"{path} is not created yet")
    except OSError as error:
        return DoctorCheck(name, False, str(error), f"Create {path} with owner-only access.")
    if not stat.S_ISDIR(info.st_mode):
        return DoctorCheck(name, False, f"{path} is not a real directory", f"Replace {path}.")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return DoctorCheck(
            name,
            False,
            f"{path} is not owned by the current user",
            "Move ephemdir state under an owner-owned directory.",
        )
    private = True
    if os.name == "posix":
        private = (info.st_mode & 0o077) == 0
    return DoctorCheck(
        name,
        private,
        f"{path}" if private else f"{path} is accessible to other users",
        None if private else f"Run: chmod 700 {path}",
    )


def _directory_chain_problem(path: Path) -> str | None:
    """Return a diagnostic for symlinked or non-directory ancestors."""
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return f"{current} is a symlink"
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return f"{current} is not a directory"
    return None


def _registry_checks(registry: Registry) -> list[DoctorCheck]:
    try:
        count, schema = _diagnose_registry(registry.path)
    except FileNotFoundError:
        return [DoctorCheck("registry", True, f"{registry.path} is not created yet")]
    except RegistryTooLargeError as error:
        return [DoctorCheck("registry", False, str(error), "Inspect or rotate the registry.")]
    except (RegistryFormatError, UnsafeRegistryError, RegistryUnavailableError) as error:
        return [DoctorCheck("registry", False, str(error), "Inspect registry contents and trust.")]
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return [DoctorCheck("registry", False, str(error), "Inspect or repair the registry JSON.")]
    return [
        DoctorCheck("registry", True, f"{count} tracked entrie(s)"),
        _schema_check(schema),
    ]


def _schema_check(schema: int | None) -> DoctorCheck:
    """Report the on-disk registry schema and any pending forward migration."""
    current = _REGISTRY_SCHEMA_VERSION
    on_disk = "v1 (flat)" if schema is None else f"v{schema}"
    needs_migration = schema is None or schema < current
    if not needs_migration:
        return DoctorCheck("registry-schema", True, f"schema {on_disk}")
    return DoctorCheck(
        "registry-schema",
        True,
        f"schema {on_disk}; will migrate to v{current} on next change",
        f"A timestamped backup is kept beside the registry when it migrates to v{current}.",
    )


def _diagnose_registry(path: Path) -> tuple[int, int | None]:
    """Read registry bytes without mutating, quarantining, or following symlinks.

    Returns the entry count and the on-disk schema version (``None`` for a flat
    pre-envelope v1 registry).
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    fd = _open_nofollow(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RegistryFormatError(f"registry {path} is not a regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise UnsafeRegistryError(f"registry {path} is not owned by the current user")
        if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o022:
            raise UnsafeRegistryError(f"registry {path} is writable by other users")
        if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o044:
            raise UnsafeRegistryError(f"registry {path} is readable by other users")
        if info.st_size > _MAX_REGISTRY_BYTES:
            raise RegistryTooLargeError("registry file is larger than 1 MiB")
        raw = handle.read(_MAX_REGISTRY_BYTES + 1)
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise RegistryTooLargeError("registry file is larger than 1 MiB")
    data = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    entries = _extract_entries(data)
    schema = data.get("schema_version") if isinstance(data, dict) else None
    malformed = [
        key
        for key, entry in entries.items()
        if not isinstance(key, str) or not _valid_entry(key, entry)
    ]
    if malformed:
        raise RegistryFormatError("registry contains malformed entries")
    return len(entries), schema
