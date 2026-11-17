"""The orchestrator loop, driven by a scripted model. No real model is called."""

import pytest

import orchestrator as orchestrator_module
from orchestrator import Orchestrator
from tests.fakes import FakeLLM, action


def make(actions=None, **llm_kwargs) -> tuple[Orchestrator, FakeLLM]:
    llm = FakeLLM(actions, **llm_kwargs)
    return Orchestrator(llm=llm, enable_memory=False, enable_market_data=False), llm


def ids(report) -> list[str]:
    return [a.agent_id for a in report.agent_analyses]


def test_simple_run_produces_a_full_report():
    orch, llm = make([action("consult_specialist", ["central_bank", "fixed_income"])])
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert ids(report) == ["central_bank", "fixed_income"]
    assert report.synthesis == "takeaways"
    assert len(report.scenario_matrix) == 3
    assert llm.count("ParsedScenario") == 1


def test_parallel_run_keeps_requested_agent_order():
    orch, _ = make([action("consult_specialist", ["forex", "central_bank", "sentiment"])])
    report = orch.run("Fed raises rates 50bps", parallel=True)
    assert ids(report) == ["forex", "central_bank", "sentiment"]


def test_deliberation_keeps_agents_that_did_not_take_part():
    orch, _ = make([
        action("consult_specialist", ["central_bank", "fixed_income", "forex"]),
        action("run_deliberation", ["central_bank", "fixed_income"]),
    ])
    report = orch.run("Fed raises rates 50bps", parallel=False)

    by_id = {a.agent_id: a for a in report.agent_analyses}
    assert set(by_id) == {"central_bank", "fixed_income", "forex"}
    assert by_id["central_bank"].immediate_impact == "revised immediate"
    assert by_id["fixed_income"].immediate_impact == "revised immediate"
    assert by_id["forex"].immediate_impact == "original immediate"


def test_submit_before_any_analysis_is_rejected():
    orch, llm = make([action("submit_report"), action("consult_specialist", ["equity_market"])])
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert ids(report) == ["equity_market"]
    step_messages = [msg for name, msg in llm.calls if name == "NextAction"]
    assert "REJECTED" in step_messages[1]


def test_an_agent_is_not_consulted_twice():
    orch, llm = make([
        action("consult_specialist", ["central_bank"]),
        action("consult_specialist", ["central_bank"]),
    ])
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert ids(report) == ["central_bank"]
    assert llm.count("AnalysisOutput") == 1


def test_unknown_agent_ids_are_reported_back_to_the_model():
    orch, llm = make([
        action("consult_specialist", ["no_such_agent"]),
        action("consult_specialist", ["sentiment"]),
    ])
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert ids(report) == ["sentiment"]
    step_messages = [msg for name, msg in llm.calls if name == "NextAction"]
    assert "No valid agent IDs" in step_messages[1]


def test_debate_runs_once_even_if_requested_twice():
    orch, llm = make([
        action("consult_specialist", ["central_bank", "equity_market"]),
        action("run_debate"),
        action("run_debate"),
    ])
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert llm.count("DebateVerdict") == 1
    assert "Debate calibration" in report.synthesis


def test_skip_debate_preference_is_enforced_in_code():
    orch, llm = make([action("consult_specialist", ["central_bank"]), action("run_debate")])
    orch.run("Fed raises rates 50bps", parallel=False, preferences={"skip_debate": True})
    assert llm.count("DebateArgument") == 0


def test_loop_stops_at_the_iteration_limit(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "MAX_ORCHESTRATOR_ITERATIONS", 3)
    never_submits = [action("consult_specialist", ["central_bank"])] + [action("search_memory")] * 10
    orch, llm = make(never_submits)
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert llm.count("NextAction") == 3
    assert ids(report) == ["central_bank"]
    assert report.synthesis == "takeaways"


def test_a_failing_decision_step_still_yields_a_report():
    orch, llm = make([action("consult_specialist", ["central_bank"])])
    original = llm.complete_structured
    seen = {"steps": 0}

    def flaky(system_prompt, user_message, schema, fast=False):
        if schema.__name__ == "NextAction":
            seen["steps"] += 1
            if seen["steps"] == 2:
                raise orchestrator_module.LLMError("model went away")
        return original(system_prompt, user_message, schema, fast)

    llm.complete_structured = flaky
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert ids(report) == ["central_bank"]
    assert report.synthesis == "takeaways"


def test_failed_synthesis_keeps_the_specialist_analyses():
    orch, _ = make([action("consult_specialist", ["central_bank"])], fail_on={"ReportDraft"})
    report = orch.run("Fed raises rates 50bps", parallel=False)

    assert ids(report) == ["central_bank"]
    assert "synthesis failed" in report.synthesis


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
def test_one_failing_agent_does_not_lose_the_others(parallel):
    orch, llm = make([action("consult_specialist", ["central_bank", "forex", "sentiment"])])
    original = llm.complete_structured

    def forex_fails(system_prompt, user_message, schema, fast=False):
        if schema.__name__ == "AnalysisOutput" and "Currency & Forex Agent" in system_prompt:
            raise orchestrator_module.LLMError("forex model error")
        return original(system_prompt, user_message, schema, fast)

    llm.complete_structured = forex_fails
    report = orch.run("Fed raises rates 50bps", parallel=parallel)
    assert ids(report) == ["central_bank", "sentiment"]


def test_skipped_debate_is_not_reported_to_the_model_as_run():
    orch, llm = make([action("consult_specialist", ["central_bank"]), action("run_debate")])
    orch.run("Fed raises rates 50bps", parallel=False, preferences={"skip_debate": True})

    step_messages = [msg for name, msg in llm.calls if name == "NextAction"]
    assert "Debate run: not allowed" in step_messages[-1]
    assert "Debate run: yes" not in step_messages[-1]


def test_status_lines_say_what_is_off():
    orch, _ = make()
    status = "\n".join(orch.status_lines())
    assert "Memory: OFF" in status
    assert "Market data: OFF" in status


def test_memory_failure_is_reported_not_hidden(monkeypatch):
    import memory.store

    def broken(*args, **kwargs):
        raise ImportError("native module missing")

    monkeypatch.setattr(memory.store, "SimulationMemory", broken)
    orch = Orchestrator(llm=FakeLLM(), enable_memory=True, enable_market_data=False)

    assert orch.memory is None
    assert "native module missing" in "\n".join(orch.status_lines())
