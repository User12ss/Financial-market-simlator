"""Orchestrator — LangGraph pipeline with debate, memory, MCP, and conditional routing.

Graph (11→14 nodes, 4 conditional decisions):

    START → parse_scenario → enrich_scenario → route_agents
        →? run_tier1 → build_context →? run_tier2 → check_disagreements
        →? deliberate → impact_debate → judge_debate → draft_synthesis
        → critic_review →? revise_synthesis → build_report → remember → END
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
from config import (
    BASE_ECONOMY, AGENT_ROUTING, TOKEN_BUDGET,
    MAX_DEBATE_ROUNDS, MEMORY_ENABLED, MEMORY_DIR, MEMORY_RETRIEVE_K,
    MCP_ENABLED, MCP_SERVERS,
)


# ── State schema ─────────────────────────────────────────────────────────────

class SimulationState(TypedDict, total=False):
    """Shared state flowing through every node in the graph."""
    user_input: str
    base_economy: str
    parallel: bool
    scenario: Any              # Scenario
    enrichment_context: str    # data from MCP tools
    memory_context: str        # past similar simulations
    tier1_agent_ids: list[str]
    tier2_agent_ids: list[str]
    tier1_results: list        # list[AgentAnalysis]
    tier2_results: list        # list[AgentAnalysis]
    tier1_context: str
    all_initial_analyses: list
    deliberated_analyses: list
    has_disagreements: bool
    # Debate state
    bull_arguments: list[str]
    bear_arguments: list[str]
    debate_rounds: int
    judge_verdict: str
    # Synthesis
    draft: dict
    critique: dict
    was_revised: bool
    report: Any                # SimulationReport


# ── Tool schemas ─────────────────────────────────────────────────────────────

SCENARIO_PARSE_TOOL: dict = {
    "name": "parse_scenario",
    "description": "Parse the user's policy scenario into structured fields.",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Restate the policy in precise economic terms (1-3 sentences)"},
            "policy_type": {"type": "string", "enum": ["monetary", "fiscal", "trade", "regulatory", "supply_shock", "mixed"]},
            "magnitude": {"type": "string", "enum": ["mild", "moderate", "severe"]},
            "is_surprise": {"type": "boolean"},
            "baseline_conditions": {"type": "string"},
            "time_horizon": {"type": "string"},
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
            "scenario_summary": {"type": "string"},
            "immediate_reaction": {"type": "string"},
            "short_term_transmission": {"type": "string"},
            "medium_term_outlook": {"type": "string"},
            "cross_domain_map": {"type": "string"},
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
            },
            "risk_factors": {"type": "string"},
            "synthesis": {"type": "string"},
        },
        "required": ["scenario_summary", "immediate_reaction", "short_term_transmission", "medium_term_outlook", "cross_domain_map", "scenario_matrix", "risk_factors", "synthesis"],
    },
}

CRITIC_TOOL: dict = {
    "name": "submit_critique",
    "description": "Submit your critique of the draft synthesis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contradictions": {"type": "string"},
            "missing_links": {"type": "string"},
            "unsupported_claims": {"type": "string"},
            "revised_synthesis": {"type": "string"},
            "revised_risk_factors": {"type": "string"},
            "confidence_adjustment": {"type": "string", "enum": ["raise", "maintain", "lower"]},
        },
        "required": ["contradictions", "missing_links", "unsupported_claims", "revised_synthesis", "revised_risk_factors", "confidence_adjustment"],
    },
}

DEBATE_TOOL: dict = {
    "name": "submit_argument",
    "description": "Submit your argument in the impact debate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "position_summary": {"type": "string", "description": "Your core thesis in 1 sentence"},
            "key_arguments": {"type": "string", "description": "2-3 strongest arguments with evidence from agent analyses"},
            "rebuttal": {"type": "string", "description": "Direct response to opposing position's strongest point"},
            "magnitude_estimate": {"type": "string", "description": "Your estimate of overall market impact magnitude"},
        },
        "required": ["position_summary", "key_arguments", "rebuttal", "magnitude_estimate"],
    },
}

JUDGE_TOOL: dict = {
    "name": "submit_verdict",
    "description": "Submit your verdict on the impact debate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "winner": {"type": "string", "enum": ["bull", "bear", "mixed"], "description": "Which side made the stronger case"},
            "reasoning": {"type": "string", "description": "Why this side's arguments were more compelling (2-3 sentences)"},
            "calibrated_impact": {"type": "string", "description": "Your calibrated view of the true impact, informed by both sides"},
            "key_uncertainties": {"type": "string", "description": "What the debate revealed as genuinely uncertain"},
        },
        "required": ["winner", "reasoning", "calibrated_impact", "key_uncertainties"],
    },
}


# ── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:
    """LangGraph pipeline: parse → enrich → route → tier1 → tier2 → debate → synthesize → remember."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.llm = LLMClient(api_key=api_key, model=model)
        self.agents: dict[str, BaseAgent] = {
            AgentCls.id: AgentCls(self.llm) for AgentCls in ALL_AGENTS
        }
        self._on_agent_start: callable | None = None
        self._on_agent_done: callable | None = None
        self._on_phase: callable | None = None

        # Memory system
        self.memory = None
        if MEMORY_ENABLED:
            try:
                from memory.store import SimulationMemory
                self.memory = SimulationMemory(persist_dir=MEMORY_DIR)
            except ImportError:
                pass

        # MCP tool system
        self.mcp = None
        if MCP_ENABLED and MCP_SERVERS:
            try:
                from mcp_tools.client import MCPManager, MCPServerConfig
                configs = [MCPServerConfig(**cfg) for cfg in MCP_SERVERS]
                self.mcp = MCPManager(configs)
                self.mcp.connect_all_sync()
            except Exception as e:
                print(f"  MCP initialization failed: {e}")

        self._graph = self._build_graph()

    # ── Graph construction ───────────────────────────────────────────────

    def _build_graph(self):
        graph = StateGraph(SimulationState)

        # Nodes
        graph.add_node("parse_scenario", self._node_parse)
        graph.add_node("enrich_scenario", self._node_enrich)
        graph.add_node("route_agents", self._node_route)
        graph.add_node("run_tier1", self._node_tier1)
        graph.add_node("build_context", self._node_build_context)
        graph.add_node("run_tier2", self._node_tier2)
        graph.add_node("check_disagreements", self._node_check_disagreements)
        graph.add_node("deliberate", self._node_deliberate)
        graph.add_node("impact_debate", self._node_debate)
        graph.add_node("judge_debate", self._node_judge)
        graph.add_node("draft_synthesis", self._node_draft_synthesis)
        graph.add_node("critic_review", self._node_critic)
        graph.add_node("revise_synthesis", self._node_revise)
        graph.add_node("build_report", self._node_build_report)
        graph.add_node("remember", self._node_remember)

        # Edges
        graph.add_edge(START, "parse_scenario")
        graph.add_edge("parse_scenario", "enrich_scenario")
        graph.add_edge("enrich_scenario", "route_agents")

        graph.add_conditional_edges("route_agents", self._should_run_tier1, {
            "run_tier1": "run_tier1",
            "skip_to_tier2": "run_tier2",
            "skip_all": "check_disagreements",
        })

        graph.add_edge("run_tier1", "build_context")
        graph.add_conditional_edges("build_context", self._should_run_tier2, {
            "run_tier2": "run_tier2",
            "skip_to_check": "check_disagreements",
        })

        graph.add_edge("run_tier2", "check_disagreements")
        graph.add_conditional_edges("check_disagreements", self._should_deliberate, {
            "deliberate": "deliberate",
            "skip_to_debate": "impact_debate",
        })

        graph.add_edge("deliberate", "impact_debate")
        graph.add_edge("impact_debate", "judge_debate")
        graph.add_edge("judge_debate", "draft_synthesis")
        graph.add_edge("draft_synthesis", "critic_review")

        graph.add_conditional_edges("critic_review", self._should_revise, {
            "revise": "revise_synthesis",
            "accept": "build_report",
        })

        graph.add_edge("revise_synthesis", "build_report")
        graph.add_edge("build_report", "remember")
        graph.add_edge("remember", END)

        return graph.compile()

    # ── Conditional functions ────────────────────────────────────────────

    def _should_run_tier1(self, state: SimulationState) -> str:
        has_tier1 = len(state.get("tier1_agent_ids", [])) > 0
        has_tier2 = len(state.get("tier2_agent_ids", [])) > 0
        if has_tier1:
            return "run_tier1"
        elif has_tier2:
            return "skip_to_tier2"
        return "skip_all"

    def _should_run_tier2(self, state: SimulationState) -> str:
        if len(state.get("tier2_agent_ids", [])) > 0:
            return "run_tier2"
        return "skip_to_check"

    def _should_deliberate(self, state: SimulationState) -> str:
        if state.get("has_disagreements", False):
            return "deliberate"
        return "skip_to_debate"

    def _should_revise(self, state: SimulationState) -> str:
        critique = state.get("critique", {})
        adjustment = critique.get("confidence_adjustment", "maintain")
        contradictions = critique.get("contradictions", "").strip().lower()
        missing = critique.get("missing_links", "").strip().lower()
        no_issue = {"", "none", "none found", "none found.", "n/a"}
        has_contradictions = contradictions not in no_issue
        has_missing = missing not in no_issue
        if adjustment == "lower" or (has_contradictions and has_missing):
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
        on_phase: callable = None,
    ) -> SimulationReport:
        self._on_agent_start = on_agent_start
        self._on_agent_done = on_agent_done
        self._on_phase = on_phase
        result = self._graph.invoke({
            "user_input": user_input,
            "base_economy": base_economy,
            "parallel": parallel,
        })
        return result["report"]

    def compare(self, input_a: str, input_b: str, base_economy: str = BASE_ECONOMY) -> str:
        report_a = self.run(input_a, base_economy, parallel=True)
        report_b = self.run(input_b, base_economy, parallel=True)
        return self._compare_reports(report_a, report_b)

    def _emit_phase(self, label: str, detail: str = ""):
        if self._on_phase:
            self._on_phase(label, detail)

    # ── Node: Parse ──────────────────────────────────────────────────────

    def _node_parse(self, state: SimulationState) -> dict:
        self._emit_phase("Phase 0: Parsing & routing scenario (fast model)...")
        system = "You are a financial scenario parser. Extract structured fields from the user's scenario. Use the provided tool."
        data = self.llm.complete_tool(
            system_prompt=system,
            user_message=f"Parse this scenario:\n{state['user_input']}",
            tool_schema=SCENARIO_PARSE_TOOL,
            max_tokens=TOKEN_BUDGET["scenario_parse"],
            temperature=0.2, cache=True, use_fast_model=True,
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
        self._emit_phase("", "  ✓ Scenario parsed (type: {}, magnitude: {})".format(scenario.policy_type, scenario.magnitude))
        return {"scenario": scenario}

    # ── Node: Enrich (MCP + Memory) ──────────────────────────────────────

    def _node_enrich(self, state: SimulationState) -> dict:
        """Enrich scenario with external data (MCP) and past memories."""
        self._emit_phase("Phase 1: Enriching with external data & memory...")
        scenario: Scenario = state["scenario"]
        enrichment = ""
        memory_ctx = ""

        # MCP: fetch real-time data from external services
        if self.mcp and self.mcp.is_connected:
            self._emit_phase("", "  ▸ Fetching live market data via MCP...")
            enrichment = self.mcp.enrich_scenario_context(scenario.description)
            if enrichment:
                self._emit_phase("", "  ✓ MCP enrichment received")
                for line in enrichment.split("\n"):
                    if line.strip() and not line.startswith("EXTERNAL"):
                        self._emit_phase("", f"    {line.strip()}")
            else:
                self._emit_phase("", "  ○ No relevant MCP data for this scenario")

        # Memory: retrieve similar past simulations
        if self.memory:
            self._emit_phase("", "  ▸ Searching memory for similar past simulations...")
            memories = self.memory.retrieve_similar(scenario, k=MEMORY_RETRIEVE_K)
            reflections = self.memory.retrieve_reflections(scenario)
            if memories or reflections:
                self._emit_phase("", f"  ✓ Found {len(memories)} past simulation(s), {len(reflections)} reflection(s)")
                for i, mem in enumerate(memories, 1):
                    summary = mem.get("metadata", {}).get("scenario_summary", "")[:80]
                    policy = mem.get("metadata", {}).get("policy_type", "?")
                    self._emit_phase("", f"    [{i}] ({policy}) {summary}")
                memory_ctx = self.memory.format_memories_for_prompt(scenario)
            else:
                self._emit_phase("", "  ○ No similar past simulations found (first run?)")

        return {"enrichment_context": enrichment, "memory_context": memory_ctx}

    # ── Node: Route ──────────────────────────────────────────────────────

    def _node_route(self, state: SimulationState) -> dict:
        scenario: Scenario = state["scenario"]
        policy = scenario.policy_type.value if hasattr(scenario.policy_type, "value") else str(scenario.policy_type)
        routing = AGENT_ROUTING.get(policy, AGENT_ROUTING["mixed"])
        skip = set(routing.get("skip", []))
        tier1 = [aid for aid in routing["tier1"] if aid in self.agents and aid not in skip]
        tier2 = [aid for aid in routing["tier2"] if aid in self.agents and aid not in skip]

        if skip:
            for aid in skip:
                if aid in self.agents:
                    self._emit_phase("", f"  ○ {self.agents[aid].name} — skipped (not relevant for {policy})")
        self._emit_phase("", f"  ✓ Routing complete: {len(tier1)} tier-1, {len(tier2)} tier-2, {len(skip)} skipped")

        return {"tier1_agent_ids": tier1, "tier2_agent_ids": tier2}

    # ── Nodes: Tier 1 & 2 ───────────────────────────────────────────────

    def _node_tier1(self, state: SimulationState) -> dict:
        self._emit_phase("Phase 2: Running upstream agents (tier 1)...")
        agents = [self.agents[aid] for aid in state["tier1_agent_ids"]]
        extra_context = self._build_extra_context(state)
        results = self._run_agent_batch(agents, state["scenario"], "tier1", state.get("parallel", True), context=extra_context)
        return {"tier1_results": results}

    def _node_build_context(self, state: SimulationState) -> dict:
        lines = []
        for a in state.get("tier1_results", []):
            lines.append(f"[{a.agent_name} | Confidence: {a.confidence}]")
            lines.append(f"  Immediate: {a.immediate_impact}")
            lines.append(f"  Cross-domain: {a.cross_domain_effects}")
            lines.append("")
        return {"tier1_context": "\n".join(lines)}

    def _node_tier2(self, state: SimulationState) -> dict:
        self._emit_phase("Phase 3: Running downstream agents with tier 1 context (tier 2)...")
        agents = [self.agents[aid] for aid in state["tier2_agent_ids"]]
        context = state.get("tier1_context", "") + "\n" + self._build_extra_context(state)
        results = self._run_agent_batch(agents, state["scenario"], "tier2", state.get("parallel", True), context=context)
        return {"tier2_results": results}

    # ── Node: Check Disagreements ────────────────────────────────────────

    def _node_check_disagreements(self, state: SimulationState) -> dict:
        self._emit_phase("Phase 4: Checking for agent disagreements...")
        all_analyses = state.get("tier1_results", []) + state.get("tier2_results", [])
        if len(all_analyses) < 2:
            return {"all_initial_analyses": all_analyses, "has_disagreements": False}

        summaries = self._compress_for_synthesis(all_analyses)
        system = """You are a conflict detector. Scan analyses and determine if there are MATERIAL directional disagreements (e.g., risk-on vs risk-off, strengthens vs weakens). Minor magnitude differences are NOT disagreements. Respond ONLY "yes" or "no"."""

        response = self.llm.complete(system, f"ANALYSES:\n{summaries}", max_tokens=8, temperature=0.0, cache=True, use_fast_model=True)
        has_disagreements = response.strip().lower().startswith("yes")
        if has_disagreements:
            self._emit_phase("", "  ✓ Disagreements detected — will deliberate")
        else:
            self._emit_phase("", "  ✓ Agents aligned — skipping deliberation")
        return {"all_initial_analyses": all_analyses, "has_disagreements": has_disagreements}

    # ── Node: Deliberation ───────────────────────────────────────────────

    def _node_deliberate(self, state: SimulationState) -> dict:
        self._emit_phase("Phase 4b: Agents deliberating (conflicts detected)...")
        all_analyses = state["all_initial_analyses"]
        scenario: Scenario = state["scenario"]
        analyses_by_id = {a.agent_id: a for a in all_analyses}

        def _deliberate_one(agent_id: str) -> AgentAnalysis:
            agent = self.agents[agent_id]
            own = analyses_by_id[agent_id]
            others = [a for a in all_analyses if a.agent_id != agent_id]
            others_ctx = "\n".join(
                f"[{o.agent_name}] Immediate: {o.immediate_impact} | Cross-domain: {o.cross_domain_effects}"
                for o in others
            )
            if self._on_agent_start:
                self._on_agent_start(f"{agent.name} (deliberation)")
            result = agent.deliberate(scenario, own, others_ctx)
            if self._on_agent_done:
                self._on_agent_done(f"{agent.name} (deliberation)")
            return result

        parallel = state.get("parallel", True)
        agent_ids = list(analyses_by_id.keys())

        if parallel and len(agent_ids) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(agent_ids)) as pool:
                futures = {pool.submit(_deliberate_one, aid): aid for aid in agent_ids}
                revised = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        revised.append(future.result())
                    except Exception:
                        aid = futures[future]
                        revised.append(analyses_by_id[aid])
        else:
            revised = [_deliberate_one(aid) for aid in agent_ids]

        self._emit_phase("", "  ✓ Deliberation complete")
        return {"deliberated_analyses": revised}

    # ── Nodes: Structured Debate (Bull/Bear + Judge) ─────────────────────

    def _node_debate(self, state: SimulationState) -> dict:
        """Bull/Bear impact debate — argues severity vs containment."""
        self._emit_phase("Phase 5: Adversarial debate (Bull vs Bear)...")
        all_analyses = state.get("deliberated_analyses") or state.get("all_initial_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        scenario: Scenario = state["scenario"]
        agent_summaries = self._compress_for_synthesis(all_analyses)
        memory_ctx = state.get("memory_context", "")

        bull_args = []
        bear_args = []

        for round_num in range(MAX_DEBATE_ROUNDS):
            self._emit_phase("", f"  ▸ Round {round_num+1}/{MAX_DEBATE_ROUNDS}: Bull arguing...")
            # Bull argues: impact will be SEVERE, widespread, lasting
            bull_system = """You are the IMPACT MAXIMIZER in a financial market simulation debate.
Your position: This policy scenario will have LARGER and MORE WIDESPREAD effects than a naive reading suggests.
Argue for: severe market reactions, contagion across domains, second-order effects that amplify the primary shock, historical precedents where markets underestimated similar policies.
You must directly rebut the opposing position's strongest argument."""

            bear_history = f"\nOPPOSING ARGUMENTS SO FAR:\n" + "\n".join(bear_args) if bear_args else ""

            bull_msg = f"""SCENARIO: {scenario.description}
MAGNITUDE: {scenario.magnitude} | SURPRISE: {scenario.is_surprise}

AGENT ANALYSES:
{agent_summaries}
{bear_history}
{memory_ctx}

Round {round_num + 1}/{MAX_DEBATE_ROUNDS}. Make your case."""

            bull_data = self.llm.complete_tool(
                system_prompt=bull_system, user_message=bull_msg,
                tool_schema=DEBATE_TOOL, max_tokens=TOKEN_BUDGET["debate_argue"],
                temperature=0.4, cache=True,
            )
            bull_args.append(f"[Round {round_num+1}] {bull_data.get('position_summary', '')} — {bull_data.get('key_arguments', '')}")
            self._emit_phase("", f"  ▸ Round {round_num+1}/{MAX_DEBATE_ROUNDS}: Bear arguing...")

            # Bear argues: impact will be CONTAINED, markets adapt, mean-reversion
            bear_system = """You are the IMPACT MINIMIZER in a financial market simulation debate.
Your position: This policy scenario will have SMALLER and MORE CONTAINED effects than the maximizer claims.
Argue for: market adaptation, structural dampeners, historical precedents where markets overreacted then mean-reverted, offsetting forces that limit contagion.
You must directly rebut the opposing position's strongest argument."""

            bull_history = f"\nOPPOSING ARGUMENTS SO FAR:\n" + "\n".join(bull_args)

            bear_msg = f"""SCENARIO: {scenario.description}
MAGNITUDE: {scenario.magnitude} | SURPRISE: {scenario.is_surprise}

AGENT ANALYSES:
{agent_summaries}
{bull_history}
{memory_ctx}

Round {round_num + 1}/{MAX_DEBATE_ROUNDS}. Make your case."""

            bear_data = self.llm.complete_tool(
                system_prompt=bear_system, user_message=bear_msg,
                tool_schema=DEBATE_TOOL, max_tokens=TOKEN_BUDGET["debate_argue"],
                temperature=0.4, cache=True,
            )
            bear_args.append(f"[Round {round_num+1}] {bear_data.get('position_summary', '')} — {bear_data.get('key_arguments', '')}")

        self._emit_phase("", "  ✓ Debate complete ({} rounds)".format(MAX_DEBATE_ROUNDS))
        return {"bull_arguments": bull_args, "bear_arguments": bear_args, "debate_rounds": MAX_DEBATE_ROUNDS}

    def _node_judge(self, state: SimulationState) -> dict:
        """Judge arbitrates the bull/bear debate and produces a calibrated verdict."""
        self._emit_phase("Phase 6: Judge arbitrating debate...")
        scenario: Scenario = state["scenario"]
        bull_args = state["bull_arguments"]
        bear_args = state["bear_arguments"]
        all_analyses = state.get("deliberated_analyses") or state.get("all_initial_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        agent_summaries = self._compress_for_synthesis(all_analyses)

        system = """You are the JUDGE in a financial market impact debate. Two sides argued:
- BULL (Impact Maximizer): argued the policy will have severe, widespread effects
- BEAR (Impact Minimizer): argued the policy will have contained, limited effects

Your job:
1. Decide which side made the STRONGER case based on evidence quality and economic logic.
2. Produce a CALIBRATED view — the true impact is usually between the two extremes.
3. Identify what remains genuinely uncertain (neither side proved convincingly).

Be decisive. Do not default to "mixed" unless both sides were truly equally compelling."""

        user_msg = f"""SCENARIO: {scenario.description}

BULL ARGUMENTS (impact will be severe):
{chr(10).join(bull_args)}

BEAR ARGUMENTS (impact will be contained):
{chr(10).join(bear_args)}

AGENT ANALYSES (source data):
{agent_summaries}"""

        verdict = self.llm.complete_tool(
            system_prompt=system, user_message=user_msg,
            tool_schema=JUDGE_TOOL, max_tokens=TOKEN_BUDGET["debate_judge"],
            temperature=0.3, cache=True,
        )

        winner = verdict.get('winner', 'mixed')
        verdict_text = (
            f"Debate verdict: {winner} side prevailed. "
            f"{verdict.get('reasoning', '')} "
            f"Calibrated impact: {verdict.get('calibrated_impact', '')} "
            f"Key uncertainties: {verdict.get('key_uncertainties', '')}"
        )
        self._emit_phase("", f"  ✓ Verdict: {winner} side prevailed")
        return {"judge_verdict": verdict_text}

    # ── Node: Draft Synthesis ────────────────────────────────────────────

    def _node_draft_synthesis(self, state: SimulationState) -> dict:
        self._emit_phase("Phase 7: Two-pass synthesis (draft + critic)...")
        all_analyses = state.get("deliberated_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        agent_summaries = self._compress_for_synthesis(all_analyses)
        scenario: Scenario = state["scenario"]
        judge_verdict = state.get("judge_verdict", "")

        system = """You are the synthesis engine of a multi-agent financial market simulation.
You receive agent analyses AND the verdict from a structured impact debate.
Merge everything into a unified report. The debate verdict should calibrate your magnitude estimates.
Submit your synthesis using the provided tool."""

        user_msg = f"""SCENARIO: {scenario.description}
ECONOMY: {scenario.base_economy} | MAGNITUDE: {scenario.magnitude} | SURPRISE: {'Yes' if scenario.is_surprise else 'No'}

DEBATE VERDICT (use this to calibrate magnitude):
{judge_verdict}

AGENT ANALYSES:
{agent_summaries}"""

        draft = self.llm.complete_tool(
            system_prompt=system, user_message=user_msg,
            tool_schema=SYNTHESIS_TOOL, max_tokens=TOKEN_BUDGET["synthesis_draft"],
            temperature=0.3, cache=True,
        )
        return {"draft": draft}

    # ── Node: Critic ─────────────────────────────────────────────────────

    def _node_critic(self, state: SimulationState) -> dict:
        all_analyses = state.get("deliberated_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        agent_summaries = self._compress_for_synthesis(all_analyses)
        draft = state["draft"]

        system = """You are a financial analysis critic. Find contradictions, missing causal links, and unsupported claims. If solid, say so."""

        user_msg = f"""AGENT ANALYSES:\n{agent_summaries}\n\nDRAFT SYNTHESIS:\n{self._format_draft(draft)}"""

        critique = self.llm.complete_tool(
            system_prompt=system, user_message=user_msg,
            tool_schema=CRITIC_TOOL, max_tokens=TOKEN_BUDGET["synthesis_critic"],
            temperature=0.2, cache=True, use_fast_model=True,
        )
        return {"critique": critique}

    # ── Node: Revise ─────────────────────────────────────────────────────

    def _node_revise(self, state: SimulationState) -> dict:
        self._emit_phase("", "  ▸ Critic found issues — revising synthesis...")
        all_analyses = state.get("deliberated_analyses") or (state.get("tier1_results", []) + state.get("tier2_results", []))
        agent_summaries = self._compress_for_synthesis(all_analyses)
        draft = state["draft"]
        critique = state["critique"]
        scenario: Scenario = state["scenario"]

        system = """You are revising a synthesis based on critic feedback. Address all identified issues. Submit using the provided tool."""

        user_msg = f"""SCENARIO: {scenario.description}\n\nORIGINAL DRAFT:\n{self._format_draft(draft)}\n\nCRITIC FEEDBACK:\nContradictions: {critique.get('contradictions', 'None')}\nMissing links: {critique.get('missing_links', 'None')}\nUnsupported: {critique.get('unsupported_claims', 'None')}\n\nAGENT ANALYSES:\n{agent_summaries}"""

        revised = self.llm.complete_tool(
            system_prompt=system, user_message=user_msg,
            tool_schema=SYNTHESIS_TOOL, max_tokens=TOKEN_BUDGET["synthesis_draft"],
            temperature=0.3, cache=True,
        )
        return {"draft": revised, "was_revised": True}

    # ── Node: Build Report ───────────────────────────────────────────────

    def _node_build_report(self, state: SimulationState) -> dict:
        self._emit_phase("", "  ✓ Synthesis complete")
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
            no_issue = {"", "none", "none found", "none found.", "n/a"}
            contradictions = critique.get("contradictions", "").strip()
            if contradictions.lower() not in no_issue:
                final_risks += f"\n\nContradictions noted: {contradictions}"

        # Append debate verdict to synthesis
        judge_verdict = state.get("judge_verdict", "")
        if judge_verdict:
            final_synthesis += f"\n\n[Debate calibration: {judge_verdict}]"

        matrix = [ScenarioOutcome(**s) for s in draft.get("scenario_matrix", [])]

        data_sources = []
        enrichment = state.get("enrichment_context", "")
        memory = state.get("memory_context", "")
        if enrichment:
            data_sources.append(self._summarize_enrichment(enrichment))
        if memory:
            memories = self.memory.retrieve_similar(state["scenario"], k=MEMORY_RETRIEVE_K) if self.memory else []
            for i, mem in enumerate(memories, 1):
                doc = mem.get("document", "")
                policy_line = ""
                outcome_line = ""
                for line in doc.split("\n"):
                    if line.startswith("Policy:"):
                        policy_line = line.replace("Policy:", "").strip()
                    if line.startswith("Synthesis:"):
                        outcome_line = line.replace("Synthesis:", "").strip()
                if policy_line or outcome_line:
                    summary = f"Memory [{i}]: {policy_line}"
                    if outcome_line:
                        summary += f". Outcome: {outcome_line}"
                    data_sources.append(summary)

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
            data_sources=data_sources,
        )
        return {"report": report}

    # ── Node: Remember ───────────────────────────────────────────────────

    def _node_remember(self, state: SimulationState) -> dict:
        """Store this simulation in memory for future retrieval."""
        if self.memory:
            try:
                self._emit_phase("Phase 8: Storing simulation in memory...")
                self.memory.store_simulation(state["scenario"], state["report"])
                self._emit_phase("", "  ✓ Simulation stored — will inform future runs")
            except Exception as e:
                self._emit_phase("", f"  WARNING: Failed to store memory: {e}")
        return {}

    # ── Shared helpers ───────────────────────────────────────────────────

    def _run_agent_batch(self, agents, scenario, tier, parallel, context=""):
        if parallel and len(agents) > 1:
            for agent in agents:
                if self._on_agent_start:
                    self._on_agent_start(agent.name)

            def _run_silent(agent):
                return agent.analyze(scenario, context=context, tier=tier)

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
                futures = {pool.submit(_run_silent, a): a for a in agents}
                results = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                        results.append(result)
                        if self._on_agent_done:
                            self._on_agent_done(futures[future].name)
                    except Exception as e:
                        print(f"  WARNING: {futures[future].name} failed: {e}")
                return results

        results = []
        for agent in agents:
            if self._on_agent_start:
                self._on_agent_start(agent.name)
            result = agent.analyze(scenario, context=context, tier=tier)
            if self._on_agent_done:
                self._on_agent_done(agent.name)
            results.append(result)
        return results

    def _compress_for_synthesis(self, analyses):
        lines = []
        for a in analyses:
            lines.append(f"### {a.agent_name} [Confidence: {a.confidence}]")
            lines.append(f"- Immediate: {a.immediate_impact}")
            lines.append(f"- Short-term: {a.short_term_impact}")
            lines.append(f"- Medium-term: {a.medium_term_impact}")
            lines.append(f"- Spillovers: {a.cross_domain_effects}")
            lines.append("")
        return "\n".join(lines)

    def _build_extra_context(self, state):
        parts = []
        if state.get("enrichment_context"):
            parts.append(state["enrichment_context"])
        if state.get("memory_context"):
            parts.append(state["memory_context"])
        return "\n\n".join(parts)

    def _summarize_enrichment(self, enrichment: str) -> str:
        """Summarize MCP data into a brief 1-2 sentence description."""
        data_points = []
        for line in enrichment.split("\n"):
            line = line.strip()
            if not line or line.startswith("EXTERNAL"):
                continue
            # Extract key data points from lines like "[market-data/query]: 10Y Yield: 4.5% | ..."
            content = line.split("]: ", 1)[-1] if "]:" in line else line
            for segment in content.split(" | "):
                segment = segment.strip()
                if segment:
                    data_points.append(segment)

        if not data_points:
            return "MCP: Live market data fetched via yfinance"

        summary = f"MCP (live market data): {', '.join(data_points[:4])}"
        if len(data_points) > 4:
            summary += f", and {len(data_points) - 4} more indicators"
        return summary

    def _format_draft(self, draft):
        return f"Summary: {draft.get('scenario_summary', '')}\nImmediate: {draft.get('immediate_reaction', '')}\nShort-term: {draft.get('short_term_transmission', '')}\nMedium-term: {draft.get('medium_term_outlook', '')}\nCross-domain: {draft.get('cross_domain_map', '')}\nRisks: {draft.get('risk_factors', '')}\nSynthesis: {draft.get('synthesis', '')}"

    def _compare_reports(self, report_a, report_b):
        system = """Compare two policy simulation reports. Highlight divergences, risk differences, and key decision points. Write in prose with headers."""
        user_msg = f"SCENARIO A:\n{report_a.to_markdown()}\n\n---\n\nSCENARIO B:\n{report_b.to_markdown()}"
        return self.llm.complete(system, user_msg, max_tokens=TOKEN_BUDGET["comparison"], temperature=0.4, cache=True)
