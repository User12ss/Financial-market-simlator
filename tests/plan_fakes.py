"""Offline stand-ins for the desk's plans: a market whose days can be set one by one, and scripted plans."""

from __future__ import annotations

import re
from datetime import date

import pandas as pd

from data.market_calendar import _calendar
from data.providers import BAR_COLUMNS
from models.planning import PlanDraft
from tests.desk_fakes import ARM, desk_llm, make_desk
from tests.pipeline_fakes import ORIGIN, SyntheticMarket, condition, plan_draft

FRIDAY, MONDAY, TUESDAY, WEDNESDAY, THURSDAY = (date(2026, 11, day) for day in (13, 16, 17, 18, 19))


class MovingMarket(SyntheticMarket):
    """The synthetic market, with any single day of any name set by hand.

    `moves` gives a day's return in place of the name's drift, `lows` and
    `highs` a day's low or high as a price, and `reports` the date of a
    name's next earnings report. Everything before a day that is set is what
    it would have been anyway, so a day can be set after earlier days have
    already been read.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.moves: dict[tuple[str, date], float] = {}
        self.lows: dict[tuple[str, date], float] = {}
        self.highs: dict[tuple[str, date], float] = {}
        self.reports: dict[str, date] = {}

    def path(self, symbol: str, end: date) -> pd.DataFrame:
        sessions = [stamp.date() for stamp in _calendar().sessions_in_range(pd.Timestamp(ORIGIN), pd.Timestamp(end))]
        drift, base = self.drift.get(symbol, 0.0005), self.base.get(symbol, 100.0)
        rows, level = [], base / (1 + drift)
        for bar_date in sessions:
            previous = level
            level *= 1 + self.moves.get((symbol, bar_date), drift)
            open_ = previous if rows else level
            rows.append({
                "open": open_, "high": self.highs.get((symbol, bar_date), max(open_, level) * 1.002),
                "low": self.lows.get((symbol, bar_date), min(open_, level) * 0.998), "close": level,
                "adj_close": level, "volume": self.volume.get(symbol, 5_000_000.0), "dividends": 0.0, "splits": 0.0,
            })
        return pd.DataFrame(rows, index=sessions, columns=BAR_COLUMNS)

    def close_on(self, symbol: str, day: date) -> float:
        return float(self.path(symbol, day)["close"].iloc[-1])

    def set_close(self, symbol: str, day: date, close: float) -> None:
        """Make the name close at `close` on `day`. The day before has to be as it will stay."""
        before = float(self.path(symbol, day)["close"].iloc[-2])
        self.moves[(symbol, day)] = close / before - 1

    def next_earnings(self, symbol, after):
        report = self.reports.get(symbol)
        return report if report is not None and report > after else None


def shown(user_message: str) -> tuple[str, float, float]:
    """The name a plan was asked for, with the last close and the stop the manager was shown for it."""
    ticker = re.search(r"Write the plan for (\S+)\.", user_message).group(1)
    close, stop = re.search(r"- Last close ([\d,.]+)\. Stop ([\d,.]+)\.", user_message).groups()
    return ticker, float(close.replace(",", "")), float(stop.rstrip(".").replace(",", ""))


def between(share: float, close: float, stop: float) -> float:
    """A price that share of the way from the close down to the stop, to the cent."""
    return round(close - share * (close - stop), 2)


def plans_by(make):
    """A scripted manager: `make(ticker, close, stop)` returns the plan for that name, or None for the usual one."""
    def reply(system_prompt: str, user_message: str):
        ticker, close, stop = shown(user_message)
        return make(ticker, close, stop) or plan_draft()
    return reply


def wrong_below(share: float = 0.6, *others, only: str | None = "UPA"):
    """Plans that are out on a price that share of the way down to the stop. For `only`, or for every name.

    The stop the sizing rule sets is two usual daily ranges under the close, and a plan's price has to be at
    least one range under it: a share of a half or more.
    """
    def make(ticker, close, stop):
        if only is not None and ticker != only:
            return None
        return plan_draft(condition("price_below", "exit", level=between(share, close, stop)),
                          *(other(close, stop) if callable(other) else other for other in others))
    return plans_by(make)


def plan_desk(tmp_path, plan=None, market=None, clock=None, llm=None):
    """A desk on a market whose days can be set, with a scripted manager. Its Friday meeting is due."""
    llm = llm or desk_llm()
    if plan is not None:
        llm.replies[PlanDraft] = plan
    desk = make_desk(tmp_path, llm=llm, market=market or MovingMarket(), clock=clock)
    desk.market = desk.builder.market
    return desk


def plan_events(desk, run_id, kind=None, ticker=None):
    return [event for event in desk.journal.events(run_id)
            if (event.stage == "plans" or event.kind in ("plan_action", "stop_raised", "plan_call"))
            and (kind is None or event.kind == kind) and (ticker is None or event.ticker == ticker)]


def standing(desk, ticker="UPA"):
    return desk.journal.standing_plans(ARM).get(ticker)


def versions(desk, ticker="UPA"):
    return [plan for plan in desk.journal.plans(ARM) if plan.ticker == ticker]


def orders(desk, side=None, ticker=None, status="open"):
    return [tuple(row) for row in desk.journal.rows(
        "SELECT ticker, side, kind, shares, status FROM paper_orders WHERE account = ? ORDER BY rowid", (ARM,))
        if (side is None or row["side"] == side) and (ticker is None or row["ticker"] == ticker)
        and (status is None or row["status"] == status)]
