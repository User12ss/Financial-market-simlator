"""Prices for the evaluation: fetched once for everything that will be asked, then read many times.

One fetch matters for more than speed. The source restates earlier bars each
time a split takes effect, so bars from a single fetch are all on one share
scale and can be compared with each other directly.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Iterable

import pandas as pd

from data.providers import MarketDataProvider


class Prices:
    def __init__(self, bars: dict[str, pd.DataFrame]) -> None:
        self._bars = bars

    @classmethod
    def fetch(cls, provider: MarketDataProvider, symbols: Iterable[str], start: date, end: date) -> Prices:
        wanted = sorted(set(symbols))
        if not wanted or end < start:
            return cls({})
        return cls(provider.daily_bars(wanted, start, end))

    def frame(self, symbol: str) -> pd.DataFrame | None:
        return self._bars.get(symbol)

    def price(self, symbol: str, day: date, column: str) -> float | None:
        """A bar's open, high, low or close. None when there is no such bar or the value is not a usable price."""
        frame = self._bars.get(symbol)
        if frame is None or day not in frame.index:
            return None
        value = float(frame.at[day, column])
        return value if math.isfinite(value) and value > 0 else None

    def dividend(self, symbol: str, day: date) -> float:
        """The dividend per share that goes ex on `day`, or 0."""
        frame = self._bars.get(symbol)
        if frame is None or day not in frame.index:
            return 0.0
        value = float(frame.at[day, "dividends"])
        return value if math.isfinite(value) and value > 0 else 0.0

    def dividends(self, symbol: str, after: date, through: date) -> float:
        """Dividends per share whose ex-date is after `after` and no later than `through`."""
        frame = self._bars.get(symbol)
        if frame is None:
            return 0.0
        paid = frame.loc[(frame.index > after) & (frame.index <= through), "dividends"]
        return float(paid[paid > 0].sum())

    def total_return(self, symbol: str, first: date, last: date) -> float | None:
        """Percent made from the open of `first` to the close of `last`, dividends included.

        Someone who buys at the open of an ex-dividend day does not receive
        that dividend, so only ex-dates after `first` count.
        """
        start, end = self.price(symbol, first, "open"), self.price(symbol, last, "close")
        if start is None or end is None:
            return None
        return ((end + self.dividends(symbol, first, last)) / start - 1) * 100
