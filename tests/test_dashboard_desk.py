"""The dashboard over a desk's records: what the desk is at now, the day's meetings, and the watch's last round.

Everything is read. The journal and the ledger are left exactly as they were.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from datetime import date

import pytest

from dashboard.reader import Reader, today
from execution.paper import PaperBroker
from models.signals import CheckinDraft, ProposalSetDraft, ViewUpdateDraft
from pipeline.daily import Arm, DailyPipeline
from tests.desk_fakes import ARM, decisions, hold, make_desk, ny, round_at, update, update_by_ticker
from tests.pipeline_fakes import ScriptedLLM, proposals
from tests.test_dashboard import browser, get, stream

TUESDAY = "2026-11-17"


def a_tuesday(folder, close=False):
    """A desk that holds UPA and UPB, and its Tuesday: a quiet check-in before the open, and at midday one
    in which the analyst finds UPA's view broken and the manager's sell is noted for the close."""
    desk = make_desk(folder)
    assert hold(desk, ny(13, 18, 30)).status == "ok"
    desk.llm.replies[ProposalSetDraft] = proposals(("UPA", "hold", 0.6), ("UPB", "hold", 0.6))
    assert hold(desk, ny(16, 18, 30)).status == "ok"
    round_at(desk, ny(17, 8, 15))
    desk.premarket = hold(desk, ny(17, 8, 30))
    desk.news.add("UPA", "UPA loses its largest customer", ny(17, 11, 0))
    desk.news.add("UPB", "UPB opens a new plant", ny(17, 11, 5))
    round_at(desk, ny(17, 11, 45))
    desk.llm.replies[ViewUpdateDraft] = update_by_ticker(
        {"UPA": update("broken", "The largest customer has gone.", cited=[1])}, update(cited=[1]))
    desk.llm.replies[CheckinDraft] = decisions(("UPA", "sell"), ("UPB", "keep"))
    desk.midday = hold(desk, ny(17, 12, 0))
    assert (desk.premarket.status, desk.midday.status) == ("ok", "ok")
    if close:
        desk.close = hold(desk, ny(17, 18, 30))
        assert desk.close.status == "ok"
    return desk


@pytest.fixture(scope="module")
def tuesday(tmp_path_factory):
    """Shared by the tests that only read."""
    folder = tmp_path_factory.mktemp("tuesday")
    return a_tuesday(folder), folder


@pytest.fixture
def client(tuesday):
    _, folder = tuesday
    return browser(folder / "journal.sqlite3", folder / "evaluation")


def fingerprint(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_of(source, target):
    """A whole copy of a journal or a ledger. Copying the file alone would miss what is still in its write-ahead log."""
    with sqlite3.connect(source) as old, sqlite3.connect(target) as new:
        old.backup(new)
    return target


# ── What the desk is at now ──────────────────────────────────────────────────

def test_with_no_meeting_named_the_page_gets_what_the_desk_is_at_now(tuesday, client):
    desk, _ = tuesday
    status, body = get(client, "/api/meeting", arm=ARM)
    assert status == 200
    assert (body["session"], body["stands"]) == (TUESDAY, True)
    run = body["run"]
    assert (run["run_id"], run["kind"], run["status"], run["live"]) == (desk.midday.meeting_id, "midday", "ok", False)
    assert run["as_of"] == ny(17, 12, 0).isoformat() and run["started_at"] == ny(17, 12, 0).isoformat()
    # Word for word and in order, as the journal has them.
    recorded = desk.journal.events(desk.midday.meeting_id)
    assert [(e["seq"], e["stage"], e["kind"], e["ticker"], e["remark"]) for e in body["events"]] == [
        (e.seq, e.stage, e.kind, e.ticker, e.remark) for e in recorded]
    assert [e.payload for e in recorded] == [e["payload"] for e in body["events"]]
    first = body["events"][0]
    assert (first["stage_title"], first["speaker"], first["speaker_title"], first["is_llm"], first["to_title"]) == (
        "情报", "news_desk", "情报员", False, "所有人")
    assert [e["stage_title"] for e in body["events"]] == ["情报"] * 3 + ["观点"] * 2 + ["提案"] + ["检查"] * 2 + ["下单"]
    assert body["today"] == {
        "ticker_analyst": "2 条更新：不变 1，加强 0，削弱 0，不成立 1",
        "portfolio_manager": "2 条决定：保留 1，减仓 0，卖出 1，撤回买单 0",
        "risk_officer": "提案：接受 2，拒绝 0；订单：批准 0，削减 0，拒绝 0",
        "trader": "留给收盘会 1 条，撤回买单 0 张，下单 0 张",
    }


def test_a_checkin_is_opened_by_name_and_only_by_its_own_configuration(tuesday, client):
    desk, _ = tuesday
    status, body = get(client, "/api/meeting", arm=ARM, run_id=desk.premarket.meeting_id)
    assert status == 200 and body["run"]["kind"] == "premarket" and body["session"] == TUESDAY
    assert len(body["events"]) == 1 and body["run"]["as_of"] == ny(17, 8, 30).isoformat()
    assert Reader(desk.journal.path).meeting("fake/other", run_id=desk.premarket.meeting_id)["run"] is None
    assert get(client, "/api/meeting", arm=ARM, run_id="no-such-meeting")[1]["run"] is None


def test_a_day_is_shown_by_its_close_meeting_and_until_there_is_one_by_its_last_checkin(tmp_path, tuesday, client):
    desk, _ = tuesday
    assert get(client, "/api/meeting", arm=ARM, session=TUESDAY)[1]["run"]["run_id"] == desk.midday.meeting_id
    monday = get(client, "/api/meeting", arm=ARM, session="2026-11-16")[1]
    assert monday["run"]["kind"] == "close" and monday["run"]["as_of"] == ny(16, 18, 30).isoformat()
    assert get(client, "/api/meeting", arm=ARM, session="2026-11-19")[1]["run"] is None

    closed = a_tuesday(tmp_path, close=True)
    reader = Reader(closed.journal.path)
    assert reader.meeting(ARM, session=TUESDAY)["run"]["run_id"] == closed.close.run_id
    assert reader.meeting(ARM)["run"]["run_id"] == closed.close.run_id, "the close meeting is the last one held"
    assert reader.meeting(ARM)["run"]["kind"] == "close"
    assert reader.meeting(ARM, run_id=closed.midday.meeting_id)["stands"] is True


def test_the_latest_close_meeting_can_be_asked_for_whatever_checkins_came_after_it(tuesday, client):
    desk, _ = tuesday
    status, body = get(client, "/api/meeting", arm=ARM, kind="close")
    assert status == 200 and (body["session"], body["run"]["kind"]) == ("2026-11-16", "close")
    assert body["run"]["run_id"] == desk.journal.completed_run(ARM, date(2026, 11, 16))
    # Asking for a kind other than the close meeting changes nothing, and a name still wins.
    assert get(client, "/api/meeting", arm=ARM, kind="midday")[1]["run"]["run_id"] == desk.midday.meeting_id
    named = get(client, "/api/meeting", arm=ARM, kind="close", run_id=desk.premarket.meeting_id)[1]
    assert named["run"]["run_id"] == desk.premarket.meeting_id
    assert Reader(desk.journal.path).meeting("fake/none", close_only=True)["run"] is None


def test_every_day_with_a_meeting_can_be_picked_newest_first(tuesday, client):
    desk, _ = tuesday
    status, body = get(client, "/api/sessions", arm=ARM)
    assert status == 200
    assert [(s["session"], s["kind"], s["status"]) for s in body["sessions"]] == [
        (TUESDAY, "midday", "ok"), ("2026-11-16", "close", "ok"), ("2026-11-13", "close", "ok")]
    assert body["sessions"][0]["run_id"] == desk.midday.meeting_id and body["sessions"][0]["events"] == 9


# ── The day's strip ──────────────────────────────────────────────────────────

def test_the_days_meetings_are_listed_in_the_order_they_were_held_with_the_watchs_last_round(tuesday, client):
    desk, _ = tuesday
    status, body = get(client, "/api/day", arm=ARM)
    assert status == 200 and (body["arm"], body["day"]) == (ARM, TUESDAY)
    assert [(m["kind"], m["run_id"], m["status"], m["events"], m["as_of"], m["session"]) for m in body["meetings"]] == [
        ("premarket", desk.premarket.meeting_id, "ok", 1, ny(17, 8, 30).isoformat(), TUESDAY),
        ("midday", desk.midday.meeting_id, "ok", 9, ny(17, 12, 0).isoformat(), TUESDAY),
    ]
    watch = body["watch"]
    assert watch["day"] == TUESDAY and watch["tickers"] == 2
    assert watch["headlines"]["added"] == 2 and watch["quotes"] == {"count": 4, "error": ""}
    assert watch["next_meeting"] == {"kind": "midday", "at": ny(17, 12, 0).isoformat()}
    assert [move["ticker"] for move in watch["moves"]] == ["UPA", "UPB"]

    monday = get(client, "/api/day", arm=ARM, day="2026-11-16")[1]
    assert [(m["kind"], m["as_of"]) for m in monday["meetings"]] == [("close", ny(16, 18, 30).isoformat())]
    assert monday["watch"] == watch, "the watch's last round is the same whichever day is on show"
    assert get(client, "/api/day", arm=ARM, day="2026-11-19")[1]["meetings"] == []


def test_of_several_attempts_at_one_checkin_the_one_that_stands_is_listed(tmp_path):
    desk = a_tuesday(tmp_path)
    desk.news.add("UPA", "UPA opens a new plant", ny(17, 14, 0))
    round_at(desk, ny(17, 14, 45))
    desk.llm.replies[ViewUpdateDraft] = RuntimeError("killed")
    failed = hold(desk, ny(17, 15, 0))
    reader = Reader(desk.journal.path)
    assert failed.status == "failed"
    assert [(m["kind"], m["status"]) for m in reader.day(ARM)["meetings"]][-1] == ("afternoon", "failed")
    assert reader.meeting(ARM)["run"]["kind"] == "midday", "a failed check-in is not what the desk is at"

    desk.llm.replies[ViewUpdateDraft] = update(cited=[1])
    held = hold(desk, ny(17, 15, 15))
    assert held.status == "ok"
    listed = reader.day(ARM)["meetings"]
    assert [(m["kind"], m["run_id"]) for m in listed][-1] == ("afternoon", held.meeting_id) and len(listed) == 3
    assert reader.meeting(ARM)["run"]["run_id"] == held.meeting_id
    assert reader.meeting(ARM, run_id=failed.meeting_id)["stands"] is False
    assert reader.meeting(ARM, run_id=held.meeting_id)["stands"] is True


def test_a_checkin_that_is_going_is_what_the_desk_is_at_and_one_whose_process_is_gone_is_not(tmp_path):
    desk = a_tuesday(tmp_path)
    reader = Reader(desk.journal.path)
    desk.journal.start_meeting("going", ARM, date(2026, 11, 17), "afternoon", date(2026, 11, 16), ny(17, 15, 0),
                               ny(17, 15, 0), pid=os.getpid())
    now = reader.meeting(ARM)["run"]
    assert (now["run_id"], now["status"], now["live"], now["kind"]) == ("going", "running", True, "afternoon")
    assert reader.day(ARM)["meetings"][-1]["live"] is True

    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()
    with desk.journal.transaction():
        desk.journal.write("UPDATE meetings SET pid = ? WHERE meeting_id = 'going'", (gone.pid,))
    assert reader.meeting(ARM)["run"]["run_id"] == desk.midday.meeting_id
    listed = reader.day(ARM)["meetings"][-1]
    assert (listed["run_id"], listed["status"], listed["live"]) == ("going", "interrupted", False)


# ── The market data of a check-in ────────────────────────────────────────────

def test_a_checkin_is_shown_the_data_of_the_close_meeting_it_asked_about(tmp_path, tuesday, client):
    desk, _ = tuesday
    status, body = get(client, "/api/market", arm=ARM, run_id=desk.midday.meeting_id)
    assert status == 200 and body["available"] is True
    assert (body["session"], body["run_id"]) == ("2026-11-16", desk.midday.meeting_id)
    assert body["tickers"]["UPA"]["bars"]["dates"][-1] == "2026-11-16", "the bars stop at the session it built on"
    assert body["tickers"]["UPA"]["after"] is None, "nothing later is on record yet"
    monday = get(client, "/api/market", arm=ARM, session="2026-11-16")[1]
    assert body["captured_at"] == monday["captured_at"] and body["tickers"] == monday["tickers"]

    closed = a_tuesday(tmp_path, close=True)
    later = Reader(closed.journal.path).market(ARM, run_id=closed.midday.meeting_id)
    assert later["tickers"]["UPA"]["after"]["dates"] == [TUESDAY], "what followed, once the day's close is on record"


def test_a_checkin_whose_close_meeting_left_no_data_says_so(tmp_path):
    desk = a_tuesday(tmp_path)
    shutil.rmtree(tmp_path / "snapshots")
    answer = Reader(desk.journal.path).market(ARM, run_id=desk.midday.meeting_id)
    assert (answer["available"], answer["reason"], answer["run_id"]) == (False, "no_snapshot", desk.midday.meeting_id)
    with desk.journal.transaction():
        desk.journal.write("UPDATE runs SET status = 'failed' WHERE arm = ? AND session_date = '2026-11-16'", (ARM,))
    answer = Reader(desk.journal.path).market(ARM, run_id=desk.midday.meeting_id)
    assert (answer["available"], answer["reason"]) == (False, "no_snapshot")


# ── The live stream ──────────────────────────────────────────────────────────

def test_a_page_that_connects_is_told_the_newest_meeting_whether_a_run_or_a_checkin(tuesday, client):
    desk, _ = tuesday
    messages = stream(client, arm=ARM, polls=1)
    assert messages[0]["type"] == "run" and messages[0]["new"] is True
    assert (messages[0]["run"]["run_id"], messages[0]["run"]["kind"], messages[0]["session"]) == (
        desk.midday.meeting_id, "midday", TUESDAY)
    assert [m["event"]["seq"] for m in messages[1:]] == list(range(1, 10))


def test_a_page_following_one_meeting_is_given_its_last_lines_before_being_told_of_the_next(tmp_path):
    desk = a_tuesday(tmp_path)
    reader = Reader(desk.journal.path)
    # The page had seen the first line of the check-in before the open when the midday one took its place.
    messages, run_id, seq, status = reader.tail(ARM, desk.premarket.meeting_id, 0, "running")
    assert [m["type"] for m in messages] == ["event", "run", "run"] + ["event"] * 9
    assert messages[0]["event"]["kind"] == "briefing"
    assert (messages[1]["run"]["run_id"], messages[1]["run"]["status"], messages[1]["new"]) == (
        desk.premarket.meeting_id, "ok", False)
    assert (messages[2]["run"]["run_id"], messages[2]["new"]) == (desk.midday.meeting_id, True)
    assert (run_id, seq, status) == (desk.midday.meeting_id, 9, "ok")
    assert reader.tail(ARM, run_id, seq, status)[0] == [], "nothing new since"

    assert hold(desk, ny(17, 18, 30)).status == "ok"
    messages, run_id, _, _ = reader.tail(ARM, run_id, seq, status)
    assert (messages[0]["run"]["kind"], messages[0]["new"]) == ("close", True) and run_id.startswith("2026-11-17_desk")


# ── A configuration with no desk, and journals from before there was one ─────

def test_the_baseline_is_shown_as_it_always_was_with_its_run_as_the_days_one_meeting(tmp_path):
    desk = a_tuesday(tmp_path)
    baseline = DailyPipeline(
        [Arm("fake/a", ScriptedLLM())], builder=desk.builder, journal=desk.journal, universe=desk.universe,
        runs_dir=tmp_path / "runs", snapshot_dir=tmp_path / "snapshots", now=desk.clock,
    )
    desk.clock.now = ny(17, 18, 0)
    [result] = baseline.run()
    assert result.status == "ok"
    reader = Reader(desk.journal.path)
    shown = reader.meeting("fake/a")
    assert (shown["run"]["run_id"], shown["run"]["kind"], shown["run"]["as_of"], shown["session"]) == (
        result.run_id, "close", None, TUESDAY)
    day = reader.day("fake/a")
    assert [(m["kind"], m["run_id"]) for m in day["meetings"]] == [("close", result.run_id)] and day["watch"] is None
    assert [s["session"] for s in reader.sessions("fake/a")] == [TUESDAY]
    assert reader.tail("fake/a", None, 0, None)[1] == result.run_id
    # And the desk's own meetings are still its own.
    assert reader.meeting(ARM)["run"]["run_id"] == desk.midday.meeting_id


def test_a_journal_no_desk_has_written_to_is_read_without_its_meetings_table(tmp_path):
    desk = a_tuesday(tmp_path)
    old = copy_of(desk.journal.path, tmp_path / "old.sqlite3")
    db = sqlite3.connect(old)
    db.execute("DROP TABLE meetings")
    db.commit()
    db.close()
    reader = Reader(old)
    shown = reader.meeting(ARM)
    assert (shown["session"], shown["run"]["kind"], shown["run"]["as_of"]) == ("2026-11-16", "close", None)
    assert [m["kind"] for m in reader.day(ARM)["meetings"]] == ["close"]
    assert [s["session"] for s in reader.sessions(ARM)] == ["2026-11-16", "2026-11-13"]
    assert reader.market(ARM)["available"] is True and reader.tail(ARM, None, 0, None)[1] == shown["run"]["run_id"]


# ── The ledger is only read ──────────────────────────────────────────────────

def test_the_ledger_is_found_beside_the_journal_or_where_it_is_named_and_is_never_created(tmp_path, monkeypatch):
    desk = a_tuesday(tmp_path)
    assert Reader(desk.journal.path).ledger_path == tmp_path / "ledger.sqlite3"
    assert Reader(desk.journal.path).watch(ARM)["day"] == TUESDAY
    assert Reader(desk.journal.path).watch("fake/a") is None, "another configuration's rounds are not this one's"

    elsewhere = tmp_path / "elsewhere" / "ledger.sqlite3"
    elsewhere.parent.mkdir()
    copy_of(tmp_path / "ledger.sqlite3", elsewhere)
    moved = tmp_path / "moved"
    moved.mkdir()
    copy_of(desk.journal.path, moved / "journal.sqlite3")
    assert Reader(moved / "journal.sqlite3").watch(ARM) is None, "no ledger beside this journal"
    assert not (moved / "ledger.sqlite3").exists(), "and none is made"
    assert Reader(moved / "journal.sqlite3", ledger_path=elsewhere).watch(ARM)["day"] == TUESDAY
    assert get(browser(moved / "journal.sqlite3", tmp_path, ledger_path=elsewhere), "/api/day", arm=ARM)[1]["watch"]["day"] == TUESDAY

    import dashboard.reader as reader_module
    monkeypatch.setattr(reader_module.config, "JOURNAL_PATH", str(desk.journal.path))
    monkeypatch.setattr(reader_module.config, "LEDGER_PATH", str(elsewhere))
    assert Reader().ledger_path == elsewhere, "with nothing named, both are where the settings say"

    (moved / "ledger.sqlite3").write_text("not a ledger")
    assert Reader(moved / "journal.sqlite3").watch(ARM) is None


def test_looking_at_the_desks_pages_leaves_the_journal_and_the_ledger_exactly_as_they_were(tuesday, client):
    desk, folder = tuesday
    before = (fingerprint(folder / "journal.sqlite3"), fingerprint(folder / "ledger.sqlite3"))
    for path, params in [
        ("/api/meeting", {}), ("/api/meeting", {"run_id": desk.premarket.meeting_id}), ("/api/day", {}),
        ("/api/day", {"day": "2026-11-16"}), ("/api/sessions", {}), ("/api/market", {"run_id": desk.midday.meeting_id}),
        ("/api/overview", {}), ("/api/book", {}), ("/api/history", {}), ("/api/status", {}),
    ]:
        assert get(client, path, arm=ARM, **params)[0] == 200, path
    stream(client, arm=ARM, polls=2)
    assert (fingerprint(folder / "journal.sqlite3"), fingerprint(folder / "ledger.sqlite3")) == before
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/day").status_code == 405


# ── One line per figure ──────────────────────────────────────────────────────

def test_each_figures_line_at_a_checkin_is_counted_from_its_own_lines():
    def said(speaker, kind, payload=None):
        return {"speaker": speaker, "kind": kind, "ticker": None, "payload": payload or {}}

    before_open = [
        said("news_desk", "briefing"),
        said("ticker_analyst", "view_update", {"effect": "weakened"}),
        said("ticker_analyst", "view_update", {"effect": "strengthened"}),
        said("ticker_analyst", "view_update", {"effect": "weakened"}),
        said("portfolio_manager", "checkin_decisions", {"decisions": [
            {"action": "withdraw"}, {"action": "trim"}, {"action": "keep"}, {"action": "trim"}]}),
        said("risk_officer", "accepted"), said("risk_officer", "rejected"), said("risk_officer", "risk_approved"),
        said("trader", "order_withdrawn"), said("trader", "order_placed"),
    ]
    assert today(before_open) == {
        "ticker_analyst": "3 条更新：不变 0，加强 1，削弱 2，不成立 0",
        "portfolio_manager": "4 条决定：保留 1，减仓 2，卖出 0，撤回买单 1",
        "risk_officer": "提案：接受 1，拒绝 1；订单：批准 1，削减 0，拒绝 0",
        "trader": "留给收盘会 0 条，撤回买单 1 张，下单 1 张",
    }
    assert today([said("news_desk", "briefing")]) == {}
    # At a close meeting an order taken back is counted as it always was: the trader's line is not the check-in's.
    assert today([said("trader", "order_withdrawn"), said("trader", "order_placed")]) == {"trader": "成交 0 笔，下单 1 张"}


def test_the_desks_account_is_its_own(tuesday, client):
    desk, _ = tuesday
    status, body = get(client, "/api/book", arm=ARM)
    assert status == 200
    assert sorted(position["ticker"] for position in body["positions"]) == sorted(PaperBroker(desk.journal, ARM).positions())
