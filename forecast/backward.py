"""Working back from how a trade ends: of the simulated paths that end each way, what did they go through?

A name with a stop and a target has three endings within its horizon:

    good      the target is reached before the stop
    bad       the stop is touched first (on a day that reaches both, the stop is taken to come first)
    middle    neither, by the time the horizon runs out

Every path ends exactly one way. For each ending this module says what the
paths that end that way have in common: how deep they dipped first, the day
that settled them, and how often each kind of event happened on the way. The
chance of an event given an ending is counted, not derived: of the paths that
end this way, the share on which it happened. Bayes' rule says the same number
comes out of the chance of the ending given the event, and a test holds the
two to each other.

An event counts when it happens on or before the day that settles the path. A
jump after the stop has sold is no part of the trade.

The ratio of an event's chance given the bad ending to its chance given the
good one says how much the event tells about where the trade is heading: at 1
it tells nothing, far from 1 it is the thing to watch.

All of it is true inside the simulation and no further: an event the
simulation does not contain cannot appear here.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np

import config
from forecast.engine import DAYS, Trace
from models.forecast import OUTCOMES, Backward, EventOdds, TickerForecast, Way

GOOD, BAD, MIDDLE = range(3)
_ENDS = {"good": "at the target", "bad": "at the stop", "middle": "at neither"}


def endings(trace: Trace, sessions: int) -> tuple[np.ndarray, np.ndarray]:
    """How each path ends within `sessions`, and the simulated day that settles it."""
    horizon = min(sessions, DAYS)
    good = (trace.reached < horizon) & (trace.reached < trace.stopped)
    bad = (trace.stopped < horizon) & (trace.stopped <= trace.reached)
    which = np.where(good, GOOD, np.where(bad, BAD, MIDDLE))
    day = np.where(good, trace.reached, np.where(bad, trace.stopped, horizon - 1))
    return which, day


def _by(day: np.ndarray, marks: np.ndarray) -> np.ndarray:
    """For each path: is any of its marked days on or before the day that settles it?"""
    return (marks & (np.arange(marks.shape[1])[None, :] <= day[:, None])).any(axis=1)


def happenings(
    trace: Trace, states: np.ndarray, market: np.ndarray, day: np.ndarray, *,
    state_names: list[str], state_now: list[float], labels: dict[str, str] | None = None,
) -> list[tuple[str, str, str, np.ndarray]]:
    """Every event looked for: its key, what it is in words, its kind, and on which paths it happened in time."""
    found: list[tuple[str, str, str, np.ndarray]] = []
    if trace.report_index is not None and trace.report_move is not None:
        moves = config.BACKWARD_REPORT_MOVES
        size = "one of that day's expected moves" if moves == 1 else f"{moves:g} of that day's expected moves"
        in_time = trace.report_index <= day
        found += [
            ("report_down", f"its earnings report is taken badly: it opens at least {size} lower", "calendar",
             in_time & (trace.report_move <= -moves)),
            ("report_up", f"its earnings report is taken well: it opens at least {size} higher", "calendar",
             in_time & (trace.report_move >= moves)),
        ]
    found += [
        ("jump_down", "a sharp fall of its own, unannounced", "statistical", _by(day, trace.jumps < 0)),
        ("jump_up", "a sharp rise of its own, unannounced", "statistical", _by(day, trace.jumps > 0)),
    ]
    if trace.fund_jumps is not None:
        found += [
            ("sector_down", "its sector falls sharply, beyond the market's move", "statistical", _by(day, trace.fund_jumps < 0)),
            ("sector_up", "its sector rises sharply, beyond the market's move", "statistical", _by(day, trace.fund_jumps > 0)),
        ]
    likeliest = int(np.argmax(state_now))
    for index, name in enumerate(state_names):
        if index != likeliest:
            found.append((f"market_{name}", f"the market spends at least a day in its {name} state", "statistical",
                          _by(day, states == index)))
    move = config.BACKWARD_MARKET_MOVE_PCT
    found += [
        ("market_down", f"the index falls {move:g}% or more below today's close", "statistical",
         _by(day, market <= math.log(1 - move / 100))),
        ("market_up", f"the index rises {move:g}% or more above today's close", "statistical",
         _by(day, market >= math.log(1 + move / 100))),
    ]
    for key, (fell_on, _) in trace.events.items():
        found.append((f"proposed:{key}", (labels or {}).get(key, key), "proposed", fell_on <= day))
    return found


def odds(key: str, label: str, kind: str, happened: np.ndarray, which: np.ndarray) -> EventOdds:
    """One event's row: its chance, its chance given each ending, and the ratio of the bad to the good."""
    given: dict[str, float | None] = {}
    counts: dict[str, tuple[int, int]] = {}
    for index, name in enumerate(OUTCOMES):
        ends = which == index
        counts[name] = (int(happened[ends].sum()), int(ends.sum()))
        given[name] = counts[name][0] / counts[name][1] if counts[name][1] >= config.BACKWARD_MIN_PATHS else None
    good, bad = given["good"], given["bad"]
    ratio, weight, note = None, None, ""
    if good is not None and bad is not None:
        if good > 0:
            ratio = bad / good
        elif bad > 0:
            note = "It happened on paths that end at the stop and on none that reach the target."
        (on_bad, all_bad), (on_good, all_good) = counts["bad"], counts["good"]
        weight = math.log((on_bad + 0.5) / (all_bad + 1)) - math.log((on_good + 0.5) / (all_good + 1))
    return EventOdds(key=key, label=label, kind=kind, chance=float(happened.mean()), given=given, ratio=ratio,
                     weight=weight, note=note)


def telling(event: EventOdds) -> float:
    """How much an event says about where the trade is heading, either way. An event that cannot be weighed says least."""
    return -1.0 if event.weight is None else abs(event.weight)


def plain_chances(vol: float, close: float, stop: float, target: float, sessions: int) -> dict[str, float]:
    """The three endings' chances from the plain forecast: no drift, no states, no jumps, the same volatility every day.

    The textbook series for a driftless diffusion between two barriers. It is
    what the simulation's chances have to beat once they are scored.
    """
    down, up = math.log(close / stop), math.log(target / close)
    width = down + up
    if not (vol > 0 and down > 0 and up > 0):
        return {}
    # The series is summed until its terms have died away: the narrower the spread against the band, the more it takes.
    terms = min(max(200, math.ceil(9 * width / (math.pi * vol * math.sqrt(sessions)))), 50_000)
    decay = [math.exp(-(n * math.pi * vol) ** 2 * sessions / (2 * width * width)) for n in range(1, terms)]

    def leaves(behind: float) -> float:
        """The chance of leaving within the horizon at the edge that lies `behind` away, not at the other."""
        x = width - behind
        return x / width + sum(
            2 * (-1) ** n / (n * math.pi) * math.sin(n * math.pi * x / width) * decay[n - 1] for n in range(1, terms)
        )

    good, bad = min(max(leaves(up), 0.0), 1.0), min(max(leaves(down), 0.0), 1.0)
    return {"good": good, "bad": bad, "middle": max(1.0 - good - bad, 0.0)}


def _marks(values: np.ndarray, percents: tuple[int, ...], digits: int = 2) -> dict[str, float]:
    return {str(percent): round(float(np.percentile(values, percent)), digits) for percent in percents}


def read(
    trace: Trace, states: np.ndarray, market: np.ndarray, forecast: TickerForecast, *, sessions: int,
    matures_on: date, state_names: list[str], state_now: list[float], labels: dict[str, str] | None = None,
) -> Backward:
    """One name's paths read backwards from their three endings. The name must have a stop."""
    if forecast.stop is None or forecast.target is None or trace.sold is None:
        raise ValueError(f"{trace.ticker} has no stop, so there is no bad ending to work back from.")
    paths = len(trace.stopped)
    horizon = min(sessions, DAYS)
    which, day = endings(trace, sessions)
    rows = np.arange(paths)
    steps = np.abs(np.diff(trace.path, axis=1, prepend=0.0))
    in_time = np.arange(DAYS)[None, :] <= day[:, None]
    whole = np.where(in_time, steps, 0.0).sum(axis=1)
    largest = np.where(in_time, steps, 0.0).max(axis=1)
    fall = (1.0 - np.exp(np.minimum(trace.lowest[rows, day], 0.0))) * 100

    ways: dict[str, Way] = {}
    notes: list[str] = []
    for index, name in enumerate(OUTCOMES):
        ends = which == index
        count = int(ends.sum())
        way = Way(share=count / paths, paths=count)
        if count >= config.BACKWARD_MIN_PATHS:
            if index != MIDDLE:
                way.settled = _marks(day[ends] + 1, (25, 50, 75), digits=1)
            if index == BAD:
                way.sold_below = _marks((1.0 - np.exp(trace.sold[ends])) * 100, (50, 95))
            else:
                way.adverse = _marks(fall[ends], (50, 75, 95))
            moved = whole[ends] > 0
            if moved.any():
                way.biggest_day = round(float(np.median(largest[ends][moved] / whole[ends][moved])), 4)
        elif count:
            notes.append(f"Only {count} of {paths:,} paths end {_ENDS[name]}: too few to say what led to it.")
        ways[name] = way

    bad = which == BAD
    events = [
        odds(key, label, kind, happened, which)
        for key, label, kind, happened in happenings(
            trace, states, market, day, state_names=state_names, state_now=state_now, labels=labels,
        )
        # A measured event that never happens in the simulation has nothing to say. A proposed one is kept: that it never happened is the finding.
        if kind == "proposed" or happened.any()
    ]
    events.sort(key=lambda event: (-telling(event), event.key))
    for key, reason in trace.left_out.items():
        notes.append(f"The proposed event {(labels or {}).get(key, key)!r} is not simulated: {reason}")
    for key, most in trace.capped.items():
        notes.append(f"The proposed event {(labels or {}).get(key, key)!r} is held to the largest past jump, "
                     f"{(math.exp(most) - 1) * 100:.1f}%: the range given for it reaches further.")
    return Backward(
        ticker=trace.ticker, sessions=horizon, matures_on=matures_on, close=forecast.close, stop=forecast.stop,
        target=forecast.target, target_from=forecast.target_from, proposed=bool(trace.events or trace.left_out),
        ways=ways, plain=plain_chances(forecast.naive_vol, forecast.close, forecast.stop, forecast.target, horizon),
        shaken_out=float((bad & (trace.path[:, horizon - 1] > 0)).mean()),
        target_after_stop=float((bad & (trace.reached < horizon)).mean()),
        events=events, notes=notes,
    )
