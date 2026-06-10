"""Vector memory store for simulation history — enables cross-run learning."""

from __future__ import annotations
import json
import time
from pathlib import Path

import chromadb
from chromadb.config import Settings

from models.scenario import Scenario
from models.report import SimulationReport, AgentAnalysis


class SimulationMemory:
    """ChromaDB-backed memory for past simulation results.

    Stores scenario + synthesis outcomes. Agents retrieve past similar
    scenarios before analysis to learn from prior simulations.
    """

    def __init__(self, persist_dir: str = ".memory"):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )

        self.scenarios = self.client.get_or_create_collection(
            name="simulation_scenarios",
            metadata={"hnsw:space": "cosine"},
        )

        self.reflections = self.client.get_or_create_collection(
            name="simulation_reflections",
            metadata={"hnsw:space": "cosine"},
        )

    def store_simulation(self, scenario: Scenario, report: SimulationReport) -> str:
        """Store a completed simulation for future retrieval."""
        doc_id = f"sim_{int(time.time())}_{hash(scenario.raw_input) % 10000}"

        scenario_text = (
            f"Policy: {scenario.description}\n"
            f"Type: {scenario.policy_type.value if hasattr(scenario.policy_type, 'value') else scenario.policy_type}\n"
            f"Magnitude: {scenario.magnitude}\n"
            f"Economy: {scenario.base_economy}\n"
            f"Surprise: {scenario.is_surprise}"
        )

        outcome_text = (
            f"Synthesis: {report.synthesis}\n"
            f"Immediate: {report.immediate_reaction[:300]}\n"
            f"Medium-term: {report.medium_term_outlook[:300]}\n"
            f"Risks: {report.risk_factors[:200]}"
        )

        metadata = {
            "policy_type": scenario.policy_type.value if hasattr(scenario.policy_type, "value") else str(scenario.policy_type),
            "magnitude": scenario.magnitude,
            "economy": scenario.base_economy,
            "timestamp": int(time.time()),
            "scenario_summary": report.scenario_summary[:500],
        }

        self.scenarios.add(
            ids=[doc_id],
            documents=[f"{scenario_text}\n---\nOUTCOME:\n{outcome_text}"],
            metadatas=[metadata],
        )

        return doc_id

    def store_reflection(self, scenario: Scenario, reflection: str) -> str:
        """Store a post-simulation reflection (lessons learned)."""
        doc_id = f"ref_{int(time.time())}_{hash(scenario.raw_input) % 10000}"

        self.reflections.add(
            ids=[doc_id],
            documents=[reflection],
            metadatas=[{
                "policy_type": scenario.policy_type.value if hasattr(scenario.policy_type, "value") else str(scenario.policy_type),
                "timestamp": int(time.time()),
                "scenario": scenario.description[:200],
            }],
        )
        return doc_id

    def retrieve_similar(self, scenario: Scenario, k: int = 3) -> list[dict]:
        """Retrieve past simulations most similar to the current scenario."""
        if self.scenarios.count() == 0:
            return []

        query = (
            f"{scenario.description}\n"
            f"Type: {scenario.policy_type.value if hasattr(scenario.policy_type, 'value') else scenario.policy_type}\n"
            f"Magnitude: {scenario.magnitude}"
        )

        results = self.scenarios.query(
            query_texts=[query],
            n_results=min(k, self.scenarios.count()),
        )

        memories = []
        if results and results["documents"]:
            for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
                memories.append({
                    "document": doc,
                    "metadata": meta,
                })
        return memories

    def retrieve_reflections(self, scenario: Scenario, k: int = 2) -> list[str]:
        """Retrieve past reflections relevant to the current scenario."""
        if self.reflections.count() == 0:
            return []

        query = f"{scenario.description} {scenario.policy_type}"

        results = self.reflections.query(
            query_texts=[query],
            n_results=min(k, self.reflections.count()),
        )

        if results and results["documents"]:
            return results["documents"][0]
        return []

    def format_memories_for_prompt(self, scenario: Scenario) -> str:
        """Retrieve and format past memories into a prompt-ready string."""
        memories = self.retrieve_similar(scenario)
        reflections = self.retrieve_reflections(scenario)

        if not memories and not reflections:
            return ""

        lines = ["PAST SIMULATION MEMORY (similar scenarios this system has analyzed before):"]

        if memories:
            for i, mem in enumerate(memories, 1):
                lines.append(f"\n--- Past Simulation {i} ---")
                lines.append(mem["document"])

        if reflections:
            lines.append("\n--- Lessons Learned from Past Runs ---")
            for ref in reflections:
                lines.append(ref)

        return "\n".join(lines)
