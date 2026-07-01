"""Human-friendly directory name generation without runtime dependencies."""

from __future__ import annotations

import os
import secrets
import unicodedata

__all__ = ["clean_name", "funny_name"]

_FALLBACK_ADJECTIVES = (
    "agile", "amber", "bold", "brave", "bright", "brisk", "calm", "clever",
    "cool", "cosmic", "crisp", "dapper", "eager", "fair", "fancy", "fleet",
    "fresh", "fuzzy", "gentle", "glad", "golden", "grand", "happy", "hardy",
    "humble", "jolly", "keen", "kind", "lively", "lucky", "mellow", "merry",
    "mighty", "neat", "nimble", "noble", "odd", "plucky", "proud", "quick",
    "quiet", "quirky", "rapid", "ready", "rosy", "savvy", "serene", "shiny",
    "slick", "smart", "snug", "soft", "solar", "spry", "steady", "sunny",
    "swift", "tidy", "vivid", "warm", "wild", "wise", "witty", "zany",
)
_FALLBACK_NOUNS = (
    "antelope", "badger", "beacon", "beaver", "bison", "bobcat", "caribou",
    "comet", "coral", "cougar", "crane", "dingo", "dolphin", "eagle",
    "falcon", "ferret", "finch", "fox", "gazelle", "gecko", "heron", "ibis",
    "jaguar", "kestrel", "koala", "lark", "lemur", "lynx", "marmot",
    "meteor", "moose", "narwhal", "orca", "otter", "owl", "panda",
    "panther", "penguin", "puffin", "puma", "quail", "quokka", "rabbit",
    "raptor", "raven", "robin", "salmon", "seal", "shark", "sparrow",
    "squid", "stoat", "stork", "swan", "tapir", "tiger", "toucan",
    "turtle", "walrus", "whale", "wolf", "wombat", "yak", "zebra",
)
# 64 random bits appended to every name. The word lists alone offer at most
# 64 * 64 = 4096 combinations, which a local user could exhaust in advance in
# a shared sticky directory like /tmp and so block creation entirely. The
# suffix makes names effectively unguessable and collisions negligible.
_SUFFIX_BYTES = 8


def _validate_separator(separator: str) -> None:
    if not isinstance(separator, str):
        raise TypeError("separator must be a string")
    forbidden = {"\x00", "/", "\\"}
    if os.sep:
        forbidden.add(os.sep)
    if os.altsep:
        forbidden.add(os.altsep)
    if any(token in separator for token in forbidden):
        raise ValueError("separator must not contain path separators or NUL")
    if any(
        ord(character) < 32 or ord(character) == 127
        or unicodedata.category(character) in ("Cc", "Cf", "Cs")
        for character in separator
    ):
        raise ValueError("separator must not contain control or formatting characters")


def funny_name(words: int = 2, separator: str = "-") -> str:
    """Return a safe playful name made of ``words`` joined by ``separator``,
    followed by a 16-character random hexadecimal token.

    The result is always one relative path component, e.g.
    ``quiet-falcon-a81f42c9d047315b``. ``words`` must be an integer between
    1 and 4. The 64-bit token defeats name-space exhaustion in shared sticky
    directories: the readable words alone have only 4096 combinations.
    """
    if not isinstance(words, int) or isinstance(words, bool) or not 1 <= words <= 4:
        raise ValueError("words must be an integer between 1 and 4")
    _validate_separator(separator)

    parts = [secrets.choice(_FALLBACK_ADJECTIVES) for _ in range(words - 1)]
    parts.append(secrets.choice(_FALLBACK_NOUNS))
    parts.append(secrets.token_hex(_SUFFIX_BYTES))
    name = separator.join(parts)
    if not name or name in {".", ".."} or os.path.isabs(name):
        raise ValueError("generated name is not a safe path component")
    return name


def clean_name(words: int = 2, separator: str = "-") -> str:
    """Return a readable name without the random suffix.

    Callers may use this only after proving the parent directory is private to
    the current user; otherwise the finite wordlist is vulnerable to namespace
    exhaustion by another local user.
    """
    if not isinstance(words, int) or isinstance(words, bool) or not 1 <= words <= 4:
        raise ValueError("words must be an integer between 1 and 4")
    _validate_separator(separator)
    parts = [secrets.choice(_FALLBACK_ADJECTIVES) for _ in range(words - 1)]
    parts.append(secrets.choice(_FALLBACK_NOUNS))
    name = separator.join(parts)
    if not name or name in {".", ".."} or os.path.isabs(name):
        raise ValueError("generated name is not a safe path component")
    return name
