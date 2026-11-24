"""How a record earns a say: exactly zero until it has one, and never more than the cap."""

from __future__ import annotations

import math
from datetime import date
from statistics import NormalDist

import pytest

from forecast import weights
from forecast.weights import adopted, event_weight, lean, track
from models.evaluation import ViewScore

RULES = dict(minimum=200, z=1.64, cap=0.25)


# ── An analyst's lean ────────────────────────────────────────────────────────

@pytest.mark.parametrize("scored, hits", [
    (0, 0), (1, 1), (50, 50), (199, 199),       # too few, however good
    (200, 100), (200, 90), (10_000, 5_000),      # no better than a coin toss, or worse
    (200, 111),                                  # 55.5%: 1.56 standard errors above a half, short of 1.64
    (400, 216),                                  # 54%: 1.60 standard errors
])
def test_the_lean_is_exactly_zero_until_the_record_earns_one(scored, hits):
    assert lean(scored, hits, **RULES) == 0.0


def test_the_edge_of_too_few_and_of_a_coin_toss():
    assert lean(199, 150, **RULES) == 0.0 and lean(200, 150, **RULES) > 0.0
    # Exactly z standard errors above a half is not above it.
    scored = 400
    edge = 0.5 + 1.64 * 0.5 / math.sqrt(scored)             # 54.1% of 400 is 216.4: 216 is short, 217 clears
    assert 216 / scored < edge < 217 / scored
    assert lean(scored, 216, **RULES) == 0.0 and lean(scored, 217, **RULES) > 0.0
    assert lean(100, 50, minimum=100, z=0.0, cap=0.25) == 0.0   # one half is never "above" one half
    # In numbers a computer holds exactly: 40 of 64 is 62.5%, and two standard errors of 6.25 points
    # bring it down to one half and not a hair above. One view more does clear it.
    assert lean(64, 40, minimum=64, z=2.0, cap=0.25) == 0.0
    assert lean(64, 41, minimum=64, z=2.0, cap=0.25) > 0.0


def test_a_record_that_clears_the_bar_is_pulled_towards_a_half_before_it_counts():
    scored, hits = 400, 240                                   # 60%
    pulled = 0.5 + (0.6 - 0.5) * 400 / (400 + 200)            # 56.7%
    assert lean(scored, hits, **RULES) == pytest.approx(NormalDist().inv_cdf(pulled))
    assert lean(scored, hits, **RULES) == pytest.approx(0.1679, abs=1e-4)
    # The same hit rate over more views counts for more.
    assert lean(4000, 2400, **RULES) > lean(400, 240, **RULES)


def test_the_lean_never_exceeds_the_cap():
    assert lean(100_000, 90_000, **RULES) == 0.25
    assert lean(100_000, 90_000, minimum=200, z=1.64, cap=0.1) == 0.1
    assert lean(400, 240, minimum=200, z=1.64, cap=0.1) == 0.1


def test_the_three_numbers_come_from_the_configuration(monkeypatch):
    assert lean(400, 240) == pytest.approx(0.1679, abs=1e-4)          # 200, 1.64 and 0.25 as shipped
    monkeypatch.setattr(weights.config, "FORECAST_TRACK_MIN", 500)
    assert lean(400, 240) == 0.0
    monkeypatch.setattr(weights.config, "FORECAST_TRACK_MIN", 200)
    monkeypatch.setattr(weights.config, "FORECAST_TRACK_Z", 5.0)
    assert lean(400, 240) == 0.0
    monkeypatch.setattr(weights.config, "FORECAST_TRACK_Z", 1.64)
    monkeypatch.setattr(weights.config, "FORECAST_TILT_CAP", 0.05)
    assert lean(400, 240) == 0.05


# ── The record a lean is worked out from ─────────────────────────────────────

def score(confidence=0.65, direction="bullish", beat=True, status="scored", matures=date(2026, 11, 10)) -> ViewScore:
    return ViewScore(
        run_id="r", arm="a", ticker="X", session_date=date(2026, 10, 27), direction=direction, confidence=confidence,
        horizon_days=10, held=False, starts_on=date(2026, 10, 28), matures_on=matures, status=status,
        beat_benchmark=beat if direction != "neutral" and status == "scored" else None,
    )


def test_only_scored_views_that_took_a_side_in_the_same_confidence_bucket_and_were_known_by_then_count():
    scores = [
        score(beat=True), score(beat=False), score(confidence=0.69, beat=True),       # counted: 3, 2 of them hits
        score(confidence=0.70),                                                        # the next bucket
        score(confidence=0.59),                                                        # the one below
        score(direction="neutral"),                                                    # took no side
        score(status="pending"), score(status="unpriced"),                             # no outcome
        score(matures=date(2026, 11, 21)),                                             # its outcome came later
    ]
    assert track(scores, "0.60 to 0.69", known_by=date(2026, 11, 20)) == (3, 2)
    assert track(scores, "0.60 to 0.69", known_by=date(2026, 11, 21)) == (4, 3)
    assert track(scores, "0.70 to 0.79", known_by=date(2026, 11, 20)) == (1, 1)
    assert track([], "0.60 to 0.69", known_by=date(2026, 11, 20)) == (0, 0)


# ── A seat's stated probabilities ────────────────────────────────────────────

@pytest.mark.parametrize("cases, brier, reference", [
    (0, 0.1, 0.25),          # nothing judged yet
    (500, 0.25, 0.25),       # no better than quoting the frequency
    (500, 0.30, 0.25),       # worse
    (500, 0.10, 0.0),        # nothing to compare with
])
def test_the_weight_is_exactly_zero_until_the_seat_beats_the_frequency(cases, brier, reference):
    assert event_weight(cases, brier, reference, minimum=200) == 0.0


def test_skill_counts_for_more_as_the_cases_add_up():
    assert event_weight(200, 0.20, 0.25, minimum=200) == pytest.approx(0.2 * 0.5)
    assert event_weight(600, 0.20, 0.25, minimum=200) == pytest.approx(0.2 * 0.75)
    assert event_weight(1, 0.0, 0.25, minimum=200) == pytest.approx(1 / 201)
    assert event_weight(10 ** 9, 0.0, 0.25, minimum=200) == pytest.approx(1.0)
    assert event_weight(200, 0.20, 0.25) == pytest.approx(0.1)                   # 200 as shipped


def test_the_probability_used_is_the_frequency_moved_by_the_weight():
    assert adopted(0.10, 0.60, 0.0) == 0.10                 # no weight: what the seat said changes nothing
    assert adopted(0.10, 0.60, 1.0) == pytest.approx(0.60)
    assert adopted(0.10, 0.60, 0.2) == pytest.approx(0.20)
    assert adopted(0.30, 0.10, 0.5) == pytest.approx(0.20)
