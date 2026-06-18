"""Read-only cleanup decision engine."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ._registry import Entry

__all__ = ["CleanupDecision", "CleanupPolicy", "SweepMode", "decide_cleanup"]


class CleanupPolicy(str, Enum):
    AUTO = "auto"
    NEXT_SWEEP = "next-sweep"


class SweepMode(str, Enum):
    MAINTENANCE = "maintenance"
    FULL = "full"
    FORCE = "force"


@dataclass(frozen=True)
class CleanupDecision:
    path: Path
    due: bool
    status: str
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    destructive_allowed: bool
    measured_size_bytes: int | None
    max_size_bytes: int | None


def decide_cleanup(
    path: Path,
    entry: Entry,
    *,
    now: float,
    current_boot: float | None,
    current_boot_id: str | None,
    mode: SweepMode,
    path_state: str,
    ownership: str,
    parent_error: str | None,
    in_use: bool | None = False,
    measured_size_bytes: int | None = None,
    size_complete: bool = True,
    same_boot_func: Callable[[float | None, float | None], bool] | None = None,
) -> CleanupDecision:
    """Return a deletion decision without mutating registry or filesystem."""
    reasons: list[str] = []
    blockers: list[str] = []

    if mode is SweepMode.FORCE:
        reasons.append("forced")

    expires_at = entry.get("expires_at")
    if isinstance(expires_at, (int, float)) and now >= float(expires_at):
        reasons.append("expired")

    if entry.get("remove_on_restart") and _restarted(
        entry,
        current_boot=current_boot,
        current_boot_id=current_boot_id,
        same_boot_func=same_boot_func,
    ):
        reasons.append("restarted")

    if (
        entry.get("cleanup_policy", CleanupPolicy.AUTO.value) == CleanupPolicy.NEXT_SWEEP.value
        and mode in {SweepMode.FULL, SweepMode.FORCE}
    ):
        reasons.append("next-sweep")

    max_size_value = entry.get("max_size")
    max_size = max_size_value if isinstance(max_size_value, int) and max_size_value >= 0 else None
    if max_size is not None:
        if measured_size_bytes is None:
            blockers.append("size-unknown")
        elif measured_size_bytes > max_size:
            reasons.append("oversize")
        if (
            not size_complete
            and measured_size_bytes is not None
            and measured_size_bytes <= max_size
        ):
            blockers.append("scan-budget-exceeded")

    if path_state == "unknown":
        blockers.append("ownership-unverified")
    if ownership == "foreign":
        blockers.append("identity-changed")
    elif ownership == "unverified":
        blockers.append("ownership-unverified")
    if parent_error is not None:
        blockers.append("unsafe-parent")
    backend = entry.get("backend")
    if isinstance(backend, str) and backend != "posix":
        blockers.append("unsupported-backend")
    platform = entry.get("platform")
    if isinstance(platform, str) and platform != sys.platform:
        blockers.append("foreign-platform")
    if in_use is True:
        blockers.append("in-use")
    elif in_use is None and sys.platform != "win32":
        blockers.append("in-use-unknown")

    due = bool(reasons)
    destructive_allowed = due and not blockers
    status = "due" if due else "active"
    if blockers:
        status = "blocked"
    return CleanupDecision(
        path=path,
        due=due,
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        blockers=tuple(dict.fromkeys(blockers)),
        destructive_allowed=destructive_allowed,
        measured_size_bytes=measured_size_bytes,
        max_size_bytes=max_size,
    )


def _restarted(
    entry: Entry,
    *,
    current_boot: float | None,
    current_boot_id: str | None,
    same_boot_func: Callable[[float | None, float | None], bool] | None,
) -> bool:
    created_id = entry.get("boot_id")
    if current_boot_id is not None:
        return isinstance(created_id, str) and created_id != current_boot_id
    if isinstance(created_id, str):
        return False
    if sys.platform == "darwin" and same_boot_func is not None:
        created_boot = entry.get("boot_time")
        created_boot = created_boot if isinstance(created_boot, (int, float)) else None
        return not same_boot_func(created_boot, current_boot)
    return False
