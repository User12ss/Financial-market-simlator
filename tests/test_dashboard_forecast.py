"""The dashboard over a desk's forecasts: what the quant engineer said, and how its forecasts held up. Everything is read."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from dashboard.reader import Reader
from tests.desk_fakes import ARM, hold, ny
from tests.forecast_fakes import forecast_desk
from tests.pipeline_fakes import make_pipeline
from tests.test_dashboard import browser, get


@pytest.fixture(scope="module")
def two_days(tmp_path_factory):
    folder = tmp_path_factory.mktemp("forecasts")
    desk = forecast_desk(folder, ny(23, 18, 30))
    results = [hold(desk, ny(day, 18, 30)) for day in (23, 24)]
    assert [result.status for result in results] == ["ok", "ok"]
    desk.journal.close()
    return folder, results


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_quant_engineers_lines_are_shown_as_code_speaking_in_a_stage_of_their_own(two_days):
    folder, (_, tuesday) = two_days
    client = browser(folder / "journal.sqlite3", folder / "evaluation")
    status, body = get(client, "/api/meeting", arm=ARM, run=tuesday.run_id)
    assert status == 200
    lines = [event for event in body["events"] if event["stage"] == "forecast"]
    assert {(e["stage_title"], e["speaker"], e["speaker_title"], e["is_llm"], e["to_title"]) for e in lines} == {
        ("预测", "quant_engineer", "量化工程师", False, "所有人")}
    assert [e["kind"] for e in lines] == ["market_state", "forecast", "forecast", "book_forecast", "forecast_scores"]
    # What it said is shown as it was recorded.
    rise = next(e for e in lines if e["ticker"] == "RISE")
    assert rise["remark"].startswith("RISE, 10 sessions on: half the simulated paths end between ")
    assert rise["payload"]["horizons"][2]["quantiles"]["50"] > 0
    stages = [e["stage_title"] for e in body["events"]]
    assert stages.index("预测") > stages.index("观点") and stages.index("预测") < stages.index("提案")


def test_the_evaluation_page_is_given_the_forecasts_record_with_or_without_a_report(two_days):
    folder, (monday, tuesday) = two_days
    before = digest(folder / "journal.sqlite3")
    client = browser(folder / "journal.sqlite3", folder / "evaluation")
    status, body = get(client, "/api/evaluation", arm=ARM)
    assert status == 200 and body["report"] is None                # no evaluation has been run
    forecasts = body["forecasts"]
    assert forecasts["arm"] == ARM and forecasts["sessions"] == 2

    card = forecasts["card"]
    assert (card["total"], card["scored"], card["pending"], card["unpriced"]) == (16, 2, 14, 0)
    one = card["by_horizon"][0]
    assert (one["label"], one["scored"], one["sessions"]) == ("1 session", 2, 1)
    assert set(one["engine"]["coverage"]) == set(one["naive"]["coverage"]) == {"50", "80", "95"}
    assert sum(one["engine"]["histogram"]) == sum(one["naive"]["histogram"]) == 2
    assert one["engine"]["loss"] > 0 and one["naive"]["loss"] > 0

    latest = forecasts["latest"]
    assert latest["session_date"] == "2026-11-24" and latest["run_id"] == tuesday.run_id != monday.run_id
    assert (latest["params_version"], latest["params_cutoff"], latest["paths"]) == (1, "2026-11-20", 10_000)
    assert latest["states"] == ["calm", "turbulent"] and len(latest["state_now"]) == 2
    assert [item["ticker"] for item in latest["tickers"]] == ["CLMB", "RISE"]
    ten = latest["tickers"][1]["horizons"][2]
    assert ten["sessions"] == 10 and set(ten["quantiles"]) == {"5", "25", "50", "75", "95"}
    assert "grid" not in ten and "naive_grid" not in ten            # the percentiles kept for scoring are not sent
    assert latest["tickers"][1]["stop_from"] == "position" and latest["book"]["positions"] == 1
    assert digest(folder / "journal.sqlite3") == before             # reading changed nothing


def test_a_group_without_forecasts_and_a_journal_from_before_them_have_none(two_days, tmp_path):
    folder, _ = two_days
    reader = Reader(folder / "journal.sqlite3", folder / "evaluation")
    nothing = {"arm": "fake/a", "sessions": 0, "card": None, "latest": None}
    assert reader.forecasts("fake/a") == nothing

    pipeline = make_pipeline(tmp_path)
    assert pipeline.run()[0].status == "ok"
    pipeline.journal.close()
    old = sqlite3.connect(tmp_path / "journal.sqlite3")
    old.execute("DROP TABLE forecast_sets")
    old.execute("DROP TABLE forecast_scores")
    old.commit()
    old.close()
    before = digest(tmp_path / "journal.sqlite3")
    reader = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    assert reader.forecasts("fake/a") == nothing
    assert reader.evaluation("fake/a")["forecasts"] == nothing
    # The reader does not add the tables it did not find: it never writes.
    assert digest(tmp_path / "journal.sqlite3") == before


def test_a_record_written_by_another_version_is_left_unshown_not_guessed_at(two_days, tmp_path):
    import shutil

    folder, _ = two_days
    copy = tmp_path / "journal.sqlite3"
    shutil.copy(folder / "journal.sqlite3", copy)
    db = sqlite3.connect(copy)
    db.execute("UPDATE forecast_sets SET set_json = '{\"layout\": \"another\"}'")
    db.commit()
    db.close()
    assert Reader(copy, tmp_path / "evaluation").forecasts(ARM) == {"arm": ARM, "sessions": 0, "card": None, "latest": None}


def test_one_groups_page_shows_only_its_own_forecasts_and_scores(two_days, tmp_path):
    import shutil

    folder, _ = two_days
    copy = tmp_path / "journal.sqlite3"
    shutil.copy(folder / "journal.sqlite3", copy)
    db = sqlite3.connect(copy)
    for table in ("forecast_sets", "forecast_scores"):
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
        marks = ",".join("?" * len(rows[0]))
        # The same records again under another group's name, and dated later, as that group's own.
        db.executemany(f"INSERT INTO {table} VALUES ({marks})",
                       [("desk/other", "2026-12-" + row[1][-2:], *row[2:]) for row in rows])
    db.commit()
    db.close()
    reader = Reader(copy, tmp_path / "evaluation")
    mine = reader.forecasts(ARM)
    assert (mine["sessions"], mine["card"]["total"], mine["card"]["scored"]) == (2, 16, 2)
    assert mine["latest"]["session_date"] == "2026-11-24"
    theirs = reader.forecasts("desk/other")
    assert (theirs["sessions"], theirs["card"]["total"]) == (2, 16)
