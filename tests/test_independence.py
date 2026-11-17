"""Agents must give their own view: copied text is undone or flagged, and the
debate's vocabulary cannot be confused with market direction."""

from typing import get_args

import pytest

from agents.base import AnalysisOutput, DeliberationOutput, OWN_WORDS_RULE
from models.report import AgentAnalysis
from orchestrator import DebateVerdict, Orchestrator, SYNTHESIS_SYSTEM_PROMPT
from tests.fakes import FakeLLM, action
from utils.text_overlap import copied_fraction, copied_words

FOREX_VIEW = (  # 26 words
    "The dollar appreciates sharply against the euro and the yen as rate differentials widen. "
    "Carry trades funded in yen are unwound over the following two weeks."
)
EQUITY_VIEW = (  # 28 words
    "Growth stocks fall harder than value stocks because their cash flows are further away. "
    "Banks benefit from wider net interest margins in the first quarter after the hike."
)
EQUITY_OWN_REVISION = "Exporters with large overseas revenue face an earnings headwind from the stronger currency."
LONG_PASSAGE = (  # 39 words
    FOREX_VIEW + " Emerging market central banks sell reserves to slow the decline of their currencies."
)
OWN_TEXT_A = (  # about 80 words that appear nowhere else
    "Regional lenders tighten underwriting standards while mortgage applications slow across most metropolitan "
    "areas. Corporate treasurers postpone refinancing plans and draw on existing credit lines instead. Small "
    "business confidence surveys soften as input financing becomes more expensive each quarter. Pension funds "
    "rebalance toward shorter maturities to limit mark to market losses. Insurers face pressure on their legacy "
    "portfolios but gain from reinvesting new premiums at better levels. Municipal issuers delay infrastructure "
    "offerings until funding conditions settle down again."
)
OWN_TEXT_B = (  # about 80 different words
    "Shipping rates ease as importers work through inventories built earlier in the year. Semiconductor makers "
    "keep capital budgets intact because orders are booked far ahead. Airlines hedge more of their fuel needs "
    "after the recent swings in crude. Grocery chains accept thinner margins to defend volumes against discount "
    "rivals. Utilities file for higher allowed returns to offset their rising cost of debt. Software vendors "
    "lengthen payment terms to close deals before the fiscal period ends."
)


# ── text_overlap ─────────────────────────────────────────────────────────────

def test_short_shared_phrases_are_not_treated_as_copying():
    assert copied_fraction("The dollar strengthens. Yields rise.", ["The dollar strengthens. Yields rise."]) == 0.0


def test_copied_words_counts_the_lifted_part():
    assert copied_words(FOREX_VIEW, [FOREX_VIEW]) == (26, 26)
    assert copied_words(EQUITY_VIEW, [FOREX_VIEW]) == (0, 28)
    assert copied_words(EQUITY_OWN_REVISION + " " + FOREX_VIEW, [FOREX_VIEW]) == (26, 39)


def test_matching_ignores_case_spacing_and_punctuation_style():
    shouted = FOREX_VIEW.upper().replace(" ", "  ").replace(".", " !")
    assert copied_fraction(shouted, [FOREX_VIEW]) == 1.0

    source = "The risk-free rate, it's fair to say, rose by 5.50% in the U.S. after the surprise hike"
    restyled = "the risk free rate it’s fair to say rose by 5.50 % in the US after the surprise hike"
    assert copied_fraction(restyled.replace("US", "U S"), [source]) == 1.0


def test_a_lightly_edited_passage_still_counts_as_copied():
    """Joining two lifted sentences with a comma defeats sentence matching, not this."""
    joined = FOREX_VIEW.replace(" widen. Carry trades", " widen, so carry trades")
    assert copied_fraction(joined, [FOREX_VIEW]) > 0.8


def test_an_agents_own_earlier_text_does_not_count_as_copied():
    assert copied_fraction(FOREX_VIEW, [FOREX_VIEW], own=[FOREX_VIEW]) == 0.0
    assert copied_fraction(FOREX_VIEW, [FOREX_VIEW], own=FOREX_VIEW) == 0.0  # a bare string is one unit


def test_a_run_never_spans_two_units():
    eight = "one two three four five six seven eight"
    assert copied_fraction(eight, ["one two three four", "five six seven eight"]) == 0.0
    assert copied_fraction(eight, [eight]) == 1.0


def test_line_breaks_inside_a_field_do_not_hide_copying():
    bulleted = "- The dollar appreciates sharply\n- against the euro and the yen\n- as rate differentials widen."
    assert copied_fraction(bulleted, [FOREX_VIEW]) == 1.0


CHINESE_PASSAGE = "美联储意外加息五十个基点导致美元对欧元和日元大幅升值并引发新兴市场资本外流"
CHINESE_PASSAGE_WORDS = len(CHINESE_PASSAGE) / 2   # a Chinese character counts as half a word


def test_chinese_text_needs_a_longer_run():
    sentence = "美联储意外加息导致美元对主要货币大幅升值"  # 19 characters
    assert copied_fraction(sentence, [f"此前的分析认为{sentence}并引发资本外流"]) == 1.0
    assert copied_fraction(sentence, ["财政刺激推高长期国债收益率并挤出私人投资"]) == 0.0
    stock_phrase = "美联储加息导致美元升值"  # 11 characters: common wording, not evidence of copying
    assert copied_fraction(stock_phrase, [stock_phrase]) == 0.0


def test_a_chinese_character_counts_as_half_a_word():
    assert copied_words("美元升值", ["无关"]) == (0, 2)
    assert copied_words(CHINESE_PASSAGE, [CHINESE_PASSAGE]) == (CHINESE_PASSAGE_WORDS, CHINESE_PASSAGE_WORDS)


def test_a_copied_passage_is_found_whatever_language_surrounds_it():
    """The copier's field and the source need not be in the same language mix."""
    mixed_field = OWN_TEXT_A + " " + CHINESE_PASSAGE        # mostly English, Chinese passage pasted in
    covered, _ = copied_words(mixed_field, [CHINESE_PASSAGE])
    assert covered == CHINESE_PASSAGE_WORDS

    covered, total = copied_words(CHINESE_PASSAGE, [mixed_field])   # Chinese-only field, mixed source
    assert (covered, total) == (CHINESE_PASSAGE_WORDS, CHINESE_PASSAGE_WORDS)

    english_in_chinese = "此前的分析认为 " + FOREX_VIEW + " 这一判断仍然成立"
    assert copied_words(FOREX_VIEW, [english_in_chinese]) == (26, 26)


def test_own_earlier_text_is_excluded_across_language_mixes():
    mixed_field = OWN_TEXT_A + " " + CHINESE_PASSAGE
    assert copied_words(mixed_field, [CHINESE_PASSAGE], own=[CHINESE_PASSAGE])[0] == 0


# ── flagging pairs in the final report ───────────────────────────────────────

def analysis(name: str, cross_domain: str) -> AgentAnalysis:
    return AgentAnalysis(
        agent_id=name, agent_name=name, domain="", immediate_impact="short", short_term_impact="short",
        medium_term_impact="short", cross_domain_effects=cross_domain,
    )


def warnings_for(*analyses: AgentAnalysis) -> list[str]:
    orch = Orchestrator(llm=FakeLLM(), enable_memory=False, enable_market_data=False)
    return orch._shared_text_warnings(list(analyses))


def test_a_pair_is_flagged_when_the_first_agent_is_the_one_that_copied():
    # Only the first agent's field is mostly copied; the second agent's own
    # field is long, so the same passage is a small share of it.
    found = warnings_for(analysis("First", EQUITY_VIEW), analysis("Second", EQUITY_VIEW + " " + OWN_TEXT_A))
    assert len(found) == 1 and "First and Second" in found[0]
    assert "100% of one agent's cross-domain text" in found[0]


def test_a_long_passage_is_flagged_even_as_a_small_share_of_a_long_field():
    first = analysis("First", OWN_TEXT_A + " " + LONG_PASSAGE)
    second = analysis("Second", LONG_PASSAGE + " " + OWN_TEXT_B)
    covered, total = copied_words(first.cross_domain_effects, [second.cross_domain_effects])
    assert covered == 39 and covered / total < 0.4   # below the share rule on both sides

    found = warnings_for(first, second)
    assert len(found) == 1 and "about 39 words" in found[0]


def test_one_stock_sentence_in_a_short_field_is_not_flagged():
    stock = "The unexpected hike will likely cause a sharp sell-off in risk assets."  # 13 words
    first = analysis("First", stock + " Credit spreads widen as well.")
    second = analysis("Second", stock + " " + OWN_TEXT_A)
    covered, total = copied_words(first.cross_domain_effects, [second.cross_domain_effects])
    assert covered / total > 0.4 and covered < 20     # a large share, but too few words to mean anything

    assert warnings_for(first, second) == []


def test_independent_agents_get_no_warning():
    assert warnings_for(analysis("First", OWN_TEXT_A), analysis("Second", OWN_TEXT_B)) == []


# ── deliberation ─────────────────────────────────────────────────────────────

def distinct_llm(deliberation: dict[str, str], actions) -> FakeLLM:
    """FakeLLM whose forex and equity agents write different text, and whose
    deliberation rewrites the cross-domain field with the text given per agent."""
    llm = FakeLLM(actions)
    canned = llm.complete_structured
    views = {"Currency & Forex Agent": FOREX_VIEW, "Equity Market Agent": EQUITY_VIEW}

    def reply(system_prompt, user_message, schema, fast=False):
        agent = next((name for name in views if name in system_prompt), None)
        if schema is AnalysisOutput and agent:
            canned(system_prompt, user_message, schema, fast)
            return AnalysisOutput(
                immediate_impact="short", short_term_impact="short", medium_term_impact="short",
                cross_domain_effects=views[agent], confidence="moderate",
            )
        if schema is DeliberationOutput and agent:
            canned(system_prompt, user_message, schema, fast)
            return DeliberationOutput(
                agreements="some", disagreements="None", revised_immediate_impact="short",
                revised_medium_term_impact="short",
                revised_cross_domain_effects=deliberation[agent], confidence="high",
            )
        return canned(system_prompt, user_message, schema, fast)

    llm.complete_structured = reply
    return llm


def run(llm: FakeLLM, parallel: bool = False):
    orch = Orchestrator(llm=llm, enable_memory=False, enable_market_data=False)
    report = orch.run("Fed raises rates 50bps", parallel=parallel)
    return report, {a.agent_id: a for a in report.agent_analyses}


CONSULT_AND_DELIBERATE = [
    action("consult_specialist", ["forex", "equity_market"]),
    action("run_deliberation", ["forex", "equity_market"]),
]


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
def test_a_revision_that_copies_another_agent_is_undone_and_reported(parallel):
    llm = distinct_llm(
        {"Currency & Forex Agent": FOREX_VIEW,                     # keeps its own view
         "Equity Market Agent": EQUITY_VIEW + " " + FOREX_VIEW},   # pastes forex's text in
        CONSULT_AND_DELIBERATE,
    )
    report, by_id = run(llm, parallel)

    assert by_id["equity_market"].cross_domain_effects == EQUITY_VIEW
    assert by_id["forex"].cross_domain_effects == FOREX_VIEW
    assert len(report.quality_warnings) == 1
    assert "Equity Market Agent" in report.quality_warnings[0]
    assert "cross-domain" in report.quality_warnings[0]
    assert "## Quality Warnings" in report.to_markdown()


def test_a_revision_in_the_agents_own_words_is_kept():
    llm = distinct_llm(
        {"Currency & Forex Agent": FOREX_VIEW,
         "Equity Market Agent": EQUITY_VIEW + " " + EQUITY_OWN_REVISION},
        CONSULT_AND_DELIBERATE,
    )
    report, by_id = run(llm)

    assert EQUITY_OWN_REVISION in by_id["equity_market"].cross_domain_effects
    assert report.quality_warnings == []
    assert "Quality Warnings" not in report.to_markdown()


def test_agents_sharing_a_passage_without_deliberation_are_flagged():
    """A downstream agent can also lift text from the upstream signals it is shown."""
    llm = distinct_llm({}, [action("consult_specialist", ["forex", "equity_market"])])
    canned = llm.complete_structured

    def equity_copies_forex(system_prompt, user_message, schema, fast=False):
        if schema is AnalysisOutput and "Equity Market Agent" in system_prompt:
            return AnalysisOutput(
                immediate_impact="short", short_term_impact="short", medium_term_impact="short",
                cross_domain_effects=FOREX_VIEW, confidence="moderate",
            )
        return canned(system_prompt, user_message, schema, fast)

    llm.complete_structured = equity_copies_forex
    report, _ = run(llm)

    assert len(report.quality_warnings) == 1
    warning = report.quality_warnings[0]
    assert "Currency & Forex Agent and Equity Market Agent share a passage of about 26 words" in warning
    assert "100% of one agent's cross-domain text" in warning


def test_agents_are_told_to_use_their_own_words_whenever_they_see_others():
    llm = FakeLLM([
        action("consult_specialist", ["central_bank"]),
        action("consult_specialist", ["fixed_income"], tier="tier2"),
        action("run_deliberation"),
    ])
    Orchestrator(llm=llm, enable_memory=False, enable_market_data=False).run("Fed raises rates 50bps", parallel=False)

    analysis_prompts = [msg for name, msg in llm.calls if name == "AnalysisOutput"]
    assert OWN_WORDS_RULE not in analysis_prompts[0]      # first agent sees nobody else
    assert OWN_WORDS_RULE in analysis_prompts[1]          # downstream agent sees upstream text
    assert OWN_WORDS_RULE in llm.system_prompts["DeliberationOutput"]


# ── debate vocabulary ────────────────────────────────────────────────────────

def test_debate_sides_are_named_by_impact_size_not_market_direction():
    assert get_args(DebateVerdict.model_fields["winner"].annotation) == ("severe", "contained", "mixed")

    llm = FakeLLM([action("consult_specialist", ["central_bank", "equity_market"]), action("run_debate")])
    report = Orchestrator(llm=llm, enable_memory=False, enable_market_data=False).run("Fed raises rates 50bps", parallel=False)

    debate_schemas = ("NextAction", "DebateArgument", "DebateVerdict")
    argument_prompts = [prompt for name, prompt in llm.all_system_prompts if name == "DebateArgument"]
    assert any("SEVERE-IMPACT" in prompt for prompt in argument_prompts)
    assert any("CONTAINED-IMPACT" in prompt for prompt in argument_prompts)

    everything_the_model_saw = " ".join(
        [prompt for name, prompt in llm.all_system_prompts if name in debate_schemas]
        + [msg for name, msg in llm.calls if name in debate_schemas]
    ).lower()
    assert "bull" not in everything_the_model_saw and "bear" not in everything_the_model_saw
    assert "magnitude" in llm.system_prompts["DebateVerdict"]
    assert "Impact size judged mixed" in report.synthesis


def test_synthesis_is_told_what_bull_and_bear_mean():
    assert "BETTER for risk assets" in SYNTHESIS_SYSTEM_PROMPT
    assert "WORSE for risk assets" in SYNTHESIS_SYSTEM_PROMPT

    llm = FakeLLM([action("consult_specialist", ["central_bank"]), action("run_debate")])
    Orchestrator(llm=llm, enable_memory=False, enable_market_data=False).run("Fed raises rates 50bps", parallel=False)

    assert llm.system_prompts["ReportDraft"] == SYNTHESIS_SYSTEM_PROMPT
    synthesis_input = [msg for name, msg in llm.calls if name == "ReportDraft"][0]
    assert "says nothing about market direction" in synthesis_input
