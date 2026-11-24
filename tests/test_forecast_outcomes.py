"""What the chances given to a trade's endings, and to proposed events, turned out to be worth."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from data.market_calendar import trading_day_after, trading_days
from forecast.backward import plain_chances
from forecast.outcomes import (
    SETTLE_SESSIONS, ending, in_words, pending, reconcile, score_event, score_outcome, settled, tally,
)
from journal.store import Journal
from models.forecast import Backward, Way
from models.planning import BackwardSet, ProposedEvent
from tests.test_forecast_engine import leaves_at

ARM = "desk/fake/a"
MONDAY = date(2026, 11, 23)
ASKED = datetime(2026, 11, 23, 23, 30, tzinfo=timezone.utc)     # the meeting that proposed the events
MATURES = trading_day_after(MONDAY, 5)              # 1 December: the 26th is a holiday


def frame(closes: dict[date, float], lows: dict[date, float] | None = None, highs: dict[date, float] | None = None,
          first: date = MONDAY, last: date = date(2026, 12, 11), base: float = 100.0) -> pd.DataFrame:
    """Daily bars that sit at `base` unless a day is given another close, low or high."""
    days = trading_days(first, last)
    close = pd.Series({day: closes.get(day, base) for day in days})
    low = pd.Series({day: (lows or {}).get(day, min(close[day], base) - 0.5) for day in days})
    high = pd.Series({day: (highs or {}).get(day, max(close[day], base) + 0.5) for day in days})
    return pd.DataFrame({"close": close, "adj_close": close, "low": low, "high": high})


def table(ticker="ACME", good=0.3, bad=0.25, sessions=5, stop=95.0, target=110.0, close=100.0, proposed=False,
          plain=None) -> Backward:
    ways = {"good": Way(share=good, paths=3000), "bad": Way(share=bad, paths=2500),
            "middle": Way(share=1 - good - bad, paths=4500)}
    return Backward(
        ticker=ticker, sessions=sessions, matures_on=trading_day_after(MONDAY, sessions), close=close, stop=stop,
        target=target, ways=ways, shaken_out=0.0, target_after_stop=0.0, proposed=proposed,
        plain={"good": 0.2, "bad": 0.3, "middle": 0.5} if plain is None else plain,
    )


def day(offset: int) -> date:
    return trading_day_after(MONDAY, offset)


# ── Which ending happened ────────────────────────────────────────────────────

def test_the_first_day_whose_low_reaches_the_stop_or_whose_high_reaches_the_target_settles_it():
    assert ending(frame({}, highs={day(3): 110.0}), 100.0, 95.0, 110.0) == ("good", day(3))
    assert ending(frame({}, lows={day(2): 95.0}), 100.0, 95.0, 110.0) == ("bad", day(2))
    # The stop first, the target later: the trade was over by then.
    assert ending(frame({}, lows={day(2): 94.0}, highs={day(3): 111.0}), 100.0, 95.0, 110.0) == ("bad", day(2))
    assert ending(frame({}, lows={day(4): 94.0}, highs={day(3): 111.0}), 100.0, 95.0, 110.0) == ("good", day(3))
    # A hair short of either is neither, settled on the window's last day.
    window = frame({}, lows={day(2): 95.01}, highs={day(3): 109.99}).loc[trading_days(MONDAY, MATURES)]
    assert ending(window, 100.0, 95.0, 110.0) == ("middle", MATURES)


def test_on_a_day_that_reaches_both_the_stop_is_taken_to_have_come_first_as_in_the_simulation():
    assert ending(frame({}, lows={day(2): 94.0}, highs={day(2): 111.0}), 100.0, 95.0, 110.0) == ("bad", day(2))


def test_the_meetings_own_day_does_not_count_and_a_split_since_then_moves_neither_level():
    # Monday's own bar reached the target before the meeting was held: that is not on the way.
    assert ending(frame({}, highs={MONDAY: 120.0}).loc[trading_days(MONDAY, MATURES)], 100.0, 95.0, 110.0)[0] == "middle"
    # After a 2-for-1 split every bar the source now shows is halved, the meeting's day included.
    halved = frame({}, base=50.0, lows={day(2): 47.5}).loc[trading_days(MONDAY, MATURES)]
    assert ending(halved, 100.0, 95.0, 110.0) == ("bad", day(2))
    assert ending(frame({}, base=50.0, lows={day(2): 47.6}).loc[trading_days(MONDAY, MATURES)], 100.0, 95.0, 110.0)[0] == "middle"


# ── Scoring the chances ──────────────────────────────────────────────────────

def test_an_ending_is_scored_only_once_its_horizon_has_ended():
    bars = frame({}, lows={day(2): 90.0})
    waiting = score_outcome(ARM, MONDAY, table(), "measured", bars, as_of=day(4))
    assert waiting.status == "pending" and waiting.outcome is None and waiting.brier is None
    assert waiting.chances == {"good": 0.3, "bad": 0.25, "middle": pytest.approx(0.45)}
    assert (waiting.sessions, waiting.matures_on, waiting.basis, waiting.scored_on) == (5, MATURES, "measured", day(4))
    scored = score_outcome(ARM, MONDAY, table(), "measured", bars, as_of=MATURES)
    assert (scored.status, scored.outcome, scored.settled_on) == ("scored", "bad", day(2))
    # (0.3 - 0)^2 + (0.25 - 1)^2 + (0.45 - 0)^2
    assert scored.brier == pytest.approx(0.09 + 0.5625 + 0.2025)
    assert scored.plain_brier == pytest.approx(0.04 + 0.49 + 0.25)


def test_the_best_and_the_worst_a_score_can_be():
    bars = frame({}, highs={day(1): 111.0})
    sure = score_outcome(ARM, MONDAY, table(good=1.0, bad=0.0), "measured", bars, MATURES)
    wrong = score_outcome(ARM, MONDAY, table(good=0.0, bad=1.0), "measured", bars, MATURES)
    assert sure.brier == pytest.approx(0.0) and wrong.brier == pytest.approx(2.0)
    assert score_outcome(ARM, MONDAY, table(plain={}), "rehearsed", bars, MATURES).plain_brier is None


def test_missing_or_unusable_bars_leave_it_unpriced_not_guessed():
    for bars in (None, frame({}, last=day(4)), frame({}).drop(index=day(2)), frame({}, lows={day(3): float("nan")}),
                 frame({}, lows={day(3): 0.0})):
        score = score_outcome(ARM, MONDAY, table(), "measured", bars, MATURES)
        assert score.status == "unpriced" and score.outcome is None and score.brier is None


def test_the_plain_forecasts_chances_are_the_textbook_series_and_add_up():
    chances = plain_chances(0.012, 100.0, 95.0, 110.0, 10)
    down, up = math.log(100 / 95), math.log(110 / 100)
    assert chances["good"] == pytest.approx(leaves_at(up, down + up, 0.012, 10), abs=1e-9)
    assert chances["bad"] == pytest.approx(leaves_at(down, down + up, 0.012, 10), abs=1e-9)
    assert sum(chances.values()) == pytest.approx(1.0) and chances["bad"] > chances["good"] > 0
    # Given long enough, one of the two is reached, in proportion to how near it is.
    forever = plain_chances(0.012, 100.0, 95.0, 110.0, 5000)
    assert forever["middle"] == pytest.approx(0.0, abs=1e-6) and forever["good"] == pytest.approx(down / (down + up), abs=1e-6)
    # In no time at all, neither.
    assert plain_chances(0.0001, 100.0, 95.0, 110.0, 1) == pytest.approx({"good": 0.0, "bad": 0.0, "middle": 1.0}, abs=1e-9)
    # Levels on the wrong side of the close, or no volatility, give no chances at all.
    assert plain_chances(0.0, 100.0, 95.0, 110.0, 10) == {}
    assert plain_chances(0.01, 100.0, 101.0, 110.0, 10) == {} and plain_chances(0.01, 100.0, 95.0, 99.0, 10) == {}


# ── Scoring proposed events ──────────────────────────────────────────────────

def proposed(check="close_below", level=92.0, keywords=(), first=2, last=4, probability=0.2, direction="down",
             low=4.0, high=8.0, key="1", ticker="ACME") -> ProposedEvent:
    return ProposedEvent(
        key=key, ticker=ticker, label="The recall widens", first_session=first, last_session=last, first_on=day(first),
        last_on=day(last), probability=probability, direction=direction, low_pct=low, high_pct=high,
        check_kind=check, check_keywords=list(keywords), check_level=level,
        check_text="A headline says the recall has widened.", basis="The recall in the headlines.",
    )


def judged(event, bars=None, headlines=None, as_of=None, close=100.0, asked_at=ASKED):
    return score_event(ARM, MONDAY, event, close, bars, headlines, as_of or event.last_on, asked_at)


def test_a_close_beyond_the_level_inside_the_events_days_is_the_event():
    event = proposed("close_below", level=92.0)
    hit = judged(event, frame({day(3): 91.9}))
    assert (hit.status, hit.happened) == ("scored", True) and hit.brier == pytest.approx(0.64)
    assert hit.judged_by == f"The close of {day(3)} was below 92.00."
    # Exactly at the level counts. Before the first day or after the last does not, and a low below it is not a close.
    assert judged(event, frame({day(2): 92.0})).happened is True
    for bars in (frame({day(1): 80.0}), frame({day(5): 80.0}), frame({}, lows={day(3): 80.0}), frame({day(3): 92.01})):
        miss = judged(event, bars)
        assert (miss.status, miss.happened) == ("scored", False) and miss.brier == pytest.approx(0.04)
        assert miss.judged_by == "No close in those days was below 92.00."
    above = proposed("close_above", level=104.0, direction="up")
    assert judged(above, frame({day(4): 104.0})).happened is True
    assert judged(above, frame({day(4): 103.99})).happened is False
    assert "above 104.00" in judged(above, frame({})).judged_by
    # A split since the meeting: the level is taken as a share of the meeting's close.
    assert judged(event, frame({day(3): 45.9}, base=50.0)).happened is True
    assert judged(event, frame({day(3): 46.1}, base=50.0)).happened is False


def test_a_one_day_move_of_the_stated_size_in_the_stated_direction_is_the_event():
    event = proposed("day_move", level=None, low=4.0)
    assert judged(event, frame({day(3): 96.0})).happened is True           # down 4.0% from 100
    assert "moved -4.0% in a day" in judged(event, frame({day(3): 96.0})).judged_by
    assert judged(event, frame({day(3): 96.1})).happened is False
    # A move of exactly the stated size counts. (A quarter, so that the arithmetic leaves nothing over.)
    quarter = proposed("day_move", level=None, low=25.0, high=30.0)
    assert judged(quarter, frame({day(3): 75.0})).happened is True
    assert judged(quarter, frame({day(3): 75.01})).happened is False
    assert judged(event, frame({day(3): 105.0, day(4): 105.0})).happened is False       # the other way
    # Measured from the day before, so the first day's move is measured from the day before the event's days.
    assert judged(event, frame({day(1): 104.5, day(2): 100.0})).happened is True
    assert judged(event, frame({day(4): 100.0, day(5): 90.0})).happened is False      # a day too late
    rise = proposed("day_move", level=None, direction="up", low=3.0)
    assert judged(rise, frame({day(2): 103.0})).happened is True
    assert judged(rise, frame({day(2): 102.9})).judged_by == "No day in those days moved up by 3% or more."


UNREAD = "No ledger at hand read ACME's headlines through those days without a gap, so it is scored neither way."


def test_a_word_in_a_headline_is_the_event_and_none_in_a_reading_without_a_gap_is_not():
    event = proposed("headline", level=None, keywords=["recall", "widens"])
    asked = []

    def read(ticker, first, last, after):
        asked.append((ticker, first, last, after))
        return ["Acme posts record sales", "Acme RECALL now covers six models"], True

    hit = judged(event, headlines=read)
    # The ledger is asked for what belongs to the event's days and came in after the meeting that proposed it.
    assert (hit.status, hit.happened) == ("scored", True) and asked == [("ACME", day(2), day(4), ASKED)]
    assert hit.judged_by == "A headline in those days has 'recall': Acme RECALL now covers six models"
    miss = judged(event, headlines=lambda *_: (["Acme posts record sales"], True))
    assert (miss.status, miss.happened) == ("scored", False)
    assert miss.judged_by == "No headline of the 1 belonging to those days has any of: 'recall', 'widens'."
    assert judged(event, headlines=lambda *_: ([], True)).happened is False
    # Any of the words will do, not the first one only.
    second = judged(event, headlines=lambda *_: (["Acme probe widens"], True))
    assert second.happened is True and second.judged_by == "A headline in those days has 'widens': Acme probe widens"
    # Whole words only: "recall" is not found in "recalls", nor "widens" in "widenst".
    assert judged(event, headlines=lambda *_: (["Acme recalls nothing", "It widenst"], True)).happened is False
    assert judged(proposed("headline", level=None, keywords=["safety probe"]),
                  headlines=lambda *_: (["A Safety Probe is opened"], True)).happened is True


def test_a_reading_of_the_headlines_with_a_gap_in_it_is_no_verdict_either_way():
    event = proposed("headline", level=None, keywords=["recall", "widens"])
    # Not "did not happen": the headline may have gone by unseen.
    gap = judged(event, headlines=lambda *_: (["Acme posts record sales"], False))
    assert (gap.status, gap.happened, gap.brier, gap.judged_by) == ("unread", None, None, UNREAD)
    # Nor "happened", though the headline was seen: scoring only the events seen to happen would tilt the record
    # towards them. What was seen is said.
    seen = judged(event, headlines=lambda *_: (["Acme posts record sales", "Acme recall widens"], False))
    assert (seen.status, seen.happened, seen.brier) == ("unread", None, None)
    assert seen.judged_by == UNREAD + " (A headline with 'recall' was seen: Acme recall widens)"
    # No ledger at hand, or no saying when the event was proposed: no verdict, and the event waits.
    for blind in (judged(event, headlines=None), judged(event, headlines=lambda *_: (["Acme recall widens"], True), asked_at=None)):
        assert (blind.status, blind.happened, blind.brier, blind.judged_by) == ("unread", None, None, UNREAD)


def test_an_event_waits_for_its_last_day_and_one_checked_in_words_only_is_never_scored():
    event = proposed("close_below", level=92.0)
    early = judged(event, frame({day(2): 80.0}), as_of=day(3))
    assert (early.status, early.happened) == ("pending", None)
    words = judged(proposed("other", level=None), frame({day(2): 80.0}))
    assert (words.status, words.judged_by) == ("cannot_be_judged", "Its check is in words only.")
    assert judged(event, None).status == "unpriced" and judged(event, frame({}, last=day(3))).status == "unpriced"


def test_when_a_score_is_final():
    scored = score_outcome(ARM, MONDAY, table(), "measured", frame({}), as_of=MATURES)
    assert not settled(scored)
    final = trading_day_after(MATURES, SETTLE_SESSIONS)
    assert settled(scored.model_copy(update={"scored_on": final}))
    assert not settled(scored.model_copy(update={"scored_on": trading_day_after(MATURES, SETTLE_SESSIONS - 1)}))
    assert not settled(score_outcome(ARM, MONDAY, table(), "measured", None, as_of=final))        # unpriced: tried again
    assert settled(judged(proposed("other", level=None)))
    # An event nobody could read the headlines for is never final: a ledger may be at hand another time.
    unread = judged(proposed("headline", level=None, keywords=["widens"]), headlines=None)
    assert unread.status == "unread" and not settled(unread.model_copy(update={"scored_on": trading_day_after(final, 30)}))
    event = judged(proposed("close_below", level=92.0), frame({}), as_of=day(4))
    assert not settled(event) and settled(event.model_copy(update={"scored_on": trading_day_after(day(4), SETTLE_SESSIONS)}))


# ── On record ────────────────────────────────────────────────────────────────

def a_set(measured, rehearsed=(), events=(), session=MONDAY) -> BackwardSet:
    return BackwardSet(
        arm=ARM, session_date=session, run_id=f"{session}_run", created_at=datetime(2026, 11, 23, 23, 30, tzinfo=timezone.utc),
        params_version=1, seed=7, paths=10_000, measured=list(measured), rehearsed=list(rehearsed), events=list(events),
    )


def test_every_ending_and_event_of_a_set_gets_a_row_that_waits(tmp_path):
    tables = a_set([table("ACME"), table("BOLT")], [table("ACME", good=0.2, bad=0.4, proposed=True)],
                   [proposed(), proposed("other", level=None, key="2"), proposed(ticker="GONE")])
    endings, events = pending(tables)
    assert [(row.ticker, row.basis, row.status) for row in endings] == [
        ("ACME", "measured", "pending"), ("BOLT", "measured", "pending"), ("ACME", "rehearsed", "pending")]
    # An event for a name that was not worked back has no close to be judged against, and no row.
    assert [(row.ticker, row.key, row.status) for row in events] == [("ACME", "1", "pending"), ("ACME", "2", "cannot_be_judged")]
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_backward_set(tables)
    journal.save_outcome_scores(endings)
    journal.save_event_scores(events)
    assert journal.backward_set(ARM, MONDAY) == tables and journal.backward_sets(ARM) == [tables]
    assert journal.outcome_scores(ARM) == sorted(endings, key=lambda row: (row.ticker, row.basis))
    assert journal.event_scores(ARM) == events and journal.backward_set(ARM, MATURES) is None
    # A session worked back again replaces the set, and the rows that measured the old one go with it.
    journal.save_backward_set(a_set([table("BOLT")]))
    assert journal.outcome_scores(ARM) == [] and journal.event_scores(ARM) == []
    assert [item.ticker for item in journal.backward_set(ARM, MONDAY).measured] == ["BOLT"]
    assert journal.outcome_scores("other/arm") == [] and journal.backward_sets("other/arm") == []


def test_reconciling_scores_what_has_come_due_keeps_what_has_settled_and_says_what_is_new(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    tables = a_set([table("ACME"), table("BOLT", sessions=10)], [table("ACME", good=0.2, bad=0.5, proposed=True)],
                   [proposed("close_below", level=92.0), proposed("headline", level=None, keywords=["recall"], key="2")])
    journal.save_backward_set(tables)
    bars = {"ACME": frame({day(3): 91.0}, lows={day(3): 90.0}), "BOLT": frame({})}
    asked = []

    def fetch(ticker):
        asked.append(ticker)
        return bars.get(ticker)

    read = lambda *_: (["Acme recall widens"], True)                                         # noqa: E731
    endings, events, fresh = reconcile(journal, ARM, fetch, read, as_of=day(4))
    # Day 4: the events' last day has come, the five-session endings have not.
    assert [(row.ticker, row.basis, row.status) for row in endings] == [
        ("ACME", "measured", "pending"), ("BOLT", "measured", "pending"), ("ACME", "rehearsed", "pending")]
    assert [(row.key, row.status, row.happened) for row in events] == [("1", "scored", True), ("2", "scored", True)]
    assert fresh == events and asked == ["ACME"]

    endings, events, fresh = reconcile(journal, ARM, fetch, read, as_of=MATURES)
    assert [(row.ticker, row.basis, row.status, row.outcome) for row in endings] == [
        ("ACME", "measured", "scored", "bad"), ("BOLT", "measured", "pending", None), ("ACME", "rehearsed", "scored", "bad")]
    assert [row.basis for row in fresh] == ["measured", "rehearsed"]
    # The reading with the events in gave the stop more: it scores better here.
    measured, rehearsed = endings[0], endings[2]
    assert rehearsed.brier < measured.brier
    assert journal.outcome_scores(ARM) == sorted(endings, key=lambda row: (row.ticker, row.basis))

    # Two sessions after the end a score is final: the prices are not even asked for again.
    final = trading_day_after(MATURES, SETTLE_SESSIONS)
    reconcile(journal, ARM, fetch, read, as_of=final)
    asked.clear()
    bars["ACME"] = frame({})                                        # the source now tells another story
    endings, events, fresh = reconcile(journal, ARM, fetch, lambda *_: ([], True), as_of=trading_day_after(final, 1))
    assert endings[0].outcome == "bad" and events[0].happened is True and events[1].happened is True
    assert fresh == [] and asked == []


def test_news_is_what_came_after_the_cut_off_of_what_the_seat_that_proposed_the_event_was_shown(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    shown = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)       # half an hour before the set itself was made
    event = proposed("headline", level=None, keywords=["recall"])
    asked = []

    def read(ticker, first, last, after):
        asked.append(after)
        return [], True

    journal.save_backward_set(a_set([table("ACME")], events=[event]).model_copy(update={"asked_at": shown}))
    assert journal.backward_set(ARM, MONDAY).asked_at == shown
    reconcile(journal, ARM, lambda ticker: frame({}), read, as_of=day(4))
    assert asked == [shown]
    # A set kept without it: the moment the set was made stands in.
    journal.save_backward_set(a_set([table("ACME")], events=[event]))
    reconcile(journal, ARM, lambda ticker: frame({}), read, as_of=day(4))
    assert asked == [shown, ASKED]


def test_a_score_not_yet_final_is_worked_out_again_and_stands_if_the_prices_have_gone_missing(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_backward_set(a_set([table("ACME")], events=[proposed("close_below", level=92.0)]))
    bars = {"ACME": frame({}, lows={day(3): 90.0})}
    first, events, _ = reconcile(journal, ARM, bars.get, None, as_of=MATURES)
    assert first[0].outcome == "bad" and events[0].happened is False
    bars["ACME"] = frame({day(3): 91.0}, highs={day(1): 112.0})      # a corrected bar the next day
    again, events, fresh = reconcile(journal, ARM, bars.get, None, as_of=trading_day_after(MATURES, 1))
    assert again[0].outcome == "good" and events[0].happened is True and fresh == []
    gone, events, fresh = reconcile(journal, ARM, lambda ticker: None, None, as_of=trading_day_after(MATURES, 1))
    assert (gone[0].status, gone[0].outcome) == ("scored", "good") and events[0].happened is True and fresh == []


# ── The record in words ──────────────────────────────────────────────────────

def test_the_tally_counts_scored_ones_only_by_reading_and_says_it_in_words(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_backward_set(a_set(
        [table("ACME"), table("BOLT"), table("CASK", sessions=10)], [table("ACME", good=0.2, bad=0.5, proposed=True)],
        [proposed("close_below", level=92.0), proposed("other", level=None, key="2"),
         proposed("close_below", level=92.0, key="3", ticker="BOLT", probability=0.5),
         proposed("close_below", level=95.0, key="4", probability=0.6)],
    ))
    bars = {"ACME": frame({day(3): 91.0}, lows={day(3): 90.0}), "BOLT": frame({}, highs={day(2): 111.0}), "CASK": frame({})}
    endings, events, _ = reconcile(journal, ARM, bars.get, None, as_of=MATURES)
    summary = tally(endings, events)
    measured, rehearsed = summary["outcomes"]["measured"], summary["outcomes"]["rehearsed"]
    assert (measured["scored"], measured["sessions"]) == (2, 1)
    assert measured["ended"] == {"good": 1, "bad": 1, "middle": 0}
    assert measured["mean_chance"] == {"good": pytest.approx(0.3), "bad": pytest.approx(0.25), "middle": pytest.approx(0.45)}
    assert measured["brier"] == pytest.approx(((0.09 + 0.5625 + 0.2025) + (0.49 + 0.0625 + 0.2025)) / 2)
    assert measured["plain_brier"] == pytest.approx(((0.04 + 0.49 + 0.25) + (0.64 + 0.09 + 0.25)) / 2)
    assert (rehearsed["scored"], rehearsed["ended"]) == (1, {"good": 0, "bad": 1, "middle": 0})
    assert summary["events"] == {
        "proposed": 4, "scored": 3, "happened": 2, "cannot_be_judged": 1, "unread": 0, "pending": 0,
        "mean_probability": pytest.approx((0.2 + 0.5 + 0.6) / 3), "brier": pytest.approx((0.64 + 0.25 + 0.16) / 3),
    }
    text = in_words(summary)
    assert text.startswith("From the simulation alone, 2 trade(s) worked back on 1 day(s) have ended: 1 at the target, "
                           "1 at the stop, 0 at neither. Brier score 0.805, the plain forecast's 0.880 (lower is better).")
    assert "With the proposed events put in, 1 trade(s) worked back on 1 day(s) have ended: 0 at the target, 1 at the stop" in text
    assert "Of 3 proposed event(s) that could be judged, 2 happened; they were given 43% on average. Brier score 0.350." in text
    assert text.endswith("1 proposed event(s) cannot be judged by code and are not scored.")
    # Nothing scored, nothing said.
    assert in_words(tally(*pending(a_set([table("ACME")])))) == ""
    # An event that waits for a ledger is counted apart, and said.
    waiting = a_set([table("ACME")], events=[proposed("headline", level=None, keywords=["widens"])])
    journal.save_backward_set(waiting)
    _, unread, _ = reconcile(journal, ARM, bars.get, None, as_of=MATURES)
    assert tally([], unread)["events"]["unread"] == 1 and tally([], unread)["events"]["pending"] == 0
    assert in_words(tally([], unread)) == ("1 wait for a reading of the name's headlines that covers their days "
                                           "without a gap.")
    # A ledger at hand the next time whose reading has a gap in it changes nothing, whatever it saw.
    _, still, fresh = reconcile(journal, ARM, bars.get, lambda *_: (["Acme recall widens"], False), as_of=MATURES)
    assert still[0].status == "unread" and fresh == []
    # With one that read those days without a gap, it is judged after all.
    _, judged_now, fresh = reconcile(journal, ARM, bars.get, lambda *_: (["Acme recall widens"], True), as_of=MATURES)
    assert (judged_now[0].status, judged_now[0].happened) == ("scored", True) and fresh == judged_now
    assert tally([], [])["outcomes"] == {} and tally([], [])["events"]["brier"] is None
