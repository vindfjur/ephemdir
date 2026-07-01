"""Lifetime usage counters / stats ledger (0.7.0 feature tier)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ephemdir import keep, remove, sweep, tempdir
from ephemdir._stats import COUNTERS, StatsLedger
from ephemdir.cli import main


def _zero():
    return dict.fromkeys(COUNTERS, 0)


def test_snapshot_is_zero_without_a_ledger(tmp_path):
    assert StatsLedger(path=tmp_path / "stats.json").snapshot() == _zero()


def test_record_increments_and_persists(tmp_path):
    path = tmp_path / "stats.json"
    led = StatsLedger(path=path)
    led.record(created=2)
    led.record(created=1, swept=3)
    assert StatsLedger(path=path).snapshot() == {
        "created": 3,
        "swept": 3,
        "kept": 0,
        "removed": 0,
    }


def test_ledger_tolerates_corruption(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert StatsLedger(path=path).snapshot() == _zero()
    StatsLedger(path=path).record(created=5)  # overwrites the corrupt file
    assert StatsLedger(path=path).snapshot()["created"] == 5


def test_record_is_best_effort_and_never_raises(tmp_path):
    # Parent directory missing: record must swallow the error, write nothing.
    led = StatsLedger(path=tmp_path / "missing" / "stats.json")
    led.record(created=1)
    assert led.snapshot() == _zero()


def test_counters_track_the_lifecycle(tmp_path):
    a = tempdir(parent=tmp_path)
    b = tempdir(parent=tmp_path)
    tempdir(parent=tmp_path)  # c, will be swept
    keep(str(a.path))
    remove(str(b.path))
    assert sweep(force=True) == 1
    snapshot = StatsLedger().snapshot()
    assert snapshot == {"created": 3, "swept": 1, "kept": 1, "removed": 1}


def test_stats_command_json(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] >= 1
    assert payload["currently_tracked"] >= 1


def test_stats_command_human(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "created" in out
    assert "currently tracked" in out


def test_stats_is_read_only_no_data_dir(capsys):
    data_dir = Path(os.environ["EPHEMDIR_DATA_DIR"])
    assert not data_dir.exists()
    assert main(["stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] == 0
    assert not data_dir.exists()
