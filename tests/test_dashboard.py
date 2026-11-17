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
from evaluation import run as evaluation_run
from evaluation.run import evaluate, save
from execution.paper import PaperBroker
from journal.store import Journal
from models.evaluation import Evaluation
from models.signals import Event, ProposalSetDraft, TickerViewDraft, UsageSummary
from pipeline.daily import Arm
from risk.gate import RiskLimits, halted
from tests.pipeline_fakes import ScriptedLLM, by_ticker, day, make_pipeline, proposals, session, view
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
    "/api/roster", "/api/arms", "/api/sessions", "/api/meeting", "/api/overview", "/api/book",
    "/api/evaluation", "/api/status", "/api/stream",
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
        "app.js", "charts.js", "dom.js", "figures.js", "live.js", "meeting.js", "pages.js", "playback.js"}
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
    """Playback, keeping a room in step with a live run, text kept as text, number formats, seats: tests/js, run by node."""
    files = sorted(path.name for path in (ROOT / "tests" / "js").glob("*.test.mjs"))
    assert files == ["dom.test.mjs", "format.test.mjs", "live.test.mjs", "playback.test.mjs"]
    result = subprocess.run([NODE, "--test", *(f"tests/js/{name}" for name in files)],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert int(re.search(r"pass (\d+)", result.stdout).group(1)) >= 37 and re.search(r"fail 0\b", result.stdout)


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
        "settle", "macro", "views", "debate", "proposals", "checks", "risk", "orders"]
    assert all(figure["title"] and figure["does"] for figure in roster)


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
    # The next session reuses the macro view, so the analysts say nothing and the narrator says where it came from.
    later = get(client, "/api/meeting", arm=ARM, session=session(16).isoformat())[1]["today"]
    assert "central_bank" not in later and later["pipeline"] == f"沿用 {session(13).isoformat()} 的宏观结论"
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
