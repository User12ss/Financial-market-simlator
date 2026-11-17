"""Base agent class — all specialized agents inherit from this."""

from __future__ import annotations
from abc import ABC
from typing import Literal

from pydantic import BaseModel, Field

from data.render import DATA_ONLY_RULE, GROUNDING_RULE
from models.scenario import Scenario
from models.report import AgentAnalysis
from models.signals import DomainAssessmentDraft
from utils.llm_client import LLMClient


# Shown whenever an agent can see other agents' text. Without it a weak model
# pastes their sentences into its own analysis, and the independent views the
# system depends on collapse into one. The orchestrator also checks for this.
OWN_WORDS_RULE = (
    "Write in your own words and stay inside your own domain. You may refer to what another agent "
    "concluded, but never copy their sentences: say what their conclusion means for YOUR domain."
)


class AnalysisOutput(BaseModel):
    """What an agent returns for its first-pass analysis."""

    immediate_impact: str = Field(description="0-1 week reaction analysis (2-4 sentences)")
    short_term_impact: str = Field(description="1-3 month transmission analysis (2-4 sentences)")
    medium_term_impact: str = Field(description="3-12 month structural outlook (2-4 sentences)")
    cross_domain_effects: str = Field(
        description="How your domain's changes spill into other market domains (2-4 sentences)"
    )
    confidence: Literal["low", "moderate", "high"] = Field(description="Your confidence in this analysis")


class DeliberationOutput(BaseModel):
    """What an agent returns after reading the other agents' analyses."""

    agreements: str = Field(
        description="Which other agents' conclusions align with yours and reinforce your analysis (1-2 sentences)"
    )
    disagreements: str = Field(
        description="Where you disagree with other agents and why your domain expertise leads to a "
        "different conclusion (1-2 sentences). Say 'None' if no disagreements."
    )
    revised_immediate_impact: str = Field(
        description="Your updated 0-1 week analysis incorporating other agents' signals (2-4 sentences)"
    )
    revised_medium_term_impact: str = Field(
        description="Your updated 3-12 month analysis incorporating other agents' signals (2-4 sentences)"
    )
    revised_cross_domain_effects: str = Field(
        description="Updated spillover effects now that you see the full picture (2-4 sentences)"
    )
    confidence: Literal["low", "moderate", "high"] = Field(description="Your confidence after deliberation")


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
- Give direction and magnitude (mild/moderate/severe). Do NOT predict specific price levels.
- Any number you state must come from the scenario or from a DATA SNAPSHOT you are shown. Never invent figures.
- Use real economic frameworks as reasoning foundations — do NOT name-drop them as labels.
- Distinguish between the announcement effect (immediate) and economic transmission (lagged).
- Identify how your domain's changes will spill over into other agents' domains.
- Frame all outputs as model suggestions, never as financial advice.
- If the scenario has limited relevance to your domain, say so briefly and focus on the indirect effects.

Give your analysis in the requested structured fields."""

    def analyze(
        self,
        scenario: Scenario,
        context: str = "",
        tier: str = "tier1",
        data: str = "",
    ) -> AgentAnalysis:
        """Run this agent's analysis on a scenario.

        `context` is what other agents concluded. `data` is the rendered data
        snapshot. They are kept apart so a model never mistakes market data
        for another agent's opinion, or the other way round.
        """
        user_msg = f"""POLICY SCENARIO:
{scenario.description or scenario.raw_input}

BASELINE CONDITIONS: {scenario.baseline_conditions or 'Current macro environment'}
ECONOMY: {scenario.base_economy}
SURPRISE FACTOR: {'Unexpected by markets' if scenario.is_surprise else 'Largely priced in'}
MAGNITUDE: {scenario.magnitude}
"""
        if data:
            user_msg += f"\n{data}\n\n{GROUNDING_RULE}\n"
        if context:
            user_msg += f"\nUPSTREAM AGENT SIGNALS (use these as inputs to your analysis — do not contradict unless you have strong domain-specific reasons):\n{context}\n"
            user_msg += f"\n{OWN_WORDS_RULE}\n"

        if tier == "tier2":
            user_msg += "\nYou are running as a DOWNSTREAM agent. The upstream signals above represent the primary policy transmission. Focus your analysis on how these signals propagate into YOUR domain specifically.\n"

        data = self.llm.complete_structured(self.system_prompt, user_msg, AnalysisOutput)

        return AgentAnalysis(
            agent_id=self.id,
            agent_name=self.name,
            domain=self.domain,
            immediate_impact=data.immediate_impact,
            short_term_impact=data.short_term_impact,
            medium_term_impact=data.medium_term_impact,
            cross_domain_effects=data.cross_domain_effects,
            confidence=data.confidence,
        )

    @property
    def current_state_prompt(self) -> str:
        """System prompt for reading the present state of the domain, with no scenario."""
        return f"""You are the {self.name} on the macro team of a US equity swing-trading desk. The desk only buys shares and holds them for days to weeks.

YOUR DOMAIN: {self.domain}

{self.domain_prompt}

The guidance above was written for analysing a policy scenario. There is no scenario today. Apply the same reasoning to the CURRENT state of your domain, as shown in the data.

RULES:
- Judge what the current state of your domain means for US equities over the next one to four weeks.
- Stay inside your domain. Other analysts cover the other domains.
- {DATA_ONLY_RULE}
- confidence is a number from 0 to 1. Use 0.5 when the data in your domain points both ways.
- List a sector tilt only when your domain gives a clear reason for it. An empty list is a normal answer.
- This is a model output for a paper-trading system, not financial advice.

Give your assessment in the requested structured fields."""

    def assess_current(self, data: str) -> DomainAssessmentDraft:
        """Read the current state of this agent's domain from a rendered data snapshot."""
        return self.llm.complete_structured(
            self.current_state_prompt,
            f"{data}\n\nAssess the current state of your domain and what it means for US equities.",
            DomainAssessmentDraft,
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
{OWN_WORDS_RULE}
If nothing the others wrote changes your view of a field, repeat your own earlier text for that field.
Give your revised analysis in the requested structured fields."""

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

        data = self.llm.complete_structured(system, user_msg, DeliberationOutput)

        return AgentAnalysis(
            agent_id=self.id,
            agent_name=self.name,
            domain=self.domain,
            immediate_impact=data.revised_immediate_impact,
            short_term_impact=own_analysis.short_term_impact,
            medium_term_impact=data.revised_medium_term_impact,
            cross_domain_effects=data.revised_cross_domain_effects,
            confidence=data.confidence,
        )
