"""Orders, fills, positions and accounts: what sizing, the risk gate and a broker pass around.

Nothing here is written by a language model. These are the facts of the
book, and every one of them is produced by code.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["buy", "sell"]
OrderKind = Literal["market", "limit", "stop"]
OrderStatus = Literal["intent", "open", "filled", "cancelled", "expired", "rejected"]


class OrderRequest(BaseModel):
    """An order someone wants placed. Long only: a sell never exceeds what is held."""

    order_id: str = Field(description="Unique and repeatable: the same decision always gets the same id")
    ticker: str
    side: Side
    kind: OrderKind = "market"
    shares: int
    limit_price: float | None = Field(default=None, description="For a limit order")
    stop_price: float | None = Field(default=None, description="Trigger price, for a stop order")
    protective_stop: float | None = Field(
        default=None, description="For a buy: the stop to attach to the position once the buy fills"
    )
    day_only: bool = Field(default=False, description="Expires unfilled at the end of its first session")
    reason: str = ""


class Order(OrderRequest):
    """An order as the broker knows it."""

    status: OrderStatus
    created_session: date = Field(description="The session after whose close the order was created")
    filled_price: float | None = None
    filled_date: date | None = None
    note: str = ""


class Fill(BaseModel):
    order_id: str
    ticker: str
    side: Side
    kind: Literal["market", "limit", "stop", "close"] = Field(
        description="How it filled. 'close' is the broker closing a position whose data stopped."
    )
    shares: int
    price: float
    fill_date: date
    pnl_pct: float | None = Field(default=None, description="For sells: return against the average cost")
    note: str = ""


class Position(BaseModel):
    ticker: str
    shares: int
    avg_cost: float
    entry_date: date
    stop_price: float | None = None
    sector: str = ""
    last_close: float | None = Field(default=None, description="None when today's data is unusable")
    value: float = Field(description="Shares times the last usable close")
    unrealized_pct: float | None = None
    sessions_held: int = 0


class Account(BaseModel):
    account: str
    session_date: date | None = Field(description="The last session the book was brought up to")
    cash: float
    positions_value: float
    equity: float
    initial_cash: float
    high_water: float = Field(description="Highest equity seen at any session's close")
    day_change_pct: float | None = Field(default=None, description="Equity against the previous session's")
    drawdown_pct: float = Field(description="How far equity is below the high-water mark, as a positive percent")


class RiskDecision(BaseModel):
    """What the risk gate did with one requested order, and why."""

    order_id: str
    ticker: str
    side: Side
    requested_shares: int
    approved_shares: int
    verdict: Literal["approved", "reduced", "rejected"]
    reasons: list[str] = Field(default_factory=list, description="One line per rule that cut or refused the order")
