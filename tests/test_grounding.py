"""The check behind "every number must come from the data or the scenario"."""

import pytest

import config
from agents.base import AnalysisOutput
from models.report import ScenarioOutcome
from orchestrator import Orchestrator, ParsedScenario, ReportDraft
from tests.data_fakes import fake_builder
from models.report import AgentAnalysis
from models.scenario import Scenario
from orchestrator import OrchestratorContext
from tests.fakes import FakeLLM, action
from utils import grounding
from utils.grounding import figures, unsupported_figures


def stated(text: str) -> list[str]:
    return [figure.text for figure in figures(text)]


@pytest.fixture(autouse=True)
def the_year_is_2026(monkeypatch):
    """Which bare numbers read as calendar years depends on today's date."""
    monkeypatch.setattr(grounding, "_this_year", lambda: 2026)


# ── What counts as a figure ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Immediate (0-1 week), short-term (1-3 months), medium-term (3-12 months), over 12 months.",
    "The 10Y and 2Y yields, the 20/50/200-day averages, the 52-week range, 5 days ago, 3 sessions.",
    "On 2026-11-13, in Q4 2026, by the end of 2027.",
    "DGS10, T10Y2Y and BAMLH0A0HYM2; tier1 agents; XLK.",
    "The S&P 500, the Russell 2000, the Nasdaq 100 and a 60/40 portfolio.",
])
def test_durations_dates_identifiers_and_index_names_are_not_figures(text):
    assert stated(text) == []


def test_figures_are_found_with_their_currency_and_percent_signs():
    assert stated("The index could fall to 6,000-6,500 while yields go from 5.15% to 5.5-6%.") == \
        ["6,000", "6,500", "5.15%", "5.5", "6%"]
    assert stated("Oil at $150 adds 0.3-0.4% to CPI after a 50bps hike; -0.40% on the day.") == \
        ["$150", "0.3", "0.4%", "50", "0.40%"]
    assert stated("Payrolls rose 165 thousand to 159,293. VIX spikes to 30. Gold near 2,050.") == \
        ["165", "159,293", "30", "2,050"]


def test_a_number_that_only_looks_like_a_year_is_still_a_figure():
    assert stated("The index is at 2,026 points and the spread is 2026.5.") == ["2,026", "2026.5"]
    assert stated("A 2000 bps move.") == ["2000"]


@pytest.mark.parametrize("text, expected", [
    ("DAX 18,000", ["18,000"]),                       # a level after an index name is a figure
    ("The Nikkei 40,000 mark and Dow 40,000", ["40,000", "40,000"]),
    ("S&P 500 at 7,500", ["7,500"]),
    ("the Nasdaq 100 and Russell 2000 and FTSE 250", []),
    ("S&P 500,000 contracts", ["500,000"]),
])
def test_index_names_are_removed_but_levels_after_them_are_kept(text, expected):
    assert stated(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("inventories fell by 3M barrels", ["3"]),        # three million
    ("the 3M Treasury yield and the 3M bill", []),    # three-month
    ("the 10Y, 2y and a 5d window", []),
    ("a 5-year high, with the 5-year yield at 4.98%", ["4.98%"]),
    ("up 20% in 2 days", ["20%"]),
    ("between 10 and 20 percent, or 1.5x", ["10", "20", "1.5"]),
    ("a range of 5.5–6% (about 0.5 above)", ["5.5", "6%", "0.5"]),
    ("$2 trillion of issuance", ["$2"]),
])
def test_tenors_and_durations_versus_amounts(text, expected):
    assert stated(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("2026 will be hard; the 2027 outlook; fiscal 2026/27; FY2025; in 1987; since 2008; March 2020.", []),
    ("gold at 2050 and the index at 2000", ["2050", "2000"]),   # not near today, no year word before them
    ("a level of 2030", []),                                    # the documented hole: reads as a year
    # Words such as "of", "by", "from" and "after" introduce amounts as often as years.
    ("payrolls of 2000", ["2000"]),
    ("a rise of 2000 jobs", ["2000"]),
    ("up by 2000 basis points", ["2000"]),
    ("from 2000 to 2100", ["2000", "2100"]),
    ("after 2050 barrels were released", ["2050"]),
    # A counted thing after the number makes it an amount even after "in" or near today.
    ("in 2000 shares", ["2000"]),
    ("2026 contracts changed hands", ["2026"]),
    ("measured in 2020 dollars", []),                           # a price base year, not an amount
])
def test_years_are_recognised_by_context_or_by_being_close_to_today(text, expected):
    assert stated(text) == expected


def test_list_numbering_and_stage_labels_are_not_figures():
    numbered = "1. Rates rise.\n2) Equities fall 5%.\n  3. Tier 1 agents ran in round 2."
    assert stated(numbered) == ["5%"]


def test_decimals_are_recorded_as_written():
    by_text = {figure.text: figure for figure in figures("5 then 5.1 then 5.15% then 7,561.67")}
    assert [by_text[t].decimals for t in ("5", "5.1", "5.15%", "7,561.67")] == [0, 1, 2, 2]
    assert by_text["7,561.67"].value == 7561.67


# ── What counts as supported ─────────────────────────────────────────────────

SOURCES = [
    "10-year Treasury yield (^TNX): 5.15% on 2026-11-13, 1 day +0.05 pp, 5 days +0.29 pp",
    "S&P 500 (^GSPC): 7,561.67 on 2026-11-13, 1 day -0.40%",
    "Oil prices spike to $150 a barrel",
]


@pytest.mark.parametrize("claim", [
    "The yield is 5.15%.",
    "The yield is about 5.1%.",          # rounded to one decimal
    "The yield is near 5%.",             # rounded to a whole number
    "The index stands at 7,560.",        # rounded level
    "It fell 0.4% on the day.",          # sign and trailing zero do not matter
    "Oil at $150.",                      # from the scenario
    "Yields rose 0.29 points in a week.",
])
def test_figures_that_round_to_something_shown_are_supported(claim):
    assert unsupported_figures(claim, SOURCES) == []


@pytest.mark.parametrize("claim, missing", [
    ("The yield could reach 5.5%.", ["5.5%"]),
    ("The index may fall to 7,500.", ["7,500"]),
    ("The index may fall to 6000.", ["6000"]),
    ("VIX spikes to 30.", ["30"]),
    ("A range of 6,000-6,500, then 6,000 again.", ["6,000", "6,500"]),   # each reported once, in order
])
def test_figures_from_nowhere_are_reported(claim, missing):
    assert unsupported_figures(claim, SOURCES) == missing


def test_durations_and_numbering_in_the_sources_support_nothing():
    """Otherwise "5 days" in the data, or "5." in a prompt, would let any "5" through."""
    assert unsupported_figures("It fell 5.", ["5 days ago"]) == ["5"]
    assert unsupported_figures("It fell 5.", ["Steps:\n5. Assess the reaction function."]) == ["5"]
    assert unsupported_figures("It fell 5.", ["It fell 5 points."]) == []


def test_the_scenario_supports_the_same_amount_in_other_units():
    scenario = "The Fed raises rates by 50bps"
    assert unsupported_figures("a 0.5% hike", [scenario]) == ["0.5%"]
    assert unsupported_figures("a 0.5% hike", [scenario], restatable=[scenario]) == []
    assert unsupported_figures("a 25 basis point cut", [], restatable=["The Fed cuts by a quarter point"]) == []
    assert unsupported_figures("a 50 basis point cut", [], restatable=["a half-point cut"]) == []
    assert unsupported_figures("a 75 basis point cut", [], restatable=["a quarter point cut"]) == ["75"]
    assert unsupported_figures("0.5%, or 50 bps", [], restatable=["rates rise half a percent"]) == []


def test_a_number_word_counts_only_next_to_a_unit():
    """ "one of the reasons" must not make 1, 100 and 0.01 supported."""
    prose = "This is one of the two reasons the Fed moved."
    assert unsupported_figures("a 100 point drop and a 2% fall", [], restatable=[prose]) == ["100", "2%"]
    assert unsupported_figures("a 100 bps move", [], restatable=["a one percentage point move"]) == []


def test_with_no_sources_every_figure_is_unsupported():
    assert unsupported_figures("Up 3% to 105.", []) == ["3%", "105"]
    assert unsupported_figures("No numbers here.", []) == []


# ── In the report ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def snapshots_go_to_a_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))


def run(analysis_text: str, with_data: bool = True, summary: str = "summary",
        parsed: str = "The Fed raises its policy rate by 50 basis points.", scenario="Fed raises rates 50bps"):
    """One central-bank analysis whose immediate-impact text is `analysis_text`."""
    actions = ([action("fetch_market_data")] if with_data else []) + [action("consult_specialist", ["central_bank"])]
    llm = FakeLLM(actions)
    canned = llm.complete_structured

    def reply(system_prompt, user_message, schema, fast=False):
        result = canned(system_prompt, user_message, schema, fast)
        if schema is AnalysisOutput:
            return result.model_copy(update={"immediate_impact": analysis_text})
        if schema is ReportDraft:
            return result.model_copy(update={"scenario_summary": summary})
        if schema is ParsedScenario:
            return result.model_copy(update={"description": parsed})
        return result

    llm.complete_structured = reply
    orch = Orchestrator(llm=llm, enable_memory=False, data_builder=fake_builder() if with_data else None,
                        enable_market_data=with_data)
    return orch.run(scenario, parallel=False).quality_warnings


def figure_warnings(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "figure(s) not found" in w or "scenario parser" in w]


def test_an_invented_level_is_named_in_the_report():
    warnings = figure_warnings(run("The S&P 500 will drop to 6,000 and the VIX will hit 45."))
    assert warnings == [
        "Central Bank Agent states 2 figure(s) not found in the data snapshot or the scenario: 6,000, 45."
    ]


def test_figures_from_the_snapshot_the_scenario_or_the_agents_own_instructions_pass():
    # 5.15 is the 10-year yield in the recorded snapshot, 50 is in the scenario,
    # and 25 and 150 are anchors written in the central bank agent's own prompt.
    text = "With the 10-year yield at 5.15%, a 50bps move is urgent; 25bps would be routine and 150bps severe."
    assert figure_warnings(run(text)) == []


def test_without_a_snapshot_the_wording_says_none_was_fetched():
    warnings = figure_warnings(run("The 10-year yield is 5.15%.", with_data=False))
    assert warnings == [
        "Central Bank Agent states 1 figure(s) not found in the scenario (no data snapshot was fetched): 5.15%."
    ]


def test_the_reports_own_summary_sections_are_checked_too():
    warnings = figure_warnings(run("No figures.", summary="Equities fall 12% and the index reaches 6,650."))
    assert warnings == [
        "The report's summary sections state 2 figure(s) not found in the data snapshot or the scenario: 12%, 6,650."
    ]


def test_scenario_matrix_probabilities_are_not_treated_as_data():
    """FakeLLM's matrix carries 60%, 20% and 20%; they are estimates, not claims about data."""
    assert figure_warnings(run("No figures.")) == []


def test_a_figure_added_by_the_scenario_parser_is_reported_but_not_blamed_on_agents():
    warnings = figure_warnings(run(
        "With inflation at 6.2%, policy stays tight.",
        parsed="The Fed raises its policy rate by 50 basis points while inflation runs at 6.2%.",
    ))
    assert len(warnings) == 1
    assert warnings[0].startswith("The scenario parser introduced 6.2%, which your scenario did not state.")


def test_a_restatement_by_the_parser_is_not_reported():
    warnings = figure_warnings(run(
        "A 25bps cut is routine.", with_data=False,
        scenario="The Fed cuts rates by a quarter point",
        parsed="The Federal Reserve lowers its policy rate by 25 basis points (0.25%).",
    ))
    assert warnings == []


def test_small_whole_numbers_are_not_excused_by_the_numbering_in_an_agents_prompt():
    """The central bank prompt numbers its steps 1 to 5; that must not support "3%"."""
    warnings = figure_warnings(run("Inflation rises 3% and unemployment reaches 7%.", with_data=False))
    assert warnings == [
        "Central Bank Agent states 2 figure(s) not found in the scenario (no data snapshot was fetched): 3%, 7%."
    ]


def test_figures_are_still_reported_when_the_synthesis_step_fails():
    llm = FakeLLM([action("consult_specialist", ["central_bank"])], fail_on={"ReportDraft"})
    canned = llm.complete_structured
    llm.complete_structured = lambda s, u, schema, fast=False: (
        canned(s, u, schema, fast).model_copy(update={"immediate_impact": "The index falls to 6,000."})
        if schema is AnalysisOutput else canned(s, u, schema, fast)
    )
    report = Orchestrator(llm=llm, enable_memory=False, enable_market_data=False).run("Fed raises rates 50bps", parallel=False)

    assert "synthesis failed" in report.synthesis
    assert figure_warnings(report.quality_warnings) == [
        "Central Bank Agent states 1 figure(s) not found in the scenario (no data snapshot was fetched): 6,000."
    ]


def test_an_analysis_from_an_unknown_agent_is_checked_without_its_instructions():
    orch = Orchestrator(llm=FakeLLM(), enable_memory=False, enable_market_data=False)
    ctx = OrchestratorContext("Fed raises rates 50bps", "US")
    ctx.scenario = Scenario(raw_input=ctx.user_input, description=ctx.user_input)
    stranger = AgentAnalysis(
        agent_id="not_registered", agent_name="Guest Agent", domain="", immediate_impact="A 50bps move, then 150bps.",
        short_term_impact="", medium_term_impact="", cross_domain_effects="",
    )
    assert orch._grounding_warnings(ctx, [stranger], narrative="") == [
        "Guest Agent states 1 figure(s) not found in the scenario (no data snapshot was fetched): 150."
    ]


def test_long_lists_of_figures_are_cut_short():
    text = "Levels: " + ", ".join(str(9000 + 10 * i) for i in range(12)) + "."
    (warning,) = figure_warnings(run(text))
    assert "states 12 figure(s)" in warning and warning.endswith("9070 and 4 more.")


def test_warnings_appear_in_the_rendered_report():
    actions = [action("consult_specialist", ["central_bank"])]
    llm = FakeLLM(actions)
    canned = llm.complete_structured
    llm.complete_structured = lambda s, u, schema, fast=False: (
        canned(s, u, schema, fast).model_copy(update={"immediate_impact": "The index falls to 6,000."})
        if schema is AnalysisOutput else canned(s, u, schema, fast)
    )
    report = Orchestrator(llm=llm, enable_memory=False, enable_market_data=False).run("Fed raises rates 50bps", parallel=False)
    assert "## Quality Warnings" in report.to_markdown() and "6,000" in report.to_markdown().split("## Policy")[0]
    assert all(isinstance(row, ScenarioOutcome) for row in report.scenario_matrix)
