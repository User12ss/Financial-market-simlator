"""Scoring every analyst view against what the price went on to do.

Every view is scored, not only the ones that became trades: a view names a
direction and a horizon, and both can be checked. That gives far more
evidence than the trades alone, though views written on the same day rise
and fall together, so they count for less than their number suggests.

How a view is measured:
- From the open of the first session after it was written. The view is formed
  after a close, with that evening's news in hand, and the close does not
  yet reflect that news. The next open is the first price anyone could act on.
- To the close of the session that ends the view's own horizon.
- Dividends included, for the ticker and for the benchmark alike.

A bullish view is right when the ticker rose, a bearish one when it fell. A
ticker that ends exactly where it started is right for neither. In a rising
market almost every bullish view is "right", so each view is also checked
against the benchmark: bullish and it beat the benchmark, or bearish and it
lagged. A neutral view claims no direction and is counted apart.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from data.market_calendar import trading_day_after
from data.snapshot import load_snapshot
from execution.paper import sector_of
from evaluation.prices import Prices
from journal.store import COMPLETED, Journal
from models.evaluation import Scorecard, Tally, ViewScore
from models.signals import SignalTable, TickerView

# A score is final once it has been worked out this many sessions after the view matured.
SETTLE_SESSIONS = 2

CONFIDENCE_BUCKETS = [
    (0.0, "below 0.50"), (0.5, "0.50 to 0.59"), (0.6, "0.60 to 0.69"), (0.7, "0.70 to 0.79"),
    (0.8, "0.80 to 0.89"), (0.9, "0.90 to 1.00"),
]


def standing_runs(journal: Journal, arm: str) -> list[sqlite3.Row]:
    """The run that stands for each session an arm has signals for, oldest session first."""
    latest: dict[str, sqlite3.Row] = {}
    for row in journal.runs(arm):                      # newest first
        if row["status"] in COMPLETED and row["signals_json"] and row["session_date"] not in latest:
            latest[row["session_date"]] = row
    return [latest[session] for session in sorted(latest)]


def standing_tables(journal: Journal, arm: str) -> list[SignalTable]:
    return [SignalTable.model_validate_json(row["signals_json"]) for row in standing_runs(journal, arm)]


def sectors_in(snapshot_path: str | None) -> dict[str, str]:
    """Each ticker's sector as the run's own snapshot had it. Empty when the snapshot is gone."""
    if not snapshot_path or not Path(snapshot_path).is_file():
        return {}
    try:
        snapshot = load_snapshot(snapshot_path)
    except (OSError, ValueError):
        return {}
    return {ticker: sector_of(item) for ticker, item in snapshot.tickers.items()}


def window(view: TickerView) -> tuple[date, date]:
    """The session whose open a view is measured from, and the one whose close ends its horizon."""
    return trading_day_after(view.session_date, 1), trading_day_after(view.session_date, max(1, view.horizon_days))


def score_view(
    view: TickerView, *, run_id: str, arm: str, sector: str, regime: str,
    prices: Prices, benchmark: str, as_of: date,
) -> ViewScore:
    starts_on, matures_on = window(view)
    score = ViewScore(
        run_id=run_id, arm=arm, ticker=view.ticker, session_date=view.session_date, direction=view.direction,
        confidence=view.confidence, horizon_days=view.horizon_days, held=view.held, sector=sector, regime=regime,
        starts_on=starts_on, matures_on=matures_on, status="pending", benchmark=benchmark, scored_as_of=as_of,
    )
    if matures_on > as_of:
        return score
    made = prices.total_return(view.ticker, starts_on, matures_on)
    reference = prices.total_return(benchmark, starts_on, matures_on)
    if made is None or reference is None:
        return score.model_copy(update={"status": "unpriced"})
    excess = made - reference
    right = beat = None
    if view.direction == "bullish":
        right, beat = made > 0, excess > 0
    elif view.direction == "bearish":
        right, beat = made < 0, excess < 0
    return score.model_copy(update={
        "status": "scored", "return_pct": made, "benchmark_return_pct": reference, "excess_pct": excess,
        "direction_right": right, "beat_benchmark": beat,
    })


def score_views(
    journal: Journal, arm: str, *, prices: Prices, benchmark: str, as_of: date,
    earlier: Iterable[ViewScore] = (), sectors: dict[str, dict[str, str]] | None = None,
) -> list[ViewScore]:
    """A score for every view in an arm's standing runs, oldest first.

    A view scored by an earlier evaluation keeps that score once it has
    settled, so that a source which later drops a delisted ticker does not
    turn a known outcome into an unknown one. Settled means it was worked
    out at least SETTLE_SESSIONS sessions after the view matured: a close
    read minutes after the bell can still change, and a dividend can be
    posted a day late. A score that has not settled is worked out again, and
    the earlier one stands only if the prices have gone missing since. A
    score measured against another benchmark is never kept.
    `sectors` maps a run to its tickers' sectors; without it each run's
    snapshot is read when one of its views needs scoring.
    """
    known = {
        (score.run_id, score.ticker): score for score in earlier
        if score.status == "scored" and score.benchmark == benchmark
    }
    scores = []
    for row in standing_runs(journal, arm):
        table = SignalTable.model_validate_json(row["signals_json"])
        regime = table.regime.risk_appetite if table.regime else "none"
        of_run = sectors.get(table.run_id) if sectors is not None else None
        for view in table.views:
            before = known.get((table.run_id, view.ticker))
            if before is not None and settled(before):
                scores.append(before)
                continue
            if of_run is None:
                of_run = sectors_in(row["snapshot_path"])
            score = score_view(
                view, run_id=table.run_id, arm=arm, sector=of_run.get(view.ticker, ""), regime=regime,
                prices=prices, benchmark=benchmark, as_of=as_of,
            )
            scores.append(before if before is not None and score.status == "unpriced" else score)
    return scores


def settled(score: ViewScore) -> bool:
    """Whether a score was worked out long enough after its view matured to be final."""
    return score.scored_as_of is not None and score.scored_as_of >= trading_day_after(score.matures_on, SETTLE_SESSIONS)


# ── The scorecard ────────────────────────────────────────────────────────────

def _tally(label: str, scores: list[ViewScore]) -> Tally:
    """Scored views that took a side, summed up. `scores` holds nothing else."""
    count = len(scores)
    if not count:
        return Tally(label=label, views=0, right=0, beat_benchmark=0)
    right = sum(1 for score in scores if score.direction_right)
    beat = sum(1 for score in scores if score.beat_benchmark)
    # Signed so that a bearish view on something that lagged counts in the view's favour.
    edge = sum(score.excess_pct if score.direction == "bullish" else -score.excess_pct for score in scores)
    return Tally(
        label=label, views=count, right=right, hit_rate=right / count, beat_benchmark=beat, beat_rate=beat / count,
        mean_edge_pct=edge / count, mean_confidence=sum(score.confidence for score in scores) / count,
        margin=0.5 / math.sqrt(count),
    )


def _split(scores: list[ViewScore], key: Callable[[ViewScore], str], order: list[str] | None = None) -> list[Tally]:
    groups: dict[str, list[ViewScore]] = {}
    for score in scores:
        groups.setdefault(key(score), []).append(score)
    labels = [label for label in order if label in groups] if order else sorted(groups)
    return [_tally(label, groups[label]) for label in labels]


def confidence_bucket(confidence: float) -> str:
    label = CONFIDENCE_BUCKETS[0][1]
    for floor, name in CONFIDENCE_BUCKETS:
        if confidence + 1e-9 >= floor:
            label = name
    return label


def scorecard(arm: str, scores: list[ViewScore]) -> Scorecard:
    scored = [score for score in scores if score.status == "scored"]
    sided = [score for score in scored if score.direction != "neutral"]
    return Scorecard(
        arm=arm, total=len(scores), scored=len(scored),
        pending=sum(1 for score in scores if score.status == "pending"),
        unpriced=sum(1 for score in scores if score.status == "unpriced"),
        neutral=len(scored) - len(sided),
        sessions=len({score.session_date for score in scored}),
        overall=_tally("all", sided) if sided else None,
        by_confidence=_split(sided, lambda s: confidence_bucket(s.confidence), [name for _, name in CONFIDENCE_BUCKETS]),
        by_direction=_split(sided, lambda s: s.direction, ["bullish", "bearish"]),
        by_sector=_split(sided, lambda s: s.sector or "unknown"),
        by_regime=_split(sided, lambda s: s.regime, ["risk_on", "neutral", "risk_off", "none"]),
        by_position=_split(sided, lambda s: "held" if s.held else "candidate", ["candidate", "held"]),
    )
