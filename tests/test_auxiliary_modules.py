"""Focused tests for descriptor, UX helper and decision modules."""

from __future__ import annotations

import ctypes
import os
import runpy
import stat
from pathlib import Path

import pytest

from ephemdir import _mounts, _security, _size, _trusted_exec
from ephemdir._backends import default_backend, windows_api
from ephemdir._backends import posix as posix_module
from ephemdir._backends import windows as windows_module
from ephemdir._backends.posix import PosixBackend
from ephemdir._backends.windows import WindowsBackend
from ephemdir._completion import completion_script
from ephemdir._menu import run_menu
from ephemdir._policy import CleanupPolicy, SweepMode, decide_cleanup


def _stat(mode: int, uid: int | None = None) -> os.stat_result:
    uid_value = os.geteuid() if uid is None and hasattr(os, "geteuid") else (uid or 0)
    return os.stat_result((mode, 1, 1, 1, uid_value, 0, 0, 0, 0, 0))


@pytest.mark.parametrize(
    ("shell", "needle"),
    [
        ("bash", "complete -F _ephemdir_complete ephemdir"),
        ("zsh", "#compdef ephemdir"),
        ("fish", "complete -c ephemdir"),
        ("powershell", "Register-ArgumentCompleter"),
        ("pwsh", "Register-ArgumentCompleter"),
    ],
)
def test_completion_scripts_cover_supported_shells(shell: str, needle: str):
    script = completion_script(shell)

    assert needle in script
    assert "install-service" in script


def test_completion_rejects_unknown_shell():
    with pytest.raises(ValueError, match="shell must be"):
        completion_script("tcsh")


def test_python_module_entrypoint_delegates_to_cli(monkeypatch):
    import ephemdir.cli as cli_module

    monkeypatch.setattr(cli_module, "main", lambda: 23)

    with pytest.raises(SystemExit) as error:
        runpy.run_module("ephemdir.__main__", run_name="__main__")

    assert error.value.code == 23


def test_windows_api_boundary_imports():
    assert windows_api.__all__ == []


def test_menu_dispatches_all_choices_and_confirmed_sweep():
    answers = iter(["1", "2", "3", "y", "4", "wat", "q"])
    calls: list[list[str]] = []
    printed: list[str] = []

    result = run_menu(
        lambda argv: calls.append(argv) or 0,
        input_func=lambda prompt: next(answers),
        print_func=lambda *args, **kwargs: printed.append(" ".join(map(str, args))),
    )

    assert result == 0
    assert calls == [["list"], ["new"], ["sweep", "--dry-run"], ["sweep"], ["doctor"]]
    assert "unknown choice" in printed


def test_menu_handles_eof_without_dispatch():
    result = run_menu(
        lambda argv: pytest.fail(f"unexpected dispatch: {argv}"),
        input_func=lambda prompt: (_ for _ in ()).throw(EOFError),
        print_func=lambda *args, **kwargs: None,
    )

    assert result == 0


def test_default_backend_selects_platform_backend(monkeypatch):
    monkeypatch.setattr("ephemdir._backends.os.name", "posix")
    assert isinstance(default_backend(), PosixBackend)

    monkeypatch.setattr("ephemdir._backends.os.name", "nt")
    assert isinstance(default_backend(), WindowsBackend)


def test_posix_backend_descriptor_and_unsupported_operations(tmp_path, monkeypatch):
    backend = PosixBackend()

    assert backend.capabilities().safe_delete is (os.name == "posix")
    assert backend.validate_parent(tmp_path).ok is True
    assert backend.validate_parent(tmp_path / "missing").ok is False

    monkeypatch.setattr(posix_module.os, "name", "nt")
    assert backend.capabilities().safe_delete is False
    assert "POSIX" in backend.capabilities().reason

    with pytest.raises(NotImplementedError):
        backend.create_owned_directory(tmp_path, "x", "0" * 32)
    with pytest.raises(NotImplementedError):
        backend.probe_ownership(tmp_path, {})
    with pytest.raises(NotImplementedError):
        backend.claim(tmp_path, {}, "staging")
    with pytest.raises(NotImplementedError):
        backend.delete_claimed_tree(None, None)  # type: ignore[arg-type]


def test_windows_backend_descriptor_and_disabled_operations(tmp_path, monkeypatch):
    backend = WindowsBackend()

    assert backend.capabilities().safe_delete is False
    assert "not enabled" in str(backend.capabilities().reason)
    assert backend.validate_parent(tmp_path).ok is True
    assert backend.validate_parent(tmp_path / "missing").ok is False
    assert backend.probe_ownership(tmp_path, {}).status == "unsupported"
    assert backend.delete_claimed_tree(None, None).removed is False  # type: ignore[arg-type]

    monkeypatch.setattr(
        windows_module,
        "measure_tree",
        lambda path, limit=None: f"measured:{path.name}:{limit}",
    )
    assert backend.measure_tree(tmp_path, {}, 7, None) == f"measured:{tmp_path.name}:7"

    with pytest.raises(NotImplementedError):
        backend.create_owned_directory(tmp_path, "x", "0" * 32)
    with pytest.raises(NotImplementedError):
        backend.claim(tmp_path, {}, "staging")


def test_policy_combines_reasons_and_blockers(monkeypatch):
    monkeypatch.setattr("ephemdir._policy.sys.platform", "linux")
    entry = {
        "expires_at": 10.0,
        "remove_on_restart": True,
        "boot_id": "old",
        "cleanup_policy": CleanupPolicy.NEXT_SWEEP.value,
        "max_size": 5,
        "backend": "windows",
        "platform": "win32",
    }

    decision = decide_cleanup(
        Path("/tmp/x"),
        entry,
        now=20.0,
        current_boot=None,
        current_boot_id="new",
        mode=SweepMode.FORCE,
        path_state="unknown",
        ownership="unverified",
        parent_error="shared parent",
        in_use=None,
        measured_size_bytes=6,
    )

    assert decision.status == "blocked"
    assert decision.reasons == ("forced", "expired", "restarted", "next-sweep", "oversize")
    assert decision.blockers == (
        "ownership-unverified",
        "unsafe-parent",
        "unsupported-backend",
        "foreign-platform",
        "in-use-unknown",
    )
    assert decision.destructive_allowed is False


def test_policy_size_unknown_and_scan_budget_blockers():
    entry = {"remove_on_restart": False, "max_size": 10}

    unknown = decide_cleanup(
        Path("/tmp/x"),
        entry,
        now=0.0,
        current_boot=None,
        current_boot_id=None,
        mode=SweepMode.MAINTENANCE,
        path_state="present",
        ownership="owned",
        parent_error=None,
        measured_size_bytes=None,
    )
    assert unknown.blockers == ("size-unknown",)

    incomplete = decide_cleanup(
        Path("/tmp/x"),
        entry,
        now=0.0,
        current_boot=None,
        current_boot_id=None,
        mode=SweepMode.MAINTENANCE,
        path_state="present",
        ownership="owned",
        parent_error=None,
        measured_size_bytes=10,
        size_complete=False,
    )
    assert incomplete.blockers == ("scan-budget-exceeded",)


def test_policy_darwin_boot_time_fallback(monkeypatch):
    monkeypatch.setattr("ephemdir._policy.sys.platform", "darwin")
    entry = {"remove_on_restart": True, "boot_time": 1.0}

    decision = decide_cleanup(
        Path("/tmp/x"),
        entry,
        now=0.0,
        current_boot=2.0,
        current_boot_id=None,
        mode=SweepMode.FULL,
        path_state="present",
        ownership="owned",
        parent_error=None,
        same_boot_func=lambda created, current: False,
    )

    assert decision.reasons == ("restarted",)
    assert decision.destructive_allowed is True


def test_security_component_policy_helpers():
    current = Path("/tmp/current")
    assert _security.trusted_component_error(current, _stat(stat.S_IFREG | 0o600))
    assert _security.trusted_component_error(current, _stat(stat.S_IFDIR | 0o777))

    foreign_uid = os.geteuid() + 1 if hasattr(os, "geteuid") else 99999
    assert _security.trusted_component_error(
        current,
        _stat(stat.S_IFDIR | 0o755, uid=foreign_uid),
    )
    assert _security.trusted_component_error(current, _stat(stat.S_IFDIR | 0o1777)) is None

    assert _security.trusted_final_parent_error(
        current,
        _stat(stat.S_IFDIR | 0o1777, uid=foreign_uid),
    )
    assert _security.private_directory_error(current, _stat(stat.S_IFDIR | 0o755))
    assert _security.private_directory_error(current, _stat(stat.S_IFDIR | 0o700)) is None


@pytest.mark.parametrize(
    "bad",
    [
        True,
        -1,
        2**63,
        object(),
        "",
        "wat",
        "-1",
        "1XB",
        "1000000000000000000000000000000TB",
    ],
)
def test_parse_size_rejects_invalid_limits(bad):
    with pytest.raises((TypeError, ValueError)):
        _size.parse_size(bad)


def test_measure_tree_reports_unsupported_backends(tmp_path, monkeypatch):
    monkeypatch.setattr(_size.os, "name", "nt")
    assert _size.measure_tree(tmp_path).error == "unsupported-backend"

    monkeypatch.setattr(_size.os, "name", "posix")
    monkeypatch.setattr(_size.os, "supports_fd", frozenset())
    assert _size.measure_tree(tmp_path).error == "unsupported-filesystem"


def test_allocated_size_prefers_blocks_when_available():
    class WithBlocks:
        st_blocks = 3
        st_size = 99

    class WithoutBlocks:
        st_blocks = 0
        st_size = 17

    assert _size._allocated_size(WithBlocks()) == 1536
    assert _size._allocated_size(WithoutBlocks()) == 17


def test_linux_mount_id_statx_reads_successful_probe(monkeypatch):
    class FakeLibc:
        def statx(self, fd, path, flags, mask, output):
            assert fd == 9
            buffer = ctypes.cast(output, ctypes.POINTER(_mounts._Statx)).contents
            buffer.stx_mask = _mounts._STATX_MNT_ID
            buffer.stx_mnt_id = 456
            return 0

    monkeypatch.setattr(_mounts.sys, "platform", "linux")
    monkeypatch.setattr(_mounts, "_LIBC", FakeLibc())
    monkeypatch.setattr(_mounts, "_STATX_UNAVAILABLE", False)

    assert _mounts.linux_mount_id_statx(9) == 456


def test_linux_mount_id_fdinfo_reads_decimal_mnt_id(tmp_path, monkeypatch):
    fdinfo = tmp_path / "fdinfo"
    fdinfo.write_text("pos:\t0\nmnt_id:\t77\nflags:\t0\n", encoding="ascii")
    real_open = os.open

    def fake_open(path, flags):
        assert path == "/proc/self/fdinfo/12"
        return real_open(fdinfo, flags)

    monkeypatch.setattr(_mounts.sys, "platform", "linux")
    monkeypatch.setattr(_mounts.os, "open", fake_open)

    assert _mounts.linux_mount_id_fdinfo(12) == 77


def test_linux_mount_id_mountinfo_uses_deepest_decoded_mount(tmp_path, monkeypatch):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "10 1 0:1 / /mnt rw - tmpfs tmpfs rw\n"
        "20 1 0:2 / /mnt/space\\040child rw - tmpfs tmpfs rw\n"
        "30 1 0:3 / /mnt/space\\040child/deep rw - tmpfs tmpfs rw\n",
        encoding="utf-8",
    )
    real_open = os.open

    def fake_open(path, flags):
        assert path == "/proc/self/mountinfo"
        return real_open(mountinfo, flags)

    monkeypatch.setattr(_mounts.sys, "platform", "linux")
    monkeypatch.setattr(
        _mounts.os,
        "readlink",
        lambda path: "/mnt/space child/deep/file.txt (deleted)",
    )
    monkeypatch.setattr(_mounts.os, "open", fake_open)

    assert _mounts.linux_mount_id_mountinfo(14) == 30


def test_mount_id_for_fd_uses_probe_chain(monkeypatch):
    calls: list[str] = []

    def statx(fd):
        calls.append("statx")
        return None

    def fdinfo(fd):
        calls.append("fdinfo")
        return 88

    def mountinfo(fd):
        calls.append("mountinfo")
        return 99

    monkeypatch.setattr(_mounts.sys, "platform", "linux")
    monkeypatch.setattr(_mounts, "linux_mount_id_statx", statx)
    monkeypatch.setattr(_mounts, "linux_mount_id_fdinfo", fdinfo)
    monkeypatch.setattr(_mounts, "linux_mount_id_mountinfo", mountinfo)

    assert _mounts.mount_id_for_fd(3) == 88
    assert calls == ["statx", "fdinfo"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd semantics required")
def test_verify_same_mount_reports_precise_boundary_errors(tmp_path):
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        required = _mounts.MountBoundary(
            root_dev=info.st_dev,
            root_mount_id=10,
            mount_id_required=True,
        )
        _mounts.verify_same_mount(fd, required, mount_id_func=lambda child_fd: 10)

        with pytest.raises(_mounts.MountBoundaryError) as unavailable:
            _mounts.verify_same_mount(
                fd,
                _mounts.MountBoundary(info.st_dev, None, True),
                mount_id_func=lambda child_fd: 10,
            )
        assert unavailable.value.code == "mount-id-unavailable"

        with pytest.raises(_mounts.MountBoundaryError) as crossed_mount:
            _mounts.verify_same_mount(fd, required, mount_id_func=lambda child_fd: 11)
        assert crossed_mount.value.code == "unsupported-filesystem"

        with pytest.raises(_mounts.MountBoundaryError) as crossed_device:
            _mounts.verify_same_mount(
                fd,
                _mounts.MountBoundary(info.st_dev + 1, None, False),
            )
        assert crossed_device.value.code == "unsupported-filesystem"
    finally:
        os.close(fd)


def test_trusted_exec_env_and_cwd_are_minimal(tmp_path, monkeypatch):
    trusted_bin = tmp_path / "bin"
    monkeypatch.setenv("HOME", "/home/example")
    monkeypatch.setenv("PYTHONPATH", "attacker")
    monkeypatch.setattr(_trusted_exec, "trusted_system_dirs", lambda: (trusted_bin,))
    monkeypatch.setattr(_trusted_exec.sys, "platform", "linux")

    env = _trusted_exec.minimal_subprocess_env()

    assert env["HOME"] == "/home/example"
    assert env["PATH"] == str(trusted_bin)
    assert "PYTHONPATH" not in env

    monkeypatch.setattr(
        _trusted_exec.Path,
        "home",
        lambda: (_ for _ in ()).throw(RuntimeError("no home")),
    )
    assert _trusted_exec.stable_subprocess_cwd() == os.sep
