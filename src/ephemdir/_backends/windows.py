"""Conservative Windows backend placeholder."""

from __future__ import annotations

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

__all__ = ["WindowsBackend"]


class WindowsBackend:
    name = "windows"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            safe_delete=False,
            reason="safe Windows handle-bound deletion is not enabled in this build",
        )

    def validate_parent(self, parent: Path) -> ParentValidation:
        return ParentValidation(parent.is_dir(), None if parent.is_dir() else "not a directory")

    def create_owned_directory(
        self,
        parent: Path,
        name: str,
        marker_id: str,
    ) -> CreatedDirectory:
        raise NotImplementedError("Windows creation backend is not enabled")

    def probe_ownership(self, path: Path, entry: Entry) -> OwnershipProbe:
        return OwnershipProbe("unsupported", "Windows ownership probing is not enabled")

    def claim(self, path: Path, entry: Entry, staging_name: str) -> ClaimedDirectory:
        raise NotImplementedError("Windows claim backend is not enabled")

    def delete_claimed_tree(
        self,
        claimed: ClaimedDirectory,
        budget: DeleteBudget,
    ) -> DeleteResult:
        return DeleteResult(False, "Windows deletion backend is not enabled")

    def measure_tree(
        self,
        path: Path,
        entry: Entry,
        limit: int | None,
        budget: ScanBudget,
    ) -> SizeResult:
        return measure_tree(path, limit=limit)
