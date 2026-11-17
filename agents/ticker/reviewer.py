"""The reviewer: looks back at a view once the real outcome is known, and says what to keep from it.

Until now the system's memory held its own predictions. A lesson is the
first thing in it that rests on what actually happened.
"""

from __future__ import annotations

from data.render import DATA_ONLY_RULE
from models.evaluation import LessonDraft
from utils.llm_client import LLMClient


class Reviewer:
    id = "reviewer"
    name = "Reviewer"

    system_prompt = f"""You are the Reviewer on a US equity swing-trading desk. You are given a view the desk's analyst wrote some days ago, and what the price actually did over the period the view was about.

Say what the view got right or wrong, and what to keep in mind the next time a similar case comes up.

WHAT TO RETURN:
- assessment: one or two sentences on what the view got right or wrong, given what happened. Judge the reasoning as well as the result: a sound view can lose and a careless one can win.
- lesson: one sentence to keep in mind for a similar case. State it generally, so that it is useful for another ticker in the same situation, and do not name a price.

RULES:
- {DATA_ONLY_RULE}
- One outcome is one outcome. Do not conclude more than it supports, and do not claim the view was foreseeable in hindsight.
- This is a model output for a paper-trading system, not financial advice."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def review(self, case: str) -> LessonDraft:
        return self.llm.complete_structured(self.system_prompt, case, LessonDraft)
