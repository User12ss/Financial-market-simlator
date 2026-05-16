"""Orchestrator — LangGraph pipeline with conditional routing and revision loop.

Graph structure (conditional edges marked with ?):

    START → parse_scenario → route_agents
        ─?→ run_tier1 → build_context ─?→ run_tier2 → draft_synthesis
        ─?→ run_tier2 (no tier1 context)  ↗
        ─?→ draft_synthesis (no agents)  ↗

    draft_synthesis → critic_review
        ─?→ build_report (accept)   → END
        ─?→ revise_synthesis (fix)  → build_report → END
"""

from __future__ import annotations
from typing import TypedDict, Any
import concurrent.futures

from langgraph.graph import StateGraph, START, END

from models.scenario import Scenario
from models.report import AgentAnalysis, SimulationReport, ScenarioOutcome
from agents import ALL_AGENTS
from agents.base import BaseAgent
from utils.llm_client import LLMClient
from config import BASE_ECONOMY, AGENT_ROUTING, TOKEN_BUDGET


# ── State schema ─────────────────────────────────────────────────────────────

class SimulationState(TypedDict, total=False):
    """Shared state flowing through every node in the graph."""
    user_input: str
    base_economy: str
    parallel: bool
    scenario: Any              # Scenario
    tier1_agent_ids: list[str]
    tier2_agent_ids: list[str]
    tier1_results: list        # list[AgentAnalysis]
    tier2_results: list        # list[AgentAnalysis]
    tier1_context: str
    all_initial_analyses: list # list[AgentAnalysis] — pre-deliberation
    deliberated_analyses: list # list[AgentAnalysis] — post-deliberation
    has_disagreements: bool
    draft: dict
    critique: dict
    was_revised: bool
    report: Any                # SimulationReport


# ── Tool schemas for structured output ───────────────────────────────────────

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
            "contradictions": {"type": "string", "description": "Logical contradictions found between agents or in the synthesis"},
            "missing_links": {"type": "string", "description": "Causal chains that were missed or under-explored"},
            "unsupported_claims": {"type": "string", "description": "Claims not backed by agent analyses"},
            "revised_synthesis": {"type": "string", "description": "Corrected 2-3 sentence takeaway"},
            "revised_risk_factors": {"type": "string", "description": "Updated risk factors if any were missed"},
            "confidence_adjustment": {"type": "string", "enum": ["raise", "maintain", "lower"]},
        },
        "required": ["contradictions", "missing_links", "unsupported_claims", "revised_synthesis", "revised_risk_factors", "confidence_adjustment"],
    },
}


# ── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    """Builds and invokes a LangGraph simulation pipeline with conditional routing."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.llm = LLMClient(api_key=api_key, model=model)
        self.agents: dict[str, BaseAgent] = {
            AgentCls.id: AgentCls(self.llm) for AgentCls in ALL_AGENTS
        }
        self._on_agent_start: callable | None = None
        self._on_agent_done: callable | None = None
        self._graph = self._build_graph()

    # ── Graph construction ───────────────────────────────────────────────

    def _build_graph(self):
        """
        Build the simulation pipeline as a directed graph with conditional edges.

        Conditional routing:
          - route_agents   →? run_tier1 | run_tier2 | draft_synthesis
          - build_context  →? run_tier2 | draft_synthesis
          - critic_review  →? revise_synthesis | build_report
        """
        graph = StateGraph(SimulationState)

        # ── Nodes ──
        graph.add_node("parse_scenario", self._node_parse)
        graph.add_node("route_agents", self._node_route)
        graph.add_node("run_tier1", self._node_tier1)
        graph.add_node("build_context", self._node_build_context)
        graph.add_node("run_tier2", self._node_tier2)
        graph.add_node("check_disagreements", self._node_check_disagreements)
        graph.add_node("deliberate", self._node_deliberate)
        graph.add_node("draft_synthesis", self._node_draft_synthesis)
        graph.add_node("critic_review", self._node_critic)
        graph.add_node("revise_synthesis", self._node_revise)
        graph.add_node("build_report", self._node_build_report)

        # ── Edges ──
        graph.add_edge(START, "parse_scenario")
        graph.add_edge("parse_scenario", "route_agents")

        # Decision 1: After routing, decide which tier to start with
        graph.add_conditional_edges(
            "route_agents",
            self._should_run_tier1,
            {
                "run_tier1": "run_tier1",
                "skip_to_tier2": "run_tier2",
                "skip_all": "check_disagreements",
            },
        )

        graph.add_edge("run_tier1", "build_context")

        # Decision 2: After tier 1 context is built, check if tier 2 is needed
        graph.add_conditional_edges(
            "build_context",
            self._should_run_tier2,
            {
                "run_tier2": "run_tier2",
                "skip_to_check": "check_disagreements",
            },
        )

        graph.add_edge("run_tier2", "check_disagreements")

        # Decision 3: After all agents run, check if deliberation is needed
        graph.add_conditional_edges(
            "check_disagreements",
            self._should_deliberate,
            {
                "deliberate": "deliberate",
                "skip_to_synthesis": "draft_synthesis",
            },
        )

        graph.add_edge("deliberate", "draft_synthesis")
        graph.add_edge("draft_synthesis", "critic_review")

        # Decision 4: After critic, accept or revise
        graph.add_conditional_edges(
            "critic_review",
            self._should_revise,
            {
                "revise": "revise_synthesis",
                "accept": "build_report",
            },
        )

        graph.add_edge("revise_synthesis", "build_report")
        graph.add_edge("build_report", END)

        return graph.compile()

    # ── Conditional edge functions ───────────────────────────────────────

    def _should_run_tier1(self, state: SimulationState) -> str:
        """Decide whether to run tier 1, skip to tier 2, or skip all agents."""
        has_tier1 = len(state.get("tier1_agent_ids", [])) > 0
        has_tier2 = len(state.get("tier2_agent_ids", [])) > 0

        if has_tier1:
            return "run_tier1"
        elif has_tier2:
            return "skip_to_tier2"
        else:
            return "skip_all"

    def _should_run_tier2(self, state: SimulationState) -> str:
        """After tier 1 completes, decide whether tier 2 is needed."""
        if len(state.get("tier2_agent_ids", [])) > 0:
            return "run_tier2"
        return "skip_to_check"

    def _should_deliberate(self, state: SimulationState) -> str:
        """After all agents run, check if their analyses conflict enough to warrant deliberation."""
        if not state.get("has_disagreements", False):
            return "skip_to_synthesis"
        return "deliberate"

    def _should_revise(self, state: SimulationState) -> str:
        """After critic review, decide whether the draft needs revision."""
        critique = state.get("critique", {})
        adjustment = critique.get("confidence_adjustment", "maintain")

        contradictions = critique.get("contradictions", "").strip().lower()
        missing = critique.get("missing_links", "").strip().lower()
        no_issue_markers = {"", "none", "none found", "none found.", "n/a", "no contradictions found", "no contradictions found.", "no missing links", "no missing links found", "no missing links found."}

        has_contradictions = contradictions not in no_issue_markers
        has_missing_links = missing not in no_issue_markers
        confidence_lowered = adjustment == "lower"

        if confidence_lowered or (has_contradictions and has_missing_links):
            return "revise"
        return "accept"

    # ── Public API ───────────────────────────────────────────────────────

    def run(
        self,
        user_input: str,
        base_economy: str = BASE_ECONOMY,
        parallel: bool = True,
        on_agent_start: callable = None,
        on_agent_done: callable = None,
    ) -> SimulationReport:
        """Invoke the full simulation graph from user input to final report."""
        self._on_agent_start = on_agent_start
        self._on_agent_done = on_agent_done

        result = self._graph.invoke({
            "user_input": user_input,
            "base_economy": base_economy,
            "parallel": parallel,
        })

        return result["report"]

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

    # ── Graph nodes ──────────────────────────────────────────────────────

    def _node_parse(self, state: SimulationState) -> dict:
        """Phase 0: Parse raw input into structured Scenario (Haiku)."""
        system = """You are a financial scenario parser. Given a user's plain-language description of a policy scenario, extract structured information. Use the provided tool to submit your parse."""

        data = self.llm.complete_tool(
            system_prompt=system,
            user_message=f"Parse this scenario:\n{state['user_input']}",
            tool_schema=SCENARIO_PARSE_TOOL,
            max_tokens=TOKEN_BUDGET["scenario_parse"],
            temperature=0.2,
            cache=True,
            use_fast_model=True,
        )

        scenario = Scenario(
            raw_input=state["user_input"],
            base_economy=state["base_economy"],
            description=data.get("description", state["user_input"]),
            policy_type=data.get("policy_type", "mixed"),
            magnitude=data.get("magnitude", "moderate"),
            is_surprise=data.get("is_surprise", True),
            baseline_conditions=data.get("baseline_conditions", "Current macro environment"),
            time_horizon=data.get("time_horizon", "12 months"),
        )
        return {"scenario": scenario}

    def _node_route(self, state: SimulationState) -> dict:
        """Phase 0.5: Classify agents into tiers, skip irrelevant ones."""
        scenario: Scenario = state["scenario"]
        policy = scenario.policy_type.value if hasattr(scenario.policy_type, "value") else str(scenario.policy_type)
        routing = AGENT_ROUTING.get(policy, AGENT_ROUTING["mixed"])

        skip = set(routing.get("skip", []))
        tier1 = [aid for aid in routing["tier1"] if aid in self.agents and aid not in skip]
        tier2 = [aid for aid in routing["tier2"] if aid in self.agents and aid not in skip]

        return {"tier1_agent_ids": tier1, "tier2_agent_ids": tier2}

    def _node_tier1(self, state: SimulationState) -> dict:
        """Phase 1: Run upstream agents in parallel — no cross-agent context."""
        agents = [self.agents[aid] for aid in state["tier1_agent_ids"]]
        results = self._run_agent_batch(agents, state["scenario"], "tier1", state.get("parallel", True))
        return {"tier1_results": results}

    def _node_build_context(self, state: SimulationState) -> dict:
        """Compress tier 1 outputs into a context string for downstream agents."""
        lines = []
        for a in state.get("tier1_results", []):
            lines.append(f"[{a.agent_name} | Confidence: {a.confidence}]")
            lines.append(f"  Immediate: {a.immediate_impact}")
            lines.append(f"  Cross-domain: {a.cross_domain_effects}")
            lines.append("")
        return {"tier1_context": "\n".join(lines)}

    def _node_tier2(self, state: SimulationState) -> dict:
        """Phase 2: Run downstream agents with tier 1 context (if available)."""
        agents = [self.agents[aid] for aid in state["tier2_agent_ids"]]
        context = state.get("tier1_context", "")
        results = self._run_agent_batch(
            agents, state["scenario"], "tier2",
            state.get("parallel", True), context=context,
        )
        return {"tier2_results": results}

    def _node_check_disagreements(self, state: SimulationState) -> dict:
        """Check if agents produced conflicting signals that warrant a deliberation round.

        Uses Haiku to quickly scan for contradictions across all agent outputs.
        If agents disagree on direction (e.g., one says risk-on, another says risk-off),
        the graph routes through deliberation so agents can revise.
        """
        all_analyses = state.get("tier1_results", []) + state.get("tier2_results", [])

        if len(all_analyses) < 2:
            return {"all_initial_analyses": all_analyses, "has_disagreements": False}

        summaries = self._compress_for_synthesis(all_analyses)

        system = """You are a conflict detector for a multi-agent financial simulation. Scan the agent analyses and determine if there are MATERIAL disagreements — cases where agents contradict each other on the DIRECTION of impact (not just magnitude).

Examples of material disagreements:
- One agent says "risk-on" while another says "risk-off"
- One agent says "currency strengthens" while another says "capital outflows" (which weakens currency)
- One agent says "yields rise" while another's logic implies they should fall

Minor differences in magnitude or confidence are NOT disagreements.

Respond with ONLY "yes" or "no" — nothing else."""

        response = self.llm.complete(
            system_prompt=system,
            user_message=f"AGENT ANALYSES:\n{summaries}",
            max_tokens=8,
            temperature=0.0,
            cache=True,
            use_fast_model=True,
        )

        has_disagreements = response.strip().lower().startswith("yes")
        return {"all_initial_analyses": all_analyses, "has_disagreements": has_disagreements}

    def _node_deliberate(self, state: SimulationState) -> dict:
        """Deliberation round: each agent sees all others' analyses and can revise.

        This is where real inter-agent interaction happens. Each agent:
        1. Reads every other agent's conclusions
        2. Identifies agreements and disagreements
        3. Revises its own analysis where warranted
        4. Defends its position where its domain expertise justifies it

        Runs in parallel — each agent deliberates independently but simultaneously.
        """
        all_analyses = state["all_initial_analyses"]
        scenario: Scenario = state["scenario"]

        analyses_by_id = {a.agent_id: a for a in all_analyses}
        all_agent_ids = list(analyses_by_id.keys())

        def _deliberate_one(agent_id: str) -> AgentAnalysis:
            agent = self.agents[agent_id]
            own = analyses_by_id[agent_id]

            others = [a for a in all_analyses if a.agent_id != agent_id]
            other_lines = []
            for o in others:
                other_lines.append(f"[{o.agent_name} | Confidence: {o.confidence}]")
                other_lines.append(f"  Immediate: {o.immediate_impact}")
                other_lines.append(f"  Medium-term: {o.medium_term_impact}")
                other_lines.append(f"  Cross-domain: {o.cross_domain_effects}")
                other_lines.append("")
            others_context = "\n".join(other_lines)

            if self._on_agent_start:
                self._on_agent_start(f"{agent.name} (deliberation)")
            result = agent.deliberate(scenario, own, others_context)
            if self._on_agent_done:
                self._on_agent_done(f"{agent.name} (deliberation)")
            return result

        parallel = state.get("parallel", True)

        if parallel and len(all_agent_ids) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(all_agent_ids)) as pool:
                futures = {pool.submit(_deliberate_one, aid): aid for aid in all_agent_ids}
                revised = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        revised.append(future.result())
                    except Exception as e:
                        aid = futures[future]
                        print(f"  WARNING: {aid} deliberation failed, keeping original")
                        revised.append(analyses_by_id[aid])
        else:
            revised = []
            for aid in all_agent_ids:
                try:
                    revised.append(_deliberate_one(aid))
                except Exception as e:
                    print(f"  WARNING: {aid} deliberation failed, keeping original")
                    revised.append(analyses_by_id[aid])

        return {"deliberated_analyses": revised}

    def _node_draft_synthesis(self, state: SimulationState) -> dict:
        """Phase 3a: Merge all agent outputs into a draft synthesis (Sonnet)."""
        all_analyses = state.get("deliberated_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        agent_summaries = self._compress_for_synthesis(all_analyses)
        scenario: Scenario = state["scenario"]

        system = """You are the synthesis engine of a multi-agent financial market simulation. You receive analyses from specialized agents and must merge them into a unified, coherent report.

Your job:
1. Resolve any contradictions between agents by weighing confidence levels and economic logic.
2. Identify the causal chains that connect agent outputs across domains.
3. Build a probability-weighted scenario matrix (base/bull/bear).
4. Highlight the key risk factors and assumptions.

Submit your synthesis using the provided tool."""

        user_msg = f"""SCENARIO: {scenario.description}
ECONOMY: {scenario.base_economy}
MAGNITUDE: {scenario.magnitude}
SURPRISE: {'Yes' if scenario.is_surprise else 'No (priced in)'}

AGENT ANALYSES:
{agent_summaries}"""

        draft = self.llm.complete_tool(
            system_prompt=system,
            user_message=user_msg,
            tool_schema=SYNTHESIS_TOOL,
            max_tokens=TOKEN_BUDGET["synthesis_draft"],
            temperature=0.3,
            cache=True,
        )
        return {"draft": draft}

    def _node_critic(self, state: SimulationState) -> dict:
        """Phase 3b: Critic pass — catch contradictions and gaps (Haiku)."""
        all_analyses = state.get("deliberated_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        agent_summaries = self._compress_for_synthesis(all_analyses)
        draft = state["draft"]

        system = """You are a financial analysis critic. You review a draft synthesis report produced by a multi-agent simulation system. Your job is to find:
1. Logical contradictions between agents that weren't resolved
2. Missing causal links in the cross-domain chain
3. Claims in the synthesis not supported by the agent analyses
4. Risk factors that were overlooked

Be precise and specific. If the draft is solid, say so — do not fabricate issues."""

        user_msg = f"""AGENT ANALYSES:
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
            system_prompt=system,
            user_message=user_msg,
            tool_schema=CRITIC_TOOL,
            max_tokens=TOKEN_BUDGET["synthesis_critic"],
            temperature=0.2,
            cache=True,
            use_fast_model=True,
        )
        return {"critique": critique}

    def _node_revise(self, state: SimulationState) -> dict:
        """Phase 3c: Re-synthesize incorporating critic feedback (Sonnet)."""
        all_analyses = state.get("deliberated_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        agent_summaries = self._compress_for_synthesis(all_analyses)
        draft = state["draft"]
        critique = state["critique"]
        scenario: Scenario = state["scenario"]

        system = """You are revising a financial market simulation synthesis based on critic feedback.
The critic identified specific issues — contradictions, missing causal links, or unsupported claims.
Produce a corrected synthesis that resolves ALL identified problems while maintaining analytical accuracy.

Submit your revised synthesis using the provided tool."""

        user_msg = f"""SCENARIO: {scenario.description}
ECONOMY: {scenario.base_economy}

ORIGINAL DRAFT:
Summary: {draft.get('scenario_summary', '')}
Immediate: {draft.get('immediate_reaction', '')}
Short-term: {draft.get('short_term_transmission', '')}
Medium-term: {draft.get('medium_term_outlook', '')}
Cross-domain map: {draft.get('cross_domain_map', '')}
Risk factors: {draft.get('risk_factors', '')}
Synthesis: {draft.get('synthesis', '')}

CRITIC FEEDBACK TO ADDRESS:
Contradictions: {critique.get('contradictions', 'None')}
Missing links: {critique.get('missing_links', 'None')}
Unsupported claims: {critique.get('unsupported_claims', 'None')}

AGENT ANALYSES (source of truth):
{agent_summaries}"""

        revised = self.llm.complete_tool(
            system_prompt=system,
            user_message=user_msg,
            tool_schema=SYNTHESIS_TOOL,
            max_tokens=TOKEN_BUDGET["synthesis_draft"],
            temperature=0.3,
            cache=True,
        )
        return {"draft": revised, "was_revised": True}

    def _node_build_report(self, state: SimulationState) -> dict:
        """Merge draft (or revised draft) + critique into final SimulationReport."""
        draft = state["draft"]
        critique = state.get("critique", {})
        was_revised = state.get("was_revised", False)
        all_analyses = state.get("deliberated_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))

        if was_revised:
            final_synthesis = draft.get("synthesis", "")
            final_risks = draft.get("risk_factors", "")
        else:
            final_synthesis = critique.get("revised_synthesis") or draft.get("synthesis", "")
            final_risks = critique.get("revised_risk_factors") or draft.get("risk_factors", "")

            contradictions = critique.get("contradictions", "").strip()
            missing_links = critique.get("missing_links", "").strip()
            no_issue = {"", "none", "none found", "none found.", "n/a"}
            if contradictions.lower() not in no_issue:
                final_risks += f"\n\nContradictions noted: {contradictions}"
            if missing_links.lower() not in no_issue:
                final_risks += f"\n\nMissing links noted: {missing_links}"

        matrix = [ScenarioOutcome(**s) for s in draft.get("scenario_matrix", [])]

        report = SimulationReport(
            scenario_summary=draft.get("scenario_summary", state["scenario"].description),
            agent_analyses=all_analyses,
            immediate_reaction=draft.get("immediate_reaction", ""),
            short_term_transmission=draft.get("short_term_transmission", ""),
            medium_term_outlook=draft.get("medium_term_outlook", ""),
            cross_domain_map=draft.get("cross_domain_map", ""),
            scenario_matrix=matrix,
            risk_factors=final_risks,
            synthesis=final_synthesis,
        )
        return {"report": report}

    # ── Shared helpers ───────────────────────────────────────────────────

    def _run_agent_batch(
        self,
        agents: list[BaseAgent],
        scenario: Scenario,
        tier: str,
        parallel: bool,
        context: str = "",
    ) -> list[AgentAnalysis]:
        """Run a batch of agents, optionally in parallel, with shared context."""

        def _run_one(agent: BaseAgent) -> AgentAnalysis:
            if self._on_agent_start:
                self._on_agent_start(agent.name)
            result = agent.analyze(scenario, context=context, tier=tier)
            if self._on_agent_done:
                self._on_agent_done(agent.name)
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

    def _compress_for_synthesis(self, analyses: list[AgentAnalysis]) -> str:
        """Format agent outputs efficiently for synthesis prompts."""
        lines = []
        for a in analyses:
            lines.append(f"### {a.agent_name} [Confidence: {a.confidence}]")
            lines.append(f"- Immediate: {a.immediate_impact}")
            lines.append(f"- Short-term: {a.short_term_impact}")
            lines.append(f"- Medium-term: {a.medium_term_impact}")
            lines.append(f"- Spillovers: {a.cross_domain_effects}")
            lines.append("")
        return "\n".join(lines)

    def _compare_reports(self, report_a: SimulationReport, report_b: SimulationReport) -> str:
        """Produce a comparative analysis of two simulation reports."""
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
