"""Where the data comes from.

Each kind of data sits behind a small interface, so a source can be replaced
without touching the code that builds snapshots. The first implementations
are free and need no API key: Yahoo Finance (through yfinance) for prices,
fundamentals and headlines, and FRED's public CSV endpoint for macro series.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import httpx
import pandas as pd

from data.models import Fundamentals, NewsItem

# yfinance reports failures on its own logger as well as by returning nothing.
# Failures are turned into snapshot issues here, so its log lines are noise.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

BAR_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume", "dividends", "splits"]


class DataError(RuntimeError):
    """A data source failed or returned something unusable."""


# ── Interfaces ───────────────────────────────────────────────────────────────

class MarketDataProvider(Protocol):
    def daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Daily bars per symbol from `start` to `end` inclusive.

        Each frame is indexed by session date, oldest first, with BAR_COLUMNS.
        Open, high, low and close are adjusted for splits but not for
        dividends; `adj_close` is adjusted for both. Symbols with no data are
        left out of the result.
        """

    def fundamentals(self, symbol: str) -> Fundamentals: ...

    def next_earnings(self, symbol: str, after: date) -> date | None: ...


class NewsProvider(Protocol):
    def headlines(self, symbol: str, limit: int) -> list[NewsItem]: ...


class MacroProvider(Protocol):
    def series(self, series_id: str, start: date) -> list[tuple[date, float]]:
        """Observations of one series from `start`, oldest first, missing values dropped."""


# ── Yahoo Finance ────────────────────────────────────────────────────────────

_YF_COLUMNS = {
    "Open": "open", "High": "high", "Low": "low", "Close": "close", "Adj Close": "adj_close",
    "Volume": "volume", "Dividends": "dividends", "Stock Splits": "splits",
}


def normalize_download(frame: pd.DataFrame | None, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Turn the frame yfinance returns for several symbols into one tidy frame each."""
    result: dict[str, pd.DataFrame] = {}
    if frame is None or frame.empty:
        return result
    grouped = isinstance(frame.columns, pd.MultiIndex)
    for symbol in symbols:
        if grouped:
            if symbol not in frame.columns.get_level_values(0):
                continue
            part = frame[symbol]
        else:
            part = frame
        part = part.rename(columns=_YF_COLUMNS).copy()
        if "close" not in part.columns:
            continue
        if "adj_close" not in part.columns:
            part["adj_close"] = part["close"]
        for column in ("open", "high", "low", "volume"):
            if column not in part.columns:
                part[column] = float("nan")
        for column in ("dividends", "splits"):
            if column not in part.columns:
                part[column] = 0.0
            part[column] = part[column].fillna(0.0)
        part = part[BAR_COLUMNS].dropna(subset=["close"])
        if part.empty:
            continue
        # The index holds each session's date in the exchange's own time zone.
        # Taking .date() directly keeps that date; converting zones could shift it.
        part.index = [stamp.date() for stamp in pd.DatetimeIndex(part.index)]
        # The source occasionally repeats a session. A repeated row would be
        # counted as an extra day by every windowed indicator.
        part = part[~part.index.duplicated(keep="last")]
        result[symbol] = part.sort_index()
    return result


class YFinanceProvider:
    """Prices, fundamentals, earnings dates and headlines from Yahoo Finance.

    yfinance is an unofficial client. It is good enough for paper trading and
    research, and should be replaced by a contracted source before real money
    depends on it.
    """

    def daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        if not symbols:
            return {}
        try:
            frame = yf.download(
                symbols, start=start, end=end + timedelta(days=1), auto_adjust=False, actions=True,
                group_by="ticker", progress=False, threads=True,
            )
        except Exception as e:
            raise DataError(f"Yahoo Finance price download failed: {e}") from e
        bars = normalize_download(frame, symbols)
        return {symbol: part[(part.index >= start) & (part.index <= end)] for symbol, part in bars.items()}

    def fundamentals(self, symbol: str) -> Fundamentals:
        import yfinance as yf

        try:
            info = yf.Ticker(symbol).info or {}
        except Exception as e:
            raise DataError(f"Yahoo Finance fundamentals failed for {symbol}: {e}") from e
        if not info.get("quoteType"):
            raise DataError(f"Yahoo Finance returned no fundamentals for {symbol}.")
        return Fundamentals(
            name=info.get("shortName") or info.get("longName") or "",
            quote_type=info.get("quoteType") or "",
            sector=info.get("sector") or "",
            industry=info.get("industry") or "",
            market_cap=info.get("marketCap"),
            trailing_pe=info.get("trailingPE"),
            forward_pe=info.get("forwardPE"),
            eps_ttm=info.get("trailingEps"),
            profit_margin=info.get("profitMargins"),
            revenue_growth=info.get("revenueGrowth"),
            earnings_growth=info.get("earningsGrowth"),
            beta=info.get("beta"),
            dividend_yield=info.get("dividendYield"),
        )

    def next_earnings(self, symbol: str, after: date) -> date | None:
        import yfinance as yf

        try:
            calendar = yf.Ticker(symbol).calendar or {}
        except Exception as e:
            raise DataError(f"Yahoo Finance calendar failed for {symbol}: {e}") from e
        upcoming = [
            day for day in (calendar.get("Earnings Date") or [])
            if isinstance(day, date) and day > after
        ]
        return min(upcoming) if upcoming else None

    def earnings_reports(self, symbol: str) -> list[datetime]:
        """When the company reported its results, oldest first, as New York times.

        The source lists the next report or two as well: dates after the
        caller's own cut-off are the caller's to drop. A time of midnight
        means the source gives the day and not the hour. This goes through
        the source's earnings calendar page, which the client can only read
        with the lxml package installed.
        """
        import yfinance as yf

        try:
            frame = yf.Ticker(symbol).get_earnings_dates(limit=100)
        except ImportError as e:
            raise DataError(f"Yahoo Finance earnings dates need a package that is not installed: {e}") from e
        except Exception as e:
            raise DataError(f"Yahoo Finance earnings dates failed for {symbol}: {e}") from e
        if frame is None:
            return []
        stamps = pd.DatetimeIndex(frame.index)
        if stamps.tz is None:
            stamps = stamps.tz_localize("America/New_York")
        return sorted(stamp.tz_convert("America/New_York").to_pydatetime() for stamp in stamps)

    def headlines(self, symbol: str, limit: int) -> list[NewsItem]:
        import yfinance as yf

        try:
            found = yf.Search(symbol, news_count=limit * 3).news or []
        except Exception as e:
            raise DataError(f"Yahoo Finance news search failed for {symbol}: {e}") from e
        items = []
        for entry in found:
            related = entry.get("relatedTickers")
            if related is not None and symbol not in related:
                continue  # the search also returns general market stories
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            stamp = entry.get("providerPublishTime")
            items.append(NewsItem(
                title=title,
                publisher=entry.get("publisher") or "",
                published=datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp else None,
                link=entry.get("link") or "",
            ))
        return items[:limit]


# ── FRED ─────────────────────────────────────────────────────────────────────

class FredProvider:
    """Macro series from FRED's public CSV download. No API key is needed."""

    URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._http = client or httpx.Client(timeout=30, follow_redirects=True)

    def series(self, series_id: str, start: date) -> list[tuple[date, float]]:
        try:
            response = self._http.get(self.URL, params={"id": series_id, "cosd": start.isoformat()})
        except httpx.HTTPError as e:
            raise DataError(f"FRED request failed for {series_id}: {e}") from e
        if response.status_code != 200:
            raise DataError(f"FRED returned {response.status_code} for {series_id}.")
        lines = response.text.strip().splitlines()
        if not lines or "," not in lines[0] or series_id not in lines[0]:
            raise DataError(f"FRED did not return a CSV for {series_id}.")
        observations = []
        for line in lines[1:]:
            day, _, value = line.partition(",")
            try:
                observations.append((date.fromisoformat(day.strip()), float(value)))
            except ValueError:
                continue  # FRED writes "." on days with no observation
        if not observations:
            raise DataError(f"FRED returned no observations for {series_id}.")
        return observations
