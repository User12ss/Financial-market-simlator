"""The paper account's trades, put back together from its fills.

A trade is one position from its first buy to its last sell. Profit on what
was sold is the price it was sold at less what it cost, share for share. A
split between the buy and the sale changes both the number of shares and the
cost of each; the broker's record of the sale shows when that has happened,
and the cost is then taken from that record. What is still held is valued at
the broker's last close. Dividends are paid to the account as cash and are
not part of a trade's profit here.
"""

from __future__ import annotations

from datetime import date

from data.market_calendar import trading_days
from journal.store import Journal
from models.evaluation import Slice, Trade, TradeExit, TradeSummary

# The broker rounds the percent made on a sale to two decimals, which moves the
# cost implied by it by at most 0.005%. A cost further than this from the one
# tracked here means the shares have been split.
SPLIT_TOLERANCE = 0.005


def trades_of(journal: Journal, arm: str, sectors: dict[str, str] | None = None) -> list[Trade]:
    """Every trade of an arm's paper account, in the order the positions were opened."""
    sectors = sectors or {}
    decided = {
        row["order_id"]: date.fromisoformat(row["created_session"])
        for row in journal.rows("SELECT order_id, created_session FROM paper_orders WHERE account = ?", (arm,))
    }
    held = {
        row["ticker"]: row
        for row in journal.rows("SELECT * FROM paper_positions WHERE account = ?", (arm,))
    }
    trades: list[Trade] = []
    current: dict[str, Trade] = {}
    cost: dict[str, float] = {}                         # per share of what is still held, on today's share scale
    for fill in journal.rows("SELECT * FROM paper_fills WHERE account = ? ORDER BY fill_date, id", (arm,)):
        ticker, day = fill["ticker"], date.fromisoformat(fill["fill_date"])
        trade = current.get(ticker)
        if fill["side"] == "buy":
            if trade is None:
                trade = Trade(
                    arm=arm, ticker=ticker, sector=sectors.get(ticker, ""), decided_on=decided.get(fill["order_id"]),
                    entry_date=day, entry_price=fill["price"], shares=fill["shares"], open_shares=fill["shares"],
                    status="open",
                )
                trades.append(trade)
                current[ticker], cost[ticker] = trade, fill["price"]
            else:                                       # more of a position already open
                total = trade.shares + fill["shares"]
                trade.entry_price = (trade.entry_price * trade.shares + fill["price"] * fill["shares"]) / total
                cost[ticker] = (
                    (cost[ticker] * trade.open_shares + fill["price"] * fill["shares"])
                    / (trade.open_shares + fill["shares"])
                )
                trade.shares, trade.open_shares = total, trade.open_shares + fill["shares"]
            continue
        if trade is None:
            continue                                    # a sale of something never bought: nothing to attach it to
        # The broker records what each sale made against its cost per share at that moment, to
        # the hundredth of a percent. That is too coarse to take the profit from, but it shows
        # when the cost per share has moved since the buy, which only a split does.
        if fill["pnl_pct"] is not None and fill["pnl_pct"] > -100:
            implied = fill["price"] / (1 + fill["pnl_pct"] / 100)
            if abs(implied / cost[ticker] - 1) > SPLIT_TOLERANCE:
                trade.open_shares = round(trade.open_shares * cost[ticker] / implied)
                cost[ticker] = implied
        profit = fill["shares"] * (fill["price"] - cost[ticker])
        trade.exits.append(TradeExit(
            exit_date=day, price=fill["price"], shares=fill["shares"],
            share_of_position=min(1.0, fill["shares"] / trade.open_shares) if trade.open_shares > 0 else 1.0,
            kind=fill["kind"], pnl_usd=profit, pnl_pct=fill["pnl_pct"],
        ))
        trade.realized_usd += profit
        trade.stopped = trade.stopped or fill["kind"] == "stop"
        trade.open_shares -= fill["shares"]
        if trade.open_shares <= 0:                      # at or below: a split can leave more to sell than was bought
            trade.open_shares, trade.status = 0, "closed"
            trade.sessions_held = max(0, len(trading_days(trade.entry_date, day)) - 1)
            del current[ticker], cost[ticker]

    for ticker, trade in current.items():
        position = held.get(ticker)
        if position is None:
            continue                                    # the fills say open, the book says gone: leave it as the fills have it
        trade.open_shares = position["shares"]          # the book's count is on today's share scale
        trade.sector = trade.sector or position["sector"]
        trade.sessions_held = position["sessions_held"]
        trade.last_price = position["last_close"]
        if position["last_close"] is not None:
            trade.unrealized_usd = position["shares"] * (position["last_close"] - position["avg_cost"])
    return trades


def _slices(trades: list[Trade], label) -> list[Slice]:
    groups: dict[str, list[Trade]] = {}
    for trade in trades:
        groups.setdefault(label(trade), []).append(trade)
    slices = [
        Slice(label=name, trades=len(group), pnl_usd=sum(t.realized_usd + (t.unrealized_usd or 0.0) for t in group))
        for name, group in groups.items()
    ]
    return sorted(slices, key=lambda item: (-item.pnl_usd, item.label))


def how_it_ended(trade: Trade) -> str:
    if trade.status == "open":
        return "still open"
    if trade.stopped:
        return "stopped out"
    if any(exit_.kind == "close" for exit_ in trade.exits):
        return "closed by the broker"
    return "sold"


def summarize(arm: str, trades: list[Trade]) -> TradeSummary:
    closed = [trade for trade in trades if trade.status == "closed"]
    wins = [trade.realized_usd for trade in closed if trade.realized_usd > 0]
    losses = [trade.realized_usd for trade in closed if trade.realized_usd < 0]
    return TradeSummary(
        arm=arm, trades=trades, closed=len(closed), open=len(trades) - len(closed),
        winners=len(wins), losers=len(losses), win_rate=len(wins) / len(closed) if closed else None,
        stopped_out=sum(1 for trade in closed if trade.stopped),
        realized_usd=sum(trade.realized_usd for trade in trades),
        unrealized_usd=sum(trade.unrealized_usd or 0.0 for trade in trades),
        average_win_usd=sum(wins) / len(wins) if wins else None,
        average_loss_usd=sum(losses) / len(losses) if losses else None,
        by_ticker=_slices(trades, lambda trade: trade.ticker),
        by_sector=_slices(trades, lambda trade: trade.sector or "unknown"),
        by_exit=_slices(trades, how_it_ended),
    )
