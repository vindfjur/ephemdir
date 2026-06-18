"""Backend protocol and shared data structures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ephemdir._registry import Entry
from ephemdir._size import SizeResult

__all__ = [
    "BackendCapabilities",
    "ParentValidation",
    "CreatedDirectory",
    "OwnershipProbe",
    "ClaimedDirectory",
    "DeleteBudget",
    "ScanBudget",
    "DeleteResult",
    "DeletionBackend",
]


@dataclass(frozen=True)
class BackendCapabilities:
    safe_delete: bool
    local_filesystem_required: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class ParentValidation:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class CreatedDirectory:
    path: Path
    marker_id: str
    identity: dict[str, object]


@dataclass(frozen=True)
class OwnershipProbe:
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class ClaimedDirectory:
    original: Path
    staging: Path
    identity: dict[str, object]


@dataclass(frozen=True)
class DeleteBudget:
    max_entries: int | None = None


@dataclass(frozen=True)
class ScanBudget:
    max_entries: int | None = None


@dataclass(frozen=True)
class DeleteResult:
    removed: bool
    error: str | None = None


class DeletionBackend(Protocol):
    name: str

    def capabilities(self) -> BackendCapabilities: ...

    def validate_parent(self, parent: Path) -> ParentValidation: ...

    def create_owned_directory(
        self,
        parent: Path,
        name: str,
        marker_id: str,
    ) -> CreatedDirectory: ...

    def probe_ownership(self, path: Path, entry: Entry) -> OwnershipProbe: ...

    def claim(self, path: Path, entry: Entry, staging_name: str) -> ClaimedDirectory: ...

    def delete_claimed_tree(
        self,
        claimed: ClaimedDirectory,
        budget: DeleteBudget,
    ) -> DeleteResult: ...

    def measure_tree(
        self,
        path: Path,
        entry: Entry,
        limit: int | None,
        budget: ScanBudget,
    ) -> SizeResult: ...
