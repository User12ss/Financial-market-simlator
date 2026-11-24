"""What the chances given to a trade's endings, and to the events a seat proposed, turned out to be worth.

Once a trade's horizon has ended, the daily bars say which of its three
endings happened: the first day whose high reaches the target or whose low
reaches the stop settles it, and on a day that does both the stop is taken to
have come first, as in the simulation. The chances that were given are then
scored against it (a Brier score over the three endings: 0 is perfect, 2 the
worst). Each reading is scored on its own, so that the record can say whether
putting a seat's events into the simulation made the chances better or worse,
and beside both stands the plain forecast's.

A proposed event is scored once its days have passed, where code can tell
whether it happened: a close beyond a level, a one-day move, or a word in a
headline the ledger holds. An event whose check is in words only is never
scored, and one whose check needs a record that is not there is said to be
beyond judging, not counted as not having happened.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Callable

import pandas as pd

from data.market_calendar import trading_day_after, trading_days
from journal.store import Journal
from models.forecast import OUTCOMES, Backward
from models.planning import BackwardSet, EventScore, OutcomeScore, ProposedEvent

# As for the forecasts: a score is final once it has been worked out this many sessions after the end.
SETTLE_SESSIONS = 2
# The headlines about a name that belong to the days asked about and came in after the moment given (the
# cut-off of the meeting that proposed the event), and whether the ledger read those days without a gap.
Headlines = Callable[[str, date, date, datetime], "tuple[list[str], bool]"]


def word_in(word: str, text: str) -> bool:
    """Whether a word or phrase stands in a text as a whole word, whatever its case. "recall" is not in "recalls"."""
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None


def _window(frame: pd.DataFrame | None, first: date, last: date) -> pd.DataFrame | None:
    """The bars of every session from `first` to `last`, or None when one is missing or unusable."""
    days = trading_days(first, last)
    if frame is None or any(day not in frame.index for day in days):
        return None
    window = frame.loc[days, ["close", "low", "high"]].astype(float)
    usable = all(math.isfinite(value) and value > 0 for value in window.to_numpy().ravel())
    return window if usable else None


def ending(window: pd.DataFrame, close: float, stop: float, target: float) -> tuple[str, date]:
    """Which ending the bars show, and the session that settled it. The first row is the meeting's own session.

    Both levels are taken as a share of the close on the meeting's day, as the
    bars now show it: a split since then moves neither.
    """
    base = float(window["close"].iloc[0])
    for day, row in window.iloc[1:].iterrows():
        if row["low"] / base <= stop / close:
            return "bad", day
        if row["high"] / base >= target / close:
            return "good", day
    return "middle", window.index[-1]


def score_outcome(
    arm: str, session: date, table: Backward, basis: str, frame: pd.DataFrame | None, as_of: date,
) -> OutcomeScore:
    score = OutcomeScore(
        arm=arm, session_date=session, ticker=table.ticker, basis=basis, sessions=table.sessions,
        matures_on=table.matures_on, status="pending",
        chances={name: table.ways[name].share for name in OUTCOMES}, plain=table.plain, scored_on=as_of,
    )
    if table.matures_on > as_of:
        return score
    window = _window(frame, session, table.matures_on)
    if window is None:
        return score.model_copy(update={"status": "unpriced"})
    outcome, settled_on = ending(window, table.close, table.stop, table.target)

    def brier(chances: dict[str, float]) -> float:
        return sum((chances[name] - (1.0 if name == outcome else 0.0)) ** 2 for name in OUTCOMES)

    return score.model_copy(update={
        "status": "scored", "outcome": outcome, "settled_on": settled_on, "brier": brier(score.chances),
        "plain_brier": brier(table.plain) if table.plain else None,
    })


def score_event(
    arm: str, session: date, event: ProposedEvent, close: float, frame: pd.DataFrame | None,
    headlines: Headlines | None, as_of: date, asked_at: datetime | None = None,
) -> EventScore:
    """One proposed event against what happened. `asked_at` is the moment of the meeting that proposed it:
    a headline known by then is what the event was proposed from, and cannot be the proof that it happened."""
    score = EventScore(
        arm=arm, session_date=session, ticker=event.ticker, key=event.key, label=event.label,
        probability=event.probability, last_on=event.last_on, status="pending", scored_on=as_of,
    )
    if event.check_kind == "other":
        return score.model_copy(update={"status": "cannot_be_judged", "judged_by": "Its check is in words only."})
    if event.last_on > as_of:
        return score

    def judged(happened: bool, by: str) -> EventScore:
        return score.model_copy(update={
            "status": "scored", "happened": happened, "judged_by": by,
            "brier": (event.probability - (1.0 if happened else 0.0)) ** 2,
        })

    if event.check_kind == "headline":
        # Without a reading of those days that has no gap there is no verdict, either way. Not even when a
        # matching headline was seen: if the events seen to happen were scored and the ones nobody could rule
        # out were left waiting, the scored ones would be a sample tilted towards events that happened. Nor
        # is it final: it is tried again each time.
        titles, covered = (headlines(event.ticker, event.first_on, event.last_on, asked_at)
                           if headlines and asked_at else ([], False))
        found = next(((word, title) for word in event.check_keywords for title in titles if word_in(word, title)), None)
        if not covered:
            seen = f" (A headline with {found[0]!r} was seen: {found[1]})" if found else ""
            return score.model_copy(update={
                "status": "unread",
                "judged_by": f"No ledger at hand read {event.ticker}'s headlines through those days without a gap, "
                             f"so it is scored neither way.{seen}",
            })
        if found:
            return judged(True, f"A headline in those days has {found[0]!r}: {found[1]}")
        return judged(False, f"No headline of the {len(titles)} belonging to those days has any of: "
                             + ", ".join(repr(word) for word in event.check_keywords) + ".")

    # The price checks need the day before the event's first as well: a day's move is measured from it.
    window = _window(frame, session, event.last_on)
    if window is None:
        return score.model_copy(update={"status": "unpriced"})
    base = float(window["close"].iloc[0])
    closes = window["close"]
    days = [day for day in closes.index if event.first_on <= day <= event.last_on]
    if event.check_kind in ("close_below", "close_above"):
        level = event.check_level / close
        below = event.check_kind == "close_below"
        for day in days:
            if (closes[day] / base <= level) if below else (closes[day] / base >= level):
                return judged(True, f"The close of {day} was {'below' if below else 'above'} {event.check_level:,.2f}.")
        return judged(False, f"No close in those days was {'below' if below else 'above'} {event.check_level:,.2f}.")
    moves = closes.pct_change() * 100
    sign = 1 if event.direction == "up" else -1
    for day in days:
        if sign * moves[day] >= event.low_pct:
            return judged(True, f"On {day} it moved {moves[day]:+.1f}% in a day.")
    return judged(False, f"No day in those days moved {event.direction} by {event.low_pct:g}% or more.")


def asked_at(tables: BackwardSet) -> datetime:
    """The moment after which a headline is news to the seat that proposed a set's events: the cut-off of what it was shown."""
    return tables.asked_at or tables.created_at


def settled(score: OutcomeScore | EventScore) -> bool:
    ended = score.matures_on if isinstance(score, OutcomeScore) else score.last_on
    if score.status == "cannot_be_judged":
        return True
    return score.status == "scored" and score.scored_on is not None and score.scored_on >= trading_day_after(ended, SETTLE_SESSIONS)


def pending(tables: BackwardSet) -> tuple[list[OutcomeScore], list[EventScore]]:
    """A row for every ending and every event of a set, waiting for its days to pass."""
    closes = {table.ticker: table.close for table in tables.measured}
    day = tables.session_date
    outcomes = [score_outcome(tables.arm, day, table, basis, None, day)
                for basis, group in (("measured", tables.measured), ("rehearsed", tables.rehearsed)) for table in group]
    events = [score_event(tables.arm, day, event, closes[event.ticker], None, None, day, asked_at(tables))
              for event in tables.events if event.ticker in closes]
    return outcomes, events


def reconcile(
    journal: Journal, arm: str, bars: Callable[[str], pd.DataFrame | None], headlines: Headlines | None, as_of: date,
) -> tuple[list[OutcomeScore], list[EventScore], list[OutcomeScore | EventScore]]:
    """Score every ending and every proposed event an arm has on record, and keep the scores.

    Returns all the endings' scores, all the events', and those scored for
    the first time. A score that has settled is kept as it is; one that has
    not is worked out again, and stands only if what it rested on has gone
    missing since.
    """
    before_outcomes = {(s.session_date, s.ticker, s.basis): s for s in journal.outcome_scores(arm)}
    before_events = {(s.session_date, s.ticker, s.key): s for s in journal.event_scores(arm)}
    outcomes: list[OutcomeScore] = []
    events: list[EventScore] = []
    fresh: list[OutcomeScore | EventScore] = []
    frames: dict[str, pd.DataFrame | None] = {}

    def frame_for(ticker: str) -> pd.DataFrame | None:
        if ticker not in frames:
            frames[ticker] = bars(ticker)
        return frames[ticker]

    def keep(score, earlier, into: list) -> None:
        if earlier is not None and earlier.status == "scored" and score.status != "scored":
            score = earlier
        into.append(score)
        if score.status == "scored" and (earlier is None or earlier.status != "scored"):
            fresh.append(score)

    for tables in journal.backward_sets(arm):
        day = tables.session_date
        closes = {table.ticker: table.close for table in tables.measured}
        for basis, group in (("measured", tables.measured), ("rehearsed", tables.rehearsed)):
            for table in group:
                earlier = before_outcomes.get((day, table.ticker, basis))
                if earlier is not None and settled(earlier):
                    outcomes.append(earlier)
                    continue
                frame = frame_for(table.ticker) if table.matures_on <= as_of else None
                keep(score_outcome(arm, day, table, basis, frame, as_of), earlier, outcomes)
        for event in tables.events:
            earlier = before_events.get((day, event.ticker, event.key))
            if event.ticker not in closes:
                continue
            if earlier is not None and settled(earlier):
                events.append(earlier)
                continue
            priced = event.last_on <= as_of and event.check_kind not in ("headline", "other")
            keep(score_event(arm, day, event, closes[event.ticker], frame_for(event.ticker) if priced else None,
                             headlines, as_of, asked_at(tables)), earlier, events)
    journal.save_outcome_scores(outcomes)
    journal.save_event_scores(events)
    return outcomes, events, fresh


# ── The record in words ──────────────────────────────────────────────────────

def tally(outcomes: list[OutcomeScore], events: list[EventScore]) -> dict:
    """How the chances did, by reading, and how the proposed events did. Scored ones only."""
    result: dict = {"outcomes": {}, "events": {}}
    for basis in ("measured", "rehearsed"):
        scored = [score for score in outcomes if score.basis == basis and score.status == "scored"]
        if not scored:
            continue
        plain = [score.plain_brier for score in scored if score.plain_brier is not None]
        result["outcomes"][basis] = {
            "scored": len(scored), "sessions": len({score.session_date for score in scored}),
            "ended": {name: sum(1 for score in scored if score.outcome == name) for name in OUTCOMES},
            "mean_chance": {name: sum(score.chances[name] for score in scored) / len(scored) for name in OUTCOMES},
            "brier": sum(score.brier for score in scored) / len(scored),
            "plain_brier": sum(plain) / len(plain) if plain else None,
        }
    judged = [score for score in events if score.status == "scored"]
    result["events"] = {
        "proposed": len(events), "scored": len(judged), "happened": sum(1 for score in judged if score.happened),
        "cannot_be_judged": sum(1 for score in events if score.status == "cannot_be_judged"),
        "unread": sum(1 for score in events if score.status == "unread"),
        "pending": sum(1 for score in events if score.status in ("pending", "unpriced")),
        "mean_probability": sum(score.probability for score in judged) / len(judged) if judged else None,
        "brier": sum(score.brier for score in judged) / len(judged) if judged else None,
    }
    return result


def in_words(summary: dict) -> str:
    """The record as sentences, for the meeting and the report. Empty when nothing has been scored."""
    parts = []
    for basis, name in (("measured", "From the simulation alone"), ("rehearsed", "With the proposed events put in")):
        row = summary["outcomes"].get(basis)
        if not row:
            continue
        ended = ", ".join(f"{row['ended'][key]} {word}" for key, word in
                          (("good", "at the target"), ("bad", "at the stop"), ("middle", "at neither")))
        text = (f"{name}, {row['scored']} trade(s) worked back on {row['sessions']} day(s) have ended: {ended}. "
                f"Brier score {row['brier']:.3f}")
        if row["plain_brier"] is not None:
            text += f", the plain forecast's {row['plain_brier']:.3f}"
        parts.append(text + " (lower is better).")
    events = summary["events"]
    if events.get("scored"):
        parts.append(
            f"Of {events['scored']} proposed event(s) that could be judged, {events['happened']} happened; they were "
            f"given {events['mean_probability'] * 100:.0f}% on average. Brier score {events['brier']:.3f}."
        )
    if events.get("cannot_be_judged"):
        parts.append(f"{events['cannot_be_judged']} proposed event(s) cannot be judged by code and are not scored.")
    if events.get("unread"):
        parts.append(f"{events['unread']} wait for a reading of the name's headlines that covers their days "
                     "without a gap.")
    return " ".join(parts)
