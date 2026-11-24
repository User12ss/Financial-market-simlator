"""What the forecast engine says, and what each forecast turned out to be worth.

A forecast is a distribution: where the price may be after 1, 5, 10 and 20
sessions, how likely the stop is to be touched on the way, and the same for
the book as a whole. Beside each forecast stands a plain one, made from
historical volatility alone, which the engine has to beat.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

HORIZONS = (1, 5, 10, 20)
# The percentiles kept of every forecast distribution: enough to say afterwards where the outcome fell.
GRID = tuple(range(1, 100))
BANDS = ("5", "25", "50", "75", "95")
COVERAGES = ("50", "80", "95")


class HorizonForecast(BaseModel):
    """One name, one horizon."""

    sessions: int
    matures_on: date = Field(description="The session at whose close the horizon ends")
    quantiles: dict[str, float] = Field(description="Price at the 5th, 25th, 50th, 75th and 95th percentile")
    grid: list[float] = Field(description="Log return at each percentile from 1 to 99")
    stop_touch: float | None = Field(default=None, description="Chance the stop is touched within the horizon")
    target_first: float | None = Field(default=None, description="Chance the target is reached within the horizon, before the stop")
    stop_first: float | None = Field(default=None, description="Chance the stop is touched within the horizon, before the target")
    adverse: dict[str, float] = Field(
        default_factory=dict,
        description="Largest fall below the close on the way, in percent: the median path, and the worst quarter and twentieth",
    )
    naive_grid: list[float] = Field(description="The plain forecast's log return at the same percentiles")
    naive_stop_touch: float | None = None


class TickerForecast(BaseModel):
    ticker: str
    close: float
    beta: float
    sector: str = Field(default="", description="The sector fund that is its second factor, if any")
    own_vol: float = Field(description="Its own daily volatility expected for the next session")
    daily_vol: float = Field(description="Its whole daily volatility expected for the next session, the market's share included")
    naive_vol: float = Field(description="Standard deviation of its daily returns in the snapshot: all the plain forecast uses")
    stop: float | None = None
    stop_from: Literal["position", "analyst", "rule", ""] = Field(
        default="", description="Whose stop it is: the one on the books, the analyst's, or the sizing rule's"
    )
    target: float | None = Field(default=None, description="A notional target: no seat names one yet")
    break_even: float | None = Field(
        default=None, description="Share of such trades that has to reach the target before the stop for them to pay"
    )
    direction: str = ""
    confidence: float | None = None
    view_horizon: int | None = None
    lean: float = Field(default=0.0, description="Tilt given to the drift for the view, in units of the horizon's volatility")
    lean_basis: str = Field(default="", description="The record the lean rests on, in words")
    report_on: date | None = Field(default=None, description="The session that reacts to the next earnings report, when within reach")
    report_modelled: bool = False
    notes: list[str] = Field(default_factory=list)
    horizons: list[HorizonForecast] = Field(default_factory=list)


class BookHorizon(BaseModel):
    sessions: int
    equity: dict[str, float] = Field(description="Account equity at the 5th, 25th, 50th, 75th and 95th percentile")
    stops_together: float = Field(description="Chance that two or more stops are touched on the same day within the horizon")
    drawdown_breaker: float = Field(description="Chance that the drawdown breaker is touched within the horizon")
    daily_loss_breaker: float = Field(description="Chance that the daily loss breaker is touched within the horizon")


class BookForecast(BaseModel):
    """The book as it stands at the close, with no trading after it: what it may be worth, and what may trip."""

    equity: float
    cash: float
    high_water: float
    positions: int
    horizons: list[BookHorizon] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ForecastSet(BaseModel):
    """Everything the engine said at one meeting. With the meeting's snapshot and the parameters it names, it can be worked out again to the last digit."""

    arm: str
    session_date: date
    run_id: str
    created_at: datetime
    params_version: int
    params_cutoff: date
    seed: int
    paths: int
    states: list[str]
    state_now: list[float] = Field(description="Probability of each market state after the session's close")
    state_ahead: dict[str, list[float]] = Field(default_factory=dict, description="The same after 1, 5, 10 and 20 sessions")
    tickers: list[TickerForecast] = Field(default_factory=list)
    book: BookForecast | None = None
    problems: list[str] = Field(default_factory=list)


# ── What a forecast turned out to be worth ───────────────────────────────────

class ForecastScore(BaseModel):
    """One name, one horizon, once the horizon has ended."""

    arm: str
    session_date: date
    ticker: str
    sessions: int
    matures_on: date
    status: Literal["scored", "pending", "unpriced"] = Field(
        description="pending: the horizon has not ended. unpriced: it has, but the prices are not at hand."
    )
    realized: float | None = Field(default=None, description="The log return that happened, dividends included")
    pit: float | None = Field(default=None, description="Share of the forecast distribution below what happened, 0 to 1")
    naive_pit: float | None = None
    inside: dict[str, bool] = Field(default_factory=dict, description="Whether it fell in the middle 50%, 80% and 95%")
    naive_inside: dict[str, bool] = Field(default_factory=dict)
    loss: float | None = Field(default=None, description="Mean pinball loss over the kept percentiles: lower is better")
    naive_loss: float | None = None
    stop_touch: float | None = Field(default=None, description="The chance the forecast gave of the stop being touched")
    naive_stop_touch: float | None = None
    stop_touched: bool | None = Field(default=None, description="Whether a low in the horizon reached it. None: there was no stop")
    scored_on: date | None = Field(default=None, description="The last completed session when this was worked out")


class Reliability(BaseModel):
    """How one forecaster did over a group of forecasts."""

    coverage: dict[str, float] = Field(default_factory=dict, description="Share of outcomes inside the middle 50%, 80% and 95%")
    loss: float | None = None
    histogram: list[int] = Field(default_factory=list, description="Outcomes by tenth of the forecast distribution they fell in")
    brier: float | None = Field(default=None, description="Brier score of the stop-touch chances")


class ForecastTally(BaseModel):
    label: str
    scored: int
    sessions: int = Field(description="Distinct days the forecasts were made on: forecasts of one day move together")
    stops: int = Field(default=0, description="Scored forecasts that had a stop")
    stops_touched: int = 0
    engine: Reliability = Field(default_factory=Reliability)
    naive: Reliability = Field(default_factory=Reliability)


class ForecastCard(BaseModel):
    arm: str
    total: int = 0
    scored: int = 0
    pending: int = 0
    unpriced: int = 0
    by_horizon: list[ForecastTally] = Field(default_factory=list)
