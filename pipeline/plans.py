"""The plan for each trade, as code holds it: what is kept of the manager's draft, the plan that stands in when
there is none, when a plan is looked at again, when one of its conditions holds, and what is done about it.

A plan says what the desk means to do with one name: why it owns it, where the stop is, by when the trade should
have worked, and a short list of "if this, then that". Three things are fixed in code and are not the manager's
to change:

    what may be watched for   only kinds of condition code can check (models.planning.ConditionKind)
    what may be done          a closed list of actions, none of which opens a position or adds to one
    what may act by itself    an action that takes risk off, set off by a price or a date. A headline is outside
                              text: it may call a meeting and nothing else, or a made-up story could sell the desk
                              out at the lows. The simulation's own chances call a meeting too, until the setting
                              lets them do more (PLAN_CHANCE_ACTS)

Nothing in a draft is repaired. A condition that fails a check is refused and said so; a draft that does not say,
in a form code can check, what would show it is wrong is refused whole, and the desk's own plan stands in.

A plan's time is fixed when it is first made. A later version cannot move it: the question "has this trade worked
by the day we gave it" would mean nothing if the day moved every evening.

Everything here is plain rules: no model is called and nothing is read or written. The desk's meetings
(pipeline.desk, pipeline.checkin) do that, and call these.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime

import config
from data.market_calendar import trading_day_after, trading_days
from forecast.outcomes import word_in
from models.forecast import Backward
from models.planning import (
    AUTOMATIC, AVERAGES, CHANCES, HARD, Condition, Contingency, ContingencyDraft, Plan, PlanDraft, Trigger,
)
from models.signals import TradeProposal
from pipeline.checks import figure_warning
from pipeline.scenario import usable_keywords

ENDS = {"good": "at the target", "bad": "at the stop", "middle": "at neither"}
# What shows a plan to be wrong has to end or cut the position: tightening a stop or calling a meeting does not.
WRONG_ACTIONS = ("exit", "trim")
# And it is something going against the trade, or its time: a price to fall to, an average to close below, a day.
# A price to rise to is not it. Given the chance, the local model called a rise of a seventh of a percent "wrong".
WRONG_KINDS = ("price_below", "close_below_average", "date")
NO_WRONG_IF = ("it does not say, as a price to fall to, an average to close below or a day, with exit or trim as its "
               "action, what would show that the plan is wrong.")
_CENT = 0.005       # a price is given to the cent: one that rounds onto a bound is on it
ONLY_THE_CLOCK = ("what it gives as showing the plan wrong is the time running out, which every plan has already: "
                  "it names nothing of its own.")
# Where an action of the plan's and one of the manager's meet, the one that takes more risk off stands.
_WEIGHT = {"hold": 0, "buy": 0, "trim": 1, "sell": 2}
_AS_PROPOSAL = {"exit": "sell", "trim": "trim"}


def acting_kinds() -> tuple[str, ...]:
    """The kinds of condition that may set off an action code carries out by itself."""
    return HARD + (CHANCES if config.PLAN_CHANCE_ACTS else ())


def acts(item: Contingency) -> bool:
    """Whether code carries this contingency out by itself.

    Asked every time something is to be done, and not taken from the
    record's word alone: the plan says so (`automatic`), its action is one
    of the four that take risk off, and its condition is of a kind that may
    set such an action off as the settings stand now. A record that marks a
    headline as acting by itself still only calls a meeting, however it came
    to say so, and turning PLAN_CHANCE_ACTS off stops every chance on record
    from selling at once.
    """
    return item.automatic and item.action in AUTOMATIC and item.condition.kind in acting_kinds()


def identity(item: Contingency) -> str:
    """What a contingency watches for and what it does, as one string. Its key and its wording are not part of it:
    the same condition with the same action is the same contingency in every version of a plan, and fires once."""
    said = json.dumps([item.condition.model_dump(mode="json"), item.action, item.new_stop], sort_keys=True)
    return hashlib.sha256(said.encode()).hexdigest()[:16]


@dataclass
class Facts:
    """What a plan's conditions are checked against at one moment.

    During the session only the price is known, and only as the watch saw
    it. Once the session has closed (`closed`), the day's bar, the averages
    and the simulation's chances are.
    """

    day: date
    closed: bool = False
    price: float | None = None          # the last price the watch saw today
    low: float | None = None            # the closed session's low
    high: float | None = None
    close: float | None = None
    averages: dict[int, float | None] = field(default_factory=dict)
    chances: dict[str, float] = field(default_factory=dict)
    titles: list[str] = field(default_factory=list)     # headlines first seen since the plan's version was made


# ── In words ─────────────────────────────────────────────────────────────────

def _price(value: float | None) -> str:
    return "none" if value is None else f"{value:,.2f}"


def describe(condition: Condition) -> str:
    kind = condition.kind
    if kind == "price_below":
        return f"the price at or below {condition.level:,.2f}"
    if kind == "price_above":
        return f"the price at or above {condition.level:,.2f}"
    if kind == "close_below_average":
        return f"a close below the {condition.average}-day average"
    if kind == "date":
        return f"the close of {condition.on}"
    if kind in CHANCES:
        side = "below" if kind == "chance_below" else "above"
        return f"the chance of ending {ENDS[condition.ending]} {side} {condition.level * 100:.0f}%"
    return "a headline with " + " or ".join(repr(word) for word in condition.keywords)


def what_then(item: Contingency) -> str:
    action = {"exit": "sell the position", "trim": "sell half", "withdraw": "take back the buy",
              "call_meeting": "call a meeting"}.get(item.action)
    return action or f"raise the stop to {item.new_stop:,.2f}"


def render_contingency(item: Contingency) -> str:
    how = "by itself" if acts(item) else "at a meeting"
    return f"[{item.key}] On {describe(item.condition)}: {what_then(item)}, {how}. {item.reason}"


def trigger_line(trigger: Trigger) -> str:
    """A condition that has held, as it is said at a meeting: what it was, what it does, and what was seen."""
    item = trigger.contingency
    return (f"[{item.key}] On {describe(item.condition)}: {what_then(item)}, "
            f"{'by itself' if acts(item) else 'at a meeting'}. {trigger.evidence}")


def calling(triggers: list[Trigger], standing: dict[str, Plan], as_of: datetime) -> list[Trigger]:
    """The conditions that called a meeting by this moment and that no meeting has heard yet.

    Only those of a plan that still stands: a condition of a plan whose
    trade is over calls no one.
    """
    return [
        trigger for trigger in triggers
        if trigger.status == "fired" and not acts(trigger.contingency) and trigger.fired_at <= as_of
        and trigger.ticker in standing and standing[trigger.ticker].opened == trigger.opened
    ]


def as_before(
    every: list[Plan], triggers: list[Trigger], session: date, runs: set[str],
) -> tuple[dict[str, Plan], list[Trigger]]:
    """The plan that stood for each name, and every trigger, as they were before any close meeting of `session`.

    Usually that is simply what is on record. It differs when the session's
    close meeting is being held again: what the earlier one left about plans
    is set aside, exactly as Journal.forget_plans_of takes it back once the
    new meeting finishes. `runs` are the ids of the session's close
    meetings. Nothing is written here: a meeting that fails leaves the
    record as it found it.
    """
    standing: dict[str, Plan] = {}
    for plan in every:                               # by name, then by the plan's first session, then by version
        if plan.session_date == session:
            continue
        if plan.closed_on == session:
            plan = plan.model_copy(update={"closed_on": None, "closed_why": ""})
        if plan.closed_on is None:
            standing[plan.ticker] = plan
    kept = []
    for trigger in triggers:
        if trigger.found_by == "close" and trigger.session_date == session:
            continue
        if trigger.status != "fired" and trigger.handled_by in runs:
            trigger = trigger.model_copy(update={"status": "fired", "result": "", "handled_by": "", "handled_on": None})
        kept.append(trigger)
    return standing, kept


def render_plan(plan: Plan) -> str:
    """A plan as a seat is shown it."""
    lines = [
        f"{plan.ticker}, version {plan.version}, made on {plan.session_date}"
        + (" by the desk's own rule" if plan.source == "default" else ""),
        f"- Thesis: {plan.thesis}",
        f"- Stop {_price(plan.stop)}; time runs out at the close of {plan.ends_on}.",
    ]
    lines += [f"- {render_contingency(item)}" for item in plan.contingencies]
    return "\n".join(lines)


# ── The checks ───────────────────────────────────────────────────────────────

def usual_range(atr_pct: float | None) -> float:
    """One usual daily range, in percent of the price: what the desk counts as a move worth a look.

    The same measure a check-in uses for "trading beyond its usual range":
    the 14-day ATR, or a flat percentage where the range is unknown. A price
    nearer the close than this is reached on an ordinary day.
    """
    return config.DESK_MOVE_ATR * atr_pct if atr_pct else config.DESK_MOVE_PCT


def price_window(close: float, usual: float) -> tuple[float, float]:
    """The nearest prices a plan may name: the highest a fall may be set at, and the lowest a rise, to the cent."""
    return math.floor(close * (1 - usual / 100) * 100) / 100, math.ceil(close * (1 + usual / 100) * 100) / 100


def _why_not(
    item: ContingencyDraft, *, held: bool, close: float, stop: float | None, left: int, usual: float,
    averages: dict[int, float | None], chances: dict[str, float], keywords: list[str],
) -> str:
    """Why a drafted condition cannot be kept, or nothing.

    `left` is how many sessions the plan still has, `usual` one usual daily
    range in percent. A price nearer the last close than that is refused:
    the first plans the local model wrote sold a position when it rose a
    seventh of a percent, and called that being wrong.
    """
    kind, level, whole = item.condition, item.value, _whole(item.value)
    below, above = price_window(close, usual)
    if kind in ("price_below", "price_above"):
        if not (math.isfinite(level) and level > 0):
            return "its price condition names no price."
        if kind == "price_below" and not level < close:
            return f"a price at or below {level:,.2f} holds already: the last close is {close:,.2f}."
        if kind == "price_above" and not level > close:
            return f"a price at or above {level:,.2f} holds already: the last close is {close:,.2f}."
        if kind == "price_below" and stop is not None and level <= stop:
            return f"the stop at {stop:,.2f} sells before the price reaches {level:,.2f}."
        if (level > below + _CENT) if kind == "price_below" else (level < above - _CENT):
            bound = f"at or below {below:,.2f}" if kind == "price_below" else f"at or above {above:,.2f}"
            return (f"{level:,.2f} is {abs(level / close - 1) * 100:.2f}% from the last close of {close:,.2f}, inside "
                    f"one usual daily range ({usual:.2f}%): an ordinary day reaches it. It may be {bound}.")
    elif kind == "close_below_average":
        if whole not in AVERAGES:
            return f"a plan may name the 20-, 50- or 200-day average, not the {level:g}-day one."
        value = averages.get(whole)
        if value is None:
            return f"the data has no {whole}-day average for it."
        if close < value:
            return (f"a close below the {whole}-day average holds already: the last close is {close:,.2f}, "
                    f"the average {value:,.2f}.")
        # An average is held to what a price is held to, where it stands today. A stronger local model's first
        # plans were out on a close below an average half a usual day's range under the last close, which an
        # ordinary down day gets to, or on one far under the stop, which passed and could never come to anything.
        # An average has more places than a price: one that rounds onto the stop is at the stop. So a name whose
        # stop is as near the close as a price may be has no average that passes, which is what its manager is told.
        if stop is not None and value <= stop + _CENT:
            return (f"the stop at {stop:,.2f} sells before a close gets under the {whole}-day average, which "
                    f"stands at {value:,.2f}.")
        if value > below + _CENT:
            return (f"the {whole}-day average stands at {value:,.2f}, {(1 - value / close) * 100:.2f}% from the last "
                    f"close of {close:,.2f}, inside one usual daily range ({usual:.2f}%): an ordinary day closes "
                    f"under it. It may stand at {below:,.2f} at most.")
    elif kind == "date":
        if whole is None or not 1 <= whole <= left:
            return (f"its day ({level:g}) is not one of the {left} session(s) the plan has left."
                    if left else "the plan has no session left to name a day in.")
    elif kind in CHANCES:
        if item.ending is None:
            return "its chance condition names no ending."
        if not 0 < level < 1:
            return f"its chance ({level:g}) is not above 0 and below 1."
        if not chances:
            return "the simulation gave this trade no chances to set it against."
        now = chances[item.ending]
        if (now < level) if kind == "chance_below" else (now > level):
            side = "below" if kind == "chance_below" else "above"
            return (f"a chance of ending {ENDS[item.ending]} {side} {level * 100:.0f}% holds already: "
                    f"it is {now * 100:.0f}%.")
        if item.action in AUTOMATIC and kind not in acting_kinds():
            return ("one of the simulation's chances may call a meeting and nothing else: its forecasts have not "
                    "been reconciled long enough to sell on (PLAN_CHANCE_ACTS).")
    elif kind == "headline":
        if not keywords:
            return ("a headline condition needs a word to look for that is not the company's name, not a common "
                    "word, and not in a headline already known.")
        if item.action in AUTOMATIC:
            return "a headline is outside text: it may call a meeting and nothing else."
    if item.action == "withdraw" and held:
        return "there is no buy waiting to take back: the position is held."
    if item.action == "tighten_stop":
        if stop is None:
            return "there is no stop to tighten."
        if item.new_stop is None or not item.new_stop > stop:
            return f"a tightened stop has to lie above the present one ({stop:,.2f})."
        # Raised when the price gets to a level, the stop is set against that level; raised on anything else,
        # against the last close. Nearer than a usual day's range, it is not a stop but a sale put off by a day.
        base, where = (level, f"the price that raises it, {level:,.2f}") if kind == "price_above" \
            else (close, f"the last close, {close:,.2f}")
        ceiling = math.floor(base * (1 - usual / 100) * 100) / 100
        if item.new_stop > ceiling + _CENT:
            return (f"a stop at {item.new_stop:,.2f} is inside one usual daily range ({usual:.2f}%) of {where}: an "
                    f"ordinary day would sell there. It may be {ceiling:,.2f} at most.")
    if not item.reason.strip():
        return "it gives no reason."
    return ""


def _whole(value: float) -> int | None:
    """A drafted number as a count (of days, of sessions), or None when it is not a whole one."""
    return int(value) if math.isfinite(value) and float(value).is_integer() else None


def _kept(item: ContingencyDraft, key: str, session: date, keywords: list[str]) -> Contingency:
    kind = item.condition
    condition = Condition(
        kind=kind,
        level=item.value if kind in ("price_below", "price_above") + CHANCES else None,
        average=_whole(item.value) if kind == "close_below_average" else None,
        on=trading_day_after(session, _whole(item.value)) if kind == "date" else None,
        ending=item.ending if kind in CHANCES else None,
        keywords=keywords if kind == "headline" else [],
    )
    return Contingency(
        key=key, condition=condition, action=item.action,
        new_stop=round(item.new_stop, 2) if item.action == "tighten_stop" else None,
        reason=" ".join(item.reason.split()), source="manager",
        automatic=item.action in AUTOMATIC and kind in acting_kinds(),
    )


TIME_STOPS = ("exit_unless_ahead", "exit", "call_meeting", "off")


def time_stop(ends_on: date) -> Contingency | None:
    """What every plan has: when the time it was given runs out, the position goes (or the desk meets, as configured)."""
    mode = config.PLAN_TIME_STOP
    if mode not in TIME_STOPS:
        raise ValueError(f"PLAN_TIME_STOP must be exit_unless_ahead, exit, call_meeting or off, not {mode!r}.")
    if mode == "off":
        return None
    action = "call_meeting" if mode == "call_meeting" else "exit"
    return Contingency(
        key="time", condition=Condition(kind="date", on=ends_on), action=action, source="rule",
        automatic=action in AUTOMATIC,
        reason=("The time the trade was given has run out. A position that is not ahead of its cost by then has not "
                "worked and is sold; one that is ahead is put before the meeting."
                if mode == "exit_unless_ahead" else
                "The time the trade was given has run out without it reaching its stop."),
    )


def sells_on_a_day(item: Contingency) -> bool:
    """Whether a condition is a sale that the mere arrival of a day sets off: a time stop, whoever wrote it."""
    return item.condition.kind == "date" and item.action in ("exit", "trim")


def unless_ahead(item: Contingency) -> bool:
    """Whether a condition sells only a trade that is not ahead of its cost, by the setting as it stands."""
    return config.PLAN_TIME_STOP == "exit_unless_ahead" and sells_on_a_day(item)


def spared_by_the_clock(trigger: Trigger, unrealized_pct: float | None) -> str:
    """Why a day that has come does not sell this position, or nothing.

    A time stop is for a trade that has not worked. With the shipped setting
    (PLAN_TIME_STOP=exit_unless_ahead) a position that is ahead of its cost
    when its time runs out is not sold by the clock: it is put before the
    meeting, and the plan is looked at again. A day the manager names for a
    sale, or for half of one, is held to the same. Its reason for such a day
    was "if it is not profitable by then", and code that sold on the day
    whatever the position had made did not do what the plan said. Judged
    when the condition is acted on, by the setting as it stands then. A
    position with no usable price is not known to be ahead, and is sold.
    """
    item = trigger.contingency
    if not unless_ahead(item) or unrealized_pct is None or not unrealized_pct > 0:
        return ""
    if item.source == "rule":
        return (f"Its time has run out with the position {unrealized_pct:+.2f}% against its cost. The clock sells "
                "only a trade that is not ahead, so this one is not sold by it and is put before the meeting.")
    return (f"The day its plan named has come with the position {unrealized_pct:+.2f}% against its cost. A day sells "
            "only a trade that is not ahead, so this one is not sold on it and is put before the meeting.")


def sessions_left(session: date, ends_on: date) -> list[date]:
    """The sessions after `session` up to `ends_on`: what a plan looked at today still has."""
    return [day for day in trading_days(session, ends_on) if day > session] if ends_on > session else []


def assemble(
    *, arm: str, ticker: str, run_id: str, session: date, now: datetime, held: bool, direction: str, thesis: str,
    close: float, stop: float | None, stop_from: str, table: Backward | None, horizon: int,
    averages: dict[int, float | None], before: Plan | None, draft: PlanDraft | None, reasons: list[str],
    shown: list[str], name: str = "", titles: list[str] | None = None, atr_pct: float | None = None,
) -> Plan:
    """The plan to keep for a name at this meeting: the manager's where its draft passes, the desk's own where not.

    `before` is the version that stands. `reasons` is why the plan is being
    looked at again, as code found them. `horizon` counts for a first plan
    only: a later version keeps the day its first one was given. `name` and
    `titles` are the company's name and the headlines already known: a word
    any of them has cannot tell a later headline from these. `atr_pct` is
    the name's usual daily range (ATR 14) in percent of its price: a price
    in a condition has to lie at least that far from the last close.
    """
    opened = before.opened if before else session
    sessions = before.horizon if before else horizon
    ends_on = before.ends_on if before else trading_day_after(session, horizon)
    chances = {ending: table.ways[ending].share for ending in ENDS} if table else {}
    left = len(sessions_left(session, ends_on))
    clock = time_stop(ends_on)
    kept: list[Contingency] = [clock] if clock else []
    refused: list[str] = []
    source, said, warnings, basis = "default", thesis, [], ""

    if draft is not None:
        def words(item: ContingencyDraft) -> list[str]:
            return usable_keywords(item.keywords, ticker, name, titles or []) if item.condition == "headline" else []

        facts = dict(held=held, close=close, stop=stop, left=left, averages=averages, chances=chances,
                     usual=usual_range(atr_pct))
        wrong = draft.wrong_if
        reason = _why_not(wrong, keywords=words(wrong), **facts)
        if not reason and not (wrong.condition in WRONG_KINDS and wrong.action in WRONG_ACTIONS):
            reason = NO_WRONG_IF
        if not reason and clock is not None and identity(_kept(wrong, "wrong", session, [])) == identity(clock):
            reason = ONLY_THE_CLOCK
        if reason:
            refused.append("The plan is refused whole: " + (
                reason if reason in (NO_WRONG_IF, ONLY_THE_CLOCK)
                else f"what would show it wrong cannot be kept, {reason}"))
        else:
            source = "manager"
            kept.append(_kept(wrong, "wrong", session, []))
            own = 0
            for number, item in enumerate(draft.contingencies, start=1):
                reason = _why_not(item, keywords=words(item), **facts)
                candidate = None if reason else _kept(item, str(own + 1), session, words(item))
                if candidate is not None and any(identity(candidate) == identity(other) for other in kept):
                    reason = "the plan has it already."
                if not reason and own >= config.PLAN_MAX_CONDITIONS:
                    reason = (f"a plan may have {config.PLAN_MAX_CONDITIONS} conditions of the manager's beyond the "
                              "one that shows it wrong, and it has them.")
                if reason:
                    refused.append(f"Condition {number} ({item.condition}, {item.action}): {reason}")
                else:
                    kept.append(candidate)
                    own += 1
            if draft.thesis.strip():
                said = " ".join(draft.thesis.split())
            basis = " ".join(draft.change.split())
            warnings = figure_warning(
                "\n".join([draft.thesis, draft.change] + [item.reason for item in [wrong] + draft.contingencies]), shown)

    plan = Plan(
        arm=arm, ticker=ticker, opened=opened, version=(before.version + 1) if before else 1, run_id=run_id,
        session_date=session, created_at=now, source=source, held=held, thesis=said, direction=direction,
        horizon=sessions, ends_on=ends_on, close=close, stop=stop, stop_from=stop_from,
        target=table.target if table else None, chances=chances, contingencies=kept,
        basis=" ".join(part for part in (" ".join(reasons), f"The manager: {basis}" if basis else "") if part),
        refused=refused, data_warnings=warnings,
    )
    plan.changes = changes(before, plan)
    return plan


def changes(before: Plan | None, after: Plan) -> list[str]:
    """What differs between a plan and the version before it, in sentences. Worked out by code, not taken from anyone's word."""
    if before is None:
        return []
    differs = []
    if before.source != after.source:
        differs.append("It is now the manager's own plan." if after.source == "manager"
                       else "The manager's plan no longer stands: the desk's own is in its place.")
    if before.thesis != after.thesis:
        differs.append("The thesis was rewritten.")
    if before.stop != after.stop:
        differs.append(f"The stop moved from {_price(before.stop)} to {_price(after.stop)}.")
    old = {identity(item): item for item in before.contingencies}
    new = {identity(item): item for item in after.contingencies}
    differs += [f"Dropped: {render_contingency(old[name])}"
                for name in sorted(set(old) - set(new), key=lambda name: old[name].key)]
    differs += [f"Added: {render_contingency(new[name])}"
                for name in sorted(set(new) - set(old), key=lambda name: new[name].key)]
    return differs


# ── When a plan is looked at again ───────────────────────────────────────────

def reasons_to_revise(before: Plan | None, *, direction: str, stop: float | None, fired: list[Trigger]) -> list[str]:
    """Why a name's plan is put in front of the manager at this meeting. Empty: the plan stands as it is.

    A plan is not rewritten every evening. It is looked at again when the
    analyst's view of the name has turned, when the stop on the books has
    moved, or when one of its own conditions has held.
    """
    if before is None:
        return ["There is no plan for it yet."]
    reasons = []
    if direction and direction != before.direction:
        reasons.append(f"The analyst's view turned from {before.direction or 'none'} to {direction}.")
    if stop != before.stop:
        reasons.append(f"The stop on the books moved from {_price(before.stop)} to {_price(stop)}.")
    reasons += [f"Its condition [{trigger.contingency.key}] held on {trigger.session_date}: {trigger.evidence}"
                for trigger in fired]
    return reasons


# ── When a condition holds ───────────────────────────────────────────────────

def holds(condition: Condition, facts: Facts) -> str:
    """What was seen that makes a condition hold, in words. Empty when it does not hold, or cannot be told yet."""
    kind, level = condition.kind, condition.level
    if kind == "price_below":
        if facts.closed and facts.low is not None and facts.low <= level:
            return f"The low of {facts.day} was {facts.low:,.2f}."
        if not facts.closed and facts.price is not None and facts.price <= level:
            return f"It traded at {facts.price:,.2f}."
    elif kind == "price_above":
        if facts.closed and facts.high is not None and facts.high >= level:
            return f"The high of {facts.day} was {facts.high:,.2f}."
        if not facts.closed and facts.price is not None and facts.price >= level:
            return f"It traded at {facts.price:,.2f}."
    elif kind == "close_below_average":
        average = facts.averages.get(condition.average)
        if facts.closed and facts.close is not None and average is not None and facts.close < average:
            return f"It closed at {facts.close:,.2f}, below its {condition.average}-day average of {average:,.2f}."
    elif kind == "date":
        if facts.closed and facts.day >= condition.on:
            return f"The session of {facts.day} has closed."
    elif kind in CHANCES:
        now = facts.chances.get(condition.ending)
        if facts.closed and now is not None and ((now < level) if kind == "chance_below" else (now > level)):
            return f"The chance of ending {ENDS[condition.ending]} is {now * 100:.0f}%."
    elif kind == "headline":
        for word in condition.keywords:
            for title in facts.titles:
                if word_in(word, title):
                    return f"A headline has {word!r}: {title}"
    return ""


def found(plan: Plan, facts: Facts, already: set[str], *, at: datetime, by: str) -> list[Trigger]:
    """The plan's conditions that hold now and have not been found before, as triggers still to be acted on.

    `already` holds the identities of the plan's contingencies that fired
    at any time, in any of its versions: each fires once.
    """
    triggers, seen = [], set(already)
    for item in plan.contingencies:
        name = identity(item)
        if name in seen:
            continue
        evidence = holds(item.condition, facts)
        if evidence:
            seen.add(name)
            triggers.append(Trigger(
                arm=plan.arm, ticker=plan.ticker, opened=plan.opened, version=plan.version, identity=name,
                contingency=item, fired_at=at, session_date=facts.day, found_by=by, evidence=evidence,
            ))
    return triggers


# ── What is done about it ────────────────────────────────────────────────────

def wanted(triggers: list[Trigger]) -> dict[str, Trigger]:
    """For each name, the trigger whose sale the plan asks for: an exit before a trim. Only what code may do by itself."""
    chosen: dict[str, Trigger] = {}
    for trigger in triggers:
        item = trigger.contingency
        if not acts(item) or item.action not in _AS_PROPOSAL:
            continue
        held = chosen.get(trigger.ticker)
        if held is None or (item.action == "exit" and held.contingency.action != "exit"):
            chosen[trigger.ticker] = trigger
    return chosen


def override(
    proposals: list[TradeProposal], triggers: list[Trigger], holdings: set[str],
) -> tuple[list[TradeProposal], list[tuple[str, str]]]:
    """The meeting's proposals with the plans' own sales put in, and a sentence for each name it touched.

    Where a plan's condition has held and its action is to sell or to sell
    half, that stands whatever the manager said of the name today, unless
    the manager's own accepted decision already takes at least as much risk
    off. A plan's action never adds: the only two it can put here are sell
    and trim, and only for a name that is held.
    """
    result, said = list(proposals), []
    for ticker, trigger in sorted(wanted(triggers).items()):
        if ticker not in holdings:
            continue
        action = _AS_PROPOSAL[trigger.contingency.action]
        why = f"Its plan's condition [{trigger.contingency.key}] held: {trigger.evidence}"
        index = next((i for i, proposal in enumerate(result)
                      if proposal.ticker == ticker and proposal.status == "accepted"), None)
        standing = result[index] if index is not None else None
        if standing is not None and _WEIGHT[standing.action] >= _WEIGHT[action]:
            said.append((ticker, f"{ticker}: {why} The plan asks to {action}, and the {standing.action} already "
                                 "accepted at this meeting does as much."))
            continue
        put = TradeProposal(
            ticker=ticker, action=action, confidence=0.0, reason=why, source="plan", status="accepted",
            note=(f"Set by the plan. The {standing.action} given at this meeting is set aside."
                  if standing is not None else "Set by the plan."),
        )
        if standing is not None:
            result[index] = standing.model_copy(update={
                "status": "rejected", "note": f"Set aside: the plan's own condition held, and it asks to {action}."})
        result.append(put)
        said.append((ticker, f"{ticker} is {'sold' if action == 'sell' else 'trimmed'} by its plan. {why}"
                             + (f" The {standing.action} given at this meeting is set aside." if standing else "")))
    return result, said


def report_too_near(session: date, report: date | None) -> str:
    """Why no position may be opened in a name today because of its earnings report, or nothing."""
    days = config.PLAN_NO_ENTRY_BEFORE_REPORT
    if days <= 0 or report is None or report <= session:
        return ""
    away = len(sessions_left(session, report))
    if away > days:
        return ""
    return (f"Its earnings report is due on {report}, {away} session(s) away: the desk opens no position within "
            f"{days} session(s) of a report (PLAN_NO_ENTRY_BEFORE_REPORT).")


def tightened(trigger: Trigger, stop: float | None, close: float | None) -> tuple[float | None, str]:
    """The stop a tighten_stop trigger raises the position's to, or None and why it cannot be raised now."""
    new = trigger.contingency.new_stop
    if stop is None:
        return None, "the position has no stop on the books to raise."
    if close is None:
        return None, "there is no close today to set the new stop against."
    if new is None or not new > stop:
        return None, f"the stop on the books, {_price(stop)}, is already at or above {_price(new)}."
    if not new < close:
        return None, (f"the close of {close:,.2f} is at or below the new stop of {new:,.2f}: raised there it would "
                      "sell at the next open, which is not what tightening a stop is for.")
    return new, ""
