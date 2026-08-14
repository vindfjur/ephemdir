from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ephemdir import _completion, _inuse, _service, cli, core
from ephemdir._policy import CleanupDecision
from ephemdir._registry import Registry


def test_lsof_failed_scan_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_inuse, "resolve_system_executable", lambda name: "/usr/sbin/lsof")
    monkeypatch.setattr(
        _inuse.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 2, stdout="", stderr="permission denied"
        ),
    )
    assert _inuse._lsof_in_use("/tmp/example") is None


def test_relative_xdg_completion_roots_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "relative-data")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert _completion._completion_target("bash").is_absolute()
    assert _completion._completion_target("fish").is_absolute()


def test_list_json_probes_original_path_for_journal_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    staging = tmp_path / ".missing.1-deadbeef.deleting"
    entry = {
        "state": "deleting",
        "created_at": 1.0,
        "expires_at": None,
        "remove_on_restart": False,
        "staging_path": str(staging),
    }
    monkeypatch.setattr(cli, "registered", lambda: {str(missing): entry})
    args = argparse.Namespace(tag=None, json=True, plain=True)
    assert cli._cmd_list(args) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["status"] == "deleting"
    assert row["exists"] is False
    assert row["original_state"] == "missing"


def test_explain_cli_uses_one_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path("/tmp/example")
    now = time.time()
    entry = {"expires_at": now - 1, "remove_on_restart": False}
    decision = CleanupDecision(path, True, "due", ("expired",), (), True, None, None)
    monkeypatch.setattr(cli, "_explain_with_entry", lambda target: (decision, entry, now))
    monkeypatch.setattr(cli, "registered", lambda: pytest.fail("second registry load"))
    args = argparse.Namespace(target=str(path), json=True, color="never")
    assert cli._cmd_explain(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["due"] is True
    assert payload["remaining_seconds"] == -1


def test_extend_refuses_replaced_directory(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    registry = Registry(state / "registry.json")
    directory = core.tempdir(
        parent=parent,
        remove_on_restart=False,
        lifetime="1h",
        registry=registry,
    )
    shutil.rmtree(directory.path)
    directory.path.mkdir(mode=0o700)
    with pytest.raises(OSError, match="not the directory ephemdir created"):
        core.extend(directory.path, "2h", registry=registry)


def test_extend_names_a_vanished_directory_accurately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AUD-005 also fires for a deleted path, where "replaced" would be a lie.

    A directory that is already gone is rejected earlier, by ``_resolve_active``
    with its own message. The new guard sees a missing path only when the
    directory disappears between that resolve and the transaction — simulated
    here — and the ownership probe answers "foreign" for a vanished path too,
    so the refusal must not accuse it of being someone else's directory.
    """
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    registry = Registry(state / "registry.json")
    directory = core.tempdir(
        parent=parent,
        remove_on_restart=False,
        lifetime="1h",
        registry=registry,
    )
    snapshot = dict(registry.load(read_only=True)[str(directory.path)])
    shutil.rmtree(directory.path)
    monkeypatch.setattr(
        core,
        "_resolve_active",
        lambda target, registry=None: (directory.path, snapshot),
    )
    with pytest.raises(OSError, match="no longer exists"):
        core.extend(directory.path, "2h", registry=registry)


def test_cli_catches_generic_oserror(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Parser:
        def parse_args(self, argv: object) -> argparse.Namespace:
            def fail(args: argparse.Namespace) -> int:
                raise OSError("simulated I/O failure")

            return argparse.Namespace(verbose=0, quiet=False, command="audit", func=fail)

    monkeypatch.setattr(cli, "build_parser", Parser)
    assert cli.main([]) == 1
    assert "simulated I/O failure" in capsys.readouterr().err


def test_systemd_uninstall_requires_successful_state_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_service, "_systemd_dir", lambda: tmp_path)
    monkeypatch.setattr(os.path, "lexists", lambda path: True)
    monkeypatch.setattr(_service, "_resolve_scheduler", lambda name: "/usr/bin/systemctl")

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if "show" in command:
            return subprocess.CompletedProcess(
                command, 1, stdout="inactive\n", stderr="access denied\n"
            )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed\n")

    removed: list[Path] = []
    monkeypatch.setattr(_service, "_run", fake_run)
    monkeypatch.setattr(_service, "_remove_recorded_installed_file", removed.append)
    with pytest.raises(_service.ServiceError, match="access denied"):
        _service._uninstall_systemd()
    assert removed == []
