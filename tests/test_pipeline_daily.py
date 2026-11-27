"""The daily pipeline end to end, offline: synthetic prices and a scripted model."""

import re
from datetime import datetime, timezone

import pytest

import config
from journal.store import Journal
from models.signals import (
    DebateArgumentDraft, DebateRulingDraft, DomainAssessmentDraft, ProposalSetDraft, SignalTable, TickerViewDraft,
)
from pipeline import daily
from pipeline.daily import Arm, DailyPipeline, progress, status_lines
from execution.paper import PaperBroker
from tests.pipeline_fakes import (
    Clock, ScriptedLLM, SyntheticMarket, argument, assessment, by_agent, by_ticker, day, make_pipeline,
    proposals, ruling, session, view,
)
from utils.llm_client import LLMError


def run_once(pipeline, **options):
    results = pipeline.run(**options)
    assert len(results) == 1
    return results[0]


def book(pipeline, arm="fake/a"):
    return PaperBroker(pipeline.journal, arm)


def waiting_orders(pipeline, arm="fake/a"):
    """Orders at the broker that have not filled, protective stops left out: ticker to side."""
    return {order.ticker: order.side for order in book(pipeline, arm).orders() if order.kind != "stop"}


def seed_position(pipeline, arm, ticker, entry_session=10, shares=10):
    """Put a position straight into an arm's paper account, as if bought at that session's close."""
    market, when = SyntheticMarket(), session(entry_session).isoformat()
    close = 100.0 if ticker in market.missing else float(market.path(ticker, session(entry_session))["close"].iloc[-1])
    book(pipeline, arm)                                           # opens the account
    with pipeline.journal.transaction():
        pipeline.journal.write(
            "INSERT INTO paper_positions VALUES (?, ?, ?, ?, ?, 'Technology', ?, ?, NULL, ?, 1)",
            (arm, ticker, shares, close, when, when, close, close),
        )


# ── A normal day ─────────────────────────────────────────────────────────────

def test_first_run_produces_a_checked_signal_table(tmp_path):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("UPB", "buy", 0.6))
    pipeline = make_pipeline(tmp_path, llm)

    result = run_once(pipeline)
    table = result.table

    assert result.status == "ok" and table.status == "ok"
    assert table.session_date == session(13) and table.universe_size == 5
    assert table.candidates == ["UPA", "UPB", "UPC"]
    assert [v.ticker for v in table.views] == ["UPA", "UPB", "UPC"]
    assert table.regime.risk_appetite == "risk_on" and len(table.regime.assessments) == 7
    assert [(d.ticker, d.trigger, len(d.turns)) for d in table.debates] == [
        ("UPA", "top_candidate", 2), ("UPB", "top_candidate", 2), ("UPC", "top_candidate", 2),
    ]
    assert [(p.ticker, p.action, p.status) for p in table.proposals] == [
        ("UPA", "buy", "accepted"), ("UPB", "buy", "accepted"),
    ]
    assert table.holdings == [] and table.fills == []
    # 7 macro agents, 3 views, 3 debates of two arguments and a ruling, 1 portfolio manager
    assert table.usage.calls == 7 + 3 + 9 + 1 and table.usage.cost_usd is None

    # Each buy was sized, cut by the gate to the single-position limit, and placed with a stop.
    assert table.account.equity == 100_000 and table.account.cash == 100_000
    assert [(d.ticker, d.side, d.verdict) for d in table.risk] == [("UPA", "buy", "reduced"), ("UPB", "buy", "reduced")]
    assert all("Single position" in d.reasons[0] for d in table.risk)
    closes = {ticker: pipeline.builder.market.path(ticker, session(13))["close"].iloc[-1] for ticker in ("UPA", "UPB")}
    for order, decision in zip(table.orders, table.risk):
        assert order.shares == decision.approved_shares == int(10_000 // closes[order.ticker])
        assert 0 < order.protective_stop < closes[order.ticker]
    assert waiting_orders(pipeline) == {"UPA": "buy", "UPB": "buy"}


def stop_at(atrs):
    """An analyst that puts its stop this many ATRs below the close it is shown."""
    def reply(system_prompt, user_message):
        ticker_data = user_message.split("TICKER DATA")[1]
        close = float(re.search(r"- Close ([\d,.]+),", ticker_data).group(1).replace(",", ""))
        atr = float(re.search(r"ATR\(14\) ([\d,.]+) ", ticker_data).group(1).replace(",", ""))
        return view(stop=round(close - atrs * atr, 2))
    return reply


def test_an_analysts_stop_inside_the_range_is_the_one_the_order_carries(tmp_path):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = stop_at(3)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)

    result = run_once(pipeline)
    table = result.table

    assert result.status == "ok"
    stop = table.views[0].proposed_stop
    close = pipeline.builder.market.path("UPA", session(13))["close"].iloc[-1]
    assert table.views[0].ticker == "UPA" and 0 < stop < close and table.views[0].data_warnings == []
    [order], [decision] = table.orders, table.risk
    assert order.protective_stop == stop and "the analyst's stop, 3.0 x ATR" in order.reason
    # Risk budget 100,000 x 1% at the lower confidence, the analyst's 0.6, over the distance to that stop.
    assert decision.requested_shares == int(600 // (close - stop))
    text = result.markdown_path.read_text()
    assert f"Protective stops attached on fill: UPA at {stop:,.2f} (the analyst's level)." in text
    assert f"| {stop:,.2f}. Just below the 50-day average. | clean |" in text
    # The debaters and the portfolio manager are shown the stop too.
    assert f"Proposed stop for a new position: {stop:,.2f}." in llm.messages(ProposalSetDraft)[0]
    assert f"Proposed stop for a new position: {stop:,.2f}." in llm.messages(DebateArgumentDraft)[0]

    # Once filled, the position is protected at the analyst's level.
    pipeline._now.now = day(16)
    run_once(pipeline)
    assert book(pipeline).positions()["UPA"].stop_price == stop


@pytest.mark.parametrize("atrs", [0.5, 6])
def test_an_analysts_stop_outside_the_range_is_refused_and_the_rule_sets_the_stop(tmp_path, atrs):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = stop_at(atrs)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)

    result = run_once(pipeline)
    table = result.table

    # A stop the range does not allow is no reason to distrust the rest of the view, or the run.
    assert result.status == "ok" and table.warnings == []
    assert all(v.proposed_stop is None and "is not used" in v.data_warnings[0] for v in table.views)
    [order] = table.orders
    assert f"({config.SIZING_STOP_ATR_MULTIPLE:g} x ATR $" in order.reason and "analyst" not in order.reason
    text = result.markdown_path.read_text()
    assert f"UPA at {order.protective_stop:,.2f} (set by the sizing rule)." in text
    assert "| none | The proposed stop of" in text
    assert "Proposed stop for a new position" not in llm.messages(ProposalSetDraft)[0]


def test_signal_files_validate_against_the_schema_and_match_the_journal(tmp_path):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    result = run_once(pipeline)

    loaded = SignalTable.model_validate_json(result.json_path.read_text())
    assert loaded == result.table == pipeline.journal.signal_table(result.run_id)
    assert result.json_path.parent == tmp_path / "runs" / "2026-11-13" / result.run_id

    text = result.markdown_path.read_text()
    assert "# Signals for 2026-11-13" in text and "| UPA | buy | 0.70 | accepted |" in text
    assert "Paper trading: every order here is simulated" in text and "## Orders" in text
    assert "| UPA | buy |" in text and "Single position" in text and "Protective stops attached on fill: UPA at" in text
    assert "Equity 100,000.00 (cash 100,000.00, positions 0.00)" in text
    assert "20 model calls" in text and "no per-token cost" in text

    row = pipeline.journal.runs("fake/a")[0]
    assert row["status"] == "ok" and row["llm_calls"] == 20 and row["cost_usd"] is None
    assert row["snapshot_path"].startswith(str(tmp_path / "snapshots"))


def test_screen_rows_explain_every_ticker_in_the_universe(tmp_path):
    table = run_once(make_pipeline(tmp_path)).table
    reasons = {row.ticker: row.reason for row in table.screen}

    assert set(reasons) == {"UPA", "UPB", "UPC", "DOWN", "GONE"}
    assert "Not in an uptrend" in reasons["DOWN"] and "Data not usable" in reasons["GONE"]


# ── The paper account across days ──────────────────────────────────────────

def test_buys_fill_at_the_next_open_and_exits_are_proposed_and_filled(tmp_path):
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)

    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("UPB", "buy", 0.6))
    run_once(pipeline)

    clock.now = day(16)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "sell", 0.8), ("UPB", "trim", 0.5))
    second = run_once(pipeline).table

    assert [(f.ticker, f.side, f.kind, f.fill_date) for f in second.fills] == [
        ("UPA", "buy", "market", session(16)), ("UPB", "buy", "market", session(16)),
    ]
    assert [(h.ticker, h.entry_date, h.sessions_held) for h in second.holdings] == [
        ("UPA", session(16), 1), ("UPB", session(16), 1),
    ]
    assert all(h.stop_price is not None and h.stop_price < h.last_close for h in second.holdings)
    spent = sum(f.shares * f.price for f in second.fills)
    assert second.account.cash == pytest.approx(100_000 - spent, abs=0.01)
    assert second.account.equity == pytest.approx(100_000 + 0.0025 * spent, rel=1e-3)   # up 0.3%, less 0.05% slippage
    assert all(v.held for v in second.views if v.ticker in ("UPA", "UPB"))
    assert [(p.ticker, p.action, p.status) for p in second.proposals] == [
        ("UPA", "sell", "accepted"), ("UPB", "trim", "accepted"),
    ]
    briefing = llm.messages(ProposalSetDraft)[-1]
    assert f"CURRENT HOLDINGS:\n- UPA: {second.holdings[0].shares} shares bought 2026-11-16 at" in briefing
    assert "unrealized +0.25%" in briefing and "stop at" in briefing
    assert [(d.ticker, d.side, d.verdict) for d in second.risk] == [("UPA", "sell", "approved"), ("UPB", "sell", "approved")]

    clock.now = day(17)
    llm.replies[ProposalSetDraft] = proposals(("UPB", "hold", 0.6))
    third = run_once(pipeline).table

    assert [(f.ticker, f.side, f.kind) for f in third.fills] == [("UPA", "sell", "market"), ("UPB", "sell", "market")]
    assert third.fills[0].shares == second.holdings[0].shares                  # the whole position
    assert third.fills[0].pnl_pct == pytest.approx(0.2, abs=0.01)             # +0.3% less slippage both ways
    assert [h.ticker for h in third.holdings] == ["UPB"]
    assert third.holdings[0].shares == second.holdings[1].shares - second.holdings[1].shares // 2
    stops = [o for o in book(pipeline).orders() if o.kind == "stop"]
    assert [(o.ticker, o.shares) for o in stops] == [("UPB", third.holdings[0].shares)]   # UPA's stop went with it
    assert [(p.ticker, p.action, p.source) for p in third.proposals] == [("UPB", "hold", "portfolio_manager")]


def test_a_preset_holding_whose_view_has_turned_is_debated_and_can_be_sold(tmp_path):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = by_ticker({"DOWN": view("bearish", 0.7)}, view("neutral", 0.5))
    llm.replies[ProposalSetDraft] = proposals(("DOWN", "sell", 0.8))
    pipeline = make_pipeline(tmp_path, llm)
    seed_position(pipeline, "fake/a", "DOWN")

    table = run_once(pipeline).table

    assert "DOWN" not in table.candidates                      # it fails the screen but is still analysed
    assert [(v.ticker, v.held) for v in table.views][-1] == ("DOWN", True)
    assert [(d.ticker, d.trigger) for d in table.debates] == [("DOWN", "exit_signal")]
    assert [(p.ticker, p.action, p.status) for p in table.proposals] == [("DOWN", "sell", "accepted")]
    debate_prompt = llm.messages(DebateArgumentDraft)[0]
    assert "THE DESK'S POSITION\nDOWN: 10 shares bought 2026-11-10" in debate_prompt
    assert [(d.ticker, d.side, d.approved_shares) for d in table.risk] == [("DOWN", "sell", 10)]
    assert "keep holding DOWN" in llm.calls[[n for n, _, _ in llm.calls].index("DebateArgumentDraft")][1]


def test_a_holding_the_portfolio_manager_ignores_is_held_by_default(tmp_path):
    pipeline = make_pipeline(tmp_path)
    seed_position(pipeline, "fake/a", "UPC")

    table = run_once(pipeline).table

    assert [(p.ticker, p.action, p.source, p.status) for p in table.proposals] == [
        ("UPC", "hold", "default", "accepted"),
    ]
    assert waiting_orders(pipeline) == {} and table.risk == []


def test_a_holding_without_usable_data_gets_no_view_but_still_a_decision(tmp_path):
    pipeline = make_pipeline(tmp_path)
    seed_position(pipeline, "fake/a", "GONE")

    result = run_once(pipeline)
    table = result.table

    assert result.status == "degraded"                         # the desk could not look at something it holds
    assert "GONE" not in [v.ticker for v in table.views]
    assert any("GONE: no view today, its data is not usable" in w for w in table.warnings)
    assert table.holdings[0].last_close is None
    assert ("GONE", "hold", "default") in [(p.ticker, p.action, p.source) for p in table.proposals]


def test_a_buy_against_the_analyst_or_the_judge_is_rejected_by_code(tmp_path):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = by_ticker({"UPB": view("bearish", 0.8)}, view())
    llm.replies[DebateRulingDraft] = lambda system, user: (
        ruling("do_not_own", 0.7) if "open a new position in UPC" in system else ruling()
    )
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.6), ("UPB", "buy", 0.9), ("UPC", "buy", 0.9))
    table = run_once(make_pipeline(tmp_path, llm)).table

    outcome = {p.ticker: (p.status, p.note) for p in table.proposals}
    assert outcome["UPA"] == ("accepted", "")
    assert outcome["UPB"] == ("rejected", "The analyst's view of this ticker is bearish, not bullish.")
    assert outcome["UPC"] == ("rejected", "The judge ruled against owning it.")


def test_a_position_whose_split_data_disagrees_degrades_the_run_and_is_not_valued(tmp_path):
    clock, llm = Clock(day(13)), ScriptedLLM()
    market = SyntheticMarket(unlisted_splits={("UPA", session(17)): 2.0})
    pipeline = make_pipeline(tmp_path, llm, market=market, clock=clock)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    run_once(pipeline)
    clock.now = day(16)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "hold", 0.7))
    assert run_once(pipeline).status == "ok"

    clock.now = day(17)                                                  # prices halve with no split listed
    result = run_once(pipeline)

    assert result.status == "degraded"
    assert any("UPA: its listed splits and its restated prices do not agree" in w for w in result.table.warnings)
    held = result.table.holdings[0]
    assert held.ticker == "UPA" and held.last_close is None and held.unrealized_pct is None
    assert f"UPA: {held.shares} shares bought 2026-11-16 at" in llm.messages(ProposalSetDraft)[-1]
    assert "no usable price data today" in llm.messages(ProposalSetDraft)[-1]
    assert result.table.fills == []                               # its stop, far above the halved price, did not fire


def test_a_delisted_holding_is_closed_by_the_book_and_the_days_after_are_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PAPER_STALE_DAYS", 4)
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    seed_position(pipeline, "fake/a", "GONE", entry_session=12)

    assert run_once(pipeline).status == "degraded"               # held, cannot be analysed: first seen missing
    clock.now = day(18)                                           # five days later it is still missing
    result = run_once(pipeline)

    assert result.status == "degraded"
    assert [(f.ticker, f.kind, f.fill_date) for f in result.table.fills] == [("GONE", "close", session(12))]
    assert result.table.holdings == [] and "GONE" not in [p.ticker for p in result.table.proposals]
    closing = [e for e in pipeline.journal.events(result.run_id) if e.ticker == "GONE"]
    assert "Sell 10 GONE by the broker, at the last close it could be matched to on 2026-11-12" in closing[0].remark
    assert "GONE by the broker" in result.markdown_path.read_text()
    assert result.table.account.cash == pytest.approx(100_000 + 1_000)     # the seeded position came back as cash

    clock.now = day(19)
    assert run_once(pipeline).status == "ok"                      # nothing is left to complain about


def test_an_order_that_could_never_fill_is_dropped_out_loud(tmp_path, monkeypatch):
    from models.trading import OrderRequest

    monkeypatch.setattr(config, "PAPER_STALE_DAYS", 2)
    clock = Clock(day(18))
    pipeline = make_pipeline(tmp_path, clock=clock)
    book(pipeline).submit(OrderRequest(order_id="old", ticker="GONE", side="buy", shares=5), session(13))

    result = run_once(pipeline)

    assert result.status == "degraded"
    assert any("GONE: buy order from 2026-11-13 was cancelled" in w for w in result.table.warnings)
    dropped = [e for e in pipeline.journal.events(result.run_id) if e.kind == "book_problem"]
    assert len(dropped) == 1 and dropped[0].speaker == "trader" and dropped[0].stage == "settle"
    assert waiting_orders(pipeline) == {}


def test_fills_made_by_a_run_that_failed_later_appear_in_the_next_table(tmp_path):
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    run_once(pipeline)

    clock.now = day(16)
    llm.replies[TickerViewDraft] = lambda system, user: 1 / 0     # settles, then fails
    assert run_once(pipeline).status == "failed"
    assert len(book(pipeline).unreported_fills()) == 1

    llm.replies[TickerViewDraft] = view()
    table = run_once(pipeline).table
    assert [(f.ticker, f.side, f.fill_date) for f in table.fills] == [("UPA", "buy", session(16))]
    assert len(pipeline.journal.rows("SELECT 1 FROM paper_fills")) == 1       # filled once, reported once

    # A forced rerun replaces that run, and so reports the fill in its stead: once, not twice.
    forced = run_once(pipeline, force=True).table
    assert [(f.ticker, f.side, f.fill_date) for f in forced.fills] == [("UPA", "buy", session(16))]
    assert len(pipeline.journal.rows("SELECT 1 FROM paper_fills")) == 1
    clock.now = day(17)
    assert run_once(pipeline).table.fills == []


# ── The kill switch and the circuit breakers ─────────────────────────────────

def test_kill_switch_file_stops_every_order_but_not_the_analysis(tmp_path, monkeypatch):
    halt = tmp_path / "HALT"
    halt.write_text("stop")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(halt))
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("DOWN", "sell", 0.8))
    pipeline = make_pipeline(tmp_path, llm)
    seed_position(pipeline, "fake/a", "DOWN")

    result = run_once(pipeline)
    table = result.table

    assert result.status == "ok" and "kill switch file" in table.halted
    assert [p.status for p in table.proposals] == ["accepted", "accepted"]       # the signals are still recorded
    assert [(d.ticker, d.verdict) for d in table.risk] == [("DOWN", "rejected"), ("UPA", "rejected")]
    assert all("Trading is halted" in d.reasons[0] for d in table.risk)
    assert table.orders == [] and waiting_orders(pipeline) == {}
    assert "**No order was placed.** Trading is halted" in result.markdown_path.read_text()

    halt.unlink()                                                 # switch off: the same session can be run again
    again = run_once(pipeline, force=True).table
    assert again.halted == "" and waiting_orders(pipeline) == {"UPA": "buy", "DOWN": "sell"}


def test_kill_switch_by_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "HALT"))
    monkeypatch.setenv("TRADING_HALT", "1")
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    table = run_once(make_pipeline(tmp_path, llm)).table
    assert "TRADING_HALT" in table.halted and table.orders == []


def test_drawdown_breaker_stops_new_positions_but_lets_exits_through(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "HALT"))
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("DOWN", "sell", 0.8))
    pipeline = make_pipeline(tmp_path, llm)
    seed_position(pipeline, "fake/a", "DOWN")
    with pipeline.journal.transaction():                          # the account was once worth far more
        pipeline.journal.write("UPDATE paper_accounts SET high_water = 125000 WHERE account = 'fake/a'")

    table = run_once(pipeline).table

    assert table.account.drawdown_pct > 10
    verdicts = {d.ticker: d for d in table.risk}
    assert verdicts["DOWN"].verdict == "approved"
    assert verdicts["UPA"].verdict == "rejected" and "Drawdown breaker" in verdicts["UPA"].reasons[0]
    assert waiting_orders(pipeline) == {"DOWN": "sell"}


# ── Picking up an interrupted run ────────────────────────────────────────────

class Killed(BaseException):
    """Stands in for the process being killed: nothing in the pipeline catches it."""


def test_a_killed_run_is_taken_up_without_fetching_data_or_asking_any_model_twice(tmp_path):
    llm, market = ScriptedLLM(), SyntheticMarket()
    pipeline = make_pipeline(tmp_path, llm, market=market)

    def killed_at_the_last_call(system, user):
        raise Killed()

    llm.replies[ProposalSetDraft] = killed_at_the_last_call
    with pytest.raises(Killed):
        pipeline.run()
    assert pipeline.journal.runs("fake/a")[0]["status"] == "running"
    asked, fetched = len(llm.calls), len(market.requested)
    assert asked == 7 + 3 + 9 + 1                                 # the killed call was made, and lost

    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    result = run_once(pipeline)

    assert result.status == "ok"
    assert len(market.requested) == fetched                       # the saved snapshot was used
    assert len(llm.calls) == asked + 1                            # only the call that never returned
    assert result.table.usage.calls == 1
    assert len(result.table.views) == 3 and len(result.table.debates) == 3 and result.table.regime is not None
    statuses = [row["status"] for row in pipeline.journal.runs("fake/a")]
    assert statuses == ["ok", "failed"]                           # the killed run is on record as failed

    # The resumed run records the meeting again from the saved answers. The macro view was
    # already formed and saved by the killed run, so it is reused like any earlier one.
    kinds = [e.kind for e in pipeline.journal.events(result.run_id)]
    assert kinds.count("view") == 3 and kinds.count("argument") == 6 and kinds.count("ruling") == 3
    assert kinds[0] == "macro_view_reused" and result.table.regime_reused_from == session(13)


def test_a_run_killed_while_placing_orders_places_each_exactly_once(tmp_path, monkeypatch):
    import execution.placement as placement

    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("UPB", "buy", 0.6), ("UPC", "buy", 0.6))
    pipeline = make_pipeline(tmp_path, llm)
    real_submit, submitted = PaperBroker.submit, []

    def killed_after_the_second(self, request, when):
        if len(submitted) == 2:
            raise Killed()
        submitted.append(request.ticker)
        return real_submit(self, request, when)

    monkeypatch.setattr(PaperBroker, "submit", killed_after_the_second)
    with pytest.raises(Killed):
        pipeline.run()
    assert sorted(waiting_orders(pipeline)) == ["UPA", "UPB"]
    states = dict(pipeline.journal.rows("SELECT order_id, state FROM order_log"))
    assert sorted(states.values()) == ["confirmed", "confirmed", "intent"]

    monkeypatch.undo()
    result = run_once(pipeline)

    assert result.status == "ok" and sorted(waiting_orders(pipeline)) == ["UPA", "UPB", "UPC"]
    assert len(book(pipeline).orders()) == 3                      # three orders, not five
    assert all(state == "confirmed" for _, state in pipeline.journal.rows("SELECT order_id, state FROM order_log"))


def test_a_sync_problem_is_still_reported_when_the_run_is_taken_up_again(tmp_path, monkeypatch):
    from models.trading import OrderRequest

    monkeypatch.setattr(config, "PAPER_STALE_DAYS", 2)
    llm = ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=Clock(day(18)))
    book(pipeline).submit(OrderRequest(order_id="old", ticker="GONE", side="buy", shares=5), session(13))

    def killed(system, user):
        raise Killed()

    llm.replies[ProposalSetDraft] = killed
    with pytest.raises(Killed):
        pipeline.run()                                           # the sync has cancelled the stale order by now

    llm.replies[ProposalSetDraft] = proposals()
    result = run_once(pipeline)

    assert result.status == "degraded"                           # not "ok": the day did have a problem
    assert any("GONE: buy order from 2026-11-13 was cancelled" in w for w in result.table.warnings)
    assert [e.kind for e in pipeline.journal.events(result.run_id)][0] == "book_problem"


def test_a_forced_run_that_dies_does_not_leave_the_session_counted_as_done(tmp_path):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    first = run_once(pipeline)

    def killed(system, user):
        raise Killed()

    llm.replies[ProposalSetDraft] = killed
    with pytest.raises(Killed):
        pipeline.run(force=True)

    statuses = {row["run_id"]: row["status"] for row in pipeline.journal.runs("fake/a")}
    assert statuses[first.run_id] == "superseded"
    assert pipeline.journal.completed_run("fake/a", session(13)) is None
    assert waiting_orders(pipeline) == {"UPA": "buy"}            # the earlier order stands until a decision replaces it

    llm.replies[ProposalSetDraft] = proposals(("UPB", "buy", 0.7))
    result = run_once(pipeline)                                  # an ordinary run: not skipped, it takes the session up

    assert result.status == "ok" and waiting_orders(pipeline) == {"UPB": "buy"}
    events = pipeline.journal.events(result.run_id)
    withdrawn = [e for e in events if e.kind == "order_withdrawn"]
    assert len(withdrawn) == 1 and withdrawn[0].ticker == "UPA" and "Taken back: buy" in withdrawn[0].remark
    assert [row["order_id"] for row in pipeline.journal.rows("SELECT order_id FROM order_log")] == [
        result.table.orders[0].order_id,
    ]


def test_a_forced_run_that_decides_a_different_size_replaces_the_order(tmp_path, monkeypatch):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    first = run_once(pipeline).table.orders[0]

    monkeypatch.setattr(config, "RISK_MAX_POSITION_PCT", 5)      # the limit is halved, then the session is run again
    result = run_once(pipeline, force=True)

    [order] = book(pipeline).orders()
    assert order.order_id == first.order_id and order.shares == first.shares // 2
    replaced = [e for e in pipeline.journal.events(result.run_id) if e.kind == "order_withdrawn"]
    assert len(replaced) == 1 and replaced[0].remark.startswith(f"Replaced: buy {first.shares} UPA")
    assert pipeline.journal.rows("SELECT state FROM order_log")[0]["state"] == "confirmed"


def test_a_forced_run_killed_while_replacing_an_order_can_still_be_taken_up(tmp_path, monkeypatch):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    first = run_once(pipeline).table.orders[0]
    monkeypatch.setattr(config, "RISK_MAX_POSITION_PCT", 5)
    real_withdraw = PaperBroker.withdraw

    def withdraw_then_die(self, order_id):
        real_withdraw(self, order_id)
        raise Killed()

    monkeypatch.setattr(PaperBroker, "withdraw", withdraw_then_die)
    with pytest.raises(Killed):
        pipeline.run(force=True)
    monkeypatch.setattr(PaperBroker, "withdraw", real_withdraw)
    [still_there] = book(pipeline).orders()                      # the withdrawal was undone with everything else
    assert still_there.shares == first.shares

    result = run_once(pipeline)                                  # twice over, to be sure nothing is stuck
    assert result.status == "ok" and book(pipeline).orders()[0].shares == first.shares // 2
    assert run_once(pipeline).status == "skipped" and len(book(pipeline).orders()) == 1


def test_kill_switch_set_after_a_crash_takes_back_the_orders_already_placed(tmp_path, monkeypatch):
    halt = tmp_path / "HALT"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(halt))
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("UPB", "buy", 0.6), ("UPC", "buy", 0.6))
    pipeline = make_pipeline(tmp_path, llm)
    real_submit, submitted = PaperBroker.submit, []

    def killed_after_the_second(self, request, when):
        if len(submitted) == 2:
            raise Killed()
        submitted.append(request.ticker)
        return real_submit(self, request, when)

    monkeypatch.setattr(PaperBroker, "submit", killed_after_the_second)
    with pytest.raises(Killed):
        pipeline.run()
    monkeypatch.setattr(PaperBroker, "submit", real_submit)
    assert len(waiting_orders(pipeline)) == 2

    halt.write_text("stop")                                      # the user pulls the switch, then runs again
    result = run_once(pipeline)

    assert result.status == "ok" and "kill switch file" in result.table.halted and result.table.orders == []
    assert waiting_orders(pipeline) == {}                        # nothing is left to fill tomorrow
    assert pipeline.journal.rows("SELECT 1 FROM order_log") == []
    kinds = [e.kind for e in pipeline.journal.events(result.run_id)]
    assert kinds.count("order_withdrawn") == 2 and "order_placed" not in kinds


def test_a_saved_answer_is_not_reused_for_a_question_that_has_changed(tmp_path, monkeypatch):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = by_ticker({"UPB": LLMError("timed out")}, view())
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    real_submit = PaperBroker.submit

    def killed(self, request, when):
        raise Killed()

    monkeypatch.setattr(PaperBroker, "submit", killed)           # dies after the portfolio manager has answered
    with pytest.raises(Killed):
        pipeline.run()
    monkeypatch.setattr(PaperBroker, "submit", real_submit)
    asked = llm.count(ProposalSetDraft)

    llm.replies[TickerViewDraft] = view()                        # this time the analyst reaches UPB
    llm.replies[ProposalSetDraft] = proposals(("UPB", "buy", 0.7))
    result = run_once(pipeline)

    # The manager now sees a view it did not have before, so its earlier answer does not apply.
    assert llm.count(ProposalSetDraft) == asked + 1
    assert [v.ticker for v in result.table.views] == ["UPA", "UPB", "UPC"]
    assert waiting_orders(pipeline) == {"UPB": "buy"}
    assert llm.count(TickerViewDraft) == 3 + 1                   # UPA's and UPC's views were kept, UPB's asked again


def test_a_saved_answer_that_no_longer_fits_what_is_asked_for_is_asked_again(tmp_path, monkeypatch):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    real_submit = PaperBroker.submit

    def killed(self, request, when):
        raise Killed()

    monkeypatch.setattr(PaperBroker, "submit", killed)
    with pytest.raises(Killed):
        pipeline.run()
    monkeypatch.setattr(PaperBroker, "submit", real_submit)
    calls = len(llm.calls)

    # As if the run had been killed by a version whose analyst was not yet asked for a stop.
    with pipeline.journal.transaction():
        rows = pipeline.journal.rows("SELECT key, value_json FROM checkpoints WHERE key LIKE 'view:%'")
        assert len(rows) == 3
        for row in rows:
            old = re.sub(r'"stop_price":[^,]*,', "", row["value_json"])
            assert old != row["value_json"]
            pipeline.journal.write("UPDATE checkpoints SET value_json = ? WHERE key = ?", (old, row["key"]))

    result = run_once(pipeline)

    assert result.status == "ok" and waiting_orders(pipeline) == {"UPA": "buy"}
    assert len(llm.calls) == calls + 3                           # the three views, and nothing else


def test_force_starts_a_session_over_and_asks_every_model_again(tmp_path):
    llm, market = ScriptedLLM(), SyntheticMarket()
    pipeline = make_pipeline(tmp_path, llm, market=market)
    run_once(pipeline)
    asked, fetched = len(llm.calls), len(market.requested)

    run_once(pipeline, force=True)

    assert len(market.requested) == fetched + 2                   # screened and collected again
    assert len(llm.calls) == asked + 3 + 9 + 1                    # everything but the macro view, which is reused


# ── Running more than once ───────────────────────────────────────────────────

def test_a_session_that_already_has_signals_is_skipped(tmp_path):
    llm = ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm)
    first = run_once(pipeline)
    calls = len(llm.calls)

    again = run_once(pipeline)

    assert again.status == "skipped" and again.run_id == first.run_id and len(llm.calls) == calls


def test_weekend_runs_find_nothing_new_to_do(tmp_path):
    clock = Clock(day(13))
    pipeline = make_pipeline(tmp_path, clock=clock)
    run_once(pipeline)

    clock.now = day(14)          # Saturday
    assert run_once(pipeline).status == "skipped"
    clock.now = day(15)          # Sunday
    assert run_once(pipeline).status == "skipped"


def test_forcing_a_rerun_replaces_that_sessions_orders_and_fills_nothing_twice(tmp_path):
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    first = run_once(pipeline)

    llm.replies[ProposalSetDraft] = proposals(("UPB", "buy", 0.7))
    second = run_once(pipeline, force=True)

    assert second.status == "ok" and second.run_id != first.run_id
    assert second.table.regime_reused_from == session(13)        # the macro team is not asked again
    assert waiting_orders(pipeline) == {"UPB": "buy"}                      # UPA's order was taken back

    clock.now = day(16)
    table = run_once(pipeline).table
    assert [(f.ticker, f.side) for f in table.fills] == [("UPB", "buy")]
    assert len(pipeline.journal.rows("SELECT 1 FROM paper_fills")) == 1


# ── The macro view over time ─────────────────────────────────────────────────

def test_macro_view_is_reused_for_a_week_then_formed_again_when_it_is_set_to_last_a_week(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_MACRO_MAX_AGE_DAYS", 7)
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)

    assert run_once(pipeline).table.regime_reused_from is None
    assert llm.count(DomainAssessmentDraft) == 7

    clock.now = day(19)          # six days later
    table = run_once(pipeline).table
    assert table.regime_reused_from == session(13) and table.regime.session_date == session(13)
    assert llm.count(DomainAssessmentDraft) == 7

    clock.now = day(20)          # seven days later
    table = run_once(pipeline).table
    assert table.regime_reused_from is None and table.regime.session_date == session(20)
    assert llm.count(DomainAssessmentDraft) == 14


def test_refresh_macro_forms_a_new_view_at_once(tmp_path):
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    run_once(pipeline)

    clock.now = day(16)
    llm.replies[DomainAssessmentDraft] = assessment("restrictive", 0.8)
    table = run_once(pipeline, refresh_macro=True).table

    assert table.regime.risk_appetite == "risk_off" and table.regime.session_date == session(16)


def test_macro_agents_read_the_current_state_not_a_scenario(tmp_path):
    llm = ScriptedLLM()
    run_once(make_pipeline(tmp_path, llm))
    name, system, user = next(call for call in llm.calls if call[0] == "DomainAssessmentDraft")

    assert "There is no scenario today" in system and "Central Bank Agent" in system
    assert "DATA SNAPSHOT (last completed US session: 2026-11-13" in user
    assert "POLICY SCENARIO" not in user


# ── Debate selection ─────────────────────────────────────────────────────────

def test_only_the_strongest_bullish_candidates_are_debated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_DEBATE_TOP", 2)
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = by_ticker(
        {"UPA": view("bullish", 0.55), "UPB": view("neutral", 0.9), "UPC": view("bullish", 0.8)}, view(),
    )
    table = run_once(make_pipeline(tmp_path, llm)).table

    assert [d.ticker for d in table.debates] == ["UPC", "UPA"]          # by confidence; the neutral view is left out


def test_second_round_shows_each_side_the_other_sides_argument(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_DEBATE_TOP", 1)
    monkeypatch.setattr(config, "PIPELINE_DEBATE_ROUNDS", 2)
    llm = ScriptedLLM()
    table = run_once(make_pipeline(tmp_path, llm)).table

    assert [(t.side, t.round) for t in table.debates[0].turns] == [("bull", 1), ("bear", 1), ("bull", 2), ("bear", 2)]
    prompts = llm.messages(DebateArgumentDraft)
    assert "THE DEBATE SO FAR" not in prompts[0]
    assert "[BULL, round 1]" in prompts[1] and "[BEAR, round 1]" in prompts[2]
    assert prompts[3].count("round 1]") == 2 and "[BULL, round 2]" in prompts[3]


def test_untraceable_figures_in_an_argument_are_shown_to_the_judge(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_DEBATE_TOP", 1)
    llm = ScriptedLLM()
    llm.replies[DebateArgumentDraft] = argument("Analysts see the stock at 987.65 by spring.")
    table = run_once(make_pipeline(tmp_path, llm)).table

    assert "987.65" in table.debates[0].turns[0].data_warnings[0]
    assert "Automatic check of the bull's figures" in llm.messages(DebateRulingDraft)[0]


# ── When things go wrong ─────────────────────────────────────────────────────

def test_one_failed_analyst_call_degrades_the_run_and_blocks_only_that_buy(tmp_path):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = by_ticker({"UPB": LLMError("model timed out")}, view())
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("UPB", "buy", 0.7))
    result = run_once(make_pipeline(tmp_path, llm))
    table = result.table

    assert result.status == "degraded"
    assert [v.ticker for v in table.views] == ["UPA", "UPC"]
    assert any("UPB: the analyst call failed. model timed out" in w for w in table.warnings)
    statuses = {p.ticker: (p.status, p.note) for p in table.proposals}
    assert statuses["UPA"][0] == "accepted"
    assert statuses["UPB"] == ("rejected", "No checked analyst view of this ticker today.")
    assert "NO VIEW TODAY (data unusable or the analysis failed): UPB" in llm.messages(ProposalSetDraft)[0]


def test_an_out_of_range_view_is_refused_and_recorded(tmp_path):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = by_ticker({"UPA": view(confidence=85)}, view())
    pipeline = make_pipeline(tmp_path, llm)
    result = run_once(pipeline)

    assert result.status == "degraded" and "UPA" not in [v.ticker for v in result.table.views]
    refused = [e for e in pipeline.journal.events(result.run_id) if e.kind == "output_refused"]
    assert len(refused) == 1 and refused[0].speaker == "risk_officer" and refused[0].addressed_to == "ticker_analyst"
    assert refused[0].ticker == "UPA" and "confidence 85.0 is outside 0 to 1" in refused[0].remark


def test_too_few_macro_assessments_means_no_macro_view_and_no_buys(tmp_path):
    llm = ScriptedLLM()
    failing = {name: LLMError("down") for name in
               ("Central Bank Agent", "Fiscal Policy Agent", "Equity Market Agent", "Fixed Income Agent")}
    llm.replies[DomainAssessmentDraft] = by_agent(failing, assessment())
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.9), ("DOWN", "sell", 0.9))
    pipeline = make_pipeline(tmp_path, llm)
    seed_position(pipeline, "fake/a", "DOWN")

    result = run_once(pipeline)
    table = result.table

    assert result.status == "degraded" and table.regime is None
    assert any("Only 3 of 7 macro assessments were usable" in w for w in table.warnings)
    statuses = {p.ticker: (p.status, p.note) for p in table.proposals}
    assert statuses["UPA"] == ("rejected", "No current macro view, so no new positions.")
    assert statuses["DOWN"][0] == "accepted"                    # exits do not need a macro view
    assert "MACRO VIEW: not available today." in llm.messages(TickerViewDraft)[0]
    assert pipeline.journal.latest_regime("fake/a", session(13)) is None


def test_portfolio_manager_failure_holds_every_position(tmp_path):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = LLMError("no reply")
    pipeline = make_pipeline(tmp_path, llm)
    seed_position(pipeline, "fake/a", "UPA")

    result = run_once(pipeline)

    assert result.status == "degraded" and result.table.pm_summary == ""
    assert [(p.ticker, p.action, p.source) for p in result.table.proposals] == [("UPA", "hold", "default")]


def test_price_source_outage_fails_the_run_before_any_model_call(tmp_path):
    llm = ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, market=SyntheticMarket(fail=True))

    result = run_once(pipeline)

    assert result.status == "failed" and "The price source failed" in result.message
    assert llm.calls == [] and result.table is None
    row = pipeline.journal.runs("fake/a")[0]
    assert row["status"] == "failed" and "scripted outage" in row["error"]
    assert pipeline.journal.completed_run("fake/a", session(13)) is None


class LateMarket(SyntheticMarket):
    """A source that has not published the latest session's bar yet."""

    late = True

    def daily_bars(self, symbols, start, end):
        frames = super().daily_bars(symbols, start, end)
        return {symbol: frame.iloc[:-1] for symbol, frame in frames.items()} if self.late else frames


def test_prices_not_published_yet_fail_the_run_instead_of_recording_an_empty_day(tmp_path):
    llm, market = ScriptedLLM(), LateMarket()
    pipeline = make_pipeline(tmp_path, llm, market=market)

    result = run_once(pipeline)

    assert result.status == "failed" and llm.calls == []
    assert "Only 0 of 5 tickers have usable prices for 2026-11-13" in result.message
    assert "last bar is 2026-11-12" in result.message and "run again later" in result.message

    market.late = False                       # once the bar is published the same session runs normally
    assert run_once(pipeline).status == "ok"


def test_a_few_unusable_tickers_do_not_stop_the_run(tmp_path):
    pipeline = make_pipeline(tmp_path, market=SyntheticMarket(missing={"GONE", "DOWN"}))
    assert run_once(pipeline).status == "ok"                    # 3 of 5 usable


def test_a_failed_run_can_be_run_again_without_force(tmp_path):
    clock, market = Clock(day(13)), SyntheticMarket(fail=True)
    pipeline = make_pipeline(tmp_path, market=market, clock=clock)
    assert run_once(pipeline).status == "failed"

    market.fail = False
    assert run_once(pipeline).status == "ok"
    assert [row["status"] for row in pipeline.journal.runs("fake/a")] == ["ok", "failed"]


def test_an_unavailable_model_fails_the_arm_without_fetching_data(tmp_path):
    market = SyntheticMarket()
    llm = ScriptedLLM(problems=["Ollama is not reachable at http://localhost:11434."])
    result = run_once(make_pipeline(tmp_path, llm, market=market))

    assert result.status == "failed" and "The model is not available: Ollama is not reachable" in result.message
    assert market.requested == []


def test_a_run_during_the_next_sessions_trading_hours_is_refused(tmp_path):
    llm, market = ScriptedLLM(), SyntheticMarket()
    monday_11am = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)
    pipeline = make_pipeline(tmp_path, llm, market=market, clock=Clock(monday_11am))

    result = run_once(pipeline)

    assert result.status == "failed" and "The session after 2026-11-13 (2026-11-16) is already trading" in result.message
    assert llm.calls == [] and market.requested == []
    assert pipeline.journal.runs("fake/a")[0]["status"] == "failed"


def utc(year, month, day_, hour, minute):
    return datetime(year, month, day_, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, outcome, run_session", [
    # Tuesday after Labor Day: the last completed session is Friday the 4th until Tuesday's close.
    (utc(2026, 9, 7, 15, 0), "ok", "2026-09-04"),          # Labor Day itself, market closed
    (utc(2026, 9, 8, 13, 29), "ok", "2026-09-04"),         # 09:29 in New York
    (utc(2026, 9, 8, 13, 30), "failed", "2026-09-04"),     # 09:30: the open
    (utc(2026, 9, 8, 20, 30), "ok", "2026-09-08"),         # after Tuesday's close
    # The day after Thanksgiving closes at 13:00 New York time.
    (utc(2026, 11, 27, 17, 0), "failed", "2026-11-25"),    # noon: Friday is trading
    (utc(2026, 11, 27, 18, 30), "ok", "2026-11-27"),       # 13:30: Friday's short session is complete
    (utc(2026, 11, 28, 12, 0), "ok", "2026-11-27"),        # Saturday
])
def test_the_too_late_boundary_follows_the_exchange_calendar(tmp_path, now, outcome, run_session):
    pipeline = make_pipeline(tmp_path, clock=Clock(now))
    result = run_once(pipeline)

    assert result.status == outcome
    assert pipeline.journal.runs("fake/a")[0]["session_date"] == run_session


def test_the_open_arriving_while_data_is_collected_fails_the_run(tmp_path):
    clock, llm = Clock(utc(2026, 11, 16, 14, 28)), ScriptedLLM()        # two minutes before Monday's open

    class SlowMarket(SyntheticMarket):
        def fundamentals(self, symbol):
            clock.now = utc(2026, 11, 16, 14, 31)                       # the lookups run past the open
            return super().fundamentals(symbol)

    result = run_once(make_pipeline(tmp_path, llm, market=SlowMarket(), clock=clock))

    assert result.status == "failed" and llm.calls == []
    assert "The data was still being collected when it opened" in result.message


def test_the_open_arriving_while_models_work_fails_the_run_and_queues_nothing(tmp_path):
    clock, llm = Clock(utc(2026, 11, 16, 14, 0)), ScriptedLLM()

    def slow_manager(system, user):
        clock.now = utc(2026, 11, 16, 14, 30)                           # the last call finishes at the open
        return proposals(("UPA", "buy", 0.7))

    llm.replies[ProposalSetDraft] = slow_manager
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    result = run_once(pipeline)

    assert result.status == "failed" and "TooLate" in result.message and "already trading" in result.message
    assert waiting_orders(pipeline) == {}
    assert pipeline.journal.completed_run("fake/a", session(13)) is None


def dead_pid():
    """The id of a process that has already ended."""
    import subprocess, sys
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    return finished.pid


def test_a_run_that_is_still_going_in_another_process_is_not_started_twice(tmp_path):
    import os

    llm = ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm)
    pipeline.journal.start_run("other-process", "fake/a", session(13), day(13), pid=os.getppid())

    waiting = run_once(pipeline)
    assert waiting.status == "blocked" and waiting.run_id == "other-process" and llm.calls == []
    assert "is still going" in waiting.message and "stop that process" in waiting.message

    forced = run_once(pipeline, force=True)                              # two runs placing orders cannot both be right
    assert forced.status == "blocked" and llm.calls == []
    assert pipeline.journal.runs("fake/a")[0]["status"] == "running"     # and the live run is left alone


def test_a_run_whose_process_has_died_is_marked_failed_and_taken_up(tmp_path):
    pipeline = make_pipeline(tmp_path)
    pipeline.journal.start_run("crashed", "fake/a", session(13), day(13), pid=dead_pid())

    assert run_once(pipeline).status == "ok"
    rows = {row["run_id"]: row for row in pipeline.journal.runs("fake/a")}
    assert rows["crashed"]["status"] == "failed" and "ended before the run finished" in rows["crashed"]["error"]


def test_a_run_before_the_next_open_is_allowed_and_a_finished_session_is_still_skipped(tmp_path):
    clock = Clock(datetime(2026, 11, 16, 14, 29, tzinfo=timezone.utc))      # one minute before Monday's open
    pipeline = make_pipeline(tmp_path, clock=clock)
    first = run_once(pipeline)
    assert first.status == "ok" and first.table.session_date == session(13)

    clock.now = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)          # now the market is open
    assert run_once(pipeline).status == "skipped"                           # nothing new is attempted


def test_an_unexpected_error_while_collecting_data_is_recorded(tmp_path):
    llm = ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm)
    pipeline.snapshot_dir = tmp_path / "runs"
    (tmp_path / "runs").write_text("a file where the snapshot folder should be")

    result = run_once(pipeline)

    assert result.status == "failed" and "Error" in result.message and llm.calls == []
    assert pipeline.journal.runs("fake/a")[0]["status"] == "failed"


def test_if_the_run_cannot_be_recorded_the_next_one_finds_its_orders_and_places_none_twice(tmp_path, monkeypatch):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    real_finish, attempts = pipeline.journal.finish_run, []

    def finish_fails_once(run_id, status, *args, **kwargs):
        attempts.append(status)
        if len(attempts) == 1:
            raise RuntimeError("database is locked")
        return real_finish(run_id, status, *args, **kwargs)

    monkeypatch.setattr(pipeline.journal, "finish_run", finish_fails_once)
    result = run_once(pipeline)

    assert attempts == ["ok", "failed"]
    assert result.status == "failed" and "database is locked" in result.message
    assert pipeline.journal.completed_run("fake/a", session(13)) is None
    placed = book(pipeline).orders()
    assert [(o.ticker, o.side) for o in placed] == [("UPA", "buy")]     # the order went out before the failure

    calls = len(llm.calls)
    again = run_once(pipeline)                                           # no force: it picks up where it stopped

    assert again.status == "ok" and len(llm.calls) == calls              # no model was asked again
    assert book(pipeline).orders() == placed                             # and the order was not placed twice
    assert [o.order_id for o in again.table.orders] == [placed[0].order_id]


def test_data_for_a_different_session_than_the_run_is_refused(tmp_path):
    pipeline = make_pipeline(tmp_path, clock=Clock(day(13)))
    pipeline._now = Clock(day(16))                # the pipeline's clock has moved past a close the data has not

    result = run_once(pipeline)
    assert result.status == "failed" and "A session closed while the run was starting" in result.message


def test_cost_limit_stops_the_run_and_leaves_no_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_MAX_COST_USD", 2.0)
    llm = ScriptedLLM(cost_per_call=0.5)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)

    result = run_once(pipeline)

    assert result.status == "failed" and "BudgetExceeded" in result.message and "limit of $2.00" in result.message
    assert len(llm.calls) == 4                                  # the fifth call is never made
    row = pipeline.journal.runs("fake/a")[0]
    assert row["cost_usd"] == 2.0 and row["llm_calls"] == 4
    assert waiting_orders(pipeline) == {}


def test_priced_runs_record_their_cost(tmp_path):
    llm = ScriptedLLM(cost_per_call=0.01)
    table = run_once(make_pipeline(tmp_path, llm)).table
    assert table.usage.cost_usd == pytest.approx(0.20) and table.usage.input_tokens == 2000


def test_an_unexpected_error_in_one_arm_does_not_stop_the_other(tmp_path):
    broken, healthy = ScriptedLLM(), ScriptedLLM()
    broken.replies[TickerViewDraft] = lambda system, user: 1 / 0
    pipeline = make_pipeline(tmp_path, arms=[Arm("fake/broken", broken), Arm("fake/healthy", healthy)])

    results = {result.arm: result for result in pipeline.run()}

    assert results["fake/broken"].status == "failed" and "ZeroDivisionError" in results["fake/broken"].message
    assert results["fake/healthy"].status == "ok"


# ── Comparing model configurations ───────────────────────────────────────────

def test_two_arms_read_the_same_data_and_keep_separate_signals_and_books(tmp_path):
    clock, market = Clock(day(13)), SyntheticMarket()
    first, second = ScriptedLLM(), ScriptedLLM()
    first.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    second.replies[ProposalSetDraft] = proposals(("UPB", "buy", 0.7))
    second.replies[DomainAssessmentDraft] = assessment("restrictive", 0.9)
    pipeline = make_pipeline(tmp_path, arms=[Arm("fake/a", first), Arm("fake/b", second)], market=market, clock=clock)

    tables = {result.arm: result.table for result in pipeline.run()}

    assert len(market.requested) == 2                           # one screen and one full snapshot, not one per arm
    assert tables["fake/a"].snapshot_id == tables["fake/b"].snapshot_id
    assert tables["fake/a"].regime.risk_appetite == "risk_on" and tables["fake/b"].regime.risk_appetite == "risk_off"
    assert tables["fake/a"].run_id != tables["fake/b"].run_id

    clock.now = day(16)
    tables = {result.arm: result.table for result in pipeline.run()}
    assert [h.ticker for h in tables["fake/a"].holdings] == ["UPA"]
    assert [h.ticker for h in tables["fake/b"].holdings] == ["UPB"]
    assert pipeline.journal.arms() == ["fake/a", "fake/b"]


def test_a_holding_of_one_arm_is_fetched_but_not_analysed_by_the_other(tmp_path):
    first, second = ScriptedLLM(), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, arms=[Arm("fake/a", first), Arm("fake/b", second)])
    seed_position(pipeline, "fake/a", "DOWN")

    tables = {result.arm: result.table for result in pipeline.run()}

    assert "DOWN" in [v.ticker for v in tables["fake/a"].views]
    assert "DOWN" not in [v.ticker for v in tables["fake/b"].views]


# ── The record of who said what ──────────────────────────────────────────────

def test_every_utterance_is_recorded_in_order_with_speaker_and_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_DEBATE_TOP", 1)
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("TSLA", "buy", 0.9))
    first = run_once(pipeline)

    events = pipeline.journal.events(first.run_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [e.stage for e in events] == (
        ["macro"] * 8 + ["views"] * 3 + ["debate"] * 3 + ["proposals"] + ["checks"] * 2 + ["risk"] + ["orders"]
    )
    spoken = [(e.speaker, e.addressed_to, e.kind, e.is_llm) for e in events]
    assert spoken[0] == ("central_bank", "all", "assessment", True)
    assert spoken[7] == ("pipeline", "all", "macro_view", False)
    assert spoken[8] == ("ticker_analyst", "portfolio_manager", "view", True)
    assert spoken[11:14] == [
        ("bull", "bear", "argument", True), ("bear", "bull", "argument", True),
        ("judge", "portfolio_manager", "ruling", True),
    ]
    assert spoken[14] == ("portfolio_manager", "risk_officer", "proposals", True)
    assert spoken[15:] == [
        ("risk_officer", "trader", "accepted", False), ("risk_officer", "portfolio_manager", "rejected", False),
        ("risk_officer", "trader", "risk_reduced", False), ("trader", "all", "order_placed", False),
    ]
    assert events[17].remark.startswith("Reduced: buy UPA,") and "Single position" in events[17].remark
    assert events[18].remark.startswith("Placed: buy ") and "UPA at the next open, stop at" in events[18].remark
    # What a model said is kept as it said it; what code did is stated plainly.
    assert events[8].remark == "I would own this one." and events[8].ticker == "UPA"
    assert events[8].payload["direction"] == "bullish"
    assert events[16].remark == "Buy TSLA is rejected. Not one of today's candidates or holdings."
    assert all(e.ts == day(13) for e in events)

    clock.now = day(16)
    second = run_once(pipeline)
    opening = pipeline.journal.events(second.run_id)[:2]
    assert (opening[0].stage, opening[0].speaker, opening[0].kind, opening[0].ticker) == ("settle", "trader", "fill", "UPA")
    assert opening[0].remark.startswith("Buy ") and "UPA at the open on 2026-11-16" in opening[0].remark
    assert (opening[1].stage, opening[1].kind) == ("macro", "assessment"), "the macro team is asked again each session"


def test_the_macro_view_is_formed_anew_every_session_unless_it_is_told_to_last_longer(tmp_path, monkeypatch):
    llm, clock = ScriptedLLM(), Clock(day(13))
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    run_once(pipeline)
    asked = llm.count(DomainAssessmentDraft)

    clock.now = day(16)
    monday = run_once(pipeline)
    kinds = [event.kind for event in pipeline.journal.events(monday.run_id)]
    assert "macro_view" in kinds and "macro_view_reused" not in kinds
    assert monday.table.regime_reused_from is None and monday.table.regime.session_date == session(16)
    assert llm.count(DomainAssessmentDraft) == 2 * asked, "every macro analyst was asked again on Monday"

    # The setting still does what it says: with a week, Monday's view is still the one in use on Tuesday.
    monkeypatch.setattr(config, "PIPELINE_MACRO_MAX_AGE_DAYS", 7)
    clock.now = day(17)
    tuesday = run_once(pipeline)
    kinds = [event.kind for event in pipeline.journal.events(tuesday.run_id)]
    assert "macro_view_reused" in kinds and "macro_view" not in kinds
    assert tuesday.table.regime_reused_from == session(16)
    assert llm.count(DomainAssessmentDraft) == 2 * asked


def test_the_setting_as_shipped_asks_for_a_new_macro_view_every_session():
    # Read from the file, not from this process: a .env beside it may say otherwise for one machine.
    source = open(config.__file__, encoding="utf-8").read()
    assert 'os.getenv("PIPELINE_MACRO_MAX_AGE_DAYS", "1")' in source


def left_out_holding(tmp_path, stance):
    """Two sessions. UPA is bought, then the analyst turns bearish on it, the judge rules, and the manager's
    answer on the second day says nothing about UPA at all."""
    llm, clock = ScriptedLLM(), Clock(day(13))
    pipeline = make_pipeline(tmp_path, llm, clock=clock)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("UPB", "buy", 0.6))
    run_once(pipeline)
    clock.now = day(16)
    llm.replies[TickerViewDraft] = by_ticker({"UPA": view(direction="bearish", confidence=0.7)}, view())
    llm.replies[DebateRulingDraft] = lambda system, user: ruling(stance if "UPA [held]:" in user else "own")
    llm.replies[ProposalSetDraft] = proposals(("UPB", "hold", 0.5))
    return pipeline, run_once(pipeline)


def test_a_holding_the_manager_leaves_out_is_sold_when_the_analyst_and_the_judge_both_say_exit(tmp_path):
    pipeline, result = left_out_holding(tmp_path, "do_not_own")
    held = result.table.holdings
    shares = next(p.shares for p in held if p.ticker == "UPA")
    about_upa = [e for e in pipeline.journal.events(result.run_id) if e.ticker == "UPA" and e.stage in ("checks", "risk", "orders")]
    assert [(e.stage, e.kind) for e in about_upa] == [
        ("checks", "accepted"), ("risk", "risk_approved"), ("orders", "order_placed")]
    assert about_upa[0].remark == (
        "UPA is sold by default: the portfolio manager gave no decision for it, the analyst's view is bearish and "
        "the judge ruled against owning it."
    )
    assert (about_upa[0].payload["action"], about_upa[0].payload["source"]) == ("sell", "default")
    assert about_upa[2].remark == f"Placed: sell {shares} UPA at the next open."
    assert waiting_orders(pipeline) == {"UPA": "sell"}
    [filled_in] = [p for p in result.table.proposals if p.ticker == "UPA"]
    assert (filled_in.action, filled_in.source, filled_in.status) == ("sell", "default", "accepted")
    # It is an exit, but nobody proposed it: the test of "an exit proposed" still waits for the manager's own.
    assert progress(pipeline.journal, "fake/a")[:2] == (2, False)


def test_a_holding_the_manager_leaves_out_is_kept_when_the_judge_sides_with_owning_it(tmp_path):
    pipeline, result = left_out_holding(tmp_path, "own")
    [check] = [e for e in pipeline.journal.events(result.run_id) if e.ticker == "UPA" and e.stage == "checks"]
    assert check.remark == "UPA is held by default: the portfolio manager gave no valid decision for it."
    assert waiting_orders(pipeline) == {}


def test_events_are_passed_to_a_listener_as_they_happen(tmp_path):
    heard = []
    pipeline = make_pipeline(tmp_path)
    pipeline._on_event = heard.append
    result = run_once(pipeline)

    assert [e.seq for e in heard] == [e.seq for e in pipeline.journal.events(result.run_id)]


# ── Progress towards the stage's exit test ───────────────────────────────────

def run_sessions(pipeline, clock, llm, days, exits_on=None):
    for number in days:
        clock.now = day(number)
        llm.replies[ProposalSetDraft] = (
            proposals(("UPA", "sell", 0.8)) if number == exits_on else proposals(("UPA", "buy", 0.7))
        )
        run_once(pipeline)


def test_progress_counts_consecutive_clean_sessions_and_notices_an_exit(tmp_path):
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)

    run_sessions(pipeline, clock, llm, [13, 16, 17, 18])
    assert progress(pipeline.journal, "fake/a")[:2] == (4, False)

    run_sessions(pipeline, clock, llm, [19], exits_on=19)
    streak, exits, history = progress(pipeline.journal, "fake/a")
    assert (streak, exits) == (5, True)
    assert history[0] == (session(19), "ok") and len(history) == 5
    text = "\n".join(status_lines(pipeline.journal))
    assert "Sessions in a row without error: 5 of 5" in text and "A sell or trim accepted in that stretch: yes" in text


def test_a_missed_session_or_a_degraded_one_breaks_the_streak(tmp_path):
    clock, llm = Clock(day(13)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, clock=clock)

    run_sessions(pipeline, clock, llm, [13, 16, 18])            # the 17th was missed
    assert progress(pipeline.journal, "fake/a")[0] == 1

    clock.now = day(19)
    llm.replies[TickerViewDraft] = LLMError("down")
    assert run_once(pipeline).status == "degraded"
    assert progress(pipeline.journal, "fake/a")[0] == 0


def test_status_with_no_runs():
    assert status_lines(Journal(":memory:")) == ["No runs recorded yet."]


# ── Command line ─────────────────────────────────────────────────────────────

@pytest.fixture
def offline_cli(tmp_path, monkeypatch):
    """Point the command line at temporary storage, synthetic prices and a scripted model."""
    llm, clock = ScriptedLLM(), Clock(day(13))
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    monkeypatch.setattr(config, "JOURNAL_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setattr(config, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(config, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(config, "UNIVERSE_FILE", "")
    monkeypatch.setattr(daily, "default_arm", lambda: Arm("fake/cli", llm))

    def offline_pipeline(arms, **options):
        return make_pipeline(tmp_path, arms=arms, clock=clock)

    monkeypatch.setattr(daily, "DailyPipeline", offline_pipeline)
    return llm


def test_command_line_runs_and_reports_where_the_signals_are(offline_cli, tmp_path, capsys):
    assert daily.main(["--quiet"]) == 0
    out = capsys.readouterr().out

    assert "Paper trading: simulated orders only. Arms: fake/cli" in out
    assert "fake/cli: ok" in out and "3 views, 3 debates, 1 accepted proposal(s), 1 order(s) placed" in out
    assert "paper account: equity 100,000.00, cash 100,000.00" in out
    assert "signals.md" in out and "no per-token cost" in out
    assert len(list((tmp_path / "runs").rglob("signals.json"))) == 1

    assert daily.main(["--status"]) == 0
    assert "Sessions in a row without error: 1 of 5" in capsys.readouterr().out


def test_an_event_is_printed_on_one_indented_line_whatever_the_model_wrote(capsys):
    from models.signals import Event
    daily._print_event(Event(
        seq=1, ts=datetime(2026, 11, 13, tzinfo=timezone.utc), stage="views", speaker="ticker_analyst",
        speaker_name="Ticker Analyst", is_llm=True, addressed_to="portfolio_manager", kind="view", ticker="UPA",
        remark="Looks fine.\nfake/a: failed. Not a result line.\n\n  Still the same remark.",
    ))
    assert capsys.readouterr().out == (
        "  views     Ticker Analyst -> portfolio_manager [UPA]: Looks fine. fake/a: failed. Not a result line. "
        "Still the same remark.\n"
    )


def test_command_line_exits_nonzero_when_a_run_fails(offline_cli, capsys):
    offline_cli.replies[TickerViewDraft] = lambda system, user: 1 / 0
    assert daily.main(["--quiet"]) == 1
    assert "fake/cli: failed. ZeroDivisionError" in capsys.readouterr().out


def test_command_line_exits_nonzero_when_an_unfinished_run_blocks_the_session(offline_cli, capsys, tmp_path):
    import os

    Journal(tmp_path / "journal.sqlite3").start_run("running", "fake/cli", session(13), day(13), pid=os.getppid())
    assert daily.main(["--quiet"]) == 1
    assert "fake/cli: blocked" in capsys.readouterr().out
    assert daily.main(["--quiet", "--force"]) == 1


def test_command_line_reports_a_bad_arm_or_universe_file(offline_cli, capsys, tmp_path):
    assert daily.main(["--arm", "nonsense:model"]) == 1
    assert "Unknown LLM provider 'nonsense'" in capsys.readouterr().err
    assert daily.main(["--universe", str(tmp_path / "missing.txt")]) == 1
    assert "Cannot start" in capsys.readouterr().err


def test_arm_spec_keeps_colons_that_belong_to_the_model_name():
    arm = daily.parse_arm("ollama:qwen2.5-coder:7b")
    assert arm.name == "ollama/qwen2.5-coder:7b" and arm.llm.model == "qwen2.5-coder:7b"
    assert daily._slug(arm.name) == "ollama-qwen2.5-coder-7b"


def test_a_provider_named_alone_runs_the_configured_model_or_its_own_default(monkeypatch):
    # Both settings are given here, so what the machine's .env sets does not matter (config reads it in tests too).
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "LLM_MODEL", "some-model:1b")
    assert daily.parse_arm("ollama").llm.model == "some-model:1b"
    assert daily.parse_arm("ollama").name == "ollama/some-model:1b"
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    assert daily.parse_arm("ollama").llm.model == config.provider_default("ollama", "model")
