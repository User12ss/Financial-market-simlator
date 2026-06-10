"""Agentic Orchestrator — ReAct-style tool-use loop that dynamically decides analysis strategy.

Instead of a fixed pipeline, the orchestrator LLM reasons about the scenario and decides
which tools to use, which specialists to consult, and when to synthesize — adapting its
approach based on the scenario's nature and user preferences.
"""

from __future__ import annotations
from typing import Any
import concurrent.futures
import json

import anthropic

from models.scenario import Scenario, PolicyType
from models.report import AgentAnalysis, SimulationReport, ScenarioOutcome
from agents import ALL_AGENTS
from agents.base import BaseAgent
from utils.llm_client import LLMClient
from config import (
    ANTHROPIC_API_KEY, MODEL_NAME, MODEL_FAST, MAX_TOKENS,
    BASE_ECONOMY, TOKEN_BUDGET, MEMORY_ENABLED, MEMORY_DIR,
    MEMORY_RETRIEVE_K, MCP_ENABLED, MCP_SERVERS,
)


MAX_ITERATIONS = 25


# ── Orchestrator Tool Definitions ────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "parse_scenario",
        "description": "Parse the user's policy scenario into structured fields (policy type, magnitude, surprise factor, etc). Call this first to understand the scenario.",
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_input": {"type": "string", "description": "The raw scenario text to parse"},
            },
            "required": ["raw_input"],
        },
    },
    {
        "name": "fetch_market_data",
        "description": "Fetch real-time market data relevant to the scenario via MCP tools (prices, yields, technicals, sector performance). Use this when you need current market context to inform your analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What market data to fetch (e.g. 'treasury yields and equity indices' or 'commodity prices and inflation indicators')"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_memory",
        "description": "Search past simulations for similar scenarios. Returns historical analyses and outcomes that can inform the current analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Description of what to search for in memory"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "consult_specialist",
        "description": "Consult one or more specialist agents for domain-specific analysis. Each specialist analyzes the scenario through their domain lens. You can consult multiple specialists in one call for parallel execution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of agent IDs to consult. Available: central_bank, fiscal_policy, equity_market, fixed_income, forex, commodities_inflation, sentiment",
                },
                "context": {
                    "type": "string",
                    "description": "Additional context to provide the agents (e.g. upstream signals, market data, memory)",
                },
                "tier": {
                    "type": "string",
                    "enum": ["tier1", "tier2"],
                    "description": "Token budget tier. Use tier1 for primary/upstream agents, tier2 for downstream agents that receive upstream signals.",
                },
            },
            "required": ["agent_ids"],
        },
    },
    {
        "name": "run_debate",
        "description": "Run a structured bull/bear debate about the scenario's impact magnitude. Use when you want to stress-test whether the impact will be severe or contained. Returns a calibrated verdict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario_description": {"type": "string", "description": "The scenario being debated"},
                "agent_analyses_summary": {"type": "string", "description": "Summary of specialist analyses to ground the debate"},
                "rounds": {"type": "integer", "description": "Number of debate rounds (1-3)", "default": 2},
            },
            "required": ["scenario_description", "agent_analyses_summary"],
        },
    },
    {
        "name": "run_deliberation",
        "description": "Have agents review each other's analyses and revise their positions. Use when you detect disagreements between specialists that need resolution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent IDs to include in deliberation",
                },
            },
            "required": ["agent_ids"],
        },
    },
    {
        "name": "submit_report",
        "description": "Submit the final simulation report. Call this when you have enough analysis to produce a complete, well-reasoned report. This ends the simulation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario_summary": {"type": "string"},
                "immediate_reaction": {"type": "string", "description": "0-1 week market reaction"},
                "short_term_transmission": {"type": "string", "description": "1-3 month effects"},
                "medium_term_outlook": {"type": "string", "description": "3-12 month structural outlook"},
                "cross_domain_map": {"type": "string", "description": "How effects propagate across domains"},
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
                "synthesis": {"type": "string", "description": "Key takeaways and unified view"},
            },
            "required": ["scenario_summary", "immediate_reaction", "short_term_transmission", "medium_term_outlook", "cross_domain_map", "scenario_matrix", "risk_factors", "synthesis"],
        },
    },
]


# ── Orchestrator Context (shared mutable state) ─────────────────────────────

class OrchestratorContext:
    """Mutable state accumulated across the orchestrator's tool calls."""

    def __init__(self, user_input: str, base_economy: str, preferences: dict | None = None):
        self.user_input = user_input
        self.base_economy = base_economy
        self.preferences = preferences or {}
        self.scenario: Scenario | None = None
        self.market_data: list[str] = []
        self.memory_results: list[dict] = []
        self.memory_context: str = ""
        self.agent_analyses: list[AgentAnalysis] = []
        self.deliberated_analyses: list[AgentAnalysis] = []
        self.debate_verdict: str = ""
        self.data_sources: list[str] = []


# ── Main Orchestrator Class ──────────────────────────────────────────────────

class Orchestrator:
    """Agentic orchestrator using ReAct-style reasoning with tool use.

    The LLM decides dynamically what steps to take based on the scenario,
    user preferences, and intermediate results.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.model = model or MODEL_NAME
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required. Set it in .env or pass --api-key")
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.llm = LLMClient(api_key=self.api_key, model=self.model)
        self.agents: dict[str, BaseAgent] = {
            AgentCls.id: AgentCls(self.llm) for AgentCls in ALL_AGENTS
        }

        self._on_agent_start = None
        self._on_agent_done = None
        self._on_phase = None

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

    # ── Public API ───────────────────────────────────────────────────────

    def run(
        self,
        user_input: str,
        base_economy: str = BASE_ECONOMY,
        parallel: bool = True,
        preferences: dict | None = None,
        on_agent_start: Any = None,
        on_agent_done: Any = None,
        on_phase: Any = None,
    ) -> SimulationReport:
        """Run the agentic simulation loop."""
        self._on_agent_start = on_agent_start
        self._on_agent_done = on_agent_done
        self._on_phase = on_phase
        self._parallel = parallel

        ctx = OrchestratorContext(user_input, base_economy, preferences)
        report = self._agent_loop(ctx)

        # Store in memory
        if self.memory and ctx.scenario and report:
            try:
                self._emit_phase("Storing simulation in memory...")
                self.memory.store_simulation(ctx.scenario, report)
                self._emit_phase("", "  ✓ Simulation stored for future reference")
            except Exception as e:
                self._emit_phase("", f"  ○ WARNING: Memory store failed: {e}")

        return report

    def compare(self, input_a: str, input_b: str, base_economy: str = BASE_ECONOMY) -> str:
        report_a = self.run(input_a, base_economy, parallel=True)
        report_b = self.run(input_b, base_economy, parallel=True)
        return self._compare_reports(report_a, report_b)

    # ── Core Agent Loop ──────────────────────────────────────────────────

    def _agent_loop(self, ctx: OrchestratorContext) -> SimulationReport:
        """ReAct loop: the LLM reasons, picks tools, processes results, repeats."""
        self._emit_phase("Orchestrator reasoning about analysis strategy...")

        system_prompt = self._build_system_prompt(ctx)
        messages = [{"role": "user", "content": self._build_initial_message(ctx)}]

        for iteration in range(MAX_ITERATIONS):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                temperature=0.3,
                system=system_prompt,
                messages=messages,
                tools=TOOLS,
                tool_choice={"type": "auto"},
            )

            # Process response content
            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            # Check if the model is done (no tool use, just text)
            tool_uses = [b for b in assistant_content if b.type == "tool_use"]
            if not tool_uses:
                # Model finished without submitting report — force synthesis
                self._emit_phase("", "  ○ Orchestrator finished reasoning without report — forcing synthesis")
                return self._force_synthesis(ctx)

            # Execute all tool calls and collect results
            tool_results = []
            for tool_use in tool_uses:
                result = self._execute_tool(tool_use.name, tool_use.input, ctx)

                # Check if this is the final report
                if tool_use.name == "submit_report":
                    return result  # result is a SimulationReport

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result if isinstance(result, str) else json.dumps(result, default=str),
                })

            messages.append({"role": "user", "content": tool_results})

        # Max iterations reached
        self._emit_phase("", "  ○ Max iterations reached — forcing synthesis")
        return self._force_synthesis(ctx)

    # ── System Prompt Construction ───────────────────────────────────────

    def _build_system_prompt(self, ctx: OrchestratorContext) -> str:
        prefs = ctx.preferences
        pref_section = ""
        if prefs:
            pref_lines = []
            if prefs.get("depth"):
                pref_lines.append(f"- Analysis depth: {prefs['depth']} (quick/standard/deep)")
            if prefs.get("focus_domains"):
                pref_lines.append(f"- Focus on these domains: {', '.join(prefs['focus_domains'])}")
            if prefs.get("time_horizon"):
                pref_lines.append(f"- Emphasize time horizon: {prefs['time_horizon']}")
            if prefs.get("skip_debate"):
                pref_lines.append("- Skip debate (user wants faster results)")
            if prefs.get("include_memory"):
                pref_lines.append("- Include historical context from past simulations")
            if pref_lines:
                pref_section = "\n\nUSER PREFERENCES:\n" + "\n".join(pref_lines)

        available_agents = "\n".join(
            f"  - {a.id}: {a.name} — {a.domain}"
            for a in self.agents.values()
        )

        has_mcp = "AVAILABLE" if (self.mcp and self.mcp.is_connected) else "NOT AVAILABLE"
        has_memory = "AVAILABLE" if self.memory else "NOT AVAILABLE"

        return f"""You are the orchestrator of a multi-agent financial market simulation system.

Your job is to analyze a policy scenario by deciding WHAT to do and in WHAT ORDER — you are NOT following a fixed pipeline. Think carefully about what this specific scenario requires.

AVAILABLE SPECIALIST AGENTS:
{available_agents}

EXTERNAL DATA (MCP): {has_mcp}
SIMULATION MEMORY: {has_memory}

YOUR DECISION-MAKING PROCESS:
1. First, parse the scenario to understand its nature.
2. Then REASON about what this scenario needs:
   - Which domains are most affected? Consult those specialists.
   - Is real-time market data relevant? Fetch it.
   - Are there similar past scenarios in memory? Check.
   - Should upstream agents inform downstream ones? Use tiers.
   - Are there likely disagreements worth resolving? Use deliberation.
   - Is the impact magnitude uncertain? Run a debate.
3. You do NOT need to consult ALL agents — only the relevant ones.
4. You do NOT need to run debate/deliberation unless the scenario warrants it.
5. When you have sufficient analysis, synthesize and submit the report.

GUIDELINES:
- For simple/clear scenarios: parse → consult 2-4 key agents → submit report. Fast and efficient.
- For complex/ambiguous scenarios: parse → fetch data → consult all agents in tiers → deliberate → debate → submit report. Thorough and rigorous.
- For scenarios where you already have strong memory context: leverage past outcomes, consult fewer agents, focus on what's different this time.
- Always think about which agents' outputs FEED INTO other agents (monetary policy affects fixed income, fiscal affects equities, etc.)
- When consulting multiple independent agents, group them in a single consult_specialist call for parallel execution.
{pref_section}

IMPORTANT: End every simulation by calling submit_report with a complete analysis. Do not leave the simulation open-ended."""

    def _build_initial_message(self, ctx: OrchestratorContext) -> str:
        return f"""Analyze this financial policy scenario:

SCENARIO: {ctx.user_input}
ECONOMY: {ctx.base_economy}

Decide your analysis strategy and begin. Think about what this specific scenario requires — which agents to consult, whether you need market data, whether a debate would add value."""

    # ── Tool Execution ───────────────────────────────────────────────────

    def _execute_tool(self, name: str, inputs: dict, ctx: OrchestratorContext) -> Any:
        handlers = {
            "parse_scenario": self._tool_parse_scenario,
            "fetch_market_data": self._tool_fetch_market_data,
            "search_memory": self._tool_search_memory,
            "consult_specialist": self._tool_consult_specialist,
            "run_debate": self._tool_run_debate,
            "run_deliberation": self._tool_run_deliberation,
            "submit_report": self._tool_submit_report,
        }
        handler = handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        return handler(inputs, ctx)

    def _tool_parse_scenario(self, inputs: dict, ctx: OrchestratorContext) -> str:
        self._emit_phase("Parsing scenario...")
        raw = inputs.get("raw_input", ctx.user_input)

        parse_tool = {
            "name": "parse_scenario",
            "description": "Parse the policy scenario into structured fields.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "policy_type": {"type": "string", "enum": ["monetary", "fiscal", "trade", "regulatory", "supply_shock", "mixed"]},
                    "magnitude": {"type": "string", "enum": ["mild", "moderate", "severe"]},
                    "is_surprise": {"type": "boolean"},
                    "baseline_conditions": {"type": "string"},
                    "time_horizon": {"type": "string"},
                },
                "required": ["description", "policy_type", "magnitude", "is_surprise", "baseline_conditions", "time_horizon"],
            },
        }

        data = self.llm.complete_tool(
            system_prompt="You are a financial scenario parser. Extract structured fields.",
            user_message=f"Parse this scenario:\n{raw}",
            tool_schema=parse_tool,
            max_tokens=TOKEN_BUDGET["scenario_parse"],
            temperature=0.2, cache=True, use_fast_model=True,
        )

        ctx.scenario = Scenario(
            raw_input=raw,
            base_economy=ctx.base_economy,
            description=data.get("description", raw),
            policy_type=data.get("policy_type", "mixed"),
            magnitude=data.get("magnitude", "moderate"),
            is_surprise=data.get("is_surprise", True),
            baseline_conditions=data.get("baseline_conditions", "Current macro environment"),
            time_horizon=data.get("time_horizon", "12 months"),
        )

        self._emit_phase("", f"  ✓ Parsed: type={ctx.scenario.policy_type}, magnitude={ctx.scenario.magnitude}, surprise={'yes' if ctx.scenario.is_surprise else 'no'}")
        return f"Scenario parsed: type={ctx.scenario.policy_type}, magnitude={ctx.scenario.magnitude}, surprise={ctx.scenario.is_surprise}, description={ctx.scenario.description}"

    def _tool_fetch_market_data(self, inputs: dict, ctx: OrchestratorContext) -> str:
        if not self.mcp or not self.mcp.is_connected:
            return "MCP not available — no live market data."

        self._emit_phase("Fetching live market data...")
        query = inputs.get("query", ctx.user_input)
        enrichment = self.mcp.enrich_scenario_context(query)

        if enrichment:
            ctx.market_data.append(enrichment)
            ctx.data_sources.append(self._summarize_enrichment(enrichment))
            self._emit_phase("", "  ✓ Market data received")
            return f"MARKET DATA:\n{enrichment}"
        else:
            self._emit_phase("", "  ○ No relevant market data found")
            return "No relevant market data found for this query."

    def _tool_search_memory(self, inputs: dict, ctx: OrchestratorContext) -> str:
        if not self.memory:
            return "Memory system not available."

        self._emit_phase("Searching simulation memory...")
        query_text = inputs.get("query", "")

        # Use scenario if available, otherwise create temporary one for search
        if ctx.scenario:
            memories = self.memory.retrieve_similar(ctx.scenario, k=MEMORY_RETRIEVE_K)
            reflections = self.memory.retrieve_reflections(ctx.scenario)
        else:
            temp_scenario = Scenario(raw_input=query_text, description=query_text, base_economy=ctx.base_economy)
            memories = self.memory.retrieve_similar(temp_scenario, k=MEMORY_RETRIEVE_K)
            reflections = self.memory.retrieve_reflections(temp_scenario)

        if not memories and not reflections:
            self._emit_phase("", "  ○ No similar past simulations found")
            return "No similar past simulations in memory."

        ctx.memory_results = memories
        self._emit_phase("", f"  ✓ Found {len(memories)} past simulation(s), {len(reflections)} reflection(s)")

        # Format for orchestrator
        lines = []
        for i, mem in enumerate(memories, 1):
            doc = mem.get("document", "")
            policy_line = ""
            outcome_line = ""
            for line in doc.split("\n"):
                if line.startswith("Policy:"):
                    policy_line = line.replace("Policy:", "").strip()
                if line.startswith("Synthesis:"):
                    outcome_line = line.replace("Synthesis:", "").strip()
            summary = f"[{i}] {policy_line}"
            if outcome_line:
                summary += f" → Outcome: {outcome_line[:150]}"
            lines.append(summary)
            ctx.data_sources.append(f"Memory [{i}]: {policy_line}. Outcome: {outcome_line[:100]}")

        for ref in reflections:
            lines.append(f"[Reflection] {ref[:200]}")

        ctx.memory_context = "\n".join(lines)
        return f"PAST SIMULATIONS:\n" + "\n".join(lines)

    def _tool_consult_specialist(self, inputs: dict, ctx: OrchestratorContext) -> str:
        agent_ids = inputs.get("agent_ids", [])
        extra_context = inputs.get("context", "")
        tier = inputs.get("tier", "tier1")

        # Filter to valid agents
        valid_ids = [aid for aid in agent_ids if aid in self.agents]
        if not valid_ids:
            return f"No valid agent IDs provided. Available: {list(self.agents.keys())}"

        agents = [self.agents[aid] for aid in valid_ids]
        scenario = ctx.scenario or Scenario(raw_input=ctx.user_input, description=ctx.user_input, base_economy=ctx.base_economy)

        # Build context from accumulated data
        context_parts = []
        if extra_context:
            context_parts.append(extra_context)
        if ctx.market_data:
            context_parts.append("\n".join(ctx.market_data))
        if ctx.memory_context:
            context_parts.append(ctx.memory_context)
        full_context = "\n\n".join(context_parts)

        self._emit_phase(f"Consulting specialists ({', '.join(a.name for a in agents)})...")

        results = self._run_agent_batch(agents, scenario, tier, self._parallel, context=full_context)
        ctx.agent_analyses.extend(results)

        # Format results for orchestrator
        lines = []
        for a in results:
            lines.append(f"\n### {a.agent_name} [Confidence: {a.confidence}]")
            lines.append(f"- Immediate: {a.immediate_impact}")
            lines.append(f"- Short-term: {a.short_term_impact}")
            lines.append(f"- Medium-term: {a.medium_term_impact}")
            lines.append(f"- Cross-domain: {a.cross_domain_effects}")

        return "SPECIALIST ANALYSES:" + "\n".join(lines)

    def _tool_run_debate(self, inputs: dict, ctx: OrchestratorContext) -> str:
        self._emit_phase("Running adversarial debate (Bull vs Bear)...")
        scenario_desc = inputs.get("scenario_description", "")
        analyses_summary = inputs.get("agent_analyses_summary", "")
        rounds = min(inputs.get("rounds", 2), 3)

        bull_args = []
        bear_args = []

        debate_tool = {
            "name": "submit_argument",
            "description": "Submit your argument.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "position_summary": {"type": "string"},
                    "key_arguments": {"type": "string"},
                    "rebuttal": {"type": "string"},
                    "magnitude_estimate": {"type": "string"},
                },
                "required": ["position_summary", "key_arguments", "rebuttal", "magnitude_estimate"],
            },
        }

        judge_tool = {
            "name": "submit_verdict",
            "description": "Submit your verdict.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "winner": {"type": "string", "enum": ["bull", "bear", "mixed"]},
                    "reasoning": {"type": "string"},
                    "calibrated_impact": {"type": "string"},
                    "key_uncertainties": {"type": "string"},
                },
                "required": ["winner", "reasoning", "calibrated_impact", "key_uncertainties"],
            },
        }

        for round_num in range(rounds):
            self._emit_phase("", f"  ▸ Debate round {round_num+1}/{rounds}...")

            bull_system = "You are the IMPACT MAXIMIZER. Argue this policy will have SEVERE, WIDESPREAD effects. Rebut the opposing view."
            bear_history = f"\nOPPOSING:\n" + "\n".join(bear_args) if bear_args else ""
            bull_data = self.llm.complete_tool(
                system_prompt=bull_system,
                user_message=f"SCENARIO: {scenario_desc}\nANALYSES:\n{analyses_summary}{bear_history}\nRound {round_num+1}.",
                tool_schema=debate_tool,
                max_tokens=TOKEN_BUDGET["debate_argue"],
                temperature=0.4, cache=True,
            )
            bull_args.append(f"[R{round_num+1}] {bull_data.get('position_summary', '')} — {bull_data.get('key_arguments', '')}")

            bear_system = "You are the IMPACT MINIMIZER. Argue this policy will have CONTAINED, LIMITED effects. Markets adapt. Rebut the opposing view."
            bull_history = f"\nOPPOSING:\n" + "\n".join(bull_args)
            bear_data = self.llm.complete_tool(
                system_prompt=bear_system,
                user_message=f"SCENARIO: {scenario_desc}\nANALYSES:\n{analyses_summary}{bull_history}\nRound {round_num+1}.",
                tool_schema=debate_tool,
                max_tokens=TOKEN_BUDGET["debate_argue"],
                temperature=0.4, cache=True,
            )
            bear_args.append(f"[R{round_num+1}] {bear_data.get('position_summary', '')} — {bear_data.get('key_arguments', '')}")

        # Judge
        self._emit_phase("", "  ▸ Judge arbitrating...")
        judge_system = "You are the JUDGE. Decide which side (bull=severe impact, bear=contained impact) made the stronger case. Produce a calibrated view."
        verdict = self.llm.complete_tool(
            system_prompt=judge_system,
            user_message=f"SCENARIO: {scenario_desc}\n\nBULL:\n" + "\n".join(bull_args) + f"\n\nBEAR:\n" + "\n".join(bear_args),
            tool_schema=judge_tool,
            max_tokens=TOKEN_BUDGET["debate_judge"],
            temperature=0.3, cache=True,
        )

        winner = verdict.get("winner", "mixed")
        ctx.debate_verdict = f"Verdict: {winner}. {verdict.get('reasoning', '')} Calibrated: {verdict.get('calibrated_impact', '')}"
        self._emit_phase("", f"  ✓ Debate complete — {winner} side prevailed")

        return f"DEBATE VERDICT: {ctx.debate_verdict}\nKey uncertainties: {verdict.get('key_uncertainties', '')}"

    def _tool_run_deliberation(self, inputs: dict, ctx: OrchestratorContext) -> str:
        agent_ids = inputs.get("agent_ids", [])
        self._emit_phase("Running agent deliberation...")

        # Get analyses for these agents
        analyses_by_id = {a.agent_id: a for a in ctx.agent_analyses if a.agent_id in agent_ids}
        if len(analyses_by_id) < 2:
            return "Need at least 2 agent analyses to deliberate."

        scenario = ctx.scenario or Scenario(raw_input=ctx.user_input, description=ctx.user_input, base_economy=ctx.base_economy)
        all_analyses = list(analyses_by_id.values())

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

        ids_to_deliberate = list(analyses_by_id.keys())
        if self._parallel and len(ids_to_deliberate) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(ids_to_deliberate)) as pool:
                futures = {pool.submit(_deliberate_one, aid): aid for aid in ids_to_deliberate}
                revised = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        revised.append(future.result())
                    except Exception:
                        aid = futures[future]
                        revised.append(analyses_by_id[aid])
        else:
            revised = [_deliberate_one(aid) for aid in ids_to_deliberate]

        ctx.deliberated_analyses = revised
        self._emit_phase("", "  ✓ Deliberation complete")

        lines = []
        for a in revised:
            lines.append(f"### {a.agent_name} (revised) [Confidence: {a.confidence}]")
            lines.append(f"- Immediate: {a.immediate_impact}")
            lines.append(f"- Medium-term: {a.medium_term_impact}")
            lines.append(f"- Cross-domain: {a.cross_domain_effects}")
        return "DELIBERATED ANALYSES:\n" + "\n".join(lines)

    def _tool_submit_report(self, inputs: dict, ctx: OrchestratorContext) -> SimulationReport:
        self._emit_phase("Building final report...")

        # Use deliberated analyses if available, otherwise originals
        all_analyses = ctx.deliberated_analyses if ctx.deliberated_analyses else ctx.agent_analyses

        # Append debate verdict to synthesis if available
        synthesis = inputs.get("synthesis", "")
        if ctx.debate_verdict and ctx.debate_verdict not in synthesis:
            synthesis += f"\n\n[Debate calibration: {ctx.debate_verdict}]"

        matrix = [ScenarioOutcome(**s) for s in inputs.get("scenario_matrix", [])]

        report = SimulationReport(
            scenario_summary=inputs.get("scenario_summary", ctx.user_input),
            agent_analyses=all_analyses,
            immediate_reaction=inputs.get("immediate_reaction", ""),
            short_term_transmission=inputs.get("short_term_transmission", ""),
            medium_term_outlook=inputs.get("medium_term_outlook", ""),
            cross_domain_map=inputs.get("cross_domain_map", ""),
            scenario_matrix=matrix,
            risk_factors=inputs.get("risk_factors", ""),
            synthesis=synthesis,
            data_sources=ctx.data_sources,
        )

        self._emit_phase("", "  ✓ Report complete")
        return report

    # ── Fallback Synthesis ───────────────────────────────────────────────

    def _force_synthesis(self, ctx: OrchestratorContext) -> SimulationReport:
        """Force a report when the loop ends without submit_report."""
        all_analyses = ctx.deliberated_analyses if ctx.deliberated_analyses else ctx.agent_analyses

        if not all_analyses:
            return SimulationReport(
                scenario_summary=ctx.user_input,
                synthesis="Simulation completed without sufficient agent analyses.",
                data_sources=ctx.data_sources,
            )

        # Use LLM to synthesize from available analyses
        analyses_text = "\n".join(
            f"[{a.agent_name}] Immediate: {a.immediate_impact} | Short: {a.short_term_impact} | Medium: {a.medium_term_impact}"
            for a in all_analyses
        )

        synthesis_tool = {
            "name": "submit_synthesis",
            "description": "Submit synthesis.",
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

        market_data = "\n".join(ctx.market_data) if ctx.market_data else ""
        debate_info = f"\nDEBATE VERDICT: {ctx.debate_verdict}" if ctx.debate_verdict else ""

        draft = self.llm.complete_tool(
            system_prompt="Synthesize all available analyses into a final simulation report.",
            user_message=f"SCENARIO: {ctx.user_input}\n\nANALYSES:\n{analyses_text}\n{market_data}{debate_info}",
            tool_schema=synthesis_tool,
            max_tokens=TOKEN_BUDGET["synthesis_draft"],
            temperature=0.3, cache=True,
        )

        matrix = [ScenarioOutcome(**s) for s in draft.get("scenario_matrix", [])]
        return SimulationReport(
            scenario_summary=draft.get("scenario_summary", ctx.user_input),
            agent_analyses=all_analyses,
            immediate_reaction=draft.get("immediate_reaction", ""),
            short_term_transmission=draft.get("short_term_transmission", ""),
            medium_term_outlook=draft.get("medium_term_outlook", ""),
            cross_domain_map=draft.get("cross_domain_map", ""),
            scenario_matrix=matrix,
            risk_factors=draft.get("risk_factors", ""),
            synthesis=draft.get("synthesis", ""),
            data_sources=ctx.data_sources,
        )

    # ── Agent Execution ──────────────────────────────────────────────────

    def _run_agent_batch(self, agents, scenario, tier, parallel, context=""):
        if parallel and len(agents) > 1:
            for agent in agents:
                if self._on_agent_start:
                    self._on_agent_start(agent.name)

            def _run_one(agent):
                return agent.analyze(scenario, context=context, tier=tier)

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
                futures = {pool.submit(_run_one, a): a for a in agents}
                results = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                        results.append(result)
                        if self._on_agent_done:
                            self._on_agent_done(futures[future].name)
                    except Exception as e:
                        self._emit_phase("", f"  ○ WARNING: {futures[future].name} failed: {e}")
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

    # ── Helpers ──────────────────────────────────────────────────────────

    def _emit_phase(self, label: str, detail: str = ""):
        if self._on_phase:
            self._on_phase(label, detail)

    def _summarize_enrichment(self, enrichment: str) -> str:
        data_points = []
        for line in enrichment.split("\n"):
            line = line.strip()
            if not line or line.startswith("EXTERNAL"):
                continue
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

    def _compare_reports(self, report_a, report_b):
        system = "Compare two policy simulation reports. Highlight divergences, risk differences, and key decision points."
        user_msg = f"SCENARIO A:\n{report_a.to_markdown()}\n\n---\n\nSCENARIO B:\n{report_b.to_markdown()}"
        return self.llm.complete(system, user_msg, max_tokens=TOKEN_BUDGET.get("comparison", 4096), temperature=0.4, cache=True)
