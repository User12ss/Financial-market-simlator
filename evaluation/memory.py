"""Reading lessons back: what the ticker analyst is shown about earlier, similar views.

"Similar" is decided by plain rules, not by a search: first what was learned
about the same ticker, then about other tickers of the same sector. Three
things keep this honest:
- A lesson is only shown for a decision made after its outcome was known.
- A lesson that failed a check when it was written is kept in the journal
  but never shown: one that states figures the reviewer was never shown
  would hand a made-up number to the next model as if it were data, and one
  whose sentence names any figure or runs on is not the general statement
  that was asked for.
- The facts on each line (what the price and the benchmark did) are code's.
  Only the sentence after "Lesson:" is a model's, and it is one line without
  figures.

The pipeline shows lessons only when PIPELINE_USE_LESSONS is on.
"""

from __future__ import annotations

from datetime import date

from journal.store import Journal
from models.evaluation import Lesson

HEADING = (
    "LESSONS FROM EARLIER VIEWS (each written after the real outcome was known; "
    "they are background for your judgement, not a verdict on this ticker)"
)


def lessons_for(journal: Journal, arm: str, ticker: str, sector: str, session: date, limit: int) -> list[Lesson]:
    """Up to `limit` lessons for a view of `ticker` written after the close of `session`."""
    if limit < 1:
        return []
    known = journal.lessons(arm, known_by=session, clean_only=True)      # most recently learned first
    same_ticker = [lesson for lesson in known if lesson.ticker == ticker]
    same_sector = [lesson for lesson in known if lesson.ticker != ticker and sector and lesson.sector == sector]
    return (same_ticker + same_sector)[:limit]


def render_lessons(lessons: list[Lesson]) -> str:
    """The block shown to the analyst, or "" when there is nothing to show."""
    if not lessons:
        return ""
    return "\n".join([HEADING] + [f"- {lesson.outcome} Lesson: {lesson.lesson}" for lesson in lessons])
