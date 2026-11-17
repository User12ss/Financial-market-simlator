"""Technical indicators computed from daily bars.

Pure functions over pandas Series. Each returns None when there is not
enough history, rather than a number computed from too few points.
"""

from __future__ import annotations

import math

import pandas as pd


def _last(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = float(series.iloc[-1])
    return None if math.isnan(value) else value


def sma(close: pd.Series, window: int) -> float | None:
    """Simple moving average of the last `window` closes."""
    if len(close) < window:
        return None
    return _last(close.rolling(window).mean())


def pct_return(close: pd.Series, sessions: int) -> float | None:
    """Percent change over the last `sessions` trading days."""
    if len(close) <= sessions:
        return None
    start = float(close.iloc[-1 - sessions])
    if not start or math.isnan(start):
        return None
    return (float(close.iloc[-1]) / start - 1.0) * 100.0


def change(close: pd.Series, sessions: int) -> float | None:
    """Change over the last `sessions` trading days, in the series' own units."""
    if len(close) <= sessions:
        return None
    difference = float(close.iloc[-1]) - float(close.iloc[-1 - sessions])
    return None if math.isnan(difference) else difference


def _wilder(values: pd.Series, period: int) -> float | None:
    """Wilder's smoothed average: the simple average of the first `period`
    values, then each later value folded in with weight 1/period."""
    if len(values) < period:
        return None
    average = float(values.iloc[:period].mean())
    for value in values.iloc[period:]:
        average = (average * (period - 1) + float(value)) / period
    return average


def rsi(close: pd.Series, period: int = 14) -> float | None:
    """Relative strength index as Wilder defined it. 0 to 100."""
    if len(close) <= period:
        return None
    moves = close.diff().dropna()
    gain = _wilder(moves.clip(lower=0), period)
    loss = _wilder(-moves.clip(upper=0), period)
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float] | None:
    """MACD line and its signal line."""
    if len(close) < slow + signal:
        return None
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return float(line.iloc[-1]), float(line.ewm(span=signal, adjust=False).mean().iloc[-1])


def bollinger(close: pd.Series, window: int = 20, width: float = 2.0) -> tuple[float, float] | None:
    """Lower and upper Bollinger bands, using the population standard deviation
    as in Bollinger's definition."""
    if len(close) < window:
        return None
    recent = close.iloc[-window:]
    middle = float(recent.mean())
    spread = float(recent.std(ddof=0)) * width
    return middle - spread, middle + spread


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    """Average true range as Wilder defined it, in price units."""
    if len(close) <= period:
        return None
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1).iloc[1:]
    value = _wilder(true_range, period)
    return None if value is None or math.isnan(value) else value
