"""The paper broker: a simulated account matched against daily bars.

No real money and no real orders. Its books live in the journal's SQLite
file, one account per model configuration, and every change is saved in the
same transaction as the fills that caused it.

Matching rules, per session, in this order:

1. Dividends are paid in cash on positions held before the ex-date.
2. Stop orders on positions held before the session: if the session opens at
   or below the stop, the fill is at the open; otherwise, if the low reaches
   the stop, the fill is at the stop.
3. Limit orders: a buy fills when the low reaches the limit, a sell when the
   high does, at the limit or the open, whichever is better. A day-only order
   that does not fill on its first session expires.
4. Market orders fill at the open, sells before buys. A buy the account
   cannot pay for is rejected whole.
5. A position opened in this session is then checked against its own stop,
   using the session's low.

A buy whose fill price would already be at or below its own protective stop
is not made: the stop was set from the last close, and a session that opens
through it has undone the reason for the trade before it began.

Stops come before limits, so when both could have triggered in one session
the stop is taken to have triggered first. Orders fill whole or not at all.
Market and stop fills are made worse by the configured slippage.

Share scale. The price source restates every earlier bar after a split, so
all prices in one snapshot are on the share scale of that snapshot's
session. Each position records which session's scale it is on and what the
ticker closed at then. Before anything is matched, every position, with its
open orders, is brought onto the scale of the snapshot in hand, and only when
the listed splits and the restated prices agree. A position that syncs have
found out of step for longer than the limit is closed at the last close it
could be matched to; that also ends a position in a delisted ticker.

Known simplifications: no partial fills, no intraday path (a session is its
open, high, low and close), no borrowing, no fees. A session on which a
ticker could not be priced is not gone back to: its orders and its stop are
taken up on the next session it can be priced, at that session's prices.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

import config
from data.market_calendar import next_trading_day
from data.models import Snapshot, TickerSnapshot
from journal.store import Journal
from models.trading import Account, Fill, Order, OrderRequest, Position

# How far a restated close may be from the recorded one, as a fraction. Within
# the first it is rounding. Up to the second it is taken to be the source
# correcting a price, and is accepted with a note. Beyond that the listed
# splits and the prices are taken to disagree.
SCALE_TOLERANCE = 0.01
CORRECTION_LIMIT = 0.05


class SyncResult(BaseModel):
    fills: list[Fill] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)   # things that went wrong and need to be seen
    notices: list[str] = Field(default_factory=list)    # ordinary events: dividends, expiries, buys not made


@dataclass
class _Lot:
    ticker: str
    shares: int
    avg_cost: float
    entry_date: date
    sector: str
    last_adjusted: date
    anchor_close: float
    out_of_sync_since: date | None = None
    last_close: float | None = None
    sessions_held: int = 0
    mark: float = 0.0        # the latest close it was valued at during a sync; not stored


@dataclass
class _Bar:
    open: float
    high: float
    low: float
    close: float


# The data gives a fund no sector. A fund that holds one sector's stocks is counted under that
# sector, in the words the data uses for the stocks themselves, so that the limit on a sector cannot
# be passed by buying its fund on top of its stocks. A fund of the whole market (SPY, QQQ, IWM)
# belongs to no sector and is left without one.
SECTOR_FUNDS: dict[str, str] = {
    "XLB": "Basic Materials", "XLC": "Communication Services", "XLE": "Energy", "XLF": "Financial Services",
    "XLI": "Industrials", "XLK": "Technology", "XLP": "Consumer Defensive", "XLRE": "Real Estate",
    "XLU": "Utilities", "XLV": "Healthcare", "XLY": "Consumer Cyclical",
}


def sector_of(item: TickerSnapshot | None) -> str:
    """The sector a ticker is counted under. Empty when the data does not say and it is not a sector fund."""
    if item is None:
        return ""
    stated = item.fundamentals.sector if item.fundamentals else ""
    return stated or SECTOR_FUNDS.get(item.ticker, "")


class PaperBroker:
    def __init__(
        self, journal: Journal, account: str, initial_cash: float | None = None,
        slippage_bps: float | None = None, stale_days: int | None = None,
    ) -> None:
        self.journal = journal
        self.name = account
        self.slippage = (config.PAPER_SLIPPAGE_BPS if slippage_bps is None else slippage_bps) / 10_000
        self.stale_days = config.PAPER_STALE_DAYS if stale_days is None else stale_days
        if not journal.rows("SELECT 1 FROM paper_accounts WHERE account = ?", (account,)):
            cash = config.PAPER_INITIAL_CASH if initial_cash is None else initial_cash
            with journal.transaction():
                journal.write("INSERT INTO paper_accounts VALUES (?, ?, ?, ?, NULL)", (account, cash, cash, cash))

    # ── Reading the books ────────────────────────────────────────────────

    def _lots(self) -> dict[str, _Lot]:
        return {
            row["ticker"]: _Lot(
                row["ticker"], row["shares"], row["avg_cost"], date.fromisoformat(row["entry_date"]),
                row["sector"], date.fromisoformat(row["last_adjusted"]), row["anchor_close"],
                date.fromisoformat(row["out_of_sync_since"]) if row["out_of_sync_since"] else None,
                row["last_close"], row["sessions_held"], row["anchor_close"],
            )
            for row in self.journal.rows(
                "SELECT * FROM paper_positions WHERE account = ? ORDER BY ticker", (self.name,)
            )
        }

    @staticmethod
    def _order(row) -> Order:
        return Order(
            order_id=row["order_id"], ticker=row["ticker"], side=row["side"], kind=row["kind"],
            shares=row["shares"], limit_price=row["limit_price"], stop_price=row["stop_price"],
            protective_stop=row["protective_stop"], day_only=bool(row["day_only"]), reason=row["reason"],
            status=row["status"], created_session=date.fromisoformat(row["created_session"]),
            filled_price=row["filled_price"],
            filled_date=date.fromisoformat(row["filled_date"]) if row["filled_date"] else None, note=row["note"],
        )

    def orders(self, open_only: bool = True) -> list[Order]:
        status = "AND status = 'open'" if open_only else ""
        return [self._order(row) for row in self.journal.rows(
            f"SELECT * FROM paper_orders WHERE account = ? {status} ORDER BY created_session, rowid", (self.name,)
        )]

    def positions(self) -> dict[str, Position]:
        stops = {order.ticker: order.stop_price for order in self.orders() if order.kind == "stop"}
        result = {}
        for lot in self._lots().values():
            # An unpriced position is carried at the last close it could be matched to.
            price = lot.last_close if lot.last_close is not None else lot.anchor_close
            result[lot.ticker] = Position(
                ticker=lot.ticker, shares=lot.shares, avg_cost=round(lot.avg_cost, 4), entry_date=lot.entry_date,
                # A sector fund booked before funds were given a sector has none on record: it is read as its
                # sector all the same, or it would stay outside that sector's limit for as long as it is held.
                stop_price=stops.get(lot.ticker), sector=lot.sector or SECTOR_FUNDS.get(lot.ticker, ""),
                last_close=lot.last_close,
                value=round(lot.shares * price, 2),
                unrealized_pct=(
                    round((lot.last_close / lot.avg_cost - 1) * 100, 2) if lot.last_close is not None else None
                ),
                sessions_held=lot.sessions_held,
            )
        return result

    def account(self) -> Account:
        row = self.journal.rows("SELECT * FROM paper_accounts WHERE account = ?", (self.name,))[0]
        value = sum(position.value for position in self.positions().values())
        equity = row["cash"] + value
        marks = self.journal.rows(
            "SELECT equity FROM paper_equity WHERE account = ? ORDER BY session_date DESC LIMIT 2", (self.name,)
        )
        change = (marks[0]["equity"] / marks[1]["equity"] - 1) * 100 if len(marks) == 2 and marks[1]["equity"] else None
        high = max(row["high_water"], equity)
        return Account(
            account=self.name,
            session_date=date.fromisoformat(row["last_session"]) if row["last_session"] else None,
            cash=round(row["cash"], 2), positions_value=round(value, 2), equity=round(equity, 2),
            initial_cash=row["initial_cash"], high_water=round(high, 2),
            day_change_pct=round(change, 2) if change is not None else None,
            drawdown_pct=round((1 - equity / high) * 100, 2) if high > 0 else 0.0,
        )

    def equity_history(self) -> list[tuple[date, float]]:
        return [(date.fromisoformat(row["session_date"]), row["equity"]) for row in self.journal.rows(
            "SELECT session_date, equity FROM paper_equity WHERE account = ? ORDER BY session_date", (self.name,)
        )]

    def unreported_fills(self) -> list[Fill]:
        """Fills that no finished run has listed yet, oldest first."""
        return [
            Fill(order_id=row["order_id"], ticker=row["ticker"], side=row["side"], kind=row["kind"],
                 shares=row["shares"], price=row["price"], fill_date=date.fromisoformat(row["fill_date"]),
                 pnl_pct=row["pnl_pct"], note=row["note"])
            for row in self.journal.rows(
                "SELECT * FROM paper_fills WHERE account = ? AND reported_run_id IS NULL ORDER BY id", (self.name,)
            )
        ]

    def unreport_fills(self, run_ids: list[str]) -> None:
        """Fills that only replaced runs have listed count as not listed yet."""
        for run_id in run_ids:
            self.journal.write(
                "UPDATE paper_fills SET reported_run_id = NULL WHERE account = ? AND reported_run_id = ?",
                (self.name, run_id),
            )

    def mark_fills_reported(self, run_id: str) -> None:
        self.journal.write(
            "UPDATE paper_fills SET reported_run_id = ? WHERE account = ? AND reported_run_id IS NULL",
            (run_id, self.name),
        )

    # ── Orders ───────────────────────────────────────────────────────────

    def submit(self, request: OrderRequest, session: date) -> Order:
        with self.journal.transaction():
            existing = self.journal.rows("SELECT * FROM paper_orders WHERE order_id = ?", (request.order_id,))
            if existing:
                return self._order(existing[0])
            status, note = ("open", "") if request.shares >= 1 else ("rejected", "An order needs at least one share.")
            self.journal.write(
                "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (request.order_id, self.name, request.ticker, request.side, request.kind, request.shares,
                 request.limit_price, request.stop_price, request.protective_stop, int(request.day_only),
                 request.reason, status, session.isoformat(), note),
            )
        return self._order(self.journal.rows("SELECT * FROM paper_orders WHERE order_id = ?", (request.order_id,))[0])

    def cancel(self, order_id: str, note: str = "") -> None:
        with self.journal.transaction():
            self.journal.write(
                "UPDATE paper_orders SET status = 'cancelled', note = ? "
                "WHERE order_id = ? AND account = ? AND status = 'open'",
                (note, order_id, self.name),
            )

    def withdraw(self, order_id: str) -> bool:
        """Take back an order that has not traded. It is removed, so its id can be used again."""
        with self.journal.transaction():
            found = self.journal.rows(
                "SELECT 1 FROM paper_orders WHERE order_id = ? AND account = ? AND status != 'filled'",
                (order_id, self.name),
            )
            self.journal.write(
                "DELETE FROM paper_orders WHERE order_id = ? AND account = ? AND status != 'filled'",
                (order_id, self.name),
            )
        return bool(found)

    def raise_stop(self, ticker: str, price: float) -> bool:
        """Move the position's protective stop up to `price`. Never down: a lower price changes nothing."""
        with self.journal.transaction():
            found = self.journal.rows(
                "SELECT order_id FROM paper_orders WHERE account = ? AND ticker = ? AND kind = 'stop' "
                "AND status = 'open' AND stop_price < ?",
                (self.name, ticker, price),
            )
            for row in found:
                self.journal.write(
                    "UPDATE paper_orders SET stop_price = ? WHERE order_id = ? AND account = ?",
                    (price, row["order_id"], self.name),
                )
        return bool(found)

    # ── Bringing the books up to a session ───────────────────────────────

    def sync(self, snapshot: Snapshot) -> SyncResult:
        """Replay every session since the last sync, up to the snapshot's session.

        Applies splits and dividends, matches open orders against each
        session's bar, and marks equity at each close. Syncing again to the
        session the books are at changes nothing and returns what that sync
        found, so a run taken up after a crash still sees it. Everything is
        saved together or not at all.
        """
        session = snapshot.session_date
        result = SyncResult()
        with self.journal.transaction():
            row = self.journal.rows("SELECT * FROM paper_accounts WHERE account = ?", (self.name,))[0]
            last = date.fromisoformat(row["last_session"]) if row["last_session"] else None
            if last is not None and session < last:
                return result                               # an older snapshot: the books are already past it
            if session == last:
                stored = self.journal.rows(
                    "SELECT result_json FROM paper_syncs WHERE account = ? AND session_date = ?",
                    (self.name, session.isoformat()),
                )
                return SyncResult.model_validate_json(stored[0]["result_json"]) if stored else result
            self._cash = row["cash"]
            self._lots_now = self._lots()
            self._open = {order.order_id: order for order in self.orders()}
            self._snapshot, self._session, self._result, self._last = snapshot, session, result, last
            self._closed: list[Order] = []          # orders this sync finished
            self._new_orders: list[str] = []        # ids of stop orders this sync created

            for lot in list(self._lots_now.values()):
                self._bring_onto_scale(lot)

            # A new account has nothing to replay: its first sync only marks equity.
            days, day = [], next_trading_day(last) if last else session
            while day <= session:
                days.append(day)
                day = next_trading_day(day)
            high_water = row["high_water"]
            for day in days:
                equity = self._replay(day)
                high_water = max(high_water, equity)

            for lot in self._lots_now.values():
                item = snapshot.tickers.get(lot.ticker)
                on_scale = lot.last_adjusted == session
                lot.last_close = item.price.close if on_scale and item and item.price else None
                lot.sessions_held = sum(1 for d in item.bars.dates if d >= lot.entry_date) if item else lot.sessions_held
            self._save(high_water, session)
            self.journal.write(
                "INSERT OR REPLACE INTO paper_syncs VALUES (?, ?, ?)",
                (self.name, session.isoformat(), result.model_dump_json()),
            )
        return result

    # Share scale ---------------------------------------------------------

    def _bring_onto_scale(self, lot: _Lot) -> None:
        item, session = self._snapshot.tickers.get(lot.ticker), self._session
        if item is not None and item.usable and session > lot.last_adjusted:
            factor = 1.0
            for action in item.actions:
                if action.kind == "split" and action.value > 0 and lot.last_adjusted < action.date <= session:
                    factor *= action.value
            restated = dict(zip(item.bars.dates, item.bars.close)).get(lot.last_adjusted)
            gap = None if restated is None else abs(restated * factor / lot.anchor_close - 1)
            if gap is None or gap > CORRECTION_LIMIT:
                seen = "no bar for that day" if restated is None else f"{restated:,.4f}"
                self._result.problems.append(
                    f"{lot.ticker}: its listed splits and its restated prices do not agree, so the position is "
                    f"not valued and its orders wait. It closed at {lot.anchor_close:,.4f} on {lot.last_adjusted}; "
                    f"the data now shows {seen} for that day with a split factor of {factor:g}."
                )
            else:
                if gap > SCALE_TOLERANCE:
                    self._result.problems.append(
                        f"{lot.ticker}: the source now shows a close of {restated:,.4f} for {lot.last_adjusted}, "
                        f"where {lot.anchor_close / factor:,.4f} was recorded ({gap:.1%} apart). "
                        "Taken as a price correction by the source."
                    )
                if factor != 1.0:
                    self._apply_split(lot, factor, item.bars.close[-1])
                # Dividends that went ex while the position could not be priced were
                # skipped on their day. They are paid now; later ones are paid day by day.
                for action in item.actions:
                    if action.kind == "dividend" and self._last is not None and lot.entry_date < action.date \
                            and lot.last_adjusted < action.date <= self._last:
                        self._pay_dividend(lot, action.value, late=True)
                lot.last_adjusted = session
                lot.mark = restated                         # its close on the old day, on the new scale
                lot.anchor_close = item.bars.close[-1]      # usable data ends on the session
                lot.out_of_sync_since = None
                return
        if lot.last_adjusted == session:
            return
        if lot.out_of_sync_since is None:
            lot.out_of_sync_since = session
        if (session - lot.out_of_sync_since).days > self.stale_days:
            # Cost and anchor are on the same scale, so this result is exact.
            self._sell(lot, lot.shares, lot.anchor_close, lot.last_adjusted, "close",
                       f"{session}|{self.name}|{lot.ticker}|close",
                       "Closed by the broker: no usable, consistent price for too long.")
            self._result.problems.append(
                f"{lot.ticker}: no usable, consistent price in any sync since {lot.out_of_sync_since}. The "
                f"position was closed at its {lot.last_adjusted} close, {lot.anchor_close:,.2f}, the last price "
                "it could be matched to, and is no longer tracked."
            )

    def _apply_split(self, lot: _Lot, factor: float, close: float) -> None:
        exact = lot.shares * factor
        whole = math.floor(exact + 1e-9)
        if exact - whole > 1e-9:
            self._cash += (exact - whole) * close
            self._result.notices.append(
                f"{lot.ticker}: a split left {exact - whole:.4f} of a share, paid out in cash at {close:,.2f}."
            )
        lot.shares, lot.avg_cost = whole, lot.avg_cost / factor
        for order in [o for o in self._open.values() if o.ticker == lot.ticker]:
            order.shares = whole if order.kind == "stop" else math.floor(order.shares * factor + 1e-9)
            for name in ("limit_price", "stop_price", "protective_stop"):
                if getattr(order, name) is not None:
                    setattr(order, name, round(getattr(order, name) / factor, 4))
            if order.shares < 1:
                self._finish(order, "cancelled", note="A split left this order with no whole share.")
        if whole < 1:
            del self._lots_now[lot.ticker]
            self._cancel_ticker(lot.ticker, "The position ended in a split.")

    # One session -----------------------------------------------------------

    def _bar(self, ticker: str, day: date) -> _Bar | None:
        """The session's bar, or None when the ticker cannot be priced on this snapshot's scale."""
        item = self._snapshot.tickers.get(ticker)
        if item is None or not item.usable:
            return None
        lot = self._lots_now.get(ticker)
        if lot is not None and lot.last_adjusted != self._session:
            return None
        bars = item.bars
        if day not in bars.dates:
            return None
        index = bars.dates.index(day)
        return _Bar(bars.open[index], bars.high[index], bars.low[index], bars.close[index])

    def _replay(self, day: date) -> float:
        snapshot = self._snapshot
        for lot in list(self._lots_now.values()):
            item = snapshot.tickers.get(lot.ticker)
            if item is None or lot.last_adjusted != self._session:
                continue
            # Paid before the day's orders fill, so shares bought on the ex-date get nothing.
            for action in item.actions:
                if action.kind == "dividend" and action.date == day:
                    self._pay_dividend(lot, action.value)

        def due(kind: str, side: str | None = None) -> list[Order]:
            return [o for o in list(self._open.values())
                    if o.kind == kind and o.created_session < day and (side is None or o.side == side)]

        for order in due("stop"):
            # Matched before the day's buys: a position opened today meets its stop further down,
            # against the low only, because it did not exist at the open.
            lot, bar = self._lots_now.get(order.ticker), self._bar(order.ticker, day)
            if lot is None or bar is None:
                continue
            if bar.open <= order.stop_price:
                self._sell(lot, order.shares, bar.open * (1 - self.slippage), day, "stop", order.order_id,
                           "Opened below the stop.", order)
            elif bar.low <= order.stop_price:
                self._sell(lot, order.shares, order.stop_price * (1 - self.slippage), day, "stop",
                           order.order_id, "", order)

        for order in due("limit"):
            bar = self._bar(order.ticker, day)
            first_day = next_trading_day(order.created_session)
            if order.side == "buy" and self._sized_before_a_split(order):
                continue
            if bar is not None and order.side == "buy" and bar.low <= order.limit_price:
                price = min(bar.open, order.limit_price)
                self._buy(order, price, price, day)
            elif bar is not None and order.side == "sell" and bar.high >= order.limit_price:
                lot = self._lots_now.get(order.ticker)
                if lot is None:
                    self._finish(order, "cancelled", note="There is no position to sell.")
                else:
                    self._sell(lot, order.shares, max(bar.open, order.limit_price), day, "limit", order.order_id,
                               "", order)
            elif order.day_only and day >= first_day:
                self._finish(order, "expired", note=f"Not filled on {first_day}.")
                self._result.notices.append(f"{order.ticker}: day-only {order.side} limit expired unfilled.")

        for side in ("sell", "buy"):
            for order in due("market", side):
                bar = self._bar(order.ticker, day)
                if bar is None:
                    if (day - order.created_session).days > self.stale_days:
                        self._finish(order, "cancelled", note="No usable opening price appeared in time.")
                        self._result.problems.append(
                            f"{order.ticker}: {order.side} order from {order.created_session} was cancelled, no "
                            f"usable opening price appeared within {self.stale_days} days."
                        )
                    continue
                if side == "sell":
                    lot = self._lots_now.get(order.ticker)
                    if lot is None:
                        self._finish(order, "cancelled", note="There is no position to sell.")
                        continue
                    self._sell(lot, order.shares, bar.open * (1 - self.slippage), day, "market", order.order_id,
                               "", order)
                else:
                    if self._sized_before_a_split(order):
                        continue
                    self._buy(order, bar.open * (1 + self.slippage), bar.open, day)

        # Positions opened today face their own stop for the rest of the session.
        for order in [o for o in list(self._open.values()) if o.kind == "stop"]:
            lot, bar = self._lots_now.get(order.ticker), self._bar(order.ticker, day)
            if lot is not None and bar is not None and lot.entry_date == day and bar.low <= order.stop_price:
                self._sell(lot, order.shares, order.stop_price * (1 - self.slippage), day, "stop", order.order_id,
                           "Stopped out on the day it was opened.", order)

        # A position with no bar today is carried at the last close it was valued at.
        value = 0.0
        for lot in self._lots_now.values():
            bar = self._bar(lot.ticker, day)
            if bar is not None:
                lot.mark = bar.close
            value += lot.shares * lot.mark
        equity = self._cash + value
        self.journal.write(
            "INSERT OR REPLACE INTO paper_equity VALUES (?, ?, ?, ?, ?)",
            (self.name, day.isoformat(), self._cash, value, equity),
        )
        return equity

    def _pay_dividend(self, lot: _Lot, per_share: float, late: bool = False) -> None:
        self._cash += lot.shares * per_share
        self._result.notices.append(
            f"{lot.ticker}: dividend of {per_share:g} a share on {lot.shares} shares, "
            f"{lot.shares * per_share:,.2f} in cash{' (paid late: the ticker could not be priced on its day)' if late else ''}."
        )

    # Fills -----------------------------------------------------------------

    def _sized_before_a_split(self, order: Order) -> bool:
        """Cancel a buy whose share count was fixed before a split, and say whether it was.

        Every price here is on this snapshot's share scale, but the order's
        share count and prices were fixed on the scale of the day it was
        created. If a split falls anywhere between the two, they no longer
        mean what they did. Orders on a held ticker were rescaled with it.
        """
        item = self._snapshot.tickers.get(order.ticker)
        if item is None or order.ticker in self._lots_now or not any(
            a.kind == "split" and order.created_session < a.date <= self._session for a in item.actions
        ):
            return False
        self._finish(order, "cancelled", note="A split took effect after the order was sized.")
        self._result.problems.append(
            f"{order.ticker}: buy order from {order.created_session} was cancelled, a split took "
            "effect after it was sized and its share count no longer means what it did."
        )
        return True

    def _buy(self, order: Order, price: float, quoted: float, day: date) -> None:
        """Fill a buy at `price`. `quoted` is the price before slippage: the open, or the limit."""
        if order.protective_stop is not None and quoted <= order.protective_stop:
            self._finish(order, "cancelled", note=(
                f"Would have filled at {quoted:,.2f}, at or below its stop of {order.protective_stop:,.2f}."
            ))
            self._result.notices.append(
                f"{order.ticker}: buy of {order.shares} shares was not made. It would have filled at "
                f"{quoted:,.2f}, at or below the stop of {order.protective_stop:,.2f} it was sized against."
            )
            return
        cost = order.shares * price
        if cost > self._cash + 1e-6:
            self._finish(order, "rejected", note=f"Cost {cost:,.2f} is more than the cash available.")
            self._result.problems.append(
                f"{order.ticker}: buy of {order.shares} shares was rejected, it cost {cost:,.2f} "
                f"and the account had {self._cash:,.2f} in cash."
            )
            return
        self._cash -= cost
        item = self._snapshot.tickers[order.ticker]
        lot = self._lots_now.get(order.ticker)
        if lot is None:
            lot = _Lot(order.ticker, order.shares, price, day, sector_of(item), self._session,
                       item.bars.close[-1], mark=price)
            self._lots_now[order.ticker] = lot
        else:
            lot.avg_cost = (lot.avg_cost * lot.shares + cost) / (lot.shares + order.shares)
            lot.shares += order.shares
        self._record(order.order_id, order.ticker, "buy", order.kind, order.shares, price, day, None, "")
        self._finish(order, "filled", price, day)

        if order.protective_stop is not None:
            existing = next((o for o in self._open.values() if o.kind == "stop" and o.ticker == order.ticker), None)
            if existing is not None:
                # Adding to a position never loosens the stop on the shares already held.
                existing.shares = lot.shares
                existing.stop_price = max(existing.stop_price, order.protective_stop)
            else:
                stop_id = f"{order.order_id}|stop"
                self._open[stop_id] = Order(
                    order_id=stop_id, ticker=order.ticker, side="sell", kind="stop", shares=lot.shares,
                    stop_price=order.protective_stop, reason="Protective stop attached when the buy filled.",
                    status="open", created_session=order.created_session,
                )
                self._new_orders.append(stop_id)

    def _sell(self, lot: _Lot, shares: int, price: float, day: date, kind: str, order_id: str, note: str,
              order: Order | None = None) -> None:
        shares = min(shares, lot.shares)
        self._cash += shares * price
        pnl = round((price / lot.avg_cost - 1) * 100, 2)
        self._record(order_id, lot.ticker, "sell", kind, shares, price, day, pnl, note)
        if order is not None:
            order.shares = shares                           # what actually traded
            self._finish(order, "filled", price, day, note)
        lot.shares -= shares
        if lot.shares < 1:
            del self._lots_now[lot.ticker]
            self._cancel_ticker(lot.ticker, "The position was closed.")
        else:
            for stop in [o for o in self._open.values() if o.kind == "stop" and o.ticker == lot.ticker]:
                stop.shares = lot.shares

    def _record(self, order_id, ticker, side, kind, shares, price, day, pnl, note) -> None:
        price = round(price, 4)
        self.journal.write(
            "INSERT INTO paper_fills (account, order_id, ticker, side, kind, shares, price, fill_date, pnl_pct, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.name, order_id, ticker, side, kind, shares, price, day.isoformat(), pnl, note),
        )
        self._result.fills.append(Fill(
            order_id=order_id, ticker=ticker, side=side, kind=kind, shares=shares, price=price,
            fill_date=day, pnl_pct=pnl, note=note,
        ))

    def _finish(self, order: Order, status: str, price: float | None = None, day: date | None = None,
                note: str = "") -> None:
        order.status, order.note = status, note or order.note
        order.filled_price = round(price, 4) if price is not None else None
        order.filled_date = day
        self._closed.append(order)
        self._open.pop(order.order_id, None)

    def _cancel_ticker(self, ticker: str, note: str) -> None:
        for order in [o for o in self._open.values() if o.ticker == ticker]:
            self._finish(order, "cancelled", note=note)

    # Saving ----------------------------------------------------------------

    def _save(self, high_water: float, session: date) -> None:
        write = self.journal.write
        write("UPDATE paper_accounts SET cash = ?, high_water = ?, last_session = ? WHERE account = ?",
              (self._cash, high_water, session.isoformat(), self.name))
        write("DELETE FROM paper_positions WHERE account = ?", (self.name,))
        for lot in self._lots_now.values():
            write(
                "INSERT INTO paper_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.name, lot.ticker, lot.shares, lot.avg_cost, lot.entry_date.isoformat(), lot.sector,
                 lot.last_adjusted.isoformat(), lot.anchor_close,
                 lot.out_of_sync_since.isoformat() if lot.out_of_sync_since else None,
                 lot.last_close, lot.sessions_held),
            )
        for order in self._closed + list(self._open.values()):
            if order.order_id in self._new_orders:
                write(
                    "INSERT OR REPLACE INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (order.order_id, self.name, order.ticker, order.side, order.kind, order.shares,
                     order.limit_price, order.stop_price, order.protective_stop, int(order.day_only),
                     order.reason, order.status, order.created_session.isoformat(), order.filled_price,
                     order.filled_date.isoformat() if order.filled_date else None, order.note),
                )
            else:
                write(
                    "UPDATE paper_orders SET shares = ?, limit_price = ?, stop_price = ?, protective_stop = ?, "
                    "status = ?, filled_price = ?, filled_date = ?, note = ? WHERE order_id = ?",
                    (order.shares, order.limit_price, order.stop_price, order.protective_stop, order.status,
                     order.filled_price, order.filled_date.isoformat() if order.filled_date else None,
                     order.note, order.order_id),
                )
