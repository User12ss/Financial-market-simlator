"""What the evaluation produces: scores for views and trades, benchmarks, lessons and the report.

Everything here is worked out by code from what the journal recorded and from
prices that came afterwards. The one exception is `Lesson`, whose wording is a
model's; the facts beside it are code's.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from models.forecast import ForecastCard
from models.signals import Direction, Draft, UsageSummary


# ── Views ────────────────────────────────────────────────────────────────────

class ViewScore(BaseModel):
    """One analyst view, measured against what the price did over the view's own horizon.

    The return runs from the open of the first session after the view, the
    first price anyone could have acted on, to the close of the session that
    ends the horizon, dividends included.
    """

    run_id: str
    arm: str
    ticker: str
    session_date: date = Field(description="The session after whose close the view was written")
    direction: Direction
    confidence: float
    horizon_days: int
    held: bool
    sector: str = ""
    regime: str = Field(default="none", description="The desk's risk appetite that day, or 'none' without a macro view")
    starts_on: date = Field(description="The session whose open the return is measured from")
    matures_on: date = Field(description="The session whose close ends the horizon")
    status: Literal["scored", "pending", "unpriced"] = Field(
        description="pending: the horizon has not ended. unpriced: it has, but a price is missing."
    )
    return_pct: float | None = None
    benchmark_return_pct: float | None = None
    excess_pct: float | None = Field(default=None, description="The ticker's return less the benchmark's")
    direction_right: bool | None = Field(
        default=None, description="Bullish and it rose, or bearish and it fell. None for a neutral view."
    )
    beat_benchmark: bool | None = Field(
        default=None, description="Bullish and it beat the benchmark, or bearish and it lagged. None for neutral."
    )
    benchmark: str = Field(default="", description="What it was measured against")
    scored_as_of: date | None = Field(
        default=None, description="The last completed session when this score was worked out"
    )


class Tally(BaseModel):
    """How one group of scored views did. Neutral views are counted apart: they claim no direction."""

    label: str
    views: int = Field(description="Scored views that took a side")
    right: int
    hit_rate: float | None = None
    beat_benchmark: int
    beat_rate: float | None = None
    mean_edge_pct: float | None = Field(
        default=None, description="Mean excess return, signed so that positive means the view helped"
    )
    mean_confidence: float | None = None
    margin: float | None = Field(
        default=None, description="Rough margin of error on the rates: 0.5 / sqrt(views), as a fraction"
    )


class Scorecard(BaseModel):
    arm: str
    total: int = 0
    scored: int = 0
    pending: int = 0
    unpriced: int = 0
    neutral: int = Field(default=0, description="Scored views that took no side")
    sessions: int = Field(default=0, description="Distinct sessions with a scored view: views of one day move together")
    overall: Tally | None = None
    by_confidence: list[Tally] = Field(default_factory=list)
    by_direction: list[Tally] = Field(default_factory=list)
    by_sector: list[Tally] = Field(default_factory=list)
    by_regime: list[Tally] = Field(default_factory=list)
    by_position: list[Tally] = Field(default_factory=list, description="Views of holdings against views of candidates")


# ── Trades ───────────────────────────────────────────────────────────────────

class TradeExit(BaseModel):
    exit_date: date
    price: float
    shares: int
    share_of_position: float = Field(
        description="This sale as a share of what was held just before it, on that day's share scale: 1 sells it all"
    )
    kind: Literal["market", "limit", "stop", "close"]
    pnl_usd: float
    pnl_pct: float | None = None


class Trade(BaseModel):
    """One position of the paper account, from its first buy to its last sell."""

    arm: str
    ticker: str
    sector: str = ""
    decided_on: date | None = Field(default=None, description="The session after whose close the buy was decided")
    entry_date: date
    entry_price: float = Field(description="Average price paid")
    shares: int = Field(description="Shares bought")
    exits: list[TradeExit] = Field(default_factory=list)
    open_shares: int = 0
    status: Literal["open", "closed"]
    stopped: bool = Field(default=False, description="Whether any of it was sold by its stop")
    realized_usd: float = 0.0
    last_price: float | None = Field(default=None, description="For what is still held: the last close")
    unrealized_usd: float | None = None
    sessions_held: int | None = None


class Slice(BaseModel):
    """Profit and loss of the trades in one group."""

    label: str
    trades: int
    pnl_usd: float


class TradeSummary(BaseModel):
    arm: str
    trades: list[Trade] = Field(default_factory=list)
    closed: int = 0
    open: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float | None = None
    stopped_out: int = 0
    realized_usd: float = 0.0
    unrealized_usd: float = 0.0
    average_win_usd: float | None = None
    average_loss_usd: float | None = None
    by_ticker: list[Slice] = Field(default_factory=list)
    by_sector: list[Slice] = Field(default_factory=list)
    by_exit: list[Slice] = Field(default_factory=list)


# ── Benchmarks ───────────────────────────────────────────────────────────────

class EquityPoint(BaseModel):
    session_date: date
    equity: float


class Curve(BaseModel):
    """A value through time, and the two numbers people read off it."""

    name: str
    description: str
    points: list[EquityPoint] = Field(default_factory=list)
    return_pct: float | None = None
    max_drawdown_pct: float | None = None


class PickResult(BaseModel):
    """One of the account's buys, set against what any other candidate of that day would have made."""

    ticker: str
    decided_on: date
    entry_date: date
    replayed_usd: float = Field(
        description="What the pick made when replayed by the same rules as every other candidate of that day"
    )
    booked_usd: float = Field(description="What the account's books say the position made, dividends included")
    pool: int = Field(description="Candidates of that day the same rules would have let the account buy")
    pool_mean_usd: float | None = None
    rank: int = Field(description="Its place among those candidates by what each made: 1 is the best")


class RandomBenchmark(BaseModel):
    """The account's buys replaced, a thousand times over, by random picks from the same day's candidates.

    The account's own ticker and every other candidate of the day go through
    one and the same replay: bought on the same day, sized and gated by the
    same rules, and held on the same schedule. Only the ticker differs, so
    the comparison measures the choice of ticker and nothing else. What the
    account's books recorded is given beside it, and differs wherever the
    real position was not sized or stopped the way the rule alone would do.
    """

    entries: int = Field(default=0, description="Buys of the account that could be replayed")
    skipped: int = Field(default=0, description="Buys left out: no candidate list, no usable prices, or the pick itself could not be replayed")
    simulations: int = 0
    seed: int = 0
    valued_on: date | None = Field(default=None, description="The session open positions are valued at: where the books stand")
    replayed_pnl_usd: float | None = Field(default=None, description="The account's own picks, replayed like the random ones")
    booked_pnl_usd: float | None = Field(default=None, description="The same buys as the account's books have them")
    mean_pnl_usd: float | None = None
    quantiles_usd: dict[str, float] = Field(default_factory=dict, description="5th, 25th, 50th, 75th and 95th percentile")
    percentile: float | None = Field(
        default=None, description="Share of the random outcomes the account's own picks did better than, 0 to 100"
    )
    picks: list[PickResult] = Field(default_factory=list)


# ── Lessons ──────────────────────────────────────────────────────────────────

class LessonDraft(Draft):
    assessment: str = Field(description="What the view got right or wrong, given what happened (1-2 sentences)")
    lesson: str = Field(description="One sentence to keep in mind for a similar case, stated generally")


class Lesson(BaseModel):
    """What a model wrote about one of its own views once the real outcome was known."""

    arm: str
    ticker: str
    sector: str = ""
    session_date: date = Field(description="When the view was written")
    known_on: date = Field(description="The session at whose close the outcome was known")
    direction: Direction
    confidence: float
    outcome: str = Field(description="The facts, written by code: what the price and the benchmark did")
    assessment: str
    lesson: str
    data_warnings: list[str] = Field(default_factory=list)
    created_at: datetime


# ── The report ───────────────────────────────────────────────────────────────

class ArmEvaluation(BaseModel):
    arm: str
    first_session: date | None = None
    last_session: date | None = None
    sessions_run: int = 0
    curves: list[Curve] = Field(default_factory=list, description="The account, then each benchmark")
    scorecard: Scorecard
    trades: TradeSummary
    random: RandomBenchmark
    forecasts: ForecastCard | None = Field(
        default=None, description="How the forecast engine's distributions held up. None for an arm that makes none.",
    )
    usage: UsageSummary = Field(default_factory=UsageSummary)
    lessons_written: int = 0
    lessons_total: int = 0
    notes: list[str] = Field(default_factory=list)


class Evaluation(BaseModel):
    """Everything one evaluation found, for every model configuration in the journal."""

    as_of: date = Field(description="The last completed session when the evaluation ran")
    created_at: datetime
    benchmark: str
    arms: list[ArmEvaluation] = Field(default_factory=list)
