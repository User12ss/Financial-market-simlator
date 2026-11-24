"""What the desk works out before it acts: events that may happen, and how each trade may end.

As everywhere in the pipeline, a class whose name ends in `Draft` is what a
model is asked for, and the class without the suffix is what code keeps after
checking it. Nothing downstream reads a draft.

An event a seat proposes is a judgement. It carries a probability nobody
measured, and it is kept apart from everything measured for that reason: it is
simulated only in the rehearsal, never in the forecast, and it is scored once
its dates have passed, where code can tell whether it happened.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from models.forecast import Backward
from models.signals import Draft

# How code can tell afterwards that a proposed event happened. "other" is a check in words only:
# such an event may be rehearsed, and is never scored.
CheckKind = Literal["headline", "close_below", "close_above", "day_move", "other"]


class ProposedEventDraft(Draft):
    label: str = Field(description="What would happen, in a few words")
    first_session: int = Field(description="The first trading day it could fall on, counted from today: 1 is the next session")
    last_session: int = Field(description="The last trading day it could fall on, counted the same way")
    probability: float = Field(description="Your probability that it happens within those days, above 0 and below 1")
    direction: Literal["up", "down"] = Field(description="Which way it would move the price on the day")
    low_pct: float = Field(description="The smallest one-day move it would cause, in percent, above zero")
    high_pct: float = Field(description="The largest one-day move it would cause, in percent")
    check_kind: CheckKind = Field(
        description="How anyone could tell afterwards that it happened. headline: a headline about the ticker "
                    "contains one of check_keywords. close_below or close_above: a close beyond check_level. "
                    "day_move: a one-day move of at least low_pct in the direction given. other: none of these"
    )
    check_keywords: list[str] = Field(default_factory=list, description="For headline: one to four words to look for")
    check_level: float | None = Field(default=None, description="For close_below and close_above: the price")
    check_text: str = Field(description="The public information that would show it has happened (1 sentence)")
    cited: list[int] = Field(description="The numbers of the headlines it follows from, as numbered in the list you were shown. At least one")
    basis: str = Field(description="How it follows from those headlines (1 sentence)")


class ScenarioDraft(Draft):
    events: list[ProposedEventDraft] = Field(description="Events the simulation does not already contain. None is a normal answer")
    remark: str = Field(description="One short sentence, in your own voice, said to the desk")


class ProposedEvent(BaseModel):
    """An event a seat proposed for one name, after the checks. A subjective probability, marked as such wherever it is shown."""

    key: str = Field(description="Its number among the name's events at this meeting")
    ticker: str
    label: str
    first_session: int
    last_session: int
    first_on: date
    last_on: date
    probability: float
    direction: Literal["up", "down"]
    low_pct: float
    high_pct: float
    check_kind: CheckKind
    check_keywords: list[str] = Field(default_factory=list)
    check_level: float | None = None
    check_text: str
    basis: str
    cited: list[str] = Field(default_factory=list, description="The ledger's ids of the headlines it follows from")
    cited_titles: list[str] = Field(default_factory=list)
    data_warnings: list[str] = Field(default_factory=list)


class BackwardSet(BaseModel):
    """Every name's paths read backwards at one meeting, with what the scenario seat proposed.

    `measured` rests on the forecast's own simulation and nothing else.
    `rehearsed` is the same reading with the proposed events simulated too,
    for the names that have any: it is for discussion, and no order may rest
    on it.
    """

    arm: str
    session_date: date
    run_id: str
    created_at: datetime
    params_version: int
    seed: int
    paths: int
    measured: list[Backward] = Field(default_factory=list)
    asked_at: datetime | None = Field(
        default=None,
        description="The cut-off of what the scenario analyst was shown: a headline the ledger first saw later is "
                    "news to it, and only such a headline can show that an event it proposed has happened",
    )
    events: list[ProposedEvent] = Field(default_factory=list)
    rehearsed: list[Backward] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


# ── What they turned out to be worth ─────────────────────────────────────────

class OutcomeScore(BaseModel):
    """The chances given to a trade's three endings, set against the one that happened."""

    arm: str
    session_date: date
    ticker: str
    basis: Literal["measured", "rehearsed"] = Field(description="Which reading the chances come from")
    sessions: int
    matures_on: date
    status: Literal["scored", "pending", "unpriced"]
    chances: dict[str, float] = Field(description="The chance given to each ending: good, bad, middle")
    plain: dict[str, float] = Field(
        default_factory=dict,
        description="The same from the plain forecast: no drift, no states, no jumps, historical volatility",
    )
    outcome: Literal["good", "bad", "middle"] | None = None
    settled_on: date | None = Field(default=None, description="The session that settled it")
    brier: float | None = Field(default=None, description="Sum over the three endings of (chance - happened) squared: 0 is perfect, 2 the worst")
    plain_brier: float | None = None
    scored_on: date | None = None


class EventScore(BaseModel):
    """A proposed event, once its dates have passed: did it happen, as far as code can tell?"""

    arm: str
    session_date: date
    ticker: str
    key: str
    label: str
    probability: float
    last_on: date
    status: Literal["scored", "pending", "unpriced", "unread", "cannot_be_judged"] = Field(
        description="cannot_be_judged: its check is in words only, and it is never scored. unread: no ledger at "
                    "hand read the name's headlines through its days without a gap, so nothing says whether it "
                    "happened; it is tried again each time"
    )
    happened: bool | None = None
    judged_by: str = Field(default="", description="What decided it, in words")
    brier: float | None = Field(default=None, description="(probability - happened) squared")
    scored_on: date | None = None
