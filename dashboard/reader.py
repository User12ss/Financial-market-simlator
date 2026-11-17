"""Everything the dashboard shows, read from the journal and from the last evaluation report.

Read-only in the strict sense: the journal is opened in SQLite's read-only
mode, so nothing here can change what it holds or create a table in it, and
a journal that is missing is not created. (SQLite itself may put its own
empty `-wal` and `-shm` files beside a journal when it is opened for
reading; they hold nothing of ours.) The evaluation is not run from here
either: its last report is read from disk, and the page says how old it is.

Every function returns plain data ready to be sent as JSON. What a model
said is passed on exactly as it was recorded.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import config
from dashboard.personas import STAGES, title_of
from journal.store import COMPLETED
from models.evaluation import Evaluation
from models.signals import SignalTable
from risk.gate import halted

STAGE_TITLES = dict(STAGES)
FILLS_SHOWN = 100
RUNS_SHOWN = 60
# Not a status the journal holds. It is what the page calls a run that is on record as
# running although the process that was running it no longer exists.
INTERRUPTED = "interrupted"


class NoJournal(Exception):
    """There is no journal yet: the pipeline has not run."""


def _process_exists(pid: int | None) -> bool:
    """Whether a process with this id exists on this computer right now. Asking changes nothing."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)                     # signal 0 is delivered to no one: it only says whether the process is there
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                         # it is there, and belongs to someone else
    return True


def _status(row: sqlite3.Row) -> str:
    """The status to show for a run.

    A run writes "running" when it starts and its real ending when it ends.
    One that was killed never writes the ending, so the journal goes on
    saying "running" until the pipeline next takes that session up. The
    page must not show such a run as going. (If the system has since given
    the same process id to something else, it will still look alive; the
    pipeline's own check has the same blind spot.)
    """
    if row["status"] == "running" and not _process_exists(row["pid"]):
        return INTERRUPTED
    return row["status"]


class Reader:
    def __init__(self, journal_path: str | Path | None = None, evaluation_dir: str | Path | None = None) -> None:
        self.journal_path = Path(journal_path or config.JOURNAL_PATH)
        self.evaluation_dir = Path(evaluation_dir or config.EVALUATION_DIR)
        self._report: tuple[Path, bytes, Evaluation] | None = None      # the last report read, to read it only once

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        if not self.journal_path.is_file():
            raise NoJournal(str(self.journal_path))
        db = sqlite3.connect(f"{self.journal_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    # ── Runs and sessions ────────────────────────────────────────────────

    def arms(self) -> list[str]:
        with self._db() as db:
            return [row["arm"] for row in db.execute("SELECT DISTINCT arm FROM runs ORDER BY arm")]

    @staticmethod
    def _shown_run(db: sqlite3.Connection, arm: str, session: str) -> sqlite3.Row | None:
        """The run to show for a session: one still going, else the one that stands, else the last attempt."""
        rows = db.execute(
            "SELECT * FROM runs WHERE arm = ? AND session_date = ? ORDER BY started_at DESC, rowid DESC", (arm, session)
        ).fetchall()
        for wanted in (("running",), COMPLETED):
            for row in rows:
                if _status(row) in wanted:
                    return row
        return rows[0] if rows else None

    def sessions(self, arm: str) -> list[dict]:
        """Every session an arm has a run for, newest first, with the run that is shown for it."""
        with self._db() as db:
            days = [row["session_date"] for row in db.execute(
                "SELECT DISTINCT session_date FROM runs WHERE arm = ? ORDER BY session_date DESC", (arm,))]
            found = []
            for day in days:
                run = self._shown_run(db, arm, day)
                count = db.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (run["run_id"],)).fetchone()[0]
                found.append({"session": day, **_run(run), "events": count})
            return found

    # ── The meeting ──────────────────────────────────────────────────────

    def meeting(self, arm: str, session: str | None = None, run_id: str | None = None) -> dict:
        """One run and everything said in it, in order.

        Named by `run_id`, it is that run, whichever attempt at its session
        it was. Otherwise it is the run shown for `session`, or for the
        latest session when none is named. A run or session this arm does
        not have comes back empty, with no run. `stands` says whether the
        run is the one shown for its session, or another attempt at it.
        """
        with self._db() as db:
            if run_id is not None:
                run = db.execute("SELECT * FROM runs WHERE run_id = ? AND arm = ?", (run_id, arm)).fetchone()
            else:
                if session is None:
                    session = db.execute("SELECT MAX(session_date) FROM runs WHERE arm = ?", (arm,)).fetchone()[0]
                run = self._shown_run(db, arm, session)
            events, stands = [], None
            if run is not None:
                events = [_event(row) for row in db.execute(
                    "SELECT event_json FROM events WHERE run_id = ? ORDER BY seq", (run["run_id"],))]
                stands = self._shown_run(db, arm, run["session_date"])["run_id"] == run["run_id"]
        return {
            "arm": arm, "session": None if run is None else run["session_date"],
            "run": None if run is None else _run(run), "stands": stands,
            "events": events, "today": today(events), "records": self.records(arm),
        }

    def records(self, arm: str) -> dict:
        """How the past work of a figure has turned out, for the figures the evaluation measures.

        Counted by the last evaluation report: the analyst's views and the
        trades the portfolio manager proposed. The other figures have no
        line, because nothing measures them one by one.
        """
        evaluation, _ = self._evaluation()
        mine = next((item for item in evaluation.arms if item.arm == arm), None) if evaluation else None
        if mine is None:
            return {"as_of": None, "lines": {}}
        lines = {}
        card, trades = mine.scorecard, mine.trades
        if card.overall is not None:
            lines["ticker_analyst"] = (
                f"{card.overall.views} 条有方向的观点已评分：方向对了 {card.overall.right} 条，"
                f"跑赢基准 {card.overall.beat_benchmark} 条；另有 {card.pending} 条还没到期"
            )
        elif card.total:
            lines["ticker_analyst"] = f"写过 {card.total} 条观点，还没有一条评分（{card.pending} 条没到期）"
        if trades.closed or trades.open:
            lines["portfolio_manager"] = (
                f"提出的交易里已平仓 {trades.closed} 笔（赢 {trades.winners}，亏 {trades.losers}，"
                f"其中止损出场 {trades.stopped_out}），还开着 {trades.open} 笔"
            )
        return {"as_of": evaluation.as_of.isoformat(), "lines": lines}

    def tail(self, arm: str, run_id: str | None, seq: int, status: str | None) -> tuple[list[dict], str | None, int, str | None]:
        """What has happened since the page last looked: for the live stream.

        The page follows the arm's newest run. Returns the messages to send
        and the new position: a "run" message when that run is a different
        one or its status has changed, then an "event" message for each
        event the page has not seen. When a newer run has taken the place of
        the one the page was following, the old one's last lines and its
        ending are sent first, so nothing said between two looks is lost.
        """
        def about(run: sqlite3.Row, new: bool) -> dict:
            return {"type": "run", "run": _run(run), "session": run["session_date"], "new": new}

        def unseen(of_run: str, after: int) -> list[dict]:
            return [{"type": "event", "event": _event(row)} for row in db.execute(
                "SELECT event_json FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (of_run, after))]

        with self._db() as db:
            newest = db.execute(
                "SELECT * FROM runs WHERE arm = ? ORDER BY started_at DESC, rowid DESC LIMIT 1", (arm,)
            ).fetchone()
            if newest is None:
                return [], run_id, seq, status
            messages = []
            if newest["run_id"] != run_id:
                left = db.execute("SELECT * FROM runs WHERE run_id = ? AND arm = ?", (run_id, arm)).fetchone()
                if left is not None:
                    messages += unseen(run_id, seq)
                    if _status(left) != status:
                        messages.append(about(left, new=False))
                run_id, seq = newest["run_id"], 0
                messages.append(about(newest, new=True))
            elif _status(newest) != status:
                messages.append(about(newest, new=False))
            status = _status(newest)
            fresh = unseen(run_id, seq)
            if fresh:
                seq = fresh[-1]["event"]["seq"]
            return messages + fresh, run_id, seq, status

    # ── The account ──────────────────────────────────────────────────────

    def overview(self, arm: str) -> dict:
        with self._db() as db:
            account = db.execute("SELECT * FROM paper_accounts WHERE account = ?", (arm,)).fetchone()
            marks = db.execute(
                "SELECT session_date, equity FROM paper_equity WHERE account = ? ORDER BY session_date", (arm,)
            ).fetchall()
            positions = self._positions(db, arm)
            open_orders = db.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE account = ? AND status = 'open' AND kind != 'stop'", (arm,)
            ).fetchone()[0]
            latest = db.execute(
                "SELECT * FROM runs WHERE arm = ? ORDER BY started_at DESC, rowid DESC LIMIT 1", (arm,)
            ).fetchone()
            finished = db.execute(
                "SELECT session_date, signals_json FROM runs WHERE arm = ? AND status IN ('ok', 'degraded') "
                "AND signals_json IS NOT NULL ORDER BY session_date DESC, started_at DESC LIMIT 1", (arm,)
            ).fetchone()

        figures = None
        if account is not None:
            value = sum(position["value"] for position in positions)
            equity = account["cash"] + value
            high = max(account["high_water"], equity)
            change = None
            if len(marks) >= 2 and marks[-2]["equity"]:
                change = (marks[-1]["equity"] / marks[-2]["equity"] - 1) * 100
            figures = {
                "as_of": account["last_session"], "cash": round(account["cash"], 2),
                "positions_value": round(value, 2), "equity": round(equity, 2),
                "initial_cash": account["initial_cash"], "high_water": round(high, 2),
                "day_change_pct": None if change is None else round(change, 2),
                "drawdown_pct": round((1 - equity / high) * 100, 2) if high > 0 else 0.0,
                "return_pct": round((equity / account["initial_cash"] - 1) * 100, 2) if account["initial_cash"] else None,
            }

        curves = [{
            "name": "模拟账户", "key": "account",
            "points": [{"d": row["session_date"], "v": row["equity"]} for row in marks],
        }]
        evaluation, _ = self._evaluation()
        measured = next((item for item in evaluation.arms if item.arm == arm), None) if evaluation else None
        if measured is not None:
            for key, curve in zip(("benchmark", "candidates"), measured.curves[1:]):
                curves.append({
                    "name": f"{evaluation.benchmark} 买入持有" if key == "benchmark" else "候选池等权", "key": key,
                    "points": [{"d": point.session_date.isoformat(), "v": point.equity} for point in curve.points],
                })

        return {
            "arm": arm, "account": figures, "curves": curves,
            "evaluated_as_of": evaluation.as_of.isoformat() if measured is not None else None,
            "guards": self._guards(figures, None if finished is None else {
                "session": finished["session_date"], "halted": json.loads(finished["signals_json"]).get("halted", ""),
            }),
            "positions": len(positions), "open_orders": open_orders,
            "latest_run": None if latest is None else {"session": latest["session_date"], **_run(latest)},
        }

    @staticmethod
    def _guards(figures: dict | None, last_run: dict | None = None) -> dict:
        """The switch and the two circuit breakers, with the numbers they are judged on.

        The switch is put to the risk gate's own function, so the rule is the
        gate's. The answer is what this process sees at this moment: the
        switch file is shared with the pipeline, but TRADING_HALT belongs to
        each process, so one set for the pipeline alone does not show here.
        That is why `last_run` is given beside it: what the last finished run
        itself recorded about being halted. The breakers use the gate's own
        comparisons: at the limit counts as tripped.
        """
        reason = halted()
        day, fall = (figures or {}).get("day_change_pct"), (figures or {}).get("drawdown_pct")
        return {
            "kill_switch": {
                "on": bool(reason), "reason": reason, "file": str(config.KILL_SWITCH_FILE), "last_run": last_run,
            },
            "daily_loss": {
                "limit_pct": config.RISK_DAILY_LOSS_LIMIT_PCT, "value_pct": day,
                "tripped": day is not None and day <= -config.RISK_DAILY_LOSS_LIMIT_PCT,
            },
            "drawdown": {
                "limit_pct": config.RISK_MAX_DRAWDOWN_PCT, "value_pct": fall,
                "tripped": fall is not None and fall >= config.RISK_MAX_DRAWDOWN_PCT,
            },
        }

    @staticmethod
    def _positions(db: sqlite3.Connection, arm: str) -> list[dict]:
        stops = {row["ticker"]: row["stop_price"] for row in db.execute(
            "SELECT ticker, stop_price FROM paper_orders WHERE account = ? AND status = 'open' AND kind = 'stop'", (arm,))}
        found = []
        for row in db.execute("SELECT * FROM paper_positions WHERE account = ? ORDER BY ticker", (arm,)):
            # A position without a usable price today is carried at the last close it could be matched to.
            price = row["last_close"] if row["last_close"] is not None else row["anchor_close"]
            found.append({
                "ticker": row["ticker"], "shares": row["shares"], "avg_cost": round(row["avg_cost"], 4),
                "entry_date": row["entry_date"], "sector": row["sector"], "stop_price": stops.get(row["ticker"]),
                "last_close": row["last_close"], "value": round(row["shares"] * price, 2),
                "unrealized_pct": (
                    None if row["last_close"] is None else round((row["last_close"] / row["avg_cost"] - 1) * 100, 2)
                ),
                "unrealized_usd": (
                    None if row["last_close"] is None else round(row["shares"] * (row["last_close"] - row["avg_cost"]), 2)
                ),
                "sessions_held": row["sessions_held"],
            })
        return found

    def book(self, arm: str) -> dict:
        """Positions, orders, fills, and the latest finished session's candidates and signals."""
        with self._db() as db:
            positions = self._positions(db, arm)
            orders = [dict(row) for row in db.execute(
                "SELECT order_id, ticker, side, kind, shares, limit_price, stop_price, protective_stop, status, "
                "created_session, reason FROM paper_orders WHERE account = ? AND status = 'open' "
                "ORDER BY created_session DESC, ticker", (arm,))]
            fills = [dict(row) for row in db.execute(
                "SELECT ticker, side, kind, shares, price, fill_date, pnl_pct, note FROM paper_fills "
                "WHERE account = ? ORDER BY fill_date DESC, id DESC LIMIT ?", (arm, FILLS_SHOWN))]
            standing = db.execute(
                "SELECT signals_json FROM runs WHERE arm = ? AND status IN ('ok', 'degraded') AND signals_json IS NOT NULL "
                "ORDER BY session_date DESC, started_at DESC LIMIT 1", (arm,)
            ).fetchone()
        signals = None
        if standing is not None:
            table = SignalTable.model_validate_json(standing["signals_json"])
            signals = {
                "session": table.session_date.isoformat(), "status": table.status, "universe_size": table.universe_size,
                "candidates": table.candidates, "halted": table.halted, "warnings": table.warnings,
                "regime": None if table.regime is None else {
                    "risk_appetite": table.regime.risk_appetite, "confidence": table.regime.confidence,
                    "formed_on": table.regime.session_date.isoformat(),
                },
                "views": [
                    {"ticker": view.ticker, "held": view.held, "direction": view.direction, "confidence": view.confidence,
                     "horizon_days": view.horizon_days, "proposed_stop": view.proposed_stop, "thesis": view.thesis,
                     "warnings": view.data_warnings}
                    for view in table.views
                ],
                "proposals": [proposal.model_dump() for proposal in table.proposals],
                "risk": [decision.model_dump() for decision in table.risk],
                "orders": [order.model_dump() for order in table.orders],
            }
        return {"arm": arm, "positions": positions, "orders": orders, "fills": fills, "signals": signals}

    # ── The evaluation ───────────────────────────────────────────────────

    def _evaluation(self) -> tuple[Evaluation | None, Path | None]:
        """The newest evaluation report on disk. The dashboard never runs the evaluation itself."""
        if not self.evaluation_dir.is_dir():
            return None, None
        for folder in sorted((item for item in self.evaluation_dir.iterdir() if item.is_dir()), reverse=True):
            report = folder / "evaluation.json"
            if not report.is_file():
                continue
            written = report.read_bytes()
            if self._report is not None and self._report[:2] == (report, written):
                return self._report[2], report          # the same file with the same contents as last time
            try:
                evaluation = Evaluation.model_validate_json(written)
            except ValueError:
                continue                                # written by another version: look for an older one
            self._report = (report, written, evaluation)
            return evaluation, report
        return None, None

    def evaluation(self, arm: str) -> dict:
        evaluation, path = self._evaluation()
        lessons = []
        with self._db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lessons'").fetchone():
                for row in db.execute(
                    "SELECT clean, lesson_json FROM lessons WHERE arm = ? ORDER BY known_on DESC, ticker", (arm,)
                ):
                    lesson = json.loads(row["lesson_json"])
                    lessons.append({
                        "ticker": lesson["ticker"], "session": lesson["session_date"], "known_on": lesson["known_on"],
                        "outcome": lesson["outcome"], "assessment": lesson["assessment"], "lesson": lesson["lesson"],
                        "clean": bool(row["clean"]), "warnings": lesson.get("data_warnings", []),
                    })
        if evaluation is None:
            return {"arm": arm, "report": None, "arms": [], "lessons": lessons,
                    "lessons_shown": config.PIPELINE_USE_LESSONS}
        mine = next((item for item in evaluation.arms if item.arm == arm), None)
        return {
            "arm": arm, "as_of": evaluation.as_of.isoformat(), "created_at": evaluation.created_at.isoformat(),
            "benchmark": evaluation.benchmark, "file": str(path),
            "report": None if mine is None else mine.model_dump(mode="json"),
            # One line per model configuration, to set them side by side.
            "arms": [
                {
                    "arm": item.arm, "sessions": item.sessions_run, "scored": item.scorecard.scored,
                    "hit_rate": item.scorecard.overall.hit_rate if item.scorecard.overall else None,
                    "beat_rate": item.scorecard.overall.beat_rate if item.scorecard.overall else None,
                    "return_pct": item.curves[0].return_pct if item.curves else None,
                    "closed": item.trades.closed, "percentile": item.random.percentile, "buys": item.random.entries,
                    "cost_usd": item.usage.cost_usd, "calls": item.usage.calls,
                }
                for item in evaluation.arms
            ],
            "lessons": lessons, "lessons_shown": config.PIPELINE_USE_LESSONS,
        }

    # ── How the runs went ────────────────────────────────────────────────

    def status(self, arm: str) -> dict:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE arm = ? ORDER BY started_at DESC, rowid DESC LIMIT ?", (arm, RUNS_SHOWN)
            ).fetchall()
            totals = db.execute(
                "SELECT COUNT(*) AS runs, SUM(llm_calls) AS calls, SUM(input_tokens) AS tokens_in, "
                "SUM(output_tokens) AS tokens_out, SUM(model_seconds) AS seconds, SUM(cost_usd) AS cost, "
                "SUM(status = 'failed') AS failed, SUM(status = 'degraded') AS degraded FROM runs WHERE arm = ?", (arm,)
            ).fetchone()
        runs = []
        for row in rows:
            warnings = []
            if row["signals_json"]:
                warnings = json.loads(row["signals_json"]).get("warnings", [])
            snapshot = row["snapshot_path"]
            runs.append({
                "session": row["session_date"], **_run(row), "llm_calls": row["llm_calls"],
                "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
                "model_seconds": row["model_seconds"], "cost_usd": row["cost_usd"], "warnings": warnings,
                "snapshot": None if not snapshot else {"path": snapshot, "exists": Path(snapshot).is_file()},
            })
        return {
            "arm": arm, "runs": runs,
            "totals": {key: totals[key] for key in totals.keys()},
            "journal": {"path": str(self.journal_path), "bytes": self.journal_path.stat().st_size},
        }


# ── Shapes ───────────────────────────────────────────────────────────────────

def _run(row: sqlite3.Row) -> dict:
    seconds = None
    if row["finished_at"]:
        seconds = (datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()
    status = _status(row)
    return {
        "run_id": row["run_id"], "status": status, "started_at": row["started_at"],
        "finished_at": row["finished_at"], "seconds": seconds, "error": row["error"],
        "live": status == "running",
    }


def _event(row: sqlite3.Row) -> dict:
    """One recorded event, with the Chinese titles the page shows beside the words as they were recorded."""
    event = json.loads(row["event_json"])
    return {
        "seq": event["seq"], "ts": event["ts"], "stage": event["stage"],
        "stage_title": STAGE_TITLES.get(event["stage"], event["stage"]),
        "speaker": event["speaker"], "speaker_title": title_of(event["speaker"]), "is_llm": event["is_llm"],
        "to": event["addressed_to"], "to_title": title_of(event["addressed_to"]),
        "kind": event["kind"], "ticker": event.get("ticker"), "remark": event["remark"],
        "payload": event.get("payload") or {},
    }


def today(events: list[dict]) -> dict[str, str]:
    """One line per figure on what it has done in this run so far, counted from the events themselves."""
    def of(speaker: str, *kinds: str) -> list[dict]:
        return [event for event in events if event["speaker"] == speaker and (not kinds or event["kind"] in kinds)]

    lines: dict[str, str] = {}
    reused = next((event for event in events if event["kind"] == "macro_view_reused"), None)
    for event in events:
        if event["kind"] == "assessment":
            payload = event["payload"]
            stance = {"supportive": "偏多", "neutral": "中性", "restrictive": "偏空"}.get(payload.get("stance"), payload.get("stance"))
            lines[event["speaker"]] = f"立场：{stance}，把握度 {payload.get('confidence', 0):.2f}"
    if reused is not None:
        lines["pipeline"] = f"沿用 {reused['payload'].get('session_date')} 的宏观结论"
    views = of("ticker_analyst", "view")
    if views:
        count = {name: sum(1 for event in views if event["payload"].get("direction") == name)
                 for name in ("bullish", "neutral", "bearish")}
        lines["ticker_analyst"] = f"{len(views)} 条观点：看多 {count['bullish']}，中性 {count['neutral']}，看空 {count['bearish']}"
    for side in ("bull", "bear"):
        said = of(side, "argument")
        if said:
            lines[side] = f"在 {len({event['ticker'] for event in said})} 场辩论中发言 {len(said)} 次"
    rulings = of("judge", "ruling")
    if rulings:
        own = sum(1 for event in rulings if event["payload"].get("stance") == "own")
        lines["judge"] = f"{len(rulings)} 条裁决：持有 {own}，不持有 {len(rulings) - own}"
    for event in of("portfolio_manager", "proposals"):
        items = event["payload"].get("proposals", [])
        count = {name: sum(1 for item in items if item.get("action") == name) for name in ("buy", "sell", "trim", "hold")}
        lines["portfolio_manager"] = (
            f"{len(items)} 条提案：买入 {count['buy']}，卖出 {count['sell']}，减仓 {count['trim']}，持有 {count['hold']}"
        )
    checked = of("risk_officer")
    if checked:
        kinds = {kind: sum(1 for event in checked if event["kind"] == kind)
                 for kind in ("accepted", "rejected", "risk_approved", "risk_reduced", "risk_rejected")}
        lines["risk_officer"] = (
            f"提案：接受 {kinds['accepted']}，拒绝 {kinds['rejected']}；"
            f"订单：批准 {kinds['risk_approved']}，削减 {kinds['risk_reduced']}，拒绝 {kinds['risk_rejected']}"
        )
    traded = of("trader")
    if traded:
        lines["trader"] = (
            f"成交 {sum(1 for event in traded if event['kind'] == 'fill')} 笔，"
            f"下单 {sum(1 for event in traded if event['kind'] == 'order_placed')} 张"
        )
    return lines
