"""rc7 ops-contract tests: verified uninstall, completion contract, service status."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from ephemdir import _service
from ephemdir import core as core_module
from ephemdir._completion import completion_status, install_completion, uninstall_completion
from ephemdir._service import ServiceError
from ephemdir._stats import COUNTERS, StatsLedger
from ephemdir.cli import main

# --- verified removal edge branches -----------------------------------------

def test_remove_verified_refuses_fifo(tmp_path):
    target = tmp_path / "fifo"
    os.mkfifo(target)
    with pytest.raises(ServiceError, match="not a regular file"):
        _service._remove_verified_installed_file(target, "0" * 64)
    assert target.exists()


def test_remove_verified_missing_file(tmp_path):
    with pytest.raises(ServiceError, match="disappeared"):
        _service._remove_verified_installed_file(tmp_path / "gone", "0" * 64)


def test_remove_verified_refuses_oversized(tmp_path, monkeypatch):
    target = tmp_path / "f"
    target.write_text("abcdef", encoding="utf-8")
    monkeypatch.setattr(_service, "_INSTALLED_FILE_MAX_BYTES", 3)
    with pytest.raises(ServiceError, match="unexpectedly large"):
        _service._remove_verified_installed_file(target, "0" * 64)
    assert target.is_file()


def test_uninstall_launchd_refuses_unrecorded_plist(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    plist.write_text("hand-made", encoding="utf-8")
    monkeypatch.setattr(_service, "_launchd_path", lambda: plist)
    monkeypatch.setattr(
        _service,
        "_run_scheduler",
        lambda name, args: type("C", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    with pytest.raises(ServiceError, match="not recorded"):
        _service._uninstall_launchd()
    assert plist.is_file()


def test_installed_state_unreadable_and_foreign(tmp_path, monkeypatch):
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    if os.geteuid() != 0:
        os.chmod(target, 0)
        assert _service._installed_file_state(target, None) == "unreadable"
        os.chmod(target, 0o600)
    monkeypatch.setattr(_service.os, "geteuid", lambda: 1 << 30)
    assert _service._installed_file_state(target, None) == "foreign"


# --- manifest and pinned-environment parsing --------------------------------

def test_manifest_ignored_when_oversized_or_foreign(tmp_path, monkeypatch):
    manifest = tmp_path / "installed-files.json"
    monkeypatch.setattr(_service, "_manifest_path", lambda: manifest)
    manifest.write_text(json.dumps({"/tmp/x": "a" * 64}), encoding="utf-8")
    monkeypatch.setattr(_service, "_MANIFEST_MAX_BYTES", 4)
    assert _service._load_manifest() == {}


def test_pinned_environment_handles_garbage(tmp_path):
    plist = tmp_path / "x.plist"
    plist.write_text("not a plist", encoding="utf-8")
    assert _service._pinned_service_environment(plist) == {}
    unit = tmp_path / "x.service"
    unit.write_text(
        'Environment="unterminated\nExecStart=/bin/true\nEnvironment="EPHEMDIR_B=2"\n',
        encoding="utf-8",
    )
    assert _service._pinned_service_environment(unit) == {"EPHEMDIR_B": "2"}
    assert _service._pinned_service_environment(tmp_path / "absent.service") == {}


# --- service environment / scheduler resolution refusals --------------------

def test_effective_service_environment_refuses_volatile_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(_service, "user_data_dir", lambda create=False: tmp_path / "data")
    monkeypatch.setattr(_service, "user_config_dir", lambda create=False: tmp_path / "config")
    monkeypatch.setattr(
        _service, "_volatile_service_state_reason", lambda path: "under volatile root /x"
    )
    with pytest.raises(ServiceError, match="not reboot-persistent"):
        _service._effective_service_environment()


def test_effective_service_environment_refuses_broken_registry(tmp_path, monkeypatch):
    from ephemdir import _registry

    monkeypatch.setattr(_service, "user_data_dir", lambda create=False: tmp_path / "data")
    monkeypatch.setattr(_service, "user_config_dir", lambda create=False: tmp_path / "config")
    monkeypatch.setattr(_service, "_volatile_service_state_reason", lambda path: None)
    monkeypatch.setattr(
        _service, "open_private_directory",
        lambda directory, *, create: os.open(tmp_path, os.O_RDONLY),
    )

    def broken_transaction(self):
        raise OSError("disk on fire")

    monkeypatch.setattr(_registry.Registry, "transaction", broken_transaction)
    with pytest.raises(ServiceError, match="cannot establish registry"):
        _service._effective_service_environment()


def test_resolve_scheduler_missing(monkeypatch):
    monkeypatch.setattr(_service, "resolve_executable_in_dirs", lambda *args, **kwargs: None)
    with pytest.raises(ServiceError, match="trusted scheduler"):
        _service._resolve_scheduler("launchctl")


def test_volatile_reason_linux_roots(monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "linux")
    assert _service._volatile_service_state_reason(Path("/run/user/1000/x")) is not None
    assert _service._volatile_service_state_reason(Path("/dev/shm/x")) is not None


def test_managed_ancestor_oserror_is_inconclusive(tmp_path, monkeypatch):
    def boom(path):
        raise OSError("probe denied")

    monkeypatch.setattr(_service.os.path, "lexists", boom)
    assert _service._managed_directory_ancestor(tmp_path) is None


def test_service_status_linux_scheduler_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "linux")
    monkeypatch.setattr(_service, "_systemd_dir", lambda: tmp_path)

    def missing(name):
        raise ServiceError("no systemctl")

    monkeypatch.setattr(_service, "_resolve_scheduler", missing)
    status = _service.service_status()
    assert status["active"] is None
    assert status["installed"] is False


# --- completion contract ------------------------------------------------------

def test_completion_module_roundtrip_bash_and_zsh():
    for shell in ("bash", "zsh"):
        message = install_completion(shell)
        assert shell in message
        report = completion_status(shell)
        assert report[0]["status"] == "installed"
        assert uninstall_completion(shell).startswith("removed")
        assert completion_status(shell)[0]["status"] == "not-installed"
    assert uninstall_completion("bash") == "no bash completion installed"


def test_completion_install_refuses_powershell():
    with pytest.raises(ServiceError, match="not supported"):
        install_completion("powershell")


# --- CLI surface ---------------------------------------------------------------

def test_cli_service_status_text_and_json(capsys, monkeypatch):
    monkeypatch.setattr(
        "ephemdir.cli.service_status",
        lambda: {
            "platform": "darwin",
            "manager": "launchd",
            "unit": "unit-name",
            "installed": True,
            "active": None,
            "files": {"/x/y.plist": "installed"},
            "environment": {"EPHEMDIR_DATA_DIR": "/data"},
            "command": ["/usr/bin/python3", "-I", "-m", "ephemdir", "sweep"],
            "command_valid": True,
            "working_directory": "/",
            "working_directory_valid": True,
            "registry_status": "present",
            "registry_present": True,
        },
    )
    assert main(["service", "status"]) == 0
    out = capsys.readouterr().out
    assert "manager:   launchd" in out
    assert "active:    unknown" in out
    assert "file:      installed" in out
    assert "env:       EPHEMDIR_DATA_DIR=/data" in out
    assert "argv-ok:   True" in out
    assert "cwd:       /" in out
    assert "cwd-ok:    True" in out
    assert "registry:  present" in out

    assert main(["service", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True


def test_cli_service_namespace_and_legacy_aliases(monkeypatch):
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "ephemdir.cli.install_service",
        lambda interval, runtime_policy: calls.append(("install", interval)) or "ok",
    )
    monkeypatch.setattr(
        "ephemdir.cli.uninstall_service", lambda: calls.append(("uninstall",)) or "ok"
    )
    assert main(["service", "install", "--interval", "5"]) == 0
    assert main(["service", "uninstall"]) == 0
    assert main(["install-service", "--interval", "7"]) == 0
    assert main(["uninstall-service"]) == 0
    assert calls == [("install", 5), ("uninstall",), ("install", 7), ("uninstall",)]


def test_cli_completion_error_paths(monkeypatch):
    def boom(shell):
        raise ServiceError("boom")

    monkeypatch.setattr("ephemdir.cli.install_completion", boom)
    monkeypatch.setattr("ephemdir.cli.uninstall_completion", boom)
    assert main(["completion", "install", "bash"]) == 1
    assert main(["completion", "uninstall", "bash"]) == 1


def test_cli_completion_status_text(capsys):
    assert main(["completion", "status"]) == 0
    out = capsys.readouterr().out
    for shell in ("bash", "zsh", "fish"):
        assert shell in out
    assert "not-installed" in out


# --- stats error paths ----------------------------------------------------------

def _zero_counters() -> dict[str, int]:
    return dict.fromkeys(COUNTERS, 0)


def test_stats_snapshot_garbage_and_unreadable(tmp_path):
    garbage = tmp_path / "stats.json"
    garbage.write_text("not json", encoding="utf-8")
    assert StatsLedger(path=garbage).snapshot() == _zero_counters()

    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    nested = StatsLedger(path=blocker / "stats.json")  # parent is a file
    assert nested.snapshot() == _zero_counters()


def test_stats_record_ignores_unknown_and_zero_deltas(tmp_path):
    path = tmp_path / "stats.json"
    ledger = StatsLedger(path=path)
    ledger._record({"unknown-counter": 5})
    ledger._record({"created": 0})
    assert not path.exists()  # nothing worth writing


# --- recovery-plan decision branches (explain over journal states) -------------

def test_explain_reports_recovery_journal_states(tmp_path, registry):
    import shutil

    from ephemdir import recover, registered, tempdir
    from ephemdir.core import _MARKER_NAME, explain

    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"

    with registry.transaction() as state:
        state[str(d.path)].update({"state": "recovery", "claim_id": None})
    decision = explain(str(d.path), registry=registry)
    assert decision.status == "blocked"
    assert "recovery-required" in decision.blockers
    assert decision.destructive_allowed is False

    # A staging path that passes registry shape checks but not core's stricter
    # suffix pattern must plan as recovery-required, never as deletable.
    loose = d.path.parent / f".{d.path.name}.zz.deleting"
    with registry.transaction() as state:
        state[str(d.path)].update(
            {"state": "deleting", "claim_id": None, "staging_path": str(loose)}
        )
    decision = explain(str(d.path), registry=registry)
    assert decision.status == "blocked"
    assert "recovery-required" in decision.blockers

    # Valid staging reference, nothing on disk at the staging path.
    with registry.transaction() as state:
        state[str(d.path)]["staging_path"] = str(staging)
    decision = explain(str(d.path), registry=registry)
    assert decision.status == "blocked"
    assert "recovery-required" in decision.blockers

    # Staging exists but carries no marker: unverified, still blocked.
    staging.mkdir()
    decision = explain(str(d.path), registry=registry)
    assert decision.status == "blocked"
    assert "recovery-required" in decision.blockers
    staging.rmdir()

    # Move the real directory (marker intact) to staging: resumable, due.
    shutil.rmtree(staging, ignore_errors=True)
    import os as os_module

    os_module.rename(d.path, staging)
    decision = explain(str(d.path), registry=registry)
    assert decision.status == "due"
    assert decision.reasons == ("recovery-resume",)
    assert decision.destructive_allowed is True
    assert (staging / _MARKER_NAME).is_file()

    # And recover() finishes it.
    assert recover(str(d.path), action="retry", registry=registry) == d.path
    assert not staging.exists()
    assert str(d.path) not in registered(registry=registry)


def test_recover_retires_completed_root_only_journal(tmp_path, registry):
    import shutil

    from ephemdir import recover, registered, tempdir

    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    staging = d.path.parent / f".{d.path.name}.123-abcdef12.deleting"
    shutil.rmtree(d.path)
    with registry.transaction() as state:
        state[str(d.path)].update(
            {
                "state": "deleting",
                "claim_id": None,
                "staging_path": str(staging),
                "delete_phase": "root-only",
            }
        )

    assert recover(str(d.path), action="retry", registry=registry) == d.path
    assert str(d.path) not in registered(registry=registry)


# --- registry corrupt-entry load path -------------------------------------------

def test_load_raises_corrupt_registry_for_invalid_entry(tmp_path):
    from ephemdir._registry import CorruptRegistryError, Registry

    path = tmp_path / "registry.json"
    envelope = {
        "schema_version": 3,
        "writer": {"name": "ephemdir", "format": "registry-v3"},
        "entries": {"relative-key": {"created_at": 1.0}},
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(CorruptRegistryError, match="corrupt"):
        Registry(path=path).load()


# --- backend refusal branches ----------------------------------------------------

def test_open_verified_staging_refuses_non_directory(tmp_path):
    from ephemdir._backends import posix

    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        posix.open_verified_staging(target, {"marker_id": "a" * 32})
    assert target.is_file()


def test_preflight_claim_source_missing_and_foreign(tmp_path):
    from ephemdir._backends import posix

    parent_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        missing = posix.preflight_claim_source(
            tmp_path / "gone", parent_fd, {"marker_id": "a" * 32}, allow_unverified=False
        )
        assert missing.status == "foreign"  # nothing verifiable at the path
        assert missing.source_fd is None

        source = tmp_path / "present"
        source.mkdir()
        foreign = posix.preflight_claim_source(
            source, parent_fd, {"marker_id": "a" * 32}, allow_unverified=False
        )
        assert foreign.status == "foreign"
        assert foreign.source_fd is None
        assert source.is_dir()
    finally:
        os.close(parent_fd)


# --- CLI logging levels and small exits ------------------------------------------

def test_cli_verbosity_flags(capsys):
    assert main(["--quiet", "list"]) == 0
    capsys.readouterr()
    assert main(["--verbose", "list"]) == 0
    capsys.readouterr()


def test_cli_extend_requires_lifetime_or_forever(tmp_path, capsys):
    from ephemdir import tempdir

    d = tempdir(parent=tmp_path)
    try:
        assert main(["extend", d.path.name]) == 2
    finally:
        assert main(["rm", d.path.name]) == 0

# --- rc7 audit regressions ----------------------------------------------------

def test_service_status_linux_is_strictly_read_only(tmp_path, monkeypatch):
    config = tmp_path / "fresh-config"
    monkeypatch.setenv("EPHEMDIR_CONFIG_DIR", str(config))
    monkeypatch.setattr(_service.sys, "platform", "linux")
    monkeypatch.setattr(
        _service,
        "_resolve_scheduler",
        lambda name: (_ for _ in ()).throw(ServiceError("unavailable")),
    )

    assert not config.exists()
    status = _service.service_status()
    assert status["installed"] is False
    assert not config.exists()


def test_installed_state_reports_writable_file_as_unsafe():
    install_completion("fish")
    row = completion_status("fish")[0]
    target = Path(row["path"])
    os.chmod(target, 0o666)

    assert completion_status("fish")[0]["status"] == "unsafe"
    with pytest.raises(ServiceError, match="writable by group or others"):
        uninstall_completion("fish")
    assert target.exists()


def test_manifest_with_non_owner_permissions_is_fail_closed(tmp_path, monkeypatch):
    target = tmp_path / "managed"
    target.write_text("content", encoding="utf-8")
    os.chmod(target, 0o600)
    manifest = tmp_path / "installed-files.json"
    manifest.write_text(
        json.dumps({str(target): _service._content_digest("content")}),
        encoding="utf-8",
    )
    os.chmod(manifest, 0o644)
    monkeypatch.setattr(_service, "_manifest_path", lambda: manifest)

    assert _service._load_manifest() == {}
    with pytest.raises(ServiceError, match="not recorded"):
        _service._remove_recorded_installed_file(target)
    assert target.exists()


def test_uninstall_launchd_dangling_symlink_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "agent.plist"
    path.symlink_to(tmp_path / "missing-target")
    monkeypatch.setattr(_service, "_launchd_path", lambda: path)
    monkeypatch.setattr(
        _service,
        "_run_scheduler",
        lambda name, args: type("C", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    with pytest.raises(ServiceError, match="not recorded"):
        _service._uninstall_launchd()
    assert os.path.lexists(path)


def test_service_status_does_not_trust_modified_unit_environment(tmp_path, monkeypatch):
    units = tmp_path / "units"
    service = units / f"{_service.SYSTEMD_UNIT}.service"
    timer = units / f"{_service.SYSTEMD_UNIT}.timer"
    content = _service.render_systemd_units(
        60,
        _service.sweep_command(),
        env_executable="/usr/bin/env",
        environment={"EPHEMDIR_DATA_DIR": str(tmp_path / "data")},
    )
    _service._write_service_file(service, content[service.name])
    _service._record_installed_file(service, content[service.name])
    _service._write_service_file(timer, content[timer.name])
    _service._record_installed_file(timer, content[timer.name])
    service.write_text(
        content[service.name].replace(str(tmp_path / "data"), "/attacker/path"),
        encoding="utf-8",
    )
    os.chmod(service, 0o600)
    monkeypatch.setattr(_service.sys, "platform", "linux")
    monkeypatch.setattr(_service, "_systemd_dir", lambda: units)
    monkeypatch.setattr(
        _service,
        "_resolve_scheduler",
        lambda name: (_ for _ in ()).throw(ServiceError("unavailable")),
    )

    status = _service.service_status()
    assert status["files"][str(service)] == "modified"
    assert status["environment"] == {}
    assert status["command_valid"] is None
    assert status["registry_present"] is None


def test_service_status_reports_exact_command_cwd_and_safe_registry(tmp_path, monkeypatch):
    units = tmp_path / "units"
    service = units / f"{_service.SYSTEMD_UNIT}.service"
    timer = units / f"{_service.SYSTEMD_UNIT}.timer"
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    registry = data / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    os.chmod(registry, 0o600)
    expected_command = _service.sweep_command()
    content = _service.render_systemd_units(
        60,
        expected_command,
        env_executable="/usr/bin/env",
        environment={"EPHEMDIR_DATA_DIR": str(data)},
    )
    for path in (service, timer):
        _service._write_service_file(path, content[path.name])
        _service._record_installed_file(path, content[path.name])
    monkeypatch.setattr(_service.sys, "platform", "linux")
    monkeypatch.setattr(_service, "_systemd_dir", lambda: units)
    monkeypatch.setattr(
        _service,
        "_resolve_scheduler",
        lambda name: "/usr/bin/env" if name == "env" else "/usr/bin/systemctl",
    )
    monkeypatch.setattr(
        _service,
        "_run",
        lambda args: type("C", (), {"returncode": 0, "stdout": "inactive\n", "stderr": ""})(),
    )

    status = _service.service_status()
    assert status["command"] == ["/usr/bin/env", "--", *expected_command]
    assert status["command_valid"] is True
    assert status["working_directory"] == "/"
    assert status["working_directory_valid"] is True
    assert status["registry_status"] == "present"
    assert status["registry_present"] is True


def test_registry_status_refuses_symlink(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    real = tmp_path / "real-registry"
    real.write_text("{}", encoding="utf-8")
    (data / "registry.json").symlink_to(real)
    assert _service._registry_file_state(str(data)) == "unsafe"


def test_pinned_definition_refuses_unsafe_shapes(tmp_path, monkeypatch):
    fifo = tmp_path / "unit.service"
    os.mkfifo(fifo)
    assert _service._pinned_service_definition(fifo) == {}
    fifo.unlink()

    unit = tmp_path / "unit.service"
    unit.write_text("ExecStart=/bin/true\n", encoding="utf-8")
    os.chmod(unit, 0o666)
    assert _service._pinned_service_definition(unit) == {}
    os.chmod(unit, 0o600)

    monkeypatch.setattr(_service.os, "geteuid", lambda: os.stat(unit).st_uid + 1)
    assert _service._pinned_service_definition(unit) == {}


def test_pinned_definition_refuses_unsafe_parent_and_oversized(tmp_path, monkeypatch):
    parent = tmp_path / "unsafe-parent"
    parent.mkdir(mode=0o700)
    unit = parent / "unit.service"
    unit.write_text("ExecStart=/bin/true\n", encoding="utf-8")
    os.chmod(unit, 0o600)
    os.chmod(parent, 0o777)
    assert _service._pinned_service_definition(unit) == {}
    os.chmod(parent, 0o700)

    monkeypatch.setattr(_service, "_INSTALLED_FILE_MAX_BYTES", 2)
    assert _service._pinned_service_definition(unit) == {}


def test_pinned_definition_parsing_failures_and_defaults(tmp_path):
    plist = tmp_path / "bad.plist"
    plist.write_text("not plist", encoding="utf-8")
    os.chmod(plist, 0o600)
    assert _service._pinned_service_definition(plist) == {}

    plist.write_bytes(__import__("plistlib").dumps(["not", "a", "dict"]))
    os.chmod(plist, 0o600)
    assert _service._pinned_service_definition(plist) == {}

    plist.write_bytes(
        __import__("plistlib").dumps(
            {
                "ProgramArguments": ["ok", 3],
                "EnvironmentVariables": ["not", "a", "dict"],
            }
        )
    )
    os.chmod(plist, 0o600)
    parsed = _service._pinned_service_definition(plist)
    assert parsed == {"command": [], "working_directory": None, "environment": {}}

    unit = tmp_path / "broken.service"
    unit.write_text(
        'WorkingDirectory=/\nExecStart="unterminated\nEnvironment="unterminated\n',
        encoding="utf-8",
    )
    os.chmod(unit, 0o600)
    parsed = _service._pinned_service_definition(unit)
    assert parsed["command"] == []
    assert parsed["working_directory"] == "/"
    assert parsed["environment"] == {}


def test_service_command_valid_and_registry_state_error_branches(tmp_path, monkeypatch):
    assert _service._service_command_valid("launchd", "not-a-list") is False
    monkeypatch.setattr(
        _service,
        "_resolve_scheduler",
        lambda name: (_ for _ in ()).throw(ServiceError("missing")),
    )
    assert _service._service_command_valid("systemd", ["x"]) is None

    monkeypatch.setattr(
        _service.os,
        "stat",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert _service._registry_file_state(str(tmp_path)) == "unreadable"


def test_manifest_updates_are_serialized_across_concurrent_writers(tmp_path, monkeypatch):
    import threading
    import time

    real_load = _service._load_manifest
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_guard = threading.Lock()
    errors: list[BaseException] = []

    def slow_load():
        nonlocal calls
        with calls_guard:
            calls += 1
            current = calls
        if current == 1:
            entered.set()
            assert release.wait(5)
        return real_load()

    def writer(name: str) -> None:
        try:
            _service._record_installed_file(tmp_path / name, name)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    monkeypatch.setattr(_service, "_load_manifest", slow_load)
    first = threading.Thread(target=writer, args=("first",))
    second = threading.Thread(target=writer, args=("second",))
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.1)
    assert calls == 1  # second writer is blocked before its read
    release.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    monkeypatch.setattr(_service, "_load_manifest", real_load)
    manifest = _service._load_manifest()
    assert manifest[str(tmp_path / "first")] == _service._content_digest("first")
    assert manifest[str(tmp_path / "second")] == _service._content_digest("second")


def test_manifest_lock_refuses_unsafe_existing_lock(tmp_path, monkeypatch):
    manifest = tmp_path / "config" / "installed-files.json"
    manifest.parent.mkdir(mode=0o700)
    lock = manifest.parent / _service._MANIFEST_LOCK_NAME
    lock.write_text("", encoding="utf-8")
    os.chmod(lock, 0o644)
    monkeypatch.setattr(_service, "_manifest_path", lambda: manifest)

    with pytest.raises(ServiceError, match="lock is not owner-only"):
        _service._record_installed_file(tmp_path / "target", "content")
    assert not manifest.exists()


def test_manifest_save_enforces_size_bound(monkeypatch):
    monkeypatch.setattr(_service, "_MANIFEST_MAX_BYTES", 8)
    with pytest.raises(ServiceError, match="safety bound"):
        _service._save_manifest({"/x": "a" * 64})


def test_manifest_lock_timeout_is_fail_closed(tmp_path, monkeypatch):
    from ephemdir import _registry

    monkeypatch.setattr(_service, "_MANIFEST_LOCK_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(
        _registry,
        "_try_lock",
        lambda fd: (_ for _ in ()).throw(BlockingIOError("busy")),
    )
    with pytest.raises(ServiceError, match="could not lock installed-files manifest"):
        _service._record_installed_file(tmp_path / "target", "content")
    assert _service._load_manifest() == {}


def test_manifest_lock_symlink_is_refused(tmp_path, monkeypatch):
    manifest = tmp_path / "config" / "installed-files.json"
    manifest.parent.mkdir(mode=0o700)
    lock = manifest.parent / _service._MANIFEST_LOCK_NAME
    lock.symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(_service, "_manifest_path", lambda: manifest)

    with pytest.raises(ServiceError, match="cannot open installed-files lock safely"):
        _service._record_installed_file(tmp_path / "target", "content")
    assert os.path.lexists(lock)
    assert not manifest.exists()


def test_forget_installed_file_updates_manifest(tmp_path):
    target = tmp_path / "target"
    _service._record_installed_file(target, "content")
    assert str(target) in _service._load_manifest()
    _service._forget_installed_file(target)
    assert str(target) not in _service._load_manifest()


def test_cli_service_status_optional_fields_and_legacy_registry_fallback(capsys, monkeypatch):
    monkeypatch.setattr(
        "ephemdir.cli.service_status",
        lambda: {
            "platform": "linux",
            "manager": "systemd",
            "unit": "ephemdir-sweep.timer",
            "installed": False,
            "active": False,
            "files": [],
            "environment": [],
            "command": [],
            "command_valid": None,
            "working_directory": None,
            "working_directory_valid": None,
            "registry_status": None,
            "registry_present": False,
        },
    )
    assert main(["service", "status"]) == 0
    out = capsys.readouterr().out
    assert "active:    False" in out
    assert "command:" not in out
    assert "argv-ok:" not in out
    assert "cwd:" not in out
    assert "registry:  MISSING" in out


def test_manifest_lock_refuses_non_regular_file(tmp_path, monkeypatch):
    manifest = tmp_path / "config" / "installed-files.json"
    manifest.parent.mkdir(mode=0o700)
    lock = manifest.parent / _service._MANIFEST_LOCK_NAME
    os.mkfifo(lock)
    monkeypatch.setattr(_service, "_manifest_path", lambda: manifest)

    with pytest.raises(ServiceError, match="lock is not a regular file"):
        _service._record_installed_file(tmp_path / "target", "content")
    assert lock.exists()
    assert not manifest.exists()


def test_registry_state_rejects_foreign_and_readable_files(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    registry = data / "registry.json"
    registry.write_text("{}", encoding="utf-8")
    os.chmod(registry, 0o644)
    assert _service._registry_file_state(str(data)) == "unsafe"

    os.chmod(registry, 0o600)
    real_uid = os.stat(registry).st_uid
    monkeypatch.setattr(_service.os, "geteuid", lambda: real_uid + 1)
    assert _service._registry_file_state(str(data)) == "unsafe"


def test_service_status_darwin_scheduler_error_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "darwin")
    monkeypatch.setattr(_service, "_launchd_path", lambda: tmp_path / "missing.plist")
    monkeypatch.setattr(
        _service,
        "_run_scheduler",
        lambda name, args: (_ for _ in ()).throw(ServiceError("unavailable")),
    )
    status = _service.service_status()
    assert status["active"] is None
    assert status["installed"] is False
    assert status["command_valid"] is None


# --- field-test findings from the 3-day manual gate ---------------------------

def test_explain_states_restart_cleanup_instead_of_calling_it_kept(tmp_path, registry, capsys):
    """F-1: the default `new` is restart-scoped; explain must not say "kept"."""
    from ephemdir import tempdir
    from ephemdir._display import explain_payload, explain_trace
    from ephemdir.core import explain

    d = tempdir(parent=tmp_path, registry=registry)
    entry = registry.load(read_only=True)[str(d.path)]
    assert entry["remove_on_restart"] is True
    decision = explain(str(d.path), registry=registry)
    now = time.time()

    trace = explain_trace(decision, entry, now, ascii_only=True)
    assert "kept until the next restart" in trace
    assert "kept (no time limit)" not in trace
    assert "removed when the machine restarts" in trace

    payload = explain_payload(decision, entry, now)
    assert payload["remove_on_restart"] is True
    assert payload["keep_while_in_use"] is False


def test_explain_reports_keep_while_in_use_and_plain_permanent_entries(tmp_path, registry):
    from ephemdir import tempdir
    from ephemdir._display import explain_payload, explain_trace
    from ephemdir.core import explain

    held = tempdir(parent=tmp_path, registry=registry, remove_on_restart=False,
                   keep_while_in_use=True)
    entry = registry.load(read_only=True)[str(held.path)]
    now = time.time()
    trace = explain_trace(explain(str(held.path), registry=registry), entry, now, ascii_only=True)
    assert "kept (no time limit)" in trace          # genuinely permanent
    assert "removed when the machine restarts" not in trace
    assert "held back while any process uses it" in trace
    assert explain_payload(
        explain(str(held.path), registry=registry), entry, now
    )["keep_while_in_use"] is True


def test_vanishing_path_is_reported_missing_not_replaced(tmp_path, registry, monkeypatch):
    """F-3: a sweep renaming the directory mid-probe must not read as 'replaced'.

    `replaced` means a foreign directory occupies the path and is a safety
    signal, so it may never fire for a path that simply disappeared between
    the two probes inside dir_status().
    """
    from ephemdir import tempdir
    from ephemdir.core import dir_status

    d = tempdir(parent=tmp_path, registry=registry)
    entry = registry.load(read_only=True)[str(d.path)]
    assert dir_status(entry, d.path, time.time(), None) == "until-restart"

    # Simulate the real race: _path_state() above sees the directory, then the
    # concurrent sweep renames it to its staging name, and the ownership probe
    # that follows classifies the now-absent path as "foreign". Both the
    # vanish and the resulting verdict are genuine here -- only the timing is
    # forced -- so the fix is exercised through the real code path.
    real_ownership = core_module._ownership

    def racing_ownership(path, probe_entry):
        if path == d.path and d.path.exists():
            shutil.move(str(d.path), str(tmp_path / "moved-away"))
        return real_ownership(path, probe_entry)

    monkeypatch.setattr(core_module, "_ownership", racing_ownership)
    assert real_ownership(d.path, entry) == "ours"          # sanity: ours until moved
    assert dir_status(entry, d.path, time.time(), None) == "missing"
    assert not d.path.exists()
    assert (tmp_path / "moved-away").is_dir()               # data intact, just renamed


def test_genuinely_foreign_directory_still_reads_replaced(tmp_path, registry):
    """The safety signal must survive the F-3 fix for a real replacement."""
    from ephemdir import tempdir
    from ephemdir.core import dir_status

    d = tempdir(parent=tmp_path, registry=registry)
    entry = registry.load(read_only=True)[str(d.path)]
    shutil.rmtree(d.path)
    d.path.mkdir()                                # someone else's directory now
    (d.path / "not-ours.txt").write_text("foreign", encoding="utf-8")

    assert dir_status(entry, d.path, time.time(), None) == "replaced"
    assert (d.path / "not-ours.txt").exists()


def test_list_json_row_is_self_consistent(tmp_path, registry, capsys):
    """F-3: exists and original_state come from one probe, never contradict."""
    from ephemdir import tempdir

    tempdir(parent=tmp_path, registry=registry)
    assert main(["list", "--json"]) == 0
    for row in json.loads(capsys.readouterr().out):
        if row["exists"] is True:
            assert row["original_state"] == "present"
        elif row["exists"] is False:
            assert row["original_state"] == "missing"
        else:
            assert row["original_state"] == "unknown"
