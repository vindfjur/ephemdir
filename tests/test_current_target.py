"""Tests for current-directory target resolution (0.6.0 Tier 1)."""

from __future__ import annotations

import os
import time

import pytest

from ephemdir.cli import main
from ephemdir.core import (
    _MARKER_NAME,
    _CurrentTargetMismatch,
    _CurrentTargetNotFound,
    _resolve_current_target,
    registered,
    tempdir,
)

# --- Core resolver: detection ------------------------------------------------


def test_current_target_detects_root_marker(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    path, entry = _resolve_current_target(registry=registry, start=d.path)
    assert path == d.path
    assert entry["marker_id"] == registered(registry=registry)[str(d.path)]["marker_id"]


def test_current_target_detects_from_child_directory(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    child = d.path / "a" / "b"
    child.mkdir(parents=True)
    path, _ = _resolve_current_target(registry=registry, start=child)
    assert path == d.path  # the managed root, not the child


def test_current_target_uses_nearest_nested_marker(tmp_path, registry):
    outer = tempdir(parent=tmp_path, registry=registry)
    inner = tempdir(parent=outer.path, registry=registry)
    start = inner.path / "sub"
    start.mkdir()
    path, _ = _resolve_current_target(registry=registry, start=start)
    assert path == inner.path  # nearest marker wins over the outer one


# --- Core resolver: fail-closed rejections -----------------------------------


def test_current_target_rejects_invalid_marker(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    (d.path / _MARKER_NAME).write_text("not-32-hex-garbage", encoding="utf-8")
    with pytest.raises(_CurrentTargetMismatch):
        _resolve_current_target(registry=registry, start=d.path)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_current_target_rejects_marker_symlink(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    marker = d.path / _MARKER_NAME
    marker.unlink()
    marker.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(_CurrentTargetMismatch):
        _resolve_current_target(registry=registry, start=d.path)


def test_current_target_rejects_registry_marker_mismatch(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    # A different but well-formed marker no longer matches the registry id.
    (d.path / _MARKER_NAME).write_text("0" * 32, encoding="utf-8")
    with pytest.raises(_CurrentTargetMismatch):
        _resolve_current_target(registry=registry, start=d.path)


def test_current_target_rejects_replaced_directory(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    import shutil

    shutil.rmtree(d.path)
    d.path.mkdir()
    (d.path / _MARKER_NAME).write_text("0" * 32, encoding="utf-8")  # foreign marker
    with pytest.raises(_CurrentTargetMismatch):
        _resolve_current_target(registry=registry, start=d.path)


def test_current_target_broken_inner_marker_does_not_reach_outer(tmp_path, registry):
    # Nearest-invalid-marker stops the search: a broken inner marker must fail
    # closed, never fall through to a valid outer marker.
    outer = tempdir(parent=tmp_path, registry=registry)
    inner = tempdir(parent=outer.path, registry=registry)
    (inner.path / _MARKER_NAME).write_text("garbage", encoding="utf-8")
    start = inner.path / "sub"
    start.mkdir()
    with pytest.raises(_CurrentTargetMismatch):
        _resolve_current_target(registry=registry, start=start)


def test_current_target_not_found_outside_any_ephemdir(tmp_path, registry):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(_CurrentTargetNotFound):
        _resolve_current_target(registry=registry, start=plain)


def test_failed_resolution_does_not_mutate_registry(tmp_path, registry):
    d = tempdir(parent=tmp_path, registry=registry)
    (d.path / _MARKER_NAME).write_text("not-hex", encoding="utf-8")
    before = registered(registry=registry)
    with pytest.raises(_CurrentTargetMismatch):
        _resolve_current_target(registry=registry, start=d.path)
    assert registered(registry=registry) == before


# --- CLI: no-target commands act on the current directory --------------------


def test_keep_without_target_uses_current_directory(tmp_path, monkeypatch, capsys):
    d = tempdir(parent=tmp_path)
    monkeypatch.chdir(d.path)
    assert main(["keep"]) == 0
    assert d.path.is_dir()  # kept on disk
    assert str(d.path) not in registered()  # no longer tracked
    assert not (d.path / _MARKER_NAME).exists()  # our marker removed


def test_rm_without_target_uses_current_directory_root(tmp_path, monkeypatch):
    d = tempdir(parent=tmp_path)
    child = d.path / "a" / "b"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    assert main(["rm"]) == 0
    assert not d.path.exists()  # the managed root was removed, not just the child
    assert str(d.path) not in registered()


def test_explain_without_target_uses_current_directory(tmp_path, monkeypatch, capsys):
    d = tempdir(parent=tmp_path)
    monkeypatch.chdir(d.path)
    assert main(["explain"]) == 0
    assert str(d.path) in capsys.readouterr().out


def test_extend_without_target_uses_current_directory(tmp_path, monkeypatch):
    d = tempdir(parent=tmp_path, lifetime="1h")
    monkeypatch.chdir(d.path)
    assert main(["extend", "30m"]) == 0
    entry = registered()[str(d.path)]
    expires = entry["expires_at"]
    assert expires is not None
    assert abs(float(expires) - (time.time() + 30 * 60)) < 30


def test_extend_without_target_forever(tmp_path, monkeypatch):
    d = tempdir(parent=tmp_path, lifetime="1h")
    monkeypatch.chdir(d.path)
    assert main(["extend", "--forever"]) == 0
    assert registered()[str(d.path)]["expires_at"] is None


def test_extend_forever_with_lifetime_is_usage_error(tmp_path, monkeypatch, capsys):
    d = tempdir(parent=tmp_path, lifetime="1h")
    monkeypatch.chdir(d.path)
    # `extend --forever 30m` must not silently treat 30m as a target name.
    assert main(["extend", "--forever", "30m"]) == 2
    assert "cannot be combined with a lifetime" in capsys.readouterr().err
    assert registered()[str(d.path)]["expires_at"] is not None  # unchanged


def test_extend_with_explicit_target_still_works(tmp_path, monkeypatch):
    d = tempdir(parent=tmp_path, lifetime="1h")
    # Run from elsewhere; the explicit name must still resolve.
    monkeypatch.chdir(tmp_path)
    assert main(["extend", d.path.name, "45m"]) == 0
    entry = registered()[str(d.path)]
    assert abs(float(entry["expires_at"]) - (time.time() + 45 * 60)) < 30


# --- CLI: outside context, fail closed without side effects ------------------


def test_extend_without_target_outside_context_fails(tmp_path, monkeypatch, capsys):
    tempdir(parent=tmp_path, lifetime="1h")  # exists but we are not inside it
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    before = registered()
    assert main(["extend", "30m"]) == 1
    assert registered() == before  # no mutation
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["keep"], ["rm"], ["extend", "30m"]])
def test_no_target_destructive_commands_do_not_fallback_to_latest(
    tmp_path, monkeypatch, argv
):
    survivor = tempdir(parent=tmp_path, lifetime="1h")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert main(argv) == 1
    assert survivor.path.is_dir()  # latest was never touched
    assert str(survivor.path) in registered()


def test_no_target_error_has_no_traceback(tmp_path, monkeypatch, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert main(["rm"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "not inside a tracked ephemdir directory" in err


# --- CLI: path command priority ---------------------------------------------


def test_path_prefers_current_context_over_latest(tmp_path, monkeypatch, capsys):
    first = tempdir(parent=tmp_path)
    time.sleep(0.01)
    latest = tempdir(parent=tmp_path)  # most recently created
    monkeypatch.chdir(first.path)  # but cwd is inside the first
    assert main(["path"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(first.path)
    assert out != str(latest.path)


def test_path_outside_context_preserves_latest_fallback(tmp_path, monkeypatch, capsys):
    tempdir(parent=tmp_path)
    time.sleep(0.01)
    latest = tempdir(parent=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(latest.path)


def test_path_inside_invalid_marker_fails_instead_of_latest_fallback(
    tmp_path, monkeypatch, capsys
):
    tempdir(parent=tmp_path)  # a valid 'latest' that must NOT be returned
    broken = tempdir(parent=tmp_path)
    (broken.path / _MARKER_NAME).write_text("garbage", encoding="utf-8")
    monkeypatch.chdir(broken.path)
    assert main(["path"]) == 1  # fail closed, no fallback
    out = capsys.readouterr()
    assert out.out.strip() == ""
    assert "refusing to guess" in out.err
