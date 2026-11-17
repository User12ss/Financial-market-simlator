"""Three things to hold the paper account against. All of them are plain code: no model is asked anything.

- The benchmark bought and held: what doing nothing clever would have made.
- The day's candidates in equal weight: what the screen alone is worth,
  before any model has an opinion.
- Random picks from the same candidates: the account's own buys replaced, a
  thousand times over, by picks drawn at random from that day's candidates.
  Where the account's picks fall among those outcomes says how likely it is
  that they were no better than chance.

The first two are fully invested from the first day, which the account never
is: it keeps cash and opens a few positions a day. Read them as references,
not as like-for-like rivals.

The random picks are meant to be like for like, and for that the account's
own ticker is put through exactly what every other candidate is put through:
sized by the sizing rule and the risk gate against the account as it stood
that day, bought at the same open, protected by the rule's own stop, held on
the account's schedule and valued on the same day. The pool is the day's
candidates the analyst gave a view on, the only ones the pipeline could buy.
The account's books are not what is compared: the real position may have
been stopped where the analyst proposed, cut because of another order placed
the same day, or last valued on an earlier day, and none of that is about
which ticker was chosen. The books' figure is reported beside the comparison.

One thing besides the ticker is not neutral: the schedule. A random pick is
sold when the account sold its own pick, and that moment was decided by what
happened to the account's ticker, not to the random one. There is no way to
hold a pick "as long as the account held its own" without this, and no
reason to expect it to lean one way.
"""

from __future__ import annotations

import random
from bisect import bisect_left
from datetime import date

import config
from data import indicators
from data.market_calendar import trading_days
from data.snapshot import _adjusted
from evaluation.prices import Prices
from journal.store import Journal
from models.evaluation import Curve, EquityPoint, PickResult, RandomBenchmark, Trade
from models.signals import SignalTable, TickerView, TradeProposal
from portfolio.sizing import Quote, size_orders
from risk.gate import MarketFacts, RiskLimits, review

QUANTILES = (5, 25, 50, 75, 95)
# A draw that picks the very tickers the account bought adds up the same
# numbers in the same order. This only allows for the last decimal.
LEVEL_USD = 1e-6


# ── Curves ───────────────────────────────────────────────────────────────────

def max_drawdown_pct(values: list[float]) -> float | None:
    """The largest fall from an earlier high, as a positive percent."""
    if not values:
        return None
    high, worst = values[0], 0.0
    for value in values:
        high = max(high, value)
        worst = max(worst, (high - value) / high * 100 if high > 0 else 0.0)
    return worst


def _curve(name: str, description: str, start: float, points: list[EquityPoint]) -> Curve:
    values = [start] + [point.equity for point in points]
    return Curve(
        name=name, description=description, points=points,
        return_pct=(values[-1] / start - 1) * 100 if points and start > 0 else None,
        max_drawdown_pct=max_drawdown_pct(values) if points else None,
    )


def account_curve(journal: Journal, arm: str) -> tuple[Curve, float | None]:
    """The paper account's equity at each session's close, and the cash it started with."""
    account = journal.rows("SELECT initial_cash FROM paper_accounts WHERE account = ?", (arm,))
    points = [
        EquityPoint(session_date=date.fromisoformat(row["session_date"]), equity=row["equity"])
        for row in journal.rows(
            "SELECT session_date, equity FROM paper_equity WHERE account = ? ORDER BY session_date", (arm,)
        )
    ]
    initial = account[0]["initial_cash"] if account else None
    description = "The paper account: cash and positions at each session's close."
    if initial is None:
        return Curve(name="Paper account", description=description), None
    return _curve("Paper account", description, initial, points), initial


def benchmark_curve(prices: Prices, symbol: str, days: list[date], initial: float) -> Curve:
    """All of the starting cash put into `symbol` at the open of the second day, and left there."""
    description = (
        f"{symbol} bought with all of the starting cash at the open of the first session after the account's "
        "first run, and held. Dividends are kept as cash."
    )
    if len(days) < 2:
        return Curve(name=f"{symbol} bought and held", description=description)
    bought_on = days[1]
    paid = prices.price(symbol, bought_on, "open")
    if paid is None:
        return Curve(name=f"{symbol} bought and held", description=description)
    shares, points = initial / paid, [EquityPoint(session_date=days[0], equity=initial)]
    for day in days[1:]:
        close = prices.price(symbol, day, "close")
        if close is not None:
            points.append(EquityPoint(session_date=day, equity=shares * (close + prices.dividends(symbol, bought_on, day))))
    return _curve(f"{symbol} bought and held", description, initial, points)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def candidate_curve(tables: list[SignalTable], prices: Prices, days: list[date], initial: float) -> Curve:
    """Each day's candidates in equal weight, bought at the open and swapped for the next day's at the next open."""
    description = (
        "The screen's candidates in equal weight, fully invested: each session's list is bought at the next open "
        "and replaced by the following list at the open after that. No costs."
    )
    name = "Candidates in equal weight"
    if len(days) < 2:
        return Curve(name=name, description=description)
    sessions = [table.session_date for table in tables]

    def basket(day: date) -> list[str]:
        """The candidates of the last run before `day`."""
        index = bisect_left(sessions, day) - 1
        return tables[index].candidates if index >= 0 else []

    equity, points = initial, [EquityPoint(session_date=days[0], equity=initial)]
    held: list[str] = []
    previous = days[0]
    for day in days[1:]:
        overnight = []
        for ticker in held:                             # yesterday's basket, from its close to today's open
            close, open_ = prices.price(ticker, previous, "close"), prices.price(ticker, day, "open")
            if close is not None and open_ is not None:
                overnight.append((open_ + prices.dividend(ticker, day)) / close - 1)
        equity *= 1 + _mean(overnight)
        held = basket(day)
        today = []
        for ticker in held:                             # today's basket, from the open to the close
            open_, close = prices.price(ticker, day, "open"), prices.price(ticker, day, "close")
            if open_ is not None and close is not None:
                today.append(close / open_ - 1)
        equity *= 1 + _mean(today)
        points.append(EquityPoint(session_date=day, equity=equity))
        previous = day
    return _curve(name, description, initial, points)


# ── Random picks ─────────────────────────────────────────────────────────────

def replay(
    prices: Prices, ticker: str, *, shares: float, stop: float, entry_date: date,
    exits: list[tuple[date, float, str]], end: date, slippage: float,
) -> float | None:
    """Profit in dollars of holding `ticker` the way the paper broker would, on a given schedule.

    Bought at the open of `entry_date`, protected by `stop`, and sold on the
    schedule in `exits`: each entry is a date, the share of what is then
    held to sell, and how the account's own pick left that day. Where the account sold
    at the open, so does this; where its pick was stopped out or closed by
    the broker, this is held to that day's close. What is left at `end` is
    valued at the last close. None when the ticker has no usable price to be
    bought at.

    The order of events within a day is the broker's. On a day after the
    buy: the dividend, then the stop (at the open when the day opens at or
    below it, at the stop when the low reaches it), then the schedule. On
    the day of the buy there is no dividend and no gap to check, but the low
    is still checked against the stop before anything the schedule holds
    for that day. A day without a usable bar is passed over, and what the
    schedule held for it happens on the next day that has one.
    """
    first_open = prices.price(ticker, entry_date, "open")
    if first_open is None:
        return None
    if first_open <= stop:
        return 0.0                                      # the broker does not make a buy that opens at or below its stop
    entry = first_open * (1 + slippage)
    waiting = sorted(exits)
    held, profit, last_close = float(shares), 0.0, None
    for day in trading_days(entry_date, end):
        open_, low, close = (prices.price(ticker, day, column) for column in ("open", "low", "close"))
        if open_ is None or low is None or close is None:
            continue
        if day > entry_date:
            profit += held * prices.dividend(ticker, day)
            if open_ <= stop:
                profit += held * (open_ * (1 - slippage) - entry)
                held = 0.0
                break
        if low <= stop:
            profit += held * (stop * (1 - slippage) - entry)
            held = 0.0
            break
        due, waiting = [exit_ for exit_ in waiting if exit_[0] <= day], [exit_ for exit_ in waiting if exit_[0] > day]
        for _, fraction, kind in due:
            quantity = held * fraction
            price = open_ * (1 - slippage) if kind in ("market", "limit") else close
            profit += quantity * (price - entry)
            held -= quantity
        if held <= 1e-9:
            held = 0.0
            break
        last_close = close
    if held > 0 and last_close is not None:
        profit += held * (last_close - entry)
    return profit


def _atr(prices: Prices, ticker: str, day: date) -> float | None:
    """The 14-day ATR as of `day`, as the pipeline's snapshot of that day would have had it.

    The snapshot works on prices adjusted for the dividends paid up to its
    own day. A fetch made today has also adjusted every earlier bar for the
    dividends paid since, which nobody knew of then. Putting the series back
    on the scale where the bar of `day` is at its own close removes exactly
    those, and leaves the share scale of today's fetch untouched.
    """
    frame = prices.frame(ticker)
    if frame is None:
        return None
    past = frame[frame.index <= day]
    if past.empty:
        return None
    adjusted = _adjusted(past)
    value = indicators.atr(adjusted["high"], adjusted["low"], adjusted["close"])
    close, adj_close = float(past["close"].iloc[-1]), float(past["adj_close"].iloc[-1])
    if value is None or not (close > 0 and adj_close > 0):
        return value
    return value * close / adj_close


def options(
    table: SignalTable, prices: Prices, *, confidence: float, sectors: dict[str, str], limits: RiskLimits,
) -> dict[str, tuple[int, float]]:
    """What the account could have bought that day in place of its pick: shares and stop for each candidate.

    Only candidates the analyst gave a view on that day are options: without
    a view (the data was unusable, or the analysis failed or was refused)
    the pipeline cannot buy a ticker, so a random pick must not either. Which
    way the view pointed does not matter here: telling the candidates apart
    is the very thing being tested.

    Each of them, the account's own pick included, is put through the same
    sizing and the same risk gate as a real buy, at the confidence the real
    buy was sized at, against the account and the positions as they stood
    after that day's close. Each is judged alone: the other orders the
    account placed that day are not counted, and the limits are today's
    settings. The stop is the sizing rule's, for every one of them.
    """
    if table.account is None:
        return {}
    session = table.session_date
    positions = {holding.ticker: holding for holding in table.holdings}
    volume = {row.ticker: row.avg_dollar_volume_20d for row in table.screen}
    viewed = {view.ticker for view in table.views}
    found: dict[str, tuple[int, float]] = {}
    for ticker in table.candidates:
        if ticker not in viewed:
            continue
        if ticker in positions:
            continue                                    # a position is opened once: the account could not buy it again
        close, atr = prices.price(ticker, session, "close"), _atr(prices, ticker, session)
        if close is None or atr is None:
            continue
        view = TickerView(
            ticker=ticker, session_date=session, held=False, direction="bullish", confidence=confidence,
            horizon_days=1, thesis="", catalysts="", invalidation="", remark="",
        )
        proposal = TradeProposal(ticker=ticker, action="buy", confidence=confidence, reason="", status="accepted")
        requests, _ = size_orders(
            [proposal], session=session, account=table.account, positions=positions, views={ticker: view},
            quotes={ticker: Quote(close=close, atr=atr)},
        )
        if not requests:
            continue
        [decision] = review(
            requests, account=table.account, positions=positions, open_orders=[],
            facts={ticker: MarketFacts(usable=True, close=close, avg_dollar_volume=volume.get(ticker),
                                       sector=sectors.get(ticker, ""))},
            universe={ticker}, limits=limits,
        )
        if decision.approved_shares >= 1:
            found[ticker] = (decision.approved_shares, requests[0].protective_stop)
    return found


def sized_at(table: SignalTable, ticker: str) -> float | None:
    """The confidence the account's buy of `ticker` was sized at: the lower of the manager's and the analyst's."""
    proposal = next((p for p in table.proposals if p.ticker == ticker and p.action == "buy" and p.status == "accepted"), None)
    view = next((v for v in table.views if v.ticker == ticker), None)
    if proposal is None or view is None:
        return None
    return min(proposal.confidence, view.confidence)


def _schedule(trade: Trade) -> list[tuple[date, float, str]]:
    """When the account sold, and how much of what it then held: the schedule a replay follows."""
    return [(exit_.exit_date, exit_.share_of_position, exit_.kind) for exit_ in trade.exits]


def _dividends_received(trade: Trade, prices: Prices, end: date) -> float:
    """Dividends the account was paid on a trade: they went to its cash, not into the trade's own profit."""
    total = 0.0
    for day in trading_days(trade.entry_date, end)[1:]:
        per_share = prices.dividend(trade.ticker, day)
        if per_share:
            held = trade.shares - sum(exit_.shares for exit_ in trade.exits if exit_.exit_date < day)
            total += max(0, held) * per_share
    return total


def _quantile(ordered: list[float], percent: float) -> float:
    position = (len(ordered) - 1) * percent / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def random_benchmark(
    tables: list[SignalTable], trades: list[Trade], prices: Prices, *, as_of: date, valued_on: date | None = None,
    sectors: dict[str, str] | None = None, simulations: int | None = None, seed: int | None = None,
) -> RandomBenchmark:
    """Where the account's picks fall among random picks from the same candidates.

    `valued_on` is the session the account's books stand at. A position
    still open is valued there on both sides, and never later than `as_of`:
    a replay that ran on past the books would include a day's move the
    account's own figure does not.
    """
    simulations = config.EVALUATION_SIMULATIONS if simulations is None else simulations
    seed = config.EVALUATION_SEED if seed is None else seed
    sectors, limits = sectors or {}, RiskLimits.from_config()
    slippage = config.PAPER_SLIPPAGE_BPS / 10_000
    by_session = {table.session_date: table for table in tables}
    last = min(as_of, valued_on) if valued_on else as_of

    # What every eligible candidate, the account's own pick among them, would have made in
    # place of each of the account's buys.
    entries: list[tuple[Trade, dict[str, float]]] = []
    result = RandomBenchmark(simulations=simulations, seed=seed, valued_on=last)
    replayed_total = booked_total = 0.0
    for trade in trades:
        table = by_session.get(trade.decided_on) if trade.decided_on else None
        confidence = sized_at(table, trade.ticker) if table else None
        if table is None or confidence is None:
            result.skipped += 1
            continue
        end = trade.exits[-1].exit_date if trade.status == "closed" else last
        outcomes: dict[str, float] = {}
        for ticker, (shares, stop) in options(table, prices, confidence=confidence, sectors=sectors, limits=limits).items():
            made = replay(prices, ticker, shares=shares, stop=stop, entry_date=trade.entry_date,
                          exits=_schedule(trade), end=end, slippage=slippage)
            if made is not None:
                outcomes[ticker] = made
        own = outcomes.get(trade.ticker)
        if own is None:
            # The pick itself cannot be put through the replay (no usable prices, or today's
            # limits would not allow it), so there is nothing to compare like for like.
            result.skipped += 1
            continue
        booked = trade.realized_usd + (trade.unrealized_usd or 0.0) + _dividends_received(trade, prices, end)
        replayed_total += own
        booked_total += booked
        result.picks.append(PickResult(
            ticker=trade.ticker, decided_on=trade.decided_on, entry_date=trade.entry_date, replayed_usd=own,
            booked_usd=booked, pool=len(outcomes), pool_mean_usd=_mean(list(outcomes.values())),
            rank=1 + sum(1 for value in outcomes.values() if value > own),
        ))
        entries.append((trade, outcomes))

    result.entries = len(entries)
    if not entries:
        return result
    result.replayed_pnl_usd, result.booked_pnl_usd = replayed_total, booked_total
    if simulations < 1:
        return result

    generator = random.Random(seed)
    totals = []
    for _ in range(simulations):
        total, taken = 0.0, {}
        for trade, outcomes in entries:
            # Two buys decided on the same day are two different tickers, as the account's own are.
            used = taken.setdefault(trade.decided_on, set())
            free = sorted(ticker for ticker in outcomes if ticker not in used) or sorted(outcomes)
            pick = generator.choice(free)
            used.add(pick)
            total += outcomes[pick]
        totals.append(total)
    totals.sort()
    below = sum(1 for total in totals if total < replayed_total - LEVEL_USD)
    level = sum(1 for total in totals if abs(total - replayed_total) <= LEVEL_USD)
    result.mean_pnl_usd = _mean(totals)
    result.quantiles_usd = {str(percent): _quantile(totals, percent) for percent in QUANTILES}
    result.percentile = (below + level / 2) / len(totals) * 100
    return result
