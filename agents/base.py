"""Base agent class — all specialized agents inherit from this."""

from __future__ import annotations
from abc import ABC, abstractmethod
from models.scenario import Scenario
from models.report import AgentAnalysis
from utils.llm_client import LLMClient
from config import TOKEN_BUDGET

ANALYSIS_TOOL: dict = {
    "name": "submit_analysis",
    "description": "Submit your domain analysis for this policy scenario.",
    "input_schema": {
        "type": "object",
        "properties": {
            "immediate_impact": {
                "type": "string",
                "description": "0-1 week reaction analysis (2-4 sentences)",
            },
            "short_term_impact": {
                "type": "string",
                "description": "1-3 month transmission analysis (2-4 sentences)",
            },
            "medium_term_impact": {
                "type": "string",
                "description": "3-12 month structural outlook (2-4 sentences)",
            },
            "cross_domain_effects": {
                "type": "string",
                "description": "How your domain's changes spill into other market domains (2-4 sentences)",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "moderate", "high"],
                "description": "Your confidence level in this analysis",
            },
        },
        "required": [
            "immediate_impact",
            "short_term_impact",
            "medium_term_impact",
            "cross_domain_effects",
            "confidence",
        ],
    },
}

DELIBERATION_TOOL: dict = {
    "name": "submit_revised_analysis",
    "description": "Submit your revised analysis after reviewing other agents' positions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agreements": {
                "type": "string",
                "description": "Which other agents' conclusions align with yours and reinforce your analysis (1-2 sentences)",
            },
            "disagreements": {
                "type": "string",
                "description": "Where you disagree with other agents and why your domain expertise leads to a different conclusion (1-2 sentences). Say 'None' if no disagreements.",
            },
            "revised_immediate_impact": {
                "type": "string",
                "description": "Your updated 0-1 week analysis incorporating other agents' signals (2-4 sentences)",
            },
            "revised_medium_term_impact": {
                "type": "string",
                "description": "Your updated 3-12 month analysis incorporating other agents' signals (2-4 sentences)",
            },
            "revised_cross_domain_effects": {
                "type": "string",
                "description": "Updated spillover effects now that you see the full picture (2-4 sentences)",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "moderate", "high"],
            },
        },
        "required": [
            "agreements",
            "disagreements",
            "revised_immediate_impact",
            "revised_medium_term_impact",
            "revised_cross_domain_effects",
            "confidence",
        ],
    },
}


class BaseAgent(ABC):
    """Abstract base for all financial market simulation agents."""

    id: str = ""
    name: str = ""
    domain: str = ""
    outputs: str = ""
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
- Use real economic frameworks as reasoning foundations — do NOT name-drop them as labels.
- Distinguish between the announcement effect (immediate) and economic transmission (lagged).
- Identify how your domain's changes will spill over into other agents' domains.
- Frame all outputs as model suggestions, never as financial advice.
- If the scenario has limited relevance to your domain, say so briefly and focus on the indirect effects.

Submit your analysis using the provided tool."""

    def analyze(
        self,
        scenario: Scenario,
        context: str = "",
        tier: str = "tier1",
    ) -> AgentAnalysis:
        """Run this agent's analysis on a scenario with tier-aware token budgeting."""
        user_msg = f"""POLICY SCENARIO:
{scenario.description or scenario.raw_input}

BASELINE CONDITIONS: {scenario.baseline_conditions or 'Current macro environment'}
ECONOMY: {scenario.base_economy}
SURPRISE FACTOR: {'Unexpected by markets' if scenario.is_surprise else 'Largely priced in'}
MAGNITUDE: {scenario.magnitude}
"""
        if context:
            user_msg += f"\nUPSTREAM AGENT SIGNALS (use these as inputs to your analysis — do not contradict unless you have strong domain-specific reasons):\n{context}\n"

        if tier == "tier2":
            user_msg += "\nYou are running as a DOWNSTREAM agent. The upstream signals above represent the primary policy transmission. Focus your analysis on how these signals propagate into YOUR domain specifically.\n"

        max_tokens = TOKEN_BUDGET.get(tier, 2048)

        data = self.llm.complete_tool(
            system_prompt=self.system_prompt,
            user_message=user_msg,
            tool_schema=ANALYSIS_TOOL,
            max_tokens=max_tokens,
            temperature=0.3,
            cache=True,
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

    def deliberate(
        self,
        scenario: Scenario,
        own_analysis: AgentAnalysis,
        all_analyses_context: str,
    ) -> AgentAnalysis:
        """Review other agents' analyses and revise own position where warranted."""
        system = f"""You are the {self.name}. You already submitted your initial analysis for a policy scenario. Now you can see what ALL other agents concluded.

YOUR DOMAIN: {self.domain}

Your task:
1. Read every other agent's analysis carefully.
2. Identify where their conclusions REINFORCE yours (agreement strengthens confidence).
3. Identify where their conclusions CONTRADICT yours — and decide whether to revise.
   - If another agent's domain expertise reveals something you missed, UPDATE your analysis.
   - If you have strong domain-specific reasons to hold your position, KEEP it and explain why.
4. Look for EMERGENT effects: causal chains that only become visible when you see the full picture
   (e.g., central bank tightening + fiscal expansion + commodity shock create a specific pattern
   that no single agent would predict alone).

Do NOT simply agree with everyone. Defend your domain expertise where it matters.
Submit your revised analysis using the provided tool."""

        user_msg = f"""SCENARIO: {scenario.description or scenario.raw_input}
ECONOMY: {scenario.base_economy}
MAGNITUDE: {scenario.magnitude}

YOUR INITIAL ANALYSIS:
- Immediate: {own_analysis.immediate_impact}
- Short-term: {own_analysis.short_term_impact}
- Medium-term: {own_analysis.medium_term_impact}
- Cross-domain: {own_analysis.cross_domain_effects}
- Confidence: {own_analysis.confidence}

ALL OTHER AGENTS' ANALYSES:
{all_analyses_context}"""

        data = self.llm.complete_tool(
            system_prompt=system,
            user_message=user_msg,
            tool_schema=DELIBERATION_TOOL,
            max_tokens=TOKEN_BUDGET.get("deliberation", 1536),
            temperature=0.3,
            cache=True,
        )

        return AgentAnalysis(
            agent_id=self.id,
            agent_name=self.name,
            domain=self.domain,
            immediate_impact=data.get("revised_immediate_impact", own_analysis.immediate_impact),
            short_term_impact=own_analysis.short_term_impact,
            medium_term_impact=data.get("revised_medium_term_impact", own_analysis.medium_term_impact),
            cross_domain_effects=data.get("revised_cross_domain_effects", own_analysis.cross_domain_effects),
            confidence=data.get("confidence", own_analysis.confidence),
        )
