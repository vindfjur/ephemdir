"""Shared presentation helpers.

This is the single rendering layer for the CLI: colour handling, human-readable
sizes/durations/timestamps, the machine-readable JSON contract, and the
``explain`` decision trace all live here so every command formats the same way.
"""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from datetime import datetime
from typing import TextIO

from ._policy import CleanupDecision
from ._registry import Entry

__all__ = [
    "Painter",
    "color_enabled",
    "emit_json",
    "escape_human_text",
    "explain_payload",
    "explain_trace",
    "format_decision",
    "format_duration",
    "format_size",
    "format_timestamp",
    "supports_unicode",
]


def escape_human_text(value: object) -> str:
    """Escape control and formatting characters for terminal-facing output.

    Covers ASCII C0 controls and DEL, C1 controls, and Unicode format
    characters (category ``Cf``) such as U+202E RIGHT-TO-LEFT OVERRIDE and
    zero-width joiners, so a hostile name cannot inject escape sequences or
    spoof the rendered line with bidi/zero-width tricks.
    """
    text = str(value)
    escaped = []
    for char in text:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            # A raw undecodable byte smuggled in via POSIX surrogateescape; show
            # it as the original byte rather than letting it round-trip to stdout.
            escaped.append(f"\\x{code - 0xDC00:02x}")
        elif code < 32 or code == 127 or unicodedata.category(char) in ("Cc", "Cf", "Cs"):
            if code <= 0xFF:
                escaped.append(f"\\x{code:02x}")
            elif code <= 0xFFFF:
                escaped.append(f"\\u{code:04x}")
            else:
                escaped.append(f"\\U{code:08x}")
        else:
            escaped.append(char)
    return "".join(escaped)


# --- colour ----------------------------------------------------------------

_CODES = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "cyan": "\x1b[36m",
}


def color_enabled(stream: TextIO, mode: str = "auto") -> bool:
    """Whether ANSI colour should be used for ``stream`` under ``mode``.

    ``always``/``never`` are explicit; ``auto`` honours the ``NO_COLOR``
    convention and otherwise enables colour only for an interactive TTY, so a
    pipe or file capture never receives escape codes.
    """
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


class Painter:
    """Apply ANSI styles, or pass text through unchanged when colour is off."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        codes = "".join(_CODES[s] for s in styles if s in _CODES)
        return f"{codes}{text}{_CODES['reset']}" if codes else text


def supports_unicode(stream: TextIO) -> bool:
    """Return True when ``stream`` can encode the tick/bullet glyphs."""
    encoding = getattr(stream, "encoding", None) or ""
    try:
        "✓▸".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


# --- units -----------------------------------------------------------------


def format_timestamp(value: object) -> str:
    """Render a Unix timestamp as a readable local time, or ``never``."""
    if not isinstance(value, (int, float)):
        return "never"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    """Humanize a duration: ``"1d 4h"``, ``"1h 23m"``, ``"5m 12s"``, ``"42s"``.

    At most the two largest non-zero units are shown.
    """
    total = max(0, int(seconds))
    components = [("d", total // 86400), ("h", total % 86400 // 3600),
                  ("m", total % 3600 // 60), ("s", total % 60)]
    nonzero = [(unit, value) for unit, value in components if value]
    if not nonzero:
        return "0s"
    return " ".join(f"{value}{unit}" for unit, value in nonzero[:2])


def format_size(num_bytes: object) -> str:
    """Render a byte count with IEC binary units (KiB/MiB/GiB).

    Matches the ``--max-size`` input convention, where ``GiB`` is 1024-based.
    """
    if not isinstance(num_bytes, (int, float)):
        return "unknown"
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(num_bytes)} B"  # pragma: no cover - unreachable


# --- JSON contract ---------------------------------------------------------


def emit_json(payload: object, stream: TextIO | None = None) -> None:
    """Write the stable machine-readable JSON form to stdout (data, not logs)."""
    print(json.dumps(payload, indent=2, allow_nan=False), file=stream or sys.stdout)


# --- sweep dry-run (output kept stable for scripts) ------------------------


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


# --- explain decision trace ------------------------------------------------

# Human phrases for the deletion-trigger reasons that decide_cleanup emits.
_REASON_LABELS = {
    "forced": "forced by --force",
    "expired": "lifetime has expired",
    "restarted": "the machine restarted since it was created",
    "next-sweep": "marked for removal on the next full sweep",
    "oversize": "it grew past its size limit",
}

# Human phrases for the blockers that hold a due directory back.
_BLOCKER_LABELS = {
    "in-use": "files are still open inside it",
    "in-use-unknown": "cannot tell whether files are open (lsof unavailable)",
    "identity-changed": "the directory's identity no longer matches",
    "ownership-unverified": "ownership could not be verified",
    "unsafe-parent": "its parent directory is not safe to delete within",
    "unsupported-backend": "it was created on an unsupported backend",
    "foreign-platform": "it was created on a different platform",
    "size-unknown": "its size could not be measured",
    "scan-budget-exceeded": "it is too large to measure fully",
    "path-missing": "the directory is missing on disk",
}


def _seconds_left(entry: Entry, now: float) -> float | None:
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, (int, float)):
        return float(expires_at) - now
    return None


def _entry_tags(entry: Entry) -> list[str]:
    tags = entry.get("tags")
    return [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []


def _entry_description(entry: Entry) -> str | None:
    description = entry.get("description")
    return description if isinstance(description, str) else None


def explain_payload(decision: CleanupDecision, entry: Entry, now: float) -> dict[str, object]:
    """Stable machine form of an ``explain`` result."""
    remaining = _seconds_left(entry, now)
    checks: list[dict[str, object]] = [
        {"check": reason, "ok": False, "detail": _REASON_LABELS.get(reason, reason)}
        for reason in decision.reasons
    ]
    checks += [
        {"check": blocker, "ok": False, "detail": _BLOCKER_LABELS.get(blocker, blocker)}
        for blocker in decision.blockers
    ]
    return {
        "path": str(decision.path),
        "name": decision.path.name,
        "status": decision.status,
        "due": decision.due,
        "destructive_allowed": decision.destructive_allowed,
        # Unified with `list --json` on a single spelling (RC3 freeze).
        "remaining_seconds": None if remaining is None else int(remaining),
        "size_bytes": decision.measured_size_bytes,
        "max_size_bytes": decision.max_size_bytes,
        "reasons": list(decision.reasons),
        "blocked_by": list(decision.blockers),
        "decision": checks,
        "tags": _entry_tags(entry),
        "description": _entry_description(entry),
    }


def explain_trace(
    decision: CleanupDecision,
    entry: Entry,
    now: float,
    paint: Painter | None = None,
    *,
    ascii_only: bool = False,
) -> str:
    """Render a human-readable decision trace for one tracked directory."""
    paint = paint or Painter(False)
    tick = "[ok]" if ascii_only else "✓"
    cross = "[x]" if ascii_only else "✗"
    bullet = "[*]" if ascii_only else "▸"

    remaining = _seconds_left(entry, now)
    if decision.due and decision.destructive_allowed:
        headline = paint("will be removed on the next sweep", "yellow")
    elif decision.due:
        headline = paint("due, but held back", "red")
    elif remaining is not None and remaining > 0:
        headline = paint(f"kept for now ({format_duration(remaining)} left)", "green")
    elif remaining is not None:
        headline = paint(f"overdue by {format_duration(-remaining)}", "yellow")
    else:
        headline = paint("kept (no time limit)", "green")

    lines = [
        f"{escape_human_text(decision.path.name)}: {headline}",
        f"  {paint(escape_human_text(decision.path), 'dim')}",
    ]
    tags = _entry_tags(entry)
    if tags:
        rendered = " ".join(f"#{escape_human_text(tag)}" for tag in tags)
        lines.append(f"  {paint(rendered, 'cyan')}")
    description = _entry_description(entry)
    if description:
        lines.append(f"  {paint(escape_human_text(description), 'dim')}")

    if decision.reasons:
        lines.append("  due because:")
        for reason in decision.reasons:
            label = _REASON_LABELS.get(reason, reason)
            lines.append(f"    {paint(bullet, 'yellow')} {label} ({reason})")
    if decision.blockers:
        lines.append("  held back because:")
        for blocker in decision.blockers:
            label = _BLOCKER_LABELS.get(blocker, blocker)
            lines.append(f"    {paint(cross, 'red')} {label} ({blocker})")
    if not decision.due:
        if remaining is not None and remaining > 0:
            lines.append(f"  {paint(tick, 'green')} lifetime: {format_duration(remaining)} left")
        elif remaining is None:
            lines.append(f"  {paint(tick, 'green')} lifetime: no time limit")
        if not decision.blockers:
            lines.append(f"  {paint(tick, 'green')} ownership and identity verified")
    return "\n".join(lines)
