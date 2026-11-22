"""The dashboard: it shows what the journal holds, word for word, and can change nothing."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

import config
from dashboard import app as dashboard_app
from dashboard import personas
from dashboard.app import create_app, host_named, names_answered
from dashboard.reader import NoJournal, Reader, _process_exists as process_exists, today
from data.models import Snapshot
from data.snapshot import MACRO_SERIES, MARKET_SERIES, load_snapshot
from evaluation import run as evaluation_run
from evaluation.run import evaluate, save
from execution.paper import PaperBroker
from journal.store import Journal
from models.evaluation import Evaluation
from models.signals import DebateRulingDraft, Event, ProposalSetDraft, TickerViewDraft, UsageSummary
from pipeline.daily import Arm
from risk.gate import RiskLimits, halted
from tests.pipeline_fakes import (
    ScriptedLLM, SyntheticMarket, by_ticker, day, make_pipeline, proposals, ruling, session, view,
)
from tests.test_evaluation import nov
from tests.test_evaluation_run import a_lesson
from tests.test_risk_gate import account as gate_account, buy, one

ARM = "fake/a"
ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
STATIC = Path(dashboard_app.__file__).parent / "static"
# Text a model might write that a careless page would treat as markup.
ODD = '<b>UPA</b> & "quoted" <script>alert(1)</script> 不改写'
ADDRESSES = {
    "/api/roster", "/api/arms", "/api/sessions", "/api/meeting", "/api/day", "/api/market", "/api/overview", "/api/book",
    "/api/history", "/api/evaluation", "/api/status", "/api/stream",
}


def a_week(folder, days=(13, 16, 17, 18, 19, 20)):
    """Six sessions: the analyst likes all three candidates, the desk buys UPA on the first day."""
    llm = ScriptedLLM(cost_per_call=0.01)
    llm.replies[TickerViewDraft] = by_ticker({"UPA": view(horizon=3, thesis=ODD)}, view(horizon=3))
    llm.replies[ProposalSetDraft] = proposals(("UPA", "buy", 0.7))
    pipeline = make_pipeline(folder, llm)
    for number in days:
        pipeline._now.now = day(number)
        [result] = pipeline.run()
        assert result.status == "ok", result.message
        llm.replies[ProposalSetDraft] = proposals()
    return pipeline


def one_more(pipeline, number, *items):
    """Another session, in which the portfolio manager proposes `items`."""
    pipeline.arms[0].llm.replies[ProposalSetDraft] = proposals(*items)
    pipeline._now.now = day(number)
    [result] = pipeline.run()
    assert result.status == "ok", result.message


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    """A week of runs and its evaluation report. Shared by the tests that only read."""
    folder = tmp_path_factory.mktemp("week")
    pipeline = a_week(folder)
    report = evaluate(pipeline.journal, pipeline.builder.market, now=day(20), simulations=50, seed=1)
    save(report, folder / "evaluation")
    return pipeline, report, folder


@pytest.fixture
def client(week):
    _, _, folder = week
    return browser(folder / "journal.sqlite3", folder / "evaluation")


def browser(journal, reports, **settings):
    """A browser on this computer looking at the dashboard, which never waits between two looks at the journal."""
    return TestClient(create_app(journal, reports, poll_seconds=0, **settings), base_url="http://127.0.0.1:8765")


def get(client, path, **params):
    response = client.get(path, params=params)
    return response.status_code, response.json()


def stream(client, **params):
    """Everything the live stream sends before it ends, in order."""
    response = client.get("/api/stream", params=params)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    messages = []
    for block in response.text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line and not line.startswith(":"))
        if "data" in fields:
            message = json.loads(fields["data"])
            assert fields["event"] == message["type"]
            messages.append(message)
    return messages


def stamp(hour: int, minute: int = 0) -> datetime:
    return datetime(2025, 11, 13, hour, minute, tzinfo=timezone.utc)


def said(seq: int, speaker="central_bank", remark="Nothing here stands in the way.", to="all") -> Event:
    return Event(seq=seq, ts=stamp(21, seq), stage="macro", speaker=speaker, speaker_name=speaker, is_llm=True,
                 addressed_to=to, kind="assessment", remark=remark, payload={"stance": "neutral", "confidence": 0.5})


# ── It can change nothing ────────────────────────────────────────────────────

def test_every_address_answers_a_read_and_refuses_everything_else(client):
    routes = client.app.routes
    assert {route.path for route in routes if isinstance(route, Route)} == ADDRESSES
    assert all(route.methods <= {"GET", "HEAD"} for route in routes if isinstance(route, Route))
    assert [type(route) for route in routes if not isinstance(route, Route)] == [Mount]       # the page's own files

    for path in sorted(ADDRESSES - {"/api/stream"}) + ["/", "/styles.css", "/js/app.js"]:
        assert client.get(path).status_code == 200, path
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, path).status_code == 405, (method, path)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, "/api/stream").status_code == 405
    # Numbers are never served from a browser's memory, and the page's files are checked before they are reused.
    assert client.get("/api/overview").headers["cache-control"] == "no-store"
    assert client.get("/js/app.js").headers["cache-control"] == "no-cache"


def test_the_journal_is_opened_so_that_it_cannot_be_written(week):
    _, _, folder = week
    reader = Reader(folder / "journal.sqlite3", folder / "evaluation")
    with reader._db() as db:
        for statement in ("DELETE FROM events", "UPDATE runs SET status = 'failed'",
                          "INSERT INTO paper_orders (order_id) VALUES ('x')", "CREATE TABLE extra (x)"):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute(statement)


def test_looking_at_every_page_leaves_the_journal_exactly_as_it_was(week, client):
    pipeline, _, folder = week

    def fingerprint():
        files = [folder / "journal.sqlite3", folder / "journal.sqlite3-wal"]
        return [hashlib.sha256(path.read_bytes()).hexdigest() for path in files if path.exists()]

    before, rows = fingerprint(), len(pipeline.journal.rows("SELECT * FROM events"))
    for path in sorted(ADDRESSES - {"/api/stream"}):
        assert client.get(path, params={"arm": ARM}).status_code == 200
    stream(client, arm=ARM, polls=2)
    assert fingerprint() == before and len(pipeline.journal.rows("SELECT * FROM events")) == rows


def test_without_a_journal_the_page_says_so_and_does_not_create_one(tmp_path):
    missing = tmp_path / "not" / "there" / "journal.sqlite3"
    client = browser(missing, tmp_path / "evaluation")

    assert get(client, "/api/arms") == (200, {"journal": False, "arms": [], "path": str(missing)})
    for path in sorted(ADDRESSES - {"/api/roster", "/api/arms", "/api/stream"}):
        status, body = get(client, path)
        assert status == 404 and body["journal"] is False and "还没有账本" in body["error"], path
    assert stream(client, polls=2) == []
    with pytest.raises(NoJournal):
        Reader(missing).arms()
    assert not (tmp_path / "not").exists()


def test_a_file_that_is_not_a_journal_is_reported_not_crashed_on(tmp_path):
    (tmp_path / "journal.sqlite3").write_text("this is not a database, and it is long enough to be read as one " * 40)
    client = browser(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    for path in ("/api/arms", "/api/overview", "/api/meeting", "/api/status"):
        status, body = get(client, path)
        assert status == 500 and body["error"].startswith("账本读不了"), path
    assert stream(client, polls=2) == []


def test_a_model_configuration_the_journal_does_not_hold_is_refused(client):
    for path in sorted(ADDRESSES - {"/api/roster", "/api/arms", "/api/stream"}):
        status, body = get(client, path, arm="someone/else")
        assert status == 404 and "error" in body, path
    # Without a name, the first one in the journal is shown.
    assert get(client, "/api/overview")[1]["arm"] == ARM


def test_the_page_has_nothing_that_could_send_a_change(client):
    page = client.get("/").text
    assert "交易看板" in page and "只读" in page
    assert not re.search(r"<form|<input|<textarea|method=", page, re.IGNORECASE)

    scripts = {path.name: path.read_text(encoding="utf-8") for path in (STATIC / "js").glob("*.js")}
    assert set(scripts) == {
        "app.js", "charts.js", "day.js", "decisions.js", "dom.js", "emblems.js", "figures.js", "live.js", "market.js",
        "meeting.js", "motion.js", "pages.js", "playback.js", "screen.js", "tape.js", "trail.js"}
    everything = "\n".join(scripts.values())
    # One place asks the server for anything, and it only reads.
    assert [name for name, text in scripts.items() if "fetch(" in text] == ["dom.js"]
    assert scripts["dom.js"].count("fetch(") == 1 and 'fetch(address, { method: "GET"' in scripts["dom.js"]
    for sender in ("XMLHttpRequest", "sendBeacon", "WebSocket", '"POST"', '"PUT"', '"PATCH"', '"DELETE"', ".submit("):
        assert sender not in everything, sender
    # Recorded words reach the page as text, never as markup.
    for markup in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert markup not in everything, markup


@pytest.mark.skipif(NODE is None, reason="node is not installed, so the page's own logic cannot be run here")
def test_the_pages_own_logic_passes_its_tests():
    """Playback, keeping a room in step with a live run, how a decision came about, how the trail shows it line
    by line, which run the 决策 page reads, what the market screens show and when, each figure's panel, text
    kept as text and revealed unchanged, number formats, seats, a check-in of the desk read off its record, and
    the strip of the day's meetings: tests/js, run by node."""
    files = sorted(path.name for path in (ROOT / "tests" / "js").glob("*.test.mjs"))
    assert files == ["checkin.test.mjs", "day.test.mjs", "decisions.test.mjs", "decisions_page.test.mjs",
                     "dom.test.mjs", "format.test.mjs", "live.test.mjs", "market.test.mjs", "motion.test.mjs",
                     "playback.test.mjs", "room.test.mjs", "screen.test.mjs", "trail.test.mjs"]
    result = subprocess.run([NODE, "--test", *(f"tests/js/{name}" for name in files)],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert int(re.search(r"pass (\d+)", result.stdout).group(1)) >= 127 and re.search(r"fail 0\b", result.stdout)


# ── Who is in the room ───────────────────────────────────────────────────────

def test_fourteen_figures_stand_in_the_room_two_of_them_rules_with_bull_and_bear_face_to_face(client):
    status, body = get(client, "/api/roster")
    roster = body["roster"]
    assert status == 200 and len(roster) == 14
    assert sorted(figure["seat"] for figure in roster) == list(range(14))
    assert {figure["id"] for figure in roster if figure["kind"] == "rule"} == {"risk_officer", "trader"}
    seat = {figure["id"]: figure["seat"] for figure in roster}
    assert (seat["bear"] - seat["bull"]) % 14 == 7
    assert (seat["portfolio_manager"], seat["judge"]) == (0, 7)
    assert [stage["id"] for stage in body["stages"]] == [
        "briefing", "settle", "macro", "views", "debate", "proposals", "checks", "risk", "orders"]
    assert all(figure["title"] and figure["does"] for figure in roster)
    # Each has the mark of its field and what it is a specialist in, and no two share a mark.
    assert all(figure["emblem"] and len(figure["craft"]) >= 3 for figure in roster)
    assert len({figure["emblem"] for figure in roster}) == 14
    # The series sorted under a macro analyst's field are ones the snapshot really records.
    recorded = {symbol for _, _, symbol in MARKET_SERIES} | {series[0] for series in MACRO_SERIES} | {"sectors"}
    reads = {figure["id"]: figure["reads"] for figure in roster}
    assert all(set(series) <= recorded for series in reads.values())
    assert {name for name, series in reads.items() if series} == {
        figure["id"] for figure in roster if figure["group"] == "宏观组"}
    assert all(len(reads[name]) >= 4 and len(set(reads[name])) == len(reads[name]) for name in reads if reads[name])


def test_everyone_the_pipeline_records_has_a_place_in_the_room(week):
    pipeline, _, _ = week
    known = {persona.id for persona in personas.ROSTER}
    events = [event for run in pipeline.journal.runs(ARM) for event in pipeline.journal.events(run["run_id"])]
    assert {event.speaker for event in events} <= known | {personas.NARRATOR["id"]}
    assert {event.addressed_to for event in events} <= known | {personas.EVERYONE["id"]}
    assert len({event.speaker for event in events} & known) >= 10            # the week has most of them speaking
    assert personas.title_of("bull") == "多方" and personas.title_of("pipeline") == "流程播报"
    assert personas.title_of("all") == "所有人" and personas.title_of("a_stranger") == "a_stranger"


# ── The meeting ──────────────────────────────────────────────────────────────

def recorded(journal, run_id):
    return [(e.seq, e.stage, e.speaker, e.addressed_to, e.is_llm, e.kind, e.ticker, e.remark, e.payload)
            for e in journal.events(run_id)]


def shown(events):
    return [(e["seq"], e["stage"], e["speaker"], e["to"], e["is_llm"], e["kind"], e["ticker"], e["remark"], e["payload"])
            for e in events]


def test_the_meeting_is_the_record_word_for_word_and_in_order(week, client):
    pipeline, _, _ = week
    status, latest = get(client, "/api/meeting", arm=ARM)
    assert status == 200 and latest["session"] == session(20).isoformat() and latest["run"]["status"] == "ok"
    assert latest["run"]["live"] is False
    assert shown(latest["events"]) == recorded(pipeline.journal, latest["run"]["run_id"])
    assert [event["seq"] for event in latest["events"]] == list(range(1, len(latest["events"]) + 1))

    first = get(client, "/api/meeting", arm=ARM, session=session(13).isoformat())[1]
    assert first["session"] == session(13).isoformat() and first["run"]["run_id"] != latest["run"]["run_id"]
    assert shown(first["events"]) == recorded(pipeline.journal, first["run"]["run_id"])
    # What the model wrote comes through untouched, markup characters and all.
    upa = next(event for event in first["events"] if event["kind"] == "view" and event["ticker"] == "UPA")
    assert upa["payload"]["thesis"] == ODD and upa["remark"] == "I would own this one."
    # Beside the words: Chinese titles for who spoke, to whom, and at which stage.
    assert (upa["speaker_title"], upa["to_title"], upa["stage_title"]) == ("标的分析师", "组合经理", "观点")

    # A day the pipeline never ran: an empty room, not an error.
    assert get(client, "/api/meeting", arm=ARM, session="1999-01-04")[1] == {
        "arm": ARM, "session": None, "run": None, "stands": None, "events": [], "today": {}, "records": latest["records"]}
    assert latest["stands"] is True and first["stands"] is True


def test_each_figures_line_for_the_day_is_counted_from_the_record(client):
    first = get(client, "/api/meeting", arm=ARM, session=session(13).isoformat())[1]["today"]
    assert first["ticker_analyst"] == "3 条观点：看多 3，中性 0，看空 0"
    assert first["central_bank"] == "立场：偏多，把握度 0.60" and first["sentiment"] == "立场：偏多，把握度 0.60"
    assert first["portfolio_manager"] == "1 条提案：买入 1，卖出 0，减仓 0，持有 0"
    assert first["trader"] == "成交 0 笔，下单 1 张"
    assert set(first) == {persona.id for persona in personas.ROSTER}             # on the first day everyone has a line
    # The next session forms its own macro view, so the macro analysts speak again and nothing is carried over.
    # (What the narrator says of a view that was reused is in the test below this one.)
    later = get(client, "/api/meeting", arm=ARM, session=session(16).isoformat())[1]["today"]
    assert later["central_bank"] == first["central_bank"] and later["sentiment"] == first["sentiment"]
    assert "pipeline" not in later, "the narrator only speaks of a macro view that was carried over"
    assert later["trader"] == "成交 1 笔，下单 0 张"


def test_a_figures_line_says_what_it_did_and_how_often():
    def did(speaker, kind, ticker=None, **payload):
        return {"speaker": speaker, "kind": kind, "ticker": ticker, "payload": payload}

    events = [
        did("central_bank", "assessment", stance="restrictive", confidence=0.7),
        did("forex", "assessment", stance="neutral", confidence=0.55),
        did("sentiment", "assessment", stance="supportive", confidence=0.6),
        did("pipeline", "macro_view", session_date="2026-11-13"),
        did("ticker_analyst", "view", "AAA", direction="bullish"), did("ticker_analyst", "view", "BBB", direction="bullish"),
        did("ticker_analyst", "view", "CCC", direction="neutral"), did("ticker_analyst", "view", "DDD", direction="bearish"),
        did("bull", "argument", "AAA"), did("bear", "argument", "AAA"), did("bull", "argument", "AAA"),
        did("bull", "argument", "DDD"), did("bear", "argument", "DDD"),
        did("judge", "ruling", "AAA", stance="own"), did("judge", "ruling", "DDD", stance="do_not_own"),
        did("judge", "ruling", "EEE", stance="do_not_own"),
        did("portfolio_manager", "proposals", proposals=[
            {"action": "buy"}, {"action": "buy"}, {"action": "sell"}, {"action": "trim"},
            {"action": "hold"}, {"action": "hold"}, {"action": "hold"}]),
        did("risk_officer", "accepted", "AAA"), did("risk_officer", "accepted", "BBB"), did("risk_officer", "rejected", "CCC"),
        did("risk_officer", "risk_approved", "AAA"), did("risk_officer", "risk_reduced", "BBB"),
        did("risk_officer", "risk_reduced", "EEE"), did("risk_officer", "risk_rejected", "DDD"),
        did("risk_officer", "risk_rejected", "FFF"), did("risk_officer", "risk_rejected", "GGG"),
        did("trader", "fill", "AAA"), did("trader", "order_placed", "AAA"), did("trader", "order_placed", "BBB"),
        did("trader", "order_withdrawn", "CCC"),
    ]
    assert today(events) == {
        "central_bank": "立场：偏空，把握度 0.70", "forex": "立场：中性，把握度 0.55", "sentiment": "立场：偏多，把握度 0.60",
        "ticker_analyst": "4 条观点：看多 2，中性 1，看空 1",
        "bull": "在 2 场辩论中发言 3 次", "bear": "在 2 场辩论中发言 2 次",
        "judge": "3 条裁决：持有 1，不持有 2",
        "portfolio_manager": "7 条提案：买入 2，卖出 1，减仓 1，持有 3",
        "risk_officer": "提案：接受 2，拒绝 1；订单：批准 1，削减 2，拒绝 3",
        "trader": "成交 1 笔，下单 2 张",
    }
    assert today([]) == {}
    assert today([did("pipeline", "macro_view_reused", session_date="2026-11-13")]) == {"pipeline": "沿用 2026-11-13 的宏观结论"}


def test_a_figures_past_record_is_counted_by_the_last_evaluation_and_only_where_one_exists(week, tmp_path):
    _, report, folder = week
    assert Reader(folder / "journal.sqlite3", folder / "evaluation").meeting(ARM)["records"] == {
        "as_of": session(20).isoformat(),
        "lines": {
            "ticker_analyst": "9 条有方向的观点已评分：方向对了 9 条，跑赢基准 9 条；另有 9 条还没到期",
            "portfolio_manager": "提出的交易里已平仓 0 笔（赢 0，亏 0，其中止损出场 0），还开着 1 笔",
        },
    }
    assert Reader(folder / "journal.sqlite3", tmp_path / "none").meeting(ARM)["records"] == {"as_of": None, "lines": {}}

    # Numbers that differ from each other, so that none can stand in for another.
    [mine] = report.arms
    card = mine.scorecard.model_copy(update={
        "pending": 4, "overall": mine.scorecard.overall.model_copy(update={"views": 12, "right": 7, "beat_benchmark": 5})})
    trades = mine.trades.model_copy(update={"closed": 6, "winners": 3, "losers": 2, "stopped_out": 1, "open": 8})
    save(report.model_copy(update={"arms": [mine.model_copy(update={"scorecard": card, "trades": trades})]}), tmp_path)
    reader = Reader(folder / "journal.sqlite3", tmp_path)
    assert reader.records(ARM)["lines"] == {
        "ticker_analyst": "12 条有方向的观点已评分：方向对了 7 条，跑赢基准 5 条；另有 4 条还没到期",
        "portfolio_manager": "提出的交易里已平仓 6 笔（赢 3，亏 2，其中止损出场 1），还开着 8 笔",
    }

    # Views written, none of them due yet, and nothing traded: said as it is, with no rate made up.
    waiting = card.model_copy(update={"overall": None, "total": 31, "scored": 0, "pending": 31})
    idle = mine.trades.model_copy(update={"closed": 0, "open": 0})
    save(report.model_copy(update={"arms": [mine.model_copy(update={"scorecard": waiting, "trades": idle})]}), tmp_path)
    assert reader.records(ARM)["lines"] == {"ticker_analyst": "写过 31 条观点，还没有一条评分（31 条没到期）"}


def test_every_session_can_be_replayed_newest_first(week, client):
    pipeline, _, _ = week
    status, body = get(client, "/api/sessions", arm=ARM)
    assert status == 200
    assert [item["session"] for item in body["sessions"]] == [session(n).isoformat() for n in (20, 19, 18, 17, 16, 13)]
    for item in body["sessions"]:
        assert item["status"] == "ok" and item["events"] == len(pipeline.journal.events(item["run_id"])) > 0


def test_the_run_shown_for_a_session_is_the_one_going_then_the_one_that_stands_then_the_last_attempt(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    reader = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    start = datetime(2025, 11, 13, 22, tzinfo=timezone.utc)

    def run(run_id, hours, status=None, error=None, when=session(13)):
        journal.start_run(run_id, ARM, when, start + timedelta(hours=hours))
        if status:
            journal.finish_run(run_id, status, start + timedelta(hours=hours, minutes=5), UsageSummary(), error=error)

    def showing(when=session(13)):
        meeting = reader.meeting(ARM, when.isoformat())
        return meeting["run"]["run_id"], meeting["run"]["status"], meeting["run"]["live"]

    run("failed-first", 0, "failed", error="The data source was down.")
    assert showing() == ("failed-first", "failed", False)
    assert reader.meeting(ARM)["run"]["error"] == "The data source was down."
    run("failed-again", 0.5, "failed", error="Still down.")
    assert showing() == ("failed-again", "failed", False)        # nothing stands yet: the last attempt
    run("good", 1, "degraded")
    assert showing() == ("good", "degraded", False)
    run("failed-later", 2, "failed")
    assert showing() == ("good", "degraded", False)              # the one that stands, not the newest attempt
    journal.supersede_runs(ARM, session(13))
    run("rerun", 3, "ok")
    assert showing() == ("rerun", "ok", False)
    assert reader.meeting(ARM)["run"]["seconds"] == 300          # five minutes, start to finish
    run("going", 4)
    assert showing() == ("going", "running", True)
    assert reader.meeting(ARM)["run"]["seconds"] is None         # still going: no duration yet

    run("next-day", 30, "ok", when=session(16))
    assert reader.meeting(ARM)["session"] == session(16).isoformat()            # the latest session when none is named
    assert [item["session"] for item in reader.sessions(ARM)] == [session(16).isoformat(), session(13).isoformat()]
    assert showing() == ("going", "running", True)


# ── The live stream ──────────────────────────────────────────────────────────

def test_a_page_that_connects_is_told_the_newest_run_and_everything_said_in_it(week, client):
    pipeline, _, _ = week
    newest = pipeline.journal.runs(ARM)[0]
    messages = stream(client, arm=ARM, polls=3)

    assert messages[0] == {
        "type": "run", "new": True, "session": session(20).isoformat(),
        "run": get(client, "/api/meeting", arm=ARM)[1]["run"],
    }
    assert all(message["type"] == "event" for message in messages[1:])           # and nothing is sent twice
    assert shown([message["event"] for message in messages[1:]]) == recorded(pipeline.journal, newest["run_id"])


def test_a_page_that_says_where_it_stands_is_sent_only_what_it_has_not_seen(week, client):
    pipeline, _, _ = week
    newest = pipeline.journal.runs(ARM)[0]
    total = len(pipeline.journal.events(newest["run_id"]))

    caught_up = stream(client, arm=ARM, run_id=newest["run_id"], seq=total, status="ok", polls=2)
    assert caught_up == []
    behind = stream(client, arm=ARM, run_id=newest["run_id"], seq=total - 2, status="ok", polls=2)
    assert [(m["type"], m["event"]["seq"]) for m in behind] == [("event", total - 1), ("event", total)]
    # A page that last saw the run still going is told it has finished.
    stale = stream(client, arm=ARM, run_id=newest["run_id"], seq=total, status="running", polls=2)
    assert [(m["type"], m["new"], m["run"]["status"]) for m in stale] == [("run", False, "ok")]
    # A position that is not a number is read as the start.
    for nonsense in ("-5", "x", "1e3"):
        assert len(stream(client, arm=ARM, run_id=newest["run_id"], seq=nonsense, status="ok", polls=2)) == total


def test_what_is_said_during_a_run_reaches_a_page_that_is_already_open(tmp_path, monkeypatch):
    """The page is open before the pipeline has ever run. Each wait of the stream is one more step of a run."""
    path = tmp_path / "journal.sqlite3"
    box = {}

    def journal():
        if "journal" not in box:
            box["journal"] = Journal(path)             # made where it is used: the server's own thread
        return box["journal"]

    steps = iter([
        lambda: journal().start_run("r1", ARM, session(13), stamp(21)),
        lambda: journal().add_event("r1", said(1)),
        lambda: [journal().add_event("r1", said(2, "bull", ODD, to="bear")), journal().add_event("r1", said(3, "bear"))],
        lambda: None,
        lambda: journal().finish_run("r1", "ok", stamp(22), UsageSummary()),
        lambda: journal().start_run("r2", ARM, session(16), stamp(23)),
        lambda: journal().add_event("r2", said(1, "judge")),
    ])

    async def one_step(seconds):
        next(steps, lambda: None)()

    monkeypatch.setattr(dashboard_app, "_pause", one_step)
    client = browser(path, tmp_path / "evaluation")
    messages = stream(client, arm=ARM, polls=9)

    told = [(m["type"], m["run"]["run_id"], m["run"]["status"], m["new"]) if m["type"] == "run"
            else (m["type"], m["event"]["seq"], m["event"]["speaker_title"], m["event"]["remark"]) for m in messages]
    assert told == [
        ("run", "r1", "running", True),
        ("event", 1, "央行分析师", "Nothing here stands in the way."),
        ("event", 2, "多方", ODD),
        ("event", 3, "空方", "Nothing here stands in the way."),
        ("run", "r1", "ok", False),
        ("run", "r2", "running", True),
        ("event", 1, "裁判", "Nothing here stands in the way."),
    ]
    assert messages[0]["run"]["live"] is True and messages[4]["run"]["live"] is False
    assert messages[5]["session"] == session(16).isoformat()


def test_a_run_that_ends_and_is_followed_by_another_between_two_looks_still_has_its_last_word(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    reader = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    journal.start_run("r1", ARM, session(13), stamp(21))
    journal.add_event("r1", said(1))
    messages, run_id, seq, status = reader.tail(ARM, None, 0, None)
    assert [message["type"] for message in messages] == ["run", "event"] and (run_id, seq, status) == ("r1", 1, "running")

    # All of this happens before the page is looked after again.
    journal.add_event("r1", said(2, "bull"))
    journal.add_event("r1", said(3, "bear"))
    journal.finish_run("r1", "degraded", stamp(22), UsageSummary())
    journal.start_run("r2", ARM, session(16), stamp(23))
    journal.add_event("r2", said(1, "judge"))

    messages, run_id, seq, status = reader.tail(ARM, run_id, seq, status)
    assert [(m["type"], m["run"]["run_id"], m["run"]["status"], m["new"]) if m["type"] == "run"
            else (m["type"], m["event"]["seq"], m["event"]["speaker"]) for m in messages] == [
        ("event", 2, "bull"), ("event", 3, "bear"), ("run", "r1", "degraded", False),
        ("run", "r2", "running", True), ("event", 1, "judge"),
    ]
    assert (run_id, seq, status) == ("r2", 1, "running")
    assert reader.tail(ARM, run_id, seq, status) == ([], "r2", 1, "running")

    # A page that had already seen the old run to its end is told nothing about it again.
    journal.finish_run("r2", "ok", stamp(23, 30), UsageSummary())
    journal.start_run("r3", ARM, session(17), stamp(23, 40))
    messages, *_ = reader.tail(ARM, "r2", 1, "ok")
    assert [(m["run"]["run_id"], m["new"]) for m in messages] == [("r3", True)]
    # A run of another model configuration is never mistaken for the one the page was following.
    journal.start_run("other", "fake/b", session(17), stamp(23, 50))
    journal.add_event("other", said(1))
    messages, *_ = reader.tail(ARM, "other", 0, "running")
    assert [(m["type"], m["run"]["run_id"]) for m in messages] == [("run", "r3")]


def a_process_that_has_ended() -> int:
    """The id of a process that existed a moment ago and is gone."""
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    return finished.pid


def test_a_run_whose_process_is_gone_is_not_shown_as_still_going(tmp_path):
    """A run that was killed never writes its ending: the journal goes on saying it is running."""
    path = tmp_path / "journal.sqlite3"
    journal, reader = Journal(path), Reader(path, tmp_path / "evaluation")
    journal.start_run("stands", ARM, session(13), stamp(20))
    journal.add_event("stands", said(1))
    journal.finish_run("stands", "ok", stamp(20, 30), UsageSummary())
    journal.start_run("killed", ARM, session(13), stamp(21), pid=a_process_that_has_ended())
    journal.add_event("killed", said(1, "bull"))
    assert journal.rows("SELECT status FROM runs WHERE run_id = 'killed'")[0]["status"] == "running"

    # The run that stands is shown for the day, not the one that died after it.
    shown_for_day = reader.meeting(ARM, session(13).isoformat())
    assert (shown_for_day["run"]["run_id"], shown_for_day["run"]["live"], shown_for_day["stands"]) == ("stands", False, True)
    assert [(item["run_id"], item["status"], item["live"]) for item in reader.sessions(ARM)] == [("stands", "ok", False)]
    # Asked for by name, the dead run says what it is.
    killed = reader.meeting(ARM, run_id="killed")
    assert (killed["run"]["status"], killed["run"]["live"], killed["stands"]) == ("interrupted", False, False)
    assert [event["speaker"] for event in killed["events"]] == ["bull"]
    assert [(run["run_id"], run["status"], run["live"]) for run in reader.status(ARM)["runs"]] == [
        ("killed", "interrupted", False), ("stands", "ok", False)]
    assert reader.overview(ARM)["latest_run"]["status"] == "interrupted"

    # A page that was following it while it lived is told, and is not left waiting for its next line.
    messages, *_ = reader.tail(ARM, "killed", 1, "running")
    assert [(m["type"], m["run"]["status"], m["run"]["live"], m["new"]) for m in messages] == [("run", "interrupted", False, False)]
    client = browser(path, tmp_path / "evaluation")
    assert stream(client, arm=ARM, polls=2)[0]["run"]["live"] is False

    # With nothing standing for the day, the dead run is the last attempt, and still not live.
    journal.start_run("alone", ARM, session(16), stamp(22), pid=a_process_that_has_ended())
    latest = reader.meeting(ARM)
    assert (latest["run"]["run_id"], latest["run"]["status"], latest["run"]["live"]) == ("alone", "interrupted", False)
    # A run by a process that is there is going; one with no process on record is not.
    journal.start_run("going", ARM, session(17), stamp(23))
    assert reader.meeting(ARM)["run"]["live"] is True and reader.meeting(ARM)["run"]["status"] == "running"
    with journal.transaction():
        journal.write("UPDATE runs SET pid = NULL WHERE run_id = 'going'")
    assert reader.meeting(ARM)["run"]["status"] == "interrupted"

    # Asking whether a process is there: this one is, the system's first one is (it belongs to someone else),
    # and the ids that would mean "every process" are never asked about.
    assert process_exists(os.getpid()) and process_exists(1)
    assert not any(process_exists(pid) for pid in (None, 0, -1, a_process_that_has_ended()))


def test_any_one_run_can_be_opened_by_name_but_only_by_its_own_model_configuration(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    journal.start_run("first-try", ARM, session(13), stamp(20))
    journal.add_event("first-try", said(1, remark="The first attempt."))
    journal.finish_run("first-try", "failed", stamp(20, 5), UsageSummary(), error="No data.")
    journal.start_run("second-try", ARM, session(13), stamp(21))
    journal.add_event("second-try", said(1, remark="The second attempt."))
    journal.add_event("second-try", said(2, "bull"))
    journal.finish_run("second-try", "ok", stamp(21, 5), UsageSummary())
    journal.start_run("theirs", "fake/b", session(13), stamp(22))
    journal.add_event("theirs", said(1, remark="Another configuration's words."))
    client = browser(path, tmp_path / "evaluation")

    day_of = get(client, "/api/meeting", arm=ARM, session=session(13).isoformat())[1]
    assert (day_of["run"]["run_id"], day_of["stands"], len(day_of["events"])) == ("second-try", True, 2)
    earlier = get(client, "/api/meeting", arm=ARM, run_id="first-try")[1]
    assert (earlier["session"], earlier["run"]["status"], earlier["run"]["error"], earlier["stands"]) == (
        session(13).isoformat(), "failed", "No data.", False)
    assert [event["remark"] for event in earlier["events"]] == ["The first attempt."]
    # The name wins over the day, and a name this configuration does not have opens nothing.
    assert get(client, "/api/meeting", arm=ARM, session="1999-01-04", run_id="second-try")[1]["run"]["run_id"] == "second-try"
    for stranger in ("theirs", "no-such-run"):
        nothing = get(client, "/api/meeting", arm=ARM, run_id=stranger)[1]
        assert (nothing["run"], nothing["session"], nothing["stands"], nothing["events"]) == (None, None, None, [])
    assert get(client, "/api/meeting", arm="fake/b", run_id="theirs")[1]["events"][0]["remark"] == "Another configuration's words."


# ── The account ──────────────────────────────────────────────────────────────

def test_the_account_on_the_page_is_the_paper_brokers_own_figures(week, client):
    pipeline, _, _ = week
    broker = PaperBroker(pipeline.journal, ARM)
    books, positions = broker.account(), broker.positions()
    status, body = get(client, "/api/overview", arm=ARM)
    shown_account = body["account"]

    assert status == 200 and positions and books.day_change_pct is not None
    for name in ("cash", "positions_value", "equity", "initial_cash", "high_water", "day_change_pct", "drawdown_pct"):
        assert shown_account[name] == getattr(books, name), name
    assert shown_account["as_of"] == books.session_date.isoformat() == session(20).isoformat()
    assert shown_account["return_pct"] == round((books.equity / books.initial_cash - 1) * 100, 2)
    assert body["positions"] == len(positions) and body["open_orders"] == 0       # the stop is not counted as waiting
    assert body["latest_run"]["session"] == session(20).isoformat() and body["latest_run"]["status"] == "ok"
    assert body["curves"][0]["key"] == "account"
    assert [(p["d"], p["v"]) for p in body["curves"][0]["points"]] == [
        (when.isoformat(), equity) for when, equity in broker.equity_history()]

    book = get(client, "/api/book", arm=ARM)[1]
    assert [row["ticker"] for row in book["positions"]] == sorted(positions)
    for row in book["positions"]:
        mine = positions[row["ticker"]]
        for name in ("shares", "avg_cost", "stop_price", "sector", "last_close", "value", "unrealized_pct"):
            assert row[name] == getattr(mine, name), name
        assert row["entry_date"] == mine.entry_date.isoformat()
        assert row["unrealized_usd"] == round(mine.shares * (mine.last_close - mine.avg_cost), 2)
    [stop] = book["orders"]
    assert (stop["ticker"], stop["side"], stop["kind"], stop["stop_price"]) == ("UPA", "sell", "stop", positions["UPA"].stop_price)
    [fill] = book["fills"]
    assert (fill["ticker"], fill["side"], fill["fill_date"]) == ("UPA", "buy", session(16).isoformat())


def test_the_books_are_read_as_the_paper_broker_reads_them_in_whatever_state_they_are(tmp_path):
    pipeline = a_week(tmp_path, days=(13, 16))
    one_more(pipeline, 17, ("UPA", "sell", 0.7))             # an exit now waits beside the position's stop
    journal = pipeline.journal
    reader, broker = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation"), PaperBroker(journal, ARM)

    def change(sql):
        with journal.transaction():
            journal.write(sql, (ARM,))

    def agreed():
        books, positions = broker.account(), broker.positions()
        account = reader.overview(ARM)["account"]
        for name in ("cash", "positions_value", "equity", "high_water", "day_change_pct", "drawdown_pct"):
            assert account[name] == getattr(books, name), name
        rows = reader.book(ARM)["positions"]
        assert [row["ticker"] for row in rows] == sorted(positions)
        for row in rows:
            for name in ("shares", "avg_cost", "stop_price", "last_close", "value", "unrealized_pct"):
                assert row[name] == getattr(positions[row["ticker"]], name), name
        return account, rows

    _, [held] = agreed()
    assert held["stop_price"] is not None                          # the waiting sell does not hide the stop
    assert reader.overview(ARM)["open_orders"] == 1                # and it is the one order waiting to fill
    assert sorted((order["side"], order["kind"]) for order in reader.book(ARM)["orders"]) == [("sell", "market"), ("sell", "stop")]

    # Equity below the high it once reached: a fall, never a negative one.
    change("UPDATE paper_accounts SET high_water = high_water + 500 WHERE account = ?")
    account, _ = agreed()
    assert account["drawdown_pct"] > 0 and account["high_water"] > account["equity"]
    # A high on record below what the account is worth now: today's worth is the high.
    change("UPDATE paper_accounts SET high_water = 1 WHERE account = ?")
    account, _ = agreed()
    assert account["high_water"] == account["equity"] and account["drawdown_pct"] == 0

    # A position with no price today is carried at the close it was last matched to, and claims no gain or loss.
    change("UPDATE paper_positions SET last_close = NULL WHERE account = ?")
    _, [unpriced] = agreed()
    assert unpriced["value"] > 0 and (unpriced["last_close"], unpriced["unrealized_pct"], unpriced["unrealized_usd"]) == (None,) * 3

    # The exit fills at the next open: nothing held, the newest fill listed first.
    one_more(pipeline, 18)
    account, rows = agreed()
    assert rows == [] and account["positions_value"] == 0 and account["cash"] == account["equity"]
    book = reader.book(ARM)
    assert [(fill["side"], fill["fill_date"]) for fill in book["fills"]] == [
        ("sell", session(18).isoformat()), ("buy", session(16).isoformat())]
    assert book["orders"] == [] and reader.overview(ARM)["open_orders"] == 0


def test_after_a_single_session_there_is_no_change_on_the_day_to_show(tmp_path):
    a_week(tmp_path, days=(13,))
    overview = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation").overview(ARM)
    assert overview["account"]["day_change_pct"] is None and overview["account"]["equity"] == 100_000
    assert overview["guards"]["daily_loss"] == {"limit_pct": config.RISK_DAILY_LOSS_LIMIT_PCT, "value_pct": None, "tripped": False}
    assert len(overview["curves"][0]["points"]) == 1
    assert (overview["positions"], overview["open_orders"]) == (0, 1)             # the buy waits for the next open


def test_the_candidates_and_signals_are_those_of_the_last_finished_session(week, client):
    pipeline, _, _ = week
    table = pipeline.journal.signal_table(pipeline.journal.completed_run(ARM, session(20)))
    signals = get(client, "/api/book", arm=ARM)[1]["signals"]

    assert (signals["session"], signals["status"], signals["universe_size"]) == (session(20).isoformat(), "ok", 5)
    assert signals["candidates"] == table.candidates and signals["halted"] == "" and signals["warnings"] == table.warnings
    assert signals["regime"] == {"risk_appetite": table.regime.risk_appetite, "confidence": table.regime.confidence,
                                 "formed_on": table.regime.session_date.isoformat()}
    assert [(v["ticker"], v["held"], v["direction"], v["confidence"], v["horizon_days"], v["thesis"])
            for v in signals["views"]] == [(v.ticker, v.held, v.direction, v.confidence, v.horizon_days, v.thesis)
                                           for v in table.views]
    assert next(v for v in signals["views"] if v["ticker"] == "UPA")["thesis"] == ODD
    assert signals["proposals"] == [proposal.model_dump() for proposal in table.proposals]
    assert signals["risk"] == [decision.model_dump() for decision in table.risk]


def test_a_configuration_that_has_only_failed_shows_its_runs_and_no_account(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.start_run("r1", ARM, session(13), stamp(21))
    journal.finish_run("r1", "failed", stamp(22), UsageSummary(calls=2, input_tokens=10), error="No data today.")
    client = browser(tmp_path / "journal.sqlite3", tmp_path / "evaluation")

    overview = get(client, "/api/overview", arm=ARM)[1]
    assert overview["account"] is None and overview["latest_run"]["status"] == "failed"
    assert [curve["points"] for curve in overview["curves"]] == [[]]
    assert not overview["guards"]["daily_loss"]["tripped"] and not overview["guards"]["drawdown"]["tripped"]
    assert overview["guards"]["kill_switch"]["last_run"] is None             # no run has finished, so none recorded it
    book = get(client, "/api/book", arm=ARM)[1]
    assert (book["positions"], book["orders"], book["fills"], book["signals"]) == ([], [], [], None)
    status = get(client, "/api/status", arm=ARM)[1]
    assert [(run["status"], run["error"], run["llm_calls"]) for run in status["runs"]] == [("failed", "No data today.", 2)]
    assert (status["totals"]["runs"], status["totals"]["failed"], status["totals"]["cost"]) == (1, 1, None)


# ── The switch and the breakers ──────────────────────────────────────────────

def test_a_breaker_is_shown_tripped_exactly_when_the_gate_refuses_to_buy(monkeypatch):
    monkeypatch.setattr(config, "RISK_DAILY_LOSS_LIMIT_PCT", 2.0)
    monkeypatch.setattr(config, "RISK_MAX_DRAWDOWN_PCT", 6.0)
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", "/nowhere/HALT")
    monkeypatch.delenv("TRADING_HALT", raising=False)
    limits = RiskLimits.from_config()

    def gate_refuses(word, **figures):
        decision = one(buy("AAA", 10), acct=gate_account(**figures), lim=limits)
        return decision.verdict == "rejected" and word in decision.reasons[0]

    for change in (None, 0.0, -1.99, -2.0, -2.01, -9.0, 3.0):
        guards = Reader._guards({"day_change_pct": change, "drawdown_pct": 0.0})
        assert guards["daily_loss"]["tripped"] is gate_refuses("Daily loss", day_change_pct=change), change
        assert guards["daily_loss"] == {"limit_pct": 2.0, "value_pct": change, "tripped": change is not None and change <= -2}
    for fall in (0.0, 5.99, 6.0, 6.01, 40.0):
        guards = Reader._guards({"day_change_pct": 0.0, "drawdown_pct": fall})
        assert guards["drawdown"]["tripped"] is gate_refuses("Drawdown", drawdown_pct=fall), fall
        assert guards["drawdown"] == {"limit_pct": 6.0, "value_pct": fall, "tripped": fall >= 6}


def test_the_kill_switch_is_shown_as_the_gate_itself_sees_it(week, client, tmp_path, monkeypatch):
    switch = tmp_path / "HALT"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(switch))
    monkeypatch.delenv("TRADING_HALT", raising=False)
    last_run = {"session": session(20).isoformat(), "halted": ""}        # what the last finished run wrote down itself
    assert get(client, "/api/overview", arm=ARM)[1]["guards"]["kill_switch"] == {
        "on": False, "reason": "", "file": str(switch), "last_run": last_run}

    switch.touch()
    shown_switch = get(client, "/api/overview", arm=ARM)[1]["guards"]["kill_switch"]
    assert shown_switch == {"on": True, "reason": halted(), "file": str(switch), "last_run": last_run}
    assert str(switch) in shown_switch["reason"]

    switch.unlink()
    monkeypatch.setenv("TRADING_HALT", "1")
    shown_switch = get(client, "/api/overview", arm=ARM)[1]["guards"]["kill_switch"]
    assert shown_switch["on"] is True and "TRADING_HALT" in shown_switch["reason"]


def test_what_a_halted_run_wrote_down_is_shown_beside_the_switch_as_it_is_now(tmp_path, monkeypatch):
    """The switch as the page sees it now and what the pipeline recorded at the time are two things, shown apart."""
    switch = tmp_path / "HALT"
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(switch))
    monkeypatch.delenv("TRADING_HALT", raising=False)
    switch.touch()
    pipeline = a_week(tmp_path / "records", days=(13,))
    written = pipeline.journal.signal_table(pipeline.journal.completed_run(ARM, session(13))).halted
    assert str(switch) in written

    switch.unlink()                                     # lifted since: the record of that evening does not change
    reader = Reader(tmp_path / "records" / "journal.sqlite3", tmp_path / "evaluation")
    assert reader.overview(ARM)["guards"]["kill_switch"] == {
        "on": False, "reason": "", "file": str(switch), "last_run": {"session": session(13).isoformat(), "halted": written}}
    assert reader.book(ARM)["signals"]["halted"] == written
    # And where the sessions are set side by side, that one is marked as halted.
    assert [(item["session"], item["halted"]) for item in reader.history(ARM)["sessions"]] == [(session(13).isoformat(), True)]


# ── How decisions changed from session to session ────────────────────────────

def test_the_decisions_page_sets_each_tickers_sessions_side_by_side_as_the_signal_tables_have_them(week, client, monkeypatch):
    pipeline, _, _ = week
    journal = pipeline.journal
    status, body = get(client, "/api/history", arm=ARM)
    days = [session(n) for n in (13, 16, 17, 18, 19, 20)]

    assert status == 200 and [item["session"] for item in body["sessions"]] == [when.isoformat() for when in days]   # oldest first
    assert body["tickers"] == ["UPA", "UPB", "UPC"]
    for item, when in zip(body["sessions"], days):
        assert item["run_id"] == journal.completed_run(ARM, when)          # the run each column was read from
        table = journal.signal_table(item["run_id"])
        assert (item["status"], item["halted"]) == (table.status, False)
        assert item["regime"] == {"risk_appetite": table.regime.risk_appetite, "confidence": table.regime.confidence}
        assert sorted(item["tickers"]) == sorted({view.ticker for view in table.views})
        for view in table.views:
            assert (item["tickers"][view.ticker]["direction"], item["tickers"][view.ticker]["confidence"]) == (view.direction, view.confidence)
            assert item["tickers"][view.ticker]["held"] is view.held
        for proposal in table.proposals:
            mine = item["tickers"][proposal.ticker]
            assert (mine["action"], mine["source"], mine["check"]) == (proposal.action, proposal.source, proposal.status)
        for decision in table.risk:
            assert (item["tickers"][decision.ticker]["verdict"], item["tickers"][decision.ticker]["approved_shares"]) == (
                decision.verdict, decision.approved_shares)
        assert {ticker: cell["ordered"] for ticker, cell in item["tickers"].items() if cell["ordered"]} == {
            order.ticker: {"side": order.side, "shares": order.shares} for order in table.orders}

    # What the week was: UPA bought on the first day and held from the next, the other two only ever viewed.
    first, second = body["sessions"][0]["tickers"], body["sessions"][1]["tickers"]
    assert first["UPA"]["action"] == "buy" and first["UPA"]["ordered"]["side"] == "buy" and first["UPA"]["held"] is False
    assert first["UPA"]["verdict"] in ("approved", "reduced") and first["UPA"]["approved_shares"] == first["UPA"]["ordered"]["shares"]
    assert (second["UPA"]["held"], second["UPA"]["action"], second["UPA"]["source"], second["UPA"]["ordered"]) == (True, "hold", "default", None)
    assert first["UPB"] == {"direction": "bullish", "confidence": 0.6, "held": False, "action": None, "source": None,
                            "check": None, "repeats": 0, "kept_by_default": False, "verdict": None,
                            "approved_shares": None, "ordered": None}
    assert (second["UPA"]["check"], second["UPA"]["repeats"], second["UPA"]["kept_by_default"]) == ("accepted", 0, False)

    # Only so many sessions are set side by side: the most recent ones, still oldest first.
    monkeypatch.setattr("dashboard.reader.SESSIONS_SHOWN", 2)
    assert [item["session"] for item in get(client, "/api/history", arm=ARM)[1]["sessions"]] == [
        session(19).isoformat(), session(20).isoformat()]


def test_a_session_that_was_run_again_is_counted_once_and_tickers_are_listed_latest_first(tmp_path):
    pipeline = a_week(tmp_path, days=(13, 16))
    journal = pipeline.journal
    # The 16th is run again, this time selling: the earlier run of that day no longer stands.
    journal.supersede_runs(ARM, session(16))
    pipeline.arms[0].llm.replies[ProposalSetDraft] = proposals(("UPA", "sell", 0.7))
    pipeline._now.now = day(16)
    [again] = pipeline.run(force=True)
    assert again.status == "ok"
    # And a session that only failed leaves nothing to set beside the others.
    journal.start_run("failed", ARM, session(17), stamp(21))
    journal.finish_run("failed", "failed", stamp(22), UsageSummary(), error="No data.")

    history = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation").history(ARM)
    assert [item["session"] for item in history["sessions"]] == [session(13).isoformat(), session(16).isoformat()]
    assert history["sessions"][1]["tickers"]["UPA"]["action"] == "sell"                     # the run that stands
    assert history["sessions"][1]["tickers"]["UPA"]["ordered"] == {"side": "sell", "shares": history["sessions"][1]["tickers"]["UPA"]["approved_shares"]}
    assert history["sessions"][1]["run_id"] == again.run_id
    assert history["tickers"] == ["UPA", "UPB", "UPC"]

    # Should a day ever be left with two finished runs, it is still one column: that of the run started last.
    with journal.transaction():
        journal.write("UPDATE runs SET status = 'ok' WHERE status = 'superseded'")
    twice = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation").history(ARM)
    assert [(item["session"], item["run_id"]) for item in twice["sessions"]] == [
        (item["session"], item["run_id"]) for item in history["sessions"]]
    with journal.transaction():
        journal.write("UPDATE runs SET status = 'superseded' WHERE session_date = ? AND run_id != ? AND status = 'ok'",
                      (session(16).isoformat(), again.run_id))

    # A ticker seen only on the first day comes after those seen on the last, whatever its name.
    empty = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    with journal.transaction():
        journal.write("UPDATE runs SET signals_json = REPLACE(signals_json, 'UPB', 'AAA') WHERE session_date = ? AND arm = ?",
                      (session(13).isoformat(), ARM))
    assert empty.history(ARM)["tickers"] == ["UPA", "UPB", "UPC", "AAA"]

    # A holding the analyst could give no view of that day (its data was unusable) is still shown as held.
    standing = journal.completed_run(ARM, session(16))
    table = journal.signal_table(standing)
    assert "UPA" in {position.ticker for position in table.holdings}
    without_view = table.model_copy(update={"views": [view for view in table.views if view.ticker != "UPA"]})
    with journal.transaction():
        journal.write("UPDATE runs SET signals_json = ? WHERE run_id = ?", (without_view.model_dump_json(), standing))
    unseen = empty.history(ARM)["sessions"][1]["tickers"]["UPA"]
    assert (unseen["held"], unseen["direction"], unseen["confidence"], unseen["action"]) == (True, None, None, "sell")
    # And a table that lists no holdings at all still shows a holding as held, because its view says so.
    assert next(view for view in table.views if view.ticker == "UPA").held is True
    with journal.transaction():
        journal.write("UPDATE runs SET signals_json = ? WHERE run_id = ?",
                      (table.model_copy(update={"holdings": []}).model_dump_json(), standing))
    listed = empty.history(ARM)["sessions"][1]["tickers"]
    assert (listed["UPA"]["held"], listed["UPA"]["direction"], listed["UPB"]["held"]) == (True, "bullish", False)
    assert Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation").history("fake/b") == {
        "arm": "fake/b", "sessions": [], "tickers": []}


def an_awkward_week(folder):
    """Three sessions in which the portfolio manager writes its list the ways the checks have to sort out.

    13th: buys UPA. 16th (UPA now held): holds UPA, then asks to sell it as well, then asks to buy nothing at
    all. 17th: asks to buy UPA, which is held already, writing the ticker in small letters with spaces, and
    then to trim it.
    """
    pipeline = a_week(folder, days=(13,))
    one_more(pipeline, 16, ("UPA", "hold", 0.5), ("UPA", "sell", 0.7), ("  ", "buy", 0.5))
    one_more(pipeline, 17, (" upa ", "buy", 0.9), ("UPA", "trim", 0.4))
    return pipeline


def test_only_the_first_proposal_for_a_ticker_is_shown_as_the_decision_as_the_pipeline_counts_it(tmp_path):
    pipeline = an_awkward_week(tmp_path)
    journal = pipeline.journal
    first, second, third = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation").history(ARM)["sessions"]

    # What the pipeline itself made of the two lists.
    made = [[(p.ticker, p.action, p.source, p.status) for p in journal.signal_table(item["run_id"]).proposals]
            for item in (second, third)]
    assert made == [
        [("UPA", "hold", "portfolio_manager", "accepted"), ("UPA", "sell", "portfolio_manager", "rejected"),
         ("", "buy", "portfolio_manager", "rejected")],
        [("UPA", "buy", "portfolio_manager", "rejected"), ("UPA", "trim", "portfolio_manager", "rejected"),
         ("UPA", "hold", "default", "accepted")],
    ]
    assert first["tickers"]["UPA"]["ordered"]["side"] == "buy" and first["tickers"]["UPA"]["repeats"] == 0

    # The 16th: the hold stands. The sell that came after it does not count, and nothing was sold.
    held = second["tickers"]["UPA"]
    assert (held["held"], held["action"], held["source"], held["check"]) == (True, "hold", "portfolio_manager", "accepted")
    assert (held["repeats"], held["kept_by_default"], held["verdict"], held["ordered"]) == (1, False, None, None)
    assert journal.signal_table(second["run_id"]).orders == []
    # The proposal written for no ticker at all is no ticker's decision: it gets no row of its own.
    assert sorted(second["tickers"]) == ["UPA", "UPB", "UPC"]

    # The 17th: the buy is the decision, and it was turned back; the trim does not count; the holding is kept
    # by the checks' own default.
    kept = third["tickers"]["UPA"]
    assert (kept["action"], kept["source"], kept["check"]) == ("buy", "portfolio_manager", "rejected")
    assert (kept["repeats"], kept["kept_by_default"], kept["ordered"]) == (1, True, None)
    assert sorted(third["tickers"]) == ["UPA", "UPB", "UPC"]                # no column of its own for " upa "


@pytest.mark.skipif(NODE is None, reason="node is not installed, so the page's own logic cannot be run here")
def test_the_page_reads_a_ticker_out_of_a_proposal_exactly_as_the_pipeline_does():
    """The pipeline reads it with str.strip().upper() (pipeline/checks.py). The characters Python takes off the
    ends are not quite the ones JavaScript's own trim() takes off, so the page is held to Python's, one by one."""
    edges = sorted({point for point in range(sys.maxunicode + 1) if chr(point).isspace()}
                   | set(range(0x21)) | set(range(0x7F, 0xA1)) | {0xAD, 0x180E, 0x200B, 0x2060, 0xFEFF})
    written = [f"{chr(point)}upa{chr(point)}" for point in edges]
    written += [" tmo ", "\tBrk.b\r\n", "upa", "", "   ", "u p a", "\x1f \x85upb　\x1c"]
    result = subprocess.run([NODE, "tests/js/ticker_of.mjs"], input=json.dumps(written), cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [text.strip().upper() for text in written]
    # The two languages do differ here, which is why this is checked: each strips something the other keeps.
    assert "\x1fupa\x1f".strip() == "upa" and "﻿upa﻿".strip() != "upa"


@pytest.mark.skipif(NODE is None, reason="node is not installed, so the page's own logic cannot be run here")
def test_the_trail_and_the_grid_tell_the_same_story_of_the_same_run(tmp_path):
    """The grid is read from a run's signal table, the trail from that run's events. Set side by side for
    real runs, awkward lists included, they must agree on every ticker."""
    an_awkward_week(tmp_path)
    client = browser(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    history = get(client, "/api/history", arm=ARM)[1]
    assert len(history["sessions"]) == 3
    endings = []
    for item in history["sessions"]:
        status, meeting = get(client, "/api/meeting", arm=ARM, run_id=item["run_id"])
        assert status == 200 and meeting["run"]["run_id"] == item["run_id"] and meeting["stands"] is True
        result = subprocess.run([NODE, "tests/js/trail_of.mjs"], input=json.dumps(meeting["events"]), cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        trail = {row["ticker"]: row for row in json.loads(result.stdout)}
        assert sorted(trail) == sorted(item["tickers"])
        for ticker, cell in item["tickers"].items():
            row = trail[ticker]
            assert (row["view"]["direction"], row["view"]["confidence"], row["view"]["held"]) == (
                cell["direction"], cell["confidence"], cell["held"])
            if cell["source"] == "portfolio_manager":
                assert (row["proposal"]["action"], row["check"]["status"], row["check"]["action"]) == (
                    cell["action"], cell["check"], cell["action"])
            else:                                       # nothing proposed: no decision, or the checks' default
                assert row["proposal"] is None
                assert (row["check"] and (row["check"]["source"], row["check"]["action"])) == (
                    ("default", cell["action"]) if cell["source"] == "default" else None)
            assert row["repeats"] == cell["repeats"]
            assert ((row["check"] or {}).get("kept") is not None) == cell["kept_by_default"]
            assert (row["risk"] and row["risk"]["verdict"]) == cell["verdict"]
            assert (row["risk"] and row["risk"]["approved"]) == cell["approved_shares"]
            assert (row["order"] and {"side": row["order"]["side"], "shares": row["order"]["shares"]}) == cell["ordered"]
        endings.append(trail["UPA"]["ended"])
    shares = history["sessions"][0]["tickers"]["UPA"]["ordered"]["shares"]
    assert endings == [
        {"kind": "ordered", "side": "buy", "shares": shares},
        {"kind": "kept", "source": "portfolio_manager"},
        {"kind": "check_rejected", "action": "buy", "kept": True},
    ]


@pytest.mark.skipif(NODE is None, reason="node is not installed, so the page's own logic cannot be run here")
def test_a_holding_sold_by_the_checks_own_default_is_told_the_same_way_by_the_trail_and_the_grid(tmp_path):
    """The manager's answer leaves a holding out, the analyst is bearish on it and the judge has ruled against
    owning it: the checks sell it (pipeline/checks.py). Read from a run the real pipeline made."""
    pipeline = a_week(tmp_path, days=(13,))                          # UPA is bought
    llm = pipeline.arms[0].llm
    llm.replies[TickerViewDraft] = by_ticker({"UPA": view(direction="bearish", confidence=0.7, horizon=3)}, view(horizon=3))
    llm.replies[DebateRulingDraft] = lambda system, user: ruling("do_not_own" if "UPA [held]:" in user else "own")
    one_more(pipeline, 16, ("UPB", "buy", 0.6))                      # and not a word about UPA
    client = browser(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    _, sold_on = get(client, "/api/history", arm=ARM)[1]["sessions"]
    cell = sold_on["tickers"]["UPA"]
    assert (cell["held"], cell["action"], cell["source"], cell["check"], cell["kept_by_default"]) == (
        True, "sell", "default", "accepted", False)
    assert cell["verdict"] == "approved" and cell["ordered"] == {"side": "sell", "shares": cell["approved_shares"]}

    events = get(client, "/api/meeting", arm=ARM, run_id=sold_on["run_id"])[1]["events"]
    result = subprocess.run([NODE, "tests/js/trail_of.mjs"], input=json.dumps(events), cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    [row] = [row for row in json.loads(result.stdout) if row["ticker"] == "UPA"]
    assert row["proposal"] is None
    assert (row["check"]["source"], row["check"]["action"], row["check"]["kept"]) == ("default", "sell", None)
    assert (row["risk"]["verdict"], row["risk"]["approved"]) == (cell["verdict"], cell["approved_shares"])
    assert row["ended"] == {"kind": "ordered", "side": "sell", "shares": cell["approved_shares"]}
    said = next(event for event in events if event["ticker"] == "UPA" and event["stage"] == "checks")
    assert said["remark"].startswith("UPA is sold by default: the portfolio manager gave no decision for it")


# ── The market data a run worked from ────────────────────────────────────────

def snapshot_file(journal, run_id) -> Path:
    return Path(journal.rows("SELECT snapshot_path FROM runs WHERE run_id = ?", (run_id,))[0]["snapshot_path"])


def as_sent(bars, start=None, stop=None) -> dict:
    """Bars the way the page is sent them."""
    return {"dates": [day.isoformat() for day in bars.dates[start:stop]],
            **{column: getattr(bars, column)[start:stop] for column in ("open", "high", "low", "close", "volume")}}


def test_the_market_data_shown_for_a_run_is_what_its_own_snapshot_recorded(week, client):
    pipeline, _, _ = week
    journal = pipeline.journal
    run_id = journal.completed_run(ARM, session(16))
    snapshot = load_snapshot(snapshot_file(journal, run_id))
    status, body = get(client, "/api/market", arm=ARM, session=session(16).isoformat())

    assert status == 200 and (body["available"], body["reason"]) == (True, None)
    assert (body["arm"], body["session"], body["run_id"]) == (ARM, session(16).isoformat(), run_id)
    assert body["captured_at"] == snapshot.created_at.isoformat()
    assert sorted(body["tickers"]) == sorted(snapshot.tickers) and len(snapshot.tickers) >= 3
    for ticker, item in snapshot.tickers.items():
        mine = body["tickers"][ticker]
        assert len(item.bars.dates) > 120                          # more on record than the page is sent
        assert mine["bars"] == as_sent(item.bars, -120)            # the latest ones, exactly as recorded
        assert mine["bars"]["dates"][-1] == session(16).isoformat()        # and none from after the session
        assert mine["price"] == item.price.model_dump(mode="json")
        assert mine["technicals"] == item.technicals.model_dump(mode="json")
        assert (mine["name"], mine["sector"], mine["industry"]) == (
            item.fundamentals.name, item.fundamentals.sector, item.fundamentals.industry)
        assert mine["fundamentals"] == item.fundamentals.model_dump(
            mode="json", exclude={"name", "quote_type", "sector", "industry"})
        assert mine["usable"] is item.usable and mine["issues"] == [issue.model_dump(mode="json") for issue in item.issues]
        assert mine["next_earnings"] == (item.next_earnings.isoformat() if item.next_earnings else None)
    assert body["market"] == [point.model_dump(mode="json") for point in snapshot.market]
    assert body["macro"] == [point.model_dump(mode="json") for point in snapshot.macro]

    # The same run by name, and with nothing named the latest session.
    assert get(client, "/api/market", arm=ARM, run_id=run_id)[1] == body
    assert get(client, "/api/market", arm=ARM)[1]["session"] == session(20).isoformat()
    # A run that is not this configuration's, or not there at all, has no market data to show.
    for asked in ({"arm": ARM, "run_id": "no-such-run"}, {"arm": ARM, "session": "1999-01-04"}):
        empty = get(client, "/api/market", **asked)[1]
        assert (empty["available"], empty["reason"], empty["tickers"], empty["market"], empty["macro"]) == (
            False, "no_run", {}, [], [])


def test_what_prices_did_after_a_session_is_read_from_later_sessions_snapshots(week, client):
    pipeline, _, _ = week
    journal = pipeline.journal
    newest = load_snapshot(snapshot_file(journal, journal.completed_run(ARM, session(20))))
    first = get(client, "/api/market", arm=ARM, session=session(13).isoformat())[1]
    assert sorted(first["tickers"]) == sorted(newest.tickers)
    for ticker, mine in first["tickers"].items():
        later = newest.tickers[ticker].bars
        cut = later.dates.index(session(13))
        # Everything the newest snapshot has for the days after that session, and nothing of that day or before.
        assert mine["after"] == {"through": session(20).isoformat(), **as_sent(later, cut + 1)}
        assert mine["after"]["dates"][0] > mine["bars"]["dates"][-1] == session(13).isoformat()
        assert mine["after"]["dates"] == [session(n).isoformat() for n in (16, 17, 18, 19, 20)]
    # The newest session has nothing after it yet.
    last = get(client, "/api/market", arm=ARM)[1]
    assert last["tickers"] and all(item["after"] is None for item in last["tickers"].values())


def test_bars_after_a_session_are_only_joined_on_when_they_are_on_the_same_price_scale(tmp_path, monkeypatch):
    # UPB splits two for one on the 17th: from then on the source gives all of its prices, old ones too, halved.
    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = view(horizon=3)
    llm.replies[ProposalSetDraft] = proposals()
    pipeline = make_pipeline(tmp_path, llm, market=SyntheticMarket(splits={("UPB", session(17)): 2.0}))
    for number in (13, 16, 17):
        pipeline._now.now = day(number)
        [result] = pipeline.run()
        assert result.status in ("ok", "degraded"), result.message
    journal = pipeline.journal
    reader = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    files = {number: snapshot_file(journal, journal.completed_run(ARM, session(number))) for number in (13, 16, 17)}
    kept = {number: file.read_bytes() for number, file in files.items()}
    old, new = load_snapshot(files[13]).tickers["UPB"].bars, load_snapshot(files[17]).tickers["UPB"].bars
    assert new.close[new.dates.index(session(13))] == pytest.approx(old.close[-1] / 2, rel=1e-3)      # the split, in the data

    first = reader.market(ARM, session(13).isoformat())["tickers"]
    # UPA is untouched: its later bars run to the newest session.
    assert (first["UPA"]["after"]["through"], first["UPA"]["after"]["dates"]) == (
        session(17).isoformat(), [session(16).isoformat(), session(17).isoformat()])
    # UPB's newest snapshot is on the new scale, so it is passed over for the one before the split.
    middle = load_snapshot(files[16]).tickers["UPB"].bars
    assert first["UPB"]["after"] == {"through": session(16).isoformat(), **as_sent(middle, middle.dates.index(session(13)) + 1)}
    assert first["UPB"]["after"]["close"] == [middle.close[-1]] and first["UPB"]["bars"]["close"][-1] == old.close[-1]
    # From the 16th, the only later snapshot is on the other scale: nothing is joined on.
    second = reader.market(ARM, session(16).isoformat())["tickers"]
    assert second["UPB"]["after"] is None and second["UPA"]["after"]["dates"] == [session(17).isoformat()]

    # Only so many bars are sent, the latest before the session and the first after it.
    monkeypatch.setattr("dashboard.reader.BARS_SHOWN", 5)
    monkeypatch.setattr("dashboard.reader.AFTER_SHOWN", 1)
    short = reader.market(ARM, session(13).isoformat())["tickers"]["UPA"]
    assert short["bars"] == as_sent(load_snapshot(files[13]).tickers["UPA"].bars, -5)
    assert short["after"]["dates"] == [session(16).isoformat()]
    # Reading changes none of the snapshot files.
    assert {number: file.read_bytes() for number, file in files.items()} == kept


def test_a_snapshot_is_read_once_and_one_that_is_gone_or_broken_is_said_to_be_missing(tmp_path, monkeypatch):
    pipeline = a_week(tmp_path, days=(13, 16))
    journal = pipeline.journal
    reader = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    older, newer = (snapshot_file(journal, journal.completed_run(ARM, session(number))) for number in (13, 16))
    read = []
    parse = Snapshot.model_validate_json.__func__
    monkeypatch.setattr(Snapshot, "model_validate_json",
                        classmethod(lambda cls, text, **options: read.append(len(text)) or parse(cls, text, **options)))

    whole = reader.market(ARM, session(13).isoformat())
    assert len(read) == 2                                         # the run's own snapshot and the later one
    assert reader.market(ARM, session(13).isoformat()) == whole and reader.market(ARM)["available"]
    assert len(read) == 2                                         # nothing is read a second time
    # A snapshot whose file has changed is read again.
    changed = load_snapshot(older)
    changed.tickers["UPA"].price.close = 1234.5
    older.write_text(changed.model_dump_json(), encoding="utf-8")
    os.utime(older, ns=(1, 1))
    del read[:]                                                   # (this test's own reading above was counted too)
    assert reader.market(ARM, session(13).isoformat())["tickers"]["UPA"]["price"]["close"] == 1234.5 and len(read) == 1
    assert reader.market(ARM, session(13).isoformat())["tickers"]["UPA"]["price"]["close"] == 1234.5 and len(read) == 1
    # No more are kept in memory than there is room for: the one read longest ago makes way.
    monkeypatch.setattr("dashboard.reader.SNAPSHOTS_KEPT", 1)
    fresh = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    fresh.market(ARM, session(13).isoformat())
    assert list(fresh._snapshots) == [str(newer)]

    # A later snapshot that cannot be read only means there is nothing to join on.
    newer.write_text("{ not a snapshot", encoding="utf-8")
    alone = reader.market(ARM, session(13).isoformat())
    assert alone["available"] and all(item["after"] is None for item in alone["tickers"].values())
    # The run's own snapshot unreadable, or gone, or never written: the page is told, and nothing is invented.
    for spoil in (lambda: older.write_text("[]", encoding="utf-8"), older.unlink):
        spoil()
        gone = reader.market(ARM, session(13).isoformat())
        assert (gone["available"], gone["reason"], gone["tickers"], gone["market"], gone["macro"], gone["captured_at"]) == (
            False, "no_snapshot", {}, [], [], None)
        assert gone["run_id"] == journal.completed_run(ARM, session(13))
    journal.start_run("early", ARM, session(17), stamp(21))
    journal.finish_run("early", "failed", stamp(22), UsageSummary(), error="No data.")
    assert reader.market(ARM, run_id="early")["reason"] == "no_snapshot"
    assert not older.exists()                                     # and the reader did not put a file back


def test_while_a_run_is_going_its_market_data_is_found_where_the_session_saved_it(tmp_path):
    """A run writes down where its snapshot is only when it ends. Until then the page finds it through the
    session's data checkpoint, which the pipeline saves before the first model is called."""
    reader = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    seen = []

    def looking(system, user):                                    # the analyst is asked: the run is in full swing
        with reader._db() as db:
            row = db.execute("SELECT status, snapshot_path FROM runs WHERE arm = ?", (ARM,)).fetchone()
        seen.append((row["status"], row["snapshot_path"], reader.market(ARM)))
        return view(horizon=3)

    llm = ScriptedLLM()
    llm.replies[TickerViewDraft] = looking
    llm.replies[ProposalSetDraft] = proposals()
    pipeline = make_pipeline(tmp_path, llm)
    [result] = pipeline.run()
    assert result.status == "ok" and len(seen) >= 3
    status, written, during = seen[0]
    assert (status, written) == ("running", None)                 # nothing about the snapshot in the run's own row yet
    finished = reader.market(ARM)
    saved = snapshot_file(pipeline.journal, finished["run_id"])
    assert during["available"] and during == finished             # the same snapshot, found both ways
    assert sorted(during["tickers"]) == sorted(load_snapshot(saved).tickers)

    # A run that never ended and whose session has no such checkpoint has nothing to show; nor has one whose
    # checkpoint cannot be read. One that names a snapshot is shown that snapshot.
    journal = pipeline.journal
    captured = load_snapshot(saved).created_at                    # a run's data is collected before the run starts
    journal.start_run("stuck", ARM, session(16), captured + timedelta(minutes=1), pid=os.getpid())
    assert reader.market(ARM, run_id="stuck")["reason"] == "no_snapshot"
    for broken in ("not json", "[]", json.dumps({"rows": []})):
        journal.save_checkpoint("*", session(16), "data", broken)
        assert reader.market(ARM, run_id="stuck")["reason"] == "no_snapshot"
    journal.save_checkpoint("*", session(16), "data", json.dumps({"snapshot_path": str(saved)}))
    stuck = reader.market(ARM, run_id="stuck")
    assert (stuck["available"], stuck["run_id"], stuck["session"]) == (True, "stuck", session(16).isoformat())
    assert stuck["tickers"] == finished["tickers"]
    # A snapshot captured after a run began cannot be that run's: another invocation has saved its own data over
    # the session's checkpoint meanwhile. A run that began at the very moment of the capture, or after it, is fine.
    journal.start_run("before", ARM, session(16), captured - timedelta(seconds=1), pid=os.getpid())
    journal.start_run("at", ARM, session(16), captured, pid=os.getpid())
    assert reader.market(ARM, run_id="before")["reason"] == "no_snapshot"
    assert reader.market(ARM, run_id="at")["available"] is True
    # Once a run has written its own snapshot down, that is its data whenever it was captured.
    journal.finish_run("before", "ok", captured, UsageSummary(), snapshot_path=str(saved))
    assert reader.market(ARM, run_id="before")["available"] is True
    # A configuration's own checkpoint is not the session's shared one, and another day's is not this day's.
    journal.clear_checkpoints("*", session(16))
    journal.save_checkpoint(ARM, session(16), "data", json.dumps({"snapshot_path": str(saved)}))
    journal.save_checkpoint("*", session(17), "data", json.dumps({"snapshot_path": str(saved)}))
    assert reader.market(ARM, run_id="stuck")["reason"] == "no_snapshot"


def test_a_run_that_is_no_longer_going_is_never_shown_data_through_the_sessions_checkpoint(tmp_path):
    """The checkpoint belongs to the session, not to one run. When a run was killed and the session is run
    again, the checkpoint holds the new run's data: the killed run must not be shown it as its own."""
    pipeline = a_week(tmp_path, days=(13,))
    journal = pipeline.journal
    reader = Reader(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    earlier = snapshot_file(journal, journal.completed_run(ARM, session(13)))
    began = load_snapshot(earlier).created_at + timedelta(minutes=1)      # after its data was collected

    # A run of the 16th that had collected its data and was then killed: on record as running, its process gone.
    journal.start_run("killed", ARM, session(16), began, pid=a_process_that_has_ended())
    journal.save_checkpoint("*", session(16), "data", json.dumps({"snapshot_path": str(earlier)}))
    assert reader.market(ARM, run_id="killed")["reason"] == "no_snapshot"        # not going: the checkpoint is not its word
    # While the same run was alive, the checkpoint was the way to its data.
    journal.start_run("alive", ARM, session(16), began, pid=os.getpid())
    assert reader.market(ARM, run_id="alive")["available"] is True
    journal.finish_run("alive", "failed", stamp(22), UsageSummary(), error="Stopped.")
    assert reader.market(ARM, run_id="alive")["reason"] == "no_snapshot"         # ended without writing its snapshot down

    # The session is run again. That closes the killed run and saves the new run's data under the same checkpoint.
    pipeline.arms[0].llm.replies[ProposalSetDraft] = proposals()
    pipeline._now.now = day(16)
    [again] = pipeline.run()
    assert again.status == "ok", again.message
    closed = journal.rows("SELECT status, snapshot_path FROM runs WHERE run_id = 'killed'")[0]
    assert (closed["status"], closed["snapshot_path"]) == ("failed", None)
    fresh = reader.market(ARM, run_id=again.run_id)
    assert fresh["available"] and fresh["tickers"]["UPA"]["bars"]["dates"][-1] == session(16).isoformat()
    gone = reader.market(ARM, run_id="killed")
    assert (gone["available"], gone["reason"], gone["tickers"], gone["market"]) == (False, "no_snapshot", {}, [])


# ── More than one model configuration ────────────────────────────────────────

def test_each_model_configuration_is_shown_its_own_records_and_nothing_of_the_others(tmp_path):
    scripts = {}
    for name, ticker in (("fake/a", "UPA"), ("fake/b", "UPB")):
        scripts[name] = ScriptedLLM()
        scripts[name].replies[TickerViewDraft] = view(horizon=3, thesis=f"Written by {name}.")
        scripts[name].replies[ProposalSetDraft] = proposals((ticker, "buy", 0.7))
    pipeline = make_pipeline(tmp_path, arms=[Arm(name, llm) for name, llm in scripts.items()])
    for number in (13, 16, 17):
        pipeline._now.now = day(number)
        assert [result.status for result in pipeline.run()] == ["ok", "ok"]
        for llm in scripts.values():
            llm.replies[ProposalSetDraft] = proposals()
    journal = pipeline.journal
    report = evaluate(journal, pipeline.builder.market, now=day(17), simulations=20, seed=1)
    save(report, tmp_path / "evaluation")
    client = browser(tmp_path / "journal.sqlite3", tmp_path / "evaluation")
    assert get(client, "/api/arms")[1]["arms"] == ["fake/a", "fake/b"]

    for arm, ticker, other in (("fake/a", "UPA", "fake/b"), ("fake/b", "UPB", "fake/a")):
        own = {row["run_id"] for row in journal.runs(arm)}
        assert len(own) == 3 and not own & {row["run_id"] for row in journal.runs(other)}
        words = {f"Written by {arm}."}

        days = get(client, "/api/sessions", arm=arm)[1]["sessions"]
        assert {item["run_id"] for item in days} == own
        for item in days:
            meeting = get(client, "/api/meeting", arm=arm, session=item["session"])[1]
            assert meeting["run"]["run_id"] == item["run_id"]
            assert shown(meeting["events"]) == recorded(journal, item["run_id"])
            assert {event["payload"]["thesis"] for event in meeting["events"] if event["kind"] == "view"} == words

        messages = stream(client, arm=arm, polls=2)
        assert messages[0]["run"]["run_id"] == journal.runs(arm)[0]["run_id"]
        assert shown([message["event"] for message in messages[1:]]) == recorded(journal, messages[0]["run"]["run_id"])

        broker = PaperBroker(journal, arm)
        overview = get(client, "/api/overview", arm=arm)[1]
        assert overview["account"]["equity"] == broker.account().equity and overview["latest_run"]["run_id"] in own
        assert [(point["d"], point["v"]) for point in overview["curves"][0]["points"]] == [
            (when.isoformat(), equity) for when, equity in broker.equity_history()]
        book = get(client, "/api/book", arm=arm)[1]
        assert [row["ticker"] for row in book["positions"]] == [ticker] == sorted(broker.positions())
        assert {fill["ticker"] for fill in book["fills"]} == {ticker} == {order["ticker"] for order in book["orders"]}
        assert {item["thesis"] for item in book["signals"]["views"]} == words

        status = get(client, "/api/status", arm=arm)[1]
        assert {run["run_id"] for run in status["runs"]} == own and status["totals"]["runs"] == 3

        mine = next(item for item in report.arms if item.arm == arm)
        evaluation = get(client, "/api/evaluation", arm=arm)[1]
        assert evaluation["report"] == mine.model_dump(mode="json") and evaluation["report"]["arm"] == arm
        assert [trade["ticker"] for trade in evaluation["report"]["trades"]["trades"]] == [ticker]
        assert [row["arm"] for row in evaluation["arms"]] == ["fake/a", "fake/b"]          # side by side, both listed
        for curve, source in zip(overview["curves"][1:], mine.curves[1:]):
            assert [point["v"] for point in curve["points"]] == [point.equity for point in source.points]

    # The two did different things, so showing one for the other would have been noticed above.
    assert PaperBroker(journal, "fake/a").account().equity != PaperBroker(journal, "fake/b").account().equity


# ── Who may ask ──────────────────────────────────────────────────────────────

def test_only_a_browser_that_names_this_computer_is_answered(week):
    _, _, folder = week
    paths = folder / "journal.sqlite3", folder / "evaluation"
    def asked_as(app, name, path="/api/overview"):
        """What a browser gets that addresses the server by this name: the Host header it would send."""
        return TestClient(app, base_url="http://127.0.0.1").get(path, headers={"Host": name})

    local = create_app(*paths, poll_seconds=0, host="127.0.0.1")
    for name in ("127.0.0.1", "127.0.0.1:8765", "localhost:8765", "LOCALHOST:8765", "[::1]:8765", "[::1]"):
        assert asked_as(local, name).status_code == 200 and asked_as(local, name, "/").status_code == 200, name
    # A page on some other site that has pointed its own name at this computer gets nothing at all.
    for path in ("/", "/js/app.js", "/api/roster", "/api/overview", "/api/book", "/api/status", "/api/stream?polls=1"):
        refused = asked_as(local, "evil.example:8765", path)
        assert refused.status_code == 400 and "UPA" not in refused.text, path
    for name in ("127.0.0.1.evil.example", "localhost.evil.example:8765", "192.168.1.20:8765", "[::2]:8765",
                 "", "[::1", "evil.example:127.0.0.1", "localhost:8765@evil.example"):
        assert asked_as(local, name).status_code == 400, name
    assert [host_named(header) for header in ("LocalHost:8765", "127.0.0.1", "[::1]:8765", "[FE80::1]", "[::1", "")] == [
        "localhost", "127.0.0.1", "::1", "fe80::1", "", ""]

    assert names_answered("127.0.0.1") == {"127.0.0.1", "localhost", "::1"}
    assert names_answered("192.168.1.20") == {"127.0.0.1", "localhost", "::1", "192.168.1.20"}
    on_the_network = create_app(*paths, poll_seconds=0, host="192.168.1.20")
    assert asked_as(on_the_network, "192.168.1.20:8765").status_code == 200
    assert asked_as(on_the_network, "evil.example").status_code == 400
    by_its_other_address = create_app(*paths, poll_seconds=0, host="FE80::1")
    assert asked_as(by_its_other_address, "[fe80::1]:8765").status_code == 200
    # Listening on every address is the owner opening the page up: then any name is answered.
    assert names_answered("0.0.0.0") is None and names_answered("::") is None
    opened = create_app(*paths, poll_seconds=0, host="0.0.0.0")
    assert asked_as(opened, "my-laptop.local:8765").status_code == 200


# ── The evaluation ───────────────────────────────────────────────────────────

def test_the_evaluation_page_shows_the_last_report_as_written_and_never_runs_one(week, client, monkeypatch):
    _, report, folder = week

    def never(*args, **kwargs):
        raise AssertionError("the dashboard ran the evaluation")

    monkeypatch.setattr(evaluation_run, "evaluate", never)
    status, body = get(client, "/api/evaluation", arm=ARM)
    [mine] = report.arms

    assert status == 200 and (body["as_of"], body["benchmark"]) == (session(20).isoformat(), "SPY")
    assert body["file"] == str(folder / "evaluation" / session(20).isoformat() / "evaluation.json")
    assert body["report"] == mine.model_dump(mode="json") and mine.scorecard.scored == 9
    assert body["arms"] == [{
        "arm": ARM, "sessions": 6, "scored": 9, "hit_rate": mine.scorecard.overall.hit_rate,
        "beat_rate": mine.scorecard.overall.beat_rate, "return_pct": mine.curves[0].return_pct,
        "closed": mine.trades.closed, "percentile": mine.random.percentile, "buys": mine.random.entries,
        "cost_usd": mine.usage.cost_usd, "calls": mine.usage.calls,
    }]
    assert body["lessons"] == [] and body["lessons_shown"] is config.PIPELINE_USE_LESSONS is False

    # The two benchmark lines on the first page are the report's own.
    overview = get(client, "/api/overview", arm=ARM)[1]
    assert [curve["key"] for curve in overview["curves"]] == ["account", "benchmark", "candidates"]
    assert overview["curves"][1]["name"] == "SPY 买入持有" and overview["evaluated_as_of"] == session(20).isoformat()
    for curve, source in zip(overview["curves"][1:], mine.curves[1:]):
        assert [(p["d"], p["v"]) for p in curve["points"]] == [(p.session_date.isoformat(), p.equity) for p in source.points]


def test_without_a_report_the_page_has_the_account_alone_and_still_lists_the_lessons(tmp_path):
    pipeline = a_week(tmp_path, days=(13, 16))
    pipeline.journal.save_lesson(a_lesson("UPA", known=nov(10), text="Wait for the trend."))
    pipeline.journal.save_lesson(a_lesson("UPB", known=nov(12), warnings=("It cites a figure nobody showed it.",), text=ODD))
    pipeline.journal.save_lesson(a_lesson("UPC", known=nov(12), text="Mind the sector."))
    client = browser(tmp_path / "journal.sqlite3", tmp_path / "nothing-here")

    body = get(client, "/api/evaluation", arm=ARM)[1]
    assert body["report"] is None and body["arms"] == [] and "as_of" not in body
    # The most recently learned first; the one that failed its check is listed and marked, in the model's own words.
    assert [(lesson["ticker"], lesson["lesson"], lesson["clean"], lesson["known_on"]) for lesson in body["lessons"]] == [
        ("UPB", ODD, False, nov(12).isoformat()), ("UPC", "Mind the sector.", True, nov(12).isoformat()),
        ("UPA", "Wait for the trend.", True, nov(10).isoformat())]
    assert body["lessons"][0]["warnings"] == ["It cites a figure nobody showed it."]
    overview = get(client, "/api/overview", arm=ARM)[1]
    assert [curve["key"] for curve in overview["curves"]] == ["account"] and overview["evaluated_as_of"] is None
    assert not (tmp_path / "nothing-here").exists()


def test_the_newest_report_that_can_be_read_is_the_one_shown(week, tmp_path):
    _, report, folder = week
    reports = tmp_path / "evaluation"
    save(report.model_copy(update={"as_of": session(18)}), reports)
    save(report, reports)
    reader = Reader(folder / "journal.sqlite3", reports)
    assert reader.evaluation(ARM)["as_of"] == session(20).isoformat()

    later = reports / "2026-12-31"                        # newer than both, and not a report this version can read
    later.mkdir()
    (later / "evaluation.json").write_text('{"written": "by another version"}', encoding="utf-8")
    (reports / "notes.txt").write_text("not a report", encoding="utf-8")
    shown_report = reader.evaluation(ARM)
    assert shown_report["as_of"] == session(20).isoformat()
    assert Evaluation.model_validate_json(Path(shown_report["file"]).read_text(encoding="utf-8")) == report

    # A report that leaves this configuration out: the page says so instead of showing another's numbers.
    assert shown_report["report"] is not None

    # The same report written again with different contents is read again, not remembered.
    save(report.model_copy(update={"benchmark": "QQQ"}), reports)
    assert (reader.evaluation(ARM)["as_of"], reader.evaluation(ARM)["benchmark"]) == (session(20).isoformat(), "QQQ")
    assert reader.overview(ARM)["curves"][1]["name"] == "QQQ 买入持有"
    save(report, reports)
    assert reader.evaluation(ARM)["benchmark"] == "SPY"

    # A report about another configuration only: the page says so instead of showing that one's numbers as this one's.
    [mine] = report.arms
    save(report.model_copy(update={"as_of": session(21), "arms": [mine.model_copy(update={"arm": "other/b"})]}), reports)
    newest = reader.evaluation(ARM)
    assert (newest["as_of"], newest["report"]) == (session(21).isoformat(), None)
    assert [row["arm"] for row in newest["arms"]] == ["other/b"]
    overview = reader.overview(ARM)
    assert [curve["key"] for curve in overview["curves"]] == ["account"] and overview["evaluated_as_of"] is None
    assert reader.meeting(ARM)["records"] == {"as_of": None, "lines": {}}


# ── How the runs went ────────────────────────────────────────────────────────

def test_the_status_page_lists_every_run_newest_first_with_what_it_cost(week, client):
    pipeline, _, folder = week
    rows = pipeline.journal.runs(ARM)
    status, body = get(client, "/api/status", arm=ARM)

    assert status == 200 and [run["run_id"] for run in body["runs"]] == [row["run_id"] for row in rows]
    assert [run["session"] for run in body["runs"]] == [session(n).isoformat() for n in (20, 19, 18, 17, 16, 13)]
    for run, row in zip(body["runs"], rows):
        assert (run["status"], run["llm_calls"], run["cost_usd"]) == ("ok", row["llm_calls"], row["cost_usd"])
        assert run["cost_usd"] == pytest.approx(0.01 * row["llm_calls"]) and run["llm_calls"] > 0
        assert run["snapshot"] == {"path": row["snapshot_path"], "exists": True} and run["seconds"] is not None
    totals = body["totals"]
    assert (totals["runs"], totals["failed"], totals["degraded"]) == (6, 0, 0)
    assert totals["calls"] == sum(row["llm_calls"] for row in rows)
    assert totals["cost"] == pytest.approx(sum(row["cost_usd"] for row in rows))
    assert body["journal"] == {"path": str(folder / "journal.sqlite3"), "bytes": (folder / "journal.sqlite3").stat().st_size}
