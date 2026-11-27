"""Offline stand-ins for the daily pipeline: a synthetic market, a clock and a scripted model."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from data.market_calendar import _calendar
from data.models import Fundamentals
from data.providers import BAR_COLUMNS, DataError
from data.snapshot import SnapshotBuilder
from journal.store import Journal
from models.planning import ContingencyDraft, PlanDraft, ProposedEventDraft, ScenarioDraft
from models.signals import (
    Citation, DebateArgumentDraft, DebateRulingDraft, DomainAssessmentDraft, ProposalDraft, ProposalSetDraft,
    SectorCall, TickerViewDraft,
)
from pipeline.daily import Arm, DailyPipeline
from utils.llm_client import Usage, UsageLog

ORIGIN = date(2025, 1, 2)            # first session of every synthetic price path
UNIVERSE = ["UPA", "UPB", "UPC", "DOWN", "GONE"]


def day(number: int) -> datetime:
    """A moment after the close on that day of November 2026 (17:30 in New York)."""
    return datetime(2026, 11, number, 22, 30, tzinfo=timezone.utc)


def session(number: int) -> date:
    return date(2026, 11, number)


class Clock:
    """A settable time source shared by the pipeline and the snapshot builder."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SyntheticMarket:
    """Smooth price paths that depend only on the date, so later sessions extend earlier ones.

    Each ticker compounds at its own daily drift from a base price. A bar
    opens at the previous close. `splits` maps (ticker, date) to a ratio.

    Splits behave as they do at the real source: once a split has taken
    effect, every bar returned, earlier ones included, is on the new share
    scale. Data fetched before the split's date knows nothing of it.
    """

    def __init__(
        self,
        drift: dict[str, float] | None = None,
        base: dict[str, float] | None = None,
        volume: dict[str, float] | None = None,
        missing: set[str] | None = None,
        splits: dict[tuple[str, date], float] | None = None,
        unlisted_splits: dict[tuple[str, date], float] | None = None,
        turns: dict[str, tuple[date, float]] | None = None,
        sectors: dict[str, str] | None = None,
        fail: bool = False,
    ) -> None:
        self.drift = {"UPA": 0.003, "UPB": 0.002, "UPC": 0.001, "DOWN": -0.002, **(drift or {})}
        self.base = base or {}
        self.volume = volume or {}
        self.missing = {"GONE"} if missing is None else missing
        self.splits = splits or {}
        # Splits the source applies to its prices without listing them: bad data.
        self.unlisted_splits = unlisted_splits or {}
        # From the given date on, the ticker compounds at the given daily rate instead.
        self.turns = turns or {}
        self.sectors = sectors or {}
        self.fail = fail
        self.requested: list[list[str]] = []

    def path(self, symbol: str, end: date) -> pd.DataFrame:
        sessions = [stamp.date() for stamp in _calendar().sessions_in_range(pd.Timestamp(ORIGIN), pd.Timestamp(end))]
        drift, base = self.drift.get(symbol, 0.0005), self.base.get(symbol, 100.0)
        scale = 1.0   # every split in effect by `end` restates the whole history
        for (ticker, split_date), ratio in {**self.splits, **self.unlisted_splits}.items():
            if ticker == symbol and split_date <= end:
                scale *= ratio
        turn_date, turn_drift = self.turns.get(symbol, (None, drift))
        rows, level = [], base / (1 + drift)
        for bar_date in sessions:
            previous = level
            level *= 1 + (turn_drift if turn_date is not None and bar_date >= turn_date else drift)
            close, open_ = level / scale, (previous if rows else level) / scale
            rows.append({
                "open": open_, "high": max(open_, close) * 1.002, "low": min(open_, close) * 0.998,
                "close": close, "adj_close": close, "volume": self.volume.get(symbol, 5_000_000.0),
                "dividends": 0.0, "splits": self.splits.get((symbol, bar_date), 0.0),
            })
        return pd.DataFrame(rows, index=sessions, columns=BAR_COLUMNS)

    def daily_bars(self, symbols, start, end):
        self.requested.append(list(symbols))
        if self.fail:
            raise DataError("Yahoo Finance price download failed: scripted outage")
        frames = {}
        for symbol in symbols:
            if symbol in self.missing:
                continue
            frame = self.path(symbol, end)
            frames[symbol] = frame[frame.index >= start]
        return frames

    def fundamentals(self, symbol):
        return Fundamentals(name=f"{symbol} Corp", quote_type="EQUITY", sector=self.sectors.get(symbol, "Technology"),
                            market_cap=250e9, trailing_pe=24.0, forward_pe=21.0, profit_margin=0.2, beta=1.1)

    def next_earnings(self, symbol, after):
        return None


class NoNews:
    def headlines(self, symbol, limit):
        return []


class SteadyMacro:
    """Every macro series, observed daily, so no series is ever missing or stale."""

    def series(self, series_id, start):
        return [(start + timedelta(days=offset), 100.0 + offset * 0.01) for offset in range(900)]


# ── Canned model replies ─────────────────────────────────────────────────────

def assessment(stance="supportive", confidence=0.6, tilts=(), summary="Conditions in this domain are steady."):
    return DomainAssessmentDraft(
        stance=stance, summary=summary, key_risk="A sharp reversal in this domain.",
        sector_tilts=[SectorCall(sector=sector, tilt=tilt) for sector, tilt in tilts],
        confidence=confidence, remark="Nothing here stands in the way.",
    )


def view(direction="bullish", confidence=0.6, horizon=10, thesis="The trend is intact and nothing argues against it.",
         evidence=(), stop=None):
    return TickerViewDraft(
        direction=direction, confidence=confidence, horizon_days=horizon, thesis=thesis,
        catalysts="Continued demand.", invalidation="A close below the 50-day average.",
        stop_price=stop, stop_reason="" if stop is None else "Just below the 50-day average.",
        evidence=[Citation(metric=metric, value=value) for metric, value in evidence],
        remark="I would own this one.",
    )


def argument(text="The case rests on the trend."):
    return DebateArgumentDraft(argument=text, strongest_evidence="The price is above its averages.",
                               remark="That is my case.")


def ruling(stance="own", confidence=0.55):
    return DebateRulingDraft(stance=stance, confidence=confidence, invalidation="A break of the trend.",
                             reasoning="The trend argument carried more weight.", remark="Own it, modestly.")


def proposals(*items: tuple[str, str, float]):
    return ProposalSetDraft(
        proposals=[ProposalDraft(ticker=t, action=a, confidence=c, reason=f"Based on the view of {t}.")
                   for t, a, c in items],
        summary="A cautious day.", remark="Here is today's list.",
    )


def proposed_event(label="The recall widens", first=1, last=5, probability=0.2, direction="down", low=4.0, high=8.0,
                   check="headline", keywords=("widens",), level=None, check_text="A headline says the recall has widened.",
                   basis="The recall named in the headlines.", cited=(1,)):
    return ProposedEventDraft(
        label=label, first_session=first, last_session=last, probability=probability, direction=direction,
        low_pct=low, high_pct=high, check_kind=check, check_keywords=list(keywords), check_level=level,
        check_text=check_text, basis=basis, cited=list(cited),
    )


def scenario(*events: ProposedEventDraft, remark="Nothing particular to add."):
    """The scenario analyst's answer for one name. With no events it is the usual one: nothing to add."""
    return ScenarioDraft(events=list(events), remark=remark)


def condition(kind="close_below_average", action="exit", reason="The trend would be broken.", level=None, average=None,
              on_session=None, value=None, **fields):
    """One condition of a drafted plan. By default a close below the 200-day average.

    A draft gives one number, `value`. Here it may be given under the name it has for the kind: a `level` for a
    price or a chance, an `average`, or the session a day falls `on_session`.
    """
    if value is None:
        given = {"close_below_average": 200 if average is None else average, "date": on_session}.get(kind, level)
        value = 0 if given is None else given
    return ContingencyDraft(condition=kind, action=action, reason=reason, value=value, **fields)


def plan_draft(wrong=None, *others, thesis="The trend carries it.", change="", remark="That is the plan."):
    """The portfolio manager's plan for one name. Given no condition it is out on a close below the 200-day
    average, which a rising name's plan cannot keep (that average is under its stop): see `usual_plan`."""
    return PlanDraft(thesis=thesis, wrong_if=wrong or condition(), contingencies=list(others), change=change,
                     remark=remark)


def shown(user_message: str) -> tuple[str, float, float]:
    """The name a plan was asked for, with the last close and the stop the manager was shown for it."""
    ticker = re.search(r"Write the plan for (\S+)\.", user_message).group(1)
    close, stop = re.search(r"- Last close ([\d,.]+)\. Stop ([\d,.]+)\.", user_message).groups()
    return ticker, float(close.replace(",", "")), float(stop.rstrip(".").replace(",", ""))


def between(share: float, close: float, stop: float) -> float:
    """A price that share of the way from the close down to the stop, to the cent."""
    return round(close - share * (close - stop), 2)


def usual_plan(system_prompt: str, user_message: str, *others, change="") -> PlanDraft:
    """The plan a scripted manager writes when a test gives it none: out on a price six tenths of the way from the
    last close down to the stop it was shown.

    A name bought at the meeting can keep that: its stop is two usual daily ranges under the close, and a plan's
    price has to lie at least one range under it and above the stop.
    """
    _, close, stop = shown(user_message)
    return plan_draft(condition("price_below", "exit", level=between(0.6, close, stop)), *others, change=change)


class ScriptedLLM:
    """Stands in for LLMClient. `replies` maps a schema to an object, an exception, or a
    function of (system prompt, user message) that returns either."""

    def __init__(self, cost_per_call: float | None = None, problems: list[str] | None = None) -> None:
        self.replies: dict[type, object] = {
            DomainAssessmentDraft: assessment(),
            TickerViewDraft: view(),
            DebateArgumentDraft: argument(),
            DebateRulingDraft: ruling(),
            ProposalSetDraft: proposals(),
            ScenarioDraft: scenario(),
            PlanDraft: usual_plan,
        }
        self.calls: list[tuple[str, str, str]] = []     # schema name, system prompt, user message
        self.usage = UsageLog()
        self.cost_per_call = cost_per_call
        self._problems = problems or []

    def problems(self) -> list[str]:
        return self._problems

    def count(self, schema: type) -> int:
        return sum(1 for name, _, _ in self.calls if name == schema.__name__)

    def messages(self, schema: type) -> list[str]:
        return [user for name, _, user in self.calls if name == schema.__name__]

    def complete_structured(self, system_prompt, user_message, schema, fast=False):
        self.calls.append((schema.__name__, system_prompt, user_message))
        self.usage.add(Usage("fake", "scripted", 100, 50, 0.5, self.cost_per_call))
        reply = self.replies[schema]
        if callable(reply):
            reply = reply(system_prompt, user_message)
        if isinstance(reply, Exception):
            raise reply
        return reply


def by_ticker(replies: dict[str, object], default: object):
    """A view reply that depends on which ticker the analyst was asked about."""
    def reply(system_prompt: str, user_message: str):
        ticker = re.search(r"Give your view of (\S+)\.", user_message).group(1)
        return replies.get(ticker, default)
    return reply


def by_agent(replies: dict[str, object], default: object):
    """An assessment reply that depends on which macro agent is asking."""
    def reply(system_prompt: str, user_message: str):
        for name, value in replies.items():
            if f"You are the {name}" in system_prompt:
                return value
        return default
    return reply


def make_pipeline(tmp_path, llm=None, arms=None, market=None, clock=None, universe=None) -> DailyPipeline:
    clock = clock or Clock(day(13))
    builder = SnapshotBuilder(market=market or SyntheticMarket(), macro=SteadyMacro(), news=NoNews(), now=clock)
    return DailyPipeline(
        arms or [Arm("fake/a", llm or ScriptedLLM())], builder=builder,
        journal=Journal(tmp_path / "journal.sqlite3"), universe=universe or UNIVERSE,
        runs_dir=tmp_path / "runs", snapshot_dir=tmp_path / "snapshots", now=clock,
    )
