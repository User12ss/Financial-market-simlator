"""Position sizing: how accepted proposals become share counts."""

from datetime import date

import pytest

import config
from models.signals import TickerView, TradeProposal
from models.trading import Account, Position
from portfolio.sizing import Quote, order_id, size_orders, usable_stop

SESSION = date(2026, 11, 16)


@pytest.fixture(autouse=True)
def sizing_settings(monkeypatch):
    monkeypatch.setattr(config, "SIZING_RISK_PCT", 1.0)
    monkeypatch.setattr(config, "SIZING_STOP_ATR_MULTIPLE", 2.0)
    monkeypatch.setattr(config, "SIZING_MIN_STOP_PCT", 1.0)
    monkeypatch.setattr(config, "SIZING_STOP_MIN_ATR_MULTIPLE", 1.0)
    monkeypatch.setattr(config, "SIZING_STOP_MAX_ATR_MULTIPLE", 4.0)


def account(equity=100_000.0):
    return Account(
        account="paper", session_date=SESSION, cash=equity, positions_value=0.0, equity=equity,
        initial_cash=equity, high_water=equity, drawdown_pct=0.0,
    )


def proposal(ticker, action="buy", confidence=0.8, status="accepted"):
    return TradeProposal(ticker=ticker, action=action, confidence=confidence, reason="test", status=status)


def view(ticker, confidence=0.9, stop=None):
    return TickerView(
        ticker=ticker, session_date=SESSION, held=False, direction="bullish", confidence=confidence,
        horizon_days=10, thesis="", catalysts="", invalidation="", remark="", proposed_stop=stop,
    )


def position(ticker, shares):
    return Position(ticker=ticker, shares=shares, avg_cost=50.0, entry_date=SESSION, value=shares * 50.0)


def size(proposals, positions=None, views=None, quotes=None, equity=100_000.0):
    return size_orders(
        proposals, session=SESSION, account=account(equity), positions=positions or {},
        views=views or {}, quotes=quotes or {},
    )


def test_order_id_is_session_account_ticker_side():
    assert order_id(SESSION, "paper", "AAPL", "buy") == "2026-11-16|paper|AAPL|buy"
    assert order_id(SESSION, "paper", "AAPL", "buy") == order_id(SESSION, "paper", "AAPL", "buy")


def test_buy_risks_the_budget_between_close_and_an_atr_stop():
    # Budget 100,000 x 1% x 0.8 = 800; stop distance 2 x 2.5 = 5; 800 / 5 = 160 shares.
    orders, notes = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9)},
                         quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert notes == []
    [order] = orders
    assert (order.ticker, order.side, order.kind, order.shares, order.protective_stop) == ("AAPL", "buy", "market", 160, 95.0)
    assert order.order_id == "2026-11-16|paper|AAPL|buy"
    assert "$800.00" in order.reason and "$5.00" in order.reason and "160 shares" in order.reason


def test_buy_share_count_is_floored_and_stop_rounded_to_cents():
    # Budget 1,000 x 0.7 = 700; distance 2 x 3.3333 = 6.6666; 700 / 6.6666 = 105.0 -> 105; stop 93.3367 -> 93.34.
    orders, _ = size([proposal("AAPL", confidence=0.7)], views={"AAPL": view("AAPL", 0.7)},
                     quotes={"AAPL": Quote(close=100.0033, atr=3.3333)})
    assert orders[0].shares == 105
    assert orders[0].protective_stop == 93.34


def test_buy_uses_the_lower_of_proposal_and_view_confidence():
    quotes = {"AAPL": Quote(close=100.0, atr=2.5)}
    orders, _ = size([proposal("AAPL", confidence=0.9)], views={"AAPL": view("AAPL", 0.5)}, quotes=quotes)
    assert orders[0].shares == 100  # 1,000 x 0.5 / 5
    orders, _ = size([proposal("AAPL", confidence=0.3)], views={"AAPL": view("AAPL", 0.9)}, quotes=quotes)
    assert orders[0].shares == 60  # 1,000 x 0.3 / 5


def test_buy_without_a_view_gives_no_order_and_a_note():
    orders, notes = size([proposal("AAPL")], quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert orders == []
    assert len(notes) == 1 and "AAPL" in notes[0] and "view" in notes[0]


@pytest.mark.parametrize("quote", [None, Quote(close=None, atr=2.0), Quote(close=0.0, atr=2.0), Quote(close=-5.0, atr=2.0)])
def test_buy_without_a_usable_close_gives_no_order_and_a_note(quote):
    quotes = {} if quote is None else {"AAPL": quote}
    orders, notes = size([proposal("AAPL")], views={"AAPL": view("AAPL")}, quotes=quotes)
    assert orders == []
    assert len(notes) == 1 and "close" in notes[0]


@pytest.mark.parametrize("atr", [None, 0.0, -1.0])
def test_buy_without_a_usable_atr_gives_no_order_and_a_note(atr):
    orders, notes = size([proposal("AAPL")], views={"AAPL": view("AAPL")}, quotes={"AAPL": Quote(close=100.0, atr=atr)})
    assert orders == []
    assert len(notes) == 1 and "ATR" in notes[0]


def test_buy_whose_stop_would_be_at_or_below_zero_gives_no_order_and_a_note():
    # Distance 2 x 5 = 10 against a close of 10: the stop would be 0.
    orders, notes = size([proposal("AAPL")], views={"AAPL": view("AAPL")}, quotes={"AAPL": Quote(close=10.0, atr=5.0)})
    assert orders == []
    assert len(notes) == 1 and "zero" in notes[0]


def test_buy_too_small_for_one_share_gives_no_order_and_a_note():
    # Budget 1,000 x 1% x 0.8 = 8 against a stop distance of 10.
    orders, notes = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL")},
                         quotes={"AAPL": Quote(close=100.0, atr=5.0)}, equity=1_000.0)
    assert orders == []
    assert len(notes) == 1 and "less than one share" in notes[0]


def test_sell_exits_the_whole_position():
    orders, notes = size([proposal("MSFT", action="sell")], positions={"MSFT": position("MSFT", 37)})
    assert notes == []
    [order] = orders
    assert (order.side, order.shares, order.kind, order.protective_stop) == ("sell", 37, "market", None)
    assert order.order_id == "2026-11-16|paper|MSFT|sell"


def test_trim_sells_half_rounded_down():
    orders, _ = size([proposal("MSFT", action="trim")], positions={"MSFT": position("MSFT", 37)})
    assert (orders[0].side, orders[0].shares) == ("sell", 18)


def test_trim_of_a_one_share_position_gives_no_order_and_a_note():
    orders, notes = size([proposal("MSFT", action="trim")], positions={"MSFT": position("MSFT", 1)})
    assert orders == []
    assert len(notes) == 1 and "MSFT" in notes[0] and "1-share" in notes[0]


@pytest.mark.parametrize("action", ["sell", "trim"])
def test_exit_without_a_position_gives_no_order_and_a_note(action):
    orders, notes = size([proposal("MSFT", action=action)])
    assert orders == []
    assert len(notes) == 1 and "no position" in notes[0]


def test_rejected_proposals_and_holds_are_ignored():
    orders, notes = size(
        [proposal("AAPL", status="rejected"), proposal("MSFT", action="hold")],
        positions={"MSFT": position("MSFT", 10)}, views={"AAPL": view("AAPL")},
        quotes={"AAPL": Quote(close=100.0, atr=2.5)},
    )
    assert orders == [] and notes == []


def test_exits_come_first_by_ticker_then_buys_by_confidence_then_ticker():
    quotes = {t: Quote(close=100.0, atr=2.5) for t in ("AAA", "BBB", "CCC")}
    views = {t: view(t, 0.9) for t in quotes}
    orders, _ = size(
        [
            proposal("CCC", confidence=0.6), proposal("BBB", confidence=0.7), proposal("AAA", confidence=0.6),
            proposal("ZZZ", action="trim"), proposal("MMM", action="sell"),
        ],
        positions={"ZZZ": position("ZZZ", 10), "MMM": position("MMM", 10)}, views=views, quotes=quotes,
    )
    assert [(o.ticker, o.side) for o in orders] == [
        ("MMM", "sell"), ("ZZZ", "sell"), ("BBB", "buy"), ("AAA", "buy"), ("CCC", "buy"),
    ]


def test_buy_ordering_uses_the_confidence_actually_used():
    # BBB proposes 0.9 but its view caps it at 0.5, so AAA at 0.6 comes first.
    quotes = {t: Quote(close=100.0, atr=2.5) for t in ("AAA", "BBB")}
    orders, _ = size(
        [proposal("BBB", confidence=0.9), proposal("AAA", confidence=0.6)],
        views={"AAA": view("AAA", 0.9), "BBB": view("BBB", 0.5)}, quotes=quotes,
    )
    assert [o.ticker for o in orders] == ["AAA", "BBB"]


def test_settings_are_read_when_called(monkeypatch):
    monkeypatch.setattr(config, "SIZING_RISK_PCT", 2.0)
    monkeypatch.setattr(config, "SIZING_STOP_ATR_MULTIPLE", 1.0)
    # Budget 100,000 x 2% x 0.5 = 1,000; distance 2.5; 400 shares; stop 97.50.
    orders, _ = size([proposal("AAPL", confidence=0.5)], views={"AAPL": view("AAPL", 0.9)},
                     quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert (orders[0].shares, orders[0].protective_stop) == (400, 97.5)


def test_stop_is_never_closer_than_the_minimum_share_of_the_price():
    # ATR of 0.10 would put the stop 0.20 away and size 4,000 shares on noise.
    # The floor is 1% of 100 = 1.00: budget 800 / 1.00 = 800 shares, stop at 99.00.
    [order], notes = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9)},
                          quotes={"AAPL": Quote(close=100.0, atr=0.10)})
    assert notes == [] and (order.shares, order.protective_stop) == (800, 99.0)
    assert "the 1% minimum, wider than 2 x ATR $0.10" in order.reason


def test_an_atr_stop_wider_than_the_minimum_is_used_as_it_is():
    [order], _ = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9)},
                      quotes={"AAPL": Quote(close=100.0, atr=0.51)})      # 1.02 away, just past the 1.00 floor
    assert order.protective_stop == 98.98 and "2 x ATR $0.51" in order.reason and "minimum" not in order.reason


# ── A stop proposed by the analyst ───────────────────────────────────────────
# Close 100 and ATR 2.50 throughout: the stop may be 2.50 to 10.00 below the close.

def test_no_proposed_stop_is_not_a_refusal():
    assert usable_stop(100.0, 2.5, None) == (None, "")


@pytest.mark.parametrize("proposed, kept", [(97.5, 97.5), (90.0, 90.0), (93.0, 93.0), (93.004, 93.0), (92.996, 93.0)])
def test_a_stop_inside_the_range_is_kept_to_the_cent(proposed, kept):
    assert usable_stop(100.0, 2.5, proposed) == (kept, "")


@pytest.mark.parametrize("proposed, why", [
    (97.6, "it is $2.40 below the $100.00 close, and the nearest allowed is $2.50 (1 x ATR $2.50)."),
    (99.99, "it is $0.01 below the $100.00 close, and the nearest allowed is $2.50 (1 x ATR $2.50)."),
    (89.9, "it is $10.10 below the $100.00 close, and the furthest allowed is $10.00 (4 x ATR $2.50)."),
    (1.0, "it is $99.00 below the $100.00 close, and the furthest allowed is $10.00 (4 x ATR $2.50)."),
    (100.0, "it is not below the $100.00 close."),
    (99.996, "it is not below the $100.00 close."),      # 100.00 once rounded to the cent
    (120.0, "it is not below the $100.00 close."),
    (0.0, "it is not a price."),
    (0.004, "it is not a price."),                       # nothing once rounded to the cent
    (-95.0, "it is not a price."),
    (float("nan"), "it is not a price."),
    (float("inf"), "it is not a price."),
])
def test_a_stop_outside_the_range_is_refused_not_moved_into_it(proposed, why):
    assert usable_stop(100.0, 2.5, proposed) == (None, why)


def test_a_stop_within_a_cent_of_a_limit_counts_as_on_it():
    # The data shows a model a close of 100.00 whether it is 99.996 or 100.004.
    assert usable_stop(99.996, 2.5, 97.5) == (97.5, "")       # 2.496 away, nearest allowed 2.50
    assert usable_stop(100.004, 2.5, 90.0) == (90.0, "")      # 10.004 away, furthest allowed 10.00
    assert usable_stop(99.98, 2.5, 97.5)[0] is None           # 2.48 away
    assert usable_stop(100.02, 2.5, 90.0)[0] is None          # 10.02 away


def test_the_limits_are_worked_out_from_the_atr_as_the_model_was_shown_it():
    # An ATR of 1.996 is shown as 2.00, so a model can put its stop 8.00 away and be at 4 x ATR.
    assert usable_stop(100.0, 1.996, 92.0) == (92.0, "")
    assert usable_stop(100.0, 2.004, 91.9) == (
        None, "it is $8.10 below the $100.00 close, and the furthest allowed is $8.00 (4 x ATR $2.00).",
    )


@pytest.mark.parametrize("low, high", [
    (5.0, 1.0), (0.0, 4.0), (-1.0, 4.0), (float("nan"), 4.0), (1.0, float("nan")), (1.0, float("inf")),
])
def test_settings_that_give_no_range_mean_no_proposed_stop_is_used(monkeypatch, low, high):
    monkeypatch.setattr(config, "SIZING_STOP_MIN_ATR_MULTIPLE", low)
    monkeypatch.setattr(config, "SIZING_STOP_MAX_ATR_MULTIPLE", high)
    for proposed in (99.5, 97.5, 95.0, 90.0, 50.0):
        stop, why = usable_stop(100.0, 2.5, proposed)
        assert stop is None and why.startswith("the settings give no range to judge it by")
    # The buy is still sized, with the stop the rule gives.
    [order], _ = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9, stop=93.0)},
                      quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert (order.shares, order.protective_stop) == (160, 95.0) and "the settings give no range" in order.reason


def test_a_proposed_stop_is_never_closer_than_the_minimum_share_of_the_price():
    # ATR 0.50: one ATR would allow a stop 0.50 away, but the floor is 1% of 100 = 1.00.
    assert usable_stop(100.0, 0.5, 99.4) == (
        None, "it is $0.60 below the $100.00 close, and the nearest allowed is $1.00 (1% of the price).",
    )
    assert usable_stop(100.0, 0.5, 99.0) == (99.0, "")
    assert usable_stop(100.0, 0.5, 98.0) == (98.0, "")        # 4 x ATR, the furthest


def test_where_the_widest_atr_stop_is_inside_the_floor_only_the_floor_is_allowed():
    # ATR 0.10: 4 x ATR is 0.40, inside the 1.00 floor.
    assert usable_stop(100.0, 0.1, 99.0) == (99.0, "")
    assert usable_stop(100.0, 0.1, 98.5) == (
        None, "it is $1.50 below the $100.00 close, and the furthest allowed is $1.00 (1% of the price).",
    )


@pytest.mark.parametrize("close, atr", [(None, 2.5), (0.0, 2.5), (100.0, None), (100.0, 0.0), (float("nan"), 2.5)])
def test_a_stop_cannot_be_judged_without_a_close_and_an_atr(close, atr):
    assert usable_stop(close, atr, 95.0) == (None, "there is no usable close and ATR to judge it by.")


def test_the_range_is_read_when_called(monkeypatch):
    monkeypatch.setattr(config, "SIZING_STOP_MIN_ATR_MULTIPLE", 2.0)
    monkeypatch.setattr(config, "SIZING_STOP_MAX_ATR_MULTIPLE", 3.0)
    assert usable_stop(100.0, 2.5, 97.0)[0] is None           # 3.00 away, nearest now 5.00
    assert usable_stop(100.0, 2.5, 95.0) == (95.0, "")
    assert usable_stop(100.0, 2.5, 92.0)[0] is None           # 8.00 away, furthest now 7.50


def test_buy_uses_the_analysts_stop_and_sizes_to_its_distance():
    # Budget 100,000 x 1% x 0.8 = 800; the stop is 7.00 away; 800 / 7 = 114 shares.
    [order], notes = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9, stop=93.0)},
                          quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert notes == []
    assert (order.shares, order.protective_stop) == (114, 93.0)
    assert "over a stop distance of $7.00 (the analyst's stop, 2.8 x ATR $2.50) gives 114 shares" in order.reason
    assert "with the stop at $93.00 below the $100.00 close." in order.reason and "not used" not in order.reason


@pytest.mark.parametrize("stop, shares", [(97.5, 320), (95.0, 160), (90.0, 80)])
def test_a_wider_stop_means_fewer_shares_and_the_same_money_at_risk(stop, shares):
    [order], _ = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9, stop=stop)},
                      quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert (order.shares, order.protective_stop) == (shares, stop)
    assert order.shares * (100.0 - order.protective_stop) == 800.0


def test_money_at_risk_never_exceeds_the_budget_whatever_stop_is_proposed():
    quotes = {"AAPL": Quote(close=187.37, atr=4.1183)}
    for cents in range(16000, 18800, 7):
        [order], _ = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9, stop=cents / 100)},
                          quotes=quotes)
        assert order.shares * (187.37 - order.protective_stop) <= 800.0 + 1e-6
        assert 4.12 - 0.0101 <= 187.37 - order.protective_stop <= 4 * 4.12 + 0.0101     # ATR as shown: 4.12


def test_a_stop_outside_the_range_falls_back_to_the_rule_and_the_order_says_so():
    # A view built by hand: the pipeline's own check would already have left this stop out.
    [order], notes = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9, stop=99.0)},
                          quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert notes == []
    assert (order.shares, order.protective_stop) == (160, 95.0)
    assert "(2 x ATR $2.50)" in order.reason
    assert order.reason.endswith(
        "The analyst's stop of $99.00 was not used: it is $1.00 below the $100.00 close, "
        "and the nearest allowed is $2.50 (1 x ATR $2.50)."
    )


def test_without_a_proposed_stop_the_order_does_not_mention_the_analyst():
    [order], _ = size([proposal("AAPL", confidence=0.8)], views={"AAPL": view("AAPL", 0.9)},
                      quotes={"AAPL": Quote(close=100.0, atr=2.5)})
    assert order.protective_stop == 95.0 and "analyst" not in order.reason


# ── The stop the rule gives ──────────────────────────────────────────────────

def test_the_rule_stop_is_sized_on_where_it_lands_once_rounded_to_the_cent():
    # The 1% minimum puts the stop 0.0551 under 5.51, at 5.4549, which is placed as 5.45: 0.06 away.
    # Sized on 0.0551 this would be 18,148 shares and $1,088.88 at risk against a $1,000 budget.
    [order], notes = size([proposal("AAPL", confidence=1.0)], views={"AAPL": view("AAPL", 1.0)},
                          quotes={"AAPL": Quote(close=5.51, atr=0.027)})
    assert notes == []
    assert (order.shares, order.protective_stop) == (16666, 5.45)
    assert order.shares * (5.51 - order.protective_stop) <= 1000.0
    assert "over a stop distance of $0.06" in order.reason


def test_money_at_risk_never_exceeds_the_budget_on_the_rule_path():
    for cents in range(500, 6000, 13):
        for atr in (0.011, 0.027, 0.4, 1.337):
            close = cents / 100
            orders, _ = size([proposal("AAPL", confidence=0.7)], views={"AAPL": view("AAPL", 0.9)},
                             quotes={"AAPL": Quote(close=close, atr=atr)})
            for order in orders:
                assert 0 < order.protective_stop < close
                assert order.shares * (close - order.protective_stop) <= 700.0 + 1e-6


def test_a_rule_stop_that_rounds_to_the_close_gives_no_order_and_a_note():
    # 1% of 0.47 is under half a cent, so the stop rounds back to 0.47.
    orders, notes = size([proposal("AAPL")], views={"AAPL": view("AAPL")}, quotes={"AAPL": Quote(close=0.47, atr=0.001)})
    assert orders == []
    assert len(notes) == 1 and "the close itself once rounded to the cent" in notes[0]
