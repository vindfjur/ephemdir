"""Backend protocol and shared data structures.

The protocol mirrors the real seam between :mod:`ephemdir.core` and a
filesystem backend (APA-BE-002): the backend owns the syscall clusters for
create / ownership probe / claim (preflight + rename) / delete, while core
keeps registry transactions, the decision policy and sequencing. Claim is
therefore two backend calls — ``preflight_claim_source`` before the journal
intent is written and ``rename_to_staging`` after — because the journal write
between them is core's job, not the backend's.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol

from ephemdir._registry import Entry
from ephemdir._size import SizeResult

__all__ = [
    "BackendCapabilities",
    "ParentValidation",
    "CreatedDirectory",
    "OwnershipProbe",
    "ClaimPreflight",
    "ScanBudget",
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
    dev: int
    ino: int


@dataclass(frozen=True)
class OwnershipProbe:
    status: str
    reason: str | None = None


class ClaimPreflight(NamedTuple):
    """Verified-source handle for a claim, or the refusal status.

    A ``NamedTuple`` so existing 4-tuple unpacking in core stays valid.
    """

    status: str
    source_fd: int | None
    source_stat: os.stat_result | None
    root_mount_id: int | None


@dataclass(frozen=True)
class ScanBudget:
    max_entries: int | None = None


class DeletionBackend(Protocol):
    name: str

    def capabilities(self) -> BackendCapabilities: ...

    def validate_parent(self, parent: Path) -> ParentValidation: ...

    def create_owned_directory(
        self,
        parent: Path,
        name: str,
        marker_id: str | None = None,
        *,
        parent_fd: int | None = None,
    ) -> CreatedDirectory: ...

    def probe_ownership(self, path: Path, entry: Entry) -> OwnershipProbe: ...

    def preflight_claim_source(
        self,
        path: Path,
        parent_fd: int,
        entry: Entry,
        *,
        allow_unverified: bool,
    ) -> ClaimPreflight: ...

    def rename_to_staging(
        self,
        path: Path,
        staging: Path,
        parent_fd: int,
        source_fd: int,
    ) -> None: ...

    def delete_claimed_tree(self, staging: Path, entry: Entry) -> None: ...

    def measure_tree(
        self,
        path: Path,
        entry: Entry,
        limit: int | None,
        budget: ScanBudget,
    ) -> SizeResult: ...
