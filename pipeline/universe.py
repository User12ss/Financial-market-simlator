"""The universe of tickers and the screen that picks each day's candidates.

The screen is plain rules on price data. No model takes part, so the same
data always gives the same candidates.
"""

from __future__ import annotations

from pathlib import Path

import config
from data.models import Snapshot, TickerSnapshot
from models.signals import ScreenRow

# A placeholder universe of large, liquid US stocks and broad and sector ETFs.
# It is not a recommendation of any of them. Replace it with UNIVERSE_FILE.
DEFAULT_UNIVERSE: list[str] = [
    # Broad and sector ETFs
    "SPY", "QQQ", "IWM", "XLK", "XLV", "XLF", "XLE", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC",
    # Technology and communication
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "CSCO", "QCOM", "TXN",
    "IBM", "INTC", "NFLX", "DIS", "VZ", "T",
    # Consumer
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "COST", "WMT", "PG", "KO", "PEP",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "BLK",
    # Healthcare
    "UNH", "JNJ", "LLY", "MRK", "ABBV", "PFE", "TMO", "ABT",
    # Industrials, energy, materials, utilities
    "CAT", "GE", "HON", "UPS", "BA", "XOM", "CVX", "COP", "LIN", "NEE",
]


def load_universe(path: str | Path | None = None) -> list[str]:
    """Tickers from a file (one per line, `#` starts a comment), or the built-in list."""
    source = path or config.UNIVERSE_FILE
    if not source:
        return list(DEFAULT_UNIVERSE)
    tickers = []
    for line in Path(source).read_text(encoding="utf-8").splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol:
            tickers.append(symbol)
    if not tickers:
        raise ValueError(f"Universe file {source} lists no tickers.")
    return list(dict.fromkeys(tickers))


def _row(item: TickerSnapshot, min_price: float, min_dollar_volume: float) -> ScreenRow:
    price, tech = item.price, item.technicals
    row = ScreenRow(
        ticker=item.ticker, passed=False,
        close=price.close if price else None,
        avg_dollar_volume_20d=price.avg_dollar_volume_20d if price else None,
        sma50=tech.sma50 if tech else None,
        sma200=tech.sma200 if tech else None,
        return_3mo_pct=price.return_3mo_pct if price else None,
    )
    blocks = [issue.message for issue in item.issues if issue.severity == "block"]
    if blocks:
        row.reason = "Data not usable: " + " ".join(blocks)
    elif price is None or tech is None:
        row.reason = "No price data."
    elif price.close < min_price:
        row.reason = f"Price {price.close:,.2f} is below the minimum {min_price:,.2f}."
    elif price.avg_dollar_volume_20d is None or price.avg_dollar_volume_20d < min_dollar_volume:
        row.reason = "Average daily dollar volume is below the minimum."
    elif tech.sma50 is None or tech.sma200 is None or price.return_3mo_pct is None:
        row.reason = "Not enough history for the trend rule."
    elif not price.close > tech.sma50 > tech.sma200:
        row.reason = "Not in an uptrend: the rule is close above the 50-day average above the 200-day average."
    else:
        row.passed = True
    return row


def screen(
    snapshot: Snapshot,
    max_candidates: int | None = None,
    min_price: float | None = None,
    min_dollar_volume: float | None = None,
) -> list[ScreenRow]:
    """One row per ticker in the snapshot, saying whether it is a candidate today and why not.

    A ticker passes when its data is usable, it is liquid enough, and it is in
    an uptrend. Those that pass are ranked by three-month return and the best
    `max_candidates` are kept.
    """
    limit = config.PIPELINE_MAX_CANDIDATES if max_candidates is None else max_candidates
    min_price = config.PIPELINE_MIN_PRICE if min_price is None else min_price
    min_dollar_volume = config.PIPELINE_MIN_DOLLAR_VOLUME if min_dollar_volume is None else min_dollar_volume

    rows = [_row(item, min_price, min_dollar_volume) for item in snapshot.tickers.values()]
    # Ties are broken by ticker so the order never depends on input order.
    ranked = sorted((row for row in rows if row.passed), key=lambda row: (-row.return_3mo_pct, row.ticker))
    for position, row in enumerate(ranked, 1):
        if position <= limit:
            row.rank = position
        else:
            row.passed = False
            row.reason = f"Passed the rules but ranked {position}, outside the top {limit}."
    return rows


def candidates(rows: list[ScreenRow]) -> list[str]:
    """Today's candidates, best first."""
    return [row.ticker for row in sorted((r for r in rows if r.passed), key=lambda r: r.rank)]
