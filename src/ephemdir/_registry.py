"""Persistent registry of tracked ephemeral directories.

The registry is a single JSON file stored in the per-user data directory. It
maps each directory's absolute path to its metadata (creation time, optional
expiry time, restart policy and the boot session it was created in).

Writes are atomic (write-to-temp then ``os.replace``) and guarded by a simple
cross-platform lock file so that a background ``sweep`` and a foreground
``tempdir()`` call cannot corrupt the file when they run concurrently.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from ._platform import user_data_dir

__all__ = ["Registry", "Entry"]

# One registry entry. Kept as a plain dict for trivial JSON serialization.
Entry = Dict[str, object]

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05


class Registry:
    """Read/modify/write access to the persisted directory registry."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (user_data_dir() / "registry.json")
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> Dict[str, Entry]:
        """Return the registry contents, or an empty mapping if absent/corrupt."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        # Be defensive: ignore anything that is not the expected shape.
        return data if isinstance(data, dict) else {}

    def save(self, state: Dict[str, Entry]) -> None:
        """Atomically persist ``state`` to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)

    @contextmanager
    def transaction(self) -> Iterator[Dict[str, Entry]]:
        """Lock, yield a mutable copy of the state, then save it on exit.

        Usage::

            with registry.transaction() as state:
                state[path] = entry
        """
        with self._lock():
            state = self.load()
            yield state
            self.save(state)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Acquire a best-effort exclusive lock via an atomic lock file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        acquired = False
        while True:
            try:
                # O_CREAT | O_EXCL fails if the lock file already exists,
                # which is our atomic "test and set".
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    # Assume a stale lock from a crashed process and proceed.
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if acquired:
                try:
                    os.unlink(self._lock_path)
                except FileNotFoundError:
                    pass
