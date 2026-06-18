"""Shared presentation helpers."""

from __future__ import annotations

from ._policy import CleanupDecision

__all__ = ["escape_human_text", "format_decision"]


def escape_human_text(value: object) -> str:
    """Escape control characters for terminal-facing output."""
    text = str(value)
    escaped = []
    for char in text:
        code = ord(char)
        if code < 32 or code == 127:
            escaped.append(f"\\x{code:02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def format_decision(decision: CleanupDecision) -> str:
    reasons = ", ".join(decision.reasons) if decision.reasons else "none"
    blockers = ", ".join(decision.blockers) if decision.blockers else "none"
    size = (
        "unknown"
        if decision.measured_size_bytes is None
        else str(decision.measured_size_bytes)
    )
    return (
        f"{escape_human_text(decision.path)}: {decision.status}; due={decision.due}; "
        f"reasons={reasons}; blockers={blockers}; size={size}"
    )
