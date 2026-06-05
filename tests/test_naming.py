"""Tests for playful name generation."""

from __future__ import annotations

import pytest

from ephemdir._naming import funny_name


def test_default_has_multiple_words():
    # coolname concepts may expand to a word or a short phrase, so we assert a
    # lower bound rather than an exact hyphen count.
    name = funny_name()
    assert name.count("-") >= 1
    assert all(part.isalpha() for part in name.split("-"))


def test_more_concepts_means_more_words():
    assert funny_name(words=3).count("-") >= 2


def test_custom_separator():
    name = funny_name(separator="_")
    assert "_" in name
    assert "-" not in name


def test_invalid_word_count():
    with pytest.raises(ValueError):
        funny_name(words=0)


def test_names_vary():
    # Extremely unlikely to collide across many draws.
    names = {funny_name() for _ in range(50)}
    assert len(names) > 1
