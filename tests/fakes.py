"""Test doubles: a scripted stand-in for LLMClient."""

from __future__ import annotations

import threading

from agents.base import AnalysisOutput, DeliberationOutput
from models.report import ScenarioOutcome
from orchestrator import DebateArgument, DebateVerdict, NextAction, ParsedScenario, ReportDraft
from utils.llm_client import LLMError, UsageLog


def action(name: str, agent_ids: list[str] | None = None, **kwargs) -> NextAction:
    """Build a NextAction with neutral values for the fields a test does not care about."""
    fields = {"reasoning": "test", "action": name, "agent_ids": agent_ids or [],
              "context": "", "tier": "tier1", "query": "", "tickers": [], "rounds": 2}
    fields.update(kwargs)
    return NextAction(**fields)


class FakeLLM:
    """Returns canned objects by schema. NextAction replies come from a script;
    once the script runs out it submits the report."""

    def __init__(self, actions: list[NextAction] | None = None, fail_on: set[str] | None = None):
        self.actions = list(actions or [])
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, str]] = []  # (schema name, user message)
        self.system_prompts: dict[str, str] = {}  # last system prompt seen, by schema name
        self.all_system_prompts: list[tuple[str, str]] = []  # every (schema name, system prompt)
        self.usage = UsageLog()
        self._lock = threading.Lock()  # agents call this from worker threads

    def describe(self) -> str:
        return "fake / scripted"

    def problems(self) -> list[str]:
        return []

    def count(self, schema_name: str) -> int:
        return sum(1 for name, _ in self.calls if name == schema_name)

    def complete(self, system_prompt, user_message, fast=False) -> str:
        with self._lock:
            self.calls.append(("text", user_message))
        return "comparison text"

    def complete_structured(self, system_prompt, user_message, schema, fast=False):
        name = schema.__name__
        with self._lock:
            self.calls.append((name, user_message))
            self.system_prompts[name] = system_prompt
            self.all_system_prompts.append((name, system_prompt))
            scripted = self.actions.pop(0) if schema is NextAction and self.actions else None
        if name in self.fail_on:
            raise LLMError(f"scripted failure for {name}")
        if schema is NextAction:
            return scripted or action("submit_report")
        if schema is ParsedScenario:
            return ParsedScenario(
                description="The Fed raises its policy rate by 50 basis points.",
                policy_type="monetary", magnitude="moderate", is_surprise=True,
                baseline_conditions="Inflation above target", time_horizon="12 months",
            )
        if schema is AnalysisOutput:
            return AnalysisOutput(
                immediate_impact="original immediate", short_term_impact="original short",
                medium_term_impact="original medium", cross_domain_effects="original cross",
                confidence="moderate",
            )
        if schema is DeliberationOutput:
            return DeliberationOutput(
                agreements="some", disagreements="None",
                revised_immediate_impact="revised immediate",
                revised_medium_term_impact="revised medium",
                revised_cross_domain_effects="revised cross", confidence="high",
            )
        if schema is DebateArgument:
            return DebateArgument(position_summary="position", key_arguments="arguments",
                                  rebuttal="rebuttal", magnitude_estimate="moderate")
        if schema is DebateVerdict:
            return DebateVerdict(winner="mixed", reasoning="balanced",
                                 calibrated_impact="moderate", key_uncertainties="path of inflation")
        if schema is ReportDraft:
            return ReportDraft(
                scenario_summary="summary", immediate_reaction="now", short_term_transmission="soon",
                medium_term_outlook="later", cross_domain_map="map",
                scenario_matrix=[
                    ScenarioOutcome(label="Base case", probability="60%", key_driver="d", market_impact="m"),
                    ScenarioOutcome(label="Bull case", probability="20%", key_driver="d", market_impact="m"),
                    ScenarioOutcome(label="Bear case", probability="20%", key_driver="d", market_impact="m"),
                ],
                risk_factors="risks", synthesis="takeaways",
            )
        raise AssertionError(f"FakeLLM has no canned reply for {name}")
