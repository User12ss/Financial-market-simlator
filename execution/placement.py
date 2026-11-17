"""Placing orders so that a crash at any point cannot place one twice.

Each order goes through three steps, and each step is saved before the next
begins:

    intent      the pipeline records that it means to place the order
    submitted   the order has been handed to the broker
    confirmed   the broker has been asked, and it has the order

An order's id is fixed by the decision that produced it (session, account,
ticker, side), and a broker returns the existing order when the same id is
submitted again. So a rerun after a crash walks the same steps and places
nothing new: an order left at "intent" or "submitted" is simply taken up
where it stopped.

Before placing, `reconcile` takes back anything an earlier attempt at the same
session placed that the present decision does not ask for.
"""

from __future__ import annotations

from datetime import date

from execution.broker import Broker
from journal.store import Journal
from models.trading import Order, OrderRequest


def _state(journal: Journal, order_id: str) -> str | None:
    rows = journal.rows("SELECT state FROM order_log WHERE order_id = ?", (order_id,))
    return rows[0]["state"] if rows else None


def _set_state(journal: Journal, order_id: str, state: str) -> None:
    with journal.transaction():
        journal.write("UPDATE order_log SET state = ? WHERE order_id = ?", (state, order_id))


def place(
    journal: Journal, broker: Broker, account: str, run_id: str, session: date, requests: list[OrderRequest],
) -> list[Order]:
    """Place each request once, whatever happened in earlier attempts. Returns the broker's orders."""
    placed = []
    for request in requests:
        state = _state(journal, request.order_id)
        if state is None:
            with journal.transaction():
                journal.write(
                    "INSERT INTO order_log VALUES (?, ?, ?, ?, ?, 'intent')",
                    (request.order_id, account, run_id, session.isoformat(), request.model_dump_json()),
                )
            state = "intent"
        if state == "intent":
            broker.submit(request, session)
            _set_state(journal, request.order_id, "submitted")
        known = {order.order_id: order for order in broker.orders(open_only=False)}
        if request.order_id not in known:
            raise RuntimeError(
                f"Order {request.order_id} was submitted but the broker does not have it. Nothing more was placed."
            )
        if state != "confirmed":
            _set_state(journal, request.order_id, "confirmed")
        placed.append(known[request.order_id])
    return placed


def reconcile(
    journal: Journal, broker: Broker, account: str, session: date, requests: list[OrderRequest],
) -> list[Order]:
    """Take back what an earlier attempt at this session placed and this one no longer wants.

    A session can be decided more than once: after a crash, after `--force`,
    or with the kill switch set in between. The orders that stand must be the
    ones the latest decision asks for, no others, and exactly as it asks for
    them. Any of the session's orders that is not among `requests`, or
    differs from its request, is withdrawn, and its log entry with it, so
    `place` can put the right one in its stead. Protective stops are left
    alone: they belong to positions, not to a session's decisions. Returns
    the orders withdrawn.

    The log is also brought back into line with the broker: an entry for an
    order the broker does not have at all is dropped, so `place` starts that
    order again from the beginning. With the paper broker the withdrawals and
    the log changes are saved together; with a broker that keeps its own
    books they cannot be, and that last rule is what mends a crash between
    the two.
    """
    wanted = {request.order_id: request for request in requests}
    withdrawn = []
    with journal.transaction():
        for order in broker.orders():
            if order.created_session != session or order.kind == "stop":
                continue
            request = wanted.get(order.order_id)
            if request is None or OrderRequest(**order.model_dump(include=set(OrderRequest.model_fields))) != request:
                broker.withdraw(order.order_id)
                withdrawn.append(order)
        # After the withdrawals, the broker has exactly the session's orders that still stand.
        # A log entry for anything else, withdrawn just now or never there, is dropped.
        at_broker = {order.order_id for order in broker.orders(open_only=False)}
        for row in journal.rows(
            "SELECT order_id FROM order_log WHERE account = ? AND session_date = ?", (account, session.isoformat())
        ):
            if row["order_id"] not in at_broker:
                journal.write("DELETE FROM order_log WHERE order_id = ?", (row["order_id"],))
    return withdrawn
