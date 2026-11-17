"""The risk gate: each rule passes an order just inside its limit and cuts or refuses it just outside."""

from dataclasses import replace
from datetime import date

import pytest

import config
from models.trading import Account, Order, OrderRequest, Position
from risk.gate import MarketFacts, RiskLimits, halted, review

SESSION = date(2026, 11, 16)

# Limits loose enough that none of them binds; each test tightens the one it is about.
LOOSE = RiskLimits(
    max_position=1.0, max_sector=1.0, max_positions=100, min_cash=0.0, max_trade_risk=1.0,
    max_new_positions_per_day=100, max_daily_turnover=10.0, daily_loss_limit=0.03, max_drawdown=0.10,
    min_price=5.0, min_dollar_volume=20_000_000.0, max_limit_deviation=0.05,
)


def limits(**changes):
    return replace(LOOSE, **changes)


def account(equity=100_000.0, cash=None, day_change_pct=0.0, drawdown_pct=0.0):
    cash = equity if cash is None else cash
    return Account(
        account="paper", session_date=SESSION, cash=cash, positions_value=equity - cash, equity=equity,
        initial_cash=100_000.0, high_water=equity, day_change_pct=day_change_pct, drawdown_pct=drawdown_pct,
    )


def position(ticker, shares, close=100.0, sector="Technology"):
    return Position(ticker=ticker, shares=shares, avg_cost=close, entry_date=SESSION, sector=sector,
                    last_close=close, value=shares * close)


def fact(close=100.0, sector="Technology", usable=True, volume=50_000_000.0):
    return MarketFacts(usable=usable, close=close, avg_dollar_volume=volume, sector=sector)


def buy(ticker, shares, stop=90.0, **fields):
    return OrderRequest(order_id=f"{ticker}|buy", ticker=ticker, side="buy", shares=shares, protective_stop=stop, **fields)


def sell(ticker, shares, **fields):
    return OrderRequest(order_id=f"{ticker}|sell", ticker=ticker, side="sell", shares=shares, **fields)


def open_order(ticker, side, kind="market", shares=10):
    return Order(order_id=f"old|{ticker}|{side}", ticker=ticker, side=side, kind=kind, shares=shares,
                 stop_price=50.0 if kind == "stop" else None, status="open", created_session=SESSION)


TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE")


def run(requests, *, acct=None, positions=None, open_orders=None, facts=None, universe=None, lim=LOOSE, halt=""):
    facts = facts if facts is not None else {t: fact() for t in TICKERS}
    return review(
        requests, account=acct or account(), positions=positions or {}, open_orders=open_orders or [],
        facts=facts, universe=set(TICKERS) if universe is None else universe, limits=lim, halt_reason=halt,
    )


def one(request, **kwargs):
    [decision] = run([request], **kwargs)
    return decision


# ── Kill switch ──────────────────────────────────────────────────────────────

def test_halt_rejects_every_order_including_exits():
    decisions = run([buy("AAA", 10), sell("BBB", 5)], positions={"BBB": position("BBB", 5)}, halt="Halted for test.")
    assert [(d.verdict, d.approved_shares, d.reasons) for d in decisions] == [
        ("rejected", 0, ["Halted for test."]), ("rejected", 0, ["Halted for test."]),
    ]


def test_halted_is_empty_without_file_or_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "HALT"))
    monkeypatch.delenv("TRADING_HALT", raising=False)
    assert halted() == ""


def test_halted_when_the_kill_switch_file_exists(tmp_path, monkeypatch):
    path = tmp_path / "HALT"
    path.touch()
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(path))
    monkeypatch.delenv("TRADING_HALT", raising=False)
    assert str(path) in halted()


def test_halted_when_the_env_is_set_to_1(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "HALT"))
    monkeypatch.setenv("TRADING_HALT", "1")
    assert "TRADING_HALT" in halted()
    monkeypatch.setenv("TRADING_HALT", "0")
    assert halted() == ""


# ── Order checks ─────────────────────────────────────────────────────────────

def test_share_count_below_one_is_rejected():
    d = one(buy("AAA", 0))
    assert d.verdict == "rejected" and "whole number of at least 1" in d.reasons[0]


def test_market_order_with_a_limit_price_is_rejected():
    d = one(buy("AAA", 10, limit_price=100.0))
    assert d.verdict == "rejected" and "market order" in d.reasons[0]


def test_limit_price_exactly_at_the_deviation_limit_passes_and_beyond_is_rejected():
    assert one(buy("AAA", 10, kind="limit", limit_price=105.0)).verdict == "approved"
    assert one(buy("AAA", 10, kind="limit", limit_price=95.0)).verdict == "approved"
    d = one(buy("AAA", 10, kind="limit", limit_price=105.01))
    assert d.verdict == "rejected" and "5%" in d.reasons[0] and "105.01" in d.reasons[0]


def test_limit_order_without_a_positive_price_is_rejected():
    assert one(buy("AAA", 10, kind="limit")).verdict == "rejected"
    assert one(buy("AAA", 10, kind="limit", limit_price=-1.0)).verdict == "rejected"


def test_stop_order_needs_a_positive_stop_price():
    positions = {"AAA": position("AAA", 10)}
    assert one(sell("AAA", 10, kind="stop", stop_price=80.0), positions=positions).verdict == "approved"
    d = one(sell("AAA", 10, kind="stop"), positions=positions)
    assert d.verdict == "rejected" and "stop price" in d.reasons[0]
    assert one(sell("AAA", 10, kind="stop", stop_price=0.0), positions=positions).verdict == "rejected"


def test_duplicate_of_an_open_broker_order_is_rejected():
    d = one(buy("AAA", 10), open_orders=[open_order("AAA", "buy")])
    assert d.verdict == "rejected" and "Duplicate" in d.reasons[0]
    # The other side of the same ticker is not a duplicate.
    assert one(buy("AAA", 10), open_orders=[open_order("AAA", "sell")]).verdict == "approved"


def test_second_request_for_the_same_ticker_and_side_in_one_call_is_rejected():
    first, second, other = run([buy("AAA", 10), buy("AAA", 5), buy("BBB", 5)])
    assert first.verdict == "approved" and other.verdict == "approved"
    assert second.verdict == "rejected" and "Duplicate" in second.reasons[0]


# ── Sells ────────────────────────────────────────────────────────────────────

def test_sell_without_a_position_is_rejected():
    d = one(sell("AAA", 10))
    assert d.verdict == "rejected" and "Long only" in d.reasons[0]


def test_sell_of_more_than_held_is_cut_to_the_position():
    positions = {"AAA": position("AAA", 30)}
    assert one(sell("AAA", 30), positions=positions).verdict == "approved"
    d = one(sell("AAA", 31), positions=positions)
    assert (d.verdict, d.approved_shares) == ("reduced", 30) and "30 shares held" in d.reasons[0]


def test_sells_ignore_every_buy_limit_and_the_circuit_breakers():
    tight = limits(max_position=0.0001, max_sector=0.0001, max_positions=0, min_cash=0.99, max_trade_risk=0.0001,
                   max_new_positions_per_day=0, max_daily_turnover=0.0001, min_price=1_000.0,
                   min_dollar_volume=1e15)
    decisions = run(
        [sell("AAA", 50), sell("BBB", 50)],
        acct=account(cash=0.0, day_change_pct=-20.0, drawdown_pct=50.0),
        positions={"AAA": position("AAA", 50), "BBB": position("BBB", 50)},
        facts={"BBB": fact(usable=False, close=None, volume=None)}, universe=set(), lim=tight,
    )
    assert [(d.verdict, d.approved_shares, d.reasons) for d in decisions] == [("approved", 50, []), ("approved", 50, [])]


def test_sells_are_decided_before_buys_whatever_their_place_in_the_list():
    # Turnover limit 30,000. The sell (10,000) is listed after the buy but counts first,
    # so the buy gets 20,000 of room: 200 shares, not 300.
    decisions = run([buy("AAA", 300), sell("BBB", 100)], positions={"BBB": position("BBB", 100)},
                    lim=limits(max_daily_turnover=0.30))
    assert [d.ticker for d in decisions] == ["AAA", "BBB"]
    assert (decisions[0].verdict, decisions[0].approved_shares) == ("reduced", 200)
    assert (decisions[1].verdict, decisions[1].approved_shares) == ("approved", 100)


def test_decisions_come_back_in_request_order():
    requests = [buy("CCC", 5), sell("AAA", 5), buy("BBB", 0), sell("DDD", 5), buy("EEE", 5)]
    decisions = run(requests, positions={"AAA": position("AAA", 5)})
    assert [d.order_id for d in decisions] == [r.order_id for r in requests]
    assert [d.verdict for d in decisions] == ["approved", "approved", "rejected", "rejected", "approved"]


# ── Circuit breakers ─────────────────────────────────────────────────────────

def test_daily_loss_breaker_trips_at_the_limit():
    assert one(buy("AAA", 10), acct=account(day_change_pct=-2.99)).verdict == "approved"
    assert one(buy("AAA", 10), acct=account(day_change_pct=None)).verdict == "approved"
    d = one(buy("AAA", 10), acct=account(day_change_pct=-3.0))
    assert d.verdict == "rejected" and "Daily loss" in d.reasons[0] and "3%" in d.reasons[0]


def test_drawdown_breaker_trips_at_the_limit():
    assert one(buy("AAA", 10), acct=account(drawdown_pct=9.99)).verdict == "approved"
    d = one(buy("AAA", 10), acct=account(drawdown_pct=10.0))
    assert d.verdict == "rejected" and "Drawdown" in d.reasons[0] and "10%" in d.reasons[0]


# ── Ticker eligibility ───────────────────────────────────────────────────────

def test_buy_outside_the_whitelist_is_rejected():
    d = one(buy("ZZZ", 10), facts={"ZZZ": fact()})
    assert d.verdict == "rejected" and "whitelist" in d.reasons[0]


@pytest.mark.parametrize("facts", [{}, {"AAA": fact(usable=False)}, {"AAA": fact(close=None)}])
def test_buy_without_usable_data_is_rejected(facts):
    d = one(buy("AAA", 10, stop=4.0), facts=facts)
    assert d.verdict == "rejected" and "usable data" in d.reasons[0]


def test_minimum_price_passes_at_the_limit_and_rejects_below():
    assert one(buy("AAA", 10, stop=4.0), facts={"AAA": fact(close=5.0)}).verdict == "approved"
    d = one(buy("AAA", 10, stop=4.0), facts={"AAA": fact(close=4.99)})
    assert d.verdict == "rejected" and "minimum price" in d.reasons[0] and "4.99" in d.reasons[0]


def test_minimum_dollar_volume_passes_at_the_limit_and_rejects_below():
    assert one(buy("AAA", 10), facts={"AAA": fact(volume=20_000_000.0)}).verdict == "approved"
    d = one(buy("AAA", 10), facts={"AAA": fact(volume=19_999_999.0)})
    assert d.verdict == "rejected" and "dollar volume" in d.reasons[0] and "$20,000,000" in d.reasons[0]
    assert one(buy("AAA", 10), facts={"AAA": fact(volume=None)}).verdict == "rejected"


@pytest.mark.parametrize("stop", [None, 0.0, -5.0, 100.0, 101.0])
def test_buy_needs_a_positive_protective_stop_below_the_close(stop):
    d = one(buy("AAA", 10, stop=stop))
    assert d.verdict == "rejected" and "protective stop" in d.reasons[0]


def test_buy_with_a_stop_just_below_the_close_passes():
    assert one(buy("AAA", 10, stop=99.99)).verdict == "approved"


# ── Position counts ──────────────────────────────────────────────────────────

def test_position_count_includes_holdings_open_buys_and_todays_buys():
    # Limit 3: one held, one buy open at the broker, AAA approved here makes 3; BBB would make 4.
    decisions = run(
        [buy("AAA", 10), buy("BBB", 10), buy("CCC", 10)],
        positions={"CCC": position("CCC", 10)}, open_orders=[open_order("DDD", "buy")],
        lim=limits(max_positions=3),
    )
    assert [(d.ticker, d.verdict) for d in decisions] == [("AAA", "approved"), ("BBB", "rejected"), ("CCC", "approved")]
    assert "4 positions" in decisions[1].reasons[0] and "limit of 3" in decisions[1].reasons[0]


def test_new_positions_per_day_limit():
    # Limit 2: AAA and BBB open new positions, CCC would be the third; adding to held DDD does not count.
    decisions = run(
        [buy("AAA", 10), buy("BBB", 10), buy("CCC", 10), buy("DDD", 10)],
        positions={"DDD": position("DDD", 10)}, lim=limits(max_new_positions_per_day=2),
    )
    assert [d.verdict for d in decisions] == ["approved", "approved", "rejected", "approved"]
    assert "number 3" in decisions[2].reasons[0] and "limit of 2" in decisions[2].reasons[0]


def test_a_buy_cut_to_zero_does_not_use_a_new_position_slot():
    # AAA's stop risk allows no share at all, so BBB still gets the single slot.
    decisions = run([buy("AAA", 10, stop=1.0), buy("BBB", 10, stop=99.0)],
                    lim=limits(max_new_positions_per_day=1, max_trade_risk=0.0001))
    assert [d.verdict for d in decisions] == ["rejected", "approved"]


# ── Limits that cut the share count ──────────────────────────────────────────

def test_per_trade_risk_cuts_the_share_count():
    # 1% of 100,000 = 1,000 of risk; 10 to the stop per share -> 100 shares.
    lim = limits(max_trade_risk=0.01)
    assert one(buy("AAA", 100, stop=90.0), lim=lim).verdict == "approved"
    d = one(buy("AAA", 101, stop=90.0), lim=lim)
    assert (d.verdict, d.approved_shares) == ("reduced", 100)
    assert d.reasons[0].startswith("Per-trade risk") and "1%" in d.reasons[0] and "$1,010.00" in d.reasons[0]


def test_single_position_limit_counts_shares_already_held():
    # 10% of 100,000 = 10,000 at 100 -> 100 shares in all; 20 held leaves 80.
    lim = limits(max_position=0.10)
    positions = {"AAA": position("AAA", 20)}
    assert one(buy("AAA", 80), positions=positions, lim=lim).verdict == "approved"
    d = one(buy("AAA", 81), positions=positions, lim=lim)
    assert (d.verdict, d.approved_shares) == ("reduced", 80)
    assert d.reasons[0].startswith("Single position") and "10%" in d.reasons[0] and "$10,100.00" in d.reasons[0]


def test_sector_limit_counts_holdings_and_earlier_buys_in_the_sector():
    # 30% = 30,000. 10,000 held in Technology, AAA approved for 10,000 -> BBB gets 10,000: 100 shares.
    lim = limits(max_sector=0.30)
    positions = {"CCC": position("CCC", 100, sector="Technology")}
    ok = run([buy("AAA", 100), buy("BBB", 100)], positions=positions, lim=lim)
    assert [d.verdict for d in ok] == ["approved", "approved"]
    cut = run([buy("AAA", 100), buy("BBB", 101)], positions=positions, lim=lim)
    assert (cut[1].verdict, cut[1].approved_shares) == ("reduced", 100)
    assert cut[1].reasons[0].startswith("Sector: Technology") and "30%" in cut[1].reasons[0]


def test_sector_limit_ignores_other_sectors():
    lim = limits(max_sector=0.30)
    positions = {"CCC": position("CCC", 290, sector="Energy")}
    assert one(buy("AAA", 300), positions=positions, lim=lim).verdict == "approved"


def test_unknown_sector_is_capped_like_any_other():
    lim = limits(max_sector=0.30)
    facts = {"AAA": fact(sector=""), "BBB": fact(sector="")}
    positions = {"CCC": position("CCC", 100, sector="")}
    d = run([buy("AAA", 100), buy("BBB", 101)], positions=positions, facts=facts, lim=lim)[1]
    assert (d.verdict, d.approved_shares) == ("reduced", 100) and "Unknown" in d.reasons[0]


def test_cash_floor_counts_earlier_buys_but_not_sells():
    # Cash 30,000, floor 10% = 10,000 -> 20,000 to spend. AAA takes 10,000, BBB gets 10,000: 100 shares.
    # The approved sell of CCC would free 5,000 but is not counted.
    lim = limits(min_cash=0.10)
    acct = account(cash=30_000.0)
    positions = {"CCC": position("CCC", 50)}
    ok = run([sell("CCC", 50), buy("AAA", 100), buy("BBB", 100)], acct=acct, positions=positions, lim=lim)
    assert [d.verdict for d in ok] == ["approved", "approved", "approved"]
    cut = run([sell("CCC", 50), buy("AAA", 100), buy("BBB", 101)], acct=acct, positions=positions, lim=lim)
    assert (cut[2].verdict, cut[2].approved_shares) == ("reduced", 100)
    assert cut[2].reasons[0].startswith("Cash floor") and "10%" in cut[2].reasons[0] and "$10,000.00" in cut[2].reasons[0]


def test_cash_already_at_the_floor_rejects_the_buy():
    d = one(buy("AAA", 10), acct=account(cash=10_000.0), lim=limits(min_cash=0.10))
    assert (d.verdict, d.approved_shares) == ("rejected", 0) and d.reasons[0].startswith("Cash floor")


def test_turnover_counts_approved_sells_and_buys():
    # 30% = 30,000. Sell 10,000 + AAA 10,000 leaves 10,000 for BBB: 100 shares.
    lim = limits(max_daily_turnover=0.30)
    positions = {"CCC": position("CCC", 100)}
    ok = run([buy("AAA", 100), buy("BBB", 100), sell("CCC", 100)], positions=positions, lim=lim)
    assert [d.verdict for d in ok] == ["approved", "approved", "approved"]
    cut = run([buy("AAA", 100), buy("BBB", 101), sell("CCC", 100)], positions=positions, lim=lim)
    assert (cut[1].verdict, cut[1].approved_shares) == ("reduced", 100)
    assert cut[1].reasons[0].startswith("Turnover") and "30%" in cut[1].reasons[0]


def test_a_sell_is_never_cut_by_turnover():
    d = one(sell("CCC", 100), positions={"CCC": position("CCC", 100)}, lim=limits(max_daily_turnover=0.01))
    assert (d.verdict, d.approved_shares) == ("approved", 100)


def test_every_limit_that_cuts_an_order_is_reported():
    # Risk allows 100, position 80, sector 60, cash 40, turnover 20.
    lim = limits(max_trade_risk=0.01, max_position=0.08, max_sector=0.06, min_cash=0.10, max_daily_turnover=0.02)
    d = one(buy("AAA", 500, stop=90.0), acct=account(cash=14_000.0), lim=lim)
    assert (d.verdict, d.approved_shares) == ("reduced", 20)
    assert [r.split(":")[0] for r in d.reasons] == ["Per-trade risk", "Single position", "Sector", "Cash floor", "Turnover"]


def test_an_order_cut_to_zero_is_rejected_with_every_reason():
    lim = limits(max_trade_risk=0.01, min_cash=0.10)
    d = one(buy("AAA", 500, stop=90.0), acct=account(cash=10_000.0), lim=lim)
    assert (d.verdict, d.approved_shares) == ("rejected", 0)
    assert [r.split(":")[0] for r in d.reasons] == ["Per-trade risk", "Cash floor"]


# ── Settings ─────────────────────────────────────────────────────────────────

def test_limits_from_config_turn_percents_into_fractions(monkeypatch):
    for name, value in {
        "RISK_MAX_POSITION_PCT": 10, "RISK_MAX_SECTOR_PCT": 30, "RISK_MAX_POSITIONS": 8, "RISK_MIN_CASH_PCT": 5,
        "RISK_MAX_TRADE_RISK_PCT": 1.0, "RISK_MAX_NEW_POSITIONS_PER_DAY": 3, "RISK_MAX_DAILY_TURNOVER_PCT": 25,
        "RISK_DAILY_LOSS_LIMIT_PCT": 3, "RISK_MAX_DRAWDOWN_PCT": 12, "RISK_MIN_PRICE": 7.0,
        "RISK_MIN_DOLLAR_VOLUME": 1e7, "RISK_MAX_LIMIT_DEVIATION_PCT": 4,
    }.items():
        monkeypatch.setattr(config, name, value)
    assert RiskLimits.from_config() == RiskLimits(
        max_position=0.10, max_sector=0.30, max_positions=8, min_cash=0.05, max_trade_risk=0.01,
        max_new_positions_per_day=3, max_daily_turnover=0.25, daily_loss_limit=0.03, max_drawdown=0.12,
        min_price=7.0, min_dollar_volume=1e7, max_limit_deviation=0.04,
    )


# ── What is already at the broker ────────────────────────────────────────────

def test_a_protective_stop_does_not_block_the_exit_of_its_position():
    held = {"AAA": position("AAA", 10)}
    decision = one(sell("AAA", 10), positions=held, open_orders=[open_order("AAA", "sell", kind="stop")])
    assert decision.verdict == "approved" and decision.approved_shares == 10

    again = one(sell("AAA", 10), positions=held, open_orders=[open_order("AAA", "sell", kind="market")])
    assert again.verdict == "rejected" and "open market sell order for AAA" in again.reasons[0]


def test_a_buy_waiting_at_the_broker_counts_against_cash_and_its_sector():
    waiting = [open_order("BBB", "buy", shares=400)]                      # 40,000 already spoken for
    elsewhere = {t: fact() for t in TICKERS} | {"BBB": fact(sector="Energy")}

    cash = one(buy("AAA", 700), open_orders=waiting, facts=elsewhere)
    assert cash.approved_shares == 600 and "Cash floor" in cash.reasons[0]      # 100,000 - 40,000 leaves 60,000

    sector = one(buy("AAA", 300), open_orders=waiting, lim=limits(max_sector=0.5))
    assert sector.approved_shares == 100 and "Sector" in sector.reasons[0]      # 50,000 cap less the 40,000
    assert one(buy("AAA", 300), open_orders=waiting, facts=elsewhere, lim=limits(max_sector=0.5)).verdict == "approved"


def test_a_waiting_buy_whose_ticker_cannot_be_priced_still_counts_against_cash():
    waiting = Order(order_id="old|BBB|buy", ticker="BBB", side="buy", kind="market", shares=400,
                    protective_stop=95.0, status="open", created_session=SESSION)
    unpriced = {t: fact() for t in TICKERS} | {"BBB": fact(close=None, usable=False, sector="Energy")}

    decision = one(buy("AAA", 700), open_orders=[waiting], facts=unpriced)

    assert decision.approved_shares == 620 and "Cash floor" in decision.reasons[0]   # 400 x 95 = 38,000 held back


def test_a_holding_that_cannot_be_priced_may_not_be_added_to():
    held = {"AAA": Position(ticker="AAA", shares=10, avg_cost=100.0, entry_date=SESSION, value=1000.0)}
    decision = one(buy("AAA", 5), positions=held)
    assert decision.verdict == "rejected" and "cannot be priced today" in decision.reasons[0]
    assert one(buy("AAA", 5), positions={"AAA": position("AAA", 10)}).verdict == "approved"
