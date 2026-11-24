"""The evaluation reconciles every forecast on record and says how the engine did beside the plain forecast."""

from __future__ import annotations

from datetime import date

import pytest

from evaluation.report import to_markdown
from evaluation.run import evaluate
from models.forecast import ForecastCard
from tests.desk_fakes import ARM, hold, ny
from tests.forecast_fakes import forecast_desk
from tests.pipeline_fakes import day, make_pipeline


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    """Four close meetings, Monday to Friday of Thanksgiving week, then an evaluation after the next Monday's close."""
    desk = forecast_desk(tmp_path_factory.mktemp("evaluated"), ny(23, 18, 30))
    for number in (23, 24, 25, 27):
        assert hold(desk, ny(number, 18, 30)).status == "ok"
    before = desk.journal.forecast_scores(ARM)
    evaluation = evaluate(desk.journal, desk.market, now=ny(30, 18, 30), arms=[ARM], simulations=20, benchmark="RISE")
    return desk, before, evaluation


def test_the_evaluation_reconciles_what_the_meetings_could_not(evaluated):
    desk, before, evaluation = evaluated
    card = evaluation.arms[0].forecasts
    assert isinstance(card, ForecastCard) and card.arm == ARM
    # Four days, two names, four horizons each.
    assert card.total == 32 and card.unpriced == 0
    # By Monday 30 November every one-session forecast has matured. (Thursday was a holiday: the five
    # sessions after Monday 23 November end on 1 December.)
    assert [(item.label, item.scored, item.sessions) for item in card.by_horizon] == [("1 session", 8, 4)]
    assert card.scored == 8 and card.pending == 24
    # The meetings had scored the one-session forecasts of the first three days; the evaluation adds Friday's.
    assert sum(1 for score in before if score.status == "scored") == 6
    assert sum(1 for score in desk.journal.forecast_scores(ARM) if score.status == "scored") == 8


def test_the_report_says_how_the_engine_did_beside_the_plain_forecast(evaluated):
    _, _, evaluation = evaluated
    text = to_markdown(evaluation)
    assert "### Forecasts" in text
    assert "8 of 32 forecast(s) reconciled; 24 not yet due, 0 without a price." in text
    assert "| Ahead | Reconciled | Made on | Engine: inside 50 / 80 / 95% | Plain: inside 50 / 80 / 95% |" in text
    row = next(line for line in text.splitlines() if line.startswith("| 1 session |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[:3] == ["1 session", "8", "4 day(s)"]
    assert all(part.strip().endswith("%") for part in cells[3].split("/") + cells[4].split("/"))
    assert cells[6] in {f"{count} of 8" for count in range(9)}
    assert any("8 forecast(s) reconciled, made on 4 day(s)." in note for note in evaluation.arms[0].notes)
    assert text.index("### Forecasts") < text.index("### Model use")


def test_before_any_forecast_has_matured_the_report_says_so(tmp_path):
    desk = forecast_desk(tmp_path, ny(23, 18, 30))
    hold(desk, ny(23, 18, 30))
    evaluation = evaluate(desk.journal, desk.market, now=ny(23, 19, 0), arms=[ARM], simulations=5, benchmark="RISE")
    card = evaluation.arms[0].forecasts
    assert (card.total, card.scored, card.pending) == (8, 0, 8) and card.by_horizon == []
    text = to_markdown(evaluation)
    assert "0 of 8 forecast(s) reconciled; 8 not yet due, 0 without a price." in text
    assert "No forecast has reached the end of its horizon yet." in text
    assert not any("forecast(s) reconciled, made on" in note for note in evaluation.arms[0].notes)


def test_an_arm_that_makes_no_forecasts_has_no_such_section(tmp_path):
    pipeline = make_pipeline(tmp_path)
    assert pipeline.run()[0].status == "ok"
    evaluation = evaluate(pipeline.journal, pipeline.builder.market, now=day(16), simulations=5, benchmark="UPA")
    assert evaluation.arms[0].forecasts is None
    assert "### Forecasts" not in to_markdown(evaluation)
    assert evaluation.model_validate_json(evaluation.model_dump_json()) == evaluation
    assert date(2026, 11, 13) == evaluation.arms[0].first_session
