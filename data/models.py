"""What a data snapshot contains.

A snapshot is everything the system knew at one moment. Analysis, evaluation
and replay read snapshots instead of calling data sources again, so a past
decision can always be examined against exactly the data it was made on.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    """A data problem. A "block" issue means the item must not be used for a decision."""

    severity: Literal["block", "warn"]
    code: str
    message: str


class Bars(BaseModel):
    """Daily bars in column form, oldest first.

    Open, high, low and close are adjusted for splits but not for dividends:
    the source restates earlier bars each time a split takes effect, so every
    bar is on the share scale of the snapshot's last session. `adj_close` is
    adjusted for dividends as well.
    """

    dates: list[date] = Field(default_factory=list)
    open: list[float] = Field(default_factory=list)
    high: list[float] = Field(default_factory=list)
    low: list[float] = Field(default_factory=list)
    close: list[float] = Field(default_factory=list)
    adj_close: list[float] = Field(default_factory=list)
    volume: list[float] = Field(default_factory=list)


class PriceSummary(BaseModel):
    session: date = Field(description="The trading day the last bar belongs to")
    close: float
    previous_close: float | None = None
    change_1d_pct: float | None = None
    return_5d_pct: float | None = None
    return_1mo_pct: float | None = None
    return_3mo_pct: float | None = None
    return_1y_pct: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    avg_dollar_volume_20d: float | None = None


class Technicals(BaseModel):
    """Computed from split- and dividend-adjusted prices."""

    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    bollinger_lower: float | None = None
    bollinger_upper: float | None = None
    atr14: float | None = None
    atr14_pct: float | None = Field(default=None, description="ATR as a percent of the last close")


class Fundamentals(BaseModel):
    name: str = ""
    quote_type: str = ""
    sector: str = ""
    industry: str = ""
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    eps_ttm: float | None = None
    profit_margin: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    beta: float | None = None
    dividend_yield: float | None = None


class CorporateAction(BaseModel):
    date: date
    kind: Literal["dividend", "split"]
    value: float = Field(description="Dividend per share, or the split ratio (2.0 for 2-for-1)")


class NewsItem(BaseModel):
    """A headline. Text from outside sources is untrusted and is shown to models only as data."""

    title: str
    publisher: str = ""
    published: datetime | None = None
    link: str = ""


class TickerSnapshot(BaseModel):
    ticker: str
    price: PriceSummary | None = None
    technicals: Technicals | None = None
    fundamentals: Fundamentals | None = None
    actions: list[CorporateAction] = Field(default_factory=list)
    next_earnings: date | None = None
    news: list[NewsItem] = Field(default_factory=list)
    bars: Bars = Field(default_factory=Bars)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        """False when any issue blocks this ticker from being used in a decision."""
        return not any(issue.severity == "block" for issue in self.issues)


class MarketPoint(BaseModel):
    """One market-wide series: an index, a yield, a commodity or a sector ETF."""

    group: Literal["market", "yields", "sectors"]
    name: str
    symbol: str
    last_date: date
    value: float
    change_1d_pct: float | None = None
    return_5d_pct: float | None = None
    return_1mo_pct: float | None = None
    # The same moves in the series' own units. For a yield this is percentage
    # points, which is how yield moves are quoted; a percent change of a
    # percentage is easy to misread.
    change_1d: float | None = None
    change_5d: float | None = None
    change_1mo: float | None = None


class MacroPoint(BaseModel):
    series_id: str
    name: str
    unit: str
    frequency: Literal["daily", "monthly"]
    latest_date: date
    latest_value: float
    previous_value: float | None = None
    yoy_change_pct: float | None = None


class Snapshot(BaseModel):
    snapshot_id: str
    created_at: datetime = Field(description="When the data was captured, UTC")
    session_date: date = Field(description="Last completed trading session at capture time")
    tickers: dict[str, TickerSnapshot] = Field(default_factory=dict)
    market: list[MarketPoint] = Field(default_factory=list)
    market_bars: dict[str, Bars] = Field(
        default_factory=dict,
        description="Daily bars of the index and the sector funds, by symbol: what the forecast engine reads the "
                    "market's and the sectors' moves from. Empty in snapshots taken before it existed.",
    )
    macro: list[MacroPoint] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list, description="Problems not tied to one ticker")

    def usable_tickers(self) -> list[str]:
        return [ticker for ticker, item in self.tickers.items() if item.usable]
