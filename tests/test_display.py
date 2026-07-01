"""Tests for the shared rendering layer (0.7.0 output unification)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from ephemdir import _display
from ephemdir._policy import CleanupDecision


def _decision(**overrides):
    base = {
        "path": Path("/tmp/brave-otter"),
        "due": False,
        "status": "active",
        "reasons": (),
        "blockers": (),
        "destructive_allowed": False,
        "measured_size_bytes": None,
        "max_size_bytes": None,
    }
    base.update(overrides)
    return CleanupDecision(**base)


def test_escape_human_text_covers_unicode_controls():
    # ASCII C0 / DEL
    assert _display.escape_human_text("a\nb\x7f") == "a\\x0ab\\x7f"
    # C1 control (category Cc, 0x80-0x9F)
    assert _display.escape_human_text("x" + chr(0x9B) + "y") == "x\\x9by"
    # Unicode format char (category Cf): RIGHT-TO-LEFT OVERRIDE
    assert _display.escape_human_text("a" + chr(0x202E) + "b") == "a\\u202eb"
    # Surrogateescape raw byte (category Cs) renders as the original byte (RC6-1)
    assert _display.escape_human_text("a" + chr(0xDC9B) + "b") == "a\\x9bb"
    assert _display.escape_human_text("a" + chr(0xDC80) + "b") == "a\\x80b"
    # ordinary text and non-ASCII letters are left intact
    assert _display.escape_human_text("brave-otter-é") == "brave-otter-é"


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1048576, "1.0 MiB"),
        (1073741824, "1.0 GiB"),
        (None, "unknown"),
        ("nope", "unknown"),
    ],
)
def test_format_size(value, expected):
    assert _display.format_size(value) == expected


def test_color_enabled_modes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    tty = io.StringIO()
    tty.isatty = lambda: True  # type: ignore[method-assign]
    pipe = io.StringIO()  # isatty() is False

    assert _display.color_enabled(pipe, "always") is True
    assert _display.color_enabled(tty, "never") is False
    assert _display.color_enabled(tty, "auto") is True
    assert _display.color_enabled(pipe, "auto") is False

    monkeypatch.setenv("NO_COLOR", "1")
    assert _display.color_enabled(tty, "auto") is False
    assert _display.color_enabled(tty, "always") is True  # explicit beats NO_COLOR


def test_painter_wraps_only_when_enabled():
    on = _display.Painter(True)
    off = _display.Painter(False)
    assert off("hi", "red") == "hi"
    painted = on("hi", "red")
    assert painted.startswith("\x1b[") and painted.endswith("\x1b[0m") and "hi" in painted
    assert on("hi") == "hi"  # no styles → unchanged


def test_explain_payload_lists_reasons_and_blockers():
    decision = _decision(
        due=True,
        status="blocked",
        reasons=("expired",),
        blockers=("in-use",),
        destructive_allowed=False,
    )
    payload = _display.explain_payload(decision, {"expires_at": 100.0}, now=160.0)
    assert payload["name"] == "brave-otter"
    assert payload["due"] is True
    assert payload["destructive_allowed"] is False
    assert payload["remaining_seconds"] == -60
    assert payload["reasons"] == ["expired"]
    assert payload["blocked_by"] == ["in-use"]
    checks = {c["check"] for c in payload["decision"]}
    assert {"expired", "in-use"} <= checks


def test_explain_trace_human_states():
    # not due, no limit → kept message + verified line
    trace = _display.explain_trace(_decision(), {"expires_at": None}, now=0.0, ascii_only=True)
    assert "kept (no time limit)" in trace
    assert "ownership and identity verified" in trace

    # due and allowed
    due = _decision(due=True, status="due", reasons=("expired",), destructive_allowed=True)
    trace = _display.explain_trace(due, {"expires_at": 1.0}, now=2.0, ascii_only=True)
    assert "will be removed on the next sweep" in trace
    assert "due because" in trace and "expired" in trace

    # due but blocked
    blocked = _decision(due=True, status="blocked", reasons=("expired",), blockers=("in-use",))
    trace = _display.explain_trace(blocked, {"expires_at": 1.0}, now=2.0, ascii_only=True)
    assert "held back because" in trace and "in-use" in trace


def test_format_decision_stable_for_scripts():
    decision = _decision(due=True, status="due", reasons=("next-sweep",))
    rendered = _display.format_decision(decision)
    assert "reasons=next-sweep" in rendered
    assert "due=True" in rendered
