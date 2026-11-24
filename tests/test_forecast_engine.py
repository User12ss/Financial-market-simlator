"""The simulation: it agrees with what can be worked out by formula, and gives the same answer every time."""

from __future__ import annotations

import math
from datetime import date
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from forecast import engine
from forecast.engine import Book, Subject, naive_grid, naive_stop_touch, run, state_today
from forecast.params import ForecastError
from forecast.regime import RegimeModel
from models.forecast import GRID, HORIZONS
from tests.forecast_fakes import END, MONDAY, at, hand_params, noise, normal_pool, plain

NORMAL = NormalDist()
PATHS = 20_000
TWO_STATES = RegimeModel(means=[0.0, 0.0], vols=[0.006, 0.02], transition=[[0.98, 0.02], [0.05, 0.95]])


def lone(sigma: float = 0.015, **subject) -> tuple:
    """One name that is the market and nothing else: one state, no drift, no jumps, normal days."""
    market = noise(sigma, seed=1)
    params = hand_params([plain("X")], RegimeModel(means=[0.0], vols=[sigma], transition=[[1.0]]))
    own = market + noise(1e-7, seed=2)
    return params, market, Subject("X", 100.0, own, **subject)


def forecast_of(result, ticker="X"):
    return next(item for item in result.tickers if item.ticker == ticker)


def horizon(forecast, sessions):
    return next(item for item in forecast.horizons if item.sessions == sessions)


def median(forecast, sessions):
    return horizon(forecast, sessions).grid[49]


# ── Against the formulas ─────────────────────────────────────────────────────

def leaves_at(distance: float, width: float, sigma: float, days: int, terms: int = 400) -> float:
    """Chance that a driftless walk leaves a band `width` wide within `days`, at the edge `distance` behind it.

    The textbook series for a Brownian motion between two barriers: in the
    long run distance-to-the-other-edge over width, less what has not left yet.
    """
    x = width - distance
    total = x / width
    for n in range(1, terms):
        total += (2 * (-1) ** n / (n * math.pi) * math.sin(n * math.pi * x / width)
                  * math.exp(-(n * math.pi * sigma) ** 2 * days / (2 * width * width)))
    return total


@pytest.mark.parametrize("sigma, stop", [(0.015, 97.0), (0.01, 98.0), (0.02, 98.0), (0.01, 95.0)])
def test_target_before_stop_agrees_with_the_formula_for_a_driftless_walk(sigma, stop):
    """Completion standard: one state, no jumps, no drift. The simulated chances are the formula's, within simulation error."""
    params, market, subject = lone(sigma, stop=stop)
    paths = 60_000
    forecast = forecast_of(run(params, MONDAY, market, {}, [subject], seed=11, paths=paths, target_r=2.0))
    assert forecast.target == pytest.approx(100 + 2 * (100 - stop))
    down, up = -math.log(stop / 100), math.log(forecast.target / 100)
    for item in forecast.horizons:
        for simulated, exact in (
            (item.target_first, leaves_at(up, down + up, sigma, item.sessions)),
            (item.stop_first, leaves_at(down, down + up, sigma, item.sessions)),
            # A stop alone: twice the chance of ending below it (the reflection principle).
            (item.stop_touch, 2 * NORMAL.cdf(-down / (sigma * math.sqrt(item.sessions)))),
        ):
            error = 4 * math.sqrt(max(exact * (1 - exact), 1e-4) / paths)
            assert simulated == pytest.approx(exact, abs=error), (item.sessions, simulated, exact)


def test_in_the_long_run_the_chances_are_the_two_distances_in_proportion():
    params, market, subject = lone(0.02, stop=98.0)
    item = horizon(forecast_of(run(params, MONDAY, market, {}, [subject], seed=3, paths=60_000, target_r=2.0)), 20)
    down, up = -math.log(0.98), math.log(1.04)
    assert item.target_first == pytest.approx(down / (down + up), abs=0.008)
    assert item.target_first + item.stop_first == pytest.approx(1.0, abs=0.002)


def test_on_a_day_that_reaches_both_the_stop_is_taken_to_have_come_first():
    """Daily bars cannot say which came first. With moves far larger than the gap between the two, the stop wins the tie."""
    params, market, subject = lone(0.10, stop=99.0)
    item = horizon(forecast_of(run(params, MONDAY, market, {}, [subject], seed=3, paths=PATHS, target_r=1.0)), 1)
    # The two are the same distance away, so each is reached first equally often in truth.
    assert item.stop_first > 0.55 > 0.45 > item.target_first
    assert item.stop_first + item.target_first > 0.9


def test_where_the_price_ends_is_the_normal_distribution_it_was_drawn_from():
    sigma = 0.012
    params, market, subject = lone(sigma)
    forecast = forecast_of(run(params, MONDAY, market, {}, [subject], seed=5, paths=60_000))
    for item in forecast.horizons:
        spread = sigma * math.sqrt(item.sessions)
        for percent in (5, 25, 50, 75, 95):
            assert item.grid[percent - 1] == pytest.approx(spread * NORMAL.inv_cdf(percent / 100), abs=0.03 * spread)
        assert item.quantiles["50"] == pytest.approx(100 * math.exp(item.grid[49]), abs=1e-4)
        assert list(item.quantiles) == ["5", "25", "50", "75", "95"]
        assert len(item.grid) == len(GRID) == 99 and item.grid == sorted(item.grid)


def test_the_largest_fall_on_the_way_agrees_with_the_formula_too():
    """For a driftless walk the lowest point by day n is distributed like the size of the move by day n."""
    sigma = 0.012
    params, market, subject = lone(sigma)
    forecast = forecast_of(run(params, MONDAY, market, {}, [subject], seed=6, paths=60_000))
    for item in forecast.horizons:
        spread = sigma * math.sqrt(item.sessions)
        for mark, percent in (("50", 0.5), ("75", 0.75), ("95", 0.95)):
            exact = (1 - math.exp(-spread * NORMAL.inv_cdf((1 + percent) / 2))) * 100
            assert item.adverse[mark] == pytest.approx(exact, rel=0.04, abs=0.02)
    assert horizon(forecast, 20).adverse["95"] > horizon(forecast, 5).adverse["95"] > horizon(forecast, 5).adverse["50"] > 0


# ── The same every time ──────────────────────────────────────────────────────

def two_names():
    market = noise(0.01, seed=1)
    params = hand_params([plain("A", beta=1.2), plain("B", beta=0.7, jump_rate=0.02, jumps=[-0.08, 0.06])], TWO_STATES,
                         state_vol=[0.8, 1.6])
    a = Subject("A", 50.0, 1.2 * market + noise(0.012, seed=2), stop=47.0)
    b = Subject("B", 80.0, 0.7 * market + noise(0.009, seed=3), stop=75.0, shares=10)
    return params, market, a, b


def test_the_same_seed_gives_the_same_forecast_to_the_last_digit_and_another_seed_does_not():
    """Completion standard: worked out again for the same meeting, a forecast is the same digit for digit."""
    params, market, a, b = two_names()
    book = Book(cash=5000.0, equity=5800.0, high_water=6000.0)
    first = run(params, MONDAY, market, {}, [a, b], seed=20261123, paths=PATHS, book=book)
    again = run(params, MONDAY, market, {}, [b, a], seed=20261123, paths=PATHS, book=book)
    assert [item.model_dump_json() for item in first.tickers] == [item.model_dump_json() for item in again.tickers]
    assert first.book.model_dump_json() == again.book.model_dump_json()
    assert first.state_now == again.state_now and first.state_ahead == again.state_ahead
    assert [item.ticker for item in first.tickers] == ["A", "B"]
    other = run(params, MONDAY, market, {}, [a, b], seed=20261124, paths=PATHS, book=book)
    assert other.tickers[0].horizons[2].grid != first.tickers[0].horizons[2].grid


def test_leaving_a_name_out_does_not_change_anothers_forecast():
    params, market, a, b = two_names()
    both = run(params, MONDAY, market, {}, [a, b], seed=4, paths=PATHS)
    alone = run(params, MONDAY, market, {}, [a], seed=4, paths=PATHS)
    assert forecast_of(alone, "A").model_dump_json() == forecast_of(both, "A").model_dump_json()


def test_names_share_one_market_so_they_fall_together():
    market = noise(0.015, seed=1)
    params = hand_params([plain("A"), plain("B")], RegimeModel(means=[0.0], vols=[0.015], transition=[[1.0]]))
    held = [Subject(name, 100.0, market + noise(1e-7, seed=seed), stop=96.0, shares=10)
            for name, seed in (("A", 2), ("B", 3))]
    result = run(params, MONDAY, market, {}, held, seed=8, paths=PATHS,
                 book=Book(cash=0.0, equity=2000.0, high_water=2000.0))
    alone = horizon(forecast_of(result, "A"), 10).stop_touch
    together = result.book.horizons[2].stops_together
    # Two names that are both the market: when one stop goes, so does the other, nearly always on the same day.
    assert together == pytest.approx(alone, abs=0.03) and together > 0.2
    # Had they moved independently, both going on one day would be rare.
    independent = hand_params([plain("A", beta=0.0), plain("B", beta=0.0)],
                              RegimeModel(means=[0.0], vols=[0.015], transition=[[1.0]]))
    apart = [Subject(name, 100.0, noise(0.015, seed=seed), stop=96.0, shares=10) for name, seed in (("A", 2), ("B", 3))]
    separate = run(independent, MONDAY, market, {}, apart, seed=8, paths=PATHS,
                   book=Book(cash=0.0, equity=2000.0, high_water=2000.0))
    assert separate.book.horizons[2].stops_together < together / 4


# ── The market's state ───────────────────────────────────────────────────────

def test_the_state_is_carried_on_from_the_cutoff_by_the_returns_since():
    params = hand_params([plain("X")], TWO_STATES, state_at_cutoff=[0.9, 0.1])
    quiet = noise(0.006, seed=1)
    quiet.loc[MONDAY] = 0.001
    assert state_today(params, quiet, MONDAY)[0] > 0.9
    crash = quiet.copy()
    crash.loc[MONDAY] = -0.06
    assert state_today(params, crash, MONDAY)[1] > 0.95
    # The cutoff day itself: nothing has happened since, the estimate's own last word stands.
    assert list(state_today(params, quiet, END)) == [0.9, 0.1]


def test_a_missing_index_bar_or_parameters_from_the_future_stop_the_forecast_in_words():
    params = hand_params([plain("X")], TWO_STATES)
    market = noise(0.006, seed=1)
    with pytest.raises(ForecastError, match="index's return for 2026-11-23 is not in the snapshot"):
        state_today(params, market.drop(MONDAY), MONDAY)
    with pytest.raises(ForecastError, match="and 1 more session"):
        state_today(params, market.drop([MONDAY]), date(2026, 11, 24))
    with pytest.raises(ForecastError, match="estimated on data to 2026-11-20, after 2026-11-13"):
        state_today(params, market, date(2026, 11, 13))


def test_a_turbulent_market_widens_every_names_band_and_the_calm_one_returns():
    params = hand_params([plain("X", beta=1.0)], TWO_STATES, state_vol=[0.8, 1.6])
    calm = noise(0.006, seed=1)
    wild = calm.copy()
    wild.loc[MONDAY] = -0.07
    subject = lambda market: Subject("X", 100.0, market + noise(0.01, seed=2))      # noqa: E731
    quiet = run(params, MONDAY, calm, {}, [subject(calm)], seed=2, paths=PATHS)
    rough = run(params, MONDAY, wild, {}, [subject(calm)], seed=2, paths=PATHS)
    assert rough.state_now[1] > 0.95 > 0.1 > quiet.state_now[1]
    width = lambda result, sessions: (horizon(forecast_of(result), sessions).grid[94]            # noqa: E731
                                      - horizon(forecast_of(result), sessions).grid[4])
    assert width(rough, 5) > 1.5 * width(quiet, 5)
    # Twenty sessions on the chain has had time to move: the turbulent state is less sure, the calm one less too.
    assert rough.state_ahead["20"][1] < rough.state_ahead["1"][1]
    assert quiet.state_ahead["20"][1] > quiet.state_ahead["1"][1]
    assert set(quiet.state_ahead) == {"1", "5", "10", "20"}


def test_a_names_own_volatility_is_scaled_by_the_state_relative_to_todays_mix():
    """Today's volatility already reflects today's state: staying in it changes nothing, leaving it does."""
    stay = RegimeModel(means=[0.0, 0.0], vols=[1e-9, 1e-9], transition=[[1.0, 0.0], [0.0, 1.0]])
    market = noise(1e-9, seed=1)
    own = noise(0.01, seed=2)
    width = lambda result: horizon(forecast_of(result), 10).grid[83] - horizon(forecast_of(result), 10).grid[15]   # noqa: E731
    base = run(hand_params([plain("X", beta=0.0)], stay, state_vol=[1.0, 1.0]), MONDAY, market, {},
               [Subject("X", 100.0, own)], seed=1, paths=PATHS)
    same = run(hand_params([plain("X", beta=0.0)], stay, state_vol=[0.5, 3.0]), MONDAY, market, {},
               [Subject("X", 100.0, own)], seed=1, paths=PATHS)
    assert width(same) == pytest.approx(width(base), rel=1e-9)
    # Today in the first state for certain (the estimate's last day is today), tomorrow and after in the second.
    swap = RegimeModel(means=[0.0, 0.0], vols=[1e-9, 1e-9], transition=[[0.0, 1.0], [0.0, 1.0]])
    moved = run(hand_params([plain("X", beta=0.0)], swap, state_vol=[0.5, 1.5], cutoff=MONDAY), MONDAY, market, {},
                [Subject("X", 100.0, own)], seed=1, paths=PATHS)
    assert moved.state_now == [1.0, 0.0] and moved.state_ahead["1"] == [0.0, 1.0]
    assert width(moved) == pytest.approx(3 * width(base), abs=5e-6)      # the kept percentiles are rounded


# ── Jumps, reports and the lean ──────────────────────────────────────────────

def test_jumps_fatten_the_tail_they_come_from_and_leave_the_middle_alone():
    market = noise(0.01, seed=1)
    own = noise(0.01, seed=2)
    plain_result = run(hand_params([plain("X", beta=0.0)]), MONDAY, market, {}, [Subject("X", 100.0, own)], seed=9, paths=PATHS)
    jumpy = run(hand_params([plain("X", beta=0.0, jump_rate=0.03, jumps=[-0.15])]), MONDAY, market, {},
                [Subject("X", 100.0, own)], seed=9, paths=PATHS)
    before, after = horizon(forecast_of(plain_result), 20), horizon(forecast_of(jumpy), 20)
    assert after.grid[0] < before.grid[0] - 0.08                  # the 1st percentile: about a jump lower
    assert after.grid[94] == pytest.approx(before.grid[94], abs=0.02)
    assert after.adverse["95"] > before.adverse["95"] + 5


def test_a_jump_is_taken_at_the_open_so_a_stop_it_passes_is_sold_below_the_stop():
    """A name that only ever jumps down 20%: the stop at -5% is touched on the jump, and the book is sold at the open."""
    market = noise(1e-9, seed=1)
    params = hand_params([plain("X", beta=0.0, jump_rate=1.0, jumps=[-0.2231435513])],
                         RegimeModel(means=[0.0], vols=[1e-9], transition=[[1.0]]))
    result = run(params, MONDAY, market, {}, [Subject("X", 100.0, noise(0.01, seed=2), stop=95.0, shares=10)],
                 seed=1, paths=2000, book=Book(cash=0.0, equity=1000.0, high_water=1000.0))
    assert horizon(forecast_of(result), 1).stop_touch == 1.0
    assert result.book.horizons[0].equity["50"] == pytest.approx(800.0, abs=0.01)      # 10 shares at the open of 80, not at 95


def reporting(lag: int, earnings: list[float] | None = None, **changes):
    return plain("X", beta=0.0, report_lag=lag, earnings=[-8.0, 8.0] if earnings is None else earnings,
                 earnings_from="own" if earnings is None or earnings else "none", **changes)


def test_a_report_inside_the_horizon_widens_the_days_after_it_and_not_the_days_before():
    market = noise(0.01, seed=1)
    own = noise(0.01, seed=2)
    quiet = run(hand_params([reporting(0)]), MONDAY, market, {}, [Subject("X", 100.0, own)], seed=5, paths=PATHS)
    # The source says Wednesday 25 November, and this name reports before the open: Wednesday reacts, the second day.
    told = run(hand_params([reporting(0)]), MONDAY, market, {},
               [Subject("X", 100.0, own, next_report=date(2026, 11, 25))], seed=5, paths=PATHS)
    forecast = forecast_of(told)
    assert forecast.report_on == date(2026, 11, 25) and forecast.report_modelled
    assert horizon(forecast, 1).grid == horizon(forecast_of(quiet), 1).grid
    spread = lambda item: item.grid[94] - item.grid[4]              # noqa: E731
    assert spread(horizon(forecast, 5)) > 2.5 * spread(horizon(forecast_of(quiet), 5))
    # A report move is a multiple of the name's volatility today: eight of them either way, about 13% in all.
    assert spread(horizon(forecast, 5)) == pytest.approx(2 * 8 * forecast.own_vol, rel=0.35)
    # A report day replaces that day's ordinary move: with report moves of nothing at all, the day is flat.
    flat = forecast_of(run(hand_params([reporting(0, earnings=[0.0])]), MONDAY, market, {},
                           [Subject("X", 100.0, own, next_report=date(2026, 11, 24))], seed=5, paths=PATHS))
    assert spread(horizon(flat, 1)) == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("reports_on, lag, reacts_on", [
    (date(2026, 11, 24), 1, date(2026, 11, 25)),       # after Tuesday's close: Wednesday reacts
    (date(2026, 11, 26), 0, date(2026, 11, 27)),       # dated on Thanksgiving: the next session reacts,
    (date(2026, 11, 26), 1, date(2026, 11, 27)),       # whatever the name usually does: the estimate's own rule
    (date(2026, 11, 28), 1, date(2026, 11, 30)),       # dated on a Saturday: Monday
    (date(2026, 11, 28), 0, date(2026, 11, 30)),
    (date(2026, 12, 22), 0, date(2026, 12, 22)),       # the twentieth session
    (date(2026, 12, 23), 0, None),                     # the twenty-first: beyond the horizon
    (date(2026, 12, 22), 1, None),
    (date(2026, 11, 23), 0, None),                     # today's, already in the bars
    (date(2026, 11, 20), 1, None),
])
def test_which_simulated_day_reacts_to_the_next_report(reports_on, lag, reacts_on):
    market = noise(0.01, seed=1)
    forecast = forecast_of(run(hand_params([reporting(lag)]), MONDAY, market, {},
                               [Subject("X", 100.0, noise(0.01, seed=2), next_report=reports_on)], seed=5, paths=2000))
    assert forecast.report_on == reacts_on and forecast.report_modelled == (reacts_on is not None)
    assert horizon(forecast, 20).matures_on == date(2026, 12, 22) and horizon(forecast, 1).matures_on == date(2026, 11, 24)


def test_a_report_with_no_past_report_days_to_draw_from_is_said_not_made_up():
    market = noise(0.01, seed=1)
    own = noise(0.01, seed=2)
    none = hand_params([reporting(0, earnings=[], notes=["Too few report days."])])
    forecast = forecast_of(run(none, MONDAY, market, {}, [Subject("X", 100.0, own, next_report=date(2026, 11, 25))],
                               seed=5, paths=PATHS))
    assert forecast.report_on == date(2026, 11, 25) and not forecast.report_modelled
    assert forecast.notes == ["Too few report days.",
                              "An earnings report moves the session of 2026-11-25, inside the horizon, and is not modelled."]
    quiet = forecast_of(run(none, MONDAY, market, {}, [Subject("X", 100.0, own)], seed=5, paths=PATHS))
    assert horizon(forecast, 5).grid == horizon(quiet, 5).grid


def test_a_report_day_in_the_snapshots_window_does_not_raise_the_volatility_expected_next():
    """The same rule as in the estimate: a report day's move is the report's, not a sign of rougher days to come."""
    market = noise(0.01, seed=1)
    own = noise(0.01, seed=2)
    day = own.index[-3]
    own.loc[day] = 0.03                                             # a large day, but not large enough to be a jump
    told = forecast_of(run(hand_params([plain("X", beta=0.0, report_days=[day])]), MONDAY, market, {},
                           [Subject("X", 100.0, own)], seed=5, paths=2000))
    untold = forecast_of(run(hand_params([plain("X", beta=0.0)]), MONDAY, market, {},
                             [Subject("X", 100.0, own)], seed=5, paths=2000))
    assert untold.own_vol > 1.15 * told.own_vol
    calm = own.copy()
    calm.loc[day] = 0.0
    without = forecast_of(run(hand_params([plain("X", beta=0.0, report_days=[day])]), MONDAY, market, {},
                              [Subject("X", 100.0, calm)], seed=5, paths=2000))
    assert told.own_vol == pytest.approx(without.own_vol, rel=1e-12)


def test_a_lean_shifts_the_middle_by_its_share_of_the_horizons_volatility_and_stops_at_the_views_horizon():
    market = noise(0.01, seed=1)
    own = noise(0.01, seed=2)
    params = hand_params([plain("X", beta=0.0)])
    view = dict(confidence=0.65, view_horizon=10, lean=0.25, lean_basis="earned")
    level = forecast_of(run(params, MONDAY, market, {}, [Subject("X", 100.0, own)], seed=5, paths=PATHS))
    up = forecast_of(run(params, MONDAY, market, {}, [Subject("X", 100.0, own, direction="bullish", **view)], seed=5, paths=PATHS))
    down = forecast_of(run(params, MONDAY, market, {}, [Subject("X", 100.0, own, direction="bearish", **view)], seed=5, paths=PATHS))
    flat = forecast_of(run(params, MONDAY, market, {}, [Subject("X", 100.0, own, direction="neutral", **view)], seed=5, paths=PATHS))
    whole = 0.25 * up.daily_vol * math.sqrt(10)
    # (The kept percentiles are rounded to six places.)
    assert median(up, 10) - median(level, 10) == pytest.approx(whole, abs=2e-6)
    assert median(down, 10) - median(level, 10) == pytest.approx(-whole, abs=2e-6)
    assert median(up, 5) - median(level, 5) == pytest.approx(whole / 2, abs=2e-6)       # half way there
    assert median(up, 20) - median(level, 20) == pytest.approx(whole, abs=2e-6)         # and no further after it
    assert (up.lean, down.lean, flat.lean, level.lean) == (0.25, -0.25, 0.0, 0.0)
    assert horizon(flat, 10).grid == horizon(level, 10).grid
    assert up.lean_basis == "earned" and up.direction == "bullish" and up.view_horizon == 10


def test_a_names_own_drift_and_its_sector_fund_both_come_through():
    market = noise(0.004, seed=1)
    fund = 1.0 * market + noise(0.02, seed=4)
    series = [plain("XLK", beta=1.0, quote_type="ETF"), plain("X", beta=0.0, sector="XLK", sector_loading=1.0, drift=0.002)]
    own = noise(0.002, seed=2)
    tied = Subject("X", 100.0, fund - market + own)
    result = run(hand_params(series, RegimeModel(means=[0.0], vols=[0.004], transition=[[1.0]])), MONDAY, market,
                 {"XLK": fund}, [tied], seed=5, paths=PATHS)
    forecast = forecast_of(result)
    assert forecast.sector == "XLK"
    assert median(forecast, 10) == pytest.approx(0.02, abs=0.004)                       # ten days of 0.2% drift
    # Its width is its fund's own move, not its own small noise.
    assert forecast.daily_vol == pytest.approx(0.02, rel=0.25) and forecast.own_vol == pytest.approx(0.002, rel=0.3)
    width = horizon(forecast, 1).grid[83] - horizon(forecast, 1).grid[15]
    assert width == pytest.approx(2 * forecast.daily_vol, rel=0.1)
    # The fund moves afresh every day: ten days are about three times as wide as one, not ten times.
    ten = horizon(forecast, 10).grid[83] - horizon(forecast, 10).grid[15]
    assert ten == pytest.approx(math.sqrt(10) * width, rel=0.1)


# ── What cannot be forecast is said ──────────────────────────────────────────

def test_a_name_that_cannot_be_forecast_is_named_with_the_reason_and_the_rest_go_on():
    market = noise(0.01, seed=1)
    params = hand_params([plain("OK"), plain("SHORT"), plain("STALE"), plain("TIED", sector="XLK", sector_loading=1.0),
                          plain("FLAT"), plain("ZERO")])
    good = market + noise(0.01, seed=2)
    result = run(params, MONDAY, market, {}, [
        Subject("OK", 100.0, good),
        Subject("NEW", 100.0, good),
        Subject("SHORT", 100.0, good.iloc[-30:]),
        Subject("STALE", 100.0, good.iloc[:-1]),
        Subject("TIED", 100.0, good),
        Subject("FLAT", 100.0, market.copy()),
        Subject("ZERO", 0.0, good),
    ], seed=1, paths=2000)
    assert [item.ticker for item in result.tickers] == ["OK"]
    assert result.problems == [
        "FLAT: its own moves in the snapshot do not vary.",
        "NEW: version 1 of the parameters has nothing on it: it was not in the estimate, or its history could not be used.",
        "SHORT: it has 30 daily returns in the snapshot in common with the market; 60 are needed.",
        "STALE: its last bar in the snapshot is 2026-11-20, not 2026-11-23.",
        "TIED: its sector fund XLK is not in the parameters.",
        "ZERO: it has no usable close.",
    ]


def test_a_sector_fund_missing_from_the_snapshot_stops_the_names_tied_to_it():
    market = noise(0.01, seed=1)
    params = hand_params([plain("XLK", quote_type="ETF"), plain("TIED", sector="XLK", sector_loading=1.0)])
    good = market + noise(0.01, seed=2)
    result = run(params, MONDAY, market, {}, [Subject("TIED", 100.0, good)], seed=1, paths=2000)
    assert result.tickers == [] and result.problems == ["TIED: its sector fund XLK is not in the snapshot."]
    short = run(params, MONDAY, market, {"XLK": good.iloc[-20:]}, [Subject("TIED", 100.0, good)], seed=1, paths=2000)
    assert short.problems == ["TIED: its sector fund XLK has 20 daily returns in the snapshot; 60 are needed."]


def test_a_stop_that_is_not_below_the_close_is_said_and_nothing_is_claimed_about_it():
    params, market, subject = lone(stop=101.0, stop_from="analyst")
    forecast = forecast_of(run(params, MONDAY, market, {}, [subject], seed=1, paths=2000))
    assert forecast.stop is None and forecast.target is None and forecast.break_even is None and forecast.stop_from == ""
    assert forecast.notes == ["The stop 101.00 is not below the close 100.00: nothing is said about it."]
    assert all(item.stop_touch is None and item.target_first is None and item.naive_stop_touch is None
               for item in forecast.horizons)
    bare = forecast_of(run(params, MONDAY, market, {}, [lone()[2]], seed=1, paths=2000))
    assert bare.stop is None and bare.notes == [] and horizon(bare, 5).stop_touch is None


def test_the_stop_the_target_and_what_the_trade_needs_to_pay():
    params, market, subject = lone(stop=96.0, stop_from="position")
    forecast = forecast_of(run(params, MONDAY, market, {}, [subject], seed=1, paths=PATHS, target_r=3.0))
    assert (forecast.stop, forecast.stop_from, forecast.target) == (96.0, "position", 112.0)
    assert forecast.break_even == pytest.approx(0.25)               # one risk against three: one in four has to work
    touches = [item.stop_touch for item in forecast.horizons]
    assert touches == sorted(touches) and 0 < touches[0] < touches[-1] < 1
    assert all(item.target_first + item.stop_first <= 1 + 1e-12 for item in forecast.horizons)
    assert all(item.stop_first <= item.stop_touch + 1e-12 for item in forecast.horizons)
    nearer = forecast_of(run(params, MONDAY, market, {}, [Subject("X", 100.0, subject.returns, stop=98.0)], seed=1, paths=PATHS))
    assert horizon(nearer, 5).stop_touch > horizon(forecast, 5).stop_touch
    shipped = forecast_of(run(params, MONDAY, market, {}, [subject], seed=1, paths=2000))
    assert shipped.target == 108.0                                  # FORECAST_TARGET_R as shipped: twice the risk


# ── The plain forecast it has to beat ────────────────────────────────────────

def test_the_plain_forecast_is_historical_volatility_and_nothing_else():
    params, market, subject = lone(stop=96.0)
    forecast = forecast_of(run(params, MONDAY, market, {}, [subject], seed=1, paths=2000))
    assert forecast.naive_vol == pytest.approx(float(np.std(subject.returns.to_numpy())))
    for item in forecast.horizons:
        assert item.naive_grid == naive_grid(forecast.naive_vol, item.sessions)
        assert item.naive_stop_touch == naive_stop_touch(forecast.naive_vol, item.sessions, math.log(0.96))
    grid = naive_grid(0.01, 4)
    assert grid[49] == 0.0 and grid[94] == pytest.approx(0.02 * NORMAL.inv_cdf(0.95), abs=1e-6)
    assert grid[4] == -grid[94] and len(grid) == 99
    assert naive_stop_touch(0.01, 4, -0.02) == pytest.approx(2 * NORMAL.cdf(-1.0))
    assert naive_stop_touch(0.0, 4, -0.02) == 0.0 and naive_stop_touch(0.5, 4, -1e-9) <= 1.0


# ── The book ─────────────────────────────────────────────────────────────────

def test_a_book_of_cash_is_worth_its_cash_whatever_happens():
    params, market, subject = lone()
    result = run(params, MONDAY, market, {}, [subject], seed=1, paths=2000,
                 book=Book(cash=1234.5, equity=1234.5, high_water=1300.0))
    assert result.book.positions == 0
    for item in result.book.horizons:
        assert set(item.equity.values()) == {1234.5}
        assert (item.stops_together, item.drawdown_breaker, item.daily_loss_breaker) == (0.0, 0.0, 0.0)
    assert run(params, MONDAY, market, {}, [subject], seed=1, paths=2000).book is None


def test_a_holding_without_a_stop_moves_the_book_as_the_price_moves():
    params, market, subject = lone(shares=10)
    result = run(params, MONDAY, market, {}, [subject], seed=1, paths=PATHS,
                 book=Book(cash=500.0, equity=1500.0, high_water=1500.0))
    forecast = forecast_of(result)
    for item, priced in zip(result.book.horizons, forecast.horizons):
        assert item.sessions == priced.sessions
        for band in ("5", "25", "50", "75", "95"):
            assert item.equity[band] == pytest.approx(500 + 10 * priced.quantiles[band], abs=0.01)


def test_a_stop_bounds_what_the_holding_can_cost_and_a_breaker_set_at_that_level_is_seen():
    params, market, subject = lone(0.02, shares=10, stop=95.0)
    floor = 500 + 10 * 95.0
    seen = run(params, MONDAY, market, {}, [subject], seed=1, paths=PATHS,
               book=Book(cash=500.0, equity=1500.0, high_water=floor / 0.9))       # the breaker sits exactly on the stopped-out book
    last = seen.book.horizons[-1]
    assert last.equity["5"] == pytest.approx(floor, abs=0.01)
    assert last.drawdown_breaker == pytest.approx(horizon(forecast_of(seen), 20).stop_touch, abs=1e-12)
    unseen = run(params, MONDAY, market, {}, [subject], seed=1, paths=PATHS,
                 book=Book(cash=500.0, equity=1500.0, high_water=(floor - 1) / 0.9))
    assert unseen.book.horizons[-1].drawdown_breaker == 0.0
    # A day's loss of 3% of the book is 4.5% on the holding: that takes a jump of more than two daily moves.
    losses = [item.daily_loss_breaker for item in seen.book.horizons]
    assert losses == sorted(losses) and 0 < losses[0] < 0.05 < losses[-1]


def test_a_holding_that_could_not_be_simulated_is_counted_at_its_close_and_named():
    params, market, subject = lone(shares=10)
    ghost = Subject("GHOST", 20.0, subject.returns, shares=5)
    result = run(params, MONDAY, market, {}, [subject, ghost], seed=1, paths=PATHS,
                 book=Book(cash=0.0, equity=1100.0, high_water=1100.0))
    assert result.book.positions == 2
    assert result.book.notes == ["GHOST is held but could not be simulated: its value is taken as fixed at its last close."]
    priced = horizon(forecast_of(result), 5)
    assert result.book.horizons[1].equity["50"] == pytest.approx(100 + 10 * priced.quantiles["50"], abs=0.01)


def test_a_holding_the_snapshot_has_no_price_for_is_counted_at_what_the_account_carries_it_at():
    """Found in the first run on live data: two holdings had no usable price that evening, were left out of the
    book, and the forecast said the drawdown breaker was certain to trip."""
    params, market, subject = lone(shares=10)
    # The account: 200 in cash, 10 X at 100, and a holding the books carry at 500 that has no price tonight.
    book = Book(cash=200.0, equity=1700.0, high_water=1700.0, unpriced={"GONE": 500.0})
    result = run(params, MONDAY, market, {}, [subject], seed=1, paths=PATHS, book=book)
    assert result.book.positions == 2
    assert result.book.notes == ["GONE is held but the snapshot has no usable price for it: its value is taken as "
                                 "fixed at the 500.00 the account carries it at."]
    priced = horizon(forecast_of(result), 5)
    assert result.book.horizons[1].equity["50"] == pytest.approx(200 + 500 + 10 * priced.quantiles["50"], abs=0.01)
    assert result.book.horizons[0].drawdown_breaker < 0.01
    # Left out, as it was: the book starts 500 short, below the level of the breaker before a day has passed.
    short = run(params, MONDAY, market, {}, [subject], seed=1, paths=PATHS,
                book=Book(cash=200.0, equity=1700.0, high_water=1700.0))
    assert short.book.horizons[0].drawdown_breaker == 1.0 and short.book.positions == 1
    # Beside a holding that could not be simulated, which is taken at its last close, and a second one without a price.
    ghost = Subject("GHOST", 20.0, subject.returns, shares=5)
    both = run(params, MONDAY, market, {}, [subject, ghost], seed=1, paths=PATHS,
               book=Book(cash=200.0, equity=1830.0, high_water=1830.0, unpriced={"GONE": 500.0, "AWAY": 30.0}))
    assert both.book.positions == 4 and len(both.book.notes) == 3
    assert both.book.notes[1].startswith("AWAY is held") and "at the 30.00 the account" in both.book.notes[1]
    assert both.book.horizons[1].equity["50"] == pytest.approx(200 + 100 + 530 + 10 * priced.quantiles["50"], abs=0.01)
    # What is carried is what the account says of that holding. Its total is not asked: one that does not add up
    # (a price that moved between the account's reading and the snapshot's) moves nothing.
    odd = run(params, MONDAY, market, {}, [subject], seed=1, paths=PATHS,
              book=Book(cash=200.0, equity=900.0, high_water=900.0, unpriced={"GONE": 500.0}))
    assert odd.book.horizons[1].equity["50"] == pytest.approx(200 + 500 + 10 * priced.quantiles["50"], abs=0.01)


def test_the_number_of_paths_and_the_target_come_from_the_configuration(monkeypatch):
    params, market, subject = lone(stop=96.0)
    monkeypatch.setattr(engine.config, "FORECAST_PATHS", 300)
    monkeypatch.setattr(engine.config, "FORECAST_TARGET_R", 1.5)
    forecast = forecast_of(run(params, MONDAY, market, {}, [subject], seed=1))
    assert forecast.target == 106.0
    assert all(round(item.stop_touch * 300, 9) == round(item.stop_touch * 300) for item in forecast.horizons)
    assert [item.sessions for item in forecast.horizons] == list(HORIZONS)
    assert pd.isna(forecast.confidence) or forecast.confidence is None
    assert normal_pool()[1000] == 0.0


@pytest.mark.parametrize("stamp, lag, reacts", [
    ((16, 5), 0, True),        # after the close: tomorrow reacts, whatever this name usually does
    ((16, 0), 0, True),        # at the bell
    ((7, 0), 1, False),        # before the open: today's bar has it already
    ((12, 30), 1, False),
    ((0, 0), 1, True),         # no hour given: as this name usually reports
    ((0, 0), 0, False),
])
def test_a_report_made_on_the_evening_of_the_meetings_own_day_moves_the_first_simulated_session(stamp, lag, reacts):
    """The snapshot only names reports dated after the session. This one is dated on it, and tomorrow's open has it."""
    market = noise(0.01, seed=1)
    own = noise(0.01, seed=2)
    series = reporting(lag, reports=[at(MONDAY, *stamp)])
    forecast = forecast_of(run(hand_params([series]), MONDAY, market, {}, [Subject("X", 100.0, own)], seed=5, paths=PATHS))
    quiet = forecast_of(run(hand_params([reporting(lag)]), MONDAY, market, {}, [Subject("X", 100.0, own)], seed=5, paths=PATHS))
    spread = lambda item: item.grid[94] - item.grid[4]              # noqa: E731
    if reacts:
        assert forecast.report_on == date(2026, 11, 24) and forecast.report_modelled
        assert spread(horizon(forecast, 1)) > 4 * spread(horizon(quiet, 1))
    else:
        # Tomorrow is an ordinary day. (Today's own move is left out of the volatility where it was the
        # report's, so the width is near the usual one and not exactly it.)
        assert forecast.report_on is None
        assert spread(horizon(forecast, 1)) == pytest.approx(spread(horizon(quiet, 1)), rel=0.2)
    # With a later report named by the snapshot as well, the nearer one is the next.
    both = forecast_of(run(hand_params([series]), MONDAY, market, {},
                           [Subject("X", 100.0, own, next_report=date(2026, 12, 2))], seed=5, paths=2000))
    assert both.report_on == (date(2026, 11, 24) if reacts else (date(2026, 12, 3) if lag else date(2026, 12, 2)))


def test_a_report_earlier_this_week_does_not_raise_the_volatility_either():
    """The report days kept with the parameters stop at last week. This week's are found from the report times."""
    market = noise(0.01, seed=1)
    own = noise(0.01, seed=2)
    day = own.index[-3]                                             # Wednesday 18 November, in the made-up week
    own.loc[day] = 0.03
    calm = own.copy()
    calm.loc[day] = 0.0
    def vol(series, returns):
        return forecast_of(run(hand_params([series]), MONDAY, market, {}, [Subject("X", 100.0, returns)],
                               seed=5, paths=2000)).own_vol
    untold = vol(plain("X", beta=0.0), own)
    before_open = vol(plain("X", beta=0.0, reports=[at(day, 7, 0)]), own)
    assert before_open == pytest.approx(vol(plain("X", beta=0.0, reports=[at(day, 7, 0)]), calm), rel=1e-12)
    assert untold > 1.15 * before_open
    # Reported after the close the day before: the same session reacted, and is left out the same way.
    evening = vol(plain("X", beta=0.0, reports=[at(own.index[-4], 16, 30)]), own)
    assert evening == pytest.approx(before_open, rel=1e-12)


def test_a_sector_fund_that_jumps_takes_its_names_with_it_at_the_open():
    market = noise(1e-9, seed=1)
    fund = market + noise(0.004, seed=4)
    tied = Subject("X", 100.0, (fund - market) + noise(0.002, seed=2), stop=95.0, shares=10)
    quiet = [plain("XLK", quote_type="ETF"), plain("X", beta=0.0, sector="XLK", sector_loading=1.0)]
    jumpy = [plain("XLK", quote_type="ETF", jump_rate=0.05, jumps=[-0.2231435513]),
             plain("X", beta=0.0, sector="XLK", sector_loading=1.0)]
    model = RegimeModel(means=[0.0], vols=[1e-9], transition=[[1.0]])
    book = Book(cash=0.0, equity=1000.0, high_water=1000.0)
    calm = run(hand_params(quiet, model), MONDAY, market, {"XLK": fund}, [tied], seed=3, paths=PATHS, book=book)
    hit = run(hand_params(jumpy, model), MONDAY, market, {"XLK": fund}, [tied], seed=3, paths=PATHS, book=book)
    # One day in twenty the fund falls a fifth, and the name with it: over twenty days most paths have met one.
    assert horizon(forecast_of(hit), 20).grid[24] < -0.15 < horizon(forecast_of(calm), 20).grid[0]
    assert horizon(forecast_of(hit), 20).stop_touch > 0.6 > horizon(forecast_of(calm), 20).stop_touch + 0.3
    # It is taken at the open: a stop it passes is sold at the open's price, 80, not at the stop's 95.
    assert hit.book.horizons[-1].equity["5"] < 10 * 85 < 10 * 94.9 < calm.book.horizons[-1].equity["5"]
    # A jump stands in for the fund's ordinary move that day, it is not added to it: a fund that "jumps"
    # by nothing every day does not move at all, and the name is left with its own small noise.
    still = [plain("XLK", quote_type="ETF", jump_rate=1.0, jumps=[0.0]), plain("X", beta=0.0, sector="XLK", sector_loading=1.0)]
    flat = run(hand_params(still, model), MONDAY, market, {"XLK": fund}, [tied], seed=3, paths=PATHS)
    width = lambda result: horizon(forecast_of(result), 5).grid[83] - horizon(forecast_of(result), 5).grid[15]   # noqa: E731
    assert width(flat) < 0.6 * width(calm)


def test_on_a_half_day_the_close_that_counts_is_one_oclock():
    """Friday 27 November 2026 closes at 13:00. A report at 13:05 that day is after the close: Monday reacts."""
    friday = date(2026, 11, 27)
    market = noise(0.01, seed=1, end=friday)
    own = noise(0.01, seed=2, end=friday)
    def reacts(hour, minute):
        series = reporting(0, reports=[at(friday, hour, minute)])
        return forecast_of(run(hand_params([series], cutoff=friday), friday, market, {}, [Subject("X", 100.0, own)],
                               seed=5, paths=2000)).report_on
    assert reacts(13, 5) == date(2026, 11, 30) and reacts(13, 0) == date(2026, 11, 30)
    assert reacts(12, 55) is None                                   # before that day's close: Friday's own bar has it
