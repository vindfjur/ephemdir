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
from pathlib import Path
from typing import Any

from ._platform import user_config_dir

__all__ = ["load_config", "config_path", "RECOGNIZED_KEYS"]

logger = logging.getLogger("ephemdir")

# Keys honoured in the config file; anything else is ignored with a warning.
RECOGNIZED_KEYS = frozenset(
    {"lifetime", "remove_on_restart", "keep_while_in_use", "parent", "prefix", "words"}
)

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
    if not target.exists():
        return {}
    if _toml is None:
        logger.warning(
            "config file %s ignored: no TOML parser (install 'tomli' on Python < 3.11)",
            target,
        )
        return {}

    try:
        with open(target, "rb") as handle:
            data = _toml.load(handle)
    except (OSError, ValueError) as error:
        logger.warning("could not read config %s: %s", target, error)
        return {}

    recognized: dict[str, Any] = {}
    for key, value in data.items():
        if key in RECOGNIZED_KEYS:
            recognized[key] = value
        else:
            logger.warning("ignoring unknown config key %r in %s", key, target)
    return recognized
