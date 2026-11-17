"""The risk gate: what may actually be ordered, whatever was proposed.

Every requested order comes back approved in full, reduced to fewer shares,
or rejected, with one sentence per rule that cut or refused it. The limits
are hard: nothing a model writes can loosen them. Exits are never blocked by
a buy limit or a circuit breaker; only the kill switch, a malformed order or
a duplicate stops a sell.

`review` does no I/O. Only `halted` looks at the file system and environment.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

import config
from models.trading import Account, Order, OrderRequest, Position, RiskDecision

# Money comparisons allow this much slack so an order landing exactly on a limit passes.
EPSILON = 1e-6
UNKNOWN_SECTOR = "Unknown"


@dataclass(frozen=True)
class RiskLimits:
    """Limits as fractions of equity (0.10 is 10%), except the counts and the dollar floors."""

    max_position: float
    max_sector: float
    max_positions: int
    min_cash: float
    max_trade_risk: float
    max_new_positions_per_day: int
    max_daily_turnover: float
    daily_loss_limit: float
    max_drawdown: float
    min_price: float
    min_dollar_volume: float
    max_limit_deviation: float

    @classmethod
    def from_config(cls) -> RiskLimits:
        return cls(
            max_position=config.RISK_MAX_POSITION_PCT / 100,
            max_sector=config.RISK_MAX_SECTOR_PCT / 100,
            max_positions=config.RISK_MAX_POSITIONS,
            min_cash=config.RISK_MIN_CASH_PCT / 100,
            max_trade_risk=config.RISK_MAX_TRADE_RISK_PCT / 100,
            max_new_positions_per_day=config.RISK_MAX_NEW_POSITIONS_PER_DAY,
            max_daily_turnover=config.RISK_MAX_DAILY_TURNOVER_PCT / 100,
            daily_loss_limit=config.RISK_DAILY_LOSS_LIMIT_PCT / 100,
            max_drawdown=config.RISK_MAX_DRAWDOWN_PCT / 100,
            min_price=config.RISK_MIN_PRICE,
            min_dollar_volume=config.RISK_MIN_DOLLAR_VOLUME,
            max_limit_deviation=config.RISK_MAX_LIMIT_DEVIATION_PCT / 100,
        )


@dataclass(frozen=True)
class MarketFacts:
    """What the gate knows about one ticker, from the data snapshot."""

    usable: bool
    close: float | None
    avg_dollar_volume: float | None
    sector: str  # "" when unknown


def halted() -> str:
    """Empty when trading may proceed, otherwise why it may not."""
    path = Path(config.KILL_SWITCH_FILE)
    if path.exists():
        return f"Trading is halted: the kill switch file {path} exists."
    if os.environ.get("TRADING_HALT") == "1":
        return "Trading is halted: TRADING_HALT is set to 1."
    return ""


def _pct(fraction: float) -> str:
    return f"{fraction * 100:g}%"


def _shares_within(room: float, price_per_share: float) -> int:
    """Whole shares whose cost fits in `room` dollars, never below zero."""
    return max(0, math.floor((room + EPSILON) / price_per_share))


def _malformed(request: OrderRequest, facts: dict[str, MarketFacts], limits: RiskLimits) -> str:
    if isinstance(request.shares, bool) or not isinstance(request.shares, int) or request.shares < 1:
        return f"Share count {request.shares} is not a whole number of at least 1."
    if request.kind == "market":
        if request.limit_price is not None:
            return f"A market order may not carry a limit price (got {request.limit_price})."
    elif request.kind == "limit":
        price = request.limit_price
        if price is None or not math.isfinite(price) or price <= 0:
            return f"A limit order needs a positive limit price (got {price})."
        fact = facts.get(request.ticker)
        if fact is None or fact.close is None or fact.close <= 0:
            return "A limit order cannot be checked: there is no close to compare the limit price with."
        deviation = abs(price - fact.close) / fact.close
        if deviation > limits.max_limit_deviation + EPSILON:
            return (
                f"Limit price {price:.2f} is {_pct(deviation)} from the {fact.close:.2f} close, "
                f"beyond the {_pct(limits.max_limit_deviation)} limit."
            )
    elif request.kind == "stop":
        price = request.stop_price
        if price is None or not math.isfinite(price) or price <= 0:
            return f"A stop order needs a positive stop price (got {price})."
    return ""


def review(
    requests: list[OrderRequest],
    *,
    account: Account,
    positions: dict[str, Position],
    open_orders: list[Order],
    facts: dict[str, MarketFacts],
    universe: set[str],
    limits: RiskLimits,
    halt_reason: str = "",
) -> list[RiskDecision]:
    """One decision per request, in the order of `requests`.

    Sells are decided before any buy, because they free room; buys are then
    decided in the order given, each against what was approved before it.
    """
    decisions: dict[int, RiskDecision] = {}

    def decide(i: int, approved: int, reasons: list[str]) -> None:
        request = requests[i]
        verdict = "rejected" if approved == 0 else "reduced" if approved < request.shares else "approved"
        decisions[i] = RiskDecision(
            order_id=request.order_id, ticker=request.ticker, side=request.side,
            requested_shares=request.shares, approved_shares=approved, verdict=verdict, reasons=reasons,
        )

    # Rules that apply to every order, checked in the order the requests came.
    # A duplicate is the same ticker, side and kind: a position's protective
    # stop is an open sell order too, and it must not block the exit.
    open_keys = {(o.ticker, o.side, o.kind) for o in open_orders}
    seen: set[tuple[str, str, str]] = set()
    survivors: list[int] = []
    for i, request in enumerate(requests):
        key = (request.ticker, request.side, request.kind)
        if halt_reason:
            decide(i, 0, [halt_reason])
        elif problem := _malformed(request, facts, limits):
            decide(i, 0, [problem])
        elif key in open_keys:
            decide(i, 0, [
                f"Duplicate: an open {request.kind} {request.side} order for {request.ticker} is already at the broker.",
            ])
        elif key in seen:
            decide(i, 0, [
                f"Duplicate: an earlier request in this batch is already a {request.kind} {request.side} "
                f"of {request.ticker}.",
            ])
        else:
            survivors.append(i)
        seen.add(key)

    def price_of(ticker: str) -> float:
        fact = facts.get(ticker)
        if fact is not None and fact.close is not None and fact.close > 0:
            return fact.close
        # A sell of a ticker whose data is unusable still needs a value for the turnover count.
        position = positions[ticker]
        return position.last_close if position.last_close is not None else position.avg_cost

    equity = account.equity
    turnover_used = 0.0

    for i in survivors:
        request = requests[i]
        if request.side != "sell":
            continue
        position = positions.get(request.ticker)
        if position is None or position.shares < 1:
            decide(i, 0, [f"Long only: there is no position in {request.ticker} to sell."])
            continue
        if request.shares > position.shares:
            decide(i, position.shares, [
                f"Long only: the sell of {request.shares} shares is cut to the {position.shares} shares held.",
            ])
        else:
            decide(i, request.shares, [])
        turnover_used += decisions[i].approved_shares * price_of(request.ticker)

    held = {t for t, p in positions.items() if p.shares > 0}
    opening_at_broker = {o.ticker for o in open_orders if o.side == "buy" and o.ticker not in held}
    opened_today: set[str] = set()
    sector_bought: dict[str, float] = {}
    cash_spent = 0.0

    def sector_of(name: str) -> str:
        return name or UNKNOWN_SECTOR

    # A buy still waiting at the broker may fill alongside today's, so its
    # cash and its sector are already spoken for.
    for order in open_orders:
        if order.side != "buy":
            continue
        fact = facts.get(order.ticker)
        # Such an order is usually waiting because its ticker cannot be priced
        # today. Its own limit or stop then stands in for the price, slightly low.
        price = (fact.close if fact is not None and fact.close else None) or order.limit_price or order.protective_stop
        if price:
            sector = sector_of(fact.sector) if fact is not None else UNKNOWN_SECTOR
            cash_spent += order.shares * price
            sector_bought[sector] = sector_bought.get(sector, 0.0) + order.shares * price

    for i in survivors:
        request = requests[i]
        if request.side != "buy":
            continue
        ticker = request.ticker

        if account.day_change_pct is not None and account.day_change_pct <= -limits.daily_loss_limit * 100:
            decide(i, 0, [
                f"Daily loss breaker: equity is {account.day_change_pct:g}% on the day, at or past the "
                f"-{_pct(limits.daily_loss_limit)} limit, so no position may be added to.",
            ])
            continue
        if account.drawdown_pct >= limits.max_drawdown * 100:
            decide(i, 0, [
                f"Drawdown breaker: equity is {account.drawdown_pct:g}% below its high, at or past the "
                f"{_pct(limits.max_drawdown)} limit, so no position may be added to.",
            ])
            continue

        fact = facts.get(ticker)
        if ticker not in universe:
            decide(i, 0, [f"{ticker} is not on the whitelist."])
            continue
        if fact is None or not fact.usable or fact.close is None or fact.close <= 0:
            decide(i, 0, [f"{ticker} has no usable data in today's snapshot."])
            continue
        close = fact.close
        if close < limits.min_price:
            decide(i, 0, [f"Close {close:.2f} is below the minimum price of {limits.min_price:.2f}."])
            continue
        if fact.avg_dollar_volume is None or fact.avg_dollar_volume < limits.min_dollar_volume:
            volume = "unknown" if fact.avg_dollar_volume is None else f"${fact.avg_dollar_volume:,.0f}"
            decide(i, 0, [
                f"Average dollar volume {volume} is below the minimum of ${limits.min_dollar_volume:,.0f}.",
            ])
            continue

        if ticker in positions and positions[ticker].last_close is None:
            decide(i, 0, [f"{ticker} is held but cannot be priced today, so it may not be added to."])
            continue

        stop = request.protective_stop
        if stop is None or not math.isfinite(stop) or stop <= 0 or stop >= close:
            decide(i, 0, [f"A buy needs a positive protective stop below the {close:.2f} close (got {stop})."])
            continue

        opens_new = ticker not in held
        if opens_new:
            count = len(held) + len(opening_at_broker | opened_today | {ticker})
            if count > limits.max_positions:
                decide(i, 0, [
                    f"Position count: this would make {count} positions, above the limit of {limits.max_positions}.",
                ])
                continue
            if len(opened_today) + 1 > limits.max_new_positions_per_day:
                decide(i, 0, [
                    f"New positions today: this would be number {len(opened_today) + 1}, above the limit of "
                    f"{limits.max_new_positions_per_day}.",
                ])
                continue

        shares, reasons = request.shares, []

        def cut(cap: int, sentence: str) -> None:
            nonlocal shares
            if cap < shares:
                shares = cap
                reasons.append(sentence)

        risk_room = limits.max_trade_risk * equity
        cap = _shares_within(risk_room, close - stop)
        cut(cap, (
            f"Per-trade risk: {shares} shares x ${close - stop:,.2f} to the stop is "
            f"${shares * (close - stop):,.2f}, above the {_pct(limits.max_trade_risk)} limit of "
            f"${risk_room:,.2f}; cut to {cap}."
        ))

        held_shares = positions[ticker].shares if ticker in positions else 0
        position_room = limits.max_position * equity
        cap = max(0, _shares_within(position_room, close) - held_shares)
        cut(cap, (
            f"Single position: {held_shares} held + {shares} shares at ${close:,.2f} is "
            f"${(held_shares + shares) * close:,.2f}, above the {_pct(limits.max_position)} limit of "
            f"${position_room:,.2f}; cut to {cap}."
        ))

        sector = sector_of(fact.sector)
        sector_held = sum(p.value for p in positions.values() if sector_of(p.sector) == sector)
        sector_used = sector_held + sector_bought.get(sector, 0.0)
        sector_room = limits.max_sector * equity
        cap = _shares_within(sector_room - sector_used, close)
        cut(cap, (
            f"Sector: {sector} already has ${sector_used:,.2f}, and {shares} more shares at ${close:,.2f} "
            f"would take it above the {_pct(limits.max_sector)} limit of ${sector_room:,.2f}; cut to {cap}."
        ))

        # Cash from sells approved today is not counted: they may not fill.
        cash_floor = limits.min_cash * equity
        cash_left = account.cash - cash_spent
        cap = _shares_within(cash_left - cash_floor, close)
        cut(cap, (
            f"Cash floor: ${cash_left:,.2f} of cash less {shares} shares at ${close:,.2f} would fall below the "
            f"{_pct(limits.min_cash)} floor of ${cash_floor:,.2f}; cut to {cap}."
        ))

        turnover_room = limits.max_daily_turnover * equity
        cap = _shares_within(turnover_room - turnover_used, close)
        cut(cap, (
            f"Turnover: ${turnover_used:,.2f} already approved today plus {shares} shares at ${close:,.2f} would "
            f"exceed the {_pct(limits.max_daily_turnover)} limit of ${turnover_room:,.2f}; cut to {cap}."
        ))

        decide(i, shares, reasons)
        if shares == 0:
            continue
        value = shares * close
        turnover_used += value
        cash_spent += value
        sector_bought[sector] = sector_bought.get(sector, 0.0) + value
        if opens_new:
            opened_today.add(ticker)

    return [decisions[i] for i in range(len(requests))]
