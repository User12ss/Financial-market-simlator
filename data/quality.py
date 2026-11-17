"""Checks that decide whether a ticker's data is fit to base a decision on.

A "block" issue takes the ticker out of consideration. The rule is to refuse
when in doubt: trading on stale or broken data is worse than not trading.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd

from data.models import Issue

MIN_BARS = 60               # fewer sessions than this cannot support the indicators
BARS_FOR_LONG_AVERAGE = 200
RECENT_SESSIONS = 20
MAX_UNEXPLAINED_MOVE = 0.5  # a one-day move beyond 50% with no split is treated as bad data


def _block(code: str, message: str) -> Issue:
    return Issue(severity="block", code=code, message=message)


def _warn(code: str, message: str) -> Issue:
    return Issue(severity="warn", code=code, message=message)


def check_bars(ticker: str, bars: pd.DataFrame | None, session: date) -> list[Issue]:
    """Issues with a ticker's daily bars as of the given completed session."""
    if bars is None or bars.empty:
        return [_block("no_price_data", f"{ticker}: no price data was returned.")]

    issues: list[Issue] = []
    last_day = bars.index[-1]
    if last_day < session:
        issues.append(_block(
            "stale_price", f"{ticker}: last bar is {last_day}, but the last completed session is {session}."
        ))
    if len(bars) < MIN_BARS:
        issues.append(_block("short_history", f"{ticker}: only {len(bars)} sessions of history."))
    elif len(bars) < BARS_FOR_LONG_AVERAGE:
        issues.append(_warn(
            "no_long_average", f"{ticker}: {len(bars)} sessions, too few for a 200-day average."
        ))

    # The whole history is checked, not just the latest bars: one bad price
    # anywhere feeds the 52-week range, the adjustment factor and every average.
    prices = bars[["open", "high", "low", "close", "adj_close"]]
    bad = prices.isna().any(axis=1) | (prices <= 0).any(axis=1)
    if bad.any():
        issues.append(_block(
            "bad_price", f"{ticker}: a missing or non-positive price on {bars.index[bad][-1]} ({int(bad.sum())} bar(s))."
        ))
    else:
        top = bars[["open", "close"]].max(axis=1)
        bottom = bars[["open", "close"]].min(axis=1)
        tolerance = bars["close"] * 0.001
        broken = (bars["high"] + tolerance < top) | (bars["low"] - tolerance > bottom)
        if broken.any():
            issues.append(_block(
                "inconsistent_bar",
                f"{ticker}: on {bars.index[broken][-1]} the high or low does not contain the open and close.",
            ))

    volume = float(bars["volume"].iloc[-1])
    if math.isnan(volume) or volume <= 0:
        issues.append(_block("no_volume", f"{ticker}: no volume on {last_day}."))

    # Adjusted prices already absorb splits and dividends, so a jump that
    # survives adjustment is either real news or bad data. Either way the
    # ticker should not be traded mechanically.
    moves = bars["adj_close"].pct_change().tail(RECENT_SESSIONS).abs()
    if (moves > MAX_UNEXPLAINED_MOVE).any():
        day = moves.idxmax()
        issues.append(_block(
            "unexplained_jump", f"{ticker}: adjusted price moved {moves.max():.0%} on {day} with no split to explain it."
        ))
    return issues
