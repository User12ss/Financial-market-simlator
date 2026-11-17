"""Find figures in model output that cannot be traced to anything the model was shown.

Agents are told that every number they state must come from the data
snapshot or the scenario. A weak model ignores that and invents index levels
and yields. This module is the check behind the instruction: it pulls the
figures out of a text and reports the ones that match nothing in the sources.

It is deliberately lenient. A figure counts as supported if any number in
the sources rounds to it, whatever that number measures, so "about 5%" is
accepted when the data says 5.15%. The aim is to catch figures that come from
nowhere, not to audit every sentence.

Known limits:
- A figure the model worked out itself, such as the gap between two yields,
  is reported too. The check cannot tell arithmetic from invention, so the
  warning built on it says "not found", not "wrong".
- A four-digit number without a thousands separator that equals a calendar
  year close to today (for example a level of exactly 2030) is read as a
  year and not checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

_DURATION_WORDS = r"(?:years?|yrs?|months?|weeks?|days?|sessions?|quarters?|hours?|minutes?)"
_RATE_WORDS = r"(?:treasur(?:y|ies)|t-?bills?|bills?|yields?|libor|sofr|rates?|notes?|bonds?|tenor|swaps?)"
# Index names that end in a number. Listed one by one: a general "name then
# digits" rule would swallow a level such as "DAX 18,000".
_INDEX_NAMES = (
    r"(?:S&P\s*-?\s*(?:500|400|600|100)|Russell\s*-?\s*(?:1000|2000|3000)|Nasdaq\s*-?\s*100"
    r"|FTSE\s*-?\s*(?:100|250)|CSI\s*-?\s*300|Stoxx\s*-?\s*(?:50|600)|CAC\s*-?\s*40"
    r"|Nikkei\s*-?\s*225|DAX\s*-?\s*40|Dow\s*-?\s*30)"
)
_YEAR = r"(?:19|20)\d{2}"
# A four-digit number followed by a unit or a counted thing is an amount,
# whatever precedes it: "2000 bps", "in 2000 shares", "2050 barrels".
_COUNTED = (
    r"(?:bps?|basis|points?|pts|pp|percent|pct|jobs|shares|barrels|contracts|units|workers|people|claims"
    r"|thousand|million|billion|trillion)"
)
_NOT_A_YEAR_IF_FOLLOWED_BY = rf"(?![\w%]|\.\d|,\d{{3}}|\s*{_COUNTED}\b)"
# Words that introduce a year. "of", "from", "by" and "after" are left out on
# purpose: they introduce amounts just as often ("payrolls of 2000").
_YEAR_CONTEXT = (
    r"(?:in|since|until|during|early|late|mid|FY|CY|Q[1-4]|H[12]|year"
    r"|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)

# Things that contain digits but are not figures to be checked.
_NOT_FIGURES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                                   # 2026-11-13
    re.compile(rf"(?<!\w){_INDEX_NAMES}\b(?![,.]\d)", re.IGNORECASE),       # S&P 500, but not "S&P 500,000"
    re.compile(r"\b60/40\b"),
    re.compile(r"(?m)^\s*\d{1,2}[.)](?=\s)"),                               # "1." or "2)" numbering a list
    re.compile(r"\b(?:tier|step|round|phase|stage|option)\s*\d{1,2}\b", re.IGNORECASE),
    # Durations and horizons: "1-3 months", "0-1 week", "200-day", "20/50/200-day", "52-week".
    re.compile(
        rf"(?<![\w.$])\d+(?:\.\d+)?(?:\s*(?:-|–|to|/)\s*\d+(?:\.\d+)?)*[\s-]*{_DURATION_WORDS}\b",
        re.IGNORECASE,
    ),
    # Tenors: "10Y", "5d". "3M" only when a rate word follows, because "3M barrels" is three million.
    re.compile(rf"(?<![\w.$])\d+(?:[YyDdWw]\b|[Mm](?=[\s-]+{_RATE_WORDS}\b))", re.IGNORECASE),
    # A year named as one: "in 1987", "since 2008", "FY2025", "Q4 2026", "March 2020".
    re.compile(rf"\b{_YEAR_CONTEXT}\s*-?\s*{_YEAR}{_NOT_A_YEAR_IF_FOLLOWED_BY}", re.IGNORECASE),
]
# A bare year close to today, alone or as a range: "2026 will be hard", "2026/27".
_BARE_YEAR = re.compile(rf"(?<![\w.$,])({_YEAR})(?:\s*[-/–]\s*\d{{2,4}})?{_NOT_A_YEAR_IF_FOLLOWED_BY}")

# A number, with "$" before or "%" after kept for display. Digits glued to
# letters (DGS10, T10Y2Y, Q4, tier1) are identifiers, not figures.
_FIGURE = re.compile(r"(?<![\w.])(\$?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(%?)")

RELATIVE_TOLERANCE = 0.005   # a cited 7,560 is supported by 7,561.67; a cited 7,500 is not

# Amounts a scenario may spell out. A word counts only next to a unit, so
# "a quarter point" and "half a percent" do, and "one of the reasons" does not.
_WORD_AMOUNTS = {
    "three-quarter": 0.75, "three quarter": 0.75, "quarter": 0.25, "half": 0.5,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_WORD_AMOUNT = re.compile(
    r"\b(" + "|".join(sorted(_WORD_AMOUNTS, key=len, reverse=True)) + r")s?"
    r"(?:[\s-]+(?:a|an|of a|of one))?[\s-]*(?:percentage[\s-]+points?|points?|percent|pct|basis|bps?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Figure:
    text: str        # as written, e.g. "$150", "5.5%", "6,000"
    value: float
    decimals: int    # digits after the decimal point as written


def _this_year() -> int:
    return date.today().year


def figures(text: str) -> list[Figure]:
    """The figures stated in a text, in order of appearance."""
    cleaned = text or ""
    for pattern in _NOT_FIGURES:
        cleaned = pattern.sub(" ", cleaned)
    near_today = range(_this_year() - 3, _this_year() + 7)
    cleaned = _BARE_YEAR.sub(lambda m: " " if int(m.group(1)) in near_today else m.group(0), cleaned)

    found = []
    for match in _FIGURE.finditer(cleaned):
        digits = match.group(2).replace(",", "")
        decimals = len(digits.partition(".")[2])
        found.append(Figure(match.group(0), float(digits), decimals))
    return found


def _supported(figure: Figure, known: list[float]) -> bool:
    # Half a unit of the last digit written covers rounding ("5.1" for 5.15);
    # the relative allowance covers rounding of large levels.
    rounding = 0.5 * 10 ** (-figure.decimals)
    return any(abs(figure.value - value) <= max(rounding, RELATIVE_TOLERANCE * abs(value)) + 1e-9 for value in known)


def _restatable(text: str) -> list[float]:
    """Numbers a text supports once restated: percent as basis points and back,
    and amounts written as words."""
    values = [figure.value for figure in figures(text)]
    values += [_WORD_AMOUNTS[match.group(1).lower()] for match in _WORD_AMOUNT.finditer(text or "")]
    return [value * scale for value in values for scale in (1.0, 100.0, 0.01)]


def unsupported_figures(text: str, sources: Iterable[str], restatable: Iterable[str] = ()) -> list[str]:
    """Figures in `text` that match no number in any of `sources`.

    Texts in `restatable` (the scenario) also support the same amount in other
    units, so "50bps" supports "0.5%" and "a quarter point" supports "25 bps".
    Returned as written, each once, in order of first appearance.
    """
    known = [figure.value for source in sources for figure in figures(source)]
    known += [value for source in restatable for value in _restatable(source)]
    missing: list[str] = []
    for figure in figures(text):
        if figure.text not in missing and not _supported(figure, known):
            missing.append(figure.text)
    return missing
