"""Writing lessons: a model looks back at one of its own views, with the real outcome beside it.

Only a few are written per evaluation, because each is a model call: first
the views the desk actually traded on, then the views that were most
confident and wrong, then the ones most confident and right. A neutral view
claims nothing and gets no lesson.
"""

from __future__ import annotations

from datetime import date, datetime

from agents.ticker.reviewer import Reviewer
from journal.store import Journal
from models.evaluation import Lesson, Trade, ViewScore
from models.signals import SignalTable, TickerView
from pipeline.checks import figure_warning
from pipeline.report import render_view
from utils.grounding import unsupported_figures
from utils.llm_client import LLMClient, LLMError

# The one sentence that may later be shown to the analyst has to be short.
LESSON_LIMIT = 300


def _moved(percent: float) -> str:
    if percent == 0:
        return "did not move"
    return f"{'rose' if percent > 0 else 'fell'} {abs(percent):.2f}%"


def outcome_line(score: ViewScore, benchmark: str) -> str:
    """What happened, as code states it. Only for a scored view that took a side."""
    verdict = "right" if score.direction_right else "wrong"
    if score.excess_pct == 0:
        against = f"matched {benchmark}"
    else:
        against = f"{'beat' if score.excess_pct > 0 else 'lagged'} {benchmark} by {abs(score.excess_pct):.2f} points"
    return (
        f"{score.ticker}, {score.direction} at confidence {score.confidence:.2f} for {score.horizon_days} "
        f"session(s) from {score.starts_on} to {score.matures_on}: it {_moved(score.return_pct)} while {benchmark} "
        f"{_moved(score.benchmark_return_pct)}, so the direction was {verdict} and the ticker {against}."
    )


def trade_line(trade: Trade, through: date) -> str:
    """What the desk did about the view, up to the close of `through` and no further.

    A lesson is dated by the day its view matured. What the desk did with
    the position after that day was not known then, so it is not told.
    """
    text = f"The desk bought {trade.shares} shares at {trade.entry_price:,.2f} on {trade.entry_date}."
    sold = 0
    for exit_ in trade.exits:
        if exit_.exit_date > through:
            continue
        how = {"stop": "by its stop", "close": "by the broker", "market": "at the open", "limit": "at its limit"}[exit_.kind]
        result = "" if exit_.pnl_pct is None else f", {exit_.pnl_pct:+.2f}% against cost"
        text += f" {exit_.shares} were sold {how} at {exit_.price:,.2f} on {exit_.exit_date}{result}."
        sold += exit_.shares
    if sold < trade.shares:
        text += f" At the close of {through} it still held {trade.shares - sold}."
    return text


def case_text(view: TickerView, score: ViewScore, benchmark: str, trade: Trade | None) -> str:
    """Everything the reviewer is shown: nothing in it happened after the view matured."""
    parts = [
        f"THE VIEW (written after the close of {view.session_date})\n{render_view(view)}",
        f"WHAT HAPPENED\n{outcome_line(score, benchmark)}",
    ]
    if trade is not None:
        parts.append(f"WHAT THE DESK DID\n{trade_line(trade, score.matures_on)}")
    parts.append(f"Review the view of {view.ticker}.")
    return "\n\n".join(parts)


def _traded(score: ViewScore, trades: list[Trade]) -> Trade | None:
    return next((t for t in trades if t.ticker == score.ticker and t.decided_on == score.session_date), None)


def pick_cases(scores: list[ViewScore], written: set[tuple[str, str]], trades: list[Trade], limit: int) -> list[ViewScore]:
    """The views to review now, most worth reviewing first. `written` holds (ticker, session) of existing lessons."""
    open_cases = [
        score for score in scores
        if score.status == "scored" and score.direction != "neutral"
        and (score.ticker, score.session_date.isoformat()) not in written
    ]

    def priority(score: ViewScore) -> tuple:
        traded = _traded(score, trades) is not None
        return (0 if traded else 1, 0 if not score.direction_right else 1, -score.confidence,
                score.session_date, score.ticker)

    return sorted(open_cases, key=priority)[:max(0, limit)]


def write_lessons(
    journal: Journal, arm: str, llm: LLMClient, *, scores: list[ViewScore], tables: list[SignalTable],
    trades: list[Trade], benchmark: str, limit: int, now: datetime,
) -> tuple[list[Lesson], list[str]]:
    """Have the model review up to `limit` views. Returns the lessons saved and the problems met."""
    views = {(table.run_id, view.ticker): view for table in tables for view in table.views}
    written = {(lesson.ticker, lesson.session_date.isoformat()) for lesson in journal.lessons(arm)}
    reviewer = Reviewer(llm)
    lessons, problems = [], []
    for score in pick_cases(scores, written, trades, limit):
        view = views.get((score.run_id, score.ticker))
        if view is None:
            continue
        case = case_text(view, score, benchmark, _traded(score, trades))
        try:
            draft = reviewer.review(case)
        except LLMError as e:
            problems.append(f"{score.ticker} ({score.session_date}): the review failed. {e}")
            continue
        # One line each, whatever the model sent: this text may be put into another prompt.
        assessment, sentence = " ".join(draft.assessment.split()), " ".join(draft.lesson.split())
        warnings = figure_warning(f"{assessment}\n{sentence}", [case, reviewer.system_prompt])
        # The lesson itself was asked to be general. One that names figures, even true ones,
        # would carry this ticker's prices into the view of another; one that runs on is not a lesson.
        if unsupported_figures(sentence, []):
            warnings.append("The lesson names figures, where a general statement was asked for.")
        if len(sentence) > LESSON_LIMIT:
            warnings.append(f"The lesson is longer than {LESSON_LIMIT} characters.")
        lesson = Lesson(
            arm=arm, ticker=score.ticker, sector=score.sector, session_date=score.session_date,
            known_on=score.matures_on, direction=score.direction, confidence=score.confidence,
            outcome=outcome_line(score, benchmark), assessment=assessment, lesson=sentence,
            data_warnings=warnings, created_at=now,
        )
        journal.save_lesson(lesson)
        lessons.append(lesson)
    return lessons, problems
