"""The paper broker: matching against daily bars, splits and dividends, cash, and crash-safe placement."""

from datetime import date

import pytest

from data.models import Bars, CorporateAction, Fundamentals, Issue, PriceSummary, Snapshot, TickerSnapshot
from execution.paper import PaperBroker
from execution.placement import place, reconcile
from journal.store import Journal
from models.trading import OrderRequest

# Sessions of the week of 16 November 2026, and the Friday before.
FRI, MON, TUE, WED, THU = date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19)
DAYS = [FRI, MON, TUE, WED, THU]


def ticker(symbol, bars, splits=(), dividends=(), usable=True, sector="Technology"):
    """A ticker's snapshot from (date, open, high, low, close) rows."""
    dates, opens, highs, lows, closes = zip(*bars)
    actions = [CorporateAction(date=d, kind="split", value=v) for d, v in splits]
    actions += [CorporateAction(date=d, kind="dividend", value=v) for d, v in dividends]
    return TickerSnapshot(
        ticker=symbol, price=PriceSummary(session=dates[-1], close=closes[-1]),
        fundamentals=Fundamentals(sector=sector), actions=actions,
        bars=Bars(dates=list(dates), open=list(opens), high=list(highs), low=list(lows), close=list(closes),
                  adj_close=list(closes), volume=[1e6] * len(dates)),
        issues=[] if usable else [Issue(severity="block", code="stale_price", message=f"{symbol}: stale.")],
    )


def flat(symbol, upto, price=100.0, **options):
    """A ticker that opens, trades and closes at one price every session up to `upto`."""
    return ticker(symbol, [(d, price, price, price, price) for d in DAYS if d <= upto], **options)


def snap(session, *tickers):
    return Snapshot(snapshot_id=f"{session}_x", created_at=f"{session}T22:00:00Z", session_date=session,
                    tickers={item.ticker: item for item in tickers})


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.sqlite3")


@pytest.fixture
def broker(journal):
    return PaperBroker(journal, "acct", initial_cash=10_000, slippage_bps=0, stale_days=10)


def buy(symbol="AAA", shares=10, stop=None, session=FRI, **options):
    return OrderRequest(order_id=f"{session}|acct|{symbol}|buy", ticker=symbol, side="buy", shares=shares,
                        protective_stop=stop, **options)


def sell(symbol="AAA", shares=10, session=MON, **options):
    return OrderRequest(order_id=f"{session}|acct|{symbol}|sell", ticker=symbol, side="sell", shares=shares, **options)


def opened(broker, shares=10, stop=None, price=100.0):
    """A position in AAA bought at Monday's open."""
    broker.sync(snap(FRI, flat("AAA", FRI, price)))
    broker.submit(buy(shares=shares, stop=stop), FRI)
    broker.sync(snap(MON, flat("AAA", MON, price)))
    return broker


# ── Accounts and market orders ───────────────────────────────────────────────

def test_a_new_account_starts_with_its_cash_and_no_history(broker):
    account = broker.account()
    assert (account.cash, account.equity, account.high_water) == (10_000, 10_000, 10_000)
    assert account.session_date is None and account.day_change_pct is None and account.drawdown_pct == 0.0
    assert broker.positions() == {} and broker.orders() == []


def test_market_buy_fills_at_the_next_open_and_cash_pays_for_it(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10), FRI)
    assert broker.sync(snap(FRI, flat("AAA", FRI))).fills == []          # same session: nothing to do

    result = broker.sync(snap(MON, ticker("AAA", [(FRI, 100, 100, 100, 100), (MON, 102, 105, 101, 104)])))

    fill = result.fills[0]
    assert (fill.side, fill.kind, fill.shares, fill.price, fill.fill_date) == ("buy", "market", 10, 102, MON)
    position = broker.positions()["AAA"]
    assert (position.shares, position.avg_cost, position.entry_date, position.sessions_held) == (10, 102, MON, 1)
    assert position.last_close == 104 and position.unrealized_pct == pytest.approx(1.96, abs=0.01)
    assert position.sector == "Technology"
    account = broker.account()
    assert account.cash == 10_000 - 1_020 and account.equity == pytest.approx(10_020)
    assert broker.orders() == [] and broker.orders(open_only=False)[0].status == "filled"


def test_slippage_makes_every_market_fill_worse(journal):
    broker = PaperBroker(journal, "acct", initial_cash=10_000, slippage_bps=50)
    opened(broker)
    assert broker.positions()["AAA"].avg_cost == pytest.approx(100.5)

    broker.submit(sell(shares=10), MON)
    fill = broker.sync(snap(TUE, flat("AAA", TUE))).fills[0]
    assert fill.price == pytest.approx(99.5) and fill.pnl_pct == pytest.approx(-1.0, abs=0.01)


def test_an_order_never_fills_on_the_session_it_was_created_after(broker):
    # The books are a day behind when the order comes in. Monday's open is already history
    # for an order created after Monday's close, so it must wait for Tuesday.
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, session=MON), MON)

    assert broker.sync(snap(MON, flat("AAA", MON))).fills == []
    fill = broker.sync(snap(TUE, ticker("AAA", [(MON, 100, 100, 100, 100), (TUE, 103, 103, 103, 103)]))).fills[0]
    assert (fill.fill_date, fill.price) == (TUE, 103)


def test_syncing_to_an_earlier_session_changes_nothing(broker, journal):
    opened(broker, shares=10, stop=95)
    broker.sync(snap(TUE, flat("AAA", TUE)))
    before = (broker.account(), broker.positions(), broker.orders(), broker.equity_history())

    result = broker.sync(snap(MON, flat("AAA", MON)))

    assert result.fills == [] and result.problems == []
    assert (broker.account(), broker.positions(), broker.orders(), broker.equity_history()) == before
    assert journal.rows("SELECT out_of_sync_since FROM paper_positions")[0][0] is None


def test_a_buy_the_account_cannot_pay_for_is_rejected_whole(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=101), FRI)

    result = broker.sync(snap(MON, flat("AAA", MON)))

    assert result.fills == [] and "cost 10,100.00 and the account had 10,000.00" in result.problems[0]
    assert broker.positions() == {} and broker.account().cash == 10_000
    assert broker.orders(open_only=False)[0].status == "rejected"


def test_sells_fill_before_buys_so_their_cash_can_be_used(broker):
    opened(broker, shares=100)                                           # all the cash is in AAA
    broker.submit(sell(shares=100), MON)
    broker.submit(buy("BBB", shares=100, session=MON), MON)

    result = broker.sync(snap(TUE, flat("AAA", TUE), flat("BBB", TUE)))

    assert [(f.ticker, f.side) for f in result.fills] == [("AAA", "sell"), ("BBB", "buy")]
    assert set(broker.positions()) == {"BBB"} and result.problems == []


def test_a_sell_is_cut_to_what_is_held_and_cancelled_when_nothing_is(broker):
    opened(broker, shares=10)
    broker.submit(sell(shares=25), MON)
    assert broker.sync(snap(TUE, flat("AAA", TUE))).fills[0].shares == 10
    assert broker.orders(open_only=False)[-1].shares == 10               # the order shows what traded, not what was asked

    broker.submit(sell(shares=5, session=TUE), TUE)
    assert broker.sync(snap(WED, flat("AAA", WED))).fills == []
    assert broker.orders(open_only=False)[-1].note == "There is no position to sell."


def test_trimming_keeps_the_position_and_shrinks_its_stop(broker):
    opened(broker, shares=10, stop=90)
    broker.submit(sell(shares=5), MON)
    broker.sync(snap(TUE, flat("AAA", TUE)))

    assert broker.positions()["AAA"].shares == 5
    stop = [o for o in broker.orders() if o.kind == "stop"][0]
    assert stop.shares == 5 and stop.stop_price == 90


def test_skipped_days_are_replayed_in_order(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10), FRI)

    bars = [(FRI, 100, 100, 100, 100), (MON, 101, 101, 101, 101), (TUE, 103, 103, 103, 103), (WED, 105, 105, 105, 105)]
    result = broker.sync(snap(WED, ticker("AAA", bars)))

    assert result.fills[0].fill_date == MON and result.fills[0].price == 101
    assert [day for day, _ in broker.equity_history()] == [FRI, MON, TUE, WED]
    assert [round(value) for _, value in broker.equity_history()] == [10_000, 10_000, 10_020, 10_040]
    assert broker.positions()["AAA"].sessions_held == 3


def test_an_order_with_no_usable_price_waits_and_is_cancelled_after_the_limit(journal):
    broker = PaperBroker(journal, "acct", initial_cash=10_000, slippage_bps=0, stale_days=3)
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10), FRI)

    assert broker.sync(snap(MON, flat("AAA", MON, usable=False))).problems == []      # three days since Friday: not yet
    result = broker.sync(snap(TUE, flat("AAA", TUE, usable=False)))

    assert "was cancelled, no usable opening price appeared within 3 days" in result.problems[0]
    assert broker.orders() == []


# ── Stops ────────────────────────────────────────────────────────────────────

def test_a_buy_gets_its_protective_stop_when_it_fills(broker):
    opened(broker, shares=10, stop=95)
    stop = broker.orders()[0]
    assert (stop.kind, stop.side, stop.shares, stop.stop_price, stop.status) == ("stop", "sell", 10, 95, "open")
    assert broker.positions()["AAA"].stop_price == 95


def test_stop_fills_at_the_stop_when_the_low_reaches_it(broker):
    opened(broker, shares=10, stop=95)
    bars = [(FRI, 100, 100, 100, 100), (MON, 100, 100, 100, 100), (TUE, 99, 99, 94, 96)]

    fill = broker.sync(snap(TUE, ticker("AAA", bars))).fills[0]

    assert (fill.kind, fill.price, fill.shares, fill.fill_date) == ("stop", 95, 10, TUE)
    assert fill.pnl_pct == pytest.approx(-5.0) and broker.positions() == {} and broker.orders() == []


def test_stop_fills_at_the_open_when_the_session_gaps_below_it(broker):
    opened(broker, shares=10, stop=95)
    bars = [(FRI, 100, 100, 100, 100), (MON, 100, 100, 100, 100), (TUE, 90, 92, 88, 91)]

    fill = broker.sync(snap(TUE, ticker("AAA", bars))).fills[0]
    assert fill.price == 90 and fill.note == "Opened below the stop."


def test_stop_does_not_fill_when_the_low_stays_above_it(broker):
    opened(broker, shares=10, stop=95)
    bars = [(FRI, 100, 100, 100, 100), (MON, 100, 100, 100, 100), (TUE, 99, 99, 95.01, 98)]
    assert broker.sync(snap(TUE, ticker("AAA", bars))).fills == []
    assert broker.positions()["AAA"].shares == 10


def test_a_position_can_be_stopped_out_on_the_day_it_opens(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, stop=95), FRI)

    result = broker.sync(snap(MON, ticker("AAA", [(FRI, 100, 100, 100, 100), (MON, 100, 101, 93, 94)])))

    assert [(f.side, f.kind, f.price) for f in result.fills] == [("buy", "market", 100), ("sell", "stop", 95)]
    assert result.fills[1].note == "Stopped out on the day it was opened." and broker.positions() == {}


def test_a_buy_that_would_fill_at_or_below_its_own_stop_is_not_made(broker):
    # Sized against a stop of 95 from Friday's close of 100. Monday opens at 94: the reason
    # for the trade is gone before it began, and selling "at the stop" of 95 would invent a profit.
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, stop=95), FRI)

    result = broker.sync(snap(MON, ticker("AAA", [(FRI, 100, 100, 100, 100), (MON, 94, 97, 93, 96)])))

    assert result.fills == [] and result.problems == []
    assert "was not made. It would have filled at 94.00, at or below the stop of 95.00" in result.notices[0]
    assert broker.positions() == {} and broker.orders() == [] and broker.account().cash == 10_000
    assert broker.orders(open_only=False)[0].status == "cancelled"


def test_a_buy_that_opens_exactly_at_its_stop_is_not_made_and_one_just_above_is(broker):
    broker.sync(snap(FRI, flat("AAA", FRI), flat("BBB", FRI)))
    broker.submit(buy("AAA", shares=10, stop=95), FRI)
    broker.submit(buy("BBB", shares=10, stop=95), FRI)

    result = broker.sync(snap(MON, ticker("AAA", [(FRI, 100, 100, 100, 100), (MON, 95, 96, 95, 96)]),
                              ticker("BBB", [(FRI, 100, 100, 100, 100), (MON, 95.01, 96, 95.01, 96)])))

    assert [(f.ticker, f.side, f.price) for f in result.fills] == [("BBB", "buy", 95.01)]


def test_a_limit_buy_that_would_fill_below_its_stop_is_not_made_either(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, stop=95, kind="limit", limit_price=98), FRI)

    result = broker.sync(snap(MON, ticker("AAA", [(FRI, 100, 100, 100, 100), (MON, 94, 97, 93, 96)])))

    assert result.fills == [] and "was not made" in result.notices[0] and broker.account().cash == 10_000


def test_adding_to_a_position_never_loosens_its_stop(broker):
    opened(broker, shares=10, stop=95)
    broker.submit(buy(shares=5, stop=80, session=MON), MON)
    broker.sync(snap(TUE, flat("AAA", TUE)))
    stop = [o for o in broker.orders() if o.kind == "stop"][0]
    assert (stop.shares, stop.stop_price) == (15, 95)                    # all fifteen shares, at the tighter stop

    broker.submit(buy(shares=5, stop=97, session=TUE), TUE)
    broker.sync(snap(WED, flat("AAA", WED)))
    stop = [o for o in broker.orders() if o.kind == "stop"][0]
    assert (stop.shares, stop.stop_price) == (20, 97)                    # a higher stop does take over


def test_stop_wins_over_an_exit_order_on_the_same_day(broker):
    opened(broker, shares=10, stop=95)
    broker.submit(sell(shares=10), MON)
    bars = [(FRI, 100, 100, 100, 100), (MON, 100, 100, 100, 100), (TUE, 90, 92, 88, 91)]

    result = broker.sync(snap(TUE, ticker("AAA", bars)))

    assert [(f.kind, f.price) for f in result.fills] == [("stop", 90)]
    assert broker.orders() == [] and broker.orders(open_only=False)[-1].status == "cancelled"


# ── Limit orders ─────────────────────────────────────────────────────────────

def test_limit_buy_fills_at_the_limit_or_better(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, kind="limit", limit_price=98), FRI)
    touched = broker.sync(snap(MON, ticker("AAA", [(FRI, 100, 100, 100, 100), (MON, 100, 101, 97, 99)])))
    assert touched.fills[0].price == 98 and touched.fills[0].kind == "limit"

    broker.submit(buy("BBB", shares=10, kind="limit", limit_price=98, session=MON), MON)
    gapped = broker.sync(snap(TUE, flat("AAA", TUE), ticker("BBB", [(MON, 100, 100, 100, 100), (TUE, 95, 96, 94, 95)])))
    assert gapped.fills[0].price == 95                                   # opened below the limit: the open is better


def test_limit_sell_fills_at_the_limit_or_better_and_a_stop_goes_first(broker):
    opened(broker, shares=10, stop=95)
    broker.submit(sell(shares=10, kind="limit", limit_price=105), MON)
    assert broker.sync(snap(TUE, ticker("AAA", [(MON, 100, 100, 100, 100), (TUE, 101, 104.9, 100, 104)]))).fills == []

    # On Wednesday the low reaches the stop and the high reaches the limit: the stop is taken first.
    bars = [(MON, 100, 100, 100, 100), (TUE, 101, 104.9, 100, 104), (WED, 100, 106, 94, 100)]
    result = broker.sync(snap(WED, ticker("AAA", bars)))
    assert [(f.kind, f.price) for f in result.fills] == [("stop", 95)] and broker.orders() == []


def test_a_day_only_limit_expires_after_its_first_session(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, kind="limit", limit_price=90, day_only=True), FRI)

    result = broker.sync(snap(MON, flat("AAA", MON)))

    assert result.fills == [] and "expired unfilled" in result.notices[0]
    assert broker.orders(open_only=False)[0].status == "expired"


def test_a_limit_without_day_only_keeps_working(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, kind="limit", limit_price=90), FRI)
    broker.sync(snap(MON, flat("AAA", MON)))

    bars = [(FRI, 100, 100, 100, 100), (MON, 100, 100, 100, 100), (TUE, 95, 95, 89, 92)]
    assert broker.sync(snap(TUE, ticker("AAA", bars))).fills[0].price == 90


# ── Dividends and splits ─────────────────────────────────────────────────────

def test_dividend_is_paid_in_cash_to_shares_held_before_the_ex_date(broker):
    opened(broker, shares=10)
    result = broker.sync(snap(TUE, flat("AAA", TUE, dividends=[(TUE, 0.5)])))

    assert broker.account().cash == 10_000 - 1_000 + 5
    assert "dividend of 0.5 a share on 10 shares, 5.00 in cash" in result.notices[0]


def test_a_position_bought_on_the_ex_date_gets_no_dividend(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10), FRI)
    broker.sync(snap(MON, flat("AAA", MON, dividends=[(MON, 0.5)])))
    assert broker.account().cash == 9_000


def test_split_changes_shares_cost_and_stop_but_not_value(broker):
    opened(broker, shares=10, stop=90)                                   # 10 shares at 100, stop 90
    # A 2-for-1 on Tuesday: the source restates every bar at half.
    result = broker.sync(snap(TUE, flat("AAA", TUE, price=50.0, splits=[(TUE, 2.0)])))

    position = broker.positions()["AAA"]
    assert (position.shares, position.avg_cost, position.stop_price) == (20, 50, 45)
    assert position.unrealized_pct == 0.0 and result.fills == [] and result.problems == []
    assert broker.account().equity == pytest.approx(10_000)


def test_a_split_leaving_a_fraction_pays_it_in_cash(broker):
    opened(broker, shares=5)
    result = broker.sync(snap(TUE, flat("AAA", TUE, price=100 / 1.5, splits=[(TUE, 1.5)])))

    assert broker.positions()["AAA"].shares == 7                         # 7.5 shares: half a share goes to cash
    assert broker.account().cash == pytest.approx(9_500 + 0.5 * 100 / 1.5)
    assert "of a share, paid out in cash" in result.notices[0]


def test_a_reverse_split_that_leaves_no_whole_share_ends_the_position(broker):
    opened(broker, shares=3, stop=90)
    broker.sync(snap(TUE, flat("AAA", TUE, price=1000.0, splits=[(TUE, 0.1)])))

    assert broker.positions() == {} and broker.orders() == []
    assert broker.account().cash == pytest.approx(10_000)                # 0.3 of a share at 1,000


def test_prices_restated_without_a_listed_split_freeze_the_position(broker):
    opened(broker, shares=10, stop=90)
    broker.submit(sell(shares=10), MON)

    result = broker.sync(snap(TUE, flat("AAA", TUE, price=50.0)))       # halved, no split listed

    assert result.fills == []                                            # neither the sale nor the stop at 90
    assert "listed splits and its restated prices do not agree" in result.problems[0]
    position = broker.positions()["AAA"]
    assert position.shares == 10 and position.last_close is None and position.value == 1_000
    assert broker.account().equity == pytest.approx(10_000)              # carried at the last matched close
    assert broker.equity_history()[-1] == (TUE, pytest.approx(10_000))   # and marked that way for the day


def test_a_position_out_of_step_too_long_is_closed_at_its_last_matched_close(journal):
    broker = opened(PaperBroker(journal, "acct", initial_cash=10_000, slippage_bps=0, stale_days=1), stop=90)

    broker.sync(snap(TUE, flat("AAA", TUE, usable=False)))               # first sync to find it unusable
    assert broker.sync(snap(WED, flat("AAA", WED, usable=False))).fills == []
    result = broker.sync(snap(THU, flat("AAA", THU, usable=False)))

    fill = result.fills[0]
    assert (fill.kind, fill.price, fill.fill_date, fill.shares) == ("close", 100, MON, 10)
    assert "no longer tracked" in result.problems[0] and broker.positions() == {} and broker.orders() == []
    assert broker.account().cash == 10_000


def test_days_without_a_sync_do_not_count_against_a_position(journal):
    broker = opened(PaperBroker(journal, "acct", initial_cash=10_000, slippage_bps=0, stale_days=1))
    assert broker.sync(snap(THU, flat("AAA", THU, usable=False))).fills == []     # first sight of the problem
    assert broker.positions()["AAA"].shares == 10


def test_a_buy_waiting_across_a_split_is_cancelled(broker):
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10), FRI)

    result = broker.sync(snap(MON, flat("AAA", MON, price=50.0, splits=[(MON, 2.0)])))

    assert result.fills == [] and "a split took effect after it was sized" in result.problems[0]
    assert broker.orders() == [] and broker.account().cash == 10_000


def test_a_limit_buy_sized_before_a_split_is_cancelled_like_a_market_one(broker):
    # A limit of 99 on a 100 stock. After a 2-for-1 every bar reads about 50, far below 99,
    # so without this the order would "fill" although the price never came down to it.
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10, kind="limit", limit_price=99), FRI)

    result = broker.sync(snap(MON, flat("AAA", MON, price=50.5, splits=[(MON, 2.0)])))

    assert result.fills == [] and "a split took effect after it was sized" in result.problems[0]
    assert broker.account().cash == 10_000


def test_a_buy_filled_from_a_bar_before_a_later_split_is_cancelled_too(broker):
    # Sized on Friday, no sync on Monday, synced on Tuesday, the day of a split. Monday's open in that
    # snapshot is already halved, so ten shares there would be half the position that was sized.
    broker.sync(snap(FRI, flat("AAA", FRI)))
    broker.submit(buy(shares=10), FRI)

    result = broker.sync(snap(TUE, flat("AAA", TUE, price=50.0, splits=[(TUE, 2.0)])))

    assert result.fills == [] and "was cancelled, a split took effect" in result.problems[0]


def test_a_sale_filled_from_a_bar_before_a_later_split_keeps_its_true_result(broker):
    opened(broker, shares=10, stop=90)
    broker.submit(sell(shares=10), MON)

    # No sync on Tuesday. Wednesday's snapshot has a 2-for-1 on Wednesday and every bar restated.
    result = broker.sync(snap(WED, flat("AAA", WED, price=50.0, splits=[(WED, 2.0)])))

    fill = result.fills[0]
    assert (fill.fill_date, fill.shares, fill.price, fill.pnl_pct) == (TUE, 20, 50, 0.0)
    assert broker.positions() == {} and broker.account().cash == pytest.approx(10_000)


def test_two_splits_between_syncs_are_both_applied(broker):
    opened(broker, shares=10, stop=90)
    result = broker.sync(snap(WED, flat("AAA", WED, price=100 / 6, splits=[(TUE, 2.0), (WED, 3.0)])))

    position = broker.positions()["AAA"]
    assert (position.shares, position.stop_price) == (60, 15) and position.avg_cost == pytest.approx(100 / 6, abs=1e-3)
    assert result.problems == [] and broker.account().equity == pytest.approx(10_000, abs=0.05)


def test_a_split_on_a_day_the_data_was_unusable_is_applied_when_the_data_returns(broker):
    opened(broker, shares=10, stop=90)

    broken = broker.sync(snap(TUE, flat("AAA", TUE, price=50.0, splits=[(TUE, 2.0)], usable=False)))
    assert broken.fills == [] and broker.positions()["AAA"].shares == 10          # untouched, and not priced
    assert broker.positions()["AAA"].last_close is None

    broker.sync(snap(WED, flat("AAA", WED, price=50.0, splits=[(TUE, 2.0)])))
    position = broker.positions()["AAA"]
    assert (position.shares, position.avg_cost, position.stop_price, position.unrealized_pct) == (20, 50, 45, 0.0)


def test_an_exit_waiting_through_an_unusable_day_and_a_split_fills_with_its_true_result(broker):
    opened(broker, shares=10, stop=90)
    broker.submit(sell(shares=5), MON)
    broker.sync(snap(TUE, flat("AAA", TUE, price=50.0, splits=[(TUE, 2.0)], usable=False)))

    result = broker.sync(snap(WED, flat("AAA", WED, price=50.0, splits=[(TUE, 2.0)])))

    # Tuesday could not be priced, so the order is taken up on Wednesday: half, on the new scale.
    fill = result.fills[0]
    assert (fill.fill_date, fill.shares, fill.price, fill.pnl_pct) == (WED, 10, 50, 0.0)
    assert broker.positions()["AAA"].shares == 10


def test_a_dividend_on_a_day_the_ticker_could_not_be_priced_is_paid_when_it_can(broker):
    opened(broker, shares=10)
    assert broker.sync(snap(TUE, flat("AAA", TUE, dividends=[(TUE, 0.5)], usable=False))).notices == []
    assert broker.account().cash == 9_000

    result = broker.sync(snap(WED, flat("AAA", WED, dividends=[(TUE, 0.5)])))
    assert broker.account().cash == 9_005 and "paid late" in result.notices[0]

    assert broker.sync(snap(THU, flat("AAA", THU, dividends=[(TUE, 0.5)]))).notices == []     # and only once


def test_a_listed_split_the_prices_do_not_reflect_is_refused(broker):
    opened(broker, shares=10, stop=90)
    result = broker.sync(snap(TUE, flat("AAA", TUE, price=100.0, splits=[(TUE, 2.0)])))

    assert "do not agree" in result.problems[0] and "split factor of 2" in result.problems[0]
    assert broker.positions()["AAA"].shares == 10 and broker.positions()["AAA"].last_close is None


def test_a_small_revision_of_an_old_price_is_accepted_as_a_correction(broker):
    opened(broker, shares=10)
    revised = [(FRI, 100, 100, 100, 100), (MON, 102, 102, 102, 102), (TUE, 103, 103, 103, 103)]

    result = broker.sync(snap(TUE, ticker("AAA", revised)))               # Monday was 100 when recorded

    assert "Taken as a price correction by the source" in result.problems[0] and "2.0% apart" in result.problems[0]
    assert broker.positions()["AAA"].last_close == 103
    following = revised + [(WED, 104, 104, 104, 104)]
    assert broker.sync(snap(WED, ticker("AAA", following))).problems == []     # nothing to say the next day


def test_a_revision_too_large_to_be_a_correction_is_refused(broker):
    opened(broker, shares=10)
    revised = [(FRI, 100, 100, 100, 100), (MON, 108, 108, 108, 108), (TUE, 109, 109, 109, 109)]
    result = broker.sync(snap(TUE, ticker("AAA", revised)))
    assert "do not agree" in result.problems[0] and broker.positions()["AAA"].last_close is None


def test_an_out_of_step_date_is_cleared_once_the_position_is_matched_again(journal):
    broker = opened(PaperBroker(journal, "acct", initial_cash=10_000, slippage_bps=0, stale_days=1))
    broker.sync(snap(TUE, flat("AAA", TUE, usable=False)))
    assert journal.rows("SELECT out_of_sync_since FROM paper_positions")[0][0] == TUE.isoformat()

    broker.sync(snap(WED, flat("AAA", WED)))
    assert journal.rows("SELECT out_of_sync_since FROM paper_positions")[0][0] is None
    assert broker.sync(snap(THU, flat("AAA", THU, usable=False))).fills == []     # the count starts afresh


# ── Equity ───────────────────────────────────────────────────────────────────

def test_equity_drawdown_and_day_change_follow_the_closes(broker):
    opened(broker, shares=50)                                            # 5,000 in AAA at 100
    broker.sync(snap(TUE, ticker("AAA", [(MON, 100, 100, 100, 100), (TUE, 100, 111, 100, 110)])))
    up = broker.account()
    assert up.equity == 10_500 and up.high_water == 10_500 and up.day_change_pct == 5.0 and up.drawdown_pct == 0.0

    broker.sync(snap(WED, ticker("AAA", [(MON, 100, 100, 100, 100), (TUE, 100, 111, 100, 110), (WED, 108, 108, 89, 90)])))
    down = broker.account()
    assert down.equity == 9_500 and down.high_water == 10_500
    assert down.day_change_pct == pytest.approx(-9.52, abs=0.01) and down.drawdown_pct == pytest.approx(9.52, abs=0.01)


def test_a_day_with_no_bar_is_valued_at_the_last_close_before_it_not_a_later_one(broker):
    opened(broker, shares=50)                                            # 5,000 in AAA at 100 on Monday
    # No sync on Tuesday, and Wednesday's data has no bar for Tuesday at all.
    result = broker.sync(snap(WED, ticker("AAA", [(FRI, 100, 100, 100, 100), (MON, 100, 100, 100, 100),
                                                 (WED, 118, 121, 118, 120)])))

    history = dict(broker.equity_history())
    assert history[TUE] == pytest.approx(10_000)                         # Monday's close, not Wednesday's 120
    assert history[WED] == pytest.approx(11_000) and result.problems == []


def test_syncing_again_to_the_same_session_reports_what_the_first_sync_found(journal):
    broker = PaperBroker(journal, "acct", initial_cash=10_000, slippage_bps=0, stale_days=3)
    broker.sync(snap(FRI, flat("AAA", FRI), flat("BBB", FRI)))
    broker.submit(buy("AAA", shares=10), FRI)
    broker.submit(buy("BBB", shares=10), FRI)
    data = snap(TUE, flat("AAA", TUE, usable=False), flat("BBB", TUE, dividends=[(TUE, 0.5)]))

    first = broker.sync(data)
    assert len(first.fills) == 1 and len(first.problems) == 1 and len(first.notices) == 1
    before = (broker.account(), broker.positions(), broker.orders(open_only=False))

    again = broker.sync(data)                                            # as a run taken up after a crash would

    assert again == first
    assert (broker.account(), broker.positions(), broker.orders(open_only=False)) == before
    assert len(journal.rows("SELECT 1 FROM paper_fills")) == 1


def test_accounts_are_separate(journal):
    first, second = PaperBroker(journal, "a", initial_cash=1_000), PaperBroker(journal, "b", initial_cash=2_000)
    first.sync(snap(FRI, flat("AAA", FRI)))
    first.submit(buy(shares=1), FRI)

    assert second.orders() == [] and second.account().cash == 2_000 and second.account().session_date is None
    assert PaperBroker(journal, "a").account().cash == 1_000             # reopening does not reset it


def test_a_sync_that_fails_part_way_changes_nothing(broker, monkeypatch):
    opened(broker, shares=10, stop=95)
    before = (broker.account(), broker.positions(), broker.orders())

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(broker, "_save", explode)
    with pytest.raises(RuntimeError, match="disk full"):
        broker.sync(snap(TUE, ticker("AAA", [(MON, 100, 100, 100, 100), (TUE, 90, 92, 88, 91)])))
    monkeypatch.undo()

    assert (broker.account(), broker.positions(), broker.orders()) == before
    assert len(broker.unreported_fills()) == 1                           # only the original buy


# ── Placement ────────────────────────────────────────────────────────────────

def test_submitting_the_same_order_twice_places_it_once(broker):
    first = broker.submit(buy(shares=10), FRI)
    again = broker.submit(buy(shares=99), FRI)
    assert again == first and len(broker.orders()) == 1 and broker.orders()[0].shares == 10


def test_placement_records_intent_then_submission_then_confirmation(journal, broker):
    placed = place(journal, broker, "acct", "run1", FRI, [buy(shares=10), buy("BBB", shares=5)])

    assert [order.status for order in placed] == ["open", "open"]
    assert [row["state"] for row in journal.rows("SELECT state FROM order_log ORDER BY order_id")] == ["confirmed"] * 2
    assert len(broker.orders()) == 2


@pytest.mark.parametrize("crash_after", ["intent", "submit"])
def test_a_crash_between_steps_does_not_place_an_order_twice(journal, broker, monkeypatch, crash_after):
    request = buy(shares=10)
    if crash_after == "intent":
        monkeypatch.setattr(broker, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("killed")))
    else:
        import execution.placement as placement
        real = placement._set_state

        def die_after_submit(journal_, order_id, state):
            if state == "submitted":
                raise RuntimeError("killed")
            return real(journal_, order_id, state)

        monkeypatch.setattr(placement, "_set_state", die_after_submit)
    with pytest.raises(RuntimeError, match="killed"):
        place(journal, broker, "acct", "run1", FRI, [request])
    monkeypatch.undo()
    assert journal.rows("SELECT state FROM order_log")[0]["state"] == "intent"

    placed = place(journal, broker, "acct", "run2", FRI, [request])     # the rerun

    assert len(broker.orders()) == 1 and placed[0].order_id == request.order_id
    assert journal.rows("SELECT state, run_id FROM order_log")[0]["state"] == "confirmed"

    assert place(journal, broker, "acct", "run3", FRI, [request]) == placed    # and a third time changes nothing
    assert len(broker.orders()) == 1


def test_placement_stops_if_the_broker_does_not_have_an_order_it_was_given(journal, broker, monkeypatch):
    monkeypatch.setattr(broker, "submit", lambda request, session: None)   # the order goes nowhere
    with pytest.raises(RuntimeError, match="was submitted but the broker does not have it"):
        place(journal, broker, "acct", "run1", FRI, [buy(shares=10), buy("BBB", shares=5)])

    states = [row["state"] for row in journal.rows("SELECT state FROM order_log ORDER BY order_id")]
    assert states == ["submitted"] and broker.orders() == []              # nothing after it was attempted


# ── Deciding a session again ─────────────────────────────────────────────────

def logged(journal):
    return sorted(row["order_id"] for row in journal.rows("SELECT order_id FROM order_log"))


def test_reconcile_takes_back_what_the_new_decision_does_not_ask_for(journal, broker):
    keep, drop, change = buy("AAA", shares=10), buy("BBB", shares=5), buy("CCC", shares=7, stop=90)
    place(journal, broker, "acct", "run1", FRI, [keep, drop, change])
    changed = buy("CCC", shares=3, stop=90)                              # the same order, cut to three shares

    withdrawn = reconcile(journal, broker, "acct", FRI, [keep, changed])

    assert sorted((o.ticker, o.shares) for o in withdrawn) == [("BBB", 5), ("CCC", 7)]
    assert [(o.ticker, o.shares) for o in broker.orders()] == [("AAA", 10)]
    assert logged(journal) == [keep.order_id]

    placed = place(journal, broker, "acct", "run2", FRI, [keep, changed])
    assert sorted((o.ticker, o.shares) for o in placed) == [("AAA", 10), ("CCC", 3)]
    assert sorted((o.ticker, o.shares) for o in broker.orders()) == [("AAA", 10), ("CCC", 3)]


def test_reconcile_with_nothing_wanted_takes_back_every_order_of_the_session(journal, broker):
    place(journal, broker, "acct", "run1", FRI, [buy("AAA", shares=10), buy("BBB", shares=5)])
    assert len(reconcile(journal, broker, "acct", FRI, [])) == 2
    assert broker.orders() == [] and logged(journal) == []


def test_reconcile_forgets_an_intent_that_never_reached_the_broker(journal, broker, monkeypatch):
    monkeypatch.setattr(broker, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("killed")))
    with pytest.raises(RuntimeError):
        place(journal, broker, "acct", "run1", FRI, [buy("AAA", shares=10)])
    monkeypatch.undo()
    assert logged(journal) != []

    assert reconcile(journal, broker, "acct", FRI, []) == [] and logged(journal) == []


def test_reconcile_mends_a_log_that_says_confirmed_for_an_order_the_broker_does_not_have(journal, broker):
    # What a crash between a withdrawal and the log clean-up leaves behind when the broker
    # keeps its own books: the order is gone there, and the log still calls it confirmed.
    wanted = buy("AAA", shares=10)
    place(journal, broker, "acct", "run1", FRI, [wanted])
    assert broker.withdraw(wanted.order_id) is True
    assert broker.orders() == [] and logged(journal) == [wanted.order_id]

    assert reconcile(journal, broker, "acct", FRI, [wanted]) == []
    assert logged(journal) == []                                         # so placing starts again from the beginning

    placed = place(journal, broker, "acct", "run2", FRI, [wanted])
    assert [(o.ticker, o.shares, o.status) for o in placed] == [("AAA", 10, "open")] and len(broker.orders()) == 1


def test_a_kill_inside_reconcile_undoes_the_withdrawal_with_the_paper_broker(journal, broker, monkeypatch):
    class Killed(BaseException):
        pass

    place(journal, broker, "acct", "run1", FRI, [buy("AAA", shares=10)])
    real_withdraw = broker.withdraw

    def withdraw_then_die(order_id):
        real_withdraw(order_id)
        raise Killed()

    monkeypatch.setattr(broker, "withdraw", withdraw_then_die)
    with pytest.raises(Killed):
        reconcile(journal, broker, "acct", FRI, [buy("AAA", shares=7)])  # the same order, now for seven shares
    monkeypatch.undo()

    assert [(o.ticker, o.shares) for o in broker.orders()] == [("AAA", 10)]      # books and log still agree
    assert logged(journal) == [f"{FRI}|acct|AAA|buy"]

    reconcile(journal, broker, "acct", FRI, [buy("AAA", shares=7)])
    place(journal, broker, "acct", "run2", FRI, [buy("AAA", shares=7)])
    assert [(o.ticker, o.shares) for o in broker.orders()] == [("AAA", 7)]


def test_reconcile_leaves_other_sessions_stops_and_filled_orders_alone(journal, broker):
    opened(broker, shares=10, stop=95)                                   # Friday's buy has filled; its stop stands
    place(journal, broker, "acct", "run2", MON, [sell(shares=5)])

    assert reconcile(journal, broker, "acct", FRI, []) == []             # nothing of Friday's is still open
    assert reconcile(journal, broker, "acct", MON, [sell(shares=5)]) == []
    kinds = sorted(o.kind for o in broker.orders())
    assert kinds == ["market", "stop"] and broker.positions()["AAA"].shares == 10
    assert broker.withdraw(f"{FRI}|acct|AAA|buy") is False               # a filled order cannot be taken back
