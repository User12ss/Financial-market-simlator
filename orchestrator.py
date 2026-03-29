"""Orchestrator — coordinates agents and synthesizes the simulation report."""

from __future__ import annotations
import json
import concurrent.futures
from models.scenario import Scenario
from models.report import AgentAnalysis, SimulationReport, ScenarioOutcome
from agents import ALL_AGENTS
from agents.base import BaseAgent
from utils.llm_client import LLMClient
from config import BASE_ECONOMY


class Orchestrator:
    """Main entry point: parses scenarios, runs agents, builds reports."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.llm = LLMClient(api_key=api_key, model=model)
        self.agents: list[BaseAgent] = [AgentCls(self.llm) for AgentCls in ALL_AGENTS]

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
        """Run a full simulation from raw user input to final report."""

        # Phase 0 — Parse the scenario
        scenario = self._parse_scenario(user_input, base_economy)

        # Phase 1 — Independent agent analyses (can run in parallel)
        analyses = self._run_agents(scenario, parallel, on_agent_start, on_agent_done)

        # Phase 2 — Cross-domain synthesis
        report = self._synthesize(scenario, analyses)

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
    # Phase 0: Scenario parsing
    # ------------------------------------------------------------------

    def _parse_scenario(self, user_input: str, base_economy: str) -> Scenario:
        """Use LLM to parse raw input into a structured scenario."""
        system = """You are a financial scenario parser. Given a user's plain-language description of a policy scenario, extract structured information.

Respond ONLY with valid JSON, no markdown, no preamble:
{
    "description": "Restate the policy in precise economic terms (1-3 sentences)",
    "policy_type": "monetary | fiscal | trade | regulatory | supply_shock | mixed",
    "magnitude": "mild | moderate | severe",
    "is_surprise": true or false,
    "baseline_conditions": "Brief description of assumed starting macro conditions",
    "time_horizon": "The relevant time horizon for analysis"
}"""

        data = self.llm.complete_json(system, f"Parse this scenario:\n{user_input}")

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
    # Phase 1: Independent agent runs
    # ------------------------------------------------------------------

    def _run_agents(
        self,
        scenario: Scenario,
        parallel: bool,
        on_start: callable = None,
        on_done: callable = None,
    ) -> list[AgentAnalysis]:
        """Run all agents on the scenario."""

        def _run_one(agent: BaseAgent) -> AgentAnalysis:
            if on_start:
                on_start(agent.name)
            result = agent.analyze(scenario)
            if on_done:
                on_done(agent.name)
            return result

        if parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
                futures = {pool.submit(_run_one, a): a for a in self.agents}
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
            for agent in self.agents:
                try:
                    results.append(_run_one(agent))
                except Exception as e:
                    print(f"  WARNING: {agent.name} failed: {e}")
            return results

    # ------------------------------------------------------------------
    # Phase 2: Synthesis
    # ------------------------------------------------------------------

    def _synthesize(self, scenario: Scenario, analyses: list[AgentAnalysis]) -> SimulationReport:
        """Use LLM to merge all agent outputs into a cohesive report."""

        agent_summaries = "\n\n".join(
            f"### {a.agent_name}\n"
            f"Immediate: {a.immediate_impact}\n"
            f"Short-term: {a.short_term_impact}\n"
            f"Medium-term: {a.medium_term_impact}\n"
            f"Cross-domain: {a.cross_domain_effects}\n"
            f"Confidence: {a.confidence}"
            for a in analyses
        )

        system = """You are the synthesis engine of a multi-agent financial market simulation. You receive analyses from 7 specialized agents and must merge them into a unified, coherent report.

Your job:
1. Resolve any contradictions between agents by weighing confidence levels and economic logic.
2. Identify the causal chains that connect agent outputs across domains.
3. Build a probability-weighted scenario matrix.
4. Highlight the key risk factors and assumptions.

Respond ONLY with valid JSON, no markdown, no preamble:
{
    "scenario_summary": "2-3 sentence precise restatement of the policy scenario",
    "immediate_reaction": "Unified 0-1 week market reaction across all domains (1 paragraph)",
    "short_term_transmission": "1-3 month economic transmission effects (1 paragraph)",
    "medium_term_outlook": "3-12 month structural outlook (1 paragraph)",
    "cross_domain_map": "Describe the causal chain: Agent A output → Agent B input → Agent C impact (1 paragraph with arrows)",
    "scenario_matrix": [
        {"label": "Base case", "probability": "X%", "key_driver": "...", "market_impact": "..."},
        {"label": "Bull case", "probability": "X%", "key_driver": "...", "market_impact": "..."},
        {"label": "Bear case", "probability": "X%", "key_driver": "...", "market_impact": "..."}
    ],
    "risk_factors": "Key risks and assumptions that could invalidate this simulation (1 paragraph)",
    "synthesis": "2-3 sentence high-level takeaway tying everything together"
}"""

        user_msg = f"""SCENARIO: {scenario.description}
ECONOMY: {scenario.base_economy}
MAGNITUDE: {scenario.magnitude}
SURPRISE: {'Yes' if scenario.is_surprise else 'No (priced in)'}

AGENT ANALYSES:
{agent_summaries}"""

        data = self.llm.complete_json(system, user_msg, max_tokens=4096)

        matrix = [
            ScenarioOutcome(**s) for s in data.get("scenario_matrix", [])
        ]

        return SimulationReport(
            scenario_summary=data.get("scenario_summary", scenario.description),
            agent_analyses=analyses,
            immediate_reaction=data.get("immediate_reaction", ""),
            short_term_transmission=data.get("short_term_transmission", ""),
            medium_term_outlook=data.get("medium_term_outlook", ""),
            cross_domain_map=data.get("cross_domain_map", ""),
            scenario_matrix=matrix,
            risk_factors=data.get("risk_factors", ""),
            synthesis=data.get("synthesis", ""),
        )

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

        return self.llm.complete(system, user_msg, max_tokens=4096, temperature=0.4)
