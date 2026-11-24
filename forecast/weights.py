"""How a record earns a say in the simulation. Until it has one, the answer is exactly zero.

This is the only place where what a model said becomes a number in the
forecast, so the rules are fixed here and nowhere else.

    lean            an analyst's bullish or bearish views tilt a name's drift, by how often
                    views held with that confidence have beaten the benchmark
    event_weight    a seat's stated probability of an event moves the simulated chance of it
                    away from how often such events happen, by how well its past
                    probabilities scored against simply quoting that frequency
"""

from __future__ import annotations

import math
from datetime import date
from statistics import NormalDist
from typing import Iterable

import config
from models.evaluation import ViewScore

_NORMAL = NormalDist()


def lean(scored: int, hits: int, *, minimum: int | None = None, z: float | None = None, cap: float | None = None) -> float:
    """How far views of this kind may tilt a name's drift, in units of the name's volatility over the view's horizon.

    `scored` views of the kind have reached their horizon and `hits` of them
    beat the benchmark. With fewer than `minimum`, or with a hit rate that
    cannot be told from a coin toss (not more than `z` standard errors above
    one half), the lean is zero. Otherwise the hit rate is first pulled
    towards one half, by scored / (scored + minimum), and the lean is the
    normal quantile of that, never more than `cap`.
    """
    minimum = config.FORECAST_TRACK_MIN if minimum is None else minimum
    z = config.FORECAST_TRACK_Z if z is None else z
    cap = config.FORECAST_TILT_CAP if cap is None else cap
    if scored < minimum or scored <= 0:
        return 0.0
    rate = hits / scored
    if rate - z * 0.5 / math.sqrt(scored) <= 0.5:
        return 0.0
    pulled = 0.5 + (rate - 0.5) * scored / (scored + minimum)
    return min(_NORMAL.inv_cdf(pulled), cap)


def track(scores: Iterable[ViewScore], bucket: str, known_by: date) -> tuple[int, int]:
    """Scored views in one confidence bucket whose outcome was known by a session: how many, and how many beat the benchmark.

    Neutral views claim no direction and are not counted. A view whose
    horizon ended after `known_by` was not yet known then, whenever it was
    scored.
    """
    from evaluation.scoring import confidence_bucket

    counted = [
        score for score in scores
        if score.status == "scored" and score.beat_benchmark is not None and score.matures_on <= known_by
        and confidence_bucket(score.confidence) == bucket
    ]
    return len(counted), sum(1 for score in counted if score.beat_benchmark)


def event_weight(cases: int, brier: float, reference: float, *, minimum: int | None = None) -> float:
    """The weight a seat's stated probabilities carry, from 0 (none) towards 1.

    `brier` is the seat's Brier score over `cases` judged events and
    `reference` the score of always quoting how often such events happen.
    The skill is 1 - brier / reference; a seat that did no better than the
    frequency has none, and no weight. What skill there is counts for
    cases / (cases + minimum) of itself.
    """
    minimum = config.FORECAST_TRACK_MIN if minimum is None else minimum
    if cases <= 0 or reference <= 0:
        return 0.0
    skill = 1.0 - brier / reference
    if skill <= 0:
        return 0.0
    return skill * cases / (cases + minimum)


def adopted(frequency: float, stated: float, weight: float) -> float:
    """The probability the simulation uses: the usual frequency, moved towards what the seat said by its weight."""
    return frequency + weight * (stated - frequency)
