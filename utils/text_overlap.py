"""Detect agents repeating each other's text instead of giving their own view.

The measure is how many words of a text sit inside a run of consecutive
words that also appears, in the same order, in another text. Runs are used
rather than whole sentences because a model that lifts a passage often joins
two sentences or changes a word, which defeats sentence matching but leaves
most runs intact.

Each string passed in is one unit, normally one field of one agent's
analysis. Runs never cross from one unit into another.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# A run is the shortest stretch of consecutive tokens worth this many words.
# Eight words is long enough that two agents writing independently about the
# same scenario rarely share a run. A Chinese character counts as half a
# word, so pure Chinese needs 16 characters. Because the length comes from
# the tokens themselves, the same passage produces the same runs whatever
# language surrounds it.
RUN_WORDS = 8.0
_CJK_CHAR_WORDS = 0.5

# A token is one CJK character or one run of Latin letters or digits. All
# punctuation separates tokens, so "risk-free" and "risk free", or a straight
# and a curly apostrophe, compare equal.
_TOKEN = re.compile(r"[一-鿿]|[a-z0-9]+")


def _tokens(unit: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", unit or "")
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _TOKEN.findall(without_accents.lower())


def _words(token: str) -> float:
    return _CJK_CHAR_WORDS if "一" <= token <= "鿿" else 1.0


def _windows(tokens: list[str]) -> list[tuple[int, int]]:
    """For each start position, the shortest [start, end) worth RUN_WORDS words."""
    windows = []
    end, weight = 0, 0.0
    for start in range(len(tokens)):
        while end < len(tokens) and weight < RUN_WORDS:
            weight += _words(tokens[end])
            end += 1
        if weight < RUN_WORDS:
            break
        windows.append((start, end))
        weight -= _words(tokens[start])
    return windows


def _runs(unit: str) -> set[tuple[str, ...]]:
    tokens = _tokens(unit)
    return {tuple(tokens[start:end]) for start, end in _windows(tokens)}


def _as_units(value: str | Iterable[str]) -> Iterable[str]:
    return [value] if isinstance(value, str) else value


def copied_words(
    text: str, sources: str | Iterable[str], own: str | Iterable[str] = ()
) -> tuple[float, float]:
    """Return (words of `text` lying in a run also present in `sources`, total words).

    Runs the author had already written in `own` do not count: an agent
    repeating its own earlier text has not copied anyone. A Chinese character
    counts as half a word.
    """
    pool: set[tuple[str, ...]] = set()
    for source in _as_units(sources):
        pool |= _runs(source)
    for earlier in _as_units(own):
        pool -= _runs(earlier)

    tokens = _tokens(text)
    marked = [False] * len(tokens)
    for start, end in _windows(tokens):
        if tuple(tokens[start:end]) in pool:
            marked[start:end] = [True] * (end - start)
    covered = sum(_words(token) for token, hit in zip(tokens, marked) if hit)
    return covered, sum(_words(token) for token in tokens)


def copied_fraction(text: str, sources: str | Iterable[str], own: str | Iterable[str] = ()) -> float:
    """Share of the words in `text` that lie in a run also present in `sources`."""
    covered, total = copied_words(text, sources, own)
    return covered / total if total else 0.0
