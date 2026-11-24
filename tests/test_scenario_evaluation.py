"""The evaluation's part in the trades worked back: it reconciles what the meetings could not, and the report says how they did."""

from __future__ import annotations

import pytest

from evaluation.report import to_markdown
from evaluation.run import evaluate
from models.planning import ScenarioDraft
from tests.desk_fakes import ARM, hold, ny, round_at
from tests.pipeline_fakes import day, make_pipeline, proposed_event, scenario
from tests.test_scenario_desk import MONDAY, RECALL, scripted_desk

PRICE = proposed_event("A slide below the stop", first=1, last=3, probability=0.1, check="close_below", level=1.0,
                       check_text="A close below one dollar.")
VAGUE = proposed_event("Sentiment sours", first=1, last=3, check="other", keywords=[],
                       check_text="Commentary turns negative.")


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    """Four meetings from Monday 23 to Friday 27 November, with three-session trades, and an evaluation on the Monday after."""
    desk = scripted_desk(tmp_path_factory.mktemp("evaluated"), horizon=3,
                         events={"RISE": scenario(RECALL, PRICE, VAGUE)})
    hold(desk, ny(23, 18, 30))
    desk.news.add("RISE", "Rise Corp recall widens to six models")
    round_at(desk, ny(24, 10, 0))
    for number in (24, 25, 27):
        assert hold(desk, ny(number, 18, 30)).status == "ok"
    before = (desk.journal.outcome_scores(ARM), desk.journal.event_scores(ARM))
    evaluation = evaluate(desk.journal, desk.market, now=ny(30, 19, 0), arms=[ARM], simulations=5, benchmark="RISE",
                          ledger=desk.ledger)
    return desk, before, evaluation


def test_the_evaluation_reconciles_the_endings_and_events_the_meetings_could_not(evaluated):
    desk, (endings_before, events_before), evaluation = evaluated
    record = evaluation.arms[0].worked_back
    endings = desk.journal.outcome_scores(ARM)
    # Friday's meeting had scored Monday's trades. The evaluation, held after Monday 30 November's close, adds Tuesday's.
    assert {score.session_date for score in endings_before if score.status == "scored"} == {MONDAY}
    scored = [score for score in endings if score.status == "scored"]
    assert {score.session_date.isoformat() for score in scored} == {"2026-11-23", "2026-11-24"}
    assert record["endings"] == {
        "scored": len(scored), "pending": sum(1 for score in endings if score.status == "pending"), "unpriced": 0}
    assert record["endings"]["scored"] > sum(1 for score in endings_before if score.status == "scored")
    measured, rehearsed = record["outcomes"]["measured"], record["outcomes"]["rehearsed"]
    assert measured["sessions"] == rehearsed["sessions"] == 2
    assert measured["scored"] == sum(1 for score in scored if score.basis == "measured")
    assert rehearsed["scored"] == sum(1 for score in scored if score.basis == "rehearsed") >= 2
    assert sum(measured["ended"].values()) == measured["scored"] and 0 <= measured["brier"] <= 2
    assert measured["plain_brier"] is not None
    # Monday's meeting kept three events for RISE: one judged by a headline, one by a price, one that code cannot
    # judge. On Tuesday morning a headline says the recall has widened, so from Tuesday's meeting on "widens" is a
    # word the headlines shown already have, and that event is refused: two a day are kept.
    events = record["events"]
    assert events["proposed"] == 3 + 2 + 2 + 2 and events["cannot_be_judged"] == 4 and events["unread"] == 0
    # By Monday 30 November the days of Monday's events have passed, and of Tuesday's three-session one.
    assert (events["scored"], events["pending"], events["happened"]) == (3, 2, 1)
    assert events["scored"] > sum(1 for score in events_before if score.status == "scored")
    labels = {score.session_date.isoformat(): [] for score in desk.journal.event_scores(ARM)}
    for score in desk.journal.event_scores(ARM):
        labels[score.session_date.isoformat()].append(score.label)
    assert labels["2026-11-23"] == ["The recall widens", "A slide below the stop", "Sentiment sours"]
    assert labels["2026-11-24"] == ["A slide below the stop", "Sentiment sours"]
    judged = {(score.key, score.happened) for score in desk.journal.event_scores(ARM)
              if score.session_date == MONDAY and score.status == "scored"}
    assert judged == {("1", True), ("2", False)}                    # the recall widened; no close fell below a dollar


def test_the_report_says_how_the_chances_did_beside_the_plain_forecast(evaluated):
    _, _, evaluation = evaluated
    record = evaluation.arms[0].worked_back
    text = to_markdown(evaluation)
    assert "### Trades worked back" in text
    counts = record["endings"]
    assert f"{counts['scored']} ending(s) reconciled; {counts['pending']} not yet due, 0 without a price." in text
    assert ("| Reading | Reconciled | Made on | Ended at target / stop / neither | Chance given, on average | "
            "Brier score | Plain forecast's |") in text
    row = next(line for line in text.splitlines() if line.startswith("| The simulation alone |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    measured = record["outcomes"]["measured"]
    assert cells[:3] == ["The simulation alone", str(measured["scored"]), "2 day(s)"]
    assert cells[3] == " / ".join(str(measured["ended"][name]) for name in ("good", "bad", "middle"))
    assert all(part.strip().endswith("%") for part in cells[4].split("/"))
    assert cells[5] == f"{measured['brier']:.3f}" and cells[6] == f"{measured['plain_brier']:.3f}"
    assert any(line.startswith("| With the proposed events |") for line in text.splitlines())
    events = record["events"]
    assert ("The scenario analyst proposed 9 event(s): 3 judged, 2 waiting, 4 that code cannot judge and that are "
            "never scored. Of those judged, 1 happened; ") in text
    assert f"Of those judged, {events['happened']} happened; they were given " in text
    assert "where always saying 50% scores 0.250." in text
    assert text.index("### Forecasts") < text.index("### Trades worked back") < text.index("### Model use")
    assert evaluation.model_validate_json(evaluation.model_dump_json()) == evaluation


def test_without_the_ledger_an_event_checked_by_a_headline_waits_and_is_judged_once_one_is_at_hand(tmp_path):
    desk = scripted_desk(tmp_path, horizon=3)
    desk.llm.replies[ScenarioDraft] = lambda system, user: scenario(RECALL) if "for CLMB." in user else scenario()
    hold(desk, ny(23, 18, 30))
    blind = evaluate(desk.journal, desk.market, now=ny(27, 19, 0), arms=[ARM], simulations=5, benchmark="RISE")
    events = blind.arms[0].worked_back["events"]
    assert (events["proposed"], events["scored"], events["cannot_be_judged"], events["unread"]) == (1, 0, 0, 1)
    text = to_markdown(blind)
    assert ("0 judged, 0 waiting, 0 that code cannot judge and that are never scored. 1 more wait for a reading of "
            "the name's headlines that covers their days without a gap: without one, nothing says whether they "
            "happened.") in text
    assert "Of those judged" not in text
    assert desk.journal.event_scores(ARM)[0].status == "unread"
    # Nothing was final about that. The same evaluation with the desk's ledger at hand: the close meetings of
    # Tuesday and Wednesday did not happen here, so nothing read CLMB's headlines, and it still waits.
    sighted = evaluate(desk.journal, desk.market, now=ny(27, 19, 0), arms=[ARM], simulations=5, benchmark="RISE",
                       ledger=desk.ledger)
    assert sighted.arms[0].worked_back["events"]["unread"] == 1
    # A reading during those days does not say how they ended.
    desk.ledger.log_fetch(ny(24, 10, 0), "headlines", "CLMB", found=1, added=0)
    assert evaluate(desk.journal, desk.market, now=ny(27, 19, 0), arms=[ARM], simulations=5, benchmark="RISE",
                    ledger=desk.ledger).arms[0].worked_back["events"]["unread"] == 1
    # Once one after their last close is on the ledger, it is judged: nothing in them says "widens".
    desk.ledger.log_fetch(ny(25, 16, 30), "headlines", "CLMB", found=1, added=0)
    judged = evaluate(desk.journal, desk.market, now=ny(27, 19, 0), arms=[ARM], simulations=5, benchmark="RISE",
                      ledger=desk.ledger)
    assert judged.arms[0].worked_back["events"]["scored"] == 1 and judged.arms[0].worked_back["events"]["happened"] == 0


def test_the_reports_row_counts_the_endings_in_the_order_its_heading_names_them(evaluated):
    _, _, evaluation = evaluated
    changed = evaluation.model_copy(deep=True)
    changed.arms[0].worked_back["outcomes"]["measured"]["ended"] = {"good": 3, "bad": 1, "middle": 2}
    row = next(line for line in to_markdown(changed).splitlines() if line.startswith("| The simulation alone |"))
    assert [cell.strip() for cell in row.strip("|").split("|")][3] == "3 / 1 / 2"


def test_before_any_trade_has_ended_the_report_says_so(tmp_path):
    desk = scripted_desk(tmp_path, events={})
    hold(desk, ny(23, 18, 30))
    evaluation = evaluate(desk.journal, desk.market, now=ny(23, 19, 0), arms=[ARM], simulations=5, benchmark="RISE")
    record = evaluation.arms[0].worked_back
    assert record["endings"] == {"scored": 0, "pending": 2, "unpriced": 0} and record["outcomes"] == {}
    text = to_markdown(evaluation)
    assert "0 ending(s) reconciled; 2 not yet due, 0 without a price." in text
    assert "No trade worked back has reached the end of its horizon yet." in text
    assert "The scenario analyst proposed" not in text                 # it proposed nothing: nothing is said of it


def test_an_arm_that_works_no_trade_back_has_no_such_section(tmp_path):
    pipeline = make_pipeline(tmp_path)
    assert pipeline.run()[0].status == "ok"
    evaluation = evaluate(pipeline.journal, pipeline.builder.market, now=day(16), simulations=5, benchmark="UPA")
    assert evaluation.arms[0].worked_back is None
    assert "### Trades worked back" not in to_markdown(evaluation)
