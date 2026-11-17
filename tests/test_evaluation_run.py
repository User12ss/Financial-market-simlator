"""The evaluation end to end: the journal's new tables, lessons, and the report on a synthetic week."""

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

import config
from data.providers import DataError
from evaluation import run as evaluation_run
from evaluation.baselines import replay
from evaluation.lessons import case_text, outcome_line, pick_cases, trade_line, write_lessons
from evaluation.memory import HEADING, lessons_for, render_lessons
from evaluation.prices import Prices
from evaluation.report import to_markdown
from evaluation.run import evaluate, save, usage_of
from evaluation.trades import trades_of
from execution.paper import PaperBroker
from journal import store
from journal.store import Journal, JournalError
from models.evaluation import Evaluation, Lesson, LessonDraft, TradeExit
from models.signals import ProposalSetDraft, TickerViewDraft
from pipeline.daily import Arm
from tests.pipeline_fakes import ScriptedLLM, SyntheticMarket, by_ticker, day, make_pipeline, proposals, session, view
from tests.test_evaluation import a_table, a_trade, nov, scored
from utils.llm_client import LLMError


# ── The journal's added tables ───────────────────────────────────────────────

def tables_in(path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_a_new_journal_has_the_evaluations_tables(tmp_path):
    Journal(tmp_path / "journal.sqlite3").close()
    assert {"view_scores", "lessons"} <= tables_in(tmp_path / "journal.sqlite3")


def test_a_journal_written_before_the_tables_existed_keeps_everything_and_gains_them(tmp_path):
    path = tmp_path / "journal.sqlite3"
    with sqlite3.connect(path) as db:                   # as the earlier program left it
        db.executescript(store._SCHEMA + f"PRAGMA user_version = {store.SCHEMA_VERSION};")
        db.execute("INSERT INTO runs (run_id, arm, session_date, started_at, status) "
                   "VALUES ('r1', 'fake/a', '2026-11-13', '2026-11-13T22:30:00+00:00', 'ok')")
        db.execute("INSERT INTO paper_accounts VALUES ('fake/a', 91234.5, 100000, 100000, '2026-11-13')")
    assert not {"view_scores", "lessons"} & tables_in(path)

    journal = Journal(path)

    assert {"view_scores", "lessons"} <= tables_in(path)
    assert [row["run_id"] for row in journal.runs("fake/a")] == ["r1"]
    assert journal.rows("SELECT cash FROM paper_accounts")[0]["cash"] == 91234.5
    journal.save_view_scores("fake/a", [scored()])
    assert len(journal.view_scores("fake/a")) == 1
    journal.close()
    # The earlier program never looks at the new tables, and the format number it checks is unchanged.
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 2


def test_opening_a_journal_that_has_the_tables_does_not_wait_for_a_run_in_progress(tmp_path):
    path = tmp_path / "journal.sqlite3"
    running = Journal(path)
    with running.transaction():                         # the pipeline holds the write lock
        reader = Journal(path, timeout=0.2)             # the evaluation opens the same file
        assert reader.view_scores("fake/a") == []
        reader.close()
    running.close()


def test_a_journal_in_another_format_is_still_refused(tmp_path):
    path = tmp_path / "journal.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(store._SCHEMA + "PRAGMA user_version = 1;")
    with pytest.raises(JournalError, match="format 1"):
        Journal(path)
    assert not {"view_scores", "lessons"} & tables_in(path)


def a_lesson(ticker="AAA", written=nov(9), known=nov(12), sector="Technology", warnings=(), text="Wait for the trend.",
             arm="fake/a", outcome=None) -> Lesson:
    return Lesson(
        arm=arm, ticker=ticker, sector=sector, session_date=written, known_on=known, direction="bullish",
        confidence=0.7, outcome=outcome or f"{ticker} fell.", assessment="The view leaned on one data point.",
        lesson=text, data_warnings=list(warnings), created_at=datetime(2026, 11, 13, tzinfo=timezone.utc),
    )


def test_scores_and_lessons_are_kept_per_arm_and_replaced_not_duplicated(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_view_scores("fake/a", [scored(ticker="AAA", status="pending"), scored(ticker="BBB"),
                                        scored(ticker="OLD", written=nov(5))])
    other_arms = scored(ticker="AAA", made=9.0).model_copy(update={"run_id": "run-of-b", "arm": "fake/b"})
    journal.save_view_scores("fake/b", [other_arms])
    # The next evaluation: AAA is now scored, and the run that held OLD has been replaced by a forced rerun.
    journal.save_view_scores("fake/a", [scored(ticker="AAA", made=3.0), scored(ticker="BBB")])
    kept = journal.view_scores("fake/a")
    assert [(s.ticker, s.status, s.return_pct) for s in kept] == [("AAA", "scored", 3.0), ("BBB", "scored", 2.0)]
    assert [s.return_pct for s in journal.view_scores("fake/b")] == [9.0]       # another arm's are left alone

    journal.save_lesson(a_lesson(text="First wording."))
    journal.save_lesson(a_lesson(text="Second wording."))
    journal.save_lesson(a_lesson(arm="fake/b"))
    assert [lesson.lesson for lesson in journal.lessons("fake/a")] == ["Second wording."]


def test_lessons_come_back_most_recently_learned_first_and_can_be_cut_off_at_a_day(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_lesson(a_lesson("AAA", written=nov(9), known=nov(12)))
    journal.save_lesson(a_lesson("BBB", written=nov(10), known=nov(16)))
    journal.save_lesson(a_lesson("CCC", written=nov(11), known=nov(13), warnings=["States 1 figure(s) not found."]))

    assert [lesson.ticker for lesson in journal.lessons("fake/a")] == ["BBB", "CCC", "AAA"]
    assert [lesson.ticker for lesson in journal.lessons("fake/a", known_by=nov(13))] == ["CCC", "AAA"]
    assert [lesson.ticker for lesson in journal.lessons("fake/a", known_by=nov(13), clean_only=True)] == ["AAA"]
    assert journal.lessons("fake/a", known_by=nov(11)) == []


# ── Writing lessons ──────────────────────────────────────────────────────────

def test_the_outcome_is_stated_by_code_in_plain_words():
    wrong = scored("bullish", made=-11.26, reference=-0.24, confidence=0.85, written=nov(9), ticker="AMD")
    assert outcome_line(wrong, "SPY") == (
        "AMD, bullish at confidence 0.85 for 3 session(s) from 2026-11-10 to 2026-11-12: it fell 11.26% while SPY "
        "fell 0.24%, so the direction was wrong and the ticker lagged SPY by 11.02 points."
    )
    right = scored("bearish", made=-2.0, reference=0.01, ticker="XYZ")
    assert "it fell 2.00% while SPY rose 0.01%, so the direction was right and the ticker lagged SPY by 2.01 points." in (
        outcome_line(right, "SPY"))
    # The words never say more, or less, than the score does: a tiny rise is a rise, and only nothing is nothing.
    tiny = scored("bullish", made=0.004, reference=0.0)
    assert "it rose 0.00% while SPY did not move, so the direction was right and the ticker beat SPY" in outcome_line(tiny, "SPY")
    level = scored("bullish", made=1.5, reference=1.5)
    assert outcome_line(level, "SPY").endswith("so the direction was right and the ticker matched SPY.")


def test_what_the_desk_did_about_a_view_is_stated_up_to_the_day_the_view_matured_and_no_further():
    stopped = TradeExit(exit_date=nov(12), price=92.0, shares=10, share_of_position=1.0, kind="stop", pnl_usd=-80.0,
                        pnl_pct=-8.0)
    trade = a_trade("AAA", shares=10, exits=[stopped], status="closed")
    assert trade_line(trade, nov(12)) == (
        "The desk bought 10 shares at 100.00 on 2026-11-10. 10 were sold by its stop at 92.00 on 2026-11-12, "
        "-8.00% against cost."
    )
    # A lesson dated the 11th must not know about a sale on the 12th.
    assert trade_line(trade, nov(11)) == (
        "The desk bought 10 shares at 100.00 on 2026-11-10. At the close of 2026-11-11 it still held 10."
    )
    half = TradeExit(exit_date=nov(11), price=104.0, shares=4, share_of_position=0.4, kind="market", pnl_usd=16.0,
                     pnl_pct=4.0)
    assert trade_line(a_trade("AAA", shares=10, exits=[half]), nov(12)).endswith(
        "4 were sold at the open at 104.00 on 2026-11-11, +4.00% against cost. At the close of 2026-11-12 it still held 6.")


def test_the_reviewer_is_shown_the_view_the_outcome_and_what_the_desk_did():
    table = a_table(nov(9), ["AAA"])
    case = case_text(table.views[0], scored(written=nov(9)), "SPY", a_trade("AAA", shares=10))
    assert "THE VIEW (written after the close of 2026-11-09)" in case and "AAA [candidate]: bullish" in case
    assert "WHAT HAPPENED\nAAA, bullish at confidence 0.70" in case
    assert "WHAT THE DESK DID\nThe desk bought 10 shares" in case and case.endswith("Review the view of AAA.")
    assert "At the close of 2026-11-12 it still held 10." in case           # the day the view matured
    assert "WHAT THE DESK DID" not in case_text(table.views[0], scored(written=nov(9)), "SPY", None)


def test_views_are_reviewed_in_order_of_what_there_is_most_to_learn_from():
    scores = [
        scored("bullish", 4.0, 1.0, confidence=0.9, ticker="RIGHT"),
        scored("bullish", -4.0, 1.0, confidence=0.6, ticker="WRONG"),
        scored("bullish", -4.0, 1.0, confidence=0.9, ticker="SUREWRONG"),
        scored("bullish", 1.0, 1.0, confidence=0.5, ticker="TRADED"),
        scored("neutral", 9.0, 1.0, ticker="NEUTRAL"),
        scored(ticker="PENDING", status="pending"),
        scored(ticker="DONE"),
    ]
    trades = [a_trade("TRADED", decided=nov(9))]
    written = {("DONE", nov(9).isoformat())}

    # Traded first, then confident and wrong, then wrong, then right. Neutral, pending and reviewed ones never.
    assert [s.ticker for s in pick_cases(scores, written, trades, 10)] == ["TRADED", "SUREWRONG", "WRONG", "RIGHT"]
    assert [s.ticker for s in pick_cases(scores, written, trades, 2)] == ["TRADED", "SUREWRONG"]
    assert pick_cases(scores, written, trades, 0) == []
    # A trade decided on another day is not this view's trade.
    assert pick_cases(scores, written, [a_trade("TRADED", decided=nov(10))], 1)[0].ticker == "SUREWRONG"


def lesson_writer(tmp_path, reply=None):
    llm = ScriptedLLM()
    llm.replies[LessonDraft] = reply or LessonDraft(
        assessment="The view leaned on momentum alone and ignored the earnings date.",
        lesson="Before a catalyst, momentum says little about the next move.",
    )
    return Journal(tmp_path / "journal.sqlite3"), llm


def write(journal, llm, scores, tables=None, trades=(), limit=5):
    return write_lessons(
        journal, "fake/a", llm, scores=scores, tables=tables or [a_table(nov(9), ["AAA", "BBB"])],
        trades=list(trades), benchmark="SPY", limit=limit, now=datetime(2026, 11, 13, tzinfo=timezone.utc),
    )


def test_a_lesson_keeps_the_models_words_beside_facts_stated_by_code(tmp_path):
    journal, llm = lesson_writer(tmp_path)
    case = scored("bullish", -4.0, 1.0, confidence=0.8, written=nov(9), ticker="AAA", sector="Energy")

    lessons, problems = write(journal, llm, [case])

    assert problems == []
    [lesson] = lessons
    assert journal.lessons("fake/a") == [lesson]
    assert (lesson.ticker, lesson.sector, lesson.session_date, lesson.known_on) == ("AAA", "Energy", nov(9), nov(12))
    assert (lesson.direction, lesson.confidence) == ("bullish", 0.8) and lesson.data_warnings == []
    assert lesson.outcome == outcome_line(case, "SPY")
    assert lesson.lesson == "Before a catalyst, momentum says little about the next move."
    [message] = llm.messages(LessonDraft)
    assert "it fell 4.00% while SPY rose 1.00%" in message and "Review the view of AAA." in message


def test_a_view_is_reviewed_once_and_only_as_many_as_asked_for(tmp_path):
    journal, llm = lesson_writer(tmp_path)
    scores = [scored(ticker="AAA", made=-1.0), scored(ticker="BBB", made=-2.0)]

    assert len(write(journal, llm, scores, limit=1)[0]) == 1
    assert len(write(journal, llm, scores, limit=5)[0]) == 1            # the other one; the first is not asked again
    assert write(journal, llm, scores, limit=5) == ([], [])
    assert llm.count(LessonDraft) == 2 and len(journal.lessons("fake/a")) == 2


def test_a_lesson_that_states_figures_the_reviewer_was_not_shown_is_kept_but_marked(tmp_path):
    journal, llm = lesson_writer(tmp_path, LessonDraft(
        assessment="The stock was 37% above its average, which the view ignored.",
        lesson="Fades after a run tend to continue.",
    ))
    [lesson], _ = write(journal, llm, [scored(ticker="AAA", made=-1.0)])
    assert "37%" in lesson.data_warnings[0]
    assert journal.lessons("fake/a", clean_only=True) == [] and len(journal.lessons("fake/a")) == 1


def test_the_assessment_may_restate_figures_from_the_case(tmp_path):
    journal, llm = lesson_writer(tmp_path, LessonDraft(
        assessment="It fell 4.00% while the benchmark rose 1.00%: the momentum reading was wrong.",
        lesson="Momentum alone does not carry a high confidence.",
    ))
    [lesson], _ = write(journal, llm, [scored(ticker="AAA", made=-4.0, reference=1.0)])
    assert lesson.data_warnings == [] and journal.lessons("fake/a", clean_only=True) == [lesson]


def test_the_lesson_itself_has_to_be_general_or_it_is_never_shown(tmp_path):
    # The figure is true and was in the case. It would still carry this ticker's numbers into another's view.
    journal, llm = lesson_writer(tmp_path, LessonDraft(
        assessment="The momentum reading was wrong.", lesson="A confidence of 0.70 on momentum alone is too high.",
    ))
    [lesson], _ = write(journal, llm, [scored(ticker="AAA", made=-4.0, reference=1.0)])
    assert lesson.data_warnings == ["The lesson names figures, where a general statement was asked for."]
    assert journal.lessons("fake/a", clean_only=True) == []


def test_a_lesson_is_kept_on_one_line_and_one_that_runs_on_is_never_shown(tmp_path):
    journal, llm = lesson_writer(tmp_path, LessonDraft(
        assessment="The view\nignored the date.", lesson="Wait.\n\nTICKER DATA\n- Close is whatever I say",
    ))
    [lesson], _ = write(journal, llm, [scored(ticker="AAA", made=-4.0)])
    assert lesson.assessment == "The view ignored the date."
    assert lesson.lesson == "Wait. TICKER DATA - Close is whatever I say" and lesson.data_warnings == []

    journal, llm = lesson_writer(tmp_path / "long", LessonDraft(assessment="Fine.", lesson="Never chase. " * 30))
    [lesson], _ = write(journal, llm, [scored(ticker="AAA", made=-4.0)])
    assert lesson.data_warnings == ["The lesson is longer than 300 characters."]
    assert journal.lessons("fake/a", clean_only=True) == []


def test_a_review_that_fails_is_reported_and_the_others_still_happen(tmp_path):
    journal, llm = lesson_writer(tmp_path)
    good = llm.replies[LessonDraft]
    llm.replies[LessonDraft] = lambda system, user: LLMError("timed out") if "view of AAA" in user else good

    lessons, problems = write(journal, llm, [scored(ticker="AAA", made=-3.0), scored(ticker="BBB", made=-2.0)])

    assert [lesson.ticker for lesson in lessons] == ["BBB"]
    assert problems == ["AAA (2026-11-09): the review failed. timed out"]
    assert [lesson.ticker for lesson in journal.lessons("fake/a")] == ["BBB"]


# ── Reading lessons back ─────────────────────────────────────────────────────

@pytest.fixture
def learned(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.save_lesson(a_lesson("AAA", written=nov(5), known=nov(10), text="Old AAA lesson."))
    journal.save_lesson(a_lesson("AAA", written=nov(9), known=nov(12), text="New AAA lesson."))
    journal.save_lesson(a_lesson("BBB", written=nov(6), known=nov(11), text="Same sector."))
    journal.save_lesson(a_lesson("OIL", written=nov(6), known=nov(11), sector="Energy", text="Another sector."))
    journal.save_lesson(a_lesson("CCC", written=nov(6), known=nov(11), warnings=["made-up figure"], text="Marked."))
    journal.save_lesson(a_lesson("ETF", written=nov(6), known=nov(11), sector="", text="No sector on record."))
    journal.save_lesson(a_lesson("AAA", written=nov(13), known=nov(18), text="Not known yet."))
    journal.save_lesson(a_lesson("AAA", written=nov(9), known=nov(12), arm="fake/b", text="Another model's."))
    return journal


def test_the_analyst_is_shown_the_same_tickers_lessons_first_then_the_same_sectors(learned):
    shown = lessons_for(learned, "fake/a", "AAA", "Technology", nov(13), 5)
    assert [lesson.lesson for lesson in shown] == ["New AAA lesson.", "Old AAA lesson.", "Same sector."]
    assert [lesson.lesson for lesson in lessons_for(learned, "fake/a", "AAA", "Technology", nov(13), 2)] == [
        "New AAA lesson.", "Old AAA lesson."]
    assert [lesson.lesson for lesson in lessons_for(learned, "fake/a", "NEW", "Energy", nov(13), 5)] == [
        "Another sector."]


def test_a_lesson_is_never_shown_before_its_outcome_was_known(learned):
    assert [lesson.lesson for lesson in lessons_for(learned, "fake/a", "AAA", "Technology", nov(11), 5)] == [
        "Old AAA lesson.", "Same sector."]
    assert lessons_for(learned, "fake/a", "AAA", "Technology", nov(9), 5) == []
    late = lessons_for(learned, "fake/a", "AAA", "Technology", nov(18), 5)
    assert late[0].lesson == "Not known yet."               # known by the close of the 18th, so usable that evening


def test_nothing_is_shown_without_a_match_a_sector_or_room(learned):
    assert lessons_for(learned, "fake/a", "NEW", "", nov(13), 5) == []          # no sector to match on
    assert lessons_for(learned, "fake/a", "NEW", "Utilities", nov(13), 5) == []
    assert lessons_for(learned, "fake/a", "AAA", "Technology", nov(13), 0) == []
    assert lessons_for(learned, "nobody", "AAA", "Technology", nov(13), 5) == []


def test_lessons_are_rendered_as_facts_then_the_models_sentence_or_not_at_all(learned):
    text = render_lessons(lessons_for(learned, "fake/a", "AAA", "Technology", nov(13), 2))
    assert text.splitlines() == [HEADING, "- AAA fell. Lesson: New AAA lesson.", "- AAA fell. Lesson: Old AAA lesson."]
    assert render_lessons([]) == ""


# ── Lessons in the daily pipeline ────────────────────────────────────────────

def seeded_pipeline(tmp_path, llm=None):
    pipeline = make_pipeline(tmp_path, llm or ScriptedLLM())
    pipeline.journal.save_lesson(a_lesson("UPA", written=nov(5), known=nov(10), text="Do not chase UPA."))
    pipeline.journal.save_lesson(a_lesson("UPA", written=nov(13), known=nov(18), text="From the future."))
    return pipeline


def test_lessons_are_not_shown_to_the_analyst_unless_they_are_turned_on(tmp_path):
    assert config.PIPELINE_USE_LESSONS is False
    llm = ScriptedLLM()
    [result] = seeded_pipeline(tmp_path, llm).run()
    assert result.status == "ok"
    assert all("LESSONS" not in message for message in llm.messages(TickerViewDraft))


def test_turned_on_the_analyst_sees_what_was_known_by_that_session_and_nothing_later(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_USE_LESSONS", True)
    llm = ScriptedLLM()
    [result] = seeded_pipeline(tmp_path, llm).run()

    assert result.status == "ok"
    messages = {message.split("Give your view of ")[1].rstrip("."): message for message in llm.messages(TickerViewDraft)}
    assert f"{HEADING}\n- UPA fell. Lesson: Do not chase UPA.\n\nTICKER DATA" in messages["UPA"]
    assert "Do not chase UPA." in messages["UPB"]           # the same sector in the synthetic market
    assert all("From the future." not in message for message in messages.values())


def test_the_limit_on_lessons_per_ticker_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_USE_LESSONS", True)
    monkeypatch.setattr(config, "PIPELINE_LESSONS_PER_TICKER", 1)
    llm = ScriptedLLM()
    pipeline = seeded_pipeline(tmp_path, llm)
    pipeline.journal.save_lesson(a_lesson("UPA", written=nov(6), known=nov(11), text="A newer UPA lesson."))
    pipeline.run()
    upa = next(message for message in llm.messages(TickerViewDraft) if message.endswith("Give your view of UPA."))
    assert "A newer UPA lesson." in upa and "Do not chase UPA." not in upa


def test_a_figure_the_analyst_takes_from_a_lesson_it_was_shown_is_not_called_made_up(tmp_path, monkeypatch):
    cites = by_ticker({"UPA": view(thesis="It fell 11.3% the last time it looked like this.")}, view())

    def warnings_with(lessons_on: bool, folder):
        monkeypatch.setattr(config, "PIPELINE_USE_LESSONS", lessons_on)
        llm = ScriptedLLM()
        llm.replies[TickerViewDraft] = cites
        pipeline = make_pipeline(folder, llm)
        pipeline.journal.save_lesson(a_lesson("UPA", written=nov(5), known=nov(10), outcome="UPA fell 11.3%."))
        [result] = pipeline.run()
        return next(v for v in result.table.views if v.ticker == "UPA").data_warnings

    assert warnings_with(True, tmp_path / "on") == []
    assert "11.3%" in warnings_with(False, tmp_path / "off")[0]


class Killed(BaseException):
    """Stands in for the process being killed."""


def test_turning_lessons_on_asks_the_analyst_again_instead_of_reusing_an_answer(tmp_path, monkeypatch):
    llm = ScriptedLLM()
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = seeded_pipeline(tmp_path, llm)
    real_submit = PaperBroker.submit

    def killed(self, request, when):
        raise Killed()

    monkeypatch.setattr(PaperBroker, "submit", killed)
    with pytest.raises(Killed):
        pipeline.run()
    monkeypatch.setattr(PaperBroker, "submit", real_submit)
    asked = llm.count(TickerViewDraft)

    monkeypatch.setattr(config, "PIPELINE_USE_LESSONS", True)
    [result] = pipeline.run()

    # Every candidate is in the lesson's sector, so every view is now a different question.
    assert result.status == "ok" and llm.count(TickerViewDraft) == 2 * asked


# ── A synthetic week, evaluated ──────────────────────────────────────────────
# UPA, UPB and UPC rise 0.3%, 0.2% and 0.1% a day and SPY 0.05%; every bar opens at the previous close.
# The analyst is bullish on all three with a three-session horizon, and the desk buys UPA on the first day.

def a_week(tmp_path, days=(13, 16, 17, 18, 19, 20), market=None, later=None):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = view(horizon=3)
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm, market=market)
    for number in days:
        pipeline._now.now = day(number)
        [result] = pipeline.run()
        assert result.status == "ok", result.message
        llm.replies[ProposalSetDraft] = later or proposals()
    return pipeline


def grown(rate: float, sessions: int) -> float:
    return ((1 + rate) ** sessions - 1) * 100


def test_a_week_is_scored_against_the_benchmark(tmp_path):
    pipeline = a_week(tmp_path)

    evaluation = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=300, seed=1)

    assert (evaluation.as_of, evaluation.benchmark) == (session(20), "SPY")
    [arm] = evaluation.arms
    assert (arm.arm, arm.sessions_run, arm.first_session, arm.last_session) == ("fake/a", 6, session(13), session(20))
    card = arm.scorecard
    # Views of the 13th, 16th and 17th have run their three sessions by the 20th; the later ones have not.
    assert (card.total, card.scored, card.pending, card.unpriced, card.neutral, card.sessions) == (18, 9, 9, 0, 0, 3)
    assert (card.overall.views, card.overall.hit_rate, card.overall.beat_rate) == (9, 1.0, 1.0)
    expected_edge = (grown(0.003, 3) + grown(0.002, 3) + grown(0.001, 3)) / 3 - grown(0.0005, 3)
    assert card.overall.mean_edge_pct == pytest.approx(expected_edge, abs=1e-6)
    assert [(t.label, t.views) for t in card.by_sector] == [("Technology", 9)]
    assert [(t.label, t.views) for t in card.by_regime] == [("risk_on", 9)]
    assert [(t.label, t.views) for t in card.by_position] == [("candidate", 7), ("held", 2)]      # UPA, once bought
    # The scores are in the journal for the dashboard to read.
    stored = pipeline.journal.view_scores("fake/a")
    assert len(stored) == 18 and sum(1 for s in stored if s.status == "scored") == 9
    upa = next(s for s in stored if s.ticker == "UPA" and s.session_date == session(13))
    assert (upa.starts_on, upa.matures_on) == (session(16), session(18))
    assert upa.return_pct == pytest.approx(grown(0.003, 3)) and upa.sector == "Technology"


def test_the_account_is_set_beside_the_benchmark_and_the_candidates(tmp_path):
    pipeline = a_week(tmp_path)
    [arm] = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=10, seed=1).arms

    account, benchmark, candidates = arm.curves
    assert [point.session_date for point in account.points] == [session(n) for n in (13, 16, 17, 18, 19, 20)]
    assert account.points[0].equity == 100_000 and account.return_pct > 0
    # SPY bought at the open of the 16th, five sessions of 0.05% by the close of the 20th.
    assert benchmark.return_pct == pytest.approx(grown(0.0005, 5), abs=1e-6)
    # The three candidates in equal weight, every day: 0.2% a day on average.
    assert candidates.return_pct == pytest.approx(grown(0.002, 5), abs=1e-3)
    assert account.return_pct < candidates.return_pct       # a tenth of the account in one ticker, the rest in cash


def test_the_accounts_buy_is_compared_with_random_picks_from_that_days_candidates(tmp_path):
    pipeline = a_week(tmp_path)
    [arm] = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=1500, seed=1).arms

    trades, random = arm.trades, arm.random
    assert (trades.open, trades.closed) == (1, 0) and trades.unrealized_usd > 0
    [trade] = trades.trades
    assert (trade.ticker, trade.decided_on, trade.entry_date, trade.sector) == ("UPA", session(13), session(16), "Technology")
    assert (random.entries, random.skipped, random.valued_on) == (1, 0, session(20))
    [pick] = random.picks
    assert (pick.ticker, pick.pool, pick.rank) == ("UPA", 3, 1)             # it bought the best of the three
    assert random.booked_pnl_usd == pytest.approx(trades.unrealized_usd)
    # Here the real buy was sized and stopped by the rule alone, so the replay lands on the books' own figure.
    assert random.replayed_pnl_usd == pytest.approx(random.booked_pnl_usd, abs=0.05)
    assert random.percentile == pytest.approx(100 * (2 / 3 + 1 / 6), abs=4)
    assert random.quantiles_usd["95"] == random.replayed_pnl_usd


def test_when_the_pipeline_has_not_run_for_days_both_sides_stop_where_the_books_stand(tmp_path):
    pipeline = a_week(tmp_path)                             # last run after the close of the 20th
    [on_the_day] = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=50, seed=1).arms
    [days_later] = evaluate(pipeline.journal, pipeline.builder.market, now=day(25), simulations=50, seed=1).arms

    assert days_later.random.valued_on == session(20)
    assert days_later.random.replayed_pnl_usd == on_the_day.random.replayed_pnl_usd
    assert days_later.random.booked_pnl_usd == on_the_day.random.booked_pnl_usd
    assert days_later.random.picks[0].pool_mean_usd == on_the_day.random.picks[0].pool_mean_usd


def test_a_closed_trade_is_given_the_sector_its_ticker_had_in_the_views(tmp_path):
    market = SyntheticMarket(turns={"UPA": (session(18), -0.04)}, sectors={"UPA": "Energy"})
    pipeline = a_week(tmp_path, days=(13, 16, 17, 18), market=market)
    [arm] = evaluate(pipeline.journal, market, now=day(18), simulations=5).arms

    [trade] = arm.trades.trades
    assert (trade.status, trade.stopped, trade.sector) == ("closed", True, "Energy")
    assert [(s.label, s.trades) for s in arm.trades.by_sector] == [("Energy", 1)]
    assert [(s.label, s.trades) for s in arm.trades.by_exit] == [("stopped out", 1)]


def test_buying_the_weakest_candidate_shows_up_as_worse_than_random(tmp_path):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = view(horizon=3)
    llm.replies[ProposalSetDraft] = proposals(("UPC", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm)
    for number in (13, 16, 17, 18):
        pipeline._now.now = day(number)
        pipeline.run()
        llm.replies[ProposalSetDraft] = proposals()
    [arm] = evaluate(pipeline.journal, pipeline.builder.market, now=day(18), simulations=1500, seed=1).arms
    assert arm.random.picks[0].rank == 3 and arm.random.percentile == pytest.approx(100 / 6, abs=4)


def test_what_every_run_cost_is_added_up_the_failed_ones_included(tmp_path):
    pipeline = a_week(tmp_path, days=(13, 16))
    usage = usage_of(pipeline.journal, "fake/a")
    assert usage.calls == sum(row["llm_calls"] for row in pipeline.journal.runs("fake/a")) > 0
    assert usage.cost_usd is None and usage.model_seconds > 0
    assert usage_of(pipeline.journal, "nobody").calls == 0


def test_a_score_once_given_is_kept_when_the_source_later_drops_the_ticker(tmp_path):
    market = SyntheticMarket()
    pipeline = a_week(tmp_path, market=market)
    evaluate(pipeline.journal, market, now=day(20), simulations=5, seed=1)

    market.missing.add("UPB")                               # delisted: the source has nothing for it any more
    [arm] = evaluate(pipeline.journal, market, now=day(25), simulations=5, seed=1).arms
    upb = [s for s in pipeline.journal.view_scores("fake/a") if s.ticker == "UPB"]
    assert [s.status for s in upb] == ["scored"] * 3 + ["unpriced"] * 3
    assert arm.scorecard.unpriced == 3

    [again] = evaluate(pipeline.journal, market, now=day(25), simulations=5, seed=1, rescore=True).arms
    assert again.scorecard.unpriced == 6                    # asked to score everything again, and it cannot


def score_of(pipeline, ticker, written):
    return next(s for s in pipeline.journal.view_scores("fake/a") if s.ticker == ticker and s.session_date == written)


def test_a_score_is_final_only_once_it_was_worked_out_two_sessions_after_the_view_matured(tmp_path):
    market = SyntheticMarket()
    pipeline = a_week(tmp_path, days=(13, 16), market=market)

    # The 13th's views mature at the close of the 18th. Scored that very evening, the close may still change.
    evaluate(pipeline.journal, market, now=day(18), simulations=5)
    early = score_of(pipeline, "UPA", session(13))
    assert (early.status, early.scored_as_of) == ("scored", session(18))
    assert early.return_pct == pytest.approx(grown(0.003, 3))

    market.drift["UPA"] = 0.004                             # the source corrects what it had said
    evaluate(pipeline.journal, market, now=day(19), simulations=5)
    corrected = score_of(pipeline, "UPA", session(13))
    assert corrected.return_pct == pytest.approx(grown(0.004, 3)) and corrected.scored_as_of == session(19)

    evaluate(pipeline.journal, market, now=day(20), simulations=5)          # two sessions after: this one stands
    final = score_of(pipeline, "UPA", session(13))
    assert final.scored_as_of == session(20)
    market.drift["UPA"] = 0.009
    evaluate(pipeline.journal, market, now=day(23), simulations=5)
    assert score_of(pipeline, "UPA", session(13)) == final


def test_a_score_measured_against_another_benchmark_is_never_kept(tmp_path):
    pipeline = a_week(tmp_path)
    evaluate(pipeline.journal, pipeline.builder.market, now=day(25), simulations=5)
    assert {s.benchmark for s in pipeline.journal.view_scores("fake/a")} == {"SPY"}

    [arm] = evaluate(pipeline.journal, pipeline.builder.market, now=day(25), simulations=5, benchmark="UPC").arms

    scores = pipeline.journal.view_scores("fake/a")
    assert {s.benchmark for s in scores} == {"UPC"} and len(scores) == 18
    # Measured against UPC, the views of UPA and UPB beat it and the views of UPC itself cannot.
    assert arm.scorecard.overall.beat_rate == pytest.approx(2 / 3)
    assert score_of(pipeline, "UPC", session(13)).excess_pct == 0.0


def test_an_evaluation_without_the_benchmarks_prices_fails_and_writes_nothing(tmp_path):
    market = SyntheticMarket()
    pipeline = a_week(tmp_path, days=(13, 16), market=market)
    evaluate(pipeline.journal, market, now=day(20), simulations=5)
    before = pipeline.journal.view_scores("fake/a")

    market.missing.add("SPY")
    with pytest.raises(DataError, match="returned nothing for SPY"):
        evaluate(pipeline.journal, market, now=day(25), simulations=5, rescore=True)
    assert pipeline.journal.view_scores("fake/a") == before


def test_the_report_says_what_was_measured_and_what_it_cannot_say(tmp_path):
    pipeline = a_week(tmp_path)
    evaluation = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=200, seed=1)

    markdown_path, json_path = save(evaluation, tmp_path / "out")

    assert markdown_path == tmp_path / "out" / "2026-11-20" / "evaluation.md"
    assert Evaluation.model_validate_json(json_path.read_text()) == evaluation
    text = markdown_path.read_text()
    assert text == to_markdown(evaluation)
    assert "# Evaluation as of the close of 2026-11-20" in text and "## fake/a" in text
    assert "9 view(s) have not reached the end of their horizon" in text
    assert "9 scored view(s) taking a side, written on 3 session(s)." in text and "roughly 17 points" in text
    assert "0 closed trade(s): far too few" in text and "rests on 1 buy(s)" in text
    assert "| Paper account |" in text and "| SPY bought and held | +0.25% |" in text
    assert "| all | 9 | 100% | 100% |" in text and "| 0.60 to 0.69 | 9 | 100% |" in text
    assert "| UPA | 2026-11-16 |" in text and "still open" in text
    assert "The account's picks did better than **" in text and "| UPA | 2026-11-13 |" in text
    assert "valued on 2026-11-20, where the account's books stand" in text
    assert "The account's books have the same buys at +" in text and "That figure is not what is compared." in text
    assert "no per-token cost (local model)" in text
    assert "0 written by this evaluation, 0 in the journal. They are not shown to the ticker analyst" in text
    assert json.loads(json_path.read_text())["arms"][0]["scorecard"]["scored"] == 9


def test_an_empty_journal_gives_an_empty_report_not_an_error(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    evaluation = evaluate(journal, SyntheticMarket(), now=day(20))
    assert evaluation.arms == [] and "No runs recorded yet." in to_markdown(evaluation)


def test_an_arm_that_only_failed_is_reported_without_numbers(tmp_path):
    llm = ScriptedLLM(problems=["the model is not running"])
    pipeline = make_pipeline(tmp_path, llm)
    assert pipeline.run()[0].status == "failed"
    [arm] = evaluate(pipeline.journal, pipeline.builder.market, now=day(20)).arms
    assert arm.sessions_run == 0 and arm.scorecard.total == 0 and arm.random.percentile is None
    assert "No finished run to evaluate." in to_markdown(evaluate(pipeline.journal, pipeline.builder.market, now=day(20)))


def test_lessons_are_written_only_for_the_arms_given_a_model(tmp_path):
    pipeline = a_week(tmp_path)
    reviewer = ScriptedLLM()
    reviewer.replies[LessonDraft] = LessonDraft(assessment="The trend carried it.", lesson="A steady trend is worth following.")

    [quiet] = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=5).arms
    assert quiet.lessons_written == 0 and reviewer.count(LessonDraft) == 0

    [arm] = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=5,
                     reviewers={"fake/a": reviewer}).arms
    assert arm.lessons_written == config.EVALUATION_MAX_LESSONS == 5 and arm.lessons_total == 5
    lessons = pipeline.journal.lessons("fake/a")
    # The view it traded on comes first, then the rest, oldest first.
    first = reviewer.messages(LessonDraft)[0]
    assert "WHAT THE DESK DID" in first and first.endswith("Review the view of UPA.")
    assert {(lesson.ticker, lesson.session_date) for lesson in lessons} == {
        ("UPA", session(13)), ("UPB", session(13)), ("UPC", session(13)), ("UPA", session(16)), ("UPB", session(16))}
    assert all(lesson.known_on <= session(20) for lesson in lessons)


def test_each_model_configuration_is_evaluated_on_its_own_and_reviews_only_its_own_views(tmp_path):
    buys_best, buys_worst = ScriptedLLM(), ScriptedLLM()
    for llm, pick in ((buys_best, "UPA"), (buys_worst, "UPC")):
        llm.replies[TickerViewDraft] = view(horizon=1)
        llm.replies[ProposalSetDraft] = proposals((pick, "buy", 0.7))
    pipeline = make_pipeline(tmp_path, arms=[Arm("fake/a", buys_best), Arm("fake/b", buys_worst)])
    for number in (13, 16, 17):
        pipeline._now.now = day(number)
        assert [result.status for result in pipeline.run()] == ["ok", "ok"]
        for llm in (buys_best, buys_worst):
            llm.replies[ProposalSetDraft] = proposals()
    reviewer = ScriptedLLM()
    reviewer.replies[LessonDraft] = LessonDraft(assessment="The trend carried it.", lesson="Follow a steady trend.")

    first, second = evaluate(pipeline.journal, pipeline.builder.market, now=day(17), simulations=600, seed=1,
                             reviewers={"fake/a": reviewer}).arms

    assert (first.arm, second.arm) == ("fake/a", "fake/b")
    assert first.scorecard.scored == second.scorecard.scored == 6           # the views of the 13th and the 16th
    assert first.trades.trades[0].ticker == "UPA" and second.trades.trades[0].ticker == "UPC"
    assert first.random.percentile > 60 > 40 > second.random.percentile
    assert first.lessons_written == 5 and (second.lessons_written, second.lessons_total) == (0, 0)
    assert pipeline.journal.lessons("fake/b") == []


# ── The replay against the real paper broker ─────────────────────────────────

def test_replaying_a_stopped_position_gives_what_the_paper_broker_recorded(tmp_path):
    # UPA turns down 4% a day from the 18th and falls through its stop that day.
    market = SyntheticMarket(turns={"UPA": (session(18), -0.04)})
    pipeline = a_week(tmp_path, days=(13, 16, 17, 18), market=market)

    [trade] = trades_of(pipeline.journal, "fake/a")
    assert trade.status == "closed" and trade.stopped and trade.exits[-1].exit_date == session(18)
    stop = pipeline.journal.signal_table(pipeline.journal.completed_run("fake/a", session(13))).orders[0].protective_stop
    prices = Prices.fetch(market, ["UPA"], date(2026, 10, 1), session(18))

    made = replay(prices, "UPA", shares=trade.shares, stop=stop, entry_date=trade.entry_date, exits=[],
                  end=session(18), slippage=config.PAPER_SLIPPAGE_BPS / 10_000)

    assert made < 0 and made == pytest.approx(trade.realized_usd, abs=0.05)


def test_replaying_a_position_the_desk_sold_gives_what_the_paper_broker_recorded(tmp_path):
    pipeline = a_week(tmp_path, days=(13, 16, 17, 18), later=proposals(("UPA", "sell", 0.7)))

    [trade] = trades_of(pipeline.journal, "fake/a")
    assert trade.status == "closed" and not trade.stopped and trade.exits[-1].exit_date == session(17)
    stop = pipeline.journal.signal_table(pipeline.journal.completed_run("fake/a", session(13))).orders[0].protective_stop
    prices = Prices.fetch(pipeline.builder.market, ["UPA"], date(2026, 10, 1), session(18))

    made = replay(prices, "UPA", shares=trade.shares, stop=stop, entry_date=trade.entry_date,
                  exits=[(session(17), 1.0, "market")], end=session(17), slippage=config.PAPER_SLIPPAGE_BPS / 10_000)

    assert made == pytest.approx(trade.realized_usd, abs=0.05)


# ── The command ──────────────────────────────────────────────────────────────

@pytest.fixture
def offline(tmp_path, monkeypatch):
    market = SyntheticMarket()
    monkeypatch.setattr(config, "JOURNAL_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setattr(config, "EVALUATION_DIR", str(tmp_path / "evaluation"))
    monkeypatch.setattr(evaluation_run, "YFinanceProvider", lambda: market)
    return market


def one_run(tmp_path, market):
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = view(horizon=1)          # due the next session, whatever today's date is
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(tmp_path, llm, market=market)
    assert pipeline.run()[0].status == "ok"
    pipeline.journal.close()


def test_the_command_says_so_when_there_is_nothing_to_evaluate(offline, tmp_path, capsys):
    assert evaluation_run.main([]) == 0
    assert f"No journal at {tmp_path / 'journal.sqlite3'}" in capsys.readouterr().out
    assert not (tmp_path / "journal.sqlite3").exists()      # looking for a journal does not create one

    Journal(tmp_path / "journal.sqlite3").close()           # a journal, but no run in it yet
    assert evaluation_run.main([]) == 0
    assert "No runs recorded yet" in capsys.readouterr().out


def test_the_command_reports_a_journal_it_cannot_use(offline, tmp_path, capsys):
    with sqlite3.connect(tmp_path / "journal.sqlite3") as db:
        db.executescript(store._SCHEMA + "PRAGMA user_version = 1;")
    assert evaluation_run.main([]) == 1
    assert "Cannot open the journal:" in capsys.readouterr().err


def test_the_number_of_simulations_has_to_be_one_or_more(offline, capsys):
    with pytest.raises(SystemExit) as stopped:
        evaluation_run.main(["--simulations", "0"])
    assert stopped.value.code == 2 and "has to be 1 or more" in capsys.readouterr().err


def test_a_source_that_hands_back_nothing_is_a_failure_and_the_scores_on_record_are_left_alone(offline, tmp_path, capsys):
    one_run(tmp_path, offline)
    assert evaluation_run.main(["--simulations", "5"]) == 0
    capsys.readouterr()
    before = [(s.ticker, s.status, s.return_pct) for s in Journal().view_scores("fake/a")]
    assert [status for _, status, _ in before] == ["scored"] * 3

    offline.missing.update({"SPY", "UPA", "UPB", "UPC"})    # no error raised: just nothing in the answer
    assert evaluation_run.main(["--rescore"]) == 1
    assert "Cannot evaluate: The price source returned nothing for SPY" in capsys.readouterr().err
    assert [(s.ticker, s.status, s.return_pct) for s in Journal().view_scores("fake/a")] == before


def test_the_command_writes_the_report_and_says_where(offline, tmp_path, capsys):
    one_run(tmp_path, offline)

    assert evaluation_run.main(["--simulations", "20"]) == 0

    captured = capsys.readouterr()
    out = captured.out
    assert captured.err == ""                               # no model was looked for: lessons were not asked for
    assert "fake/a: 1 session(s) run" in out and "views: 3 scored, 0 not yet due" in out
    assert "direction right 100%, beat SPY 100%, of 3 view(s) taking a side" in out
    [report] = (tmp_path / "evaluation").glob("*/evaluation.md")
    assert str(report) in out and report.with_suffix(".json").is_file()
    assert "0 lesson" not in out                            # none asked for, none mentioned


def test_the_command_refuses_an_arm_the_journal_does_not_hold(offline, tmp_path, capsys):
    one_run(tmp_path, offline)
    assert evaluation_run.main(["--arm", "fake/zzz"]) == 1
    assert "Not in the journal: fake/zzz. It holds: fake/a." in capsys.readouterr().err
    assert not (tmp_path / "evaluation").exists()


def test_the_command_reports_a_price_source_that_is_down(offline, tmp_path, capsys):
    one_run(tmp_path, offline)
    offline.fail = True
    assert evaluation_run.main([]) == 1
    assert "Cannot evaluate: Yahoo Finance price download failed" in capsys.readouterr().err


def test_lessons_are_written_when_asked_for_and_a_model_that_cannot_be_reached_is_named(offline, tmp_path, capsys, monkeypatch):
    one_run(tmp_path, offline)
    assert evaluation_run.main(["--lessons", "--simulations", "5"]) == 0        # "fake" is not a provider
    captured = capsys.readouterr()
    assert "fake/a: no lessons will be written." in captured.err and "lesson(s) written" not in captured.out

    reviewer = ScriptedLLM()
    reviewer.replies[LessonDraft] = LessonDraft(assessment="The trend carried it.", lesson="Follow a steady trend.")
    monkeypatch.setattr(evaluation_run, "reviewer_for", lambda arm: reviewer)
    assert evaluation_run.main(["--lessons", "--simulations", "5"]) == 0
    assert "3 lesson(s) written" in capsys.readouterr().out
    assert len(Journal().lessons("fake/a")) == 3
