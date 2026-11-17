"""Structured output against real models. Skipped by default.

    RUN_LIVE=1 pytest -m live            local Ollama, free
    RUN_LIVE_ANTHROPIC=1 pytest -m live  Claude, costs a fraction of a cent
"""

import os

import pytest

import config
from agents.base import AnalysisOutput
from utils.llm_client import LLMClient

pytestmark = pytest.mark.live

SYSTEM = "You are an equity market analyst."
QUESTION = "The Fed unexpectedly raises rates by 50bps. Analyze the impact on US equities."


def assert_is_an_analysis(result: AnalysisOutput) -> None:
    assert result.confidence in ("low", "moderate", "high")
    for text in (result.immediate_impact, result.short_term_impact,
                 result.medium_term_impact, result.cross_domain_effects):
        assert len(text) > 20


@pytest.mark.skipif(os.getenv("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to call local Ollama")
@pytest.mark.parametrize("fast", [False, True], ids=["main", "fast"])
def test_ollama_structured_output(fast):
    client = LLMClient(provider="ollama")
    problems = client.problems()
    if problems:
        pytest.skip("; ".join(problems))
    assert_is_an_analysis(client.complete_structured(SYSTEM, QUESTION, AnalysisOutput, fast=fast))


@pytest.mark.skipif(os.getenv("RUN_LIVE_ANTHROPIC") != "1", reason="set RUN_LIVE_ANTHROPIC=1 (costs money)")
def test_claude_structured_output():
    if not config.ANTHROPIC_API_KEY:
        pytest.skip("ANTHROPIC_API_KEY is not set")
    client = LLMClient(provider="anthropic")
    assert_is_an_analysis(client.complete_structured(SYSTEM, QUESTION, AnalysisOutput, fast=True))
