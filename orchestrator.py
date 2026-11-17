"""Agentic Orchestrator — a reason/act loop that decides the analysis strategy.

Each step the orchestrator model picks one action: fetch data, search memory,
consult specialists, deliberate, debate, or finish. The choice comes back as a
validated `NextAction` object, so the loop works with any provider that can
return structured output and does not depend on a vendor's tool-calling format.
"""

from __future__ import annotations
from typing import Any, Literal
import concurrent.futures

from pydantic import BaseModel, Field

from models.scenario import Scenario
from models.report import AgentAnalysis, SimulationReport, ScenarioOutcome
from agents import ALL_AGENTS
from agents.base import BaseAgent
from utils.llm_client import LLMClient, LLMError
from utils.text_overlap import copied_words
from utils.grounding import unsupported_figures
from data.render import GROUNDING_RULE, render_snapshot
from data.snapshot import save_snapshot
from config import (
    BASE_ECONOMY, MEMORY_ENABLED, MEMORY_DIR, MEMORY_RETRIEVE_K,
    MARKET_DATA_ENABLED, SNAPSHOT_DIR, MAX_ORCHESTRATOR_ITERATIONS, MAX_DEBATE_ROUNDS,
)

# Longest tool result kept in the step history, and longest history shown to
# the orchestrator model. Keeps the decision prompt inside small context windows.
_HISTORY_ENTRY_CHARS = 900
_HISTORY_TOTAL_CHARS = 6000

# Analysis fields with the name used in messages. Deliberation may rewrite
# all of them except the short-term one.
_ALL_FIELDS = (
    ("immediate_impact", "immediate"),
    ("short_term_impact", "short-term"),
    ("medium_term_impact", "medium-term"),
    ("cross_domain_effects", "cross-domain"),
)
_REVISED_FIELDS = tuple(f for f in _ALL_FIELDS if f[0] != "short_term_impact")
# When a field counts as copied from other agents. Either a long passage was
# lifted (_COPY_WORDS words), or a shorter one makes up a large share of the
# field (_COPY_FRACTION, with a floor so one stock phrase in a short field
# does not trigger it). Measured on two real runs with a 7B model: without a
# pasted passage, two agents' fields shared at most 17 words (one sentence,
# 35% of a field), while a field with another agent's passage pasted in
# shared about 70 words and 57%.
_COPY_WORDS = 30
_COPY_FRACTION = 0.4
_COPY_FRACTION_MIN_WORDS = 20


_FIGURES_LISTED = 8


def _listing(items: list[str]) -> str:
    """The first few items, with a count of the rest."""
    shown = ", ".join(items[:_FIGURES_LISTED])
    return shown + (f" and {len(items) - _FIGURES_LISTED} more" if len(items) > _FIGURES_LISTED else "")


def _is_copied(covered: float, total: float) -> bool:
    if covered >= _COPY_WORDS:
        return True
    return covered >= _COPY_FRACTION_MIN_WORDS and covered / total >= _COPY_FRACTION

SYNTHESIS_SYSTEM_PROMPT = (
    "You are the synthesis engine of a multi-agent financial market simulation. "
    "Merge the specialist analyses into one unified report. Resolve contradictions "
    "explicitly rather than ignoring them.\n\n"
    "The scenario matrix has exactly three rows, labelled 'Base case', 'Bull case' and 'Bear case':\n"
    "- Base case: the most likely path. It carries the highest probability.\n"
    "- Bull case: an outcome BETTER for risk assets such as equities and credit than the base case.\n"
    "- Bear case: an outcome WORSE for risk assets than the base case.\n"
    "The three probabilities add up to 100%."
)


# ── Structured outputs ───────────────────────────────────────────────────────

class ParsedScenario(BaseModel):
    description: str = Field(description="The policy described in precise economic terms")
    policy_type: Literal["monetary", "fiscal", "trade", "regulatory", "supply_shock", "mixed"]
    magnitude: Literal["mild", "moderate", "severe"]
    is_surprise: bool = Field(description="True if markets did not expect this policy")
    baseline_conditions: str = Field(description="Assumed starting macro conditions")
    time_horizon: str = Field(description="Time horizon of the scenario, e.g. '12 months'")


class NextAction(BaseModel):
    reasoning: str = Field(description="One or two sentences on why this is the right next step")
    action: Literal[
        "fetch_market_data", "search_memory", "consult_specialist",
        "run_deliberation", "run_debate", "submit_report",
    ]
    agent_ids: list[str] = Field(
        description="Agent IDs for consult_specialist or run_deliberation. Empty list for other actions."
    )
    context: str = Field(
        description="For consult_specialist: extra guidance to pass to the agents. Empty string otherwise."
    )
    tier: Literal["tier1", "tier2"] = Field(
        description="tier1 for upstream agents, tier2 for downstream agents that should receive upstream signals"
    )
    query: str = Field(description="For search_memory: what to look for. Empty string otherwise.")
    tickers: list[str] = Field(
        description="For fetch_market_data: stock or ETF symbols to add to the standard market and macro "
        "data, for example [\"XLF\", \"JPM\"]. Empty list when no specific symbol matters."
    )
    rounds: int = Field(description="For run_debate: number of rounds. Use 2 when unsure.")


class DebateArgument(BaseModel):
    position_summary: str
    key_arguments: str
    rebuttal: str
    magnitude_estimate: str


class DebateVerdict(BaseModel):
    # The debate is about how LARGE the impact is. The sides are deliberately
    # not called bull and bear: those words mean market direction, and using
    # them here made the scenario matrix come out inverted.
    winner: Literal["severe", "contained", "mixed"] = Field(
        description="Which side made the stronger case about the size of the impact"
    )
    reasoning: str
    calibrated_impact: str
    key_uncertainties: str


class ReportDraft(BaseModel):
    scenario_summary: str
    immediate_reaction: str = Field(description="0-1 week market reaction")
    short_term_transmission: str = Field(description="1-3 month effects")
    medium_term_outlook: str = Field(description="3-12 month structural outlook")
    cross_domain_map: str = Field(description="How effects propagate across domains")
    scenario_matrix: list[ScenarioOutcome] = Field(
        description="Exactly three rows labelled 'Base case', 'Bull case' and 'Bear case'. "
        "Bull case is better for risk assets than the base case, bear case is worse."
    )
    risk_factors: str
    synthesis: str = Field(description="Key takeaways and unified view")


# ── Orchestrator Context (shared mutable state) ─────────────────────────────

class OrchestratorContext:
    """Mutable state accumulated across the orchestrator's actions."""

    def __init__(self, user_input: str, base_economy: str, preferences: dict | None = None):
        self.user_input = user_input
        self.base_economy = base_economy
        self.preferences = preferences or {}
        self.scenario: Scenario | None = None
        self.snapshot: Any = None          # the data snapshot fetched for this run, if any
        self.market_data: list[str] = []   # that snapshot as the text shown to models
        self.memory_context: str = ""
        # Keyed by agent id, in the order agents were first consulted.
        self.analyses: dict[str, AgentAnalysis] = {}
        self.deliberated: dict[str, AgentAnalysis] = {}
        self.debate_verdict: str = ""
        self.data_sources: list[str] = []
        self.quality_warnings: list[str] = []
        self.done: set[str] = set()  # one-shot actions already taken

    def final_analyses(self) -> list[AgentAnalysis]:
        """Every consulted agent once, using its deliberated version where one exists."""
        return [self.deliberated.get(agent_id, a) for agent_id, a in self.analyses.items()]


# ── Main Orchestrator Class ──────────────────────────────────────────────────

class Orchestrator:
    """Agentic orchestrator: the model decides what to do next, the code does it."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        llm: Any = None,
        enable_memory: bool | None = None,
        enable_market_data: bool | None = None,
        data_builder: Any = None,
    ):
        self.llm = llm or LLMClient(provider=provider, model=model, api_key=api_key)
        self.agents: dict[str, BaseAgent] = {
            AgentCls.id: AgentCls(self.llm) for AgentCls in ALL_AGENTS
        }

        self._on_agent_start = None
        self._on_agent_done = None
        self._on_phase = None
        self._parallel = True

        # Memory system. A failure here is recorded and reported, never hidden.
        self.memory = None
        self.memory_error = ""
        if MEMORY_ENABLED if enable_memory is None else enable_memory:
            try:
                from memory.store import SimulationMemory
                self.memory = SimulationMemory(persist_dir=MEMORY_DIR)
            except Exception as e:
                self.memory_error = f"{type(e).__name__}: {e}"
        else:
            self.memory_error = "disabled in config"

        # Market data. Nothing is fetched until the orchestrator asks for it;
        # each fetch is saved as a point-in-time snapshot.
        self.data = None
        self.data_error = ""
        wanted = MARKET_DATA_ENABLED if enable_market_data is None else enable_market_data
        if data_builder is not None:
            self.data = data_builder
        elif wanted:
            try:
                from data.snapshot import SnapshotBuilder
                self.data = SnapshotBuilder()
            except Exception as e:
                self.data_error = f"{type(e).__name__}: {e}"
        else:
            self.data_error = "disabled in config"

    def status_lines(self) -> list[str]:
        """What is actually working right now: model, memory, market data."""
        lines = [f"LLM: {self.llm.describe()}" if hasattr(self.llm, "describe") else "LLM: custom client"]
        for problem in getattr(self.llm, "problems", lambda: [])():
            lines.append(f"LLM PROBLEM: {problem}")
        if self.memory:
            lines.append(f"Memory: on ({self.memory.scenarios.count()} past simulations)")
        else:
            lines.append(f"Memory: OFF ({self.memory_error})")
        if self.data:
            lines.append(f"Market data: on (Yahoo Finance and FRED, snapshots saved under {SNAPSHOT_DIR})")
        else:
            lines.append(f"Market data: OFF ({self.data_error})")
        return lines

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

        if self.memory and ctx.scenario and report.agent_analyses:
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
        """Parse the scenario, then let the model choose actions until it submits."""
        self._parse_scenario(ctx)
        self._emit_phase("Orchestrator reasoning about analysis strategy...")

        system_prompt = self._build_system_prompt(ctx)
        history: list[str] = []

        for iteration in range(MAX_ORCHESTRATOR_ITERATIONS):
            steps_left = MAX_ORCHESTRATOR_ITERATIONS - iteration
            try:
                step = self.llm.complete_structured(
                    system_prompt, self._build_step_message(ctx, history, steps_left), NextAction
                )
            except LLMError as e:
                self._emit_phase("", f"  ○ WARNING: Orchestrator step failed ({e}) — synthesizing with what we have")
                break

            if step.action == "submit_report":
                if not ctx.analyses:
                    history.append(
                        "submit_report -> REJECTED: no specialist has been consulted yet. "
                        "Call consult_specialist first."
                    )
                    continue
                return self._synthesize(ctx)

            result = self._execute_action(step, ctx)
            history.append(f"{step.action} -> {result[:_HISTORY_ENTRY_CHARS]}")
        else:
            self._emit_phase("", "  ○ Max iterations reached — forcing synthesis")

        return self._synthesize(ctx)

    # ── Prompt Construction ──────────────────────────────────────────────

    def _build_system_prompt(self, ctx: OrchestratorContext) -> str:
        prefs = ctx.preferences
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
        pref_section = "\n\nUSER PREFERENCES:\n" + "\n".join(pref_lines) if pref_lines else ""

        available_agents = "\n".join(
            f"  - {a.id}: {a.name} — {a.domain}" for a in self.agents.values()
        )
        has_data = "AVAILABLE" if self.data else "NOT AVAILABLE"
        has_memory = "AVAILABLE" if self.memory else "NOT AVAILABLE"

        return f"""You are the orchestrator of a multi-agent financial market simulation system.

Your job is to analyze a policy scenario by deciding WHAT to do and in WHAT ORDER. You are not following a fixed pipeline. Each turn you choose exactly one action.

AVAILABLE SPECIALIST AGENTS:
{available_agents}

MARKET DATA: {has_data}
SIMULATION MEMORY: {has_memory}

ACTIONS:
- fetch_market_data: get current market levels (equity indices, Treasury yields, sectors, dollar, gold, oil) and macro series (inflation, jobs, policy rate). Put symbols in "tickers" only if specific stocks or ETFs matter to the scenario. Only when market data is AVAILABLE.
- search_memory: look for similar past simulations. Set "query". Only when memory is AVAILABLE.
- consult_specialist: ask one or more specialists for their domain analysis. Set "agent_ids" to agents not yet consulted. Put independent agents in one call. Use tier1 for upstream agents and tier2 for downstream agents that should build on the upstream results.
- run_deliberation: have consulted specialists read each other's analyses and revise. Use when their views conflict.
- run_debate: run a two-sided debate on how large the impact is (severe versus contained), with a judge. Use when the magnitude is uncertain.
- submit_report: finish. Use when the analysis is sufficient. At least one specialist must have been consulted.

GUIDELINES:
- You do NOT need every agent. Consult the ones this scenario affects.
- Simple, clear scenarios: consult 2-4 key agents, then submit_report.
- Complex or ambiguous scenarios: fetch data, consult agents in tiers, deliberate, debate, then submit_report.
- Think about which agents' outputs feed other agents: monetary policy drives fixed income, fiscal drives equities, and so on.
- Do not repeat an action that already ran. Move forward.{pref_section}

Always end by choosing submit_report."""

    def _build_step_message(self, ctx: OrchestratorContext, history: list[str], steps_left: int) -> str:
        s = ctx.scenario
        consulted = list(ctx.analyses)
        remaining = [agent_id for agent_id in self.agents if agent_id not in ctx.analyses]

        def flag(name: str) -> str:
            return "yes" if name in ctx.done else "no"

        shown = "\n".join(f"{i}. {entry}" for i, entry in enumerate(history, 1))
        if len(shown) > _HISTORY_TOTAL_CHARS:
            shown = "(earlier steps omitted)\n..." + shown[-_HISTORY_TOTAL_CHARS:]

        return f"""SCENARIO: {s.description}
TYPE: {s.policy_type.value} | MAGNITUDE: {s.magnitude} | SURPRISE: {'yes' if s.is_surprise else 'no'} | ECONOMY: {s.base_economy}

STATE:
- Specialists consulted: {', '.join(consulted) or 'none'}
- Specialists not yet consulted: {', '.join(remaining) or 'none'}
- Market data fetched: {flag('fetch_market_data')}
- Memory searched: {flag('search_memory')}
- Deliberation run: {flag('run_deliberation')}
- Debate run: {'not allowed (user preference)' if ctx.preferences.get('skip_debate') else flag('run_debate')}
- Steps left: {steps_left}

ACTIONS SO FAR:
{shown or 'none'}

Choose the next action."""

    # ── Action Execution ─────────────────────────────────────────────────

    def _execute_action(self, step: NextAction, ctx: OrchestratorContext) -> str:
        handlers = {
            "fetch_market_data": self._act_fetch_market_data,
            "search_memory": self._act_search_memory,
            "consult_specialist": self._act_consult_specialist,
            "run_deliberation": self._act_run_deliberation,
            "run_debate": self._act_run_debate,
        }
        # Handlers add themselves to ctx.done once they have run, so the model
        # cannot spend its steps repeating the same one-shot action.
        if step.action in ctx.done:
            return "ALREADY DONE. Do not repeat it. Choose a different action or submit_report."
        try:
            return handlers[step.action](step, ctx)
        except LLMError as e:
            self._emit_phase("", f"  ○ WARNING: {step.action} failed: {e}")
            if step.action != "consult_specialist":
                ctx.done.add(step.action)
            return f"FAILED: {e}"

    def _parse_scenario(self, ctx: OrchestratorContext) -> None:
        self._emit_phase("Parsing scenario...")
        try:
            data = self.llm.complete_structured(
                "You are a financial scenario parser. Extract the structured fields from the scenario.",
                f"Parse this scenario for the {ctx.base_economy} economy:\n{ctx.user_input}",
                ParsedScenario,
                fast=True,
            )
            ctx.scenario = Scenario(
                raw_input=ctx.user_input,
                base_economy=ctx.base_economy,
                description=data.description or ctx.user_input,
                policy_type=data.policy_type,
                magnitude=data.magnitude,
                is_surprise=data.is_surprise,
                baseline_conditions=data.baseline_conditions or "Current macro environment",
                time_horizon=data.time_horizon or "12 months",
            )
            self._emit_phase(
                "",
                f"  ✓ Parsed: type={ctx.scenario.policy_type.value}, magnitude={ctx.scenario.magnitude}, "
                f"surprise={'yes' if ctx.scenario.is_surprise else 'no'}",
            )
        except LLMError as e:
            ctx.scenario = Scenario(
                raw_input=ctx.user_input, description=ctx.user_input, base_economy=ctx.base_economy
            )
            self._emit_phase("", f"  ○ WARNING: Scenario parsing failed ({e}) — using the raw text")

    def _act_fetch_market_data(self, step: NextAction, ctx: OrchestratorContext) -> str:
        ctx.done.add("fetch_market_data")
        if not self.data:
            return "Market data is not available."

        self._emit_phase("Fetching market data snapshot...")
        try:
            snapshot = self.data.build(step.tickers)
        except Exception as e:  # a data failure must not end the analysis
            self._emit_phase("", f"  ○ WARNING: Market data failed: {e}")
            return f"Market data could not be fetched: {e}"
        text = render_snapshot(snapshot)
        if not text:
            self._emit_phase("", "  ○ No market data returned")
            return "No market data was returned."

        saved = ""
        try:
            saved = f", saved to {save_snapshot(snapshot)}"
        except OSError as e:
            self._emit_phase("", f"  ○ WARNING: Snapshot could not be saved: {e}")

        ctx.snapshot = snapshot
        ctx.market_data = [text]
        ctx.data_sources.append(
            f"Data snapshot {snapshot.snapshot_id}: session {snapshot.session_date}, "
            f"{len(snapshot.market)} market series, {len(snapshot.macro)} macro series"
            + (f", tickers {', '.join(snapshot.tickers)}" if snapshot.tickers else "")
            + saved
        )
        problems = snapshot.issues + [issue for item in snapshot.tickers.values() for issue in item.issues]
        ctx.quality_warnings.extend(f"Data: {issue.message}" for issue in problems)
        self._emit_phase(
            "",
            f"  ✓ Snapshot {snapshot.snapshot_id}: {len(snapshot.market)} market and "
            f"{len(snapshot.macro)} macro series" + (f", {len(problems)} data issue(s)" if problems else ""),
        )
        return text

    def _act_search_memory(self, step: NextAction, ctx: OrchestratorContext) -> str:
        ctx.done.add("search_memory")
        if not self.memory:
            return "Memory is not available."

        self._emit_phase("Searching simulation memory...")
        memories = self.memory.retrieve_similar(ctx.scenario, k=MEMORY_RETRIEVE_K)
        reflections = self.memory.retrieve_reflections(ctx.scenario)
        if not memories and not reflections:
            self._emit_phase("", "  ○ No similar past simulations found")
            return "No similar past simulations in memory."

        self._emit_phase("", f"  ✓ Found {len(memories)} past simulation(s), {len(reflections)} reflection(s)")
        lines = []
        for i, mem in enumerate(memories, 1):
            policy_line = outcome_line = ""
            for line in mem.get("document", "").split("\n"):
                if line.startswith("Policy:"):
                    policy_line = line.replace("Policy:", "").strip()
                if line.startswith("Synthesis:"):
                    outcome_line = line.replace("Synthesis:", "").strip()
            summary = f"[{i}] {policy_line}"
            if outcome_line:
                summary += f" → Earlier simulation concluded: {outcome_line[:150]}"
            lines.append(summary)
            ctx.data_sources.append(f"Memory [{i}]: {policy_line}. Earlier conclusion: {outcome_line[:100]}")
        for ref in reflections:
            lines.append(f"[Reflection] {ref[:200]}")

        ctx.memory_context = "\n".join(lines)
        return "PAST SIMULATIONS (earlier model output, not observed market results):\n" + ctx.memory_context

    def _act_consult_specialist(self, step: NextAction, ctx: OrchestratorContext) -> str:
        valid = [agent_id for agent_id in dict.fromkeys(step.agent_ids) if agent_id in self.agents]
        if not valid:
            return f"No valid agent IDs. Available: {', '.join(self.agents)}"
        new_ids = [agent_id for agent_id in valid if agent_id not in ctx.analyses]
        if not new_ids:
            return (
                f"Already consulted: {', '.join(valid)}. Pick specialists not yet consulted, "
                "or move on to run_deliberation, run_debate or submit_report."
            )

        agents = [self.agents[agent_id] for agent_id in new_ids]
        context_parts = []
        if step.context:
            context_parts.append(step.context)
        if step.tier == "tier2" and ctx.analyses:
            context_parts.append(self._format_analyses(ctx.final_analyses(), compact=True))
        if ctx.memory_context:
            context_parts.append(ctx.memory_context)

        self._emit_phase(f"Consulting specialists ({', '.join(a.name for a in agents)})...")
        results = self._run_agent_batch(
            agents, ctx.scenario, step.tier, self._parallel,
            context="\n\n".join(context_parts), data="\n".join(ctx.market_data),
        )
        if not results:
            return "All requested specialists failed. Try again or continue without them."
        for analysis in results:
            ctx.analyses[analysis.agent_id] = analysis

        return "SPECIALIST ANALYSES:\n" + self._format_analyses(results, compact=True)

    def _act_run_deliberation(self, step: NextAction, ctx: OrchestratorContext) -> str:
        ids = [agent_id for agent_id in dict.fromkeys(step.agent_ids) if agent_id in ctx.analyses]
        if len(ids) < 2:
            ids = list(ctx.analyses)
        if len(ids) < 2:
            return "Need at least 2 consulted specialists to deliberate."

        ctx.done.add("run_deliberation")
        self._emit_phase("Running agent deliberation...")
        current = {a.agent_id: a for a in ctx.final_analyses()}

        def _deliberate_one(agent_id: str) -> AgentAnalysis:
            agent = self.agents[agent_id]
            others_ctx = "\n".join(
                f"[{o.agent_name}] Immediate: {o.immediate_impact} | Cross-domain: {o.cross_domain_effects}"
                for other_id, o in current.items() if other_id != agent_id
            )
            if self._on_agent_start:
                self._on_agent_start(f"{agent.name} (deliberation)")
            result = agent.deliberate(ctx.scenario, current[agent_id], others_ctx)
            if self._on_agent_done:
                self._on_agent_done(f"{agent.name} (deliberation)")
            return result

        revised: list[AgentAnalysis] = []
        if self._parallel and len(ids) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(ids)) as pool:
                futures = {pool.submit(_deliberate_one, agent_id): agent_id for agent_id in ids}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        revised.append(future.result())
                    except Exception as e:
                        self._emit_phase("", f"  ○ WARNING: {futures[future]} deliberation failed: {e}")
        else:
            for agent_id in ids:
                try:
                    revised.append(_deliberate_one(agent_id))
                except Exception as e:
                    self._emit_phase("", f"  ○ WARNING: {agent_id} deliberation failed: {e}")

        # Agents that did not take part, or failed, keep their original analysis.
        for analysis in revised:
            self._keep_original_where_copied(analysis, current, ctx)
            ctx.deliberated[analysis.agent_id] = analysis
        self._emit_phase("", "  ✓ Deliberation complete")

        revised.sort(key=lambda a: ids.index(a.agent_id))
        return "DELIBERATED ANALYSES:\n" + self._format_analyses(revised, compact=True)

    def _act_run_debate(self, step: NextAction, ctx: OrchestratorContext) -> str:
        ctx.done.add("run_debate")
        if ctx.preferences.get("skip_debate"):
            return "Debate is disabled by user preference. Choose a different action."

        self._emit_phase("Running impact debate (severe vs contained)...")
        scenario_desc = ctx.scenario.description
        analyses_summary = self._format_analyses(ctx.final_analyses(), compact=True) or "No specialist analyses yet."
        rounds = max(1, min(step.rounds, MAX_DEBATE_ROUNDS))

        stay_on_side = (
            " Argue your assigned side fully, even if you find the other side plausible. "
            "Rebut the opposing view and do not concede."
        )
        severe_system = (
            "You argue the SEVERE-IMPACT side of a debate: this policy will have large, widespread effects."
            + stay_on_side
        )
        contained_system = (
            "You argue the CONTAINED-IMPACT side of a debate: this policy will have limited effects, "
            "because markets and the economy adapt." + stay_on_side
        )
        severe_args: list[str] = []
        contained_args: list[str] = []

        for round_num in range(1, rounds + 1):
            self._emit_phase("", f"  ▸ Debate round {round_num}/{rounds}...")

            opposing = "\nOPPOSING SIDE SO FAR:\n" + "\n".join(contained_args) if contained_args else ""
            severe = self.llm.complete_structured(
                severe_system,
                f"SCENARIO: {scenario_desc}\nANALYSES:\n{analyses_summary}{opposing}\nRound {round_num}.",
                DebateArgument,
            )
            severe_args.append(f"[R{round_num}] {severe.position_summary} — {severe.key_arguments}")

            opposing = "\nOPPOSING SIDE SO FAR:\n" + "\n".join(severe_args)
            contained = self.llm.complete_structured(
                contained_system,
                f"SCENARIO: {scenario_desc}\nANALYSES:\n{analyses_summary}{opposing}\nRound {round_num}.",
                DebateArgument,
            )
            contained_args.append(f"[R{round_num}] {contained.position_summary} — {contained.key_arguments}")

        self._emit_phase("", "  ▸ Judge arbitrating...")
        verdict = self.llm.complete_structured(
            "You are the JUDGE of a debate about how LARGE the impact of a policy will be. One side argued "
            "the impact is severe, the other that it is contained. Decide which side made the stronger case "
            "and give a calibrated view of the size of the impact. The question is magnitude, not whether "
            "markets rise or fall.",
            f"SCENARIO: {scenario_desc}\n\nSEVERE-IMPACT SIDE:\n" + "\n".join(severe_args)
            + "\n\nCONTAINED-IMPACT SIDE:\n" + "\n".join(contained_args),
            DebateVerdict,
        )

        ctx.debate_verdict = (
            f"Impact size judged {verdict.winner}. {verdict.reasoning} Calibrated: {verdict.calibrated_impact}"
        )
        self._emit_phase("", f"  ✓ Debate complete — impact judged {verdict.winner}")
        return f"DEBATE VERDICT: {ctx.debate_verdict}\nKey uncertainties: {verdict.key_uncertainties}"

    # ── Synthesis ────────────────────────────────────────────────────────

    def _synthesize(self, ctx: OrchestratorContext) -> SimulationReport:
        """Turn everything gathered so far into the final report."""
        analyses = ctx.final_analyses()
        if not analyses:
            return SimulationReport(
                scenario_summary=ctx.user_input,
                synthesis="Simulation completed without any specialist analysis.",
                data_sources=ctx.data_sources,
            )

        self._emit_phase("Building final report...")
        sections = [
            f"SCENARIO: {ctx.scenario.description}",
            f"ECONOMY: {ctx.scenario.base_economy} | MAGNITUDE: {ctx.scenario.magnitude} | "
            f"SURPRISE: {'yes' if ctx.scenario.is_surprise else 'no'}",
            "SPECIALIST ANALYSES:\n" + self._format_analyses(analyses, compact=False),
        ]
        if ctx.debate_verdict:
            sections.append(
                "DEBATE VERDICT ON THE SIZE OF THE IMPACT (severe versus contained; it says nothing "
                f"about market direction): {ctx.debate_verdict}"
            )
        if ctx.market_data:
            sections.append("\n".join(ctx.market_data))
        if ctx.memory_context:
            sections.append("PAST SIMULATIONS (earlier model output):\n" + ctx.memory_context)

        warnings = ctx.quality_warnings + self._shared_text_warnings(analyses)

        system_prompt = SYNTHESIS_SYSTEM_PROMPT + (f"\n\n{GROUNDING_RULE}" if ctx.market_data else "")
        try:
            draft = self.llm.complete_structured(system_prompt, "\n\n".join(sections), ReportDraft)
        except LLMError as e:
            self._emit_phase("", f"  ○ WARNING: Synthesis failed: {e}")
            return SimulationReport(
                scenario_summary=ctx.scenario.description,
                agent_analyses=analyses,
                synthesis=f"The specialist analyses below are complete, but the final synthesis failed: {e}",
                data_sources=ctx.data_sources,
                quality_warnings=warnings + self._grounding_warnings(ctx, analyses, narrative=""),
            )

        # Probabilities in the matrix are the model's own estimates, not data,
        # so they are left out of the figure check.
        narrative = "\n".join(
            [draft.scenario_summary, draft.immediate_reaction, draft.short_term_transmission,
             draft.medium_term_outlook, draft.cross_domain_map, draft.risk_factors, draft.synthesis]
            + [f"{row.key_driver}\n{row.market_impact}" for row in draft.scenario_matrix]
        )
        warnings += self._grounding_warnings(ctx, analyses, narrative)

        synthesis = draft.synthesis
        if ctx.debate_verdict:
            synthesis += f"\n\n[Debate calibration: {ctx.debate_verdict}]"

        self._emit_phase("", "  ✓ Report complete")
        return SimulationReport(
            scenario_summary=draft.scenario_summary or ctx.scenario.description,
            agent_analyses=analyses,
            immediate_reaction=draft.immediate_reaction,
            short_term_transmission=draft.short_term_transmission,
            medium_term_outlook=draft.medium_term_outlook,
            cross_domain_map=draft.cross_domain_map,
            scenario_matrix=draft.scenario_matrix,
            risk_factors=draft.risk_factors,
            synthesis=synthesis,
            data_sources=ctx.data_sources,
            quality_warnings=warnings,
        )

    # ── Independence checks ──────────────────────────────────────────────

    @staticmethod
    def _analysis_fields(analysis: AgentAnalysis) -> list[str]:
        return [getattr(analysis, field) for field, _ in _ALL_FIELDS]

    def _keep_original_where_copied(
        self, revised: AgentAnalysis, before: dict[str, AgentAnalysis], ctx: OrchestratorContext
    ) -> None:
        """Undo the parts of a deliberation revision that mostly repeat other agents.

        Deliberation is meant to let agents challenge each other. A weak model
        instead pastes other agents' sentences into its own analysis, which
        turns several independent views into one. Where that happens the
        agent's own earlier text is restored.
        """
        original = before[revised.agent_id]
        own_fields = self._analysis_fields(original)
        others = [
            field_text
            for agent_id, a in before.items() if agent_id != revised.agent_id
            for field_text in self._analysis_fields(a)
        ]
        restored = []
        for field, label in _REVISED_FIELDS:
            if _is_copied(*copied_words(getattr(revised, field), others, own=own_fields)):
                setattr(revised, field, getattr(original, field))
                restored.append(label)
        if restored:
            message = (
                f"{revised.agent_name}: its revised {', '.join(restored)} text mostly repeated "
                "other agents, so its own earlier text was kept."
            )
            ctx.quality_warnings.append(message)
            self._emit_phase("", f"  ○ {message}")

    def _grounding_warnings(
        self, ctx: OrchestratorContext, analyses: list[AgentAnalysis], narrative: str
    ) -> list[str]:
        """Name the figures that appear nowhere in what their author was shown.

        The prompt tells agents to cite only figures from the data snapshot or
        the scenario. This is the check that the instruction was followed.
        """
        scenario = ctx.scenario
        parsed = f"{scenario.description}\n{scenario.baseline_conditions}"
        shown = ["\n".join(ctx.market_data), ctx.user_input, parsed]
        stated = [ctx.user_input, parsed]   # amounts here may be restated in other units
        where = "the data snapshot or the scenario" if ctx.market_data else "the scenario (no data snapshot was fetched)"
        warnings = []

        # The parsed scenario is model output too, and agents take it as given.
        # Restating the user's amount in other units ("a quarter point" as
        # "25 basis points") is not an addition.
        added = unsupported_figures(parsed, [], restatable=[ctx.user_input])
        if added:
            warnings.append(
                f"The scenario parser introduced {_listing(added)}, which your scenario did not state. "
                "Agents were shown it as part of the scenario."
            )
        for analysis in analyses:
            agent = self.agents.get(analysis.agent_id)
            sources = shown + ([agent.system_prompt] if agent else [])
            missing = unsupported_figures("\n".join(self._analysis_fields(analysis)), sources, restatable=stated)
            if missing:
                warnings.append(
                    f"{analysis.agent_name} states {len(missing)} figure(s) not found in {where}: {_listing(missing)}."
                )
        missing = unsupported_figures(narrative, shown, restatable=stated)
        if missing:
            warnings.append(
                f"The report's summary sections state {len(missing)} figure(s) not found in {where}: {_listing(missing)}."
            )
        return warnings

    def _shared_text_warnings(self, analyses: list[AgentAnalysis]) -> list[str]:
        """Name pairs of agents where a field of one largely repeats the other.

        This catches copying that deliberation did not cause, such as a
        downstream agent lifting text from the upstream signals it was shown.
        Which of the two copied cannot be known here, so the wording is neutral.
        """
        warnings = []
        for i, first in enumerate(analyses):
            for second in analyses[i + 1:]:
                worst = None  # (words copied, total words, field label) of the clearest case
                for a, b in ((first, second), (second, first)):
                    other_fields = self._analysis_fields(b)
                    for field, label in _ALL_FIELDS:
                        covered, total = copied_words(getattr(a, field), other_fields)
                        if _is_copied(covered, total) and (worst is None or covered > worst[0]):
                            worst = (covered, total, label)
                if worst:
                    covered, total, label = worst
                    warnings.append(
                        f"{first.agent_name} and {second.agent_name} share a passage of about {covered:.0f} words "
                        f"word for word ({covered / total:.0%} of one agent's {label} text). Treat them as "
                        "one view, not two independent ones."
                    )
        return warnings

    # ── Agent Execution ──────────────────────────────────────────────────

    def _run_agent_batch(self, agents, scenario, tier, parallel, context="", data="") -> list[AgentAnalysis]:
        """Run agents and return the analyses that succeeded, in the order requested."""
        results: dict[str, AgentAnalysis] = {}

        def _run_one(agent: BaseAgent) -> AgentAnalysis:
            return agent.analyze(scenario, context=context, tier=tier, data=data)

        if parallel and len(agents) > 1:
            for agent in agents:
                if self._on_agent_start:
                    self._on_agent_start(agent.name)
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(agents)) as pool:
                futures = {pool.submit(_run_one, a): a for a in agents}
                for future in concurrent.futures.as_completed(futures):
                    agent = futures[future]
                    try:
                        results[agent.id] = future.result()
                        if self._on_agent_done:
                            self._on_agent_done(agent.name)
                    except Exception as e:
                        self._emit_phase("", f"  ○ WARNING: {agent.name} failed: {e}")
        else:
            for agent in agents:
                if self._on_agent_start:
                    self._on_agent_start(agent.name)
                try:
                    results[agent.id] = _run_one(agent)
                    if self._on_agent_done:
                        self._on_agent_done(agent.name)
                except Exception as e:
                    self._emit_phase("", f"  ○ WARNING: {agent.name} failed: {e}")

        return [results[a.id] for a in agents if a.id in results]

    # ── Helpers ──────────────────────────────────────────────────────────

    def _emit_phase(self, label: str, detail: str = ""):
        if self._on_phase:
            self._on_phase(label, detail)

    @staticmethod
    def _format_analyses(analyses: list[AgentAnalysis], compact: bool) -> str:
        lines = []
        for a in analyses:
            lines.append(f"### {a.agent_name} [Confidence: {a.confidence}]")
            lines.append(f"- Immediate: {a.immediate_impact}")
            if not compact:
                lines.append(f"- Short-term: {a.short_term_impact}")
            lines.append(f"- Medium-term: {a.medium_term_impact}")
            lines.append(f"- Cross-domain: {a.cross_domain_effects}")
        return "\n".join(lines)

    def _compare_reports(self, report_a, report_b):
        system = "Compare two policy simulation reports. Highlight divergences, risk differences, and key decision points."
        user_msg = f"SCENARIO A:\n{report_a.to_markdown()}\n\n---\n\nSCENARIO B:\n{report_b.to_markdown()}"
        return self.llm.complete(system, user_msg)
