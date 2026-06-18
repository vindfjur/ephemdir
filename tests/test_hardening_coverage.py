"""Focused coverage for low-risk hardening helpers called out by audit."""

from __future__ import annotations

import builtins
import ctypes
import io
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ephemdir import _doctor, _mounts, _naming, _platform, _security, _size, _trusted_exec
from ephemdir._menu import run_menu
from ephemdir._registry import Registry


def _stat_result(mode: int, uid: int | None = None, size: int = 0) -> os.stat_result:
    owner = os.geteuid() if uid is None and hasattr(os, "geteuid") else (uid or 0)
    return os.stat_result((mode, 1, 1, 1, owner, 0, size, 0, 0, 0))


def test_platform_directory_selection_without_creation(tmp_path, monkeypatch):
    captured: list[tuple[Path, bool]] = []

    def fake_private(path: Path, *, create: bool) -> Path:
        captured.append((path, create))
        return path

    monkeypatch.setattr(_platform, "_user_private_dir", fake_private)
    monkeypatch.delenv("EPHEMDIR_DATA_DIR", raising=False)
    monkeypatch.delenv("EPHEMDIR_CONFIG_DIR", raising=False)

    monkeypatch.setattr(_platform.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert _platform.user_data_dir("app", create=False) == tmp_path / "local" / "app"
    assert _platform.user_config_dir("app", create=False) == tmp_path / "roaming" / "app"

    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.delenv("APPDATA")
    monkeypatch.setattr(_platform.Path, "home", lambda: tmp_path / "home")
    assert _platform.user_data_dir("app", create=False) == (
        tmp_path / "home" / "AppData" / "Local" / "app"
    )
    assert _platform.user_config_dir("app", create=False) == (
        tmp_path / "home" / "AppData" / "Roaming" / "app"
    )

    monkeypatch.setattr(_platform.sys, "platform", "darwin")
    assert _platform.user_data_dir("app", create=False) == (
        tmp_path / "home" / "Library" / "Application Support" / "app"
    )
    assert _platform.user_config_dir("app", create=False) == (
        tmp_path / "home" / "Library" / "Application Support" / "app"
    )

    monkeypatch.setattr(_platform.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert _platform.user_data_dir("app", create=False) == tmp_path / "xdg-data" / "app"
    assert _platform.user_config_dir("app", create=False) == tmp_path / "xdg-config" / "app"
    assert all(create is False for _, create in captured)


def test_platform_canonicalizes_only_macos_var_alias(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "darwin")

    assert _platform._canonical_private_dir_path(Path("/var/folders/x/data")) == Path(
        "/private/var/folders/x/data"
    )
    assert _platform._canonical_private_dir_path(Path("/tmp/link/data")) == Path(
        "/tmp/link/data"
    )

    monkeypatch.setattr(_platform.sys, "platform", "linux")
    assert _platform._canonical_private_dir_path(Path("/var/folders/x/data")) == Path(
        "/var/folders/x/data"
    )


def test_platform_boot_identity_and_time_probes(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "linux")
    monkeypatch.setattr(
        builtins,
        "open",
        lambda path, encoding=None: io.StringIO("boot-123\n"),
    )
    assert _platform.boot_session_id() == "boot-123"

    def fail_open(path, encoding=None):
        raise OSError("missing proc")

    monkeypatch.setattr(builtins, "open", fail_open)
    assert _platform.boot_session_id() is None

    monkeypatch.setattr(_platform.sys, "platform", "win32")
    monkeypatch.setattr(_platform, "_boot_session_id_windows", lambda: "win-7")
    assert _platform.boot_session_id() == "win-7"

    monkeypatch.setattr(_platform.sys, "platform", "darwin")
    assert _platform.boot_session_id() is None
    assert _platform.same_boot(None, 1.0) is True
    assert _platform.same_boot(100.0, 200.0) is True
    assert _platform.same_boot(100.0, 400.0) is False


def test_platform_boot_time_parsers(monkeypatch):
    monkeypatch.setattr(builtins, "open", lambda path, encoding=None: io.StringIO("10.5 99\n"))
    monkeypatch.setattr(_platform.time, "time", lambda: 110.5)
    assert _platform._boot_time_linux() == 100.0

    monkeypatch.setattr(builtins, "open", lambda path, encoding=None: io.StringIO("bad\n"))
    assert _platform._boot_time_linux() is None

    monkeypatch.setattr(_platform, "resolve_system_executable", lambda name: "/usr/sbin/sysctl")
    monkeypatch.setattr(_platform, "minimal_subprocess_env", lambda: {"PATH": "/usr/sbin"})
    monkeypatch.setattr(_platform, "stable_subprocess_cwd", lambda: "/")

    def fake_check_output(command, **kwargs):
        assert command == ["/usr/sbin/sysctl", "-n", "kern.boottime"]
        return "{ sec = 1700000000, usec = 123456 }\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert _platform._boot_time_macos() == 1_700_000_000.0

    monkeypatch.setattr(_platform, "resolve_system_executable", lambda name: None)
    assert _platform._boot_time_macos() is None
    monkeypatch.setattr(_platform, "resolve_system_executable", lambda name: "/usr/sbin/sysctl")
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda command, **kwargs: (_ for _ in ()).throw(subprocess.SubprocessError),
    )
    assert _platform._boot_time_macos() is None

    class Kernel32:
        @staticmethod
        def GetTickCount64():
            return 2_000

    class Windll:
        kernel32 = Kernel32()

    monkeypatch.setattr(ctypes, "windll", Windll(), raising=False)
    monkeypatch.setattr(_platform.time, "time", lambda: 10.0)
    assert _platform._boot_time_windows() == 8.0

    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert _platform._boot_time_windows() is None


def test_trusted_exec_windows_directory_and_env(monkeypatch, tmp_path):
    calls: list[int] = []

    def get_system_directory(buffer, size):
        calls.append(size)
        value = "C:\\Windows\\System32"
        if size < 300:
            return 300
        buffer.value = value
        return len(value)

    class Kernel32:
        GetSystemDirectoryW = staticmethod(get_system_directory)

    monkeypatch.setattr(_trusted_exec.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, use_last_error=True: Kernel32(),
        raising=False,
    )

    assert _trusted_exec._windows_system_directory() == Path("C:\\Windows\\System32")
    assert calls == [260, 301]

    monkeypatch.setattr(_trusted_exec, "_windows_system_directory", lambda: None)
    assert _trusted_exec.trusted_system_dirs() == ()

    system32 = tmp_path / "Windows" / "System32"
    monkeypatch.setattr(_trusted_exec, "trusted_system_dirs", lambda: (system32,))
    monkeypatch.setenv("PYTHONPATH", "attacker")
    monkeypatch.setenv("HOME", "/home/example")
    env = _trusted_exec.minimal_subprocess_env()
    assert env["PATH"] == str(system32)
    assert env["SystemRoot"] == str(system32.parent)
    assert env["WINDIR"] == str(system32.parent)
    assert "PYTHONPATH" not in env


def test_trusted_exec_rejects_untrusted_components(tmp_path, monkeypatch):
    assert _trusted_exec._component_is_trusted(_stat_result(stat.S_IFREG | 0o755), None) is False
    assert _trusted_exec._component_is_trusted(_stat_result(stat.S_IFDIR | 0o777), None) is False
    assert (
        _trusted_exec._component_is_trusted(_stat_result(stat.S_IFDIR | 0o755, uid=99999), 1)
        is False
    )
    assert _trusted_exec._component_is_trusted(_stat_result(stat.S_IFDIR | 0o755), None) is True

    monkeypatch.setattr(_trusted_exec.os, "name", "nt")
    assert _trusted_exec._directory_chain_is_trusted(tmp_path, None) is True

    monkeypatch.setattr(_trusted_exec.os, "name", "posix")
    monkeypatch.setattr(
        _trusted_exec.os.path,
        "realpath",
        lambda path: (_ for _ in ()).throw(OSError("bad realpath")),
    )
    assert _trusted_exec._directory_chain_is_trusted(tmp_path, None) is False

    monkeypatch.setattr(_trusted_exec.os.path, "realpath", os.path.realpath)
    monkeypatch.setattr(
        _trusted_exec.os,
        "lstat",
        lambda path: (_ for _ in ()).throw(OSError("bad lstat")),
    )
    assert _trusted_exec._directory_chain_is_trusted(tmp_path / "bin", None) is False


def test_trusted_exec_resolver_skips_bad_candidates(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    writable_dir = tmp_path / "writable"
    writable_dir.mkdir()
    bad_file_dir = tmp_path / "bad-file"
    bad_file_dir.mkdir()
    for directory in (trusted, writable_dir, bad_file_dir):
        tool = directory / "tool"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
    writable_dir.chmod(0o777)
    (bad_file_dir / "tool").chmod(0o775)
    (trusted / "tool").chmod(0o755)

    real_stat = os.stat
    real_lstat = os.lstat

    def clean_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        values = list(result)
        values[4] = os.geteuid() if hasattr(os, "geteuid") else 0
        if str(path) != str(writable_dir) and str(path) != str(bad_file_dir / "tool"):
            values[0] = result.st_mode & ~0o022
        return os.stat_result(values)

    monkeypatch.setattr(_trusted_exec.os, "stat", clean_stat)
    monkeypatch.setattr(_trusted_exec.os, "lstat", lambda path: clean_stat(path))
    monkeypatch.setattr(
        _trusted_exec.os,
        "access",
        lambda path, mode: str(path).startswith(str(trusted)),
    )
    assert _trusted_exec.resolve_executable_in_dirs(
        "tool",
        (writable_dir, bad_file_dir, trusted),
    ) == str(trusted / "tool")

    monkeypatch.setattr(_trusted_exec.os, "lstat", real_lstat)


def test_trusted_exec_stable_cwd_prefers_existing_home(tmp_path, monkeypatch):
    monkeypatch.setattr(_trusted_exec.Path, "home", lambda: tmp_path)
    assert _trusted_exec.stable_subprocess_cwd() == str(tmp_path)


def test_doctor_directory_and_registry_diagnostics(tmp_path, monkeypatch):
    missing = _doctor._directory_check("data", tmp_path / "missing")
    assert missing.ok is True

    file_path = tmp_path / "not-dir"
    file_path.write_text("", encoding="utf-8")
    assert _doctor._directory_check("data", file_path).ok is False

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    shared_check = _doctor._directory_check("data", shared)
    assert shared_check.ok is False
    assert "chmod 700" in str(shared_check.hint)

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.setattr(_doctor.os, "getuid", lambda: foreign.stat().st_uid + 1, raising=False)
    assert "not owned" in _doctor._directory_check("data", foreign).message

    reg = Registry(path=tmp_path / "registry.json")
    reg.save({})
    monkeypatch.setattr(_doctor.os, "getuid", lambda: reg.path.stat().st_uid, raising=False)
    assert _doctor._diagnose_registry(reg.path) == 0
    assert _doctor._registry_check(reg).ok is True


def test_doctor_chain_and_registry_error_mapping(tmp_path, monkeypatch):
    ancestor_file = tmp_path / "file"
    ancestor_file.write_text("", encoding="utf-8")
    assert "not a directory" in _doctor._directory_chain_problem(ancestor_file / "child")

    class FakeRegistry:
        path = tmp_path / "registry.json"

    for exc, hint in (
        (FileNotFoundError(), "not created yet"),
        (_doctor.RegistryTooLargeError("too big"), "rotate"),
        (_doctor.RegistryFormatError("bad format"), "trust"),
        (ValueError("bad json"), "repair"),
    ):
        monkeypatch.setattr(
            _doctor,
            "_diagnose_registry",
            lambda path, exc=exc: (_ for _ in ()).throw(exc),
        )
        check = _doctor._registry_check(FakeRegistry())
        assert check.ok is (isinstance(exc, FileNotFoundError))
        assert hint in (check.message + " " + str(check.hint))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version": 2, "entries": {"/tmp/x": {"bad": true}}}',
        b'{"schema_version": 2, "entries": []}',
        b'{"schema_version": 999, "entries": {}}',
        b'{"value": NaN}',
    ],
)
def test_doctor_diagnose_registry_rejects_malformed_json_payloads(tmp_path, raw):
    path = tmp_path / "registry.json"
    path.write_bytes(raw)
    path.chmod(0o600)

    with pytest.raises((ValueError, _doctor.RegistryFormatError)):
        _doctor._diagnose_registry(path)


def test_doctor_run_uses_boot_time_fallback(tmp_path, monkeypatch):
    registry = Registry(path=tmp_path / "registry.json")
    monkeypatch.setattr(_doctor, "user_data_dir", lambda create=False: tmp_path / "missing-data")
    monkeypatch.setattr(
        _doctor,
        "user_config_dir",
        lambda create=False: tmp_path / "missing-config",
    )
    monkeypatch.setattr(_doctor, "boot_session_id", lambda: None)
    monkeypatch.setattr(_doctor, "boot_time", lambda: 1.0)

    checks = _doctor.run_doctor(registry=registry)

    assert next(check for check in checks if check.name == "boot-id").ok is True


def test_doctor_env_source_diagnostics(monkeypatch, tmp_path):
    monkeypatch.delenv("EPHEMDIR_DATA_DIR", raising=False)
    monkeypatch.delenv("EPHEMDIR_CONFIG_DIR", raising=False)
    monkeypatch.setattr(_doctor.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    assert _doctor._env_value("XDG_DATA_HOME") == f"XDG_DATA_HOME={tmp_path / 'xdg-data'}"
    assert _doctor._data_dir_source() == f"XDG_DATA_HOME={tmp_path / 'xdg-data'}"
    assert _doctor._config_dir_source() == f"XDG_CONFIG_HOME={tmp_path / 'xdg-config'}"

    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert _doctor._env_value("XDG_DATA_HOME") == "XDG_DATA_HOME=unset"
    assert _doctor._data_dir_source() == "platform default"
    assert _doctor._config_dir_source() == "platform default"

    monkeypatch.setattr(_doctor.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert _doctor._data_dir_source() == f"platform env={tmp_path / 'local'}"
    assert _doctor._config_dir_source() == f"platform env={tmp_path / 'roaming'}"


def test_size_parser_and_budget_edges(tmp_path, monkeypatch):
    assert _size.parse_size("1 KiB") == 1024
    with pytest.raises(ValueError, match="could not parse"):
        _size.parse_size("NaN")

    assert _size.measure_tree(tmp_path / "missing").error

    monkeypatch.setattr(_size.sys, "platform", "linux")
    monkeypatch.setattr(_size, "_mount_id_for_fd", lambda fd: None)
    root = tmp_path / "root"
    root.mkdir()
    assert _size.measure_tree(root).error == "mount-id-unavailable"

    monkeypatch.setattr(_size.sys, "platform", "darwin")
    monkeypatch.setattr(_size, "_mount_id_for_fd", lambda fd: None)
    (root / ".ephemdir").write_text("marker", encoding="utf-8")
    (root / "a").write_text("a", encoding="utf-8")
    assert _size.measure_tree(root, max_entries=0).error == "scan-budget-exceeded"

    child = root / "child"
    child.mkdir()
    assert _size.measure_tree(root, max_depth=0).error == "scan-budget-exceeded"

    times = iter([100.0, 101.0])
    monkeypatch.setattr(_size.time, "monotonic", lambda: next(times))
    assert _size.measure_tree(root, max_seconds=0).error == "scan-budget-exceeded"


def test_size_measure_tree_identity_and_disappearing_entries(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    disappearing = root / "gone"
    disappearing.write_text("x", encoding="utf-8")
    stable = root / "stable"
    stable.write_text("x", encoding="utf-8")
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        if path == "gone":
            raise FileNotFoundError
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(_size.sys, "platform", "darwin")
    monkeypatch.setattr(_size.os, "stat", fake_stat)
    result = _size.measure_tree(root)
    assert result.complete is True
    assert result.error is None

    monkeypatch.setattr(_size.os, "stat", real_stat)
    if hasattr(os, "link"):
        first = root / "hard-a"
        second = root / "hard-b"
        first.write_text("same", encoding="utf-8")
        os.link(first, second)
        assert _size.measure_tree(root).complete is True


def test_size_measure_tree_reports_identity_changes(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    real_fstat = os.fstat
    seen_child = False

    def fake_fstat(fd):
        nonlocal seen_child
        info = real_fstat(fd)
        if seen_child:
            values = list(info)
            values[1] = info.st_ino + 1000
            return os.stat_result(values)
        seen_child = True
        return info

    monkeypatch.setattr(_size.sys, "platform", "darwin")
    monkeypatch.setattr(_size.os, "fstat", fake_fstat)
    assert _size.measure_tree(root).error == "identity-changed"


def test_menu_non_tty_and_interrupt_paths(monkeypatch):
    class NonTty:
        def isatty(self):
            return False

    printed: list[str] = []
    monkeypatch.setattr("sys.stdin", NonTty())
    monkeypatch.setattr("sys.stdout", NonTty())

    assert run_menu(lambda argv: 0, print_func=lambda *args: printed.append(" ".join(args))) == 2
    assert printed == ["ephemdir menu requires an interactive terminal"]

    printed.clear()
    assert (
        run_menu(
            lambda argv: 0,
            input_func=lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt),
            print_func=lambda *args: printed.append(" ".join(args)),
        )
        == 0
    )
    assert "" in printed

    answers = iter(["3"])
    assert (
        run_menu(
            lambda argv: 0,
            input_func=lambda prompt: next(answers)
            if "Sweep now" not in prompt
            else (_ for _ in ()).throw(KeyboardInterrupt),
            print_func=lambda *args: None,
        )
        == 0
    )


def test_naming_rejects_generated_unsafe_components(monkeypatch):
    monkeypatch.setattr(_naming.secrets, "choice", lambda values: "..")
    with pytest.raises(ValueError, match="safe path component"):
        _naming.clean_name(words=1)

    monkeypatch.setattr(_naming.secrets, "choice", lambda values: "name")
    monkeypatch.setattr(_naming.secrets, "token_hex", lambda size: "token")
    monkeypatch.setattr(_naming.os.path, "isabs", lambda value: True)
    with pytest.raises(ValueError, match="safe path component"):
        _naming.funny_name(words=1)


def test_mount_probe_error_branches(tmp_path, monkeypatch):
    monkeypatch.setattr(_mounts.sys, "platform", "darwin")
    assert _mounts.mount_id_for_fd(1) is None

    monkeypatch.setattr(_mounts.sys, "platform", "linux")
    monkeypatch.setattr(_mounts, "_LIBC", None)
    monkeypatch.setattr(_mounts, "_STATX_UNAVAILABLE", False)
    monkeypatch.setattr(
        _mounts.ctypes,
        "CDLL",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no libc")),
    )
    assert _mounts.linux_mount_id_statx(1) is None

    class NoStatx:
        pass

    monkeypatch.setattr(_mounts, "_LIBC", NoStatx())
    monkeypatch.setattr(_mounts, "_STATX_UNAVAILABLE", False)
    assert _mounts.linux_mount_id_statx(1) is None

    fdinfo = tmp_path / "fdinfo"
    fdinfo.write_text("mnt_id:\tnot-decimal\n", encoding="ascii")
    real_open = os.open
    monkeypatch.setattr(_mounts.os, "open", lambda path, flags: real_open(fdinfo, flags))
    assert _mounts.linux_mount_id_fdinfo(1) is None

    monkeypatch.setattr(_mounts.os, "readlink", lambda path: "/not-mounted")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("bad line\n", encoding="utf-8")
    monkeypatch.setattr(_mounts.os, "open", lambda path, flags: real_open(mountinfo, flags))
    assert _mounts.linux_mount_id_mountinfo(1) is None


def test_security_walk_trusted_directory_edges(tmp_path, monkeypatch):
    with pytest.raises(PermissionError, match="absolute"):
        _security.walk_trusted_directory(
            Path("relative"),
            create_missing=False,
        )

    parent = tmp_path / "parent"
    missing = parent / "missing"
    parent.mkdir()
    parent.chmod(0o700)
    fd = _security.walk_trusted_directory(missing, create_missing=True)
    os.close(fd)
    assert missing.is_dir()

    with pytest.raises(PermissionError, match="final says no"):
        _security.walk_trusted_directory(
            parent,
            create_missing=False,
            final_validator=lambda path, info: "final says no",
        )

    file_parent = tmp_path / "file-parent"
    file_parent.write_text("", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        _security.walk_trusted_directory(file_parent / "child", create_missing=False)

    monkeypatch.setattr(_security, "walk_trusted_directory", lambda *args, **kwargs: None)
    with pytest.raises(FileNotFoundError):
        _security.open_trusted_directory(parent)
    with pytest.raises(FileNotFoundError):
        _security.open_private_directory(parent, create=False)
