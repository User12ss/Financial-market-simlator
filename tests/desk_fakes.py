"""Offline stand-ins for the desk: scripted headlines, New York times, and a desk built on the synthetic market."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from data.edgar import FilingsRead, NotListed, Unavailable
from data.ledger import Ledger
from data.models import Filing, NewsItem
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


class ScriptedFilings:
    """Filings with the SEC per ticker, as a reader of EDGAR would hand them back.

    A filing is returned from the moment it is added, unless the caller says it holds it already. `broken` holds
    tickers whose list cannot be read; `unreadable` maps a ticker to problems with single filings.
    """

    def __init__(self) -> None:
        self.on_file: dict[str, list[Filing]] = {}
        self.broken: set[str] = set()
        self.funds: set[str] = set()                     # tickers the SEC lists no company under
        self.down: set[str] = set()                      # tickers at which EDGAR turns the reader away
        self.stops: dict[str, str] = {}                  # tickers part-way through whose filings it does
        self.unreadable: dict[str, list[str]] = {}
        self.asked: list[tuple[str, dict, frozenset]] = []

    def add(self, ticker: str, accession: str, title: str, accepted: datetime, kind: str = "event", **more) -> Filing:
        form = {"event": "8-K", "insider": "4", "holder": "SCHEDULE 13D"}[kind]
        filing = Filing(accession=accession, form=form, kind=kind, accepted=accepted, title=title,
                        link=f"https://example.test/{accession}", **more)
        self.on_file.setdefault(ticker, []).append(filing)
        return filing

    def filings(self, ticker, since, known=frozenset(), budget=None) -> FilingsRead:
        self.asked.append((ticker, dict(since), frozenset(known)))
        if ticker in self.funds:
            raise NotListed(f"{ticker} is not in the SEC's list of tickers, so its filings cannot be looked up.")
        if ticker in self.down:
            raise Unavailable(f"EDGAR answered 429 for the list of {ticker}'s filings.")
        if ticker in self.broken:
            raise DataError(f"EDGAR answered 503 for the list of {ticker}'s filings.")
        read = FilingsRead(problems=list(self.unreadable.get(ticker, [])))
        if ticker in self.stops:
            read.problems.append(f"4 X-1: {self.stops[ticker]}")
            read.unavailable = self.stops[ticker]
        wanted = [f for f in self.on_file.get(ticker, []) if f.accession not in known and f.accepted.date() >= since[f.kind]]
        for filing in sorted(wanted, key=lambda f: f.accepted, reverse=True):
            if budget is not None and budget.left <= 0:
                read.waiting += 1
                continue
            if budget is not None:
                budget.left -= 1
            read.filings.append(filing)
        return read


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


def make_desk(tmp_path, llm=None, market=None, news=None, clock=None, arm=ARM, filings=None) -> Desk:
    """A desk on the synthetic market. The clock starts after Friday 13 November's close, when its meeting is due.

    It reads no filings unless it is given a scripted source of them: like a desk with no contact set.
    """
    clock = clock or Clock(ny(13, 18, 30))
    news = news or ScriptedNews()
    builder = SnapshotBuilder(market=market or SyntheticMarket(), macro=SteadyMacro(), news=news, now=clock)
    desk = Desk(
        Arm(arm, llm or desk_llm()), journal=Journal(tmp_path / "journal.sqlite3"),
        ledger=Ledger(tmp_path / "ledger.sqlite3"), builder=builder, universe=UNIVERSE,
        runs_dir=tmp_path / "runs", snapshot_dir=tmp_path / "snapshots", now=clock,
        forecast_dir=tmp_path / "forecast", filings=filings,
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
