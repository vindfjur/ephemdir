"""Tests for playful name generation."""

from __future__ import annotations

import re

import pytest

from ephemdir._naming import funny_name

_HEX_SUFFIX = re.compile(r"^[0-9a-f]{16}$")


def test_default_has_words_and_random_suffix():
    name = funny_name()
    parts = name.split("-")
    # adjective + noun + 16-hex-char suffix
    assert len(parts) == 3
    assert all(part.isalpha() for part in parts[:-1])
    assert _HEX_SUFFIX.fullmatch(parts[-1])


def test_more_concepts_means_more_words():
    assert funny_name(words=3).count("-") == 3


def test_single_word_keeps_random_suffix():
    parts = funny_name(words=1).split("-")
    assert len(parts) == 2
    assert parts[0].isalpha()
    assert _HEX_SUFFIX.fullmatch(parts[1])


def test_custom_separator():
    name = funny_name(separator="_")
    assert "_" in name
    assert "-" not in name


def test_invalid_word_count():
    with pytest.raises(ValueError):
        funny_name(words=0)


def test_names_vary():
    # The 64-bit suffix makes collisions across many draws negligible.
    names = {funny_name() for _ in range(50)}
    assert len(names) == 50


def test_suffix_defeats_wordlist_exhaustion():
    # Two names sharing the same word pair must still differ via the suffix.
    suffixes = {funny_name().rsplit("-", 1)[1] for _ in range(100)}
    assert len(suffixes) == 100


@pytest.mark.parametrize("separator", ["/", "\\", "\x00", "\n"])
def test_path_like_or_control_separator_is_rejected(separator):
    with pytest.raises(ValueError):
        funny_name(separator=separator)
