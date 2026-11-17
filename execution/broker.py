"""What the trading pipeline needs from a broker, whichever broker it is.

The paper broker implements this now. A real broker's adapter will implement
the same five calls later, and nothing above this line has to change.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from models.trading import Account, Order, OrderRequest, Position


class Broker(Protocol):
    def account(self) -> Account:
        """Cash, equity and how the account stands against its high-water mark."""

    def positions(self) -> dict[str, Position]:
        """Current positions by ticker."""

    def orders(self, open_only: bool = True) -> list[Order]:
        """Orders the broker knows about, oldest first."""

    def submit(self, request: OrderRequest, session: date) -> Order:
        """Place an order created after `session`'s close.

        Submitting the same `order_id` twice must return the existing order
        and place nothing new: that is what makes a rerun safe.
        """

    def cancel(self, order_id: str, note: str = "") -> None:
        """Cancel an order that is still open. Anything else is left as it is."""

    def withdraw(self, order_id: str) -> bool:
        """Take back an order that has not traded, so that its id can be used again.

        Returns whether there was such an order. Used when a session's
        decisions are made again and one of them changed.
        """
