"""How the research-mode orchestrator uses the data layer."""

import pytest

import config
from agents.base import OWN_WORDS_RULE
from data.providers import DataError
from data.render import GROUNDING_RULE
from data.snapshot import load_snapshot
from orchestrator import Orchestrator, SYNTHESIS_SYSTEM_PROMPT
from tests.data_fakes import FakeMarket, fake_builder
from tests.fakes import FakeLLM, action


@pytest.fixture(autouse=True)
def snapshots_go_to_a_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return tmp_path / "snapshots"


def run(actions, builder=None, scenario="Fed raises rates 50bps", **orch_kwargs):
    llm = FakeLLM(actions)
    orch = Orchestrator(llm=llm, enable_memory=False, data_builder=builder or fake_builder(), **orch_kwargs)
    return orch.run(scenario, parallel=False), llm


FETCH_THEN_CONSULT = [action("fetch_market_data"), action("consult_specialist", ["central_bank"])]


def test_agents_get_the_snapshot_as_data_with_the_grounding_rule():
    _, llm = run(FETCH_THEN_CONSULT)
    prompt = next(msg for name, msg in llm.calls if name == "AnalysisOutput")

    assert "DATA SNAPSHOT (last completed US session: 2026-11-13" in prompt
    assert "Consumer price index (CPIAUCSL)" in prompt
    assert GROUNDING_RULE in prompt
    # Market data is not another agent's opinion and must not be presented as one.
    assert "UPSTREAM AGENT SIGNALS" not in prompt and OWN_WORDS_RULE not in prompt


def test_data_and_upstream_signals_are_kept_in_separate_sections():
    _, llm = run([
        action("fetch_market_data"),
        action("consult_specialist", ["central_bank"]),
        action("consult_specialist", ["fixed_income"], tier="tier2"),
    ])
    downstream = [msg for name, msg in llm.calls if name == "AnalysisOutput"][1]

    data_at = downstream.index("DATA SNAPSHOT")
    upstream_at = downstream.index("UPSTREAM AGENT SIGNALS")
    assert data_at < upstream_at
    assert "Consumer price index" not in downstream[upstream_at:]


def test_report_records_the_snapshot_and_the_file_is_on_disk(snapshots_go_to_a_temp_dir):
    report, _ = run(FETCH_THEN_CONSULT)

    source = report.data_sources[0]
    assert source.startswith("Data snapshot 2026-11-13_") and "session 2026-11-13" in source
    saved = list(snapshots_go_to_a_temp_dir.glob("2026-11-13/*.json"))
    assert len(saved) == 1 and str(saved[0]) in source
    assert load_snapshot(saved[0]).session_date.isoformat() == "2026-11-13"


def test_data_problems_reach_the_report_as_quality_warnings():
    report, _ = run([action("fetch_market_data", tickers=["AAPL", "ZZZZ"]),
                     action("consult_specialist", ["equity_market"])])

    assert "Data: ZZZZ: no price data was returned." in report.quality_warnings
    assert any(w.startswith("Data: FRED returned 404") for w in report.quality_warnings)
    assert "tickers AAPL, ZZZZ" in report.data_sources[0]


def test_requested_tickers_are_passed_to_the_data_layer():
    market = FakeMarket()
    _, llm = run([action("fetch_market_data", tickers=["aapl"]), action("consult_specialist", ["equity_market"])],
                 builder=fake_builder(market=market))

    assert market.requested[0][0] == "AAPL"
    prompt = next(msg for name, msg in llm.calls if name == "AnalysisOutput")
    assert "AAPL (Apple Inc.), session 2026-11-13:" in prompt


@pytest.mark.parametrize("scenario", [
    "The Fed raises interest rates by 50bps unexpectedly",
    "Congress passes a $2 trillion fiscal stimulus",
    "Oil prices spike to $150 a barrel after a supply disruption",
])
def test_every_kind_of_scenario_gets_the_same_full_data(scenario):
    """The old keyword routing gave fiscal scenarios nothing and commodity scenarios an error."""
    _, llm = run(FETCH_THEN_CONSULT, scenario=scenario)
    prompt = next(msg for name, msg in llm.calls if name == "AnalysisOutput")
    assert "Treasury yields:" in prompt and "Macro series (FRED):" in prompt
    assert "Error" not in prompt and "validation error" not in prompt


def test_synthesis_gets_the_data_and_the_grounding_rule():
    _, llm = run(FETCH_THEN_CONSULT)
    assert llm.system_prompts["ReportDraft"] == SYNTHESIS_SYSTEM_PROMPT + "\n\n" + GROUNDING_RULE
    assert "DATA SNAPSHOT" in next(msg for name, msg in llm.calls if name == "ReportDraft")


def test_without_a_fetch_agents_are_not_told_about_a_snapshot():
    _, llm = run([action("consult_specialist", ["central_bank"])])
    prompt = next(msg for name, msg in llm.calls if name == "AnalysisOutput")
    assert "DATA SNAPSHOT" not in prompt and GROUNDING_RULE not in prompt
    assert llm.system_prompts["ReportDraft"] == SYNTHESIS_SYSTEM_PROMPT


def test_a_data_failure_does_not_end_the_analysis():
    class Broken:
        def build(self, tickers):
            raise DataError("source is down")

    report, llm = run(FETCH_THEN_CONSULT, builder=Broken())

    assert [a.agent_id for a in report.agent_analyses] == ["central_bank"]
    assert report.data_sources == []
    second_step = [msg for name, msg in llm.calls if name == "NextAction"][1]
    assert "Market data could not be fetched: source is down" in second_step
    assert "Market data fetched: yes" in second_step      # and it is not retried


def test_market_data_can_be_switched_off():
    llm = FakeLLM(FETCH_THEN_CONSULT)
    orch = Orchestrator(llm=llm, enable_memory=False, enable_market_data=False)
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert "Market data: OFF (disabled in config)" in orch.status_lines()
    assert "MARKET DATA: NOT AVAILABLE" in llm.system_prompts["NextAction"]
    assert report.data_sources == []


def test_status_line_says_where_snapshots_go():
    orch = Orchestrator(llm=FakeLLM(), enable_memory=False, data_builder=fake_builder())
    assert any(line.startswith("Market data: on (Yahoo Finance and FRED") for line in orch.status_lines())
