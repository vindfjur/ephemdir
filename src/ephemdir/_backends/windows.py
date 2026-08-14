"""Conservative Windows backend placeholder."""

from __future__ import annotations

from pathlib import Path

from ephemdir._registry import Entry
from ephemdir._size import SizeResult, measure_tree

from .base import (
    BackendCapabilities,
    ClaimPreflight,
    CreatedDirectory,
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
        marker_id: str | None = None,
        *,
        parent_fd: int | None = None,
    ) -> CreatedDirectory:
        raise NotImplementedError("Windows creation backend is not enabled")

    def probe_ownership(self, path: Path, entry: Entry) -> OwnershipProbe:
        return OwnershipProbe("unsupported", "Windows ownership probing is not enabled")

    def preflight_claim_source(
        self,
        path: Path,
        parent_fd: int,
        entry: Entry,
        *,
        allow_unverified: bool,
    ) -> ClaimPreflight:
        raise NotImplementedError("Windows claim backend is not enabled")

    def rename_to_staging(
        self,
        path: Path,
        staging: Path,
        parent_fd: int,
        source_fd: int,
    ) -> None:
        raise NotImplementedError("Windows claim backend is not enabled")

    def delete_claimed_tree(self, staging: Path, entry: Entry) -> None:
        raise NotImplementedError("Windows deletion backend is not enabled")

    def measure_tree(
        self,
        path: Path,
        entry: Entry,
        limit: int | None,
        budget: ScanBudget,
    ) -> SizeResult:
        return measure_tree(path, limit=limit)
