"""Orchestrator — tiered execution, selective activation, and two-pass synthesis."""

from __future__ import annotations
import concurrent.futures
from models.scenario import Scenario
from models.report import AgentAnalysis, SimulationReport, ScenarioOutcome
from agents import ALL_AGENTS
from agents.base import BaseAgent
from utils.llm_client import LLMClient
from config import BASE_ECONOMY, AGENT_ROUTING, TOKEN_BUDGET

# --- Tool schemas for structured output ---

SCENARIO_PARSE_TOOL: dict = {
    "name": "parse_scenario",
    "description": "Parse the user's policy scenario into structured fields.",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Restate the policy in precise economic terms (1-3 sentences)"},
            "policy_type": {"type": "string", "enum": ["monetary", "fiscal", "trade", "regulatory", "supply_shock", "mixed"]},
            "magnitude": {"type": "string", "enum": ["mild", "moderate", "severe"]},
            "is_surprise": {"type": "boolean", "description": "Whether this is unexpected by markets"},
            "baseline_conditions": {"type": "string", "description": "Brief assumed starting macro conditions"},
            "time_horizon": {"type": "string", "description": "Relevant time horizon for analysis"},
        },
        "required": ["description", "policy_type", "magnitude", "is_surprise", "baseline_conditions", "time_horizon"],
    },
}

SYNTHESIS_TOOL: dict = {
    "name": "submit_synthesis",
    "description": "Submit the unified cross-domain synthesis report.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scenario_summary": {"type": "string", "description": "2-3 sentence precise restatement"},
            "immediate_reaction": {"type": "string", "description": "Unified 0-1 week reaction (1 paragraph)"},
            "short_term_transmission": {"type": "string", "description": "1-3 month transmission effects (1 paragraph)"},
            "medium_term_outlook": {"type": "string", "description": "3-12 month structural outlook (1 paragraph)"},
            "cross_domain_map": {"type": "string", "description": "Causal chain with arrows: Agent A → Agent B → Agent C"},
            "scenario_matrix": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "probability": {"type": "string"},
                        "key_driver": {"type": "string"},
                        "market_impact": {"type": "string"},
                    },
                    "required": ["label", "probability", "key_driver", "market_impact"],
                },
                "description": "Base / Bull / Bear case matrix",
            },
            "risk_factors": {"type": "string", "description": "Key risks and assumptions (1 paragraph)"},
            "synthesis": {"type": "string", "description": "2-3 sentence high-level takeaway"},
        },
        "required": [
            "scenario_summary", "immediate_reaction", "short_term_transmission",
            "medium_term_outlook", "cross_domain_map", "scenario_matrix",
            "risk_factors", "synthesis",
        ],
    },
}

CRITIC_TOOL: dict = {
    "name": "submit_critique",
    "description": "Submit your critique of the draft synthesis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contradictions": {"type": "string", "description": "Any logical contradictions found between agent analyses or in the synthesis"},
            "missing_links": {"type": "string", "description": "Causal chains that were missed or under-explored"},
            "unsupported_claims": {"type": "string", "description": "Claims in the synthesis not backed by agent analyses"},
            "revised_synthesis": {"type": "string", "description": "Corrected 2-3 sentence takeaway incorporating your critique"},
            "revised_risk_factors": {"type": "string", "description": "Updated risk factors if any were missed"},
            "confidence_adjustment": {"type": "string", "enum": ["raise", "maintain", "lower"], "description": "Whether overall confidence should change"},
        },
        "required": ["contradictions", "missing_links", "unsupported_claims", "revised_synthesis", "revised_risk_factors", "confidence_adjustment"],
    },
}


class Orchestrator:
    """Main entry point: parses scenarios, runs tiered agents, builds reports."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.llm = LLMClient(api_key=api_key, model=model)
        self.agents: dict[str, BaseAgent] = {
            AgentCls.id: AgentCls(self.llm) for AgentCls in ALL_AGENTS
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        user_input: str,
        base_economy: str = BASE_ECONOMY,
        parallel: bool = True,
        on_agent_start: callable = None,
        on_agent_done: callable = None,
    ) -> SimulationReport:
        """Run a full simulation: parse → route → tier1 → tier2 → synthesize → critique."""

        # Phase 0 — Parse scenario (Haiku — cheap structured extraction)
        scenario = self._parse_scenario(user_input, base_economy)

        # Phase 0.5 — Route agents by policy type
        tier1_agents, tier2_agents = self._route_agents(scenario)

        # Phase 1 — Tier 1: upstream agents (no context, parallel)
        tier1_results = self._run_agent_batch(
            tier1_agents, scenario, tier="tier1",
            parallel=parallel, on_start=on_agent_start, on_done=on_agent_done,
        )

        # Build context summary from tier 1 for downstream agents
        tier1_context = self._build_tier1_context(tier1_results)

        # Phase 2 — Tier 2: downstream agents (with tier 1 context, parallel)
        tier2_results = self._run_agent_batch(
            tier2_agents, scenario, tier="tier2",
            parallel=parallel, on_start=on_agent_start, on_done=on_agent_done,
            context=tier1_context,
        )

        all_analyses = tier1_results + tier2_results

        # Phase 3 — Two-pass synthesis: draft then critique
        report = self._synthesize_with_critic(scenario, all_analyses)

        return report

    def compare(
        self,
        input_a: str,
        input_b: str,
        base_economy: str = BASE_ECONOMY,
    ) -> str:
        """Run two scenarios and produce a comparative analysis."""
        report_a = self.run(input_a, base_economy, parallel=True)
        report_b = self.run(input_b, base_economy, parallel=True)
        return self._compare_reports(report_a, report_b)

    # ------------------------------------------------------------------
    # Phase 0: Scenario parsing (Haiku — model tiering)
    # ------------------------------------------------------------------

    def _parse_scenario(self, user_input: str, base_economy: str) -> Scenario:
        """Use fast model to parse raw input into a structured scenario."""
        system = """You are a financial scenario parser. Given a user's plain-language description of a policy scenario, extract structured information. Use the provided tool to submit your parse."""

        data = self.llm.complete_tool(
            system_prompt=system,
            user_message=f"Parse this scenario:\n{user_input}",
            tool_schema=SCENARIO_PARSE_TOOL,
            max_tokens=TOKEN_BUDGET["scenario_parse"],
            temperature=0.2,
            cache=True,
            use_fast_model=True,
        )

        return Scenario(
            raw_input=user_input,
            base_economy=base_economy,
            description=data.get("description", user_input),
            policy_type=data.get("policy_type", "mixed"),
            magnitude=data.get("magnitude", "moderate"),
            is_surprise=data.get("is_surprise", True),
            baseline_conditions=data.get("baseline_conditions", "Current macro environment"),
            time_horizon=data.get("time_horizon", "12 months"),
        )

    # ------------------------------------------------------------------
    # Phase 0.5: Agent routing (selective activation)
    # ------------------------------------------------------------------

    def _route_agents(self, scenario: Scenario) -> tuple[list[BaseAgent], list[BaseAgent]]:
        """Route agents into tiers based on policy type. Skip irrelevant agents."""
        policy = scenario.policy_type.value if hasattr(scenario.policy_type, 'value') else str(scenario.policy_type)
        routing = AGENT_ROUTING.get(policy, AGENT_ROUTING["mixed"])

        tier1_ids = routing["tier1"]
        tier2_ids = routing["tier2"]
        skip_ids = set(routing.get("skip", []))

        tier1 = [self.agents[aid] for aid in tier1_ids if aid in self.agents and aid not in skip_ids]
        tier2 = [self.agents[aid] for aid in tier2_ids if aid in self.agents and aid not in skip_ids]

        return tier1, tier2

    # ------------------------------------------------------------------
    # Phase 1 & 2: Tiered agent execution with context passing
    # ------------------------------------------------------------------

    def _run_agent_batch(
        self,
        agents: list[BaseAgent],
        scenario: Scenario,
        tier: str,
        parallel: bool,
        on_start: callable = None,
        on_done: callable = None,
        context: str = "",
    ) -> list[AgentAnalysis]:
        """Run a batch of agents, optionally in parallel, with shared context."""

        def _run_one(agent: BaseAgent) -> AgentAnalysis:
            if on_start:
                on_start(agent.name)
            result = agent.analyze(scenario, context=context, tier=tier)
            if on_done:
                on_done(agent.name)
            return result

        if parallel and len(agents) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
                futures = {pool.submit(_run_one, a): a for a in agents}
                results = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        agent = futures[future]
                        print(f"  WARNING: {agent.name} failed: {e}")
                return results
        else:
            results = []
            for agent in agents:
                try:
                    results.append(_run_one(agent))
                except Exception as e:
                    print(f"  WARNING: {agent.name} failed: {e}")
            return results

    def _build_tier1_context(self, analyses: list[AgentAnalysis]) -> str:
        """Compress tier 1 outputs into a context string for tier 2 agents."""
        lines = []
        for a in analyses:
            lines.append(f"[{a.agent_name} | Confidence: {a.confidence}]")
            lines.append(f"  Immediate: {a.immediate_impact}")
            lines.append(f"  Cross-domain: {a.cross_domain_effects}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Phase 3: Two-pass synthesis with critic (Optimization 4)
    # ------------------------------------------------------------------

    def _synthesize_with_critic(
        self,
        scenario: Scenario,
        analyses: list[AgentAnalysis],
    ) -> SimulationReport:
        """Draft synthesis, then run a critic pass to catch errors."""

        # --- Draft pass ---
        agent_summaries = self._compress_for_synthesis(analyses)

        draft_system = """You are the synthesis engine of a multi-agent financial market simulation. You receive analyses from specialized agents and must merge them into a unified, coherent report.

Your job:
1. Resolve any contradictions between agents by weighing confidence levels and economic logic.
2. Identify the causal chains that connect agent outputs across domains.
3. Build a probability-weighted scenario matrix (base/bull/bear).
4. Highlight the key risk factors and assumptions.

Submit your synthesis using the provided tool."""

        draft_user = f"""SCENARIO: {scenario.description}
ECONOMY: {scenario.base_economy}
MAGNITUDE: {scenario.magnitude}
SURPRISE: {'Yes' if scenario.is_surprise else 'No (priced in)'}

AGENT ANALYSES:
{agent_summaries}"""

        draft = self.llm.complete_tool(
            system_prompt=draft_system,
            user_message=draft_user,
            tool_schema=SYNTHESIS_TOOL,
            max_tokens=TOKEN_BUDGET["synthesis_draft"],
            temperature=0.3,
            cache=True,
        )

        # --- Critic pass (cheaper — reviewing, not generating) ---
        critic_system = """You are a financial analysis critic. You review a draft synthesis report produced by a multi-agent simulation system. Your job is to find:
1. Logical contradictions between agents that weren't resolved
2. Missing causal links in the cross-domain chain
3. Claims in the synthesis not supported by the agent analyses
4. Risk factors that were overlooked

Be precise and specific. If the draft is solid, say so — do not fabricate issues."""

        critic_user = f"""AGENT ANALYSES:
{agent_summaries}

DRAFT SYNTHESIS:
Summary: {draft.get('scenario_summary', '')}
Immediate: {draft.get('immediate_reaction', '')}
Short-term: {draft.get('short_term_transmission', '')}
Medium-term: {draft.get('medium_term_outlook', '')}
Cross-domain map: {draft.get('cross_domain_map', '')}
Risk factors: {draft.get('risk_factors', '')}
Synthesis: {draft.get('synthesis', '')}"""

        critique = self.llm.complete_tool(
            system_prompt=critic_system,
            user_message=critic_user,
            tool_schema=CRITIC_TOOL,
            max_tokens=TOKEN_BUDGET["synthesis_critic"],
            temperature=0.2,
            cache=True,
            use_fast_model=True,
        )

        # --- Merge draft + critique into final report ---
        final_synthesis = critique.get("revised_synthesis") or draft.get("synthesis", "")
        final_risks = critique.get("revised_risk_factors") or draft.get("risk_factors", "")

        contradictions = critique.get("contradictions", "")
        missing_links = critique.get("missing_links", "")
        if contradictions and contradictions.lower() not in ("none", "none found", "n/a", ""):
            final_risks += f"\n\nContradictions noted: {contradictions}"
        if missing_links and missing_links.lower() not in ("none", "none found", "n/a", ""):
            final_risks += f"\n\nMissing links noted: {missing_links}"

        matrix = [ScenarioOutcome(**s) for s in draft.get("scenario_matrix", [])]

        return SimulationReport(
            scenario_summary=draft.get("scenario_summary", scenario.description),
            agent_analyses=analyses,
            immediate_reaction=draft.get("immediate_reaction", ""),
            short_term_transmission=draft.get("short_term_transmission", ""),
            medium_term_outlook=draft.get("medium_term_outlook", ""),
            cross_domain_map=draft.get("cross_domain_map", ""),
            scenario_matrix=matrix,
            risk_factors=final_risks,
            synthesis=final_synthesis,
        )

    def _compress_for_synthesis(self, analyses: list[AgentAnalysis]) -> str:
        """Format agent outputs efficiently for the synthesis prompt."""
        lines = []
        for a in analyses:
            lines.append(f"### {a.agent_name} [Confidence: {a.confidence}]")
            lines.append(f"- Immediate: {a.immediate_impact}")
            lines.append(f"- Short-term: {a.short_term_impact}")
            lines.append(f"- Medium-term: {a.medium_term_impact}")
            lines.append(f"- Spillovers: {a.cross_domain_effects}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def _compare_reports(self, report_a: SimulationReport, report_b: SimulationReport) -> str:
        """Use LLM to produce a comparative analysis of two simulation reports."""
        system = """You are a financial market analyst comparing two policy simulation reports side-by-side.
Produce a clear comparative analysis highlighting:
1. Where the two scenarios diverge most sharply
2. Which market domains are affected differently
3. Which scenario carries more risk
4. The key decision points that would favor one path over the other

Write in clear prose with section headers. Frame as model analysis, not financial advice."""

        user_msg = f"""SCENARIO A:
{report_a.to_markdown()}

---

SCENARIO B:
{report_b.to_markdown()}"""

        return self.llm.complete(
            system, user_msg,
            max_tokens=TOKEN_BUDGET["comparison"],
            temperature=0.4,
            cache=True,
        )
