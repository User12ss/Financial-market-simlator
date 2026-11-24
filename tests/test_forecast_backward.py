"""Working back from how a trade ends: counted by hand, set against formulas, and held to Bayes' rule."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

import config
from forecast import backward
from forecast.backward import BAD, GOOD, MIDDLE, endings, happenings, read, telling
from forecast.engine import DAYS, NEVER, Proposed, Subject, Trace, run
from forecast.regime import RegimeModel
from models.forecast import OUTCOMES, EventOdds, TickerForecast
from tests.forecast_fakes import MONDAY, at, hand_params, noise, plain

PATHS = 20_000
TWO_STATES = RegimeModel(means=[0.0, 0.0], vols=[0.006, 0.02], transition=[[0.98, 0.02], [0.05, 0.95]])
QUIET = RegimeModel(means=[0.0], vols=[1e-6], transition=[[1.0]])
MATURES = date(2026, 12, 8)


def ticket(stop=95.0, target=105.0, **changes) -> TickerForecast:
    fields = dict(ticker="X", close=100.0, beta=1.0, own_vol=0.01, daily_vol=0.01, naive_vol=0.01, stop=stop, target=target)
    return TickerForecast(**{**fields, **changes})


def by_hand(stopped, reached, *, path=None, lowest=None, jumps=None, sold=None, **more) -> Trace:
    """A trace of a few paths written out by hand. Days not mentioned are flat."""
    count = len(stopped)
    flat = np.zeros((count, DAYS))
    return Trace(
        ticker="X", path=flat.copy() if path is None else np.array(path, dtype=float),
        lowest=flat.copy() if lowest is None else np.array(lowest, dtype=float),
        stopped=np.array(stopped), reached=np.array(reached),
        sold=np.full(count, math.log(0.95)) if sold is None else np.array(sold, dtype=float),
        jumps=flat.copy() if jumps is None else np.array(jumps, dtype=float),
        report_index=more.pop("report_index", None), report_move=more.pop("report_move", None),
        fund_jumps=more.pop("fund_jumps", None), **more,
    )


def days(count: int, marks: dict[tuple[int, int], float]) -> np.ndarray:
    """A paths-by-days array that is zero except at the (path, day) places given."""
    array = np.zeros((count, DAYS))
    for (row, day), value in marks.items():
        array[row, day] = value
    return array


def reading(trace, *, sessions=10, states=None, market=None, forecast=None, names=("calm",), now=(1.0,), labels=None):
    count = len(trace.stopped)
    return read(
        trace, np.zeros((count, DAYS), dtype=np.int64) if states is None else states,
        np.zeros((count, DAYS)) if market is None else market, forecast or ticket(), sessions=sessions,
        matures_on=MATURES, state_names=list(names), state_now=list(now), labels=labels,
    )


def event(result, key) -> EventOdds:
    return next(item for item in result.events if item.key == key)


@pytest.fixture(autouse=True)
def every_path_counts(monkeypatch):
    """The hand-made traces have a handful of paths. The tests of the floor set it themselves."""
    monkeypatch.setattr(config, "BACKWARD_MIN_PATHS", 1)


# ── The three endings ────────────────────────────────────────────────────────

def test_every_path_ends_exactly_one_way_and_the_day_that_settles_it_is_the_first_of_the_two():
    #                 target first   stop first   same day   neither   both after the horizon   stop in time, target later
    trace = by_hand(stopped=[7, 2, 4, NEVER, 12, 3], reached=[1, 6, 4, NEVER, 11, 15])
    which, day = endings(trace, sessions=10)
    assert list(which) == [GOOD, BAD, BAD, MIDDLE, MIDDLE, BAD]
    assert list(day) == [1, 2, 4, 9, 9, 3]
    result = reading(trace)
    assert {name: way.paths for name, way in result.ways.items()} == {"good": 1, "bad": 3, "middle": 2}
    assert sum(way.share for way in result.ways.values()) == pytest.approx(1.0)
    assert result.ways["bad"].share == pytest.approx(0.5)
    assert list(result.ways) == list(OUTCOMES)


def test_the_horizon_is_what_cuts_an_ending_off_and_the_last_day_inside_it_still_counts():
    trace = by_hand(stopped=[9, 10, NEVER], reached=[NEVER, NEVER, 9])
    assert list(endings(trace, sessions=10)[0]) == [BAD, MIDDLE, GOOD]
    assert list(endings(trace, sessions=11)[0]) == [BAD, BAD, GOOD]
    assert list(endings(trace, sessions=9)[0]) == [MIDDLE, MIDDLE, MIDDLE]
    # A horizon beyond the simulated days is the simulated days: a path that ends at neither is settled on the last of them.
    assert list(endings(trace, sessions=99)[1]) == [9, 10, 9]
    assert list(endings(by_hand(stopped=[NEVER], reached=[NEVER]), sessions=99)[1]) == [DAYS - 1]
    assert reading(trace, sessions=99).sessions == DAYS


def test_a_name_without_a_stop_has_no_bad_ending_to_work_back_from():
    trace = by_hand(stopped=[NEVER], reached=[NEVER])
    trace.sold = None
    with pytest.raises(ValueError, match="no stop"):
        reading(trace, forecast=ticket(stop=None, target=None))


# ── What the paths of each ending went through ───────────────────────────────

def test_the_day_that_settled_it_the_dip_on_the_way_and_where_the_stop_sold():
    stopped = [NEVER, NEVER, NEVER, 0, 1, 2, NEVER, NEVER]
    reached = [1, 3, 5, NEVER, NEVER, NEVER, NEVER, NEVER]
    lowest = days(8, {})
    for row, depth in enumerate([-0.01, -0.02, -0.04]):       # the three that reach the target dip first
        lowest[row, :] = depth
    lowest[6, :5], lowest[6, 5:] = -0.01, -0.03                # a path that ends at neither, deepest late
    lowest[7, :] = 0.02                                        # one that never went below the close
    sold = [0.0] * 3 + [math.log(0.95), math.log(0.95), math.log(0.90)] + [0.0] * 2
    result = reading(by_hand(stopped, reached, lowest=lowest, sold=sold))
    good, bad, middle = (result.ways[name] for name in OUTCOMES)
    assert good.settled == {"25": 3.0, "50": 4.0, "75": 5.0}           # days 1, 3, 5, counted from one
    assert bad.settled == {"25": 1.5, "50": 2.0, "75": 2.5}
    assert middle.settled == {}
    falls = [(1 - math.exp(depth)) * 100 for depth in (-0.01, -0.02, -0.04)]
    assert good.adverse == {mark: pytest.approx(np.percentile(falls, int(mark)), abs=0.006) for mark in ("50", "75", "95")}
    assert good.adverse["50"] == pytest.approx(falls[1], abs=0.006)
    # The dip that counts is the lowest point by the day that settles the path: for these two, the horizon's last.
    # A path that never went below the close fell by nothing, not by less than nothing.
    late = (1 - math.exp(-0.03)) * 100
    assert middle.adverse == {mark: pytest.approx(np.percentile([late, 0.0], int(mark)), abs=0.006) for mark in ("50", "75", "95")}
    assert bad.adverse == {} and good.sold_below == {} and middle.sold_below == {}
    assert bad.sold_below["50"] == pytest.approx(5.0, abs=0.01)
    assert bad.sold_below["95"] == pytest.approx(9.5, abs=0.06)       # one of three opened through the stop


def test_the_dip_of_a_path_that_reaches_the_target_is_measured_up_to_that_day_and_not_after():
    lowest = days(2, {})
    lowest[0, :3], lowest[0, 3:] = -0.01, -0.20                # falls hard after the target was reached on day 2
    lowest[1, :] = -0.01
    result = reading(by_hand(stopped=[NEVER, NEVER], reached=[2, 2], lowest=lowest))
    assert result.ways["good"].adverse["95"] == pytest.approx((1 - math.exp(-0.01)) * 100, abs=0.01)


def test_how_much_of_the_way_came_on_its_single_largest_day():
    path = days(4, {})
    path[0] = np.cumsum([0.01] * DAYS)                          # ten even steps to day 9: a tenth each
    path[1] = np.cumsum([0.09] + [0.001] * (DAYS - 1))          # nearly all of it on the first day
    path[2] = 0.0                                               # a path that never moves has no share to speak of
    path[3] = np.cumsum([0.02] * DAYS)                          # even again: a tenth
    result = reading(by_hand(stopped=[NEVER] * 4, reached=[NEVER] * 4, path=path))
    # The middle one of a tenth, a tenth and nine tenths: a tenth. Their average would be far from it.
    assert result.ways["middle"].biggest_day == pytest.approx(0.1, abs=1e-4)


def test_stopped_out_and_yet_right_about_the_direction():
    path = days(4, {})
    path[0, :], path[1, :], path[2, :], path[3, :] = 0.03, -0.08, 0.03, 0.06
    #                bad, ends above   bad, ends below   never stopped   bad, and reaches the target afterwards
    trace = by_hand(stopped=[2, 2, NEVER, 1], reached=[NEVER, NEVER, NEVER, 6], path=path)
    result = reading(trace)
    assert result.shaken_out == pytest.approx(2 / 4)
    assert result.target_after_stop == pytest.approx(1 / 4)
    # The target reached after the horizon is not "afterwards within the horizon".
    assert reading(trace, sessions=6).target_after_stop == 0.0


# ── Events on the way ────────────────────────────────────────────────────────

def test_an_event_counts_when_it_happens_on_or_before_the_day_that_settles_the_path():
    stopped, reached = [3, 3, 3, NEVER], [NEVER, NEVER, NEVER, NEVER]
    jumps = days(4, {(0, 3): -0.05, (1, 4): -0.05, (2, 1): 0.04, (3, 9): -0.05})
    result = reading(by_hand(stopped, reached, jumps=jumps))
    down, up = event(result, "jump_down"), event(result, "jump_up")
    # Path 0 jumps on the day it is stopped: that counts. Path 1 jumps the day after: it does not.
    assert down.given == {"good": None, "bad": pytest.approx(1 / 3), "middle": pytest.approx(1.0)}
    assert down.chance == pytest.approx(2 / 4)
    assert up.given["bad"] == pytest.approx(1 / 3) and up.chance == pytest.approx(1 / 4)
    assert down.kind == up.kind == "statistical"


def test_the_ratio_is_the_bad_endings_chance_over_the_good_ones_and_the_most_telling_come_first():
    #         good ×4                         bad ×4
    stopped = [NEVER] * 4 + [5] * 4
    reached = [5] * 4 + [NEVER] * 4
    jumps = days(8, {(0, 1): -0.05, (4, 1): -0.05, (5, 1): -0.05, (6, 1): -0.05,      # down: 1 of 4 good, 3 of 4 bad
                     (0, 2): 0.05, (1, 2): 0.05, (4, 2): 0.05, (5, 2): 0.05})        # up:   2 of 4 good, 2 of 4 bad
    fund = days(8, {(4, 0): -0.03, (5, 0): -0.03, (6, 0): -0.03, (7, 0): -0.03,       # sector down: on the bad ones only
                    (0, 0): 0.03, (1, 0): 0.03})                                      # sector up:   on good ones only
    result = reading(by_hand(stopped, reached, jumps=jumps, fund_jumps=fund))
    assert event(result, "jump_down").ratio == pytest.approx(3.0)
    assert event(result, "jump_up").ratio == pytest.approx(1.0)
    only_bad, only_good = event(result, "sector_down"), event(result, "sector_up")
    assert only_bad.ratio is None and "none that reach the target" in only_bad.note
    assert only_good.ratio == 0.0 and only_good.note == ""
    # The weight is the log of the ratio with half a path added to each count: on 4 of 4 against 0 of 4,
    # (4.5 / 5) over (0.5 / 5); on 0 of 4 against 2 of 4, (0.5 / 5) over (2.5 / 5). An event seen under one
    # ending only still has a weight, and the more paths it was seen on the larger it is.
    assert only_bad.weight == pytest.approx(math.log(9)) and only_good.weight == pytest.approx(-math.log(5))
    assert telling(only_bad) == pytest.approx(math.log(9)) and telling(only_good) == pytest.approx(math.log(5))
    assert event(result, "jump_down").weight == pytest.approx(math.log(3.5 / 1.5))       # 3 of 4 against 1 of 4
    assert telling(event(result, "jump_up")) == pytest.approx(0.0)
    keys = [item.key for item in result.events]
    assert keys.index("jump_down") < keys.index("jump_up")
    assert set(keys[:2]) == {"sector_down", "sector_up"}


def test_a_weight_below_zero_tells_as_much_as_the_same_above_and_an_event_without_one_tells_least():
    def row(weight):
        return EventOdds(key="a", label="", kind="statistical", chance=0.1, given={}, weight=weight)

    assert telling(row(-1.4)) == telling(row(1.4)) == 1.4
    assert telling(row(None)) < telling(row(0.0)) == 0.0


def test_an_event_on_a_handful_of_paths_weighs_less_than_one_as_lopsided_on_many():
    """Seen on 2 of 400 paths that end at the stop and on none of 400 that reach the target, against 200 of 400 and none."""
    stopped = [3] * 400 + [NEVER] * 400
    reached = [NEVER] * 400 + [3] * 400
    rare = days(800, {(0, 1): -0.05, (1, 1): -0.05})
    common = days(800, {(row, 1): -0.03 for row in range(200)})
    result = reading(by_hand(stopped, reached, jumps=rare, fund_jumps=common))
    few, many = event(result, "jump_down"), event(result, "sector_down")
    assert few.ratio is None and many.ratio is None and few.note and many.note      # neither has a ratio to rank by
    assert few.weight == pytest.approx(math.log((2.5 / 401) / (0.5 / 401)))
    assert many.weight == pytest.approx(math.log((200.5 / 401) / (0.5 / 401)))
    assert [item.key for item in result.events] == ["sector_down", "jump_down"]


def test_an_ending_on_too_few_paths_is_not_read_and_that_is_said(monkeypatch):
    monkeypatch.setattr(config, "BACKWARD_MIN_PATHS", 3)
    stopped = [1, 1, 1, NEVER, NEVER, NEVER, NEVER]
    reached = [NEVER, NEVER, NEVER, 2, 2, NEVER, NEVER]
    result = reading(by_hand(stopped, reached, jumps=days(7, {(0, 0): -0.05, (3, 0): -0.05})))
    good, bad, middle = (result.ways[name] for name in OUTCOMES)
    assert good.paths == 2 and good.settled == {} and good.adverse == {} and good.biggest_day is None
    assert bad.settled != {} and middle.paths == 2
    down = event(result, "jump_down")
    assert down.given == {"good": None, "bad": pytest.approx(1 / 3), "middle": None}
    assert down.ratio is None and down.weight is None and down.note == ""
    assert "Only 2 of 7 paths end at the target: too few to say what led to it." in result.notes
    assert "Only 2 of 7 paths end at neither: too few to say what led to it." in result.notes
    # Exactly at the floor is enough.
    assert not any("at the stop" in note for note in result.notes)


def test_a_measured_event_that_never_happens_is_left_out_and_an_ending_nobody_reaches_is_not_noted():
    result = reading(by_hand(stopped=[1, 1], reached=[NEVER, NEVER]))
    assert [item.key for item in result.events] == []
    assert result.notes == [] and result.ways["good"].paths == 0


def test_the_market_events_are_its_other_states_and_a_fall_or_rise_of_the_set_size(monkeypatch):
    monkeypatch.setattr(config, "BACKWARD_MARKET_MOVE_PCT", 4.0)
    states = np.zeros((4, DAYS), dtype=np.int64)
    states[0, 2] = 1                    # in time
    states[1, 8] = 1                    # after the day that settled path 1
    market = days(4, {(2, 3): math.log(0.96), (3, 3): math.log(0.9601), (0, 1): math.log(1.04)})
    trace = by_hand(stopped=[5, 5, 5, 5], reached=[NEVER] * 4)
    result = reading(trace, states=states, market=market, names=("calm", "turbulent"), now=(0.9, 0.1))
    assert event(result, "market_turbulent").chance == pytest.approx(1 / 4)
    assert "turbulent state" in event(result, "market_turbulent").label
    assert event(result, "market_down").chance == pytest.approx(1 / 4)        # 4.00% counts, 3.99% does not
    assert event(result, "market_up").chance == pytest.approx(1 / 4)
    assert "4%" in event(result, "market_down").label
    # From a turbulent market the state to watch for is the calm one. With one state there is none.
    states = np.ones((4, DAYS), dtype=np.int64)
    states[0, 2] = 0
    turned = reading(trace, states=states, names=("calm", "turbulent"), now=(0.2, 0.8))
    assert event(turned, "market_calm").chance == pytest.approx(1 / 4)
    assert not [item for item in turned.events if item.key == "market_turbulent"]
    assert not [item for item in reading(trace).events if item.key.startswith("market_c")]


def test_a_report_counts_when_its_day_is_in_time_and_its_move_is_a_whole_expected_move_or_more(monkeypatch):
    stopped, reached = [5, 5, 5, 1, 5, 3, 1], [NEVER] * 7
    moves = np.array([-1.0, -0.99, 1.0, -3.0, 2.5, -2.0, 4.0])
    trace = by_hand(stopped, reached, report_index=3, report_move=moves)
    result = reading(trace)
    # Path 3 was stopped before its report, and so was path 6, whose report would have been taken well.
    # Path 5 was stopped on the day of the report itself: that one is on the way.
    assert event(result, "report_down").chance == pytest.approx(2 / 7)
    assert event(result, "report_up").chance == pytest.approx(2 / 7)
    assert event(result, "report_down").kind == "calendar"
    assert "one of that day's expected moves lower" in event(result, "report_down").label
    monkeypatch.setattr(config, "BACKWARD_REPORT_MOVES", 2.0)
    assert event(reading(trace), "report_down").chance == pytest.approx(1 / 7)        # path 5 alone, at exactly two
    assert event(reading(trace), "report_up").chance == pytest.approx(1 / 7)
    assert "2 of that day's expected moves higher" in event(reading(trace), "report_up").label
    # No report in the simulation, no report events.
    assert not [item for item in reading(by_hand(stopped, reached)).events if item.key.startswith("report")]


def test_a_proposed_event_is_named_as_the_seat_named_it_and_kept_even_when_it_never_happened():
    fell = {"1": (np.array([2, NEVER, 7, NEVER]), np.zeros(4)), "2": (np.array([NEVER] * 4), np.zeros(4))}
    trace = by_hand(stopped=[5, 5, 5, NEVER], reached=[NEVER] * 4, events=fell,
                    left_out={"3": "its dates lie outside the simulated days."}, capped={"1": math.log(1.08)})
    result = reading(trace, labels={"1": "the recall widens", "2": "the CFO leaves", "3": "a strike"})
    first, second = event(result, "proposed:1"), event(result, "proposed:2")
    assert first.label == "the recall widens" and first.kind == "proposed"
    assert first.chance == pytest.approx(1 / 4) and first.given["bad"] == pytest.approx(1 / 3)   # day 7 is after the stop
    assert second.chance == 0.0 and second.label == "the CFO leaves"
    assert result.proposed is True
    assert "The proposed event 'a strike' is not simulated: its dates lie outside the simulated days." in result.notes
    assert any("'the recall widens' is held to the largest past jump, 8.0%" in note for note in result.notes)
    assert reading(by_hand(stopped=[5], reached=[NEVER])).proposed is False
    # A reading for which events were proposed and none could be simulated is still not the measured one.
    none_kept = reading(by_hand(stopped=[5], reached=[NEVER], left_out={"1": "its dates lie outside the simulated days."}))
    assert none_kept.proposed is True and len(none_kept.notes) == 1
    # Without a name given for it, the key stands in.
    assert event(reading(trace), "proposed:1").label == "1"


def test_what_is_carried_over_from_the_forecast():
    result = reading(by_hand(stopped=[1], reached=[NEVER]),
                     forecast=ticket(stop=93.42, target=101.5, target_from="named"), sessions=7)
    assert (result.ticker, result.close, result.stop, result.target, result.target_from) == ("X", 100.0, 93.42, 101.5, "named")
    assert (result.sessions, result.matures_on) == (7, MATURES)


# ── Against Bayes' rule and against what can be worked out ───────────────────

def simulated(subject_changes=None, series_changes=None, model=TWO_STATES, paths=PATHS, seed=7, state_vol=None, fund=False):
    market = noise(0.01, seed=1)
    series = [plain("X", **{"beta": 0.9, "jump_rate": 0.03, "jumps": [-0.07, -0.05, 0.06, 0.04], **(series_changes or {})})]
    funds = {}
    if fund:
        series.append(plain("XLK", jump_rate=0.02, jumps=[-0.04, 0.03]))
        funds["XLK"] = market + noise(0.006, seed=5)
    params = hand_params(series, model, state_vol=state_vol or [1.0] * model.states)
    own = 0.9 * market + noise(0.012, seed=2)
    subject = Subject("X", 100.0, own, **{"stop": 95.0, **(subject_changes or {})})
    result = run(params, MONDAY, market, funds, [subject], seed=seed, paths=paths, trace=True)
    return result, result.traces["X"], result.tickers[0]


def test_the_chance_of_an_event_given_an_ending_is_what_bayes_rule_makes_of_the_ending_given_the_event(monkeypatch):
    """Completion standard: the table's conditional chances agree with the same worked out through Bayes' rule."""
    monkeypatch.setattr(config, "BACKWARD_MIN_PATHS", 100)
    result, trace, forecast = simulated(
        {"next_report": date(2026, 11, 30)},
        {"sector": "XLK", "sector_loading": 0.5, "reports": [at(date(2026, 8, 31), 16, 5)], "report_lag": 1,
         "earnings": [-3.0, -1.5, 0.5, 2.0, 4.0]},
        fund=True, state_vol=[0.8, 1.6],
    )
    table = read(trace, result.states, result.market, forecast, sessions=10, matures_on=MATURES,
                 state_names=["calm", "turbulent"], state_now=result.state_now)
    which, day = endings(trace, 10)
    found = {key: happened for key, _, _, happened in happenings(
        trace, result.states, result.market, day, state_names=["calm", "turbulent"], state_now=result.state_now)}
    assert {"report_down", "report_up", "jump_down", "jump_up", "sector_down", "sector_up", "market_turbulent",
            "market_down"} <= {item.key for item in table.events}
    for item in table.events:
        happened = found[item.key]
        chance = happened.mean()
        assert item.chance == pytest.approx(chance, abs=1e-12)
        for index, name in enumerate(OUTCOMES):
            ending = (which == index).mean()
            ending_given_event = (which[happened] == index).mean()
            assert item.given[name] == pytest.approx(ending_given_event * chance / ending, abs=1e-12), (item.key, name)
        # And the law of total probability: the endings' shares weigh the conditional chances back to the whole.
        assert sum(item.given[name] * table.ways[name].share for name in OUTCOMES) == pytest.approx(chance, abs=1e-12)
        if item.ratio is not None:
            assert item.ratio == pytest.approx(item.given["bad"] / item.given["good"], abs=1e-12)
            # With thousands of paths under each ending the half path added to each count hardly shows.
            if min(happened[which == 0].sum(), happened[which == 1].sum()) >= 50:
                assert item.weight == pytest.approx(math.log(item.ratio), abs=0.02)
    assert sum(way.share for way in table.ways.values()) == pytest.approx(1.0, abs=1e-12)
    ranked = [telling(item) for item in table.events]
    assert ranked == sorted(ranked, reverse=True)


def test_where_only_a_jump_can_end_the_trade_the_first_jump_decides_and_the_numbers_are_the_formulas():
    """A name that hardly moves except to jump 10% up or down, each as often: stop and target both 5% away."""
    rate, sessions = 0.04, 10
    result, trace, forecast = proposed([], paths=60_000, params_market=still(jump_rate=rate, jumps=[-0.10, 0.10]))
    table = read(trace, result.states, result.market, forecast, sessions=sessions, matures_on=MATURES,
                 state_names=["calm"], state_now=[1.0])
    none = (1 - rate) ** sessions
    error = 4 * math.sqrt(0.25 / 60_000)
    assert table.ways["middle"].share == pytest.approx(none, abs=error)
    assert table.ways["good"].share == pytest.approx((1 - none) / 2, abs=error)
    assert table.ways["bad"].share == pytest.approx((1 - none) / 2, abs=error)
    down, up = event(table, "jump_down"), event(table, "jump_up")
    # The first jump settles the path, so a path that ends at the stop never saw a jump up in time, nor the other way round.
    assert down.given == {"good": 0.0, "bad": 1.0, "middle": 0.0}
    assert up.given == {"good": 1.0, "bad": 0.0, "middle": 0.0}
    assert down.ratio is None and down.note and up.ratio == 0.0
    assert down.chance == pytest.approx((1 - none) / 2, abs=error)
    # The day that settles it is the day of the first jump: geometric, cut off at the horizon.
    first_by = [1 - (1 - rate) ** day for day in range(1, sessions + 1)]
    median = next(day for day, share in enumerate(first_by, start=1) if share >= (1 - none) / 2)
    assert table.ways["bad"].settled["50"] == pytest.approx(median, abs=1)
    # A 10% jump opens through a 5% stop: it sells where it opened.
    assert table.ways["bad"].sold_below["50"] == pytest.approx((1 - math.exp(-0.10)) * 100, abs=0.01)
    assert forecast.target == 105.0 and forecast.target_from == "named"
    # Stopped by a jump down, the target is still reached by the paths that then jump up twice: three jumps
    # in ten days in that order, give or take the rarer paths with more.
    three = math.comb(sessions, 3) * rate ** 3 * (1 - rate) ** (sessions - 3) / 8
    assert table.target_after_stop == pytest.approx(three, abs=0.0006)
    assert table.shaken_out >= table.target_after_stop


def test_a_named_target_replaces_the_notional_one_and_one_not_above_the_close_is_refused_in_words():
    _, _, named = simulated({"stop": 95.0, "target": 103.0}, paths=2000)
    _, _, notional = simulated({"stop": 95.0}, paths=2000)
    _, _, refused = simulated({"stop": 95.0, "target": 100.0}, paths=2000)
    assert (named.target, named.target_from) == (103.0, "named")
    assert (notional.target, notional.target_from) == (110.0, "")
    assert (refused.target, refused.target_from) == (110.0, "")
    assert any("The target 100.00 is not above the close 100.00" in note for note in refused.notes)
    assert named.break_even == pytest.approx(5 / 8)
    # A nearer target is reached first more often.
    assert named.horizons[2].target_first > notional.horizons[2].target_first


def test_the_paths_are_kept_only_when_asked_for_and_asking_changes_no_number():
    market = noise(0.01, seed=1)
    params = hand_params([plain("X")], TWO_STATES)
    subject = Subject("X", 100.0, market + noise(0.01, seed=2), stop=95.0)
    plain_run = run(params, MONDAY, market, {}, [subject], seed=3, paths=2000)
    traced = run(params, MONDAY, market, {}, [subject], seed=3, paths=2000, trace=True)
    assert plain_run.traces == {} and plain_run.states is None and plain_run.market is None
    assert traced.tickers[0].model_dump_json() == plain_run.tickers[0].model_dump_json()
    kept = traced.traces["X"]
    assert kept.path.shape == kept.lowest.shape == kept.jumps.shape == traced.states.shape == traced.market.shape == (2000, DAYS)
    assert (kept.lowest <= np.minimum(kept.path, 0.0) + 1e-12).all()
    assert (np.diff(kept.lowest, axis=1) <= 0).all()
    assert kept.fund_jumps is None and kept.report_index is None and kept.events == {}


# ── Events a seat proposed ───────────────────────────────────────────────────

def still(**series_changes) -> tuple:
    """A name that does not move at all on its own account: whatever moves it in a test is what the test put in."""
    market = noise(0.01, seed=1)
    series = plain("X", **{"beta": 0.0, "vol": 1e-6, "jumps": [-0.03, 0.05, 0.08, -0.06], **series_changes})
    return hand_params([series], QUIET), market, noise(1e-6, seed=2)


def proposed(events, *, paths=PATHS, seed=5, params_market=None, **subject):
    params, market, own = params_market or still()
    result = run(params, MONDAY, market, {}, [Subject("X", 100.0, own, stop=95.0, target=105.0, events=events, **subject)],
                 seed=seed, paths=paths, trace=True)
    return result, result.traces["X"], result.tickers[0]


def test_a_proposed_event_happens_as_often_as_stated_on_a_day_within_its_dates_and_is_sized_inside_its_range():
    fall = Proposed("1", first=2, last=5, probability=0.3, direction=-1, low=0.055, high=0.07)
    result, trace, forecast = proposed([fall])
    fell_on, size = trace.events["1"]
    happened = fell_on != NEVER
    error = 4 * math.sqrt(0.3 * 0.7 / PATHS)
    assert happened.mean() == pytest.approx(0.3, abs=error)
    assert set(fell_on[happened]) == {2, 3, 4, 5}
    for day in (2, 3, 4, 5):
        assert (fell_on == day).mean() == pytest.approx(0.3 / 4, abs=error)
    # Sizes come from its past jumps (3, 5, 6 and 8 percent), each held inside the range given: 5.5 to 7 percent.
    assert set(np.round(-size[happened], 4)) == {0.055, 0.06, 0.07}
    assert (size[~happened] == 0).all()
    # The name does nothing else, so the path is the event: down by its size from its day on.
    rows = np.arange(PATHS)[happened]
    assert trace.path[rows, fell_on[happened]] == pytest.approx(size[happened], abs=1e-4)
    assert trace.path[rows, fell_on[happened] - 1] == pytest.approx(0.0, abs=1e-4)
    assert np.abs(trace.path[~happened]).max() < 1e-3
    # Every fall of 5.5% or more passes the 5% stop, so the trade ends at the stop exactly where the event happened.
    table = read(trace, result.states, result.market, forecast, sessions=10, matures_on=MATURES,
                 state_names=["calm"], state_now=[1.0], labels={"1": "the recall widens"})
    assert table.ways["bad"].share == pytest.approx(happened.mean())
    row = event(table, "proposed:1")
    assert row.given["bad"] == 1.0 and row.given["middle"] == 0.0 and row.chance == pytest.approx(happened.mean())
    assert table.proposed and table.ways["bad"].settled["50"] in (4.0, 5.0)
    assert trace.left_out == {} and trace.capped == {}


def test_an_event_that_reaches_beyond_the_largest_past_jump_is_cut_to_it_and_that_is_noted():
    rise = Proposed("up", first=0, last=0, probability=1.0, direction=1, low=0.10, high=0.30)
    _, trace, _ = proposed([rise], paths=500)
    _, size = trace.events["up"]
    assert set(np.round(size, 6)) == {0.08}                  # the largest past jump, though the range starts above it
    assert trace.capped == {"up": pytest.approx(0.08)}
    within = Proposed("in", first=0, last=0, probability=1.0, direction=1, low=0.01, high=0.08)
    assert proposed([within], paths=500)[1].capped == {}


def test_an_event_that_cannot_be_simulated_is_left_out_with_the_reason():
    late = Proposed("late", first=DAYS, last=DAYS + 3, probability=0.5, direction=-1, low=0.02, high=0.05)
    backwards = Proposed("backwards", first=6, last=4, probability=0.5, direction=-1, low=0.02, high=0.05)
    _, trace, _ = proposed([late, backwards], paths=500)
    assert trace.events == {} and set(trace.left_out) == {"late", "backwards"}
    assert "outside the simulated days" in trace.left_out["late"]
    fine = Proposed("fine", first=0, last=3, probability=0.5, direction=-1, low=0.02, high=0.05)
    _, trace, _ = proposed([fine], paths=500, params_market=still(jumps=[], jump_rate=0.0))
    assert trace.events == {} and "past jump to size it by" in trace.left_out["fine"]
    # Dates that start before the first simulated day or run past the last are held to the simulated days.
    wide = Proposed("wide", first=-3, last=DAYS + 5, probability=1.0, direction=-1, low=0.02, high=0.05)
    fell_on = proposed([wide], paths=2000)[1].events["wide"][0]
    assert fell_on.min() == 0 and fell_on.max() == DAYS - 1


def test_a_name_and_a_sector_that_never_jumped_are_sized_by_every_names_jumps():
    market = noise(0.01, seed=1)
    quiet = plain("X", beta=0.0, vol=1e-6, jumps=[], sector="XLK")
    elsewhere = plain("Z", jumps=[-0.05, 0.11], sector="XLV")
    params = hand_params([quiet, plain("Y", jumps=[], sector="XLK"), elsewhere, plain("XLK")], QUIET)
    event_ = Proposed("1", first=0, last=0, probability=1.0, direction=-1, low=0.0, high=1.0)
    subject = Subject("X", 100.0, noise(1e-6, seed=2), stop=95.0, events=[event_])
    trace = run(params, MONDAY, market, {"XLK": market + noise(0.006, seed=5)}, [subject], seed=1, paths=2000,
                trace=True).traces["X"]
    assert set(np.round(-trace.events["1"][1], 6)) == {0.05, 0.11} and trace.left_out == {}


def test_a_name_with_few_jumps_of_its_own_is_sized_by_its_sectors_too():
    market = noise(0.01, seed=1)
    few = plain("X", beta=0.0, vol=1e-6, jumps=[0.02], sector="XLK")
    peer = plain("Y", jumps=[-0.09, 0.04] * 6, sector="XLK")
    other = plain("Z", jumps=[0.5] * 12, sector="XLV")
    fund = plain("XLK")
    params = hand_params([few, peer, other, fund], QUIET)
    event_ = Proposed("1", first=0, last=0, probability=1.0, direction=-1, low=0.0, high=1.0)
    subject = Subject("X", 100.0, noise(1e-6, seed=2), stop=95.0, events=[event_])
    funds = {"XLK": market + noise(0.006, seed=5)}
    trace = run(params, MONDAY, market, funds, [subject], seed=1, paths=3000, trace=True).traces["X"]
    assert set(np.round(-trace.events["1"][1], 6)) == {0.02, 0.04, 0.09}       # its own, and its sector peer's
    # With ten of its own it needs no one else's.
    many = plain("X", beta=0.0, vol=1e-6, jumps=[0.02] * 10, sector="XLK")
    trace = run(hand_params([many, peer, fund], QUIET), MONDAY, market, funds, [subject], seed=1, paths=500,
                trace=True).traces["X"]
    assert set(np.round(-trace.events["1"][1], 6)) == {0.02}


def test_on_a_day_a_proposed_event_falls_no_unannounced_jump_is_drawn_as_well():
    params_market = still(jump_rate=0.5, jumps=[-0.03, 0.05, 0.08, -0.06])
    fall = Proposed("1", first=4, last=4, probability=0.5, direction=-1, low=0.04, high=0.04)
    _, with_event, _ = proposed([fall], paths=4000, params_market=params_market)
    _, without, _ = proposed([], paths=4000, params_market=params_market)
    happened = with_event.events["1"][0] != NEVER
    steps = np.diff(with_event.path, axis=1, prepend=0.0)
    assert (without.jumps[:, 4] != 0).mean() == pytest.approx(0.5, abs=0.04)        # half the paths had a jump that day
    assert steps[happened, 4] == pytest.approx(-0.04, abs=1e-4)                     # the event, and nothing on top of it
    assert (with_event.jumps[happened, 4] == 0).all()
    # A path the event does not fall on is what it was without the event, to the last digit.
    assert np.array_equal(with_event.path[~happened], without.path[~happened])
    assert np.array_equal(with_event.jumps[~happened], without.jumps[~happened])
    # And so are the other days of a path it does fall on, as far as adding them up from another level allows.
    assert np.delete(steps[happened], 4, axis=1) == pytest.approx(
        np.delete(np.diff(without.path, axis=1, prepend=0.0)[happened], 4, axis=1), abs=1e-12)


def test_on_the_day_a_proposed_event_falls_the_names_ordinary_move_is_not_drawn_either():
    """A name with ordinary days of its own, of about 1% each. On the event's day the whole move is the event."""
    market = noise(0.01, seed=1)
    params = hand_params([plain("X", beta=0.0, jumps=[-0.03, 0.05, 0.08, -0.06])], QUIET)
    fall = Proposed("1", first=3, last=3, probability=1.0, direction=-1, low=0.05, high=0.05)
    subject = Subject("X", 100.0, noise(0.01, seed=2), stop=80.0, events=[fall])
    trace = run(params, MONDAY, market, {}, [subject], seed=4, paths=3000, trace=True).traces["X"]
    steps = np.diff(trace.path, axis=1, prepend=0.0)
    assert steps[:, 3] == pytest.approx(-0.05, abs=1e-9)
    assert 0.005 < steps[:, 2].std() < 0.02 and 0.005 < steps[:, 4].std() < 0.02     # the days around it are ordinary


def test_an_earnings_report_on_the_same_day_still_counts_and_two_events_on_one_day_add_up():
    reporting = still(reports=[at(date(2026, 8, 31), 16, 5)], report_lag=1, earnings=[2.0], vol=0.01, residuals=[0.0])
    fall = Proposed("1", first=4, last=4, probability=1.0, direction=-1, low=0.04, high=0.04)
    more = Proposed("2", first=4, last=4, probability=1.0, direction=-1, low=0.03, high=0.03)
    _, trace, forecast = proposed([fall, more], paths=300, params_market=reporting, next_report=date(2026, 11, 30))
    assert forecast.report_modelled and trace.report_index == 4
    steps = np.diff(trace.path, axis=1, prepend=0.0)
    report = forecast.own_vol * 2.0
    assert steps[:, 4] == pytest.approx(report - 0.04 - 0.03, abs=1e-6)
    assert trace.report_move == pytest.approx(2.0)


def test_an_event_draws_from_a_stream_of_its_own_so_the_rest_of_the_simulation_is_what_it_was():
    fall = Proposed("1", first=0, last=9, probability=0.4, direction=-1, low=0.03, high=0.08)
    rise = Proposed("2", first=0, last=9, probability=0.4, direction=1, low=0.03, high=0.08)
    _, alone, _ = proposed([fall], paths=3000)
    _, both, _ = proposed([fall, rise], paths=3000)
    _, again, _ = proposed([fall, rise], paths=3000)
    assert np.array_equal(alone.events["1"][0], both.events["1"][0])
    assert np.array_equal(alone.events["1"][1], both.events["1"][1])
    assert np.array_equal(both.path, again.path)
    assert not np.array_equal(proposed([fall], paths=3000, seed=6)[1].events["1"][0], alone.events["1"][0])
    assert not np.array_equal(both.events["1"][0], both.events["2"][0])
