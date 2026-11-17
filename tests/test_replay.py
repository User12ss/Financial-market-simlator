"""Scripted replays: the pipeline run over several days with scripted proposals and prices.

No model takes part. A real model knows how past prices turned out and would
steer round the losses, so the breakers and limits would never be reached;
here the proposals are fixed in advance and the prices are made to go against
them.
"""

import pytest

import config
from execution.paper import PaperBroker
from models.signals import ProposalSetDraft
from tests.pipeline_fakes import Clock, ScriptedLLM, SyntheticMarket, day, make_pipeline, proposals, session

THREE_BUYS = proposals(("UPA", "buy", 0.9), ("UPB", "buy", 0.8), ("UPC", "buy", 0.7))
FALLING_FROM_MONDAY = {ticker: (session(16), -0.03) for ticker in ("UPA", "UPB", "UPC")}
SEPARATE_SECTORS = {"UPB": "Energy", "UPC": "Healthcare"}


def replay(tmp_path, market, script, start=13):
    """Run one session per entry of `script`, each with that day's proposals. Returns the signal tables."""
    clock, llm = Clock(day(start)), ScriptedLLM()
    pipeline = make_pipeline(tmp_path, llm, market=market, clock=clock)
    tables = []
    for number, drafts in script:
        clock.now = day(number)
        llm.replies[ProposalSetDraft] = drafts
        [result] = pipeline.run()
        assert result.status in ("ok", "degraded"), result.message
        tables.append(result.table)
    return pipeline, tables


def verdicts(table):
    return {d.ticker: d for d in table.risk}


# ── Losing trades and the circuit breakers ───────────────────────────────────

def test_a_run_of_stopped_out_trades_trips_the_drawdown_breaker_at_its_threshold(tmp_path, monkeypatch):
    # Three positions are opened on Monday's open and all three fall through their stops the same day.
    # Each loses about 1.25% of a 10% position, so the account is down 0.38%.
    monkeypatch.setattr(config, "RISK_MAX_DRAWDOWN_PCT", 0.35)
    market = SyntheticMarket(turns=FALLING_FROM_MONDAY, sectors=SEPARATE_SECTORS)
    pipeline, (friday, monday, tuesday) = replay(tmp_path, market, [(13, THREE_BUYS), (16, THREE_BUYS), (17, THREE_BUYS)])

    assert [d.verdict for d in friday.risk] == ["reduced"] * 3 and len(friday.orders) == 3

    bought = [f for f in monday.fills if f.side == "buy"]
    stopped = [f for f in monday.fills if f.kind == "stop"]
    assert len(bought) == 3 and len(stopped) == 3 and monday.holdings == []
    assert all(f.note == "Stopped out on the day it was opened." and -2.0 < f.pnl_pct < -1.0 for f in stopped)
    assert monday.account.drawdown_pct == 0.38
    assert monday.account.equity == pytest.approx(monday.account.cash)

    # After the fall only UPA and UPB still pass the screen on Monday, and only UPA on Tuesday.
    # Every buy that reaches the gate is turned away, and the breaker holds until equity recovers.
    assert [d.ticker for d in monday.risk] == ["UPA", "UPB"] and [d.ticker for d in tuesday.risk] == ["UPA"]
    for table in (monday, tuesday):
        assert all(d.verdict == "rejected" for d in table.risk) and table.orders == []
        assert all("Drawdown breaker" in d.reasons[0] and "0.35% limit" in d.reasons[0] for d in table.risk)
    assert PaperBroker(pipeline.journal, "fake/a").orders() == []


def test_the_same_losses_just_under_the_threshold_do_not_trip_it(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RISK_MAX_DRAWDOWN_PCT", 0.39)
    market = SyntheticMarket(turns=FALLING_FROM_MONDAY, sectors=SEPARATE_SECTORS)
    _, (_, monday) = replay(tmp_path, market, [(13, THREE_BUYS), (16, THREE_BUYS)])

    assert monday.account.drawdown_pct == 0.38
    assert [(d.ticker, d.verdict) for d in monday.risk] == [("UPA", "reduced"), ("UPB", "reduced")]
    assert len(monday.orders) == 2


def test_daily_loss_breaker_blocks_buys_on_the_day_of_the_loss_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RISK_DAILY_LOSS_LIMIT_PCT", 0.35)
    market = SyntheticMarket(turns=FALLING_FROM_MONDAY, sectors=SEPARATE_SECTORS)
    _, (_, monday, tuesday) = replay(tmp_path, market, [(13, THREE_BUYS), (16, THREE_BUYS), (17, THREE_BUYS)])

    assert monday.account.day_change_pct == -0.38
    assert len(monday.risk) == 2 and monday.orders == []
    assert all("Daily loss breaker" in d.reasons[0] and "0.35% limit" in d.reasons[0] for d in monday.risk)

    assert tuesday.account.day_change_pct == 0.0                   # nothing held, nothing lost
    assert [(d.ticker, d.verdict) for d in tuesday.risk] == [("UPA", "reduced")] and len(tuesday.orders) == 1


# ── Concentration limits ─────────────────────────────────────────────────────

def test_single_name_and_sector_limits_bind_at_their_thresholds(tmp_path, monkeypatch):
    # All three candidates are in one sector, capped at 15% of a 100,000 account.
    monkeypatch.setattr(config, "RISK_MAX_SECTOR_PCT", 15)
    market = SyntheticMarket()
    _, (table,) = replay(tmp_path, market, [(13, THREE_BUYS)])
    closes = {t: float(market.path(t, session(13))["close"].iloc[-1]) for t in ("UPA", "UPB", "UPC")}
    result = verdicts(table)

    first = result["UPA"]                                          # cut to 10% of equity by the single-name limit
    assert first.verdict == "reduced" and first.approved_shares == int(10_000 // closes["UPA"])
    assert "Single position" in first.reasons[0]

    second = result["UPB"]                                         # then the sector has under 5,200 of room left
    room = 15_000 - first.approved_shares * closes["UPA"]
    assert second.verdict == "reduced" and second.approved_shares == int(room // closes["UPB"])
    assert any("Sector" in reason for reason in second.reasons)

    third = result["UPC"]                                          # and less than one share's worth after that
    assert third.verdict == "rejected" and any("Sector" in reason for reason in third.reasons)

    value = sum(result[t].approved_shares * closes[t] for t in closes)
    assert 15_000 - min(closes.values()) < value <= 15_000


def test_the_same_buys_in_separate_sectors_are_limited_only_by_position_size(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RISK_MAX_SECTOR_PCT", 15)
    _, (table,) = replay(tmp_path, SyntheticMarket(sectors=SEPARATE_SECTORS), [(13, THREE_BUYS)])
    assert all(d.verdict == "reduced" and len(d.reasons) == 1 and "Single position" in d.reasons[0] for d in table.risk)


def test_new_positions_per_day_stop_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RISK_MAX_NEW_POSITIONS_PER_DAY", 2)
    pipeline, (friday, monday) = replay(
        tmp_path, SyntheticMarket(sectors=SEPARATE_SECTORS), [(13, THREE_BUYS), (16, proposals(("UPC", "buy", 0.7)))],
    )

    result = verdicts(friday)
    assert result["UPA"].verdict == result["UPB"].verdict == "reduced"
    assert result["UPC"].verdict == "rejected" and "New positions today" in result["UPC"].reasons[0]
    assert "above the limit of 2" in result["UPC"].reasons[0] and len(friday.orders) == 2

    assert verdicts(monday)["UPC"].verdict == "reduced"            # the next day it has its turn
    assert sorted(h.ticker for h in monday.holdings) == ["UPA", "UPB"]


def test_position_count_stops_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RISK_MAX_POSITIONS", 2)
    _, (friday, monday) = replay(
        tmp_path, SyntheticMarket(sectors=SEPARATE_SECTORS),
        [(13, proposals(("UPA", "buy", 0.9), ("UPB", "buy", 0.8))), (16, proposals(("UPC", "buy", 0.7)))],
    )
    assert len(friday.orders) == 2
    # With the book full, the proposal itself is turned down before it is ever sized.
    [proposal] = [p for p in monday.proposals if p.ticker == "UPC"]
    assert proposal.status == "rejected" and "limited to 2 positions" in proposal.note and monday.risk == []


def test_cash_floor_and_turnover_cap_the_days_buying(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RISK_MAX_DAILY_TURNOVER_PCT", 15)
    market = SyntheticMarket(sectors=SEPARATE_SECTORS)
    _, (table,) = replay(tmp_path, market, [(13, THREE_BUYS)])
    closes = {t: float(market.path(t, session(13))["close"].iloc[-1]) for t in ("UPA", "UPB", "UPC")}
    result = verdicts(table)

    assert any("Turnover" in reason for reason in result["UPB"].reasons)
    assert result["UPC"].verdict == "rejected" and any("Turnover" in reason for reason in result["UPC"].reasons)
    assert sum(result[t].approved_shares * closes[t] for t in closes) <= 15_000


# ── A split while a position is held ─────────────────────────────────────────

def test_a_split_passes_through_the_pipeline_without_a_jump_in_equity(tmp_path):
    hold = proposals(("UPA", "hold", 0.7))
    market = SyntheticMarket(splits={("UPA", session(17)): 2.0})
    _, (_, monday, tuesday) = replay(tmp_path, market, [(13, proposals(("UPA", "buy", 0.9))), (16, hold), (17, hold)])

    before, after = monday.holdings[0], tuesday.holdings[0]
    assert tuesday.status == "ok" and tuesday.warnings == [] and tuesday.fills == []
    assert after.shares == before.shares * 2
    assert after.avg_cost == pytest.approx(before.avg_cost / 2, abs=1e-3)
    assert after.stop_price == pytest.approx(before.stop_price / 2, abs=1e-3)
    assert after.unrealized_pct == pytest.approx(0.55, abs=0.03)   # two sessions of +0.3%, less the slippage
    assert tuesday.account.equity == pytest.approx(monday.account.equity * (1 + 0.003 * before.value / monday.account.equity), rel=1e-4)
    assert tuesday.account.day_change_pct == pytest.approx(0.03, abs=0.01)
