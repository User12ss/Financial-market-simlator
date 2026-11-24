"""What each forecast turned out to be worth, once its horizon has ended.

A forecast is a distribution, so it is not right or wrong: it is measured by
where the outcome fell in it. If the forecasts are any good, the outcome
falls inside the middle half of the distribution about half the time, and
inside the middle 95% about 95% of the time. Beside every forecast stands
the plain one (historical volatility and nothing else), measured the same
way: the engine earns its keep only where it does better.

    reconcile   score every forecast on record that has matured, and keep the scores
    card        how the engine and the plain forecast did, by horizon
"""

from __future__ import annotations

import math
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from data.market_calendar import trading_day_after, trading_days
from data.models import Bars
from journal.store import Journal
from models.forecast import (
    COVERAGES, GRID, HORIZONS, ForecastCard, ForecastScore, ForecastTally, HorizonForecast,
    Reliability, TickerForecast,
)

# A score is final once it has been worked out this many sessions after the forecast matured:
# a close read minutes after the bell can still change.
SETTLE_SESSIONS = 2
_SHARES = np.array(GRID) / 100.0
_EDGES = {"50": (0.25, 0.75), "80": (0.10, 0.90), "95": (0.025, 0.975)}


def frame_of(bars: Bars) -> pd.DataFrame:
    """A snapshot's bars as a table by session."""
    return pd.DataFrame({"close": bars.close, "adj_close": bars.adj_close, "low": bars.low}, index=bars.dates)


def place(grid: list[float], outcome: float) -> float:
    """The share of a forecast distribution that lies below the outcome, read off its kept percentiles.

    Below the 1st percentile it is put at half a percent, above the 99th at
    99.5: the kept percentiles do not say how far out the tails go.
    """
    if outcome < grid[0]:
        return 0.005
    if outcome > grid[-1]:
        return 0.995
    first, after = np.searchsorted(grid, outcome, side="left"), np.searchsorted(grid, outcome, side="right")
    if after > first:
        # It equals one kept percentile or a run of equal ones (a distribution with no spread at all,
        # say): the middle of the run, not its top.
        return float((_SHARES[first] + _SHARES[after - 1]) / 2)
    return float(np.interp(outcome, grid, _SHARES))


def pinball(grid: list[float], outcome: float) -> float:
    """Mean pinball loss over the kept percentiles. Lower is better; it rewards a distribution that is both
    centred where the outcome fell and no wider than it needs to be."""
    miss = outcome - np.array(grid)
    return float(np.mean(np.where(miss >= 0, _SHARES * miss, (_SHARES - 1) * miss)))


def score_one(
    arm: str, session: date, forecast: TickerForecast, horizon: HorizonForecast, frame: pd.DataFrame | None, as_of: date,
) -> ForecastScore:
    score = ForecastScore(
        arm=arm, session_date=session, ticker=forecast.ticker, sessions=horizon.sessions, matures_on=horizon.matures_on,
        status="pending", stop_touch=horizon.stop_touch, naive_stop_touch=horizon.naive_stop_touch, scored_on=as_of,
    )
    if horizon.matures_on > as_of:
        return score
    days = trading_days(session, horizon.matures_on)
    if frame is None or any(day not in frame.index for day in days):
        return score.model_copy(update={"status": "unpriced"})
    window = frame.loc[days]
    start, end = float(window["adj_close"].iloc[0]), float(window["adj_close"].iloc[-1])
    lows, base = window["low"].iloc[1:].astype(float), float(window["close"].iloc[0])
    usable = all(math.isfinite(v) and v > 0 for v in (start, end, base)) and bool(np.isfinite(lows).all() and (lows > 0).all())
    if not usable:
        return score.model_copy(update={"status": "unpriced"})

    outcome = math.log(end / start)
    pit, naive_pit = place(horizon.grid, outcome), place(horizon.naive_grid, outcome)
    touched = None
    if forecast.stop is not None:
        # Both sides as a share of the close on the forecast's day: a split since then moves neither.
        touched = bool(float(lows.min()) / base <= forecast.stop / forecast.close)
    return score.model_copy(update={
        "status": "scored", "realized": outcome, "pit": pit, "naive_pit": naive_pit,
        "inside": {name: low <= pit <= high for name, (low, high) in _EDGES.items()},
        "naive_inside": {name: low <= naive_pit <= high for name, (low, high) in _EDGES.items()},
        "loss": pinball(horizon.grid, outcome), "naive_loss": pinball(horizon.naive_grid, outcome),
        "stop_touched": touched,
    })


def settled(score: ForecastScore) -> bool:
    return (score.status == "scored" and score.scored_on is not None
            and score.scored_on >= trading_day_after(score.matures_on, SETTLE_SESSIONS))


def reconcile(
    journal: Journal, arm: str, bars: Callable[[str], pd.DataFrame | None], as_of: date,
) -> tuple[list[ForecastScore], list[ForecastScore]]:
    """Score every forecast an arm has on record, and keep the scores. Returns all of them, and those scored for the first time.

    `bars` gives a name's daily bars, or None when they are not at hand; a
    forecast whose prices are missing stays unpriced and is tried again next
    time. A score that has settled is kept as it is. One that has not is
    worked out again, and stands only if the prices have gone missing since.
    """
    before = {(s.session_date, s.ticker, s.sessions): s for s in journal.forecast_scores(arm)}
    scores, fresh = [], []
    frames: dict[str, pd.DataFrame | None] = {}
    for forecasts in journal.forecast_sets(arm):
        for forecast in forecasts.tickers:
            for horizon in forecast.horizons:
                earlier = before.get((forecasts.session_date, forecast.ticker, horizon.sessions))
                if earlier is not None and settled(earlier):
                    scores.append(earlier)
                    continue
                if horizon.matures_on <= as_of and forecast.ticker not in frames:
                    frames[forecast.ticker] = bars(forecast.ticker)
                score = score_one(arm, forecasts.session_date, forecast, horizon, frames.get(forecast.ticker), as_of)
                if earlier is not None and earlier.status == "scored" and score.status == "unpriced":
                    score = earlier
                scores.append(score)
                if score.status == "scored" and (earlier is None or earlier.status != "scored"):
                    fresh.append(score)
    journal.save_forecast_scores(scores)
    return scores, fresh


# ── The card ─────────────────────────────────────────────────────────────────

def _reliability(pits: list[float], inside: list[dict[str, bool]], losses: list[float],
                 chances: list[tuple[float, bool]]) -> Reliability:
    histogram = [0] * 10
    for pit in pits:
        histogram[min(int(pit * 10), 9)] += 1
    return Reliability(
        coverage={name: sum(1 for item in inside if item[name]) / len(inside) for name in COVERAGES} if inside else {},
        loss=sum(losses) / len(losses) if losses else None,
        histogram=histogram,
        brier=sum((chance - (1.0 if touched else 0.0)) ** 2 for chance, touched in chances) / len(chances) if chances else None,
    )


def tally(label: str, scores: list[ForecastScore]) -> ForecastTally:
    """Scored forecasts of one group, summed up. `scores` holds nothing else."""
    stops = [score for score in scores if score.stop_touched is not None and score.stop_touch is not None]
    return ForecastTally(
        label=label, scored=len(scores), sessions=len({score.session_date for score in scores}),
        stops=len(stops), stops_touched=sum(1 for score in stops if score.stop_touched),
        engine=_reliability([s.pit for s in scores], [s.inside for s in scores], [s.loss for s in scores],
                            [(s.stop_touch, s.stop_touched) for s in stops]),
        naive=_reliability([s.naive_pit for s in scores], [s.naive_inside for s in scores], [s.naive_loss for s in scores],
                           [(s.naive_stop_touch, s.stop_touched) for s in stops if s.naive_stop_touch is not None]),
    )


def card(arm: str, scores: list[ForecastScore]) -> ForecastCard:
    scored = [score for score in scores if score.status == "scored"]
    return ForecastCard(
        arm=arm, total=len(scores), scored=len(scored),
        pending=sum(1 for score in scores if score.status == "pending"),
        unpriced=sum(1 for score in scores if score.status == "unpriced"),
        by_horizon=[
            tally(f"{sessions} session" + ("" if sessions == 1 else "s"), [s for s in scored if s.sessions == sessions])
            for sessions in HORIZONS if any(s.sessions == sessions for s in scored)
        ],
    )


def in_words(tally_: ForecastTally) -> str:
    """One horizon's record as a sentence, for the meeting and the report."""
    def shares(side: Reliability) -> str:
        return ", ".join(f"{side.coverage[name] * 100:.0f}% inside its {name}% band" for name in COVERAGES)

    text = (f"{tally_.label} ahead, {tally_.scored} forecast(s) made on {tally_.sessions} day(s): the outcome fell "
            f"{shares(tally_.engine)}. The plain forecast: {shares(tally_.naive)}.")
    if tally_.engine.loss is not None and tally_.naive.loss:
        gap = (tally_.engine.loss / tally_.naive.loss - 1) * 100
        text += f" Pinball loss {abs(gap):.0f}% {'below' if gap < 0 else 'above'} the plain forecast's."
    return text
