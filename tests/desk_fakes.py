"""Offline stand-ins for the desk: scripted headlines, New York times, and a desk built on the synthetic market."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from data.ledger import Ledger
from data.models import NewsItem
from data.providers import DataError
from data.snapshot import SnapshotBuilder
from journal.store import Journal
from models.signals import CheckinDecisionDraft, CheckinDraft, ProposalSetDraft, ViewUpdateDraft
from pipeline.daily import Arm
from pipeline.desk import Desk
from tests.pipeline_fakes import UNIVERSE, Clock, ScriptedLLM, SteadyMacro, SyntheticMarket, proposals

ARM = "desk/fake/a"


def ny(day_number: int, hour: int, minute: int = 0) -> datetime:
    """That New York wall-clock time on a day of November 2026, as UTC. New York is five hours behind then."""
    return datetime(2026, 11, day_number, hour, minute, tzinfo=timezone.utc) + timedelta(hours=5)


class ScriptedNews:
    """Headlines per ticker. A story is returned from the moment it is added, like a real feed.

    `stories` maps a ticker to (title, published or None). `broken` holds
    tickers whose fetch fails.
    """

    def __init__(self) -> None:
        self.stories: dict[str, list[tuple[str, datetime | None]]] = {}
        self.broken: set[str] = set()
        self.asked: list[str] = []

    def add(self, ticker: str, title: str, published: datetime | None = None) -> None:
        self.stories.setdefault(ticker, []).insert(0, (title, published))

    def headlines(self, symbol, limit):
        self.asked.append(symbol)
        if symbol in self.broken:
            raise DataError(f"Yahoo Finance news search failed for {symbol}: scripted outage")
        return [
            NewsItem(title=title, publisher="Wire", published=published,
                     link=f"https://example.test/{symbol}/{abs(hash(title)) % 10_000_000}")
            for title, published in self.stories.get(symbol, [])
        ][:limit]


def update(effect="unchanged", reason="Nothing here bears on the thesis.", cited=()):
    return ViewUpdateDraft(effect=effect, reason=reason, cited=list(cited), remark="That is where I stand.")


def decisions(*items: tuple[str, str]):
    return CheckinDraft(
        decisions=[CheckinDecisionDraft(ticker=t, action=a, reason=f"Rests on the update on {t}.") for t, a in items],
        summary="A quiet check-in.", remark="Here is what I would do.",
    )


def update_by_ticker(replies: dict[str, object], default: object):
    """An update reply that depends on which ticker the analyst was asked about."""
    def reply(system_prompt: str, user_message: str):
        ticker = re.search(r"change the standing view of (\S+)\?", user_message).group(1)
        return replies.get(ticker, default)
    return reply


def desk_llm() -> ScriptedLLM:
    llm = ScriptedLLM()
    llm.replies[ViewUpdateDraft] = update()
    llm.replies[CheckinDraft] = decisions()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7), ("UPB", "buy", 0.6))
    return llm


def make_desk(tmp_path, llm=None, market=None, news=None, clock=None, arm=ARM) -> Desk:
    """A desk on the synthetic market. The clock starts after Friday 13 November's close, when its meeting is due."""
    clock = clock or Clock(ny(13, 18, 30))
    news = news or ScriptedNews()
    builder = SnapshotBuilder(market=market or SyntheticMarket(), macro=SteadyMacro(), news=news, now=clock)
    desk = Desk(
        Arm(arm, llm or desk_llm()), journal=Journal(tmp_path / "journal.sqlite3"),
        ledger=Ledger(tmp_path / "ledger.sqlite3"), builder=builder, universe=UNIVERSE,
        runs_dir=tmp_path / "runs", snapshot_dir=tmp_path / "snapshots", now=clock,
        forecast_dir=tmp_path / "forecast",
    )
    desk.clock, desk.news, desk.llm = clock, news, desk.arm.llm
    return desk


def hold(desk: Desk, moment: datetime):
    """Move the clock and hold whatever meeting is due then."""
    desk.clock.now = moment
    return desk.meet()


def round_at(desk: Desk, moment: datetime):
    """Move the clock and run one round of the watch."""
    desk.clock.now = moment
    return desk.watch()
