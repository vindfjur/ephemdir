"""Best-effort detection of whether a directory is currently in use.

Used to optionally protect a directory from deletion while a process still has
files open inside it. Detection is tri-state: ``True`` (something is holding a
file open), ``False`` (verified free) or ``None`` (could not tell). The caller
decides what to do with ``None``; with ``keep_while_in_use`` enabled the sweep
errs on the side of keeping the data and defers the deletion.
"""

from __future__ import annotations

import os
import subprocess
import sys

from ._trusted_exec import minimal_subprocess_env, resolve_system_executable, stable_subprocess_cwd

__all__ = ["is_in_use"]

# Upper bound for the external probe so a slow ``lsof`` never stalls a sweep.
_PROBE_TIMEOUT_SECONDS = 10.0


def is_in_use(path: os.PathLike[str] | str) -> bool | None:
    """Report whether any process appears to hold a file open under ``path``.

    On Linux and macOS this shells out to ``lsof``. Unsupported platforms
    return ``None`` rather than guessing that a directory is free.

    Returns ``None`` whenever the probe cannot answer (missing tool, timeout,
    permissions) — never a confident ``False`` that was not actually verified.
    """
    if sys.platform == "win32":
        return None
    return _lsof_in_use(str(path))


def _lsof_in_use(path: str) -> bool | None:
    """Probe ``path`` with ``lsof +D`` and report whether any file is open.

    ``lsof``'s exit code is unreliable with ``+D`` -- scanning a subtree can emit
    warnings that make it exit non-zero even when it did find open files -- so we
    inspect the output instead: any line past the header means a process holds a
    file open somewhere under ``path``.
    """
    executable = resolve_system_executable("lsof")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "+D", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            env=minimal_subprocess_env(),
            cwd=stable_subprocess_cwd(),
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None  # could not tell -- let the caller decide, do not fail open
    data_lines = [line for line in result.stdout.splitlines() if line.strip()]
    # The first line is the "COMMAND PID USER ..." header.
    return len(data_lines) > 1
