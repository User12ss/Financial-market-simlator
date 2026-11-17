"""Build, save and load point-in-time data snapshots.

    python -m data.snapshot --tickers AAPL,MSFT

A snapshot only contains sessions that had already closed when it was taken.
While the market is open, today's bar is still changing and is left out.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

import config
from data import indicators
from data.market_calendar import last_completed_session
from data.models import (
    Bars, CorporateAction, Issue, MacroPoint, MarketPoint, PriceSummary, Snapshot, Technicals, TickerSnapshot,
)
from data.providers import (
    DataError, FredProvider, MacroProvider, MarketDataProvider, NewsProvider, YFinanceProvider,
)
from data.quality import check_bars

HISTORY_DAYS = 400      # calendar days of bars to fetch: enough for a 200-day average and a 52-week range
SESSIONS_PER_YEAR = 252
STORED_BARS = 300
HEADLINES_PER_TICKER = 5

# Market-wide series included in every research snapshot: (group, name, symbol).
MARKET_SERIES: list[tuple[str, str, str]] = [
    ("market", "S&P 500", "^GSPC"),
    ("market", "Nasdaq Composite", "^IXIC"),
    ("market", "VIX", "^VIX"),
    ("market", "US Dollar Index", "DX-Y.NYB"),
    ("market", "Gold futures", "GC=F"),
    ("market", "WTI crude futures", "CL=F"),
    ("yields", "3-month Treasury yield", "^IRX"),
    ("yields", "5-year Treasury yield", "^FVX"),
    ("yields", "10-year Treasury yield", "^TNX"),
    ("yields", "30-year Treasury yield", "^TYX"),
    ("sectors", "Technology", "XLK"),
    ("sectors", "Healthcare", "XLV"),
    ("sectors", "Financials", "XLF"),
    ("sectors", "Energy", "XLE"),
    ("sectors", "Consumer Discretionary", "XLY"),
    ("sectors", "Consumer Staples", "XLP"),
    ("sectors", "Industrials", "XLI"),
    ("sectors", "Materials", "XLB"),
    ("sectors", "Utilities", "XLU"),
    ("sectors", "Real Estate", "XLRE"),
    ("sectors", "Communication Services", "XLC"),
]

# FRED series: (id, name, unit, frequency, show year-over-year change).
MACRO_SERIES: list[tuple[str, str, str, str, bool]] = [
    ("FEDFUNDS", "Effective federal funds rate", "%", "monthly", False),
    ("CPIAUCSL", "Consumer price index", "index", "monthly", True),
    ("CPILFESL", "Core consumer price index", "index", "monthly", True),
    ("UNRATE", "Unemployment rate", "%", "monthly", False),
    ("PAYEMS", "Nonfarm payrolls", "thousand jobs", "monthly", False),
    ("DGS2", "2-year Treasury yield", "%", "daily", False),
    ("DGS10", "10-year Treasury yield", "%", "daily", False),
    ("T10Y2Y", "10-year minus 2-year Treasury spread", "pp", "daily", False),
    ("BAMLH0A0HYM2", "High-yield credit spread", "pp", "daily", False),
    ("DCOILWTICO", "WTI crude oil spot price", "USD per barrel", "daily", False),
]
MACRO_HISTORY_DAYS = 800
# A monthly figure is dated the first of its month and published in the
# middle of the next, so on the day before a release the latest one is already
# about 75 days old. The monthly limit leaves room for that and for a late release.
MACRO_STALE_AFTER_DAYS = {"daily": 10, "monthly": 95}
MARKET_STALE_AFTER_DAYS = 5


def _round(value: float | None, digits: int = 4) -> float | None:
    """Rounded value, or None for anything that is not a finite number."""
    if value is None or pd.isna(value) or math.isinf(value):
        return None
    return round(float(value), digits)


def _warn(code: str, message: str) -> Issue:
    return Issue(severity="warn", code=code, message=message)


class SnapshotBuilder:
    """Collects data from the providers into one validated Snapshot."""

    def __init__(
        self,
        market: MarketDataProvider | None = None,
        macro: MacroProvider | None = None,
        news: NewsProvider | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        yahoo = YFinanceProvider() if market is None or news is None else None
        self.market = market or yahoo
        self.news = news or yahoo
        self.macro = macro or FredProvider()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        tickers: Iterable[str] = (),
        include_market: bool = True,
        include_macro: bool = True,
        include_news: bool = True,
        include_fundamentals: bool = True,
    ) -> Snapshot:
        """Prices and indicators for `tickers`, plus whatever else is switched on.

        Fundamentals, earnings dates and headlines cost one request per ticker.
        Screening a whole universe only needs prices, so it switches them off.
        """
        now = self._now()
        session = last_completed_session(now)
        snapshot = Snapshot(
            snapshot_id=f"{session.isoformat()}_{now:%H%M%S}", created_at=now, session_date=session,
        )
        wanted = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
        # A sector ETF can be both a requested ticker and a market series.
        symbols = list(dict.fromkeys(wanted + ([symbol for _, _, symbol in MARKET_SERIES] if include_market else [])))

        bars: dict[str, pd.DataFrame] = {}
        if symbols:
            try:
                bars = self.market.daily_bars(symbols, session - timedelta(days=HISTORY_DAYS), session)
            except DataError as e:
                snapshot.issues.append(Issue(severity="block", code="price_source_failed", message=str(e)))
        # Never trust the provider to stop at the last completed session.
        bars = {symbol: frame[frame.index <= session] for symbol, frame in bars.items()}

        for ticker in wanted:
            snapshot.tickers[ticker] = self._ticker(
                ticker, bars.get(ticker), session, now, include_news, include_fundamentals,
            )
        if include_market:
            snapshot.market = self._market(bars, session, snapshot.issues)
        if include_macro:
            snapshot.macro = self._macro(session, snapshot.issues)
        return snapshot

    # ── One ticker ───────────────────────────────────────────────────────

    def _ticker(
        self, ticker: str, frame: pd.DataFrame | None, session: date, now: datetime,
        include_news: bool, include_fundamentals: bool,
    ) -> TickerSnapshot:
        item = TickerSnapshot(ticker=ticker, issues=check_bars(ticker, frame, session))
        if frame is None or frame.empty:
            return item

        item.price = _price_summary(frame)
        item.technicals = _technicals(frame)
        item.bars = _stored_bars(frame)
        item.actions = _actions(frame)

        if include_fundamentals:
            try:
                item.fundamentals = self.market.fundamentals(ticker)
            except DataError as e:
                item.issues.append(_warn("no_fundamentals", str(e)))
            try:
                item.next_earnings = self.market.next_earnings(ticker, session)
            except DataError as e:
                item.issues.append(_warn("no_earnings_date", str(e)))
        if include_news:
            try:
                # A headline published after the snapshot's own moment was not
                # known then. This only happens when the clock is set back.
                item.news = [
                    headline for headline in self.news.headlines(ticker, HEADLINES_PER_TICKER)
                    if headline.published is None or headline.published <= now
                ]
            except DataError as e:
                item.issues.append(_warn("no_news", str(e)))
        return item

    # ── Market-wide series ───────────────────────────────────────────────

    def _market(self, bars: dict[str, pd.DataFrame], session: date, issues: list[Issue]) -> list[MarketPoint]:
        points = []
        for group, name, symbol in MARKET_SERIES:
            frame = bars.get(symbol)
            if frame is None or frame.empty:
                issues.append(_warn("market_series_missing", f"No data for {name} ({symbol})."))
                continue
            close = frame["adj_close"]
            last_day = frame.index[-1]
            if (session - last_day).days > MARKET_STALE_AFTER_DAYS:
                issues.append(_warn("market_series_stale", f"{name} ({symbol}) was last updated on {last_day}."))
            points.append(MarketPoint(
                group=group, name=name, symbol=symbol, last_date=last_day, value=_round(close.iloc[-1]),
                change_1d_pct=_round(indicators.pct_return(close, 1), 2),
                return_5d_pct=_round(indicators.pct_return(close, 5), 2),
                return_1mo_pct=_round(indicators.pct_return(close, 21), 2),
                change_1d=_round(indicators.change(close, 1)),
                change_5d=_round(indicators.change(close, 5)),
                change_1mo=_round(indicators.change(close, 21)),
            ))
        return points

    # ── Macro series ─────────────────────────────────────────────────────

    def _macro(self, session: date, issues: list[Issue]) -> list[MacroPoint]:
        points = []
        start = session - timedelta(days=MACRO_HISTORY_DAYS)
        for series_id, name, unit, frequency, with_yoy in MACRO_SERIES:
            try:
                observations = self.macro.series(series_id, start)
            except DataError as e:
                issues.append(_warn("macro_series_failed", str(e)))
                continue
            # Only what had been published by the session date belongs in the snapshot.
            observations = [(day, value) for day, value in observations if day <= session]
            if not observations:
                issues.append(_warn("macro_series_failed", f"No observations of {series_id} up to {session}."))
                continue
            latest_day, latest = observations[-1]
            if (session - latest_day).days > MACRO_STALE_AFTER_DAYS[frequency]:
                issues.append(_warn("macro_series_stale", f"{name} ({series_id}) was last observed on {latest_day}."))
            yoy = None
            if with_yoy:
                year_ago = [value for day, value in observations if day <= latest_day - timedelta(days=360)]
                if year_ago and year_ago[-1]:
                    yoy = (latest / year_ago[-1] - 1.0) * 100.0
            points.append(MacroPoint(
                series_id=series_id, name=name, unit=unit, frequency=frequency,
                latest_date=latest_day, latest_value=latest,
                previous_value=observations[-2][1] if len(observations) > 1 else None,
                yoy_change_pct=_round(yoy, 2),
            ))
        return points


# ── Derivations from a ticker's bars ─────────────────────────────────────────

def _adjusted(frame: pd.DataFrame) -> pd.DataFrame:
    """High, low and close on the adjusted scale, so indicators are not broken by splits."""
    # A zero close would make the factor infinite. Such a bar also blocks the
    # ticker in the quality check; this keeps the numbers finite regardless.
    factor = frame["adj_close"] / frame["close"].where(frame["close"] > 0)
    return pd.DataFrame({
        "high": frame["high"] * factor, "low": frame["low"] * factor, "close": frame["adj_close"],
    })


def _price_summary(frame: pd.DataFrame) -> PriceSummary:
    adjusted = _adjusted(frame)
    year = adjusted.tail(SESSIONS_PER_YEAR)
    recent = frame.tail(20)
    return PriceSummary(
        session=frame.index[-1],
        close=_round(frame["close"].iloc[-1]),
        previous_close=_round(frame["close"].iloc[-2]) if len(frame) > 1 else None,
        change_1d_pct=_round(indicators.pct_return(adjusted["close"], 1), 2),
        return_5d_pct=_round(indicators.pct_return(adjusted["close"], 5), 2),
        return_1mo_pct=_round(indicators.pct_return(adjusted["close"], 21), 2),
        return_3mo_pct=_round(indicators.pct_return(adjusted["close"], 63), 2),
        return_1y_pct=_round(indicators.pct_return(adjusted["close"], SESSIONS_PER_YEAR), 2),
        high_52w=_round(year["high"].max()),
        low_52w=_round(year["low"].min()),
        avg_dollar_volume_20d=_round((recent["close"] * recent["volume"]).mean(), 0),
    )


def _technicals(frame: pd.DataFrame) -> Technicals:
    adjusted = _adjusted(frame)
    close = adjusted["close"]
    macd = indicators.macd(close)
    bands = indicators.bollinger(close)
    atr = indicators.atr(adjusted["high"], adjusted["low"], close)
    return Technicals(
        sma20=_round(indicators.sma(close, 20)),
        sma50=_round(indicators.sma(close, 50)),
        sma200=_round(indicators.sma(close, 200)),
        rsi14=_round(indicators.rsi(close), 2),
        macd=_round(macd[0]) if macd else None,
        macd_signal=_round(macd[1]) if macd else None,
        bollinger_lower=_round(bands[0]) if bands else None,
        bollinger_upper=_round(bands[1]) if bands else None,
        atr14=_round(atr),
        atr14_pct=_round(atr / float(close.iloc[-1]) * 100.0, 2) if atr else None,
    )


def _stored_bars(frame: pd.DataFrame) -> Bars:
    kept = frame.tail(STORED_BARS)
    return Bars(
        dates=list(kept.index),
        **{column: [round(float(v), 4) for v in kept[column]]
           for column in ("open", "high", "low", "close", "adj_close", "volume")},
    )


def _actions(frame: pd.DataFrame) -> list[CorporateAction]:
    actions = []
    for day, row in frame[(frame["dividends"] > 0) | (frame["splits"] > 0)].iterrows():
        if row["dividends"] > 0:
            actions.append(CorporateAction(date=day, kind="dividend", value=float(row["dividends"])))
        if row["splits"] > 0:
            actions.append(CorporateAction(date=day, kind="split", value=float(row["splits"])))
    return actions


# ── Storage ──────────────────────────────────────────────────────────────────

def snapshot_path(snapshot: Snapshot, directory: str | Path | None = None) -> Path:
    root = Path(directory or config.SNAPSHOT_DIR)
    return root / snapshot.session_date.isoformat() / f"{snapshot.snapshot_id}.json"


def save_snapshot(snapshot: Snapshot, directory: str | Path | None = None) -> Path:
    path = snapshot_path(snapshot, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(), encoding="utf-8")
    return path


def load_snapshot(path: str | Path) -> Snapshot:
    return Snapshot.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ── Command line ─────────────────────────────────────────────────────────────

def describe(snapshot: Snapshot) -> list[str]:
    """A short, human-readable account of what a snapshot holds and what is wrong with it."""
    lines = [f"Snapshot {snapshot.snapshot_id}: last completed session {snapshot.session_date}"]
    for ticker, item in snapshot.tickers.items():
        if item.price and item.technicals:
            facts = f"close {item.price.close}, 1d {item.price.change_1d_pct}%, RSI {item.technicals.rsi14}, " \
                    f"{len(item.bars.dates)} bars, {len(item.news)} headlines"
        else:
            facts = "no data"
        lines.append(f"  {ticker}: {'usable' if item.usable else 'NOT USABLE'} ({facts})")
        lines.extend(f"    [{issue.severity}] {issue.message}" for issue in item.issues)
    if snapshot.market:
        lines.append(f"  market series: {len(snapshot.market)} of {len(MARKET_SERIES)}")
    if snapshot.macro:
        lines.append(f"  macro series: {len(snapshot.macro)} of {len(MACRO_SERIES)}")
    lines.extend(f"  [{issue.severity}] {issue.message}" for issue in snapshot.issues)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture a point-in-time data snapshot.")
    parser.add_argument("--tickers", default="", help="Comma-separated symbols, e.g. AAPL,MSFT")
    parser.add_argument("--no-market", action="store_true", help="Skip indices, yields and sector ETFs")
    parser.add_argument("--no-macro", action="store_true", help="Skip FRED macro series")
    parser.add_argument("--no-news", action="store_true", help="Skip headlines")
    parser.add_argument("--out", default=None, help="Directory to save into (default: SNAPSHOT_DIR)")
    args = parser.parse_args(argv)

    snapshot = SnapshotBuilder().build(
        args.tickers.split(","), include_market=not args.no_market,
        include_macro=not args.no_macro, include_news=not args.no_news,
    )
    path = save_snapshot(snapshot, args.out)
    print("\n".join(describe(snapshot)))
    print(f"Saved to {path}")

    blocked = [t for t, item in snapshot.tickers.items() if not item.usable]
    source_down = any(issue.severity == "block" for issue in snapshot.issues)
    return 1 if blocked or source_down else 0


if __name__ == "__main__":
    sys.exit(main())
