"""Lifetime usage counters for ephemdir.

A tiny owner-only JSON ledger (``stats.json`` next to the registry) tracks how
many ephemeral directories were created, swept automatically, kept, or removed
by hand. It is deliberately *best-effort*: recording a counter must never block
or fail a real operation, and a corrupt or missing ledger simply reads as zero.
The ledger is never consulted by the deletion logic — losing a count is
harmless, so the counters trade strict accuracy under heavy concurrency for
staying completely out of the way of the directory lifecycle.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from ._platform import user_data_dir

__all__ = ["StatsLedger", "COUNTERS"]

logger = logging.getLogger("ephemdir")

# The counters the ledger maintains. Anything else found on disk is ignored.
COUNTERS = ("created", "swept", "kept", "removed")

_MAX_STATS_BYTES = 64 * 1024
_OWNER_ONLY_FILE_MODE = 0o600


class StatsLedger:
    """Read/increment the lifetime usage counters (best-effort)."""

    def __init__(self, path: Path | None = None) -> None:
        self._explicit_path = Path(path) if path is not None else None

    def _path(self, *, create: bool) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return user_data_dir(create=create) / "stats.json"

    def snapshot(self) -> dict[str, int]:
        """Return the current counters; all zero when absent or unreadable.

        Read-only: never creates the data directory or the ledger file.
        """
        counters = dict.fromkeys(COUNTERS, 0)
        try:
            path = self._path(create=False)
        except OSError:
            return counters
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return counters
        except OSError as error:
            logger.debug("stats ledger %s unavailable: %s", path, error)
            return counters
        try:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATS_BYTES:
                    return counters
                data = json.loads(handle.read(_MAX_STATS_BYTES + 1).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            logger.debug("ignoring unreadable stats ledger %s: %s", path, error)
            return counters
        if isinstance(data, dict):
            for name in COUNTERS:
                value = data.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    counters[name] = value
        return counters

    def record(self, **deltas: int) -> None:
        """Add to one or more counters. Any failure is swallowed (best-effort)."""
        try:
            self._record(deltas)
        except Exception as error:  # never let stats break a real operation
            logger.debug("could not record stats %s: %s", deltas, error)

    def _record(self, deltas: dict[str, int]) -> None:
        wanted = {
            name: int(delta)
            for name, delta in deltas.items()
            if name in COUNTERS and int(delta)
        }
        if not wanted:
            return
        current = self.snapshot()
        for name, delta in wanted.items():
            current[name] = max(0, current[name] + delta)
        path = self._path(create=True)
        payload = json.dumps(current, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
        directory = path.parent
        tmp = directory / f".{path.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _OWNER_ONLY_FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
