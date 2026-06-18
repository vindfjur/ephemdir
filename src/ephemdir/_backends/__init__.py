"""Platform backend selection."""

from __future__ import annotations

import os

from .base import BackendCapabilities, ParentValidation
from .posix import PosixBackend
from .windows import WindowsBackend

__all__ = [
    "BackendCapabilities",
    "ParentValidation",
    "PosixBackend",
    "WindowsBackend",
    "default_backend",
]


def default_backend() -> PosixBackend | WindowsBackend:
    return WindowsBackend() if os.name == "nt" else PosixBackend()
