"""The evaluation's arithmetic, on cases built by hand whose answers are known.

November 2026, the month every case sits in: sessions are 2-6, 9-13, 16-20,
23-25, 27 and 30. The 26th is Thanksgiving.
"""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

import config
from data.market_calendar import trading_day_after, trading_days
from data.providers import BAR_COLUMNS
from evaluation.baselines import (
    account_curve, benchmark_curve, candidate_curve, max_drawdown_pct, options, random_benchmark, replay, sized_at,
)
from evaluation.prices import Prices
from evaluation.scoring import confidence_bucket, score_view, scorecard, window
from evaluation.trades import how_it_ended, summarize, trades_of
from journal.store import Journal
from models.evaluation import Trade, TradeExit, ViewScore
from models.signals import ScreenRow, SignalTable, TickerView, TradeProposal
from models.trading import Account, Position
from risk.gate import RiskLimits


def nov(number: int) -> date:
    return date(2026, 11, number)


def bars(rows: dict[date, tuple]) -> pd.DataFrame:
    """Day -> (open, high, low, close) or (open, high, low, close, dividend)."""
    data = [
        {"open": row[0], "high": row[1], "low": row[2], "close": row[3], "adj_close": row[3], "volume": 1e6,
         "dividends": row[4] if len(row) > 4 else 0.0, "splits": 0.0}
        for row in rows.values()
    ]
    return pd.DataFrame(data, index=list(rows), columns=BAR_COLUMNS)


def simple(first: date, last: date, opens_at: float, closes_at: float) -> pd.DataFrame:
    """Bars from `first` to `last` that open at one price on the first day and close at another on the last."""
    days = trading_days(first, last)
    rows = {}
    for index, day in enumerate(days):
        close = closes_at if index == len(days) - 1 else opens_at
        rows[day] = (opens_at, max(opens_at, close), min(opens_at, close), close)
    return bars(rows)


def a_view(direction="bullish", confidence=0.7, horizon=3, written=nov(9), ticker="AAA", held=False) -> TickerView:
    return TickerView(
        ticker=ticker, session_date=written, held=held, direction=direction, confidence=confidence,
        horizon_days=horizon, thesis="", catalysts="", invalidation="", remark="",
    )


def score(view, prices, as_of=nov(30), **extra) -> ViewScore:
    return score_view(view, run_id="run-1", arm="fake/a", sector=extra.get("sector", "Technology"),
                      regime=extra.get("regime", "risk_on"), prices=prices, benchmark="SPY", as_of=as_of)


# ── Scoring one view ─────────────────────────────────────────────────────────
# A view written after the close of Monday the 9th with a three-session horizon
# is measured from Tuesday's open to Thursday's close.

def test_a_view_is_measured_from_the_next_open_to_the_close_that_ends_its_horizon():
    assert window(a_view(written=nov(9), horizon=3)) == (nov(10), nov(12))
    assert window(a_view(written=nov(12), horizon=2)) == (nov(13), nov(16))      # over a weekend
    assert window(a_view(written=nov(24), horizon=2)) == (nov(25), nov(27))      # over Thanksgiving
    assert window(a_view(written=nov(9), horizon=1)) == (nov(10), nov(10))       # one session: its open to its close


@pytest.mark.parametrize("direction, opened, closed, spy_opened, spy_closed, made, reference, right, beat", [
    ("bullish", 100, 110, 400, 404, 10.0, 1.0, True, True),        # rose, and by more than the benchmark
    ("bullish", 100, 101, 400, 412, 1.0, 3.0, True, False),        # rose, but the benchmark rose more
    ("bullish", 100, 95, 400, 368, -5.0, -8.0, False, True),       # fell, but less than the benchmark
    ("bearish", 100, 96, 400, 404, -4.0, 1.0, True, True),         # fell while the benchmark rose
    ("bearish", 100, 102, 400, 404, 2.0, 1.0, False, False),       # rose, and by more than the benchmark
    ("bearish", 100, 99, 400, 392, -1.0, -2.0, True, False),       # fell, but held up better than the benchmark
    ("bullish", 100, 100, 400, 404, 0.0, 1.0, False, False),       # flat is not "rose"
    ("bearish", 100, 100, 400, 396, 0.0, -1.0, False, False),      # nor "fell"
])
def test_views_with_known_outcomes_are_scored_as_worked_out_by_hand(
    direction, opened, closed, spy_opened, spy_closed, made, reference, right, beat,
):
    prices = Prices({
        "AAA": simple(nov(10), nov(12), opened, closed), "SPY": simple(nov(10), nov(12), spy_opened, spy_closed),
    })
    result = score(a_view(direction), prices)

    assert result.status == "scored" and (result.starts_on, result.matures_on) == (nov(10), nov(12))
    assert result.return_pct == pytest.approx(made) and result.benchmark_return_pct == pytest.approx(reference)
    assert result.excess_pct == pytest.approx(made - reference)
    assert (result.direction_right, result.beat_benchmark) == (right, beat)


def test_a_neutral_view_is_measured_but_claims_nothing_to_be_right_about():
    prices = Prices({"AAA": simple(nov(10), nov(12), 100, 110), "SPY": simple(nov(10), nov(12), 400, 404)})
    result = score(a_view("neutral"), prices)
    assert result.status == "scored" and result.return_pct == pytest.approx(10.0)
    assert result.direction_right is None and result.beat_benchmark is None


def test_the_starting_price_is_the_next_open_not_the_close_the_view_was_written_after():
    # News after Monday's close lifts Tuesday's open to 110. The analyst wrote with that news in hand,
    # so the jump is not something the view foresaw: from 110 the ticker went on to lose.
    prices = Prices({
        "AAA": bars({nov(9): (100, 100, 100, 100), nov(10): (110, 110, 108, 109), nov(11): (109, 109, 108, 108),
                     nov(12): (108, 108, 107, 107.8)}),
        "SPY": simple(nov(9), nov(12), 400, 400),
    })
    result = score(a_view("bullish"), prices)
    assert result.return_pct == pytest.approx(-2.0) and result.direction_right is False


def test_a_view_whose_horizon_has_not_ended_is_pending_and_carries_no_numbers():
    prices = Prices({"AAA": simple(nov(10), nov(12), 100, 110), "SPY": simple(nov(10), nov(12), 400, 404)})
    result = score(a_view(), prices, as_of=nov(11))
    assert result.status == "pending" and result.return_pct is None and result.direction_right is None
    assert score(a_view(), prices, as_of=nov(12)).status == "scored"            # the close that ends it has happened


@pytest.mark.parametrize("missing", ["AAA", "SPY"])
def test_a_view_that_cannot_be_priced_is_set_apart_not_guessed(missing):
    frames = {"AAA": simple(nov(10), nov(12), 100, 110), "SPY": simple(nov(10), nov(12), 400, 404)}
    frames[missing] = frames[missing].drop(index=nov(12))                       # no bar on the day it matures
    result = score(a_view(), Prices(frames))
    assert result.status == "unpriced" and result.return_pct is None and result.direction_right is None


def test_a_price_that_is_not_a_number_counts_as_missing():
    frame = simple(nov(10), nov(12), 100, 110)
    frame.loc[nov(10), "open"] = float("nan")
    result = score(a_view(), Prices({"AAA": frame, "SPY": simple(nov(10), nov(12), 400, 404)}))
    assert result.status == "unpriced"


def test_dividends_paid_inside_the_window_are_part_of_the_return():
    # Opens at 100, closes at 99 after going ex a dividend of 2 on Wednesday: 101 for every 100 put in.
    frame = bars({nov(10): (100, 100, 100, 100), nov(11): (98, 98, 98, 98, 2.0), nov(12): (98, 99, 98, 99)})
    result = score(a_view(), Prices({"AAA": frame, "SPY": simple(nov(10), nov(12), 400, 400)}))
    assert result.return_pct == pytest.approx(1.0) and result.direction_right is True


def test_a_dividend_that_goes_ex_on_the_first_day_is_not_received():
    # Whoever buys at the open of the ex-date does not get that dividend.
    frame = bars({nov(10): (100, 100, 100, 100, 2.0), nov(11): (100, 100, 99, 99), nov(12): (99, 99, 99, 99)})
    result = score(a_view(), Prices({"AAA": frame, "SPY": simple(nov(10), nov(12), 400, 400)}))
    assert result.return_pct == pytest.approx(-1.0)


def test_the_benchmarks_dividends_count_too():
    spy = bars({nov(10): (400, 400, 400, 400), nov(11): (400, 400, 400, 400, 4.0), nov(12): (400, 400, 400, 400)})
    result = score(a_view(), Prices({"AAA": simple(nov(10), nov(12), 100, 100.5), "SPY": spy}))
    assert result.benchmark_return_pct == pytest.approx(1.0) and result.beat_benchmark is False


def test_a_score_keeps_what_the_view_said_and_the_day_it_was_said():
    prices = Prices({"AAA": simple(nov(10), nov(12), 100, 110), "SPY": simple(nov(10), nov(12), 400, 404)})
    result = score(a_view("bullish", confidence=0.85, held=True), prices, sector="Energy", regime="risk_off")
    assert (result.ticker, result.session_date, result.direction, result.confidence, result.horizon_days) == (
        "AAA", nov(9), "bullish", 0.85, 3)
    assert (result.held, result.sector, result.regime, result.run_id, result.arm) == (
        True, "Energy", "risk_off", "run-1", "fake/a")


# ── The scorecard ────────────────────────────────────────────────────────────

def scored(direction="bullish", made=2.0, reference=1.0, confidence=0.7, written=nov(9), ticker="AAA",
           sector="Technology", regime="risk_on", held=False, status="scored") -> ViewScore:
    done = status == "scored"
    sided = direction != "neutral"
    excess = made - reference
    right = (made > 0 if direction == "bullish" else made < 0) if sided else None
    beat = (excess > 0 if direction == "bullish" else excess < 0) if sided else None
    return ViewScore(
        run_id=f"run-{written}", arm="fake/a", ticker=ticker, session_date=written, direction=direction,
        confidence=confidence, horizon_days=3, held=held, sector=sector, regime=regime,
        starts_on=trading_day_after(written, 1), matures_on=trading_day_after(written, 3), status=status,
        return_pct=made if done else None, benchmark_return_pct=reference if done else None,
        excess_pct=excess if done else None, direction_right=right if done else None,
        beat_benchmark=beat if done else None,
    )


def test_the_scorecard_counts_what_was_right_and_what_beat_the_benchmark():
    card = scorecard("fake/a", [
        scored("bullish", 4.0, 1.0),                    # right, beat
        scored("bullish", 1.0, 3.0),                    # right, lagged
        scored("bullish", 2.0, 5.0),                    # right, lagged
        scored("bullish", -2.0, -5.0),                  # wrong, beat
        scored("bearish", -3.0, 1.0),                   # right, and it lagged as the view said
        scored("neutral", 9.0, 1.0),
        scored(status="pending"), scored(status="pending"), scored(status="unpriced"),
    ])
    assert (card.total, card.scored, card.pending, card.unpriced, card.neutral) == (9, 6, 2, 1, 1)
    overall = card.overall
    assert (overall.views, overall.right, overall.beat_benchmark) == (5, 4, 3)
    assert overall.hit_rate == 0.8 and overall.beat_rate == 0.6
    # Edge: +3, -2, -3 and +3 for the bullish ones, and +4 for the bearish one whose ticker lagged by 4.
    assert overall.mean_edge_pct == pytest.approx((3 - 2 - 3 + 3 + 4) / 5)
    assert overall.margin == pytest.approx(0.5 / 5 ** 0.5) and overall.mean_confidence == pytest.approx(0.7)


def test_a_bearish_view_on_something_that_beat_the_benchmark_counts_against_it():
    card = scorecard("fake/a", [scored("bearish", 5.0, 1.0)])
    assert card.overall.mean_edge_pct == pytest.approx(-4.0) and card.overall.right == 0


def test_views_of_one_day_are_counted_as_one_session():
    card = scorecard("fake/a", [scored(written=nov(9), ticker="AAA"), scored(written=nov(9), ticker="BBB"),
                                scored(written=nov(10), ticker="AAA"), scored(written=nov(11), status="pending")])
    assert card.sessions == 2


def test_nothing_scored_yet_gives_an_empty_scorecard_not_a_division_by_zero():
    card = scorecard("fake/a", [scored(status="pending"), scored("neutral")])
    assert card.overall is None and card.by_confidence == [] and (card.scored, card.neutral) == (1, 1)
    assert scorecard("fake/a", []).total == 0


@pytest.mark.parametrize("confidence, bucket", [
    (0.0, "below 0.50"), (0.49, "below 0.50"), (0.5, "0.50 to 0.59"), (0.59, "0.50 to 0.59"), (0.6, "0.60 to 0.69"),
    (0.7, "0.70 to 0.79"), (0.8, "0.80 to 0.89"), (0.89, "0.80 to 0.89"), (0.9, "0.90 to 1.00"), (1.0, "0.90 to 1.00"),
    (0.1 + 0.2 + 0.3, "0.60 to 0.69"),                  # 0.6000000000000001 in floating point
    (0.3 + 0.3 + 0.1, "0.70 to 0.79"),                  # 0.7 written as 0.7000000000000001 or just under
])
def test_confidence_buckets(confidence, bucket):
    assert confidence_bucket(confidence) == bucket


def test_the_scorecard_is_split_by_confidence_direction_sector_regime_and_position():
    card = scorecard("fake/a", [
        scored("bullish", 4.0, 1.0, confidence=0.9, sector="Energy", regime="risk_on", held=True),
        scored("bullish", -1.0, 1.0, confidence=0.9, sector="Energy", regime="risk_off"),
        scored("bearish", -3.0, 1.0, confidence=0.55, sector="", regime="none"),
    ])
    assert [(t.label, t.views, t.right) for t in card.by_confidence] == [("0.50 to 0.59", 1, 1), ("0.90 to 1.00", 2, 1)]
    assert [(t.label, t.views) for t in card.by_direction] == [("bullish", 2), ("bearish", 1)]
    assert [(t.label, t.views) for t in card.by_sector] == [("Energy", 2), ("unknown", 1)]
    assert [(t.label, t.views) for t in card.by_regime] == [("risk_on", 1), ("risk_off", 1), ("none", 1)]
    assert [(t.label, t.views, t.hit_rate) for t in card.by_position] == [("candidate", 2, 0.5), ("held", 1, 1.0)]


# ── Trades, from the account's fills ─────────────────────────────────────────

@pytest.fixture
def journal(tmp_path):
    book = Journal(tmp_path / "journal.sqlite3")
    with book.transaction():
        book.write("INSERT INTO paper_accounts VALUES ('fake/a', 100000, 100000, 100000, NULL)")
    yield book
    book.close()


def order(journal, order_id, ticker, side, decided, kind="market", shares=10):
    with journal.transaction():
        journal.write(
            "INSERT INTO paper_orders VALUES (?, 'fake/a', ?, ?, ?, ?, NULL, NULL, NULL, 0, '', 'filled', ?, NULL, NULL, '')",
            (order_id, ticker, side, kind, shares, decided.isoformat()),
        )


def fill(journal, order_id, ticker, side, shares, price, day, kind="market", pnl_pct=None):
    with journal.transaction():
        journal.write(
            "INSERT INTO paper_fills (account, order_id, ticker, side, kind, shares, price, fill_date, pnl_pct, note) "
            "VALUES ('fake/a', ?, ?, ?, ?, ?, ?, ?, ?, '')",
            (order_id, ticker, side, kind, shares, price, day.isoformat(), pnl_pct),
        )


def hold(journal, ticker, shares, avg_cost, last_close, sessions_held=3, sector="Energy"):
    with journal.transaction():
        journal.write(
            "INSERT INTO paper_positions VALUES ('fake/a', ?, ?, ?, '2026-11-10', ?, '2026-11-12', ?, NULL, ?, ?)",
            (ticker, shares, avg_cost, sector, last_close or avg_cost, last_close, sessions_held),
        )


def test_a_position_bought_and_sold_is_one_closed_trade(journal):
    order(journal, "b1", "AAA", "buy", nov(9))
    fill(journal, "b1", "AAA", "buy", 10, 100.0, nov(10))
    fill(journal, "s1", "AAA", "sell", 10, 110.0, nov(13), pnl_pct=10.0)

    [trade] = trades_of(journal, "fake/a", {"AAA": "Technology"})

    assert (trade.status, trade.decided_on, trade.entry_date, trade.entry_price, trade.shares) == (
        "closed", nov(9), nov(10), 100.0, 10)
    assert trade.realized_usd == pytest.approx(100.0) and trade.open_shares == 0 and not trade.stopped
    assert trade.sessions_held == 3 and trade.sector == "Technology"             # the 10th to the 13th
    assert [(e.exit_date, e.kind, e.shares, round(e.pnl_usd, 2)) for e in trade.exits] == [(nov(13), "market", 10, 100.0)]
    assert how_it_ended(trade) == "sold"


def test_a_position_sold_by_its_stop_is_marked_as_stopped_out(journal):
    order(journal, "b1", "AAA", "buy", nov(9))
    fill(journal, "b1", "AAA", "buy", 10, 100.0, nov(10))
    fill(journal, "b1|stop", "AAA", "sell", 10, 92.0, nov(11), kind="stop", pnl_pct=-8.0)

    [trade] = trades_of(journal, "fake/a")
    assert trade.stopped and trade.realized_usd == pytest.approx(-80.0) and how_it_ended(trade) == "stopped out"


def test_a_trimmed_position_is_still_open_and_valued_at_the_books_last_close(journal):
    order(journal, "b1", "AAA", "buy", nov(9))
    fill(journal, "b1", "AAA", "buy", 10, 100.0, nov(10))
    fill(journal, "s1", "AAA", "sell", 5, 104.0, nov(12), pnl_pct=4.0)
    hold(journal, "AAA", shares=5, avg_cost=100.0, last_close=106.0, sessions_held=4)

    [trade] = trades_of(journal, "fake/a")

    assert (trade.status, trade.open_shares, trade.sessions_held, trade.last_price) == ("open", 5, 4, 106.0)
    assert trade.realized_usd == pytest.approx(20.0) and trade.unrealized_usd == pytest.approx(30.0)
    assert trade.sector == "Energy" and how_it_ended(trade) == "still open"
    assert trade.exits[0].share_of_position == 0.5


def test_a_trim_after_a_split_is_still_half_of_what_was_held(journal):
    # Bought 10 at 100; a 2-for-1 split makes that 20 at 50; 10 of the 20 are sold at 55.
    order(journal, "b1", "AAA", "buy", nov(9))
    fill(journal, "b1", "AAA", "buy", 10, 100.0, nov(10))
    fill(journal, "s1", "AAA", "sell", 10, 55.0, nov(12), pnl_pct=10.0)
    hold(journal, "AAA", shares=10, avg_cost=50.0, last_close=56.0)

    [trade] = trades_of(journal, "fake/a")
    assert (trade.status, trade.open_shares) == ("open", 10)
    assert trade.exits[0].share_of_position == 0.5 and trade.realized_usd == pytest.approx(10 * (55.0 - 50.0))


def test_profit_on_a_sale_comes_from_the_brokers_own_record_so_a_split_does_not_distort_it(journal):
    # Bought 10 at 100; a 2-for-1 split makes that 20 at 50; all 20 sold at 55, 10% above cost.
    order(journal, "b1", "AAA", "buy", nov(9))
    fill(journal, "b1", "AAA", "buy", 10, 100.0, nov(10))
    fill(journal, "s1", "AAA", "sell", 20, 55.0, nov(13), pnl_pct=10.0)

    [trade] = trades_of(journal, "fake/a")
    assert trade.status == "closed" and trade.open_shares == 0
    assert trade.realized_usd == pytest.approx(20 * (55.0 - 50.0))


def test_buying_again_after_selling_out_starts_a_new_trade(journal):
    order(journal, "b1", "AAA", "buy", nov(9))
    order(journal, "b2", "AAA", "buy", nov(16))
    fill(journal, "b1", "AAA", "buy", 10, 100.0, nov(10))
    fill(journal, "s1", "AAA", "sell", 10, 90.0, nov(12), pnl_pct=-10.0)
    fill(journal, "b2", "AAA", "buy", 8, 95.0, nov(17))
    hold(journal, "AAA", shares=8, avg_cost=95.0, last_close=None)

    first, second = trades_of(journal, "fake/a")
    assert (first.status, first.decided_on, second.status, second.decided_on) == ("closed", nov(9), "open", nov(16))
    assert second.unrealized_usd is None and second.last_price is None          # no usable close: not valued


def test_a_sale_of_something_never_bought_is_left_out(journal):
    fill(journal, "s1", "ZZZ", "sell", 10, 90.0, nov(12), pnl_pct=-10.0)
    assert trades_of(journal, "fake/a") == []


def test_the_summary_counts_wins_losses_and_where_the_money_came_from(journal):
    for number, (ticker, bought, sold, kind, sector) in enumerate([
        ("AAA", 100.0, 110.0, "market", "Technology"), ("BBB", 100.0, 95.0, "stop", "Energy"),
        ("CCC", 100.0, 97.0, "stop", "Energy"),
    ]):
        order(journal, f"b{number}", ticker, "buy", nov(9))
        fill(journal, f"b{number}", ticker, "buy", 10, bought, nov(10))
        fill(journal, f"s{number}", ticker, "sell", 10, sold, nov(12), kind=kind, pnl_pct=(sold / bought - 1) * 100)
    order(journal, "b9", "DDD", "buy", nov(11))
    fill(journal, "b9", "DDD", "buy", 10, 50.0, nov(12))
    hold(journal, "DDD", shares=10, avg_cost=50.0, last_close=52.0, sector="Technology")

    summary = summarize("fake/a", trades_of(journal, "fake/a", {"AAA": "Technology", "BBB": "Energy", "CCC": "Energy"}))

    assert (summary.closed, summary.open, summary.winners, summary.losers, summary.stopped_out) == (3, 1, 1, 2, 2)
    assert summary.win_rate == pytest.approx(1 / 3)
    assert summary.realized_usd == pytest.approx(100 - 50 - 30) and summary.unrealized_usd == pytest.approx(20.0)
    assert summary.average_win_usd == pytest.approx(100.0) and summary.average_loss_usd == pytest.approx(-40.0)
    assert [(s.label, s.trades, round(s.pnl_usd, 2)) for s in summary.by_sector] == [
        ("Technology", 2, 120.0), ("Energy", 2, -80.0)]
    assert [(s.label, s.trades, round(s.pnl_usd, 2)) for s in summary.by_exit] == [
        ("sold", 1, 100.0), ("still open", 1, 20.0), ("stopped out", 2, -80.0)]
    assert [s.label for s in summary.by_ticker] == ["AAA", "DDD", "CCC", "BBB"]


def test_no_trades_gives_an_empty_summary(journal):
    summary = summarize("fake/a", trades_of(journal, "fake/a"))
    assert (summary.closed, summary.open, summary.win_rate, summary.average_win_usd) == (0, 0, None, None)


# ── Curves ───────────────────────────────────────────────────────────────────

def test_largest_fall_from_a_high():
    assert max_drawdown_pct([100, 110, 99, 105, 120, 108]) == pytest.approx(10.0)     # 110 -> 99
    assert max_drawdown_pct([100, 101, 102]) == 0.0
    assert max_drawdown_pct([]) is None


def test_the_accounts_curve_is_its_recorded_equity_against_the_cash_it_started_with(journal):
    with journal.transaction():
        for day, equity in [(nov(9), 100_000.0), (nov(10), 101_000.0), (nov(11), 98_980.0)]:
            journal.write("INSERT INTO paper_equity VALUES ('fake/a', ?, ?, 0, ?)", (day.isoformat(), equity, equity))

    curve, initial = account_curve(journal, "fake/a")

    assert initial == 100_000.0 and [point.session_date for point in curve.points] == [nov(9), nov(10), nov(11)]
    assert curve.return_pct == pytest.approx(-1.02) and curve.max_drawdown_pct == pytest.approx(2.0)


def test_an_arm_without_an_account_has_no_curve(journal):
    curve, initial = account_curve(journal, "nobody")
    assert initial is None and curve.points == [] and curve.return_pct is None


def test_the_benchmark_is_bought_at_the_open_after_the_first_run_and_held():
    # 100,000 at an open of 400 is 250 shares. A dividend of 2 a share on the 11th stays as cash.
    spy = bars({nov(9): (398, 399, 397, 399), nov(10): (400, 404, 400, 404), nov(11): (402, 403, 401, 402, 2.0),
                nov(12): (402, 408, 402, 408)})
    curve = benchmark_curve(Prices({"SPY": spy}), "SPY", [nov(9), nov(10), nov(11), nov(12)], 100_000.0)

    assert [(p.session_date, round(p.equity, 2)) for p in curve.points] == [
        (nov(9), 100_000.0), (nov(10), 101_000.0), (nov(11), 101_000.0), (nov(12), 102_500.0)]
    assert curve.return_pct == pytest.approx(2.5) and curve.max_drawdown_pct == 0.0


def test_a_benchmark_without_a_price_to_buy_at_has_no_curve():
    spy = bars({nov(9): (398, 399, 397, 399)})
    assert benchmark_curve(Prices({"SPY": spy}), "SPY", [nov(9), nov(10)], 100_000.0).points == []
    assert benchmark_curve(Prices({"SPY": spy}), "SPY", [nov(9)], 100_000.0).points == []


def a_table(written: date, candidates, buys=(), holdings=(), equity=100_000.0, confidence=0.6) -> SignalTable:
    return SignalTable(
        run_id=f"run-{written}", arm="fake/a", session_date=written,
        created_at=datetime(2026, 11, 1, tzinfo=timezone.utc), status="ok", snapshot_id="none", universe_size=5,
        screen=[ScreenRow(ticker=ticker, passed=True, avg_dollar_volume_20d=5e8) for ticker in candidates],
        candidates=list(candidates),
        account=Account(account="fake/a", session_date=written, cash=equity, positions_value=0.0, equity=equity,
                        initial_cash=equity, high_water=equity, drawdown_pct=0.0),
        holdings=[Position(ticker=ticker, shares=10, avg_cost=100.0, entry_date=written, last_close=100.0, value=1000.0)
                  for ticker in holdings],
        views=[a_view(written=written, ticker=ticker, confidence=confidence) for ticker in candidates],
        proposals=[TradeProposal(ticker=ticker, action="buy", confidence=confidence, reason="", status="accepted")
                   for ticker in buys],
    )


def test_the_candidates_are_held_in_equal_weight_and_swapped_at_each_open():
    # The run after the 9th's close names A and B; the run after the 10th's names only B.
    prices = Prices({
        "A": bars({nov(9): (10, 10, 10, 10), nov(10): (10, 11, 10, 11), nov(11): (12.1, 12.1, 12.1, 12.1)}),
        "B": bars({nov(9): (20, 20, 20, 20), nov(10): (20, 20, 19, 19), nov(11): (19, 20.9, 19, 20.9)}),
    })
    tables = [a_table(nov(9), ["A", "B"]), a_table(nov(10), ["B"])]

    curve = candidate_curve(tables, prices, [nov(9), nov(10), nov(11)], 100_000.0)

    # The 10th: A +10% and B -5% from open to close, +2.5% together.
    # Overnight into the 11th, still A and B: A +10% (11 to 12.1) and B flat, +5% together.
    # The 11th, B alone: +10% from open to close.
    assert [round(point.equity, 2) for point in curve.points] == [100_000.0, 102_500.0, 118_387.5]
    assert curve.return_pct == pytest.approx(18.3875)


def test_a_candidate_without_a_price_is_left_out_of_that_days_average():
    prices = Prices({"A": bars({nov(9): (10, 10, 10, 10), nov(10): (10, 11, 10, 11)})})      # nothing for B
    curve = candidate_curve([a_table(nov(9), ["A", "B"])], prices, [nov(9), nov(10)], 100_000.0)
    assert curve.points[-1].equity == pytest.approx(110_000.0)


def test_days_without_candidates_leave_the_money_where_it_is():
    prices = Prices({"A": bars({nov(9): (10, 10, 10, 10), nov(10): (10, 11, 10, 11)})})
    assert candidate_curve([], prices, [nov(9), nov(10)], 100_000.0).points[-1].equity == 100_000.0
    assert candidate_curve([a_table(nov(9), [])], prices, [nov(9), nov(10)], 100_000.0).return_pct == 0.0


# ── Replaying a position the way the paper broker would ──────────────────────

def held(prices_by_day: dict[date, tuple]) -> Prices:
    return Prices({"AAA": bars(prices_by_day)})


def run(prices, exits=(), end=nov(13), stop=90.0, shares=10, slippage=0.0):
    return replay(prices, "AAA", shares=shares, stop=stop, entry_date=nov(10), exits=list(exits), end=end,
                  slippage=slippage)


QUIET = {nov(10): (100, 101, 99, 100), nov(11): (100, 103, 99, 102), nov(12): (102, 106, 101, 105),
         nov(13): (105, 108, 104, 107)}


def test_a_position_never_sold_is_valued_at_the_last_close():
    assert run(held(QUIET)) == pytest.approx(10 * (107 - 100))
    assert run(held(QUIET), end=nov(11)) == pytest.approx(10 * (102 - 100))


def test_slippage_makes_the_buy_dearer_and_a_sale_cheaper():
    # Bought at 100.10; sold at the open of the 12th, 102 less 0.1%.
    made = run(held(QUIET), exits=[(nov(12), 1.0, "market")], slippage=0.001)
    assert made == pytest.approx(10 * (102 * 0.999 - 100 * 1.001))


def test_a_sale_at_the_open_follows_the_accounts_own_and_a_stop_out_is_held_to_that_days_close():
    assert run(held(QUIET), exits=[(nov(12), 1.0, "market")]) == pytest.approx(10 * (102 - 100))
    assert run(held(QUIET), exits=[(nov(12), 1.0, "stop")]) == pytest.approx(10 * (105 - 100))
    assert run(held(QUIET), exits=[(nov(12), 1.0, "close")]) == pytest.approx(10 * (105 - 100))


def test_a_trim_sells_its_share_and_the_rest_runs_on():
    # Half at the open of the 11th (100), the other half valued at the last close (107).
    assert run(held(QUIET), exits=[(nov(11), 0.5, "market")]) == pytest.approx(5 * 0 + 5 * 7)
    # Each sale is a share of what is held at that moment: half, then all of the half that is left.
    two = run(held(QUIET), exits=[(nov(11), 0.5, "market"), (nov(13), 1.0, "market")])
    assert two == pytest.approx(5 * 0 + 5 * (105 - 100))
    halves = run(held(QUIET), exits=[(nov(11), 0.5, "market"), (nov(13), 0.5, "market")])
    assert halves == pytest.approx(5 * 0 + 2.5 * (105 - 100) + 2.5 * (107 - 100))


def test_a_gap_below_the_stop_sells_at_the_open_and_a_dip_through_it_sells_at_the_stop():
    gap = {nov(10): (100, 101, 99, 100), nov(11): (88, 89, 85, 86), nov(12): (86, 120, 86, 120)}
    assert run(held(gap), end=nov(12)) == pytest.approx(10 * (88 - 100))
    dip = {nov(10): (100, 101, 99, 100), nov(11): (97, 98, 89, 95), nov(12): (95, 120, 95, 120)}
    assert run(held(dip), end=nov(12)) == pytest.approx(10 * (90 - 100))
    touch = {nov(10): (100, 101, 99, 100), nov(11): (97, 98, 90, 95), nov(12): (95, 96, 95, 96)}
    assert run(held(touch), end=nov(12)) == pytest.approx(10 * (90 - 100))       # a low exactly at the stop sells


def test_the_stop_is_checked_on_the_day_of_the_buy_too():
    first_day = {nov(10): (100, 101, 89, 95), nov(11): (95, 120, 95, 120)}
    assert run(held(first_day), end=nov(11)) == pytest.approx(10 * (90 - 100))


def test_on_the_day_of_the_buy_the_picks_own_stop_comes_before_anything_planned_for_the_close():
    # The account's pick was stopped out on the very day it was bought, so the schedule says: out that day.
    # A random pick whose own low went through its own stop is sold at that stop, as the broker would do,
    # and not kept until the close, where it had recovered.
    hit = {nov(10): (100, 101, 85, 99)}
    assert run(held(hit), exits=[(nov(10), 1.0, "stop")], end=nov(10)) == pytest.approx(10 * (90 - 100))
    spared = {nov(10): (100, 101, 91, 99)}
    assert run(held(spared), exits=[(nov(10), 1.0, "stop")], end=nov(10)) == pytest.approx(10 * (99 - 100))


def test_what_was_planned_for_a_day_without_a_bar_happens_on_the_next_day_that_has_one():
    gappy = {nov(10): (100, 101, 99, 100), nov(12): (103, 106, 101, 105), nov(13): (105, 120, 105, 120)}
    # Nothing on the 11th: the sale planned for its open is made at the open of the 12th.
    assert run(held(gappy), exits=[(nov(11), 1.0, "market")]) == pytest.approx(10 * (103 - 100))
    assert run(held(gappy), exits=[(nov(11), 1.0, "stop")]) == pytest.approx(10 * (105 - 100))


def test_a_buy_that_would_open_at_or_below_its_stop_is_not_made():
    # With slippage, a buy made at the stop and stopped out at once would lose the slippage twice: it is not made.
    at_the_stop = {nov(10): (90, 95, 88, 94), nov(11): (94, 120, 94, 120)}
    assert run(held(at_the_stop), end=nov(11), slippage=0.001) == 0.0
    assert run(held({nov(10): (89, 95, 88, 94), nov(11): (94, 120, 94, 120)}), end=nov(11), slippage=0.001) == 0.0
    just_above = {nov(10): (90.01, 95, 90.01, 94), nov(11): (94, 120, 94, 120)}
    assert run(held(just_above), end=nov(11)) == pytest.approx(10 * (120 - 90.01))


def test_a_stop_takes_everything_before_a_sale_planned_for_the_same_day():
    dip = {nov(10): (100, 101, 99, 100), nov(11): (97, 98, 89, 95), nov(12): (95, 96, 95, 96)}
    assert run(held(dip), exits=[(nov(11), 1.0, "market")], end=nov(12)) == pytest.approx(10 * (90 - 100))


def test_dividends_are_received_while_the_position_is_held():
    paying = {nov(10): (100, 100, 100, 100, 9.0), nov(11): (100, 100, 100, 100, 1.5),
              nov(12): (100, 100, 100, 100), nov(13): (100, 100, 100, 100, 2.0)}
    assert run(held(paying)) == pytest.approx(10 * (1.5 + 2.0))                  # not the one on the day of the buy
    assert run(held(paying), exits=[(nov(12), 1.0, "market")]) == pytest.approx(10 * 1.5)      # nor after it is sold


def test_a_day_without_a_usable_bar_is_passed_over_and_no_entry_price_means_no_replay():
    gappy = {nov(10): (100, 101, 99, 100), nov(12): (102, 106, 101, 105)}        # nothing on the 11th
    assert run(held(gappy), end=nov(12)) == pytest.approx(10 * 5)
    assert run(held({nov(11): (100, 101, 99, 100)}), end=nov(12)) is None
    assert replay(Prices({}), "AAA", shares=10, stop=90.0, entry_date=nov(10), exits=[], end=nov(12), slippage=0) is None


# ── What else the account could have bought ──────────────────────────────────

@pytest.fixture
def rules(monkeypatch):
    for name, value in {
        "SIZING_RISK_PCT": 1.0, "SIZING_STOP_ATR_MULTIPLE": 2.0, "SIZING_MIN_STOP_PCT": 1.0,
        "SIZING_STOP_MIN_ATR_MULTIPLE": 1.0, "SIZING_STOP_MAX_ATR_MULTIPLE": 4.0,
        "RISK_MAX_POSITION_PCT": 10.0, "RISK_MAX_SECTOR_PCT": 30.0, "RISK_MAX_POSITIONS": 10, "RISK_MIN_CASH_PCT": 10.0,
        "RISK_MAX_TRADE_RISK_PCT": 1.0, "RISK_MAX_NEW_POSITIONS_PER_DAY": 3, "RISK_MAX_DAILY_TURNOVER_PCT": 30.0,
        "RISK_DAILY_LOSS_LIMIT_PCT": 3.0, "RISK_MAX_DRAWDOWN_PCT": 10.0, "RISK_MIN_PRICE": 5.0,
        "RISK_MIN_DOLLAR_VOLUME": 20_000_000.0, "PAPER_SLIPPAGE_BPS": 0.0,
    }.items():
        monkeypatch.setattr(config, name, value)


DECIDED, BOUGHT, LAST = nov(9), nov(10), nov(13)


def steady(ends_at: float, level: float = 100.0) -> pd.DataFrame:
    """Flat at `level` with a daily range of 2% until the 9th, then a straight line to `ends_at` on the 13th."""
    rows = {day: (level, level * 1.01, level * 0.99, level) for day in trading_days(date(2026, 10, 1), DECIDED)}
    after = trading_days(BOUGHT, LAST)
    previous = level
    for index, day in enumerate(after, start=1):
        close = level + (ends_at - level) * index / len(after)
        rows[day] = (previous, max(previous, close), min(previous, close), close)
        previous = close
    return bars(rows)


def a_trade(ticker="A", decided=DECIDED, shares=100, unrealized=None, exits=(), status="open") -> Trade:
    return Trade(
        arm="fake/a", ticker=ticker, decided_on=decided, entry_date=trading_day_after(decided, 1) if decided else BOUGHT,
        entry_price=100.0, shares=shares, exits=list(exits), open_shares=0 if status == "closed" else shares,
        status=status, unrealized_usd=unrealized, realized_usd=sum(e.pnl_usd for e in exits),
    )


def test_each_candidate_is_sized_and_gated_like_a_real_buy(rules):
    # Equity 100,000 at confidence 0.6: 600 at risk. A range of 2 a day puts the stop 4 below 100, at 96:
    # 150 shares by the sizing rule, cut to 100 by the limit of 10% of equity in one position.
    prices = Prices({"A": steady(110), "B": steady(100), "HELD": steady(100)})
    found = options(a_table(DECIDED, ["A", "B", "HELD", "NOPRICE"], holdings=["HELD"]), prices, confidence=0.6,
                    sectors={}, limits=RiskLimits.from_config())
    assert found == {"A": (100, 96.0), "B": (100, 96.0)}                         # not what is held, nor what has no price


def test_a_candidate_the_analyst_gave_no_view_on_is_not_an_option(rules):
    # Its data was unusable that day, or the analysis failed: the pipeline could not have bought it.
    prices = Prices({"A": steady(110), "B": steady(100)})
    table = a_table(DECIDED, ["A", "B"])
    table.views = [view for view in table.views if view.ticker != "B"]
    assert list(options(table, prices, confidence=0.6, sectors={}, limits=RiskLimits.from_config())) == ["A"]


def test_a_view_that_was_not_bullish_still_leaves_the_candidate_in_the_pool(rules):
    # Telling the candidates apart is what is being tested, so the analyst's own verdict cannot shrink the pool.
    prices = Prices({"A": steady(110), "B": steady(100)})
    table = a_table(DECIDED, ["A", "B"])
    table.views[1].direction = "bearish"
    assert list(options(table, prices, confidence=0.6, sectors={}, limits=RiskLimits.from_config())) == ["A", "B"]


def test_the_stop_of_a_candidate_is_not_moved_by_dividends_paid_after_the_decision(rules):
    # A fetch made later has marked every earlier bar down for a dividend nobody knew of on the day.
    plain, later = steady(110), steady(110)
    later.loc[later.index <= DECIDED, "adj_close"] = later.loc[later.index <= DECIDED, "close"] * 0.97
    table = a_table(DECIDED, ["A"])
    limits = RiskLimits.from_config()
    assert options(table, Prices({"A": later}), confidence=0.6, sectors={}, limits=limits) == options(
        table, Prices({"A": plain}), confidence=0.6, sectors={}, limits=limits) == {"A": (100, 96.0)}


def test_a_candidate_the_gate_would_refuse_is_not_an_option(rules):
    prices = Prices({"A": steady(110), "CHEAP": steady(3.3, level=3.0)})         # under the minimum price of 5
    found = options(a_table(DECIDED, ["A", "CHEAP"]), prices, confidence=0.6, sectors={},
                    limits=RiskLimits.from_config())
    assert list(found) == ["A"]


def test_a_buy_was_sized_at_the_lower_of_the_managers_and_the_analysts_confidence():
    table = a_table(DECIDED, ["A", "B"], buys=["A"], confidence=0.6)
    table.proposals[0].confidence = 0.9
    assert sized_at(table, "A") == 0.6
    table.views[0].confidence = 0.95
    assert sized_at(table, "A") == 0.9
    assert sized_at(table, "B") is None                                         # a candidate the account did not buy


# ── Random picks from the same candidates ────────────────────────────────────
# Three candidates on the 9th, each sized at 100 shares bought at 100 on the 10th:
# A ends the 13th at 110 (+1,000), B at 100 (0), C at 98 (-200).

@pytest.fixture
def three(rules):
    return Prices({"A": steady(110), "B": steady(100), "C": steady(98)})


def test_the_accounts_pick_is_set_against_every_candidate_of_that_day(three):
    result = random_benchmark([a_table(DECIDED, ["A", "B", "C"], buys=["A"])], [a_trade("A", unrealized=1000.0)],
                              three, as_of=LAST, simulations=3000, seed=1)

    assert (result.entries, result.skipped, result.simulations, result.valued_on) == (1, 0, 3000, LAST)
    assert result.replayed_pnl_usd == pytest.approx(1000.0) and result.booked_pnl_usd == pytest.approx(1000.0)
    [pick] = result.picks
    assert (pick.ticker, pick.pool, pick.rank) == ("A", 3, 1) and pick.pool_mean_usd == pytest.approx(800 / 3)
    assert (pick.replayed_usd, pick.booked_usd) == (pytest.approx(1000.0), pytest.approx(1000.0))
    # A third of the draws are A itself (level with the account), two thirds are worse.
    assert result.percentile == pytest.approx(100 * (2 / 3 + 1 / 6), abs=3)
    assert result.mean_pnl_usd == pytest.approx(800 / 3, abs=30)
    assert result.quantiles_usd["5"] == pytest.approx(-200.0) and result.quantiles_usd["95"] == pytest.approx(1000.0)


def test_what_is_compared_is_the_choice_of_ticker_not_what_the_books_happen_to_say(three):
    # The same buy, but the books have it at five times the size and a different result: a stop the
    # analyst proposed, say, or a valuation from another day. None of that is about which ticker was chosen.
    tables = [a_table(DECIDED, ["A", "B", "C"], buys=["A"])]
    plain = random_benchmark(tables, [a_trade("A", unrealized=1000.0)], three, as_of=LAST, simulations=3000, seed=1)
    other = random_benchmark(tables, [a_trade("A", shares=500, unrealized=-4321.0)], three, as_of=LAST,
                             simulations=3000, seed=1)

    assert other.percentile == plain.percentile and other.replayed_pnl_usd == plain.replayed_pnl_usd
    assert other.booked_pnl_usd == pytest.approx(-4321.0) and other.picks[0].booked_usd == pytest.approx(-4321.0)
    assert other.picks[0].replayed_usd == pytest.approx(1000.0)


def test_open_positions_are_valued_where_the_books_stand_on_both_sides(three):
    # The books were last brought up to the 11th. A replay that ran on to the 13th would count two
    # more days of the market's move for the random picks than the account's own figure holds.
    tables, trades = [a_table(DECIDED, ["A", "B", "C"], buys=["A"])], [a_trade("A", unrealized=500.0)]
    result = random_benchmark(tables, trades, three, as_of=LAST, valued_on=nov(11), simulations=50, seed=1)

    assert result.valued_on == nov(11)
    assert result.replayed_pnl_usd == pytest.approx(500.0)                     # A closed the 11th at 105
    assert result.picks[0].pool_mean_usd == pytest.approx((500 + 0 - 100) / 3)
    later = random_benchmark(tables, trades, three, as_of=nov(12), valued_on=LAST, simulations=5, seed=1)
    assert later.valued_on == nov(12)                                           # never past the last completed session


def test_picking_the_worst_of_the_candidates_lands_at_the_bottom(three):
    result = random_benchmark([a_table(DECIDED, ["A", "B", "C"], buys=["C"])], [a_trade("C", unrealized=-200.0)],
                              three, as_of=LAST, simulations=3000, seed=1)
    assert result.picks[0].rank == 3 and result.percentile == pytest.approx(100 / 6, abs=3)


def test_candidates_that_all_did_the_same_put_the_account_in_the_middle(rules):
    prices = Prices({"A": steady(105), "B": steady(105), "C": steady(105)})
    result = random_benchmark([a_table(DECIDED, ["A", "B", "C"], buys=["A"])], [a_trade("A", unrealized=500.0)],
                              prices, as_of=LAST, simulations=200, seed=1)
    assert result.percentile == 50.0 and set(result.quantiles_usd.values()) == {500.0}


def test_the_same_seed_draws_the_same_picks_and_another_seed_others(three):
    tables, trades = [a_table(DECIDED, ["A", "B", "C"], buys=["A"])], [a_trade("A", unrealized=1000.0)]
    first = random_benchmark(tables, trades, three, as_of=LAST, simulations=50, seed=7)
    again = random_benchmark(tables, trades, three, as_of=LAST, simulations=50, seed=7)
    other = random_benchmark(tables, trades, three, as_of=LAST, simulations=50, seed=8)
    assert first == again and first.mean_pnl_usd != other.mean_pnl_usd


def test_two_buys_decided_on_one_day_are_replaced_by_two_different_tickers(three):
    # Without that, one draw in nine would buy A twice and make 2,000.
    trades = [a_trade("A", unrealized=1000.0), a_trade("B", unrealized=0.0)]
    result = random_benchmark([a_table(DECIDED, ["A", "B", "C"], buys=["A", "B"])], trades, three, as_of=LAST,
                              simulations=2000, seed=3)
    assert result.entries == 2 and result.quantiles_usd["95"] <= 1000.0 + 1e-6
    assert result.mean_pnl_usd == pytest.approx((1000 + 800 - 200) / 3, abs=40)


def test_a_random_pick_is_held_for_as_long_as_the_account_held_its_own(three):
    # The account sold A at the open of the 12th. Each candidate is sold at that open too:
    # A at 105 (+500), B at 100 (0), C at 99 (-100). Held to the 13th they would have made 1,000, 0 and -200.
    sold = TradeExit(exit_date=nov(12), price=105.0, shares=100, share_of_position=1.0, kind="market", pnl_usd=500.0,
                     pnl_pct=5.0)
    result = random_benchmark([a_table(DECIDED, ["A", "B", "C"], buys=["A"])],
                              [a_trade("A", exits=[sold], status="closed")], three, as_of=LAST, simulations=10, seed=1)
    assert result.replayed_pnl_usd == pytest.approx(500.0) and result.booked_pnl_usd == pytest.approx(500.0)
    assert result.picks[0].pool_mean_usd == pytest.approx((500 + 0 - 100) / 3)


def test_when_the_account_only_trimmed_a_random_pick_is_trimmed_by_the_same_share(three):
    # Half sold at the open of the 12th, the rest still held on the 13th.
    # A: 50 at 105 and 50 at 110, +750. B: nothing. C: 50 at 99 and 50 at 98, -150.
    half = TradeExit(exit_date=nov(12), price=105.0, shares=50, share_of_position=0.5, kind="market", pnl_usd=250.0,
                     pnl_pct=5.0)
    trade = a_trade("A", exits=[half], unrealized=500.0)
    result = random_benchmark([a_table(DECIDED, ["A", "B", "C"], buys=["A"])], [trade], three, as_of=LAST,
                              simulations=10, seed=1)
    assert result.replayed_pnl_usd == pytest.approx(750.0)
    assert result.picks[0].pool_mean_usd == pytest.approx((750 + 0 - 150) / 3)


def test_a_candidate_without_a_view_is_never_drawn(three):
    table = a_table(DECIDED, ["A", "B", "C"], buys=["A"])
    table.views = [view for view in table.views if view.ticker != "C"]         # C's data was unusable that day
    result = random_benchmark([table], [a_trade("A", unrealized=1000.0)], three, as_of=LAST, simulations=500, seed=1)
    assert result.picks[0].pool == 2 and result.quantiles_usd["5"] == pytest.approx(0.0)      # never C's -200


def test_dividends_the_account_was_paid_count_towards_what_its_pick_made(rules):
    paying = steady(100)
    paying.loc[nov(11), "dividends"] = 1.5
    result = random_benchmark([a_table(DECIDED, ["A"], buys=["A"])], [a_trade("A", unrealized=0.0)],
                              Prices({"A": paying}), as_of=LAST, simulations=5, seed=1)
    # The replay receives the dividend, and so did the account: its books show it as cash, not in the trade.
    assert result.replayed_pnl_usd == pytest.approx(150.0) and result.booked_pnl_usd == pytest.approx(150.0)
    assert result.picks[0].pool_mean_usd == pytest.approx(150.0)


def test_buys_that_cannot_be_replayed_are_counted_and_left_out(three):
    tables = [a_table(DECIDED, ["A", "B", "C", "NOPRICE"], buys=["A", "NOPRICE"])]
    trades = [
        a_trade("A", unrealized=1000.0),
        a_trade("B", unrealized=0.0),                   # a candidate that day, but no accepted buy to take its size from
        a_trade("A", decided=nov(5), unrealized=1.0),   # no run on record for the day it was decided
        a_trade("A", decided=None, unrealized=1.0),     # no record of when it was decided
        a_trade("NOPRICE", unrealized=77.0),            # bought, but with no prices it cannot be put through the replay
    ]
    result = random_benchmark(tables, trades, three, as_of=LAST, simulations=10, seed=1)
    assert (result.entries, result.skipped) == (1, 4)
    assert result.replayed_pnl_usd == pytest.approx(1000.0) and result.booked_pnl_usd == pytest.approx(1000.0)


def test_with_nothing_to_replay_there_is_no_percentile(three):
    empty = random_benchmark([], [], three, as_of=LAST, simulations=10, seed=1)
    assert (empty.entries, empty.percentile, empty.replayed_pnl_usd, empty.picks) == (0, None, None, [])
    none = random_benchmark([a_table(DECIDED, ["A", "B", "C"], buys=["A"])], [a_trade("A", unrealized=1000.0)],
                            three, as_of=LAST, simulations=0, seed=1)
    assert none.entries == 1 and none.percentile is None and none.replayed_pnl_usd == pytest.approx(1000.0)
