"""Tests for the command-line interface (run through ``main`` end to end).

The autouse fixture in conftest.py points EPHEMDIR_DATA_DIR / _CONFIG_DIR at
fresh temp locations, so these tests exercise the default registry safely.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ephemdir._registry import Registry
from ephemdir.cli import _format_duration, main


def _create(capsys, tmp_path, *args):
    """Run ``ephemdir new`` and return the created path printed to stdout."""
    assert main(["new", "-p", str(tmp_path), *args]) == 0
    return capsys.readouterr().out.strip()


def test_new_prints_created_path(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert Path(created).is_dir()
    assert Path(created).parent == tmp_path


def test_list_shows_status_and_remaining(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--lifetime", "2h")
    assert main(["list", "--plain"]) == 0
    out = capsys.readouterr().out
    assert Path(created).name in out
    assert "[ok]" in out
    assert "left" in out


def test_list_marks_manually_deleted(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    shutil.rmtree(created)
    assert main(["list", "--plain"]) == 0
    out = capsys.readouterr().out
    assert "[gone]" in out
    assert "deleted manually" in out


def test_list_marks_replaced_directory(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    shutil.rmtree(created)
    Path(created).mkdir()  # another directory took the path
    assert main(["list", "--plain"]) == 0
    out = capsys.readouterr().out
    assert "[warn]" in out
    assert "will not be touched" in out


def test_list_json(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--lifetime", "1h")
    assert main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = next(item for item in payload if item["path"] == created)
    assert entry["status"] == "active"
    assert 0 < entry["remaining_seconds"] <= 3600
    assert entry["remove_on_restart"] is True


def test_list_json_empty(capsys):
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_path_resolves_by_name(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert main(["path", Path(created).name]) == 0
    assert capsys.readouterr().out.strip() == created


def test_path_defaults_to_latest(capsys, tmp_path):
    _create(capsys, tmp_path)
    latest = _create(capsys, tmp_path)
    assert main(["path"]) == 0
    assert capsys.readouterr().out.strip() == latest


def test_path_unknown_target_fails(capsys):
    assert main(["path", "no-such-dir"]) == 1
    assert "no tracked directory" in capsys.readouterr().err


def test_keep_untracks_but_keeps_on_disk(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert main(["keep", Path(created).name]) == 0
    assert capsys.readouterr().out.strip() == created
    assert Path(created).is_dir()
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_rm_removes_now(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert main(["rm", Path(created).name]) == 0
    assert not Path(created).exists()


def test_extend_requires_lifetime_or_forever(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert main(["extend", Path(created).name]) == 2


def test_extend_sets_lifetime(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--lifetime", "1s")
    assert main(["extend", Path(created).name, "2h"]) == 0
    assert main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = next(item for item in payload if item["path"] == created)
    assert entry["remaining_seconds"] > 3600


def test_extend_forever_removes_limit(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--lifetime", "1s")
    assert main(["extend", Path(created).name, "--forever"]) == 0
    assert main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    entry = next(item for item in payload if item["path"] == created)
    assert entry["expires_at"] is None
    assert entry["status"] == "until-restart"


def test_prune_forgets_deleted_directories(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    shutil.rmtree(created)
    assert main(["prune"]) == 0
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_recover_forget_untracks_without_touching_paths(capsys, tmp_path):
    created = Path(_create(capsys, tmp_path))
    staging = created.parent / f".{created.name}.123-abcdef12.deleting"
    staging.mkdir()
    (staging / "mystery.txt").write_text("keep")
    registry = Registry()
    with registry.transaction() as state:
        state[str(created)].update(
            {"state": "recovery", "claim_id": None, "staging_path": str(staging)}
        )

    assert main(["recover", created.name, "--forget"]) == 0
    result = capsys.readouterr()
    assert str(created) in result.err
    assert "no files were deleted" in result.err
    assert created.is_dir()
    assert (staging / "mystery.txt").read_text() == "keep"
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_watch_rejects_nonpositive_interval(capsys):
    assert main(["watch", "--interval", "0"]) == 2
    assert "must be >= 1" in capsys.readouterr().err


def test_shell_init_posix(capsys):
    assert main(["shell-init", "zsh"]) == 0
    out = capsys.readouterr().out
    assert "ecd()" in out
    assert "enew()" in out
    assert "ephemdir path" in out


def test_shell_init_powershell(capsys):
    assert main(["shell-init", "powershell"]) == 0
    out = capsys.readouterr().out
    assert "function ecd" in out
    assert "Set-Location" in out


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (42, "42s"),
        (5 * 60 + 12, "5m 12s"),
        (3600 + 23 * 60, "1h 23m"),
        (86400 + 4 * 3600, "1d 4h"),
        (86400 + 30, "1d 30s"),
    ],
)
def test_format_duration(seconds, expected):
    assert _format_duration(seconds) == expected
