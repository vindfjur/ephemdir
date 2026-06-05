"""ephemdir -- create temporary directories that clean themselves up.

Directories are removed automatically once their lifetime expires or after the
machine restarts. Names are playful two-word slugs (``brave-otter``) instead of
dull ones like ``tmp_data``.

Basic usage::

    from ephemdir import tempdir

    work = tempdir()                 # lives until the next restart
    work = tempdir(lifetime="2h")    # also expires after two hours
    (work.path / "data.txt").write_text("hello")

    with tempdir() as work:          # removed automatically on block exit
        ...
"""

from __future__ import annotations

from .core import EphemeralDirectory, registered, sweep, tempdir

__version__ = "0.1.0"
__author__ = "vindfjur"

__all__ = [
    "tempdir",
    "sweep",
    "registered",
    "EphemeralDirectory",
    "__version__",
    "__author__",
]
