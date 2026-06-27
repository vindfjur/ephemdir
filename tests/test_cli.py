"""Tests for the command-line interface (run through ``main`` end to end).

The autouse fixture in conftest.py points EPHEMDIR_DATA_DIR / _CONFIG_DIR at
fresh temp locations, so these tests exercise the default registry safely.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from ephemdir._doctor import DoctorCheck
from ephemdir._menu import run_menu
from ephemdir._registry import (
    Registry,
    RegistryFormatError,
    RegistryUnavailableError,
    UnsafeRegistryError,
)
from ephemdir._service import ServiceError
from ephemdir.cli import (
    _detect_shell,
    _format_duration,
    _format_timestamp,
    _status_note,
    _supports_emoji,
    main,
)


def _create(capsys, tmp_path, *args):
    """Run ``ephemdir new`` and return the created path printed to stdout."""
    assert main(["new", "-p", str(tmp_path), *args]) == 0
    return capsys.readouterr().out.strip()


def test_new_prints_created_path(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert Path(created).is_dir()
    assert Path(created).parent == tmp_path


def test_new_forwards_optional_flags(capsys, tmp_path):
    created = Path(
        _create(
            capsys,
            tmp_path,
            "--lifetime",
            "1h",
            "--prefix",
            "qa",
            "--words",
            "3",
            "--keep-on-restart",
            "--keep-while-in-use",
            "--cleanup",
            "auto",
            "--max-size",
            "1KiB",
            "--name-style",
            "secure",
        )
    )

    entry = Registry().load()[str(created)]
    assert created.name.startswith("qa")
    assert entry["remove_on_restart"] is False
    assert entry["keep_while_in_use"] is True
    assert entry["cleanup_policy"] == "auto"
    assert entry["max_size"] == 1024


def test_new_reports_user_input_error_without_traceback(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path), "--words", "0"]) == 2
    result = capsys.readouterr()
    assert "Traceback" not in result.err


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
    assert "[miss]" in out
    assert "still tracked" in out


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


def test_list_empty_plain_warns(capsys):
    assert main(["list", "--plain"]) == 0
    assert "no tracked directories" in capsys.readouterr().err


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


def test_path_default_fails_without_live_directories(capsys):
    assert main(["path"]) == 1
    assert "no tracked directories" in capsys.readouterr().err


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


def test_keep_extend_rm_unknown_targets_fail(capsys):
    assert main(["keep", "missing"]) == 1
    assert main(["extend", "missing", "1h"]) == 1
    assert main(["rm", "missing"]) == 1
    assert "no tracked directory" in capsys.readouterr().err


def test_extend_requires_lifetime_or_forever(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert main(["extend", Path(created).name]) == 2


def test_extend_rejects_lifetime_and_forever(capsys, tmp_path):
    created = _create(capsys, tmp_path)
    assert main(["extend", Path(created).name, "1h", "--forever"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


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
    assert f"forgot missing tracked directory: {created}" in capsys.readouterr().err
    assert main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_new_until_sweep_and_dry_run(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--until-sweep")
    assert main(["sweep", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert created in out
    assert "next-sweep" in out
    assert Path(created).is_dir()


def test_sweep_removes_expired_directory(capsys, tmp_path):
    created = Path(_create(capsys, tmp_path, "--lifetime", "1h"))
    with Registry().transaction() as state:
        state[str(created)]["expires_at"] = 0.0

    assert main(["sweep"]) == 0

    assert not created.exists()
    assert "swept 1 directory" in capsys.readouterr().err


def test_dry_run_does_not_create_data_dir(capsys):
    data_dir = Path(os.environ["EPHEMDIR_DATA_DIR"])
    assert not data_dir.exists()


def test_read_only_commands_do_not_mutate_registry_or_quarantine(capsys, tmp_path):
    created = Path(_create(capsys, tmp_path, "--until-sweep"))
    registry = Registry()
    before = registry.path.read_bytes()

    for command in (
        ["list"],
        ["list", "--json"],
        ["explain", created.name],
        ["sweep", "--dry-run"],
        ["doctor"],
        ["completion", "show", "zsh"],
    ):
        assert main(command) in {0, 1}
        capsys.readouterr()
        assert registry.path.read_bytes() == before
        assert list(registry.path.parent.glob("registry.json.corrupt-*")) == []


def test_read_only_commands_do_not_create_empty_registry_or_state_files(capsys):
    data_dir = Path(os.environ["EPHEMDIR_DATA_DIR"])
    assert not data_dir.exists()

    for command in (
        ["list"],
        ["list", "--json"],
        ["sweep", "--dry-run"],
        ["doctor"],
        ["completion", "show", "zsh"],
    ):
        assert main(command) in {0, 1}
        capsys.readouterr()
        assert not data_dir.exists()

    assert main(["sweep", "--dry-run"]) == 0

    assert not data_dir.exists()


def test_sweep_rejects_abbreviated_force_without_side_effects(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--lifetime", "100h")

    with pytest.raises(SystemExit) as exc:
        main(["sweep", "--f"])

    assert exc.value.code == 2
    assert Path(created).is_dir()


def test_explain_reports_reasons(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--until-sweep")
    assert main(["explain", Path(created).name]) == 0
    out = capsys.readouterr().out
    assert "reasons=next-sweep" in out


def test_explain_unknown_target_fails(capsys):
    assert main(["explain", "missing"]) == 1
    assert "no tracked directory" in capsys.readouterr().err


def test_completion_install(capsys):
    assert main(["completion", "install", "fish"]) == 0
    assert "complete -c ephemdir" in capsys.readouterr().out


def test_completion_show_alias(capsys):
    assert main(["completion", "show", "bash"]) == 0
    assert "_ephemdir_complete" in capsys.readouterr().out


def test_completion_install_help_is_explicit(capsys):
    with pytest.raises(SystemExit):
        main(["completion", "install", "--help"])
    assert "does not modify shell startup files" in capsys.readouterr().out


def test_completion_requires_subcommand():
    with pytest.raises(SystemExit):
        main(["completion"])


def test_menu_confirms_before_sweep():
    calls = []
    answers = iter(["3", "n", "q"])

    result = run_menu(
        lambda argv: calls.append(argv) or 0,
        input_func=lambda prompt: next(answers),
        print_func=lambda *args, **kwargs: None,
    )

    assert result == 0
    assert calls == [["sweep", "--dry-run"]]


def test_doctor_json(capsys):
    assert main(["doctor", "--json"]) in {0, 1}
    payload = json.loads(capsys.readouterr().out)
    assert any(item["name"] == "registry" for item in payload)
    assert any(item["name"] == "registry-path" for item in payload)
    assert any(item["name"] == "service-env" for item in payload)


def test_doctor_text_includes_hints(capsys, monkeypatch):
    monkeypatch.setattr(
        "ephemdir.cli.run_doctor",
        lambda: [DoctorCheck("registry", False, "bad registry", "inspect it")],
    )

    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "fail registry" in out
    assert "hint: inspect it" in out


def test_future_schema_is_cli_error_without_traceback(capsys):
    registry = Registry()
    registry.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry.path.write_text('{"schema_version": 999, "entries": {}}', encoding="utf-8")
    registry.path.chmod(0o600)

    assert main(["list"]) == 1
    result = capsys.readouterr()
    assert "newer than this ephemdir supports" in result.err
    assert "Traceback" not in result.err


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


def test_recover_retry_reconciles_staging_entry(capsys, tmp_path):
    created = Path(_create(capsys, tmp_path))
    staging = created.parent / f".{created.name}.123-abcdef12.deleting"
    os.rename(created, staging)
    registry = Registry()
    with registry.transaction() as state:
        state[str(created)].update(
            {"state": "recovery", "claim_id": None, "staging_path": str(staging)}
        )

    assert main(["recover", created.name]) == 0

    result = capsys.readouterr()
    assert "reconciled recovery entry" in result.err
    assert not created.exists()
    assert not staging.exists()
    assert str(created) not in Registry().load()


def test_recover_unknown_target_fails(capsys):
    assert main(["recover", "missing"]) == 1
    assert "no tracked directory" in capsys.readouterr().err


def test_watch_rejects_nonpositive_interval(capsys):
    assert main(["watch", "--interval", "0"]) == 2
    assert "must be >= 1" in capsys.readouterr().err


def test_watch_stops_on_keyboard_interrupt(capsys, monkeypatch):
    calls = []

    def interrupting_sleep(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("ephemdir.cli.sweep", lambda: calls.append("sweep") or 0)
    monkeypatch.setattr("ephemdir.cli.time.sleep", interrupting_sleep)

    assert main(["watch", "--interval", "1"]) == 0
    assert calls == ["sweep"]
    assert "stopped" in capsys.readouterr().err


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


def test_shell_init_autodetects_known_and_default_shell(capsys, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/fish")
    assert _detect_shell() == "fish"
    assert main(["shell-init"]) == 0
    assert "function ecd" in capsys.readouterr().out

    monkeypatch.setenv("SHELL", "/bin/unknown")
    assert _detect_shell() == "bash"

    monkeypatch.setattr("ephemdir.cli.sys.platform", "win32")
    assert _detect_shell() == "powershell"


def test_install_and_uninstall_service_cli(capsys, monkeypatch):
    monkeypatch.setattr(
        "ephemdir.cli.install_service",
        lambda interval, runtime_policy: f"installed every {interval}s ({runtime_policy})",
    )
    assert main(["install-service", "--interval", "7", "--runtime-policy", "strict"]) == 0
    assert "installed every 7s (strict)" in capsys.readouterr().err

    monkeypatch.setattr("ephemdir.cli.uninstall_service", lambda: "removed")
    assert main(["uninstall-service"]) == 0
    assert "removed" in capsys.readouterr().err


def test_service_cli_reports_errors(capsys, monkeypatch):
    def install_failure(interval, runtime_policy):
        raise ValueError("bad interval")

    def uninstall_failure():
        raise ServiceError("scheduler denied")

    monkeypatch.setattr("ephemdir.cli.install_service", install_failure)
    assert main(["install-service"]) == 1
    assert "bad interval" in capsys.readouterr().err

    monkeypatch.setattr("ephemdir.cli.uninstall_service", uninstall_failure)
    assert main(["uninstall-service"]) == 1
    assert "scheduler denied" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("locked"),
        UnsafeRegistryError("unsafe"),
        RegistryFormatError("future schema"),
        RegistryUnavailableError("registry unavailable"),
    ],
)
def test_main_reports_registry_errors_without_traceback(capsys, monkeypatch, error):
    def fail():
        raise error

    monkeypatch.setattr("ephemdir.cli.registered", fail)

    assert main(["list"]) == 1
    result = capsys.readouterr()
    assert str(error) in result.err
    assert "Traceback" not in result.err


def test_main_reports_permission_errors_without_traceback(capsys, monkeypatch):
    monkeypatch.setattr(
        "ephemdir.cli.registered",
        lambda: (_ for _ in ()).throw(PermissionError("/var is a symlink")),
    )

    assert main(["list"]) == 1
    result = capsys.readouterr()
    assert "/var is a symlink" in result.err
    assert "Traceback" not in result.err


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


def test_format_timestamp_status_notes_and_emoji_support(monkeypatch):
    class Stream:
        def __init__(self, encoding):
            self.encoding = encoding

    monkeypatch.setattr("ephemdir.cli.sys.platform", "linux")

    assert _format_timestamp("not a timestamp") == "never"
    assert "1970" in _format_timestamp(0)
    assert _supports_emoji(Stream("utf-8")) is True
    assert _supports_emoji(Stream("ascii")) is False
    assert _supports_emoji(Stream("not-a-codec")) is False

    now = 100.0
    assert _status_note("legacy", {}, now).startswith("from an older")
    assert _status_note("missing", {}, now) == "missing; still tracked"
    assert _status_note("deleting", {}, now).startswith("partially")
    assert _status_note("recovery", {}, now).startswith("interrupted")
    assert _status_note("unavailable", {}, now).startswith("temporarily")
    assert _status_note("blocked", {"backend": "windows", "platform": "win32"}, now) == (
        "unsupported backend; foreign platform"
    )
    assert _status_note("blocked", {}, now) == "blocked"
    assert _status_note("kept", {}, now) == "no auto-cleanup"
    assert _status_note("until-restart", {}, now) == "until restart"
    assert _status_note("until-sweep", {}, now) == "until next full sweep"
    assert _status_note("expired", {"expires_at": 90.0}, now) == "expired 10s ago"
    assert _status_note("expired", {}, now) == "due now (machine restarted)"
    assert _status_note("active", {"expires_at": 130.0}, now) == "30s left"
    assert _status_note("active", {}, now) == "tracked"
