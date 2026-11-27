"""The quant engineer's seat at the desk's close meeting: it speaks, keeps what it said, and changes nothing else."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from data.providers import DataError
from data.snapshot import KEPT_BARS, load_snapshot
from forecast.params import ForecastError, ParamStore
from models.forecast import ForecastSet
from models.planning import ScenarioDraft
from models.signals import ProposalSetDraft, TickerViewDraft
from pipeline import desk as desk_module
from pipeline import quant
from pipeline.quant import QUANT, recompute, seed_of, stop_of
from tests.desk_fakes import ARM, hold, ny, round_at
from tests.forecast_fakes import FAR, MadeUpMarket, forecast_desk, rising_names
from tests.pipeline_fakes import ScriptedLLM, by_ticker, make_pipeline, proposals, view

MONDAY, TUESDAY, WEDNESDAY = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)


def said(desk, run_id, kind=None):
    """The quant engineer's lines in a meeting."""
    return [event for event in desk.journal.events(run_id)
            if event.stage == "forecast" and (kind is None or event.kind == kind)]


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    """A desk that held its close meeting on Monday, Tuesday and Wednesday."""
    desk = forecast_desk(tmp_path_factory.mktemp("week"), ny(23, 18, 30))
    results = [hold(desk, ny(day, 18, 30)) for day in (23, 24, 25)]
    assert [result.status for result in results] == ["ok", "ok", "ok"]
    return desk, results


# ── What the seat says ───────────────────────────────────────────────────────

def test_the_seat_speaks_after_the_views_and_before_the_debates(week):
    desk, (monday, _, _) = week
    events = desk.journal.events(monday.run_id)
    stages = [event.stage for event in events]
    first, last = stages.index("forecast"), len(stages) - 1 - stages[::-1].index("forecast")
    assert set(stages[first:last + 1]) == {"forecast"}                     # one unbroken turn
    assert stages[first - 1] == "views" and stages[last + 1] == "debate"
    lines = said(desk, monday.run_id)
    assert {(event.speaker, event.speaker_name, event.is_llm, event.addressed_to) for event in lines} \
        == {(QUANT[0], QUANT[1], False, "all")}
    assert [event.kind for event in lines] == [
        "forecast_parameters", "market_state", "forecast", "forecast", "backward", "backward"]
    assert [event.ticker for event in lines] == [None, None, "CLMB", "RISE", "CLMB", "RISE"]


def test_what_it_says_about_the_market_and_each_name(week):
    desk, (monday, _, _) = week
    state = said(desk, monday.run_id, "market_state")[0]
    assert state.remark.startswith("After today's close the market's state: calm 0.")
    assert state.remark.endswith("Parameters version 1, on data to 2026-11-20.")
    assert state.payload["states"] == ["calm", "turbulent"] and sum(state.payload["now"]) == pytest.approx(1.0)
    assert set(state.payload["ahead"]) == {"1", "5", "10", "20"} and state.payload["paths"] == 10_000
    assert state.payload["seed"] == seed_of(monday.run_id)

    rise = next(event for event in said(desk, monday.run_id, "forecast") if event.ticker == "RISE")
    assert rise.remark.startswith("RISE, 10 sessions on: half the simulated paths end between ")
    assert "(the sizing rule's) is touched on the way in " in rise.remark and "a notional target at " in rise.remark
    payload = rise.payload
    assert payload["ticker"] == "RISE" and payload["sector"] == "XLK" and payload["stop_from"] == "rule"
    assert payload["direction"] == "bullish" and payload["confidence"] == 0.6 and payload["view_horizon"] == 10
    assert payload["lean"] == 0.0 and payload["lean_basis"].endswith("no lean yet.")
    assert [item["sessions"] for item in payload["horizons"]] == [1, 5, 10, 20]
    ten = payload["horizons"][2]
    assert ten["quantiles"]["5"] < ten["quantiles"]["25"] < ten["quantiles"]["50"] < ten["quantiles"]["95"]
    assert 0 < ten["stop_touch"] < 1 and ten["matures_on"] == "2026-12-08"
    # The percentiles kept for scoring stay in the forecast on record, not in what a page is sent.
    assert "grid" not in ten and "naive_grid" not in ten and len(json.dumps(payload)) < 3000


def test_a_report_inside_the_horizon_is_said(week):
    desk, (monday, _, _) = week
    climb = next(event for event in said(desk, monday.run_id, "forecast") if event.ticker == "CLMB")
    # It reports after the close on Wednesday 2 December, so Thursday reacts.
    assert climb.remark.endswith("An earnings report moves the session of 2026-12-03, drawn from past report days.")
    assert climb.payload["report_on"] == "2026-12-03" and climb.payload["report_modelled"] is True


def test_the_stop_asked_about_is_the_one_a_buy_would_get_and_then_the_one_on_the_books(week):
    desk, (monday, tuesday, _) = week
    order = next(order for order in monday.table.orders if order.ticker == "RISE")
    first = next(event for event in said(desk, monday.run_id, "forecast") if event.ticker == "RISE")
    assert first.payload["stop"] == order.protective_stop and first.payload["stop_from"] == "rule"
    held = next(event for event in said(desk, tuesday.run_id, "forecast") if event.ticker == "RISE")
    position = next(holding for holding in tuesday.table.holdings if holding.ticker == "RISE")
    assert held.payload["stop"] == position.stop_price == order.protective_stop and held.payload["stop_from"] == "position"
    assert "(on the books)" in held.remark


def test_whose_stop_it_is():
    from models.signals import TickerView
    from models.trading import Position

    def analyst(stop):
        return TickerView(ticker="X", session_date=MONDAY, held=False, direction="bullish", confidence=0.6,
                          horizon_days=10, thesis="t", catalysts="c", invalidation="i", proposed_stop=stop, remark="r")

    held = Position(ticker="X", shares=5, avg_cost=100.0, entry_date=MONDAY, stop_price=93.5, value=500.0)
    assert stop_of(100.0, 2.0, analyst(97.0), held) == (93.5, "position")              # the books come first
    assert stop_of(100.0, 2.0, analyst(97.0), held.model_copy(update={"stop_price": None})) == (None, "")
    assert stop_of(100.0, 2.0, analyst(97.0), None) == (97.0, "analyst")               # 1.5 ATR below: allowed
    assert stop_of(100.0, 2.0, analyst(99.5), None) == (96.0, "rule")                  # too near: refused, 2 ATR instead
    assert stop_of(100.0, 2.0, analyst(None), None) == (96.0, "rule")
    assert stop_of(100.0, 2.0, None, None) == (96.0, "rule")
    assert stop_of(100.0, 0.1, None, None) == (99.0, "rule")                           # never nearer than 1% of the price
    assert stop_of(100.0, None, analyst(97.0), None) == (None, "")                     # no ATR, nothing to judge by
    assert stop_of(1.0, 5.0, None, None) == (None, "")                                 # a stop below zero is no stop


def test_with_a_position_the_book_is_forecast_too(week):
    desk, (monday, tuesday, _) = week
    assert said(desk, monday.run_id, "book_forecast") == []                # nothing was held on Monday
    line = said(desk, tuesday.run_id, "book_forecast")[0]
    assert line.remark.startswith("The book as it stands, 1 position(s) and no trading after today, 10 sessions on: ")
    assert line.payload["positions"] == 1 and line.payload["equity"] == tuesday.table.account.equity
    assert [item["sessions"] for item in line.payload["horizons"]] == [1, 5, 10, 20]
    # After the names; then the trades worked back, and the scores last.
    assert [event.kind for event in said(desk, tuesday.run_id)] == [
        "market_state", "forecast", "forecast", "book_forecast", "backward", "backward", "forecast_scores"]


def test_a_holding_with_no_usable_price_that_evening_still_counts_in_the_book(tmp_path):
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    hold(desk, ny(23, 18, 30))
    tuesday = hold(desk, ny(24, 18, 30))                       # RISE was bought at Tuesday's open
    assert [holding.ticker for holding in tuesday.table.holdings] == ["RISE"]
    desk.market.missing.add("RISE")                            # on Wednesday evening the source has nothing for it
    wednesday = hold(desk, ny(25, 18, 30))
    assert wednesday.status in ("ok", "degraded")
    line = said(desk, wednesday.run_id, "book_forecast")[0]
    assert line.payload["positions"] == 1
    carried = wednesday.table.holdings[0].value                # what the account says the holding is worth
    assert carried > 0 and line.payload["notes"] == [
        "RISE is held but the snapshot has no usable price for it: its value is taken as fixed at the "
        f"{carried:,.2f} the account carries it at."]
    assert line.remark.endswith(line.payload["notes"][0])
    # Nothing in the book moves, so every path leaves it where the account has it, and no breaker is near.
    equity = wednesday.table.account.equity
    one = line.payload["horizons"][0]
    assert one["equity"]["5"] == one["equity"]["95"] == pytest.approx(equity, abs=0.01)
    assert one["drawdown_breaker"] == 0.0
    # Worked out again from the record, the same.
    store = desk_module.ParamStore(desk.forecast_dir)
    recorded = desk.journal.forecast_set(ARM, WEDNESDAY)
    assert recompute(desk.journal, wednesday.run_id, store).model_dump_json() == recorded.model_dump_json()


# ── On record, and reconciled ────────────────────────────────────────────────

def test_every_forecast_is_kept_whole(week):
    desk, (monday, tuesday, _) = week
    kept = desk.journal.forecast_set(ARM, MONDAY)
    assert isinstance(kept, ForecastSet) and kept.run_id == monday.run_id and kept.seed == seed_of(monday.run_id)
    assert (kept.params_version, kept.params_cutoff, kept.paths) == (1, date(2026, 11, 20), 10_000)
    assert [item.ticker for item in kept.tickers] == ["CLMB", "RISE"] and kept.book.positions == 0
    assert all(len(horizon.grid) == 99 and len(horizon.naive_grid) == 99
               for item in kept.tickers for horizon in item.horizons)
    assert kept.problems == []
    assert [item.session_date for item in desk.journal.forecast_sets(ARM)] == [MONDAY, TUESDAY, WEDNESDAY]
    assert desk.journal.forecast_set(ARM, TUESDAY).run_id == tuesday.run_id
    # Each meeting has a seed of its own, fixed by its id.
    assert len({item.seed for item in desk.journal.forecast_sets(ARM)}) == 3
    assert seed_of(monday.run_id) == seed_of(monday.run_id) and 0 <= seed_of("x") < 2 ** 32


def test_forecasts_that_come_due_are_set_against_what_happened_at_the_next_close(week):
    """Completion standard: every forecast is kept, and reconciled by itself once its horizon has ended."""
    desk, (monday, tuesday, wednesday) = week
    assert said(desk, monday.run_id, "forecast_scores") == []
    line = said(desk, tuesday.run_id, "forecast_scores")[0]
    assert line.remark.startswith("2 forecast(s) came due and were set against what happened. 1 session ahead, "
                                  "2 forecast(s) made on 1 day(s): the outcome fell ")
    assert "The plain forecast: " in line.remark and "the plain forecast's." in line.remark
    assert line.payload["due_now"] == 2
    card = line.payload["card"]
    assert (card["scored"], card["pending"], card["unpriced"]) == (2, 14, 0)        # two days of forecasts, two matured
    assert [item["label"] for item in card["by_horizon"]] == ["1 session"]
    later = said(desk, wednesday.run_id, "forecast_scores")[0]
    assert later.payload["due_now"] == 2 and later.payload["card"]["scored"] == 4

    scores = desk.journal.forecast_scores(ARM)
    done = [score for score in scores if score.status == "scored"]
    assert {(score.session_date, score.ticker, score.sessions) for score in done} == {
        (MONDAY, "CLMB", 1), (MONDAY, "RISE", 1), (TUESDAY, "CLMB", 1), (TUESDAY, "RISE", 1),
    }
    # What happened is read from the market itself: Monday's close to Tuesday's.
    rise = next(score for score in done if (score.session_date, score.ticker) == (MONDAY, "RISE"))
    frame = desk.market.frame("RISE", TUESDAY)
    import math
    assert rise.realized == pytest.approx(math.log(frame["close"].iloc[-1] / frame["close"].iloc[-2]), abs=1e-6)
    assert rise.stop_touched in (True, False) and 0 <= rise.pit <= 1


def test_the_parameters_are_estimated_once_a_week(tmp_path):
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    store = ParamStore(tmp_path / "forecast")
    monday = hold(desk, ny(23, 18, 30))
    assert store.versions() == [1] and len(said(desk, monday.run_id, "forecast_parameters")) == 1
    line = said(desk, monday.run_id, "forecast_parameters")[0]
    assert line.remark.startswith("The week's parameters were estimated: version 1, on data to 2026-11-20, for 15 name(s)")
    assert line.payload["version"] == 1 and line.payload["cutoff"] == "2026-11-20" and line.payload["names"] == 15
    long_fetches = len(desk.market.asked["bars"])

    tuesday = hold(desk, ny(24, 18, 30))
    assert said(desk, tuesday.run_id, "forecast_parameters") == [] and store.versions() == [1]
    # Tuesday fetched the day's data like any meeting, and no history for an estimate: nothing ending at the cut-off.
    assert date(2026, 11, 20) not in desk.market.asked["ends"][long_fetches:]

    for day in (25, 27):
        hold(desk, ny(day, 18, 30))
    next_monday = hold(desk, ny(30, 18, 30))
    assert store.versions() == [1, 2] and store.latest().cutoff == date(2026, 11, 27)
    assert "version 2, on data to 2026-11-27" in said(desk, next_monday.run_id, "forecast_parameters")[0].remark
    assert desk.journal.forecast_set(ARM, date(2026, 11, 30)).params_version == 2
    assert desk.journal.forecast_set(ARM, date(2026, 11, 27)).params_version == 1


# ── The same every time ──────────────────────────────────────────────────────

def test_a_meetings_forecast_can_be_worked_out_again_to_the_last_digit(week, tmp_path_factory):
    """Completion standard: from the meeting's snapshot, its parameters and its seed, the same forecast digit for digit."""
    desk, (monday, tuesday, _) = week
    store = desk_module.ParamStore(desk.forecast_dir)
    for result in (monday, tuesday):
        recorded = desk.journal.forecast_set(ARM, result.table.session_date)
        assert recomputed_json(desk, result.run_id, store) == recorded.model_dump_json()
    # A superseded or unknown meeting is refused in words.
    with pytest.raises(ForecastError, match="no finished close meeting nothing"):
        recompute(desk.journal, "nothing", store)
    with pytest.raises(ForecastError, match="Version 1 of the parameters is not on disk"):
        recompute(desk.journal, monday.run_id, ParamStore(tmp_path_factory.mktemp("empty")))


def recomputed_json(desk, run_id, store) -> str:
    return recompute(desk.journal, run_id, store).model_dump_json()


def test_the_snapshot_keeps_the_bars_the_forecast_was_made_from(week):
    desk, (monday, _, _) = week
    path = desk.journal.rows("SELECT snapshot_path FROM runs WHERE run_id = ?", (monday.run_id,))[0]["snapshot_path"]
    snapshot = load_snapshot(path)
    assert sorted(snapshot.market_bars) == sorted(KEPT_BARS) and len(KEPT_BARS) == 12
    index = snapshot.market_bars["^GSPC"]
    assert index.dates[-1] == MONDAY and len(index.dates) == len(index.adj_close) > 250


# ── It changes nothing else ──────────────────────────────────────────────────

def record_of(desk, results) -> dict:
    """Everything the meetings said, asked and did, the quant engineer's and the scenario analyst's turns left out.

    A plan keeps what the simulation gave its trade's endings, and the notional target they were worked out for.
    Those two fields are the forecast's own figures set down by code, and are left out with it.
    """
    def payload(event) -> str:
        kept = event.payload
        if event.kind == "plan":
            kept = {key: value for key, value in kept.items() if key not in ("chances", "target")}
        return json.dumps(kept, sort_keys=True)

    events = [
        [(e.stage, e.speaker, e.addressed_to, e.kind, e.ticker, e.remark, payload(e))
         for e in desk.journal.events(result.run_id) if e.stage not in ("forecast", "scenario")]
        for result in results
    ]
    orders = [tuple(row) for row in desk.journal.rows(
        "SELECT order_id, ticker, side, kind, shares, stop_price, protective_stop, status, created_session, "
        "filled_price, note FROM paper_orders ORDER BY order_id")]
    return {
        "prompts": [call for call in desk.llm.calls if call[0] != "ScenarioDraft"], "events": events, "orders": orders,
        # The count of model calls is the one thing in a table that the scenario analyst's turn adds to.
        "tables": [result.table.model_dump_json(exclude={"usage"}) for result in results],
        "equity": [tuple(row) for row in desk.journal.rows("SELECT * FROM paper_equity ORDER BY session_date")],
    }


def test_with_the_forecast_on_or_off_every_prompt_decision_and_order_is_the_same(tmp_path, monkeypatch):
    """Completion standard: at this stage the forecast bears on no order. Three days, a book, a check-in in between."""
    def three_days(folder):
        desk = forecast_desk(folder, ny(23, 18, 30))
        results = [hold(desk, ny(23, 18, 30)), hold(desk, ny(24, 18, 30))]
        desk.news.add("RISE", "Rise Corp loses its largest customer")
        round_at(desk, ny(25, 8, 10))                                    # the watch finds the headline
        checkin = hold(desk, ny(25, 8, 45))                              # Wednesday's check-in before the open
        results.append(hold(desk, ny(25, 18, 30)))
        return desk, results, checkin

    on, with_forecast, held_on = three_days(tmp_path / "on")
    monkeypatch.setattr(desk_module.config, "FORECAST_ENABLED", False)
    off, without, held_off = three_days(tmp_path / "off")
    # The check-in was held, with a book to look at, and went the same way in both.
    assert held_on.status == held_off.status == "ok" and held_on.record.updates
    assert held_on.record.model_dump(exclude={"meeting_id"}) == held_off.record.model_dump(exclude={"meeting_id"})

    assert sum(len(said(on, result.run_id)) for result in with_forecast) >= 10
    assert all(said(off, result.run_id) == [] for result in without)
    assert off.journal.forecast_sets(ARM) == [] and not (tmp_path / "off" / "forecast").exists()
    assert len(on.journal.forecast_sets(ARM)) == 3
    assert record_of(on, with_forecast) == record_of(off, without)
    assert len(record_of(on, with_forecast)["orders"]) >= 1               # and there were orders to compare
    # The scenario analyst was asked with the forecast on, and those calls are all the tables differ by.
    # It is asked about a name only when the ledger holds a headline about it: here, RISE on Wednesday.
    asked = on.llm.count(ScenarioDraft)
    assert asked == 1 and off.llm.count(ScenarioDraft) == 0
    assert sum(result.table.usage.calls for result in with_forecast) - asked \
        == sum(result.table.usage.calls for result in without)


def test_a_forecast_that_breaks_costs_the_meeting_nothing(tmp_path, monkeypatch):
    plain = forecast_desk(tmp_path / "plain", ny(23, 18, 30))
    expected = hold(plain, ny(23, 18, 30))

    def broken(*args, **kwargs):
        raise RuntimeError("a bug in the engine")

    monkeypatch.setattr(quant, "speak", broken)
    desk = forecast_desk(tmp_path / "broken", ny(23, 18, 30))
    result = hold(desk, ny(23, 18, 30))
    lines = said(desk, result.run_id)
    assert [(line.kind, line.remark) for line in lines] == [
        ("forecast_problem", "No forecast at this meeting: RuntimeError: a bug in the engine"),
    ]
    assert result.status == "ok" and result.table.warnings == []
    assert result.table.model_dump_json(exclude={"usage"}) == expected.table.model_dump_json(exclude={"usage"})
    # With no forecast there is nothing to rehearse, and nothing is said of the scenario analyst at all.
    assert desk.llm.count(ScenarioDraft) == 0
    assert [event for event in desk.journal.events(result.run_id) if event.stage == "scenario"] == []
    assert [event.kind for event in plain.journal.events(expected.run_id) if event.stage == "scenario"] == ["scenario_note"]
    assert desk.journal.forecast_sets(ARM) == [] and desk.journal.backward_sets(ARM) == []


class NoHistory:
    """A source that serves the day's data and fails whenever years of history are asked for."""

    def __init__(self, inner: MadeUpMarket) -> None:
        self.inner = inner
        self.names, self.asked = inner.names, inner.asked

    def daily_bars(self, symbols, start, end):
        if end - start > timedelta(days=600):
            raise DataError("Yahoo Finance price download failed: scripted outage")
        return self.inner.daily_bars(symbols, start, end)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_when_the_parameters_cannot_be_estimated_that_is_said_and_nothing_is_forecast(tmp_path):
    desk = forecast_desk(tmp_path, ny(23, 18, 30), market=NoHistory(MadeUpMarket(rising_names(), end=FAR)))
    result = hold(desk, ny(23, 18, 30))
    lines = said(desk, result.run_id)
    assert [line.kind for line in lines] == ["forecast_problem"]
    assert lines[0].remark == (
        "No forecast at this meeting. No parameters could be estimated on data to 2026-11-20. The index's history "
        "could not be fetched: Yahoo Finance price download failed: scripted outage"
    )
    assert result.status == "ok" and len(result.table.orders) == 1
    assert desk.journal.forecast_sets(ARM) == [] and ParamStore(tmp_path / "forecast").versions() == []


def test_when_this_weeks_estimate_fails_last_weeks_parameters_are_used_and_that_is_said(tmp_path):
    market = MadeUpMarket(rising_names(), end=FAR)
    desk = forecast_desk(tmp_path, ny(23, 18, 30), market=market)
    for day in (23, 24, 25, 27):
        hold(desk, ny(day, 18, 30))
    desk.builder.market = NoHistory(market)
    result = hold(desk, ny(30, 18, 30))
    lines = said(desk, result.run_id)
    assert lines[0].kind == "forecast_problem" and lines[0].remark.startswith(
        "This week's parameters could not be estimated on data to 2026-11-27 (The index's history could not be fetched: ")
    assert lines[0].remark.endswith("Version 1, on data to 2026-11-20, is used instead.")
    assert lines[1].kind == "market_state" and "Parameters version 1, on data to 2026-11-20." in lines[1].remark
    assert desk.journal.forecast_set(ARM, date(2026, 11, 30)).params_version == 1


def test_a_name_the_parameters_do_not_know_is_named_and_the_rest_are_forecast(tmp_path):
    names = rising_names()
    desk = forecast_desk(tmp_path, ny(23, 18, 30), names=names)
    hold(desk, ny(23, 18, 30))
    # A name that joins the universe in the middle of the week was not in Monday's estimate.
    names["LATE"] = names["RISE"].__class__(beta=0.5, vol=0.01, drift=0.004)
    desk.builder.market = MadeUpMarket(names, end=FAR)
    desk.universe = sorted(names)
    result = hold(desk, ny(24, 18, 30))
    assert "LATE" in result.table.candidates
    problem = said(desk, result.run_id, "forecast_problem")[0]
    assert problem.remark == ("Not forecast. LATE: version 1 of the parameters has nothing on it: it was not in "
                              "the estimate, or its history could not be used.")
    assert problem.payload == {"problems": [problem.remark.removeprefix("Not forecast. ")]}
    assert "LATE" not in [event.ticker for event in said(desk, result.run_id, "forecast")]
    assert "RISE" in [event.ticker for event in said(desk, result.run_id, "forecast")]
    assert desk.journal.forecast_set(ARM, TUESDAY).problems == problem.payload["problems"]


def test_a_snapshot_taken_before_the_bars_were_kept_gets_no_forecast_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr("data.snapshot.KEPT_BARS", [])
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    result = hold(desk, ny(23, 18, 30))
    assert [(line.kind, line.remark) for line in said(desk, result.run_id)] == [
        ("forecast_problem", "No forecast at this meeting: the index's daily bars are not in the snapshot."),
    ]
    assert result.status == "ok"


def record(count: int, hits: int, confidence: float, matured: date) -> list:
    """`count` scored bullish views held with that confidence, `hits` of which beat the benchmark."""
    from models.evaluation import ViewScore

    return [
        ViewScore(
            run_id=f"old-{confidence}-{matured}-{index}", arm=ARM, ticker="RISE", session_date=date(2026, 6, 1),
            direction="bullish", confidence=confidence, horizon_days=10, held=False, starts_on=date(2026, 6, 2),
            matures_on=matured, status="scored", beat_benchmark=index < hits,
        )
        for index in range(count)
    ]


def test_a_record_that_has_earned_it_tilts_the_forecast_and_only_what_was_known_by_then_counts(tmp_path):
    """The one way a model's words become a number: by the record of views held with the same confidence."""
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    # 400 views at 0.60 to 0.69 known by Friday, 240 of them right: a lean of 0.17. Views at 0.80 have no such record,
    # and a thousand perfect views whose outcome only came on Tuesday were not known on Monday.
    desk.journal.save_view_scores(ARM, record(400, 240, 0.65, date(2026, 11, 20))
                                  + record(50, 50, 0.85, date(2026, 11, 20))
                                  + record(1000, 1000, 0.62, TUESDAY))
    desk.llm.replies[TickerViewDraft] = by_ticker({"CLMB": view(confidence=0.85)}, view(confidence=0.6))
    monday = hold(desk, ny(23, 18, 30))
    lines = {event.ticker: event for event in said(desk, monday.run_id, "forecast")}
    rise, climb = lines["RISE"].payload, lines["CLMB"].payload
    assert rise["lean"] == pytest.approx(0.1679, abs=1e-4)
    assert rise["lean_basis"] == ("400 scored view(s) held with confidence 0.60 to 0.69, 240 of them beat the "
                                  "benchmark: a lean of 0.17.")
    assert "The bullish view tilts the drift by 0.17 of the horizon's volatility, earned by the record of such views." \
        in lines["RISE"].remark
    assert climb["lean"] == 0.0 and "tilts the drift" not in lines["CLMB"].remark
    assert climb["lean_basis"] == ("50 scored view(s) held with confidence 0.80 to 0.89, 50 of them beat the "
                                   "benchmark: no lean yet.")
    # The middle of RISE's distribution sits higher than it would with no record, by the lean's share of its volatility.
    level = forecast_desk(tmp_path / "level", ny(23, 18, 30))
    plain_monday = hold(level, ny(23, 18, 30))
    flat = next(event for event in said(level, plain_monday.run_id, "forecast") if event.ticker == "RISE").payload
    assert rise["horizons"][2]["quantiles"]["50"] > flat["horizons"][2]["quantiles"]["50"] * 1.002
    # Worked out again later, when more scores are on record, the forecast is still the one that was made.
    desk.journal.save_view_scores(ARM, record(5000, 5000, 0.65, date(2026, 11, 20)))
    store = desk_module.ParamStore(desk.forecast_dir)
    assert recompute(desk.journal, monday.run_id, store).model_dump_json() \
        == desk.journal.forecast_set(ARM, MONDAY).model_dump_json()


def test_the_daily_pipeline_has_no_such_seat(tmp_path):
    """The baseline group is the one the desk is measured against. It forecasts nothing."""
    pipeline = make_pipeline(tmp_path)
    result = pipeline.run()[0]
    assert result.status == "ok"
    assert [event for event in pipeline.journal.events(result.run_id) if event.stage == "forecast"] == []
    assert pipeline.journal.forecast_sets("fake/a") == []
    assert not (tmp_path / "forecast").exists()


def test_a_view_with_a_stop_of_its_own_and_a_bearish_one_are_carried_into_the_forecast(tmp_path):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("RISE", "buy", 0.7))
    desk = forecast_desk(tmp_path, ny(23, 18, 30), llm=llm)
    probe = hold(desk, ny(23, 18, 30))
    rise = next(event for event in said(desk, probe.run_id, "forecast") if event.ticker == "RISE").payload
    own_stop = round(rise["close"] - 1.5 * (rise["close"] - rise["stop"]) / 2, 2)      # 1.5 ATR below, inside the range

    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = by_ticker({"RISE": view(stop=own_stop), "CLMB": view(direction="bearish", confidence=0.8, horizon=5)},
                                             view())
    llm.replies[ProposalSetDraft] = proposals(("RISE", "buy", 0.7))
    desk = forecast_desk(tmp_path / "second", ny(23, 18, 30), llm=llm)
    result = hold(desk, ny(23, 18, 30))
    lines = {event.ticker: event for event in said(desk, result.run_id, "forecast")}
    assert lines["RISE"].payload["stop"] == own_stop and lines["RISE"].payload["stop_from"] == "analyst"
    assert "(the analyst's)" in lines["RISE"].remark
    assert lines["CLMB"].payload["direction"] == "bearish" and lines["CLMB"].payload["view_horizon"] == 5
    assert lines["CLMB"].remark.startswith("CLMB, 5 sessions on: ")
    assert lines["CLMB"].payload["lean"] == 0.0            # no record yet: a view tilts nothing


def failing(*args, **kwargs):
    raise RuntimeError("the manager's turn broke")


def test_a_meeting_that_fails_after_the_seat_has_spoken_leaves_no_forecast_on_record(tmp_path, monkeypatch):
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    with monkeypatch.context() as broken:
        broken.setattr(desk_module._DeskClose, "_proposals", failing)
        failed = hold(desk, ny(23, 18, 30))
    assert failed.status == "failed"
    assert len(said(desk, failed.run_id, "forecast")) == 2          # it was said, and the record of the meeting has it
    assert desk.journal.forecast_sets(ARM) == [] and desk.journal.forecast_scores(ARM) == []
    # The meeting is held again an hour later and goes through: that one's forecast is the one on record.
    again = hold(desk, ny(23, 19, 45))
    assert again.status == "ok" and again.run_id != failed.run_id
    kept = desk.journal.forecast_set(ARM, MONDAY)
    assert kept.run_id == again.run_id and kept.seed == seed_of(again.run_id)
    assert [(score.status, score.sessions) for score in desk.journal.forecast_scores(ARM)] \
        == [("pending", sessions) for _ in ("CLMB", "RISE") for sessions in (1, 5, 10, 20)]
    store = desk_module.ParamStore(desk.forecast_dir)
    assert recompute(desk.journal, again.run_id, store).model_dump_json() == kept.model_dump_json()


def test_a_session_run_again_that_fails_leaves_the_earlier_forecast_and_its_scores_as_they_were(tmp_path, monkeypatch):
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    monday, tuesday = hold(desk, ny(23, 18, 30)), hold(desk, ny(24, 18, 30))
    wednesday = hold(desk, ny(25, 18, 30))                          # Tuesday's one-session forecasts are scored here
    before = (desk.journal.forecast_set(ARM, TUESDAY), desk.journal.forecast_scores(ARM))
    assert sum(1 for score in before[1] if score.session_date == TUESDAY and score.status == "scored") == 2

    desk.clock.now = ny(25, 19, 0)
    monkeypatch.setattr(desk_module._DeskClose, "_proposals", failing)
    rerun = desk_module._DeskClose(
        [desk.arm], builder=desk.builder, journal=desk.journal, universe=desk.universe, runs_dir=desk.runs_dir,
        snapshot_dir=desk.snapshot_dir, now=desk.clock, ledger=desk.ledger, forecast_dir=desk.forecast_dir,
    ).run(force=True)[0]
    assert rerun.status == "failed" and said(desk, rerun.run_id, "forecast")
    assert (desk.journal.forecast_set(ARM, TUESDAY), desk.journal.forecast_scores(ARM)) == before
    assert desk.journal.forecast_set(ARM, WEDNESDAY).run_id == wednesday.run_id
    assert monday.status == tuesday.status == "ok"


def test_when_reconciling_fails_todays_forecast_is_still_said_and_kept(tmp_path, monkeypatch):
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    hold(desk, ny(23, 18, 30))

    def broken(*args, **kwargs):
        raise ValueError("the scores could not be read")

    monkeypatch.setattr(quant, "reconcile", broken)
    tuesday = hold(desk, ny(24, 18, 30))
    lines = said(desk, tuesday.run_id)
    assert [line.kind for line in lines] == [
        "market_state", "forecast", "forecast", "book_forecast", "backward", "backward", "forecast_problem"]
    assert lines[-1].remark == ("The forecasts that came due could not be reconciled at this meeting: "
                                "ValueError: the scores could not be read")
    assert tuesday.status == "ok" and desk.journal.forecast_set(ARM, TUESDAY).run_id == tuesday.run_id
    # What the meeting says and what is on record agree: both have today's forecast.
    assert [item.ticker for item in desk.journal.forecast_set(ARM, TUESDAY).tickers] \
        == [line.ticker for line in lines if line.kind == "forecast"]


def test_a_forecast_the_engine_cannot_make_is_said_in_the_engines_own_words(tmp_path, monkeypatch):
    def cannot(*args, **kwargs):
        raise ForecastError("the index's return for 2026-11-23 is not in the snapshot.")

    monkeypatch.setattr(quant.engine, "run", cannot)
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    result = hold(desk, ny(23, 18, 30))
    lines = said(desk, result.run_id)
    assert [(line.kind, line.remark) for line in lines[1:]] == [
        ("forecast_problem", "No forecast at this meeting: the index's return for 2026-11-23 is not in the snapshot."),
    ]
    assert lines[0].kind == "forecast_parameters"                  # the estimate itself went through, and is kept
    assert result.status == "ok" and desk.journal.forecast_sets(ARM) == []


def test_a_forecast_that_cannot_be_made_ready_to_keep_is_dropped_at_the_seats_turn_not_at_the_meetings_end(tmp_path, monkeypatch):
    """What is kept when the meeting ends is made ready while a failure still costs one line and nothing else."""
    plain_desk = forecast_desk(tmp_path / "plain", ny(23, 18, 30))
    expected = hold(plain_desk, ny(23, 18, 30))

    def unwritable(forecasts):
        raise RuntimeError("the rows could not be made")

    monkeypatch.setattr(quant, "waiting", unwritable)
    desk = forecast_desk(tmp_path / "broken", ny(23, 18, 30))
    result = hold(desk, ny(23, 18, 30))
    assert [(line.kind, line.remark) for line in said(desk, result.run_id)] == [
        ("forecast_problem", "No forecast at this meeting: RuntimeError: the rows could not be made"),
    ]
    # The meeting itself finished, with the orders it would have placed anyway, and nothing half-kept.
    assert result.status == "ok"
    assert result.table.model_dump_json(exclude={"usage"}) == expected.table.model_dump_json(exclude={"usage"})
    assert len(result.table.orders) == 1 and desk.journal.forecast_sets(ARM) == []
    assert desk.journal.backward_sets(ARM) == [] and desk.llm.count(ScenarioDraft) == 0
