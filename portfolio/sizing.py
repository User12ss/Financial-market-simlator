"""How many shares each accepted proposal becomes.

A model says what to do and how sure it is; this code decides how much. A
buy risks a fixed share of equity, scaled by confidence, between the last
close and a protective stop below it. The stop is the analyst's own when it
proposed one inside the allowed range, and a few ATRs below the close
otherwise: either way the money at risk is the same, so a wider stop means
fewer shares. A sell exits the whole position and a trim exits half. Nothing
here checks the risk limits: the risk gate does that to whatever this module
returns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import config
from models.signals import TickerView, TradeProposal
from models.trading import Account, OrderRequest, Position


@dataclass(frozen=True)
class Quote:
    """What sizing needs to know about one ticker, taken from the data snapshot."""

    close: float | None  # last close; None when unusable
    atr: float | None  # 14-day ATR in dollars; None when unknown


def order_id(session: date, account: str, ticker: str, side: str) -> str:
    """The same decision on the same session always gets the same id, so a rerun cannot place it twice."""
    return f"{session.isoformat()}|{account}|{ticker}|{side}"


def _usable(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


# A model reads the close and the ATR rounded to the cent. The limits are worked
# out from the ATR as it was shown, and a stop within a cent of a limit counts
# as on it, so a stop put exactly on a limit is not refused over rounding.
_CENT = 0.01


def usable_stop(close: float | None, atr: float | None, proposed: float | None) -> tuple[float | None, str]:
    """A model's stop for a new position, to the cent, or None and why it may not be used.

    It has to sit below the last close by at least SIZING_STOP_MIN_ATR_MULTIPLE
    ATRs, and never by less than SIZING_MIN_STOP_PCT percent of the price, and
    by at most SIZING_STOP_MAX_ATR_MULTIPLE ATRs. A stop outside that is
    refused, not moved to the nearest allowed price: a level the model did not
    choose says nothing about its view.
    """
    if proposed is None:
        return None, ""
    if not _usable(close) or not _usable(atr):
        return None, "there is no usable close and ATR to judge it by."
    low, high = config.SIZING_STOP_MIN_ATR_MULTIPLE, config.SIZING_STOP_MAX_ATR_MULTIPLE
    if not (0 < low <= high and math.isfinite(high)):
        return None, (
            "the settings give no range to judge it by: SIZING_STOP_MIN_ATR_MULTIPLE has to be above zero "
            "and no more than SIZING_STOP_MAX_ATR_MULTIPLE."
        )
    atr = round(atr, 2)
    stop = round(proposed, 2) if _usable(proposed) else 0.0
    if stop <= 0:
        return None, "it is not a price."
    distance = close - stop
    if distance <= 0:
        return None, f"it is not below the ${close:,.2f} close."

    by_atr = low * atr
    floor = close * config.SIZING_MIN_STOP_PCT / 100
    nearest = max(by_atr, floor)
    near_rule = f"{low:g} x ATR ${atr:,.2f}" if by_atr >= floor else f"{config.SIZING_MIN_STOP_PCT:g}% of the price"
    # Where even the widest ATR stop is inside the nearest one, only the nearest is allowed.
    by_max = high * atr
    furthest = max(by_max, nearest)
    far_rule = f"{high:g} x ATR ${atr:,.2f}" if by_max >= nearest else near_rule
    if distance < nearest - _CENT:
        return None, (
            f"it is ${distance:,.2f} below the ${close:,.2f} close, and the nearest allowed is "
            f"${nearest:,.2f} ({near_rule})."
        )
    if distance > furthest + _CENT:
        return None, (
            f"it is ${distance:,.2f} below the ${close:,.2f} close, and the furthest allowed is "
            f"${furthest:,.2f} ({far_rule})."
        )
    return stop, ""


def size_orders(
    proposals: list[TradeProposal],
    *,
    session: date,
    account: Account,
    positions: dict[str, Position],
    views: dict[str, TickerView],
    quotes: dict[str, Quote],
) -> tuple[list[OrderRequest], list[str]]:
    """Market orders for the accepted buys, sells and trims, and a note for each that produced none.

    Sells and trims come first, by ticker, then buys by descending confidence.
    """
    exits: list[OrderRequest] = []
    buys: list[tuple[float, OrderRequest]] = []
    notes: list[str] = []

    for proposal in proposals:
        if proposal.status != "accepted" or proposal.action == "hold":
            continue
        ticker = proposal.ticker

        if proposal.action in ("sell", "trim"):
            position = positions.get(ticker)
            if position is None or position.shares < 1:
                notes.append(f"{ticker}: no order for the {proposal.action}, because no position is held.")
                continue
            if proposal.action == "sell":
                shares, reason = position.shares, f"Sell all {position.shares} shares held."
            else:
                shares = position.shares // 2
                if shares == 0:
                    notes.append(f"{ticker}: no order for the trim, because half of a 1-share position is 0 shares.")
                    continue
                reason = f"Trim {shares} of the {position.shares} shares held."
            exits.append(OrderRequest(
                order_id=order_id(session, account.account, ticker, "sell"),
                ticker=ticker, side="sell", shares=shares, reason=reason,
            ))
            continue

        view = views.get(ticker)
        if view is None:
            notes.append(f"{ticker}: no order for the buy, because there is no analyst view to take confidence from.")
            continue
        quote = quotes.get(ticker)
        if quote is None or not _usable(quote.close):
            notes.append(f"{ticker}: no order for the buy, because there is no usable last close.")
            continue
        if not _usable(quote.atr):
            notes.append(f"{ticker}: no order for the buy, because there is no usable ATR.")
            continue

        confidence = min(proposal.confidence, view.confidence)
        budget = account.equity * config.SIZING_RISK_PCT / 100 * confidence
        stop, refusal = usable_stop(quote.close, quote.atr, view.proposed_stop)
        if stop is not None:
            distance = quote.close - stop
            how = f"the analyst's stop, {distance / quote.atr:.1f} x ATR ${quote.atr:,.2f}"
        else:
            by_atr = config.SIZING_STOP_ATR_MULTIPLE * quote.atr
            floor = quote.close * config.SIZING_MIN_STOP_PCT / 100
            distance = max(by_atr, floor)
            stop = round(quote.close - distance, 2)
            how = (
                f"{config.SIZING_STOP_ATR_MULTIPLE:g} x ATR ${quote.atr:,.2f}" if by_atr >= floor
                else f"the {config.SIZING_MIN_STOP_PCT:g}% minimum, wider than {config.SIZING_STOP_ATR_MULTIPLE:g} x ATR ${quote.atr:,.2f}"
            )
            if stop <= 0:
                notes.append(
                    f"{ticker}: no order for the buy, because a stop {distance:.2f} below the "
                    f"{quote.close:.2f} close would be at or below zero."
                )
                continue
            if stop >= quote.close:
                notes.append(
                    f"{ticker}: no order for the buy, because a stop {distance:.4f} below the "
                    f"{quote.close:.2f} close is the close itself once rounded to the cent."
                )
                continue
            # Sized on the stop as it will be placed: rounding it to the cent
            # can leave it further from the close than the rule put it.
            distance = quote.close - stop
        shares = math.floor(budget / distance)
        if shares < 1:
            notes.append(
                f"{ticker}: no order for the buy, because a risk budget of ${budget:,.2f} buys less than one "
                f"share at a stop distance of ${distance:,.2f}."
            )
            continue
        unused = f" The analyst's stop of ${view.proposed_stop:,.2f} was not used: {refusal}" if refusal else ""
        buys.append((confidence, OrderRequest(
            order_id=order_id(session, account.account, ticker, "buy"),
            ticker=ticker, side="buy", shares=shares, protective_stop=stop,
            reason=(
                f"Risk budget ${budget:,.2f} ({config.SIZING_RISK_PCT:g}% of ${account.equity:,.2f} equity at "
                f"confidence {confidence:.2f}) over a stop distance of ${distance:,.2f} "
                f"({how}) gives {shares} shares, "
                f"with the stop at ${stop:,.2f} below the ${quote.close:,.2f} close.{unused}"
            ),
        )))

    exits.sort(key=lambda r: r.ticker)
    buys.sort(key=lambda item: (-item[0], item[1].ticker))
    return exits + [request for _, request in buys], notes
