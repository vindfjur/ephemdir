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

Managing directories later (by name, unique prefix or path)::

    from ephemdir import keep, extend, remove

    keep("brave-otter")              # make it permanent
    extend("brave-otter", "2h")      # fresh lifetime from now
    remove("brave-otter")            # delete it right away
"""

from __future__ import annotations

from .core import (
    EphemeralDirectory,
    extend,
    keep,
    prune,
    registered,
    remove,
    resolve,
    sweep,
    tempdir,
)

__version__ = "0.3.0"
__author__ = "vindfjur"

__all__ = [
    "tempdir",
    "sweep",
    "registered",
    "keep",
    "extend",
    "remove",
    "resolve",
    "prune",
    "EphemeralDirectory",
    "__version__",
    "__author__",
]
