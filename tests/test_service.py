"""Tests for service file rendering (no real install side effects)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ephemdir import _service
from ephemdir._service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT,
    render_launchd_plist,
    render_systemd_units,
    sweep_command,
)


def test_sweep_command_uses_current_interpreter_in_isolated_mode():
    import sys

    command = sweep_command()
    # Always `python -I -m ephemdir`, never whatever PATH happens to find
    # first; `-I` keeps PYTHONPATH/cwd/user-site from substituting the package.
    assert command[0] == sys.executable
    assert command[1:4] == ["-I", "-m", "ephemdir"]
    assert command[-1] == "sweep"


def test_launchd_plist_contains_label_and_interval():
    plist = render_launchd_plist(900, ["/usr/local/bin/ephemdir", "sweep"])
    assert LAUNCHD_LABEL in plist
    assert "<integer>900</integer>" in plist
    assert "<string>sweep</string>" in plist


def test_launchd_plist_pins_working_directory_and_path():
    import plistlib

    parsed = plistlib.loads(render_launchd_plist(900, sweep_command()).encode("utf-8"))
    assert parsed["WorkingDirectory"] == "/"
    environment = parsed["EnvironmentVariables"]
    assert set(environment) == {"PATH"}
    assert all(part.startswith("/") for part in environment["PATH"].split(os.pathsep))


def test_systemd_units_have_service_and_timer():
    units = render_systemd_units(300, ["ephemdir", "sweep"])
    assert f"{SYSTEMD_UNIT}.service" in units
    assert f"{SYSTEMD_UNIT}.timer" in units
    assert "ExecStart=/usr/bin/env -- ephemdir sweep" in units[f"{SYSTEMD_UNIT}.service"]
    assert "OnUnitActiveSec=300" in units[f"{SYSTEMD_UNIT}.timer"]


def test_systemd_service_isolates_python_environment():
    service = render_systemd_units(300, sweep_command())[f"{SYSTEMD_UNIT}.service"]
    assert "WorkingDirectory=/\n" in service
    assert "UnsetEnvironment=PYTHONPATH PYTHONHOME PYTHONSTARTUP\n" in service
    assert ' "-I" ' not in service  # -I is a plain token, not quoted unit syntax
    assert " -I " in service


def test_install_service_rejects_bad_interval():
    with pytest.raises(ValueError):
        _service.install_service(interval=0)


def test_systemd_units_use_resolved_env_executable():
    units = render_systemd_units(300, ["ephemdir", "sweep"], env_executable="/custom/env")
    assert "ExecStart=/custom/env -- ephemdir sweep" in units[f"{SYSTEMD_UNIT}.service"]


def test_install_systemd_rejects_untrusted_env_before_writing(monkeypatch):
    # LOW-02: an `env` that fails the trusted resolver (foreign-owned,
    # writable, missing) must abort the install before any unit file exists.
    written = []
    monkeypatch.setattr(
        _service, "_write_service_file", lambda path, content: written.append(path)
    )

    def resolve(name):
        if name == "systemctl":
            return "/usr/bin/systemctl"
        raise _service.ServiceError(f"could not find trusted scheduler executable {name!r}")

    monkeypatch.setattr(_service, "_resolve_scheduler", resolve)
    with pytest.raises(_service.ServiceError, match="'env'"):
        _service._install_systemd(600)
    assert written == []


def test_launchd_plist_escapes_special_characters():
    import plistlib

    plist = render_launchd_plist(60, ["/odd path/ephem&dir", "sweep"])
    assert "&amp;" in plist  # XML-escaped, not raw
    parsed = plistlib.loads(plist.encode("utf-8"))
    assert parsed["ProgramArguments"][0] == "/odd path/ephem&dir"
    assert parsed["StartInterval"] == 60


def test_systemd_exec_start_quotes_spaces():
    units = render_systemd_units(300, ["/opt/my tools/ephemdir", "sweep"])
    service = units[f"{SYSTEMD_UNIT}.service"]
    assert 'ExecStart=/usr/bin/env -- "/opt/my tools/ephemdir" sweep' in service


def test_windows_command_quotes_spaces():
    quoted = _service.render_windows_command(["C:\\My Tools\\ephemdir.exe", "sweep"])
    assert quoted == '"C:\\My Tools\\ephemdir.exe" sweep'


def test_failed_scheduler_command_raises(monkeypatch):
    import subprocess

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(_service.subprocess, "run", fake_run)
    with pytest.raises(_service.ServiceError, match="permission denied"):
        _service._run_checked(["launchctl", "load", "x"], "launchctl load")


def test_windows_uninstall_raises_when_absence_is_ambiguous(monkeypatch):
    import subprocess

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    monkeypatch.setattr(_service.subprocess, "run", fake_run)
    monkeypatch.setattr(_service, "_resolve_scheduler", lambda name: name)
    with pytest.raises(_service.ServiceError, match="could not determine"):
        _service._uninstall_windows()

def test_windows_uninstall_raises_on_delete_failure(monkeypatch):
    import subprocess

    responses = iter(
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr="access denied"),  # /Delete
            subprocess.CompletedProcess([], 0, stdout="task", stderr=""),  # /Query: exists
        ]
    )
    monkeypatch.setattr(_service.subprocess, "run", lambda command, **kwargs: next(responses))
    monkeypatch.setattr(_service, "_resolve_scheduler", lambda name: name)
    # The task still exists, so the failed delete is a real error.
    with pytest.raises(_service.ServiceError, match="access denied"):
        _service._uninstall_windows()


def test_resolve_scheduler_ignores_path_and_uses_trusted_dir(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted"
    untrusted = tmp_path / "untrusted"
    trusted.mkdir()
    untrusted.mkdir()
    good = trusted / "systemctl"
    bad = untrusted / "systemctl"
    good.write_text("#!/bin/sh\n", encoding="utf-8")
    bad.write_text("#!/bin/sh\n", encoding="utf-8")
    good.chmod(0o755)
    bad.chmod(0o755)

    # The resolver now validates the whole ancestor chain, and pytest's
    # default basetemp lives under a world-writable /tmp on Linux. Sanitize the
    # synthetic chain (root-owned, no group/world write) so this positive test
    # exercises trusted-dir selection rather than the real perms of /tmp.
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "lstat", _synthetic_lstat(os.lstat))
        monkeypatch.setattr(os, "stat", _synthetic_lstat(os.stat))

    monkeypatch.setenv("PATH", str(untrusted))
    monkeypatch.setattr(_service, "_trusted_scheduler_dirs", lambda: (trusted,))
    assert _service._resolve_scheduler("systemctl") == str(good)


def test_run_uses_controlled_environment(monkeypatch):
    import subprocess

    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("PYTHONPATH", "attacker")
    monkeypatch.setattr(_service.subprocess, "run", fake_run)
    _service._run(["/bin/echo", "ok"])

    assert "PYTHONPATH" not in captured["env"]
    assert "PATH" in captured["env"]
    assert captured["cwd"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_write_service_file_replaces_symlink_without_following(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("secret", encoding="utf-8")
    service_file = tmp_path / "unit.service"
    service_file.symlink_to(victim)

    _service._write_service_file(service_file, "safe")

    assert victim.read_text(encoding="utf-8") == "secret"
    assert service_file.read_text(encoding="utf-8") == "safe"
    assert not service_file.is_symlink()


def test_write_service_file_creates_owner_only_file(tmp_path):
    service_file = tmp_path / "units" / "unit.service"
    _service._write_service_file(service_file, "content")
    assert service_file.read_text(encoding="utf-8") == "content"
    import stat as stat_module

    assert stat_module.S_IMODE(service_file.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_write_service_file_rejects_other_writable_unit_dir(tmp_path):
    units_dir = tmp_path / "units"
    units_dir.mkdir()
    units_dir.chmod(0o775)
    if not (units_dir.stat().st_mode & 0o020):
        pytest.skip("filesystem does not honour group-writable modes")
    with pytest.raises(_service.ServiceError, match="writable by other users"):
        _service._write_service_file(units_dir / "unit.service", "content")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_write_service_file_rejects_unsafe_ancestor(tmp_path):
    # World-writable without sticky bit: any local user could swap the chain.
    unsafe = tmp_path / "shared"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    if not (unsafe.stat().st_mode & 0o002):
        pytest.skip("filesystem does not honour world-writable modes")
    with pytest.raises(_service.ServiceError, match="writable by other users"):
        _service._write_service_file(unsafe / "units" / "unit.service", "content")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_write_service_file_rejects_symlinked_unit_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    with pytest.raises(_service.ServiceError):
        _service._write_service_file(linked / "unit.service", "content")


def test_isolated_import_check_accepts_this_package(monkeypatch):
    # A dev checkout is not importable under a bare `python -I`, so emulate an
    # installed package: keep the real subprocess round-trip but resolve the
    # package via an explicit search path. The path comparison is what matters.
    import subprocess

    package_root = os.path.dirname(os.path.abspath(_service.__file__))
    real_run = subprocess.run

    def run_with_src_on_path(command, **kwargs):
        env = dict(kwargs.pop("env", {}))
        env["PYTHONPATH"] = os.path.dirname(package_root)
        command = [argument for argument in command if argument != "-I"]
        return real_run(command, env=env, **kwargs)

    monkeypatch.setattr(_service.subprocess, "run", run_with_src_on_path)
    _service._verify_isolated_import()


def test_isolated_import_check_rejects_missing_package(monkeypatch):
    import subprocess

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="ModuleNotFoundError: No module named 'ephemdir'"
        )

    monkeypatch.setattr(_service.subprocess, "run", fake_run)
    with pytest.raises(_service.ServiceError, match="isolated"):
        _service._verify_isolated_import()


def test_isolated_import_check_rejects_other_package_location(monkeypatch):
    import subprocess

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="/somewhere/else\n", stderr="")

    monkeypatch.setattr(_service.subprocess, "run", fake_run)
    with pytest.raises(_service.ServiceError, match="resolves ephemdir"):
        _service._verify_isolated_import()



def test_windows_service_install_is_refused(monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "win32")
    monkeypatch.setattr(_service, "_reject_elevated_user_install", lambda: None)

    with pytest.raises(_service.ServiceError, match="Windows is unsupported"):
        _service.install_service()


def test_runtime_path_rejects_other_user_writable_component(tmp_path):
    runtime = tmp_path / "python"
    runtime.write_text("placeholder", encoding="utf-8")
    runtime.chmod(0o666)

    with pytest.raises(_service.ServiceError, match="writable by other users"):
        _service._validate_runtime_path(runtime, "test runtime")


def _synthetic_lstat(real_lstat, foreign=()):
    """Sanitize the ancestor chain and plant synthetic foreign owners.

    Components in ``foreign`` report a uid that is provably neither root nor
    the current user; everything else reports root ownership with group/world
    write bits cleared. Fully synthetic uids keep these tests deterministic
    regardless of where pytest puts tmp_path (e.g. under a world-writable
    sticky ``/tmp``) and regardless of the uid running the suite — under
    root, a real file would otherwise be owned by the always-trusted uid 0.
    """
    foreign_paths = {str(path) for path in foreign}

    def fake_lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        values = list(result)
        if str(path) in foreign_paths:
            values[4] = os.geteuid() + 1  # st_uid: neither 0 nor the current user
        else:
            values[0] = result.st_mode & ~0o022  # st_mode: clear group/world write
            values[4] = 0  # st_uid: root-owned
        return os.stat_result(values)

    return fake_lstat


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership semantics required")
def test_runtime_path_rejects_foreign_owned_0755_component(tmp_path, monkeypatch):
    # A 0755 directory/file owned by another user is not group/world-writable,
    # but its owner can still replace the runtime at will.
    venv = tmp_path / "venv"
    venv.mkdir(mode=0o755)
    runtime = venv / "python"
    runtime.write_text("placeholder", encoding="utf-8")
    runtime.chmod(0o755)

    monkeypatch.setattr(os, "lstat", _synthetic_lstat(os.lstat, foreign=(venv,)))
    with pytest.raises(_service.ServiceError, match="component .* owned by another user"):
        _service._validate_runtime_path(runtime, "test runtime")


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership semantics required")
def test_runtime_path_rejects_foreign_owned_final_file(tmp_path, monkeypatch):
    runtime = tmp_path / "python"
    runtime.write_text("placeholder", encoding="utf-8")
    runtime.chmod(0o755)

    # Sanitize the whole lstat-walked chain (runtime included) so the failure
    # can only come from the final os.stat() ownership check, which sees a
    # synthetic foreign owner.
    monkeypatch.setattr(os, "lstat", _synthetic_lstat(os.lstat))
    real_stat = os.stat

    def foreign_final_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path) == str(runtime):
            values = list(result)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", foreign_final_stat)
    with pytest.raises(_service.ServiceError, match="test runtime is owned by another user"):
        _service._validate_runtime_path(runtime, "test runtime")


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership semantics required")
def test_check_runtime_component_rejects_foreign_owner():
    # Direct unit check with a synthetic stat: a 0755 directory owned by a
    # different uid is rejected regardless of any real filesystem layout.
    foreign = os.stat_result(
        (0o40755, 1, 2, 1, os.geteuid() + 1, 0, 0, 0, 0, 0)
    )
    from pathlib import Path

    with pytest.raises(_service.ServiceError, match="owned by another user"):
        _service._check_runtime_component(Path("/synthetic"), foreign, "test component")


def test_install_service_validates_persistent_runtime(monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "linux")
    monkeypatch.setattr(_service, "_reject_elevated_user_install", lambda: None)

    def reject():
        raise _service.ServiceError("unsafe persistent runtime")

    monkeypatch.setattr(_service, "_validate_service_runtime", reject)
    with pytest.raises(_service.ServiceError, match="unsafe persistent runtime"):
        _service.install_service()


def _make_fake_package(root):
    """Create a package tree resembling the real ephemdir layout."""
    pkg = root / "ephemdir"
    pkg.mkdir()
    for name in ("__init__.py", "__main__.py", "cli.py", "core.py", "_registry.py"):
        (pkg / name).write_text("", encoding="utf-8")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "core.cpython-312.pyc").write_bytes(b"")
    (pkg / "py.typed").write_text("", encoding="utf-8")  # data file, not a module
    (pkg / "README.txt").write_text("", encoding="utf-8")  # not importable code
    return pkg


def test_validate_package_tree_checks_every_module(tmp_path, monkeypatch):
    # LOW-04: `python -I -m ephemdir sweep` imports __main__/cli/core and every
    # _*.py helper, so the validator must walk the whole tree -- including
    # cached bytecode -- not just one entry point. Non-module files are skipped.
    pkg = _make_fake_package(tmp_path)
    checked: list[str] = []
    monkeypatch.setattr(
        _service, "_validate_runtime_path", lambda path, label: checked.append(Path(path).name)
    )
    # The per-directory check is exercised separately; here we test file walking.
    monkeypatch.setattr(_service, "_validate_runtime_dir", lambda path, label: None)

    _service._validate_package_tree(pkg)

    for required in ("__main__.py", "cli.py", "core.py", "_registry.py"):
        assert required in checked
    assert "core.cpython-312.pyc" in checked  # cached bytecode is executable too
    assert "README.txt" not in checked  # data files are not validated as code
    assert "py.typed" not in checked


def test_validate_package_tree_rejects_writable_module(tmp_path, monkeypatch):
    # A single group/world-writable module (e.g. __main__.py at 0666) must fail
    # the install even when __init__.py and the directories are locked down.
    pkg = _make_fake_package(tmp_path)

    def fake_validate(path, label):
        if Path(path).name == "__main__.py":
            raise _service.ServiceError(f"{path} is writable by other users")

    monkeypatch.setattr(_service, "_validate_runtime_path", fake_validate)
    monkeypatch.setattr(_service, "_validate_runtime_dir", lambda path, label: None)
    with pytest.raises(_service.ServiceError, match="writable by other users"):
        _service._validate_package_tree(pkg)


def test_validate_package_tree_rejects_symlinked_subdir(tmp_path, monkeypatch):
    # LOW-06 (variant 2): a symlinked __pycache__ pointing at an attacker
    # directory is not descended into by os.walk, but Python loads its cached
    # bytecode at import time. Such a tree must be refused, not silently passed.
    pkg = _make_fake_package(tmp_path)
    outside = tmp_path / "attacker"
    outside.mkdir()
    (outside / "core.cpython-312.pyc").write_bytes(b"")
    import shutil

    shutil.rmtree(pkg / "__pycache__")
    (pkg / "__pycache__").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(_service, "_validate_runtime_path", lambda path, label: None)
    monkeypatch.setattr(_service, "_validate_runtime_dir", lambda path, label: None)
    with pytest.raises(_service.ServiceError, match="symlinked subdirectory"):
        _service._validate_package_tree(pkg)


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership semantics required")
def test_validate_package_tree_rejects_foreign_owned_empty_subdir(tmp_path, monkeypatch):
    # LOW-07: an empty (or unknown-files-only) subdirectory owned by another
    # user -- e.g. a foreign __pycache__ -- has no module file to validate
    # indirectly, yet its owner can drop an unchecked .pyc into it after the
    # service is installed. The directory itself must be validated. Synthetic
    # owners keep the result independent of the real perms of tmp_path.
    pkg = _make_fake_package(tmp_path)
    cache = pkg / "__pycache__"
    for entry in cache.iterdir():
        entry.unlink()  # the LOW-07 scenario: a foreign but *empty* __pycache__

    real_stat, real_lstat = os.stat, os.lstat
    monkeypatch.setattr(os, "lstat", _synthetic_lstat(real_lstat, foreign=(cache,)))
    monkeypatch.setattr(os, "stat", _synthetic_lstat(real_stat, foreign=(cache,)))
    with pytest.raises(_service.ServiceError, match="owned by another user"):
        _service._validate_package_tree(pkg)

    # Positive control: the same tree with no foreign directory validates.
    monkeypatch.setattr(os, "lstat", _synthetic_lstat(real_lstat))
    monkeypatch.setattr(os, "stat", _synthetic_lstat(real_stat))
    _service._validate_package_tree(pkg)  # must not raise


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_validate_package_tree_fails_closed_on_unreadable_subdir(tmp_path):
    # LOW-08: os.walk() silently skips a subdirectory it cannot enter, so a
    # foreign __pycache__ at mode 0000 would never be validated and the install
    # reported as safe. The traversal must fail closed instead. A 0000 dir is
    # unreadable even by its owner, so this exercises the onerror path without
    # needing a second uid.
    pkg = _make_fake_package(tmp_path)
    cache = pkg / "__pycache__"
    for entry in cache.iterdir():
        entry.unlink()
    os.chmod(cache, 0o000)
    try:
        with pytest.raises(_service.ServiceError):
            _service._validate_package_tree(pkg)
    finally:
        os.chmod(cache, 0o755)  # restore so tmp_path cleanup can recurse in


def test_validate_package_tree_requires_some_module(tmp_path, monkeypatch):
    # A package directory with no importable module is suspicious: never report
    # success without having verified any actual code.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(_service, "_validate_runtime_path", lambda path, label: None)
    monkeypatch.setattr(_service, "_validate_runtime_dir", lambda path, label: None)
    with pytest.raises(_service.ServiceError, match="no ephemdir module files"):
        _service._validate_package_tree(empty)


def test_iter_startup_files_includes_site_pth(tmp_path, monkeypatch):
    # LOW-05: `python -I` still runs `.pth` files in site-packages at startup,
    # so they are part of the code the scheduler executes and must be checked.
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    (site_dir / "evil.pth").write_text("import os\n", encoding="utf-8")
    (site_dir / "notes.txt").write_text("", encoding="utf-8")  # not executed
    monkeypatch.setattr(_service.site, "getsitepackages", lambda: [str(site_dir)])

    found = {Path(path).name: label for path, label in _service._iter_startup_files()}
    assert found.get("evil.pth") == "site .pth file"
    assert "notes.txt" not in found  # only .pth/startup hooks are validated


def test_validate_startup_environment_rejects_writable_pth(tmp_path, monkeypatch):
    # A world-writable .pth another local user could edit must fail the install.
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    (site_dir / "evil.pth").write_text("import os\n", encoding="utf-8")
    monkeypatch.setattr(_service.site, "getsitepackages", lambda: [str(site_dir)])

    def fake_validate(path, label):
        if Path(path).name == "evil.pth":
            raise _service.ServiceError(f"{path} is writable by other users")

    monkeypatch.setattr(_service, "_validate_runtime_path", fake_validate)
    with pytest.raises(_service.ServiceError, match="writable by other users"):
        _service._validate_startup_environment()
