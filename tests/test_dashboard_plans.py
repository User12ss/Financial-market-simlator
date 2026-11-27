"""The dashboard over the desk's plans: what each plan says, which of its conditions held, and what came of them.

The plans are made by the desk itself, run on the synthetic market as its own tests run it. Everything is read:
the journal is left exactly as it was.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from dashboard.reader import Reader
from models.signals import CheckinDraft, ProposalSetDraft, ViewUpdateDraft
from pipeline import plans
from tests.desk_fakes import ARM, decisions, hold, ny, round_at, update
from tests.pipeline_fakes import condition, make_pipeline, plan_draft, proposals
from tests.plan_fakes import TUESDAY, plan_desk, plans_by, wrong_below
from tests.test_dashboard import browser, get

WRONG_REASON = "The trend would be broken."
TIME_REASON = ("The time the trade was given has run out. A position that is not ahead of its cost by then has not "
               "worked and is sold; one that is ahead is put before the meeting.")


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def week(folder, plan):
    """The desk after Friday 13 November's close meeting, which bought UPA and UPB, each with its plan."""
    desk = plan_desk(folder, plan=plan)
    friday = hold(desk, ny(13, 18, 30))
    assert friday.status == "ok", friday.message
    return desk, friday


def tuesday_close_at(desk, price):
    """Monday's close meeting held, and Tuesday's market set to close UPA at `price`."""
    assert hold(desk, ny(16, 18, 30)).status == "ok"
    desk.market.set_close("UPA", TUESDAY, price)


def plan_of(answer, ticker="UPA", part="standing"):
    return next(item for item in answer[part] if item["ticker"] == ticker)


def by_key(item):
    return {row["key"]: row for row in item["conditions"]}


@pytest.fixture(scope="module")
def sold(tmp_path_factory):
    """UPA's plan is out below 403.01. The watch finds it on Tuesday, the close meeting sells, Wednesday ends the plan.

    The answer for the page is read at each step: after Friday, after the watch found it, and at the end.
    """
    folder = tmp_path_factory.mktemp("sold")
    desk, friday = week(folder, wrong_below(0.6))
    desk.llm.replies[ProposalSetDraft] = proposals(("UPB", "hold", 0.6))       # nobody buys UPA back
    reader = Reader(folder / "journal.sqlite3")
    seen = {"friday": reader.plans(ARM)}
    tuesday_close_at(desk, 402.74)
    round_at(desk, ny(17, 10, 0))
    seen["found"] = reader.plans(ARM)
    tuesday = hold(desk, ny(17, 18, 30))
    wednesday = hold(desk, ny(18, 18, 30))
    assert (tuesday.status, wednesday.status) == ("ok", "ok")
    desk.journal.close()
    return folder, seen, friday, tuesday


@pytest.fixture(scope="module")
def trimmed(tmp_path_factory):
    """UPA's first plan trims after two sessions. Tuesday's close meeting trims it and makes version 2 without that."""
    folder = tmp_path_factory.mktemp("trimmed")
    out = condition("price_below", "exit", level=403.01)
    asked: list[str] = []

    def manager(ticker, close, stop):
        if ticker != "UPA":
            return None
        asked.append(ticker)
        day_two = condition("date", "trim", on_session=2, reason="Half off after two sessions.")
        return plan_draft(out, day_two) if len(asked) == 1 else plan_draft(out, change="The trim is done.")

    desk, _ = week(folder, plans_by(manager))
    assert hold(desk, ny(16, 18, 30)).status == "ok"
    # Bought at 406.49 and a little behind on the day: a day that sells leaves alone a trade that is ahead.
    desk.market.set_close("UPA", TUESDAY, 406.0)
    tuesday = hold(desk, ny(17, 18, 30))
    assert tuesday.status == "ok"
    desk.journal.close()
    return folder, tuesday


@pytest.fixture(scope="module")
def called(tmp_path_factory):
    """A headline with 'subpoena' calls a meeting on Monday morning, and a called check-in hears it."""
    folder = tmp_path_factory.mktemp("called")

    def subpoena(close, stop):
        return condition("headline", "call_meeting", keywords=["subpoena"], reason="A regulator would change the case.")

    desk, friday = week(folder, wrong_below(0.6, subpoena))
    hold(desk, ny(16, 8, 45))
    desk.news.add("UPA", "UPA Corp receives a subpoena over its accounts")
    round_at(desk, ny(16, 10, 0))
    desk.llm.replies[ViewUpdateDraft] = update("weakened", "A subpoena over the accounts weakens the case.", cited=[1])
    desk.llm.replies[CheckinDraft] = decisions(("UPA", "sell"))
    meeting = hold(desk, ny(16, 10, 5))
    assert (meeting.kind, meeting.status) == ("called", "ok")
    desk.journal.close()
    return folder, friday, meeting


# ── The plans that stand ─────────────────────────────────────────────────────

def test_a_plan_made_at_fridays_meeting_is_shown_with_every_field_the_page_needs(sold):
    _, seen, _, _ = sold
    answer = seen["friday"]
    assert (answer["arm"], answer["has_plans"], answer["ended"]) == (ARM, True, [])
    assert [item["ticker"] for item in answer["standing"]] == ["UPA", "UPB"]
    upa = plan_of(answer)
    assert (upa["opened"], upa["held_now"]) == ("2026-11-13", False)           # the buy waits for Monday's open
    [version] = upa["versions"]
    assert set(version) == {
        "version", "session_date", "created_at", "source", "held", "thesis", "direction", "horizon", "ends_on", "close",
        "stop", "target", "chances", "changes", "basis", "refused", "data_warnings"}
    assert (version["version"], version["session_date"], version["source"], version["held"]) == (1, "2026-11-13", "manager", False)
    assert (version["stop"], version["ends_on"], version["horizon"]) == (400.83, "2026-11-30", 10)
    assert version["thesis"] == "The trend carries it." and version["basis"] == "There is no plan for it yet."
    assert (version["changes"], version["refused"], version["chances"], version["target"]) == ([], [], {}, None)
    assert upa["conditions"] == [
        {"key": "time", "condition": {"kind": "date", "level": None, "average": None, "on": "2026-11-30", "ending": None,
                                      "keywords": []},
         "action": "exit", "new_stop": None, "reason": TIME_REASON, "source": "rule", "automatic": True,
         # A sale on a day is carried out only for a trade that is not ahead, and the page is told so.
         "unless_ahead": True, "in_version": 1, "dropped": False, "state": "waiting", "trigger": None},
        {"key": "wrong", "condition": {"kind": "price_below", "level": 403.01, "average": None, "on": None, "ending": None,
                                       "keywords": []},
         "action": "exit", "new_stop": None, "reason": WRONG_REASON, "source": "manager", "automatic": True,
         "unless_ahead": False, "in_version": 1, "dropped": False, "state": "waiting", "trigger": None},
    ]


def test_whether_a_day_sells_a_trade_that_is_ahead_is_told_by_the_setting_as_it_stands(sold, trimmed, monkeypatch):
    folder, _ = trimmed
    def told():
        return {row["key"]: row["unless_ahead"] for row in plan_of(Reader(folder / "journal.sqlite3").plans(ARM))["conditions"]}
    assert told() == {"time": True, "wrong": False, "1": True}      # the manager's "half off after two sessions" too
    monkeypatch.setattr(plans.config, "PLAN_TIME_STOP", "exit")
    assert told() == {"time": False, "wrong": False, "1": False}


def test_a_condition_the_watch_found_is_fired_with_when_and_what_was_seen(sold):
    _, seen, _, _ = sold
    wrong = by_key(plan_of(seen["found"]))["wrong"]
    assert wrong["state"] == "fired"
    assert wrong["trigger"] == {
        "version": 1, "fired_at": "2026-11-17T15:00:00Z", "session_date": "2026-11-17", "found_by": "watch",
        "evidence": "It traded at 402.74.", "result": "", "handled_by": "", "handled_on": None}
    assert by_key(plan_of(seen["found"]))["time"]["state"] == "waiting"
    assert plan_of(seen["found"])["held_now"] is True                          # the buy filled on Monday


def test_a_plan_that_is_over_is_listed_as_ended_with_why_and_what_its_conditions_came_to(sold):
    folder, _, _, tuesday = sold
    status, answer = get(browser(folder / "journal.sqlite3", folder / "evaluation"), "/api/plans", arm=ARM)
    assert status == 200 and answer["has_plans"] is True
    assert [item["ticker"] for item in answer["standing"]] == ["UPB"]
    [ended] = answer["ended"]
    assert (ended["ticker"], ended["opened"], ended["closed_on"], ended["versions"]) == ("UPA", "2026-11-13", "2026-11-18", 1)
    assert ended["closed_why"] == "The position was sold at the open on 2026-11-18, at 402.54."
    assert ended["last_version"]["version"] == 1 and ended["last_version"]["thesis"] == "The trend carries it."
    wrong = by_key(ended)["wrong"]
    assert wrong["state"] == "done"
    assert wrong["trigger"]["result"] == "A sale of 24 share(s) is placed for the next open."
    assert (wrong["trigger"]["handled_by"], wrong["trigger"]["handled_on"]) == (tuesday.run_id, "2026-11-17")
    assert by_key(ended)["time"]["state"] == "waiting"


def test_a_condition_that_could_not_be_carried_out_is_not_done_and_says_why(tmp_path):
    desk, _ = week(tmp_path, wrong_below(0.6))
    tuesday_close_at(desk, 399.0)                                            # through the plan's price and the stop
    round_at(desk, ny(17, 10, 0))
    assert hold(desk, ny(17, 18, 30)).status == "ok"
    answer = Reader(tmp_path / "journal.sqlite3").plans(ARM)
    ended = plan_of(answer, part="ended")
    assert ended["closed_why"] == "The position was sold at its stop on 2026-11-17, at 400.63."
    wrong = by_key(ended)["wrong"]
    assert (wrong["state"], wrong["trigger"]["result"]) == ("not_done", "The plan was over before it could be acted on.")
    assert wrong["trigger"]["evidence"] == "It traded at 399.00."


def test_a_second_version_shows_what_changed_and_why_and_the_dropped_condition_keeps_its_trigger(trimmed):
    folder, tuesday = trimmed
    upa = plan_of(Reader(folder / "journal.sqlite3").plans(ARM))
    first, second = upa["versions"]
    assert (first["version"], second["version"], second["session_date"]) == (1, 2, "2026-11-17")
    assert first["changes"] == []
    assert second["changes"] == [
        "Dropped: [1] On the close of 2026-11-17: sell half, by itself. Half off after two sessions."]
    assert second["basis"] == ("Its condition [1] held on 2026-11-17: The session of 2026-11-17 has closed. "
                               "The manager: The trim is done.")
    # The latest version's conditions first, in its own order; then the one dropped, with what it did.
    assert [(row["key"], row["in_version"], row["dropped"], row["state"]) for row in upa["conditions"]] == [
        ("time", 2, False, "waiting"), ("wrong", 2, False, "waiting"), ("1", 1, True, "done")]
    dropped = upa["conditions"][2]
    assert (dropped["condition"]["kind"], dropped["condition"]["on"], dropped["action"]) == ("date", "2026-11-17", "trim")
    assert dropped["reason"] == "Half off after two sessions."
    assert dropped["trigger"]["found_by"] == "close" and dropped["trigger"]["handled_by"] == tuesday.run_id
    assert dropped["trigger"]["evidence"] == "The session of 2026-11-17 has closed."
    assert dropped["trigger"]["result"] == "A sale of 12 share(s) is placed for the next open."


def test_a_headline_that_called_a_meeting_is_heard_and_the_called_checkin_is_one_of_the_days_meetings(called):
    folder, _, meeting = called
    client = browser(folder / "journal.sqlite3", folder / "evaluation")
    upa = plan_of(get(client, "/api/plans", arm=ARM)[1])
    call = by_key(upa)["1"]
    assert (call["condition"]["kind"], call["condition"]["keywords"], call["action"], call["automatic"]) == (
        "headline", ["subpoena"], "call_meeting", False)
    assert call["state"] == "heard"
    assert call["trigger"]["evidence"] == "A headline has 'subpoena': UPA Corp receives a subpoena over its accounts"
    assert call["trigger"]["result"] == "Heard at the called check-in of 2026-11-16 10:05 New York time."
    assert (call["trigger"]["handled_by"], call["trigger"]["handled_on"]) == (meeting.meeting_id, "2026-11-16")

    status, day = get(client, "/api/day", arm=ARM, day="2026-11-16")
    assert status == 200
    assert [(item["kind"], item["status"]) for item in day["meetings"]][:2] == [("premarket", "ok"), ("called", "ok")]
    status, body = get(client, "/api/meeting", arm=ARM, run_id=meeting.meeting_id)
    assert status == 200 and body["run"]["kind"] == "called" and body["stands"] is True
    [line] = [event for event in body["events"] if event["kind"] == "plan_call"]
    assert (line["stage"], line["stage_title"], line["speaker_title"]) == ("briefing", "情报", "情报员")


def test_two_called_checkins_of_one_day_are_both_listed(called, tmp_path):
    import shutil

    folder, _, meeting = called
    copy = tmp_path / "journal.sqlite3"
    shutil.copy(folder / "journal.sqlite3", copy)
    db = sqlite3.connect(copy)
    row = db.execute("SELECT * FROM meetings WHERE meeting_id = ?", (meeting.meeting_id,)).fetchone()
    names = [name for name, *_ in db.execute("SELECT * FROM meetings").description]
    later = dict(zip(names, row), meeting_id=meeting.meeting_id + "-later",
                 started_at=row[names.index("started_at")].replace("15:05", "16:05"))
    db.execute(f"INSERT INTO meetings ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", tuple(later.values()))
    db.commit()
    db.close()
    day = Reader(copy).day(ARM, "2026-11-16")
    assert [item["run_id"] for item in day["meetings"] if item["kind"] == "called"] == [
        meeting.meeting_id, meeting.meeting_id + "-later"]


def test_the_plans_stage_has_its_title_in_the_close_meeting(called):
    folder, friday, _ = called
    status, body = get(browser(folder / "journal.sqlite3", folder / "evaluation"), "/api/meeting", arm=ARM,
                       run_id=friday.run_id)
    assert status == 200
    lines = [event for event in body["events"] if event["stage"] == "plans"]
    assert {event["stage_title"] for event in lines} == {"计划"}
    assert [(event["kind"], event["ticker"], event["speaker_title"]) for event in lines] == [
        ("plan_draft", "UPA", "组合经理"), ("plan", "UPA", "风控官"), ("plan_draft", "UPB", "组合经理"), ("plan", "UPB", "风控官")]
    stages = [event["stage_title"] for event in body["events"]]
    assert stages.index("风控") < stages.index("计划") < stages.index("下单")
    # What was recorded is passed on as it was.
    assert lines[1]["remark"].startswith("UPA, plan version 1 (the manager's): stop 400.83")


def test_a_draft_that_was_refused_leaves_the_desks_own_plan_and_says_what_was_refused(tmp_path):
    headline_only = plan_draft(condition("headline", "call_meeting", keywords=["subpoena"]))
    week(tmp_path, plans_by(lambda ticker, close, stop: headline_only if ticker == "UPA" else None))
    upa = plan_of(Reader(tmp_path / "journal.sqlite3").plans(ARM))
    [version] = upa["versions"]
    assert version["source"] == "default"
    assert version["refused"] == [f"The plan is refused whole: {plans.NO_WRONG_IF}"]
    assert version["thesis"] == "The trend is intact and nothing argues against it."
    assert [row["key"] for row in upa["conditions"]] == ["time"]


# ── Where there are none ─────────────────────────────────────────────────────

def test_the_baseline_and_a_journal_from_before_plans_have_none_and_nothing_is_written(sold, tmp_path):
    folder, _, _, _ = sold
    assert Reader(folder / "journal.sqlite3").plans("fake/a") == {
        "arm": "fake/a", "has_plans": False, "standing": [], "ended": []}

    pipeline = make_pipeline(tmp_path)
    assert pipeline.run()[0].status == "ok"
    pipeline.journal.close()
    client = browser(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    assert get(client, "/api/plans", arm="fake/a") == (200, {"arm": "fake/a", "has_plans": False, "standing": [], "ended": []})
    old = sqlite3.connect(tmp_path / "journal.sqlite3")
    old.execute("DROP TABLE plans")
    old.execute("DROP TABLE plan_triggers")
    old.commit()
    old.close()
    before = digest(tmp_path / "journal.sqlite3")
    assert get(client, "/api/plans", arm="fake/a") == (200, {"arm": "fake/a", "has_plans": False, "standing": [], "ended": []})
    assert digest(tmp_path / "journal.sqlite3") == before                  # the tables it did not find are not added


def test_a_record_written_by_another_version_is_left_unshown(sold, tmp_path):
    import shutil

    folder, _, _, _ = sold
    copy = tmp_path / "journal.sqlite3"
    shutil.copy(folder / "journal.sqlite3", copy)
    db = sqlite3.connect(copy)
    db.execute("UPDATE plans SET plan_json = '{\"layout\": \"another\"}'")
    db.commit()
    db.close()
    assert Reader(copy).plans(ARM) == {"arm": ARM, "has_plans": False, "standing": [], "ended": []}


def test_reading_the_plans_leaves_the_journal_exactly_as_it_was(sold):
    folder, _, _, _ = sold
    before = digest(folder / "journal.sqlite3")
    client = browser(folder / "journal.sqlite3", folder / "evaluation")
    for _ in range(2):
        assert get(client, "/api/plans", arm=ARM)[0] == 200
    assert digest(folder / "journal.sqlite3") == before
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/plans").status_code == 405


def test_a_sale_a_plan_put_in_is_the_decision_shown_for_its_ticker_that_day(sold):
    folder, _, _, tuesday = sold
    client = browser(folder / "journal.sqlite3", folder / "evaluation")
    status, history = get(client, "/api/history", arm=ARM)
    assert status == 200
    day = next(item for item in history["sessions"] if item["session"] == "2026-11-17")
    upa = day["tickers"]["UPA"]
    # The default that kept it was set aside for the plan's sale: that is what the day shows, and it is not a repeat.
    assert (upa["action"], upa["source"], upa["check"], upa["kept_by_default"], upa["repeats"]) == (
        "sell", "plan", "accepted", False, 0)
    assert upa["ordered"] == {"side": "sell", "shares": 24}
    status, meeting = get(client, "/api/meeting", arm=ARM, run_id=tuesday.run_id)
    [line] = [event for event in meeting["events"] if event["kind"] == "plan_action"]
    assert (line["stage"], line["ticker"], line["payload"]["source"], line["payload"]["action"]) == (
        "checks", "UPA", "plan", "sell")
