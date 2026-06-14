"""User-level configuration for ephemdir defaults.

Settings live in a small TOML file in the per-user config directory
(``config.toml``). Every key is optional; anything omitted falls back to the
library's built-in defaults. Example::

    # ~/.config/ephemdir/config.toml
    lifetime = "6h"
    remove_on_restart = true
    keep_while_in_use = true
    prefix = "scratch-"
    words = 2

Parsing requires a TOML reader: the standard-library ``tomllib`` (Python 3.11+)
or the third-party ``tomli`` on older versions. When neither is available the
config file is ignored and built-in defaults apply.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any

from ._platform import user_config_dir

__all__ = ["load_config", "config_path", "RECOGNIZED_KEYS"]

logger = logging.getLogger("ephemdir")

_MAX_CONFIG_BYTES = 1_048_576

# Keys honoured in the config file with their accepted types; anything else is
# ignored with a warning. Type-checking here prevents surprises like the string
# "false" being truthy where a real boolean is expected.
_KEY_TYPES: dict[str, tuple[type, ...]] = {
    "lifetime": (str, int, float),
    "remove_on_restart": (bool,),
    "keep_while_in_use": (bool,),
    "parent": (str,),
    "prefix": (str,),
    "words": (int,),
}
RECOGNIZED_KEYS = frozenset(_KEY_TYPES)

def _import_toml() -> Any:
    """Return a TOML reader: stdlib ``tomllib`` (3.11+), ``tomli``, or ``None``."""
    try:  # Python 3.11+
        import tomllib

        return tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python < 3.11
        try:
            import tomli

            return tomli
        except ModuleNotFoundError:
            return None


# Resolved once at import time; ``None`` means no TOML parser is available.
_toml = _import_toml()


def config_path() -> Path:
    """Return the path to the user config file (it may not exist)."""
    return user_config_dir() / "config.toml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load recognized settings from the config file.

    Returns an empty mapping when the file is absent, unreadable, or no TOML
    parser is available. Unknown keys are dropped with a warning so a typo
    never silently changes behaviour.
    """
    target = path or config_path()
    # O_NONBLOCK matters beyond regular-file semantics: opening a FIFO planted
    # at the config path would otherwise block until a writer appears, before
    # fstat() ever gets the chance to reject it. There is deliberately no
    # Path.exists() pre-check — the open itself reports an absent file without
    # a window for swapping the path between two filesystem operations.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        try:
            fd = os.open(target, flags)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as handle:
            config_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(config_stat.st_mode):
                raise OSError("config path is not a regular file")
            if _toml is None:
                logger.warning(
                    "config file %s ignored: no TOML parser "
                    "(install 'tomli' on Python < 3.11)",
                    target,
                )
                return {}
            # The config decides where future directories are created, so a
            # file another local user can rewrite must never be honoured.
            if os.name == "posix":
                if config_stat.st_uid not in (0, os.geteuid()):
                    raise OSError("config file is not owned by the current user")
                if config_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise OSError("config file is writable by other users")
            if config_stat.st_size > _MAX_CONFIG_BYTES:
                raise ValueError("config file is larger than 1 MiB")
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
            if len(raw) > _MAX_CONFIG_BYTES:
                raise ValueError("config file is larger than 1 MiB")
            data = _toml.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as error:
        logger.warning("could not read config %s: %s", target, error)
        return {}

    recognized: dict[str, Any] = {}
    for key, value in data.items():
        expected = _KEY_TYPES.get(key)
        if expected is None:
            logger.warning("ignoring unknown config key %r in %s", key, target)
            continue
        # bool is a subclass of int: reject true where a number is expected.
        if (isinstance(value, bool) and bool not in expected) or not isinstance(value, expected):
            logger.warning(
                "ignoring config key %r in %s: expected %s, got %s",
                key,
                target,
                " or ".join(t.__name__ for t in expected),
                type(value).__name__,
            )
            continue
        recognized[key] = value
    return recognized
