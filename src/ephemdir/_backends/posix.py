"""POSIX backend descriptor.

The safety-critical implementation still lives in :mod:`ephemdir.core` for
v0.5.0 compatibility; this backend exposes platform capabilities and delegates
read-only measurement.
"""

from __future__ import annotations

import os
from pathlib import Path

from ephemdir._registry import Entry
from ephemdir._size import SizeResult, measure_tree

from .base import (
    BackendCapabilities,
    ClaimedDirectory,
    CreatedDirectory,
    DeleteBudget,
    DeleteResult,
    OwnershipProbe,
    ParentValidation,
    ScanBudget,
)

__all__ = ["PosixBackend"]


class PosixBackend:
    name = "posix"

    def capabilities(self) -> BackendCapabilities:
        safe = os.name == "posix"
        return BackendCapabilities(
            safe_delete=safe,
            reason=None if safe else "not running on POSIX",
        )

    def validate_parent(self, parent: Path) -> ParentValidation:
        try:
            info = parent.stat()
        except OSError as error:
            return ParentValidation(False, str(error))
        if not parent.is_dir():
            return ParentValidation(False, "parent is not a directory")
        if os.name == "posix" and hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            return ParentValidation(False, "parent is not owned by the current user")
        return ParentValidation(True)

    def create_owned_directory(
        self,
        parent: Path,
        name: str,
        marker_id: str,
    ) -> CreatedDirectory:
        raise NotImplementedError("directory creation is orchestrated by ephemdir.core")

    def probe_ownership(self, path: Path, entry: Entry) -> OwnershipProbe:
        raise NotImplementedError("ownership probing is orchestrated by ephemdir.core")

    def claim(self, path: Path, entry: Entry, staging_name: str) -> ClaimedDirectory:
        raise NotImplementedError("claim is orchestrated by ephemdir.core")

    def delete_claimed_tree(
        self,
        claimed: ClaimedDirectory,
        budget: DeleteBudget,
    ) -> DeleteResult:
        raise NotImplementedError("deletion is orchestrated by ephemdir.core")

    def measure_tree(
        self,
        path: Path,
        entry: Entry,
        limit: int | None,
        budget: ScanBudget,
    ) -> SizeResult:
        return measure_tree(path, limit=limit, max_entries=budget.max_entries or 100_000)
