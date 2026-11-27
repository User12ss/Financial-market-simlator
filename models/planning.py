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


# ── The plan for a trade ─────────────────────────────────────────────────────

# What a plan may watch for. Every kind is something code can check.
ConditionKind = Literal[
    "price_below", "price_above", "close_below_average", "date", "chance_below", "chance_above", "headline",
]
# What a plan may say to do. The list is closed: nothing in it opens a position or adds to one.
ActionKind = Literal["exit", "trim", "withdraw", "tighten_stop", "call_meeting"]
# The actions code carries out by itself when a plan's condition holds. Each takes risk off.
AUTOMATIC: tuple[str, ...] = ("exit", "trim", "withdraw", "tighten_stop")
# The conditions that may set such an action off: prices and dates. Outside text cannot bring any of them about.
HARD: tuple[str, ...] = ("price_below", "price_above", "close_below_average", "date")
# The simulation's own chances. They are the desk's own arithmetic, not outside text, but a forecast earns the
# right to sell by being reconciled: until the setting says so (PLAN_CHANCE_ACTS) they call a meeting and nothing else.
CHANCES: tuple[str, ...] = ("chance_below", "chance_above")
AVERAGES: tuple[int, ...] = (20, 50, 200)
Ending = Literal["good", "bad", "middle"]


class ContingencyDraft(Draft):
    """One condition as the manager drafts it. The number every kind of condition needs is one field, and it has
    to be given: asked for a field of its own per kind, each of them optional, the local model named a kind and
    left its number out, ten plans out of ten."""

    condition: ConditionKind = Field(
        description="What to watch for. price_below / price_above: the price reaches `value`. close_below_average: "
                    "a close below the `value`-day average. date: the close of the session `value` trading days "
                    "from today. chance_below / chance_above: the simulation's chance of the `ending` passes "
                    "`value`. headline: a later headline about the ticker has one of `keywords`"
    )
    value: float = Field(
        description="The number the condition needs. price_below / price_above: the price. close_below_average: 20, "
                    "50 or 200. date: how many trading days from today, 1 being the next session. chance_below / "
                    "chance_above: the chance, above 0 and below 1. headline: 0"
    )
    ending: Ending | None = Field(default=None, description="For the chance conditions: good (the target first), bad (the stop first) or middle")
    keywords: list[str] = Field(default_factory=list, description="For headline: one to four distinctive words")
    action: ActionKind = Field(
        description="What to do when it holds. exit: sell the position. trim: sell half. withdraw: take back a buy "
                    "that has not filled. tighten_stop: raise the stop to `new_stop`. call_meeting: bring the desk together"
    )
    new_stop: float | None = Field(default=None, description="For tighten_stop: the new stop, above the present one and below the last close")
    reason: str = Field(description="Why this condition and this action (1 sentence)")


class PlanDraft(Draft):
    thesis: str = Field(description="Why the desk owns it, or is about to (1-2 sentences)")
    wrong_if: ContingencyDraft = Field(
        description="What would show the plan is wrong: a price, an average or a date, with exit or trim as its action"
    )
    contingencies: list[ContingencyDraft] = Field(description="Further conditions and what to do on each. None is a normal answer")
    change: str = Field(description="For a plan being looked at again: what you changed and why (1 sentence). Empty for a first plan")
    remark: str = Field(description="One short sentence, in your own voice, said to the desk")


class Condition(BaseModel):
    kind: ConditionKind
    level: float | None = None
    average: int | None = None
    on: date | None = None
    ending: Ending | None = None
    keywords: list[str] = Field(default_factory=list)


class Contingency(BaseModel):
    """One thing a plan watches for, and what happens when it holds."""

    key: str = Field(description="Its name within the plan: time, wrong, or a number")
    condition: Condition
    action: ActionKind
    new_stop: float | None = None
    reason: str
    source: Literal["manager", "rule"] = Field(description="rule: every plan has it, whoever wrote the rest")
    automatic: bool = Field(description="Whether code carries the action out itself when the condition holds")


class Plan(BaseModel):
    """What the desk means to do with one name, in a form code can hold it to. Numbered: looked at again, it is a new version."""

    arm: str
    ticker: str
    opened: date = Field(description="The session its first version was made after: it tells one plan for a name from a later one")
    version: int
    run_id: str = Field(description="The close meeting that made this version")
    session_date: date
    created_at: datetime
    source: Literal["manager", "default"] = Field(
        description="default: the manager gave no plan that passed the checks, and the desk's own stands in"
    )
    held: bool = Field(description="False: the buy was waiting to fill at the next open when this version was made")
    thesis: str
    direction: str = Field(default="", description="The analyst's view when this version was made")
    horizon: int = Field(description="Sessions the trade was given when the plan was first made")
    ends_on: date = Field(description="The session at whose close the time runs out. Fixed by the first version")
    close: float
    stop: float | None = None
    stop_from: str = ""
    target: float | None = Field(default=None, description="A notional target: nobody names one yet")
    chances: dict[str, float] = Field(default_factory=dict, description="The three endings' chances at this meeting, from the simulation alone")
    contingencies: list[Contingency] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list, description="What differs from the version before, worked out by code")
    basis: str = Field(default="", description="Why it was looked at again: what code found changed, and the manager's own words")
    refused: list[str] = Field(default_factory=list, description="What of the manager's draft was refused, and why")
    data_warnings: list[str] = Field(default_factory=list)
    closed_on: date | None = Field(default=None, description="The session at whose close meeting the plan was found to be over")
    closed_why: str = ""


class Trigger(BaseModel):
    """A plan's condition found to hold: when, on what evidence, and what became of its action."""

    arm: str
    ticker: str
    opened: date
    version: int = Field(description="The version of the plan that stood when it was found")
    identity: str = Field(description="What it watched for and what it does, as one string: a condition fires once in a plan's life")
    contingency: Contingency
    fired_at: datetime
    session_date: date = Field(description="The trading day it was found on")
    found_by: Literal["watch", "close"] = Field(description="A round of the watch during the session, or the close meeting")
    evidence: str = Field(description="What was seen, in words")
    status: Literal["fired", "done", "not_done", "heard"] = Field(
        default="fired",
        description="fired: found, its action still to come. done: carried out. not_done: it could not be, and why "
                    "is in `result`. heard: it called a meeting, and a meeting was held",
    )
    result: str = ""
    handled_by: str = Field(default="", description="The meeting that carried it out or heard it")
    handled_on: date | None = Field(default=None, description="The session whose close meeting handled it, or the day of the check-in")


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
