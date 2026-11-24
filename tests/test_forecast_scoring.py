"""Reconciling forecasts with what happened: where the outcome fell, and whether the engine beat the plain forecast."""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from data.market_calendar import trading_day_after, trading_days
from data.models import Bars
from forecast.engine import Subject, naive_grid, run
from forecast.regime import RegimeModel
from forecast.scoring import card, frame_of, in_words, pinball, place, reconcile, score_one, settled, tally
from journal.store import Journal
from models.forecast import ForecastScore, ForecastSet
from tests.forecast_fakes import MONDAY, hand_params, noise, plain

ARM = "desk/fake/a"
SIGMA = 0.015


def a_set(session: date = MONDAY, stop: float | None = 96.0, seed: int = 1, run_id: str = "run-1") -> ForecastSet:
    """What the engine says about one name that is the market: one state, normal days of 1.5%."""
    market = noise(SIGMA, seed=1, end=session)
    params = hand_params([plain("X")], RegimeModel(means=[0.0], vols=[SIGMA], transition=[[1.0]]), cutoff=session)
    result = run(params, session, market, {}, [Subject("X", 100.0, market + noise(1e-7, seed=2, end=session), stop=stop)],
                 seed=seed, paths=20_000)
    return ForecastSet(
        arm=ARM, session_date=session, run_id=run_id, created_at=datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc),
        params_version=1, params_cutoff=params.cutoff, seed=seed, paths=20_000, states=["single"],
        state_now=result.state_now, state_ahead=result.state_ahead, tickers=result.tickers,
    )


def bars_after(session: date, closes: list[float], lows: list[float] | None = None, scale: float = 1.0) -> pd.DataFrame:
    """Bars for `session` (close 100) and the sessions after it, one per given close."""
    days = trading_days(session, trading_day_after(session, len(closes)))
    close = np.array([100.0] + closes) * scale
    low = np.array([99.0] + (lows or [value * 0.995 for value in closes])) * scale
    return pd.DataFrame({"close": close, "adj_close": close, "low": low}, index=days)


def flat(count: int, last: float) -> list[float]:
    return [100.0] * (count - 1) + [last]


# ── Where an outcome falls in a forecast ─────────────────────────────────────

def test_the_place_of_an_outcome_is_read_off_the_kept_percentiles():
    grid = naive_grid(0.01, 1)
    assert place(grid, 0.0) == pytest.approx(0.5)
    assert place(grid, grid[24]) == pytest.approx(0.25) and place(grid, grid[98]) == pytest.approx(0.99)
    assert place(grid, (grid[59] + grid[60]) / 2) == pytest.approx(0.605)
    # Beyond the kept percentiles: somewhere in the last percent, no more is known.
    assert place(grid, grid[0] - 1e-9) == 0.005 and place(grid, -1.0) == 0.005
    assert place(grid, grid[98] + 1e-9) == 0.995 and place(grid, 1.0) == 0.995
    # A distribution with no spread at all, and an outcome right on it: the middle, not one end.
    assert place([0.02] * 99, 0.02) == pytest.approx(0.5)
    # A run of equal percentiles within a distribution: the middle of the run.
    stepped = [0.0] * 10 + [0.01 * step for step in range(1, 90)]
    assert place(stepped, 0.0) == pytest.approx((0.01 + 0.10) / 2)
    assert place(stepped, 0.01) == pytest.approx(0.11) and place(stepped, 0.005) == pytest.approx(0.105)


def test_the_loss_rewards_being_centred_and_being_no_wider_than_needed():
    narrow, wide = naive_grid(0.01, 1), naive_grid(0.03, 1)
    assert pinball([0.02] * 99, 0.02) == 0.0
    assert pinball(narrow, 0.0) < pinball(wide, 0.0)              # both centred: the tighter one is better
    assert pinball(narrow, 0.06) > pinball(wide, 0.06)            # a move the narrow one called all but impossible
    assert pinball(narrow, 0.01) < pinball(narrow, 0.03) < pinball(narrow, 0.05)
    assert pinball(narrow, 0.02) == pytest.approx(pinball(narrow, -0.02))


# ── One forecast ─────────────────────────────────────────────────────────────

def test_a_forecast_is_not_scored_before_its_horizon_ends():
    forecasts = a_set()
    five = forecasts.tickers[0].horizons[1]
    assert five.sessions == 5 and five.matures_on == date(2026, 12, 1)
    frame = bars_after(MONDAY, flat(5, 103.0))
    early = score_one(ARM, MONDAY, forecasts.tickers[0], five, frame, as_of=date(2026, 11, 30))
    assert early.status == "pending" and early.realized is None and early.pit is None and early.inside == {}
    assert early.stop_touch == five.stop_touch and early.matures_on == five.matures_on
    assert score_one(ARM, MONDAY, forecasts.tickers[0], five, frame, as_of=date(2026, 12, 1)).status == "scored"


def test_what_happened_is_placed_in_both_forecasts():
    forecast = a_set().tickers[0]
    five = forecast.horizons[1]
    score = score_one(ARM, MONDAY, forecast, five, bars_after(MONDAY, flat(5, 103.0)), as_of=date(2026, 12, 1))
    assert score.realized == pytest.approx(math.log(1.03))
    assert score.pit == pytest.approx(place(five.grid, math.log(1.03))) and 0.75 < score.pit < 0.9
    assert score.inside == {"50": False, "80": True, "95": True}
    assert score.naive_pit == pytest.approx(place(five.naive_grid, math.log(1.03)))
    assert score.loss == pytest.approx(pinball(five.grid, math.log(1.03)))
    assert score.naive_loss == pytest.approx(pinball(five.naive_grid, math.log(1.03)))
    assert score.scored_on == date(2026, 12, 1) and score.arm == ARM and score.ticker == "X" and score.sessions == 5

    crash = score_one(ARM, MONDAY, forecast, five, bars_after(MONDAY, flat(5, 80.0)), as_of=date(2026, 12, 1))
    assert crash.pit == 0.005 and crash.inside == {"50": False, "80": False, "95": False}
    middle = score_one(ARM, MONDAY, forecast, five, bars_after(MONDAY, flat(5, 100.0)), as_of=date(2026, 12, 1))
    assert middle.inside == {"50": True, "80": True, "95": True}


def test_the_stop_is_touched_by_a_low_on_the_way_and_only_within_the_horizon():
    forecast = a_set(stop=96.0).tickers[0]
    one, five = forecast.horizons[0], forecast.horizons[1]
    dipped = bars_after(MONDAY, [100, 100, 99, 100, 100], lows=[99.5, 99.5, 95.9, 99.5, 99.5])
    assert score_one(ARM, MONDAY, forecast, five, dipped, as_of=date(2026, 12, 1)).stop_touched is True
    assert score_one(ARM, MONDAY, forecast, one, dipped, as_of=date(2026, 12, 1)).stop_touched is False
    exact = bars_after(MONDAY, flat(5, 100.0), lows=[99.5, 96.0, 99.5, 99.5, 99.5])
    assert score_one(ARM, MONDAY, forecast, five, exact, as_of=date(2026, 12, 1)).stop_touched is True
    held = bars_after(MONDAY, flat(5, 100.0), lows=[99.5, 96.01, 99.5, 99.5, 99.5])
    assert score_one(ARM, MONDAY, forecast, five, held, as_of=date(2026, 12, 1)).stop_touched is False
    # The low of the forecast's own day came before the forecast.
    own_day = bars_after(MONDAY, flat(5, 100.0))
    own_day.iloc[0, own_day.columns.get_loc("low")] = 90.0
    assert score_one(ARM, MONDAY, forecast, five, own_day, as_of=date(2026, 12, 1)).stop_touched is False
    # No stop, nothing to touch.
    bare = a_set(stop=None).tickers[0]
    assert score_one(ARM, MONDAY, bare, bare.horizons[1], dipped, as_of=date(2026, 12, 1)).stop_touched is None


def test_a_split_since_the_forecast_changes_nothing():
    """The source restates every bar after a split. The stop was written on the old scale."""
    forecast = a_set(stop=96.0).tickers[0]
    five = forecast.horizons[1]
    closes, lows = [100, 100, 99, 100, 103], [99.5, 99.5, 95.9, 99.5, 99.5]
    before = score_one(ARM, MONDAY, forecast, five, bars_after(MONDAY, closes, lows), as_of=date(2026, 12, 1))
    after = score_one(ARM, MONDAY, forecast, five, bars_after(MONDAY, closes, lows, scale=0.5), as_of=date(2026, 12, 1))
    assert after.stop_touched is True and after.realized == pytest.approx(before.realized)
    # A stop that held still holds when every bar is restated at half the price, and one that was
    # touched is still touched when every bar is restated at twice the price.
    spared = [99.5, 96.01, 99.5, 99.5, 99.5]
    for scale in (0.5, 1.0, 2.0):
        assert score_one(ARM, MONDAY, forecast, five, bars_after(MONDAY, closes, spared, scale=scale),
                         as_of=date(2026, 12, 1)).stop_touched is False
        assert score_one(ARM, MONDAY, forecast, five, bars_after(MONDAY, closes, lows, scale=scale),
                         as_of=date(2026, 12, 1)).stop_touched is True
    assert after.model_dump(exclude={"realized", "pit", "naive_pit", "loss", "naive_loss"}) \
        == before.model_dump(exclude={"realized", "pit", "naive_pit", "loss", "naive_loss"})


def test_missing_or_unusable_prices_leave_the_forecast_unpriced_not_guessed():
    forecast = a_set().tickers[0]
    five = forecast.horizons[1]
    frame = bars_after(MONDAY, flat(5, 103.0))
    for broken in (
        None,
        frame.drop(date(2026, 11, 25)),                     # a session in between is missing
        frame.iloc[:-1],                                    # the last one is
        frame.iloc[1:],                                     # the forecast's own day is
        frame.assign(adj_close=[100, 100, 100, 100, 100, 0.0]),
        frame.assign(low=[99, 99, float("nan"), 99, 99, 99]),
        frame.assign(close=[0.0, 100, 100, 100, 100, 103]),
    ):
        score = score_one(ARM, MONDAY, forecast, five, broken, as_of=date(2026, 12, 1))
        assert score.status == "unpriced" and score.realized is None and score.stop_touched is None


def test_a_snapshots_bars_become_the_table_scoring_reads():
    bars = Bars(dates=[date(2026, 11, 23), date(2026, 11, 24)], open=[1, 2], high=[3, 4], low=[0.5, 1.5],
                close=[2, 3], adj_close=[1.9, 2.9], volume=[10, 20])
    frame = frame_of(bars)
    assert list(frame.index) == bars.dates and list(frame.columns) == ["close", "adj_close", "low"]
    assert frame.at[date(2026, 11, 24), "adj_close"] == 2.9 and frame.at[date(2026, 11, 23), "low"] == 0.5


# ── On record ────────────────────────────────────────────────────────────────

def test_forecasts_are_kept_scored_as_they_mature_and_the_scores_kept_too(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    forecasts = a_set()
    journal.save_forecast_set(forecasts)
    assert journal.forecast_set(ARM, MONDAY) == forecasts and journal.forecast_set(ARM, date(2026, 11, 24)) is None
    assert journal.forecast_set("other/arm", MONDAY) is None and journal.forecast_sets("other/arm") == []

    frame = bars_after(MONDAY, [101, 99, 100, 102, 103, 104, 103, 102, 101, 100])
    asked = []

    def bars(ticker):
        asked.append(ticker)
        return frame

    scores, fresh = reconcile(journal, ARM, bars, as_of=date(2026, 11, 24))
    assert [(s.sessions, s.status) for s in scores] == [(1, "scored"), (5, "pending"), (10, "pending"), (20, "pending")]
    assert [s.sessions for s in fresh] == [1] and fresh[0].realized == pytest.approx(math.log(1.01))
    assert journal.forecast_scores(ARM) == scores and asked == ["X"]

    scores, fresh = reconcile(journal, ARM, bars, as_of=date(2026, 12, 1))
    assert [(s.sessions, s.status) for s in scores] == [(1, "scored"), (5, "scored"), (10, "pending"), (20, "pending")]
    assert [s.sessions for s in fresh] == [5]                       # the one-session forecast is not new again
    assert journal.forecast_scores(ARM) == scores


def test_a_score_is_worked_out_again_until_it_has_settled_and_then_stands(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_forecast_set(a_set())
    first = reconcile(journal, ARM, lambda t: bars_after(MONDAY, [101.0]), as_of=date(2026, 11, 24))[0][0]
    assert not settled(first)
    # The close read on the day was revised afterwards.
    revised, fresh = reconcile(journal, ARM, lambda t: bars_after(MONDAY, [102.0, 100.0]), as_of=date(2026, 11, 25))
    assert revised[0].realized == pytest.approx(math.log(1.02)) and fresh == [] and not settled(revised[0])
    final = reconcile(journal, ARM, lambda t: bars_after(MONDAY, [102.5, 100.0, 100.0]), as_of=date(2026, 11, 27))[0][0]
    assert final.realized == pytest.approx(math.log(1.025)) and settled(final)       # two sessions on (Thursday was a holiday)
    # From here it is not looked at again, whatever the source says or stops saying.
    asked = []
    later = reconcile(journal, ARM, lambda t: asked.append(t), as_of=date(2026, 11, 30))[0][0]
    assert later == final and asked == []


def test_a_score_on_record_survives_prices_that_have_gone_missing_since(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_forecast_set(a_set())
    first = reconcile(journal, ARM, lambda t: bars_after(MONDAY, [101.0]), as_of=date(2026, 11, 24))[0][0]
    again, fresh = reconcile(journal, ARM, lambda t: None, as_of=date(2026, 11, 25))
    assert again[0] == first and fresh == []
    # A forecast that was never priced is tried again, and scored when the prices turn up.
    assert again[1].status == "pending"
    late, fresh = reconcile(journal, ARM, lambda t: None, as_of=date(2026, 12, 1))
    assert late[1].status == "unpriced" and fresh == []
    found, fresh = reconcile(journal, ARM, lambda t: bars_after(MONDAY, flat(6, 103.0)), as_of=date(2026, 12, 2))
    assert found[1].status == "scored" and [s.sessions for s in fresh] == [5]


def test_forecasting_a_session_again_replaces_what_was_said_and_drops_its_scores(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_forecast_set(a_set(run_id="run-1", seed=1))
    journal.save_forecast_set(a_set(session=date(2026, 11, 24), run_id="run-2", seed=2))
    reconcile(journal, ARM, lambda t: bars_after(MONDAY, [101.0, 102.0]), as_of=date(2026, 11, 25))
    assert len(journal.forecast_scores(ARM)) == 8

    again = a_set(run_id="run-3", seed=3)
    journal.save_forecast_set(again)
    assert journal.forecast_set(ARM, MONDAY).run_id == "run-3"
    assert [item.session_date for item in journal.forecast_sets(ARM)] == [MONDAY, date(2026, 11, 24)]
    assert {score.session_date for score in journal.forecast_scores(ARM)} == {date(2026, 11, 24)}


def test_a_journal_written_before_forecasts_existed_gains_the_tables_and_keeps_everything(tmp_path):
    path = tmp_path / "journal.sqlite3"
    Journal(path).close()
    old = sqlite3.connect(path)
    old.execute("DROP TABLE forecast_sets")
    old.execute("DROP TABLE forecast_scores")
    old.commit()
    old.close()
    journal = Journal(path)
    assert journal.forecast_sets(ARM) == [] and journal.forecast_scores(ARM) == []
    journal.save_forecast_set(a_set())
    assert len(journal.forecast_sets(ARM)) == 1


# ── The card ─────────────────────────────────────────────────────────────────

def scored(pit: float, naive_pit: float, sessions: int = 5, session: date = MONDAY, touch=None, touched=None,
           naive_touch=None, loss: float = 0.01, naive_loss: float = 0.02) -> ForecastScore:
    edges = {"50": (0.25, 0.75), "80": (0.10, 0.90), "95": (0.025, 0.975)}
    return ForecastScore(
        arm=ARM, session_date=session, ticker="X", sessions=sessions, matures_on=trading_day_after(session, sessions),
        status="scored", realized=0.0, pit=pit, naive_pit=naive_pit,
        inside={name: low <= pit <= high for name, (low, high) in edges.items()},
        naive_inside={name: low <= naive_pit <= high for name, (low, high) in edges.items()},
        loss=loss, naive_loss=naive_loss, stop_touch=touch, naive_stop_touch=naive_touch, stop_touched=touched,
    )


def test_a_tally_counts_coverage_loss_and_where_the_outcomes_fell():
    scores = [
        scored(0.50, 0.50, touch=0.2, naive_touch=0.5, touched=False),
        scored(0.30, 0.05, touch=0.8, naive_touch=0.5, touched=True, session=date(2026, 11, 24)),
        scored(0.95, 0.99, session=date(2026, 11, 24), loss=0.03, naive_loss=0.02),
        scored(0.005, 0.005, session=date(2026, 11, 25)),
    ]
    result = tally("5 sessions", scores)
    assert (result.scored, result.sessions, result.stops, result.stops_touched) == (4, 3, 2, 1)
    assert result.engine.coverage == {"50": 0.5, "80": 0.5, "95": 0.75}
    assert result.naive.coverage == {"50": 0.25, "80": 0.25, "95": 0.5}
    assert result.engine.loss == pytest.approx(0.015) and result.naive.loss == pytest.approx(0.02)
    assert result.engine.histogram == [1, 0, 0, 1, 0, 1, 0, 0, 0, 1] and sum(result.naive.histogram) == 4
    assert result.engine.brier == pytest.approx((0.2 ** 2 + 0.2 ** 2) / 2)
    assert result.naive.brier == pytest.approx(0.25)
    empty = tally("none", [])
    assert empty.scored == 0 and empty.engine.coverage == {} and empty.engine.loss is None and empty.engine.brier is None
    assert tally("edge", [scored(1.0, 0.0)]).engine.histogram[9] == 1


def test_the_card_is_by_horizon_and_counts_what_is_not_scored_yet():
    pending = ForecastScore(arm=ARM, session_date=MONDAY, ticker="X", sessions=20, matures_on=date(2026, 12, 22),
                            status="pending")
    unpriced = pending.model_copy(update={"sessions": 10, "status": "unpriced"})
    result = card(ARM, [scored(0.5, 0.5, sessions=1), scored(0.4, 0.6, sessions=5), scored(0.6, 0.4, sessions=5),
                        pending, unpriced])
    assert (result.total, result.scored, result.pending, result.unpriced) == (5, 3, 1, 1)
    assert [(item.label, item.scored) for item in result.by_horizon] == [("1 session", 1), ("5 sessions", 2)]
    assert card(ARM, []).by_horizon == [] and card(ARM, [pending]).scored == 0


def test_forecasts_that_are_what_they_claim_show_the_coverage_they_claim_and_too_narrow_ones_do_not():
    """What reconciling is for: outcomes drawn from the forecast's own distribution fall inside each band as often as it says."""
    rng = np.random.default_rng(4)
    honest, narrow = naive_grid(0.01, 5), naive_grid(0.005, 5)
    outcomes = rng.normal(0.0, 0.01 * math.sqrt(5), 4000)
    edges = {"50": (0.25, 0.75), "80": (0.10, 0.90), "95": (0.025, 0.975)}
    scores = []
    for outcome in outcomes:
        pit, naive_pit = place(honest, outcome), place(narrow, outcome)
        scores.append(ForecastScore(
            arm=ARM, session_date=MONDAY, ticker="X", sessions=5, matures_on=date(2026, 12, 1), status="scored",
            realized=outcome, pit=pit, naive_pit=naive_pit,
            inside={name: low <= pit <= high for name, (low, high) in edges.items()},
            naive_inside={name: low <= naive_pit <= high for name, (low, high) in edges.items()},
            loss=pinball(honest, outcome), naive_loss=pinball(narrow, outcome),
        ))
    result = tally("5 sessions", scores)
    assert result.engine.coverage == pytest.approx({"50": 0.5, "80": 0.8, "95": 0.95}, abs=0.025)
    assert result.naive.coverage["50"] < 0.3 and result.naive.coverage["95"] < 0.75
    assert result.engine.loss < result.naive.loss
    assert max(result.engine.histogram) - min(result.engine.histogram) < 120       # flat: about 400 in each tenth
    assert result.naive.histogram[0] > 2 * result.naive.histogram[4]                # piled up in the tails


def test_a_tally_in_words():
    result = tally("5 sessions", [scored(0.5, 0.5), scored(0.3, 0.05, session=date(2026, 11, 24))])
    assert in_words(result) == (
        "5 sessions ahead, 2 forecast(s) made on 2 day(s): the outcome fell 100% inside its 50% band, "
        "100% inside its 80% band, 100% inside its 95% band. The plain forecast: 50% inside its 50% band, "
        "50% inside its 80% band, 100% inside its 95% band. Pinball loss 50% below the plain forecast's."
    )
    worse = tally("1 session", [scored(0.5, 0.5, sessions=1, loss=0.03, naive_loss=0.02)])
    assert in_words(worse).endswith("Pinball loss 50% above the plain forecast's.")
