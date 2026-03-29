"""Base agent class — all specialized agents inherit from this."""

from __future__ import annotations
from abc import ABC, abstractmethod
from models.scenario import Scenario
from models.report import AgentAnalysis
from utils.llm_client import LLMClient


class BaseAgent(ABC):
    """Abstract base for all financial market simulation agents."""

    id: str = ""
    name: str = ""
    domain: str = ""
    outputs: str = ""

    # Override in subclasses to inject domain-specific reasoning guidance
    domain_prompt: str = ""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    @property
    def system_prompt(self) -> str:
        return f"""You are the {self.name} within a multi-agent financial market simulation system.

YOUR DOMAIN: {self.domain}
YOUR EXPECTED OUTPUTS: {self.outputs}

{self.domain_prompt}

RULES:
- Analyze the given policy scenario ONLY through the lens of your domain.
- Provide directional analysis with magnitude estimates (mild/moderate/severe), NOT specific price predictions.
- Use real economic frameworks (IS-LM, Phillips Curve, Taylor Rule, Mundell-Fleming, behavioral finance) as reasoning foundations — do NOT name-drop them as labels.
- Distinguish between the announcement effect (immediate) and economic transmission (lagged).
- Identify how your domain's changes will spill over into other agents' domains.
- Frame all outputs as model suggestions, never as financial advice.
- If the scenario has limited relevance to your domain, say so briefly and focus on the indirect effects.

OUTPUT FORMAT — respond ONLY with valid JSON, no markdown fences, no preamble:
{{
    "immediate_impact": "Your 0-1 week analysis (2-4 sentences)",
    "short_term_impact": "Your 1-3 month analysis (2-4 sentences)",
    "medium_term_impact": "Your 3-12 month analysis (2-4 sentences)",
    "cross_domain_effects": "How your domain's changes feed into other market domains (2-4 sentences)",
    "confidence": "low | moderate | high"
}}"""

    def analyze(self, scenario: Scenario, context: str = "") -> AgentAnalysis:
        """Run this agent's analysis on a scenario."""
        user_msg = f"""POLICY SCENARIO:
{scenario.description or scenario.raw_input}

BASELINE CONDITIONS: {scenario.baseline_conditions or 'Current macro environment'}
ECONOMY: {scenario.base_economy}
SURPRISE FACTOR: {'Unexpected by markets' if scenario.is_surprise else 'Largely priced in'}
MAGNITUDE: {scenario.magnitude}
"""
        if context:
            user_msg += f"\nCONTEXT FROM OTHER AGENTS:\n{context}\n"

        data = self.llm.complete_json(
            system_prompt=self.system_prompt,
            user_message=user_msg,
            temperature=0.3,
        )

        return AgentAnalysis(
            agent_id=self.id,
            agent_name=self.name,
            domain=self.domain,
            immediate_impact=data.get("immediate_impact", ""),
            short_term_impact=data.get("short_term_impact", ""),
            medium_term_impact=data.get("medium_term_impact", ""),
            cross_domain_effects=data.get("cross_domain_effects", ""),
            confidence=data.get("confidence", "moderate"),
        )
