"""Memory store."""

from models.report import SimulationReport
from models.scenario import Scenario


def test_memory_store_roundtrip(tmp_path):
    from memory.store import SimulationMemory

    memory = SimulationMemory(persist_dir=str(tmp_path / "mem"))
    scenario = Scenario(raw_input="Fed raises rates 50bps",
                        description="The Fed raises its policy rate by 50 basis points.",
                        policy_type="monetary", magnitude="moderate")
    report = SimulationReport(scenario_summary="Fed hike", synthesis="Yields rise, equities fall.")

    memory.store_simulation(scenario, report)
    found = memory.retrieve_similar(scenario, k=1)

    assert memory.scenarios.count() == 1
    assert "Yields rise" in found[0]["document"]
