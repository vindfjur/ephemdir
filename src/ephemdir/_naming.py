"""Human-friendly directory name generation.

Instead of dull names like ``tmp_data`` we generate playful two-word slugs
such as ``nimble-otter`` or ``brave-marmot`` using the ``coolname`` package.
A small built-in word list is used as a fallback if ``coolname`` is missing,
so name generation never hard-fails.
"""

from __future__ import annotations

import random

__all__ = ["funny_name"]

try:
    from coolname import generate_slug as _generate_slug
except ImportError:  # pragma: no cover - exercised only without the dependency
    _generate_slug = None

# Minimal fallback vocabulary, used only when ``coolname`` is unavailable.
_FALLBACK_ADJECTIVES = (
    "brave", "calm", "clever", "cosmic", "eager", "fuzzy", "jolly",
    "lucky", "mellow", "nimble", "quirky", "shiny", "snug", "witty",
)
_FALLBACK_NOUNS = (
    "badger", "comet", "ferret", "lemur", "marmot", "narwhal", "otter",
    "panda", "quokka", "raptor", "sparrow", "tapir", "walrus", "yak",
)


def funny_name(words: int = 2, separator: str = "-") -> str:
    """Return a playful directory name made of ``words`` joined by ``separator``.

    Examples: ``nimble-otter``, ``brave-marmot``. ``words`` must be at least 1.
    """
    if words < 1:
        raise ValueError("words must be >= 1")

    if _generate_slug is not None:
        slug = _generate_slug(words)
        # coolname always joins with a hyphen; re-join for a custom separator.
        return slug if separator == "-" else slug.replace("-", separator)

    # Fallback: adjective(s) + noun, keeping the requested word count.
    parts = [random.choice(_FALLBACK_ADJECTIVES) for _ in range(words - 1)]
    parts.append(random.choice(_FALLBACK_NOUNS))
    return separator.join(parts)
