"""Regression tests for lsof output parsing in the in-use probe.

lsof exits non-zero with ``+D`` even when it finds open files, so detection must
be based on the output, not the return code.
"""

from __future__ import annotations

import subprocess

import pytest

from ephemdir import _inuse

_HEADER = "COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF     NODE NAME"
_OPEN_FILE = "Python  12108 nikita    3w   REG   1,17        2 24359423 /tmp/d/x.log"


def _fake_run(stdout: str, returncode: int):
    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")
    return _run


def test_open_file_detected_despite_nonzero_exit(monkeypatch):
    # The real macOS behaviour: a file is listed but lsof exits 1.
    monkeypatch.setattr(_inuse, "resolve_system_executable", lambda name: "/usr/bin/lsof")
    monkeypatch.setattr(subprocess, "run", _fake_run(f"{_HEADER}\n{_OPEN_FILE}\n", 1))
    assert _inuse._lsof_in_use("/tmp/d") is True


def test_no_open_files_reported(monkeypatch):
    monkeypatch.setattr(_inuse, "resolve_system_executable", lambda name: "/usr/bin/lsof")
    monkeypatch.setattr(subprocess, "run", _fake_run("", 1))
    assert _inuse._lsof_in_use("/tmp/d") is False


def test_missing_lsof_reports_unknown(monkeypatch):
    # A failed probe must say "could not tell" (None), never a confident False:
    # with keep_while_in_use enabled the sweep then defers instead of deleting.
    def _raise(*args, **kwargs):
        raise FileNotFoundError("lsof")
    monkeypatch.setattr(_inuse, "resolve_system_executable", lambda name: "/usr/bin/lsof")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert _inuse._lsof_in_use("/tmp/d") is None


@pytest.mark.skipif(_inuse.sys.platform == "win32", reason="lsof is POSIX-only")
def test_real_open_file_is_detected(tmp_path):
    import shutil

    if shutil.which("lsof") is None:
        pytest.skip("lsof not available")

    target = tmp_path / "busy"
    target.mkdir()
    handle = open(target / "x.log", "w")
    try:
        handle.write("hold")
        handle.flush()
        assert _inuse.is_in_use(target) is True
    finally:
        handle.close()
    assert _inuse.is_in_use(target) is False
