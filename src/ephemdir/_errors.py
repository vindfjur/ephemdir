"""Public exception hierarchy for ephemdir (0.8.0 freeze candidate).

This is the freeze-candidate hierarchy; the mapping of raise sites onto it is
**incremental across the 0.8 rc cycle**. Registry errors, target resolution
(not-found/ambiguous, current-directory), platform refusals in ``tempdir``/
``remove`` and the deletion-guard refusal are mapped already; the remaining
lost-race ``LookupError`` and refusal ``OSError`` sites are mapped during
method stabilisation. Until that lands, ``except EphemdirError`` is NOT yet
guaranteed to catch every deliberate ephemdir error — catch the documented
builtin bases for exhaustive handling. Compatibility with the exceptions
callers already handle is preserved through multiple inheritance:

- registry errors remain :class:`RuntimeError` (via :class:`RegistryError`);
- target-selection errors remain :class:`LookupError` (via :class:`TargetError`);
- platform and safety refusals remain :class:`OSError`.

Invalid argument values keep raising plain :class:`TypeError`/
:class:`ValueError` — those are input bugs, not ephemdir conditions.
"""

from __future__ import annotations

__all__ = [
    "EphemdirError",
    "RegistryError",
    "SafetyError",
    "TargetError",
    "UnsupportedPlatformError",
]


class EphemdirError(Exception):
    """Freeze-candidate base for public ephemdir errors.

    Raise-site mapping onto this hierarchy is incremental across the 0.8
    cycle (see the module docstring), so catching this class is not yet
    guaranteed to be exhaustive.
    """


class RegistryError(EphemdirError, RuntimeError):
    """Base class for persistent-registry problems (trust, format, access)."""


class TargetError(EphemdirError, LookupError):
    """A directory target could not be resolved (not found or ambiguous).

    Subclasses :class:`LookupError`, so existing ``except LookupError``
    handlers keep working.
    """


class UnsupportedPlatformError(EphemdirError, OSError):
    """The platform lacks the safe primitives ephemdir requires.

    Subclasses :class:`OSError` (typically with ``errno.ENOTSUP``), so existing
    ``except OSError`` handlers keep working.
    """


class SafetyError(EphemdirError, OSError):
    """A destructive operation was refused because it would be unsafe.

    Raised for explicit refusals such as the deletion guard. Subclasses
    :class:`OSError`, so existing ``except OSError`` handlers keep working.
    """
