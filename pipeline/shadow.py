"""The shadow book: a hypothetical portfolio built from accepted proposals.

Shadow mode places no orders, so without this the portfolio manager would
never have a position to sell. Each accepted proposal is treated as an order
that fills at the open of the next session, for a fixed dollar amount.

Simplifications, all on purpose: no cash balance, fractional shares, no
dividends, no slippage. The paper broker replaces this in the next stage.

Splits. The price source restates every earlier bar each time a split takes
effect, so all prices in one snapshot are on the share scale of that
snapshot's session. A position remembers which session's scale it is on
(`last_adjusted`) and what the ticker closed at on that session
(`anchor_close`). Before anything is filled or valued, each position is
brought onto the scale of the snapshot in hand; after that, entry prices, fill
prices and closing prices can be compared directly.

The splits the source lists and the prices it restates are two separate
facts, and the move to a new scale is only made when they agree: the old
close, restated, times the listed split factor must give back the close that
was recorded. When they do not, the position is left as it is, is not valued,
and its orders wait.

A position cannot stay in that state for ever. One that runs have found out
of step for longer than the limit is closed by the book at the last close it
could trust, and is no longer tracked. The same rule ends a position in a
ticker whose data stops coming, such as one that is delisted. The limit is
counted from the first run that found the problem, not from the last good
price: days on which the pipeline simply did not run prove nothing about the
data.

Known gap: a stock dividend or other adjustment under 5% that the source
applies to its prices without listing it is accepted as a price correction,
and the position is then off by that much.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import config
from data.models import Snapshot, TickerSnapshot
from journal.store import Journal
from models.signals import Holding, ShadowFill, TradeProposal


# How far the restated close may be from the recorded one, as a fraction.
# Within the first it is rounding. Up to the second it is taken to be the
# source correcting a price, and is accepted with a note. Beyond that the
# split record and the prices are taken to disagree.
SCALE_TOLERANCE = 0.01
CORRECTION_LIMIT = 0.05


@dataclass
class _Position:
    ticker: str
    shares: float
    entry_price: float
    entry_date: date
    last_adjusted: date      # the session whose share scale `shares` and `entry_price` are on
    anchor_close: float      # the ticker's close on that session, on that scale
    out_of_sync_since: date | None = None    # first run that could not bring it onto the current scale


def _first_bar_after(item: TickerSnapshot | None, day: date) -> tuple[date, float] | None:
    """Date and opening price of the first session after `day`, if the snapshot has one.

    Nothing is filled from data the quality checks rejected: the order waits.
    """
    if item is None or not item.usable:
        return None
    for bar_date, open_price in zip(item.bars.dates, item.bars.open):
        if bar_date > day:
            return (bar_date, open_price) if open_price > 0 else None
    return None


def _rescale(position: _Position, item: TickerSnapshot | None, session: date) -> str:
    """Bring a position onto the share scale of the snapshot's session.

    The position is on the new scale afterwards exactly when its
    `last_adjusted` equals `session`. A note is returned when the split record
    and the restated prices disagree (the position is left untouched), and
    when they differ by little enough to be a price correction (the move is
    made). The position is also left untouched, without a note, when the
    ticker's data is missing or was rejected: such data may not show a split
    yet, and marking the position as adjusted would lose that split for good.
    """
    if item is None or not item.usable or session <= position.last_adjusted:
        return ""
    factor = 1.0
    for action in item.actions:
        if action.kind == "split" and action.value > 0 and position.last_adjusted < action.date <= session:
            factor *= action.value
    restated = dict(zip(item.bars.dates, item.bars.close)).get(position.last_adjusted)
    gap = None if restated is None else abs(restated * factor / position.anchor_close - 1)
    if gap is None or gap > CORRECTION_LIMIT:
        seen = "no bar for that day" if restated is None else f"{restated:,.4f}"
        return (
            f"{position.ticker}: its listed splits and its restated prices do not agree, so the position is "
            f"not valued and its orders wait. It closed at {position.anchor_close:,.4f} on "
            f"{position.last_adjusted}; the data now shows {seen} for that day with a split factor of {factor:g}."
        )
    note = ""
    if gap > SCALE_TOLERANCE:
        note = (
            f"{position.ticker}: the source now shows a close of {restated:,.4f} for {position.last_adjusted}, "
            f"where {position.anchor_close / factor:,.4f} was recorded ({gap:.1%} apart). "
            "Taken as a price correction by the source."
        )
    position.shares *= factor
    position.entry_price /= factor
    position.last_adjusted = session
    position.anchor_close = item.bars.close[-1]       # usable data ends on the session
    return note


class ShadowBook:
    def __init__(
        self, journal: Journal, arm: str, position_usd: float | None = None, pending_max_days: int | None = None,
    ) -> None:
        self.journal = journal
        self.arm = arm
        self.position_usd = position_usd if position_usd is not None else config.SHADOW_POSITION_USD
        self.pending_max_days = (
            pending_max_days if pending_max_days is not None else config.SHADOW_PENDING_MAX_DAYS
        )

    def _positions(self) -> dict[str, _Position]:
        return {
            row["ticker"]: _Position(
                row["ticker"], row["shares"], row["entry_price"],
                date.fromisoformat(row["entry_date"]), date.fromisoformat(row["last_adjusted"]),
                row["anchor_close"],
                date.fromisoformat(row["out_of_sync_since"]) if row["out_of_sync_since"] else None,
            )
            for row in self.journal.positions(self.arm)
        }

    def settle(self, snapshot: Snapshot) -> tuple[list[ShadowFill], list[str]]:
        """Fill orders left by earlier runs, using the opening prices in this snapshot.

        Returns the fills, and a note for every order that was dropped, every
        position that could not be brought onto this snapshot's share scale,
        and every position the book closed because it had gone too long
        without a usable price. An order proposed in the snapshot's own
        session stays waiting: its fill price does not exist yet. Everything
        is saved together or not at all.
        """
        session = snapshot.session_date
        fills: list[ShadowFill] = []
        notes: list[str] = []
        with self.journal.transaction():
            positions = self._positions()
            for position in list(positions.values()):
                note = _rescale(position, snapshot.tickers.get(position.ticker), session)
                if note:
                    notes.append(note)
                if position.last_adjusted == session:
                    position.out_of_sync_since = None
                    continue
                if position.out_of_sync_since is None:
                    position.out_of_sync_since = session
                if (session - position.out_of_sync_since).days > self.pending_max_days:
                    # Entry price and anchor are on the same scale, so this result is exact.
                    pnl = round((position.anchor_close / position.entry_price - 1) * 100, 2)
                    fills.append(self._fill(
                        position.ticker, "close", position.shares, position.anchor_close,
                        position.last_adjusted, position.last_adjusted, pnl,
                    ))
                    notes.append(
                        f"{position.ticker}: no usable, consistent price in any run since "
                        f"{position.out_of_sync_since}. The position was closed at its {position.last_adjusted} "
                        f"close, {position.anchor_close:,.2f}, the last price it could be matched to, and is no "
                        "longer tracked."
                    )
                    del positions[position.ticker]
                    self.journal.drop_position(self.arm, position.ticker)
                    self.journal.drop_pending(self.arm, position.ticker)

            for order in self.journal.pending(self.arm):
                ticker, action = order["ticker"], order["action"]
                proposed = date.fromisoformat(order["proposed_session"])
                item = snapshot.tickers.get(ticker)
                bar = _first_bar_after(item, proposed)
                position = positions.get(ticker)
                # A position that is not on this snapshot's scale cannot be
                # compared with this snapshot's prices, so nothing fills against it.
                if bar is None or (position and position.last_adjusted != session):
                    if (session - proposed).days > self.pending_max_days:
                        self.journal.drop_pending(self.arm, ticker)
                        why = (
                            "no usable opening price appeared" if bar is None
                            else "its position could not be matched to current prices"
                        )
                        notes.append(
                            f"{ticker}: {action} proposed on {proposed} was dropped, {why} "
                            f"within {self.pending_max_days} days."
                        )
                    continue
                bar_date, price = bar

                if action == "buy":
                    if position:
                        notes.append(f"{ticker}: buy proposed on {proposed} was dropped, the position already exists.")
                    else:
                        # The price is on this session's scale, so the position starts there too.
                        shares = self.position_usd / price
                        positions[ticker] = _Position(ticker, shares, price, bar_date, session, item.bars.close[-1])
                        fills.append(self._fill(ticker, "buy", shares, price, bar_date, proposed, None))
                elif position is None:
                    notes.append(f"{ticker}: {action} proposed on {proposed} was dropped, there is no position.")
                else:
                    sold = position.shares if action == "sell" else position.shares / 2
                    pnl = round((price / position.entry_price - 1) * 100, 2)
                    fills.append(self._fill(ticker, action, sold, price, bar_date, proposed, pnl))
                    if action == "sell":
                        del positions[ticker]
                        self.journal.drop_position(self.arm, ticker)
                    else:
                        position.shares -= sold
                self.journal.drop_pending(self.arm, ticker)

            for position in positions.values():
                self.journal.put_position(
                    self.arm, position.ticker, position.shares, position.entry_price,
                    position.entry_date, position.last_adjusted, position.anchor_close,
                    position.out_of_sync_since,
                )
        return fills, notes

    def _fill(self, ticker, action, shares, price, trade_date, proposed, pnl_pct) -> ShadowFill:
        self.journal.add_trade(self.arm, ticker, action, shares, price, trade_date, proposed, pnl_pct)
        return ShadowFill(
            ticker=ticker, action=action, shares=round(shares, 6), price=price,
            trade_date=trade_date, proposed_session=proposed, pnl_pct=pnl_pct,
        )

    def unreported_fills(self) -> list[ShadowFill]:
        """Fills that no finished run has listed yet, oldest first.

        Read from the journal, not from the last `settle`, so fills made by a
        run that failed later still appear in the next signal table.
        """
        return [
            ShadowFill(
                ticker=row["ticker"], action=row["action"], shares=round(row["shares"], 6), price=row["price"],
                trade_date=date.fromisoformat(row["trade_date"]),
                proposed_session=date.fromisoformat(row["proposed_session"]), pnl_pct=row["pnl_pct"],
            )
            for row in self.journal.unreported_trades(self.arm)
        ]

    def holdings(self, snapshot: Snapshot) -> list[Holding]:
        """Current positions, valued at the snapshot's closing prices.

        A position whose data is unusable today is not valued: its stored
        entry price may be on an older share scale than today's prices.
        """
        result = []
        for position in self._positions().values():
            item = snapshot.tickers.get(position.ticker)
            priced = bool(item and item.price and item.usable and position.last_adjusted == snapshot.session_date)
            last_close = item.price.close if priced else None
            result.append(Holding(
                ticker=position.ticker, shares=round(position.shares, 6),
                entry_price=round(position.entry_price, 4), entry_date=position.entry_date,
                last_close=last_close,
                unrealized_pct=round((last_close / position.entry_price - 1) * 100, 2) if last_close else None,
                # The entry session counts as the first one held.
                sessions_held=sum(1 for day in item.bars.dates if day >= position.entry_date) if item else 0,
            ))
        return result

    def waiting(self, before: date) -> dict[str, str]:
        """Orders from sessions before `before` that have not filled: ticker to action."""
        return {
            row["ticker"]: row["action"] for row in self.journal.pending(self.arm)
            if date.fromisoformat(row["proposed_session"]) < before
        }

    def queue(self, proposals: list[TradeProposal], session: date, run_id: str) -> None:
        """Turn this run's accepted proposals into orders for the next session's open.

        Orders an earlier run of the same session left behind are replaced,
        so running a session twice cannot fill it twice.
        """
        with self.journal.transaction():
            self.journal.drop_pending_from(self.arm, session)
            for proposal in proposals:
                if proposal.status == "accepted" and proposal.action != "hold":
                    self.journal.put_pending(self.arm, proposal.ticker, proposal.action, session, run_id)
