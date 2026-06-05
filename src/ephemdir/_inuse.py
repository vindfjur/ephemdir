"""Best-effort detection of whether a directory is currently in use.

Used to optionally protect a directory from deletion while a process still has
files open inside it. Detection is deliberately conservative: when we cannot
tell, we report "not in use" so cleanup is never blocked indefinitely.
"""

from __future__ import annotations

import os
import subprocess
import sys

__all__ = ["is_in_use"]

# Upper bound for the external probe so a slow ``lsof`` never stalls a sweep.
_PROBE_TIMEOUT_SECONDS = 10.0


def is_in_use(path: os.PathLike[str] | str) -> bool:
    """Return ``True`` if any process appears to hold a file open under ``path``.

    On Linux and macOS this shells out to ``lsof``. On Windows there is no
    reliable standard-library equivalent, so we conservatively return ``False``
    and let the transactional delete in :func:`ephemdir.core._delete_tree` detect
    locks instead (an open file makes the atomic rename fail there).
    Any error (missing tool, timeout, permissions) is treated as "not in use".
    """
    if sys.platform == "win32":
        return False
    return _lsof_in_use(str(path))


def _lsof_in_use(path: str) -> bool:
    """Probe ``path`` with ``lsof +D`` and report whether any file is open.

    ``lsof``'s exit code is unreliable with ``+D`` -- scanning a subtree can emit
    warnings that make it exit non-zero even when it did find open files -- so we
    inspect the output instead: any line past the header means a process holds a
    file open somewhere under ``path``.
    """
    try:
        result = subprocess.run(
            ["lsof", "+D", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False
    data_lines = [line for line in result.stdout.splitlines() if line.strip()]
    # The first line is the "COMMAND PID USER ..." header.
    return len(data_lines) > 1
