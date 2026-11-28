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
from data.ledger import Ledger
from data.models import Bars, Snapshot
from journal.store import COMPLETED
from forecast.scoring import card as forecast_card
from models.evaluation import Evaluation
from models.forecast import ForecastScore, ForecastSet
from models.planning import Contingency, Plan, Trigger
from models.signals import SignalTable
from pipeline.plans import acts, identity, unless_ahead
from risk.gate import halted

STAGE_TITLES = dict(STAGES)
FILLS_SHOWN = 100
RUNS_SHOWN = 60
SESSIONS_SHOWN = 30                 # how far back the page that sets sessions side by side goes
BARS_SHOWN = 120                    # daily bars of a ticker sent to the page: about half a year
TREND_SHOWN = 60                    # daily closes of a market series sent to the page, for the line in its tile
AFTER_SHOWN = 60                    # bars after a session, from later sessions' snapshots
SNAPSHOTS_KEPT = SESSIONS_SHOWN + 2 # snapshots held in memory once read
SAME_PRICE = 0.005                  # two snapshots agree on a day's close when they are this close (as a share of it)
# Not a status the journal holds. It is what the page calls a run that is on record as
# running although the process that was running it no longer exists.
INTERRUPTED = "interrupted"
# A check-in held because a condition of a plan said to call a meeting. A day may have more than one.
CALLED = "called"
# What the page shows of each version of a plan. Recorded sentences among them are passed on as they are.
PLAN_FIELDS = (
    "version", "session_date", "created_at", "source", "held", "thesis", "direction", "horizon", "ends_on", "close",
    "stop", "target", "chances", "changes", "basis", "refused", "data_warnings",
)


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


# A check-in of the desk, read as if it were a run: the same columns under the same names, so
# the page shows the two alike. Its `session_date` is the trading day it was held on, and
# `built_on` the session whose close meeting it asked about. Close meetings are runs already.
_CHECKINS = (
    "SELECT meeting_id AS run_id, arm, day AS session_date, started_at, finished_at, status, error, pid, "
    "NULL AS snapshot_path, llm_calls, input_tokens, output_tokens, model_seconds, cost_usd, NULL AS signals_json, "
    "kind, as_of, session_date AS built_on FROM meetings WHERE kind != 'close' AND arm = ?"
)


def _kind(row: sqlite3.Row) -> str:
    """Which meeting a row is: a run is the close meeting, a check-in says its own kind."""
    return row["kind"] if "kind" in row.keys() else "close"


def _shown(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    """Of several attempts, newest first: one still going, else the one that stands, else the last attempt."""
    for wanted in (("running",), COMPLETED):
        for row in rows:
            if _status(row) in wanted:
                return row
    return rows[0] if rows else None


class Reader:
    def __init__(
        self, journal_path: str | Path | None = None, evaluation_dir: str | Path | None = None,
        ledger_path: str | Path | None = None,
    ) -> None:
        self.journal_path = Path(journal_path or config.JOURNAL_PATH)
        self.evaluation_dir = Path(evaluation_dir or config.EVALUATION_DIR)
        # The desk's ledger lies beside the journal it belongs to, unless it is named.
        self.ledger_path = Path(ledger_path) if ledger_path else (
            self.journal_path.with_name("ledger.sqlite3") if journal_path else Path(config.LEDGER_PATH))
        self._report: tuple[Path, bytes, Evaluation] | None = None      # the last report read, to read it only once
        self._snapshots: dict[str, tuple[tuple[int, int], Snapshot]] = {}    # data snapshots read, by the file's path

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
        return _shown(db.execute(
            "SELECT * FROM runs WHERE arm = ? AND session_date = ? ORDER BY started_at DESC, rowid DESC", (arm, session)
        ).fetchall())

    @staticmethod
    def _checkins(db: sqlite3.Connection, arm: str, where: str = "", params: tuple = ()) -> list[sqlite3.Row]:
        """An arm's check-ins, newest first. None for a journal no desk has written to: it has no such table."""
        try:
            return db.execute(f"{_CHECKINS}{where} ORDER BY started_at DESC, rowid DESC", (arm, *params)).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return []
            raise

    @staticmethod
    def _cutoff(db: sqlite3.Connection, row: sqlite3.Row) -> str | None:
        """A meeting's cut-off: nothing first seen after it was shown. Only the desk's meetings have one."""
        if "as_of" in row.keys():
            return row["as_of"]
        try:
            found = db.execute("SELECT as_of FROM meetings WHERE meeting_id = ?", (row["run_id"],)).fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return None
            raise
        return found["as_of"] if found else None

    def _day_meetings(self, db: sqlite3.Connection, arm: str, day: str) -> list[sqlite3.Row]:
        """The meetings of one trading day, in the order they were held: for each kind, the one that is shown.

        A check-in called by a plan is a meeting of its own, not another attempt at one: each is listed.
        """
        by_kind: dict[str, list[sqlite3.Row]] = {}
        for row in self._checkins(db, arm, " AND day = ?", (day,)):
            by_kind.setdefault(row["run_id"] if row["kind"] == CALLED else row["kind"], []).append(row)
        found = [_shown(rows) for rows in by_kind.values()]
        close = self._shown_run(db, arm, day)
        if close is not None:
            found.append(close)
        return sorted(found, key=lambda row: datetime.fromisoformat(row["started_at"]))

    def _shown_for_day(self, db: sqlite3.Connection, arm: str, day: str) -> sqlite3.Row | None:
        """The meeting to show for a day: its close meeting, else the last check-in held so far."""
        close = self._shown_run(db, arm, day)
        if close is not None:
            return close
        held = self._day_meetings(db, arm, day)
        return held[-1] if held else None

    def _stands(self, db: sqlite3.Connection, arm: str, row: sqlite3.Row) -> bool:
        """Whether a meeting is the one shown for its day and kind, or another attempt at it."""
        if _kind(row) == "close":
            return self._shown_run(db, arm, row["session_date"])["run_id"] == row["run_id"]
        if _kind(row) == CALLED:
            return True                             # each called check-in is a meeting of its own
        attempts = self._checkins(db, arm, " AND day = ? AND kind = ?", (row["session_date"], row["kind"]))
        return _shown(attempts)["run_id"] == row["run_id"]

    def _latest(self, db: sqlite3.Connection, arm: str) -> sqlite3.Row | None:
        """What the desk is at now: a meeting still going, else the last one that was held.

        For an arm that holds no check-ins this is the run shown for its
        latest session, as it always was.
        """
        session = db.execute("SELECT MAX(session_date) FROM runs WHERE arm = ?", (arm,)).fetchone()[0]
        run = self._shown_run(db, arm, session) if session else None
        checkins = self._checkins(db, arm)
        going = [row for row in checkins if _status(row) == "running"]
        if going:
            return going[0]
        held = next((row for row in checkins if row["status"] in COMPLETED), None)
        if run is None or held is None or _status(run) == "running":
            return run if run is not None else (held or (checkins[0] if checkins else None))
        later = datetime.fromisoformat(held["started_at"]) > datetime.fromisoformat(run["started_at"])
        return held if later else run

    def _named_run(
        self, db: sqlite3.Connection, arm: str, session: str | None, run_id: str | None, close_only: bool = False,
    ) -> sqlite3.Row | None:
        """The meeting that is asked for: one by name, else the one shown for a day, else what the desk is at now.

        With `close_only` and nothing named, it is the close meeting of the latest session that has one.
        """
        if run_id is not None:
            run = db.execute("SELECT * FROM runs WHERE run_id = ? AND arm = ?", (run_id, arm)).fetchone()
            if run is not None:
                return run
            found = self._checkins(db, arm, " AND meeting_id = ?", (run_id,))
            return found[0] if found else None
        if session is None and close_only:
            latest = db.execute("SELECT MAX(session_date) FROM runs WHERE arm = ?", (arm,)).fetchone()[0]
            return self._shown_run(db, arm, latest) if latest else None
        if session is None:
            return self._latest(db, arm)
        return self._shown_for_day(db, arm, session)

    def _days(self, db: sqlite3.Connection, arm: str) -> list[str]:
        """Every trading day an arm held a meeting on, newest first."""
        days = {row["session_date"] for row in db.execute("SELECT DISTINCT session_date FROM runs WHERE arm = ?", (arm,))}
        days |= {row["session_date"] for row in self._checkins(db, arm)}
        return sorted(days, reverse=True)

    @staticmethod
    def _lines(db: sqlite3.Connection, run_id: str) -> int:
        return db.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]

    def sessions(self, arm: str) -> list[dict]:
        """Every trading day an arm held a meeting on, newest first, with the meeting that is shown for it."""
        with self._db() as db:
            found = []
            for day in self._days(db, arm):
                run = self._shown_for_day(db, arm, day)
                found.append({"session": day, **_run(run), "events": self._lines(db, run["run_id"])})
            return found

    def day(self, arm: str, day: str | None = None) -> dict:
        """One trading day at the desk: its meetings in the order they were held, and the watch's last round.

        Without a day it is the latest one any meeting was held on. For each
        kind of meeting the one that is shown is listed, as for a session's
        run. `watch` is the last round of the watch on record for this arm,
        whatever day it was on; None when there is no ledger or no round.
        """
        with self._db() as db:
            if day is None:
                days = self._days(db, arm)
                day = days[0] if days else None
            meetings = [] if day is None else [
                {"session": day, **_run(row, self._cutoff(db, row)), "events": self._lines(db, row["run_id"])}
                for row in self._day_meetings(db, arm, day)
            ]
        return {"arm": arm, "day": day, "meetings": meetings, "watch": self.watch(arm)}

    def watch(self, arm: str) -> dict | None:
        """The last round of the desk's watch, as the ledger recorded it. The ledger is only read."""
        try:
            ledger = Ledger(self.ledger_path, read_only=True)
        except (sqlite3.Error, OSError, ValueError):
            return None
        try:
            rounds = ledger.watches(arm, limit=1)
        except (sqlite3.Error, ValueError):
            return None
        finally:
            ledger.close()
        return rounds[0] if rounds else None

    # ── The meeting ──────────────────────────────────────────────────────

    def meeting(
        self, arm: str, session: str | None = None, run_id: str | None = None, close_only: bool = False,
    ) -> dict:
        """One meeting and everything said in it, in order.

        Named by `run_id`, it is that run or check-in, whichever attempt it
        was. Otherwise it is the meeting shown for the day `session`: its
        close meeting, or its last check-in while there is none yet. With
        nothing named it is what the desk is at now, or, with `close_only`,
        the latest close meeting. A meeting or day this arm does not have
        comes back empty, with no run. `stands` says whether the meeting is
        the one shown for its day and kind, or another attempt at it.
        """
        with self._db() as db:
            run = self._named_run(db, arm, session, run_id, close_only)
            events, stands, cutoff = [], None, None
            if run is not None:
                events = [_event(row) for row in db.execute(
                    "SELECT event_json FROM events WHERE run_id = ? ORDER BY seq", (run["run_id"],))]
                stands = self._stands(db, arm, run)
                cutoff = self._cutoff(db, run)
        return {
            "arm": arm, "session": None if run is None else run["session_date"],
            "run": None if run is None else _run(run, cutoff), "stands": stands,
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
            # The newest meeting of either sort: a run, or a check-in of the desk.
            newest = db.execute(
                "SELECT * FROM runs WHERE arm = ? ORDER BY started_at DESC, rowid DESC LIMIT 1", (arm,)
            ).fetchone()
            checkin = next(iter(self._checkins(db, arm)), None)
            if checkin is not None and (newest is None or datetime.fromisoformat(checkin["started_at"])
                                        > datetime.fromisoformat(newest["started_at"])):
                newest = checkin
            if newest is None:
                return [], run_id, seq, status
            messages = []
            if newest["run_id"] != run_id:
                left = None if run_id is None else self._named_run(db, arm, None, run_id)
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
        forecasts = self.forecasts(arm)
        if evaluation is None:
            return {"arm": arm, "report": None, "arms": [], "lessons": lessons,
                    "lessons_shown": config.PIPELINE_USE_LESSONS, "forecasts": forecasts}
        mine = next((item for item in evaluation.arms if item.arm == arm), None)
        return {
            "arm": arm, "as_of": evaluation.as_of.isoformat(), "created_at": evaluation.created_at.isoformat(),
            "benchmark": evaluation.benchmark, "file": str(path), "forecasts": forecasts,
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

    def forecasts(self, arm: str) -> dict:
        """What the forecast engine has on record for an arm: how its distributions held up, and what it last said.

        Read from the journal, where each close meeting of the desk keeps its
        forecasts and reconciles the ones that came due. Nothing is worked
        out here beyond counting the scores on record.
        """
        nothing = {"arm": arm, "sessions": 0, "card": None, "latest": None}
        with self._db() as db:
            tables = {row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('forecast_sets', 'forecast_scores')")}
            if "forecast_sets" not in tables:
                return nothing
            sessions = db.execute("SELECT COUNT(*) FROM forecast_sets WHERE arm = ?", (arm,)).fetchone()[0]
            last = db.execute(
                "SELECT set_json FROM forecast_sets WHERE arm = ? ORDER BY session_date DESC LIMIT 1", (arm,)).fetchone()
            rows = db.execute("SELECT score_json FROM forecast_scores WHERE arm = ?", (arm,)).fetchall() \
                if "forecast_scores" in tables else []
        if not sessions:
            return nothing
        try:
            scores = [ForecastScore.model_validate_json(row["score_json"]) for row in rows]
            latest = ForecastSet.model_validate_json(last["set_json"])
        except ValueError:
            return nothing                          # written by another version of the program
        return {
            "arm": arm, "sessions": sessions, "card": forecast_card(arm, scores).model_dump(mode="json"),
            # The percentiles kept for scoring are of no use to a page.
            "latest": latest.model_dump(
                mode="json", exclude={"tickers": {"__all__": {"horizons": {"__all__": {"grid", "naive_grid"}}}}}),
        }

    # ── The desk's plans ─────────────────────────────────────────────────

    def plans(self, arm: str) -> dict:
        """The desk's plan for each trade: the ones that stand, every version of each, and the ones that are over.

        Read from the journal as the close meetings and the watch left it.
        Each plan's conditions are those of its latest version, in its own
        order, then any condition an earlier version had that has since been
        dropped and that held while it stood, so that every condition found
        to hold is on the page. Only the desk keeps plans; a journal from
        before them has none.
        """
        nothing = {"arm": arm, "has_plans": False, "standing": [], "ended": []}
        with self._db() as db:
            tables = {row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('plans', 'plan_triggers', 'paper_positions')")}
            if "plans" not in tables:
                return nothing
            rows = db.execute(
                "SELECT plan_json, closed_on, closed_why FROM plans WHERE arm = ? ORDER BY ticker, opened, version", (arm,)
            ).fetchall()
            fired = db.execute("SELECT trigger_json FROM plan_triggers WHERE arm = ?", (arm,)).fetchall() \
                if "plan_triggers" in tables else []
            held = {row["ticker"] for row in db.execute(
                "SELECT ticker FROM paper_positions WHERE account = ?", (arm,))} if "paper_positions" in tables else None
        try:
            versions = [(Plan.model_validate_json(row["plan_json"]), row["closed_on"], row["closed_why"] or "") for row in rows]
            triggers = [Trigger.model_validate_json(row["trigger_json"]) for row in fired]
        except ValueError:
            return nothing                          # written by another version of the program
        by_plan: dict[tuple[str, str], list] = {}
        for plan, closed_on, closed_why in versions:
            by_plan.setdefault((plan.ticker, plan.opened.isoformat()), []).append((plan, closed_on, closed_why))
        standing, ended = [], []
        for (ticker, opened), found in by_plan.items():
            plans = [plan for plan, _, _ in found]
            mine = {trigger.identity: trigger for trigger in triggers
                    if trigger.ticker == ticker and trigger.opened.isoformat() == opened}
            conditions = _conditions(plans, mine)
            _, closed_on, closed_why = found[-1]
            if closed_on is None:
                standing.append({
                    "ticker": ticker, "opened": opened, "held_now": None if held is None else ticker in held,
                    "versions": [_version(plan) for plan in plans], "conditions": conditions,
                })
            else:
                ended.append({
                    "ticker": ticker, "opened": opened, "closed_on": closed_on, "closed_why": closed_why,
                    "versions": len(plans), "last_version": _version(plans[-1]), "conditions": conditions,
                })
        ended.sort(key=lambda item: (item["closed_on"], item["opened"]), reverse=True)
        return {"arm": arm, "has_plans": bool(by_plan), "standing": standing, "ended": ended}

    # ── How decisions changed from session to session ────────────────────

    def history(self, arm: str) -> dict:
        """What was decided about each ticker in each of the last finished sessions, oldest first.

        Read from the signal table each standing run left: the analyst's
        direction and confidence, whether the ticker was held, what was
        proposed, what the checks and the risk gate did with it, and the
        order that passed the gate. Tickers are listed with the most
        recently active first.

        Only the first proposal for a ticker counts, as in the pipeline:
        `action`, `source` and `check` are that one's, and later ones are
        counted in `repeats`. `kept_by_default` says the checks kept the
        holding by their own default after turning the proposal back. A sale
        a condition of the ticker's plan put in (source "plan") takes the
        place of whatever was decided before it, as it did at the meeting.
        """
        with self._db() as db:
            rows = db.execute(
                "SELECT run_id, session_date, signals_json FROM runs WHERE arm = ? AND status IN ('ok', 'degraded') "
                "AND signals_json IS NOT NULL ORDER BY session_date DESC, started_at DESC, rowid DESC", (arm,)
            ).fetchall()
        sessions: list[dict] = []
        taken: set[str] = set()
        for row in rows:
            if row["session_date"] in taken:
                continue                                # an older finished run of a day already taken: never twice
            if len(sessions) == SESSIONS_SHOWN:
                break
            taken.add(row["session_date"])
            table = SignalTable.model_validate_json(row["signals_json"])
            tickers: dict[str, dict] = {}

            def of(ticker: str) -> dict:
                return tickers.setdefault(ticker, {
                    "direction": None, "confidence": None, "held": False, "action": None, "source": None,
                    "check": None, "repeats": 0, "kept_by_default": False, "verdict": None, "approved_shares": None,
                    "ordered": None,
                })

            for position in table.holdings:
                of(position.ticker)["held"] = True
            for view in table.views:
                of(view.ticker).update(direction=view.direction, confidence=view.confidence)
                if view.held:
                    of(view.ticker)["held"] = True      # the view says so itself, for a table that lists no holdings
            for proposal in table.proposals:            # the manager's own first, in the order given, then the defaults
                if not proposal.ticker:
                    continue                            # written for no ticker at all: it is no ticker's decision
                cell = of(proposal.ticker)
                if proposal.source == "plan":
                    # A sale a condition of the ticker's plan put in: the decision before it was set aside for it.
                    cell.update(action=proposal.action, source=proposal.source, check=proposal.status,
                                kept_by_default=False)
                elif cell["action"] is None:
                    cell.update(action=proposal.action, source=proposal.source, check=proposal.status)
                elif proposal.source == "default":
                    cell["kept_by_default"] = True
                else:
                    cell["repeats"] += 1
            for decision in table.risk:
                of(decision.ticker).update(verdict=decision.verdict, approved_shares=decision.approved_shares)
            for order in table.orders:
                of(order.ticker)["ordered"] = {"side": order.side, "shares": order.shares}
            sessions.append({
                "session": row["session_date"], "run_id": row["run_id"], "status": table.status,
                "halted": bool(table.halted),
                "regime": None if table.regime is None else {
                    "risk_appetite": table.regime.risk_appetite, "confidence": table.regime.confidence,
                },
                "tickers": tickers,
            })
        sessions.reverse()
        last_seen = {ticker: at for at, item in enumerate(sessions) for ticker in item["tickers"]}
        return {
            "arm": arm, "sessions": sessions,
            "tickers": sorted(last_seen, key=lambda ticker: (-last_seen[ticker], ticker)),
        }

    # ── The market data a run worked from ────────────────────────────────

    def _snapshot(self, path: str | None) -> Snapshot | None:
        """The data snapshot a run recorded, read from the file the journal names. None when it cannot be read.

        The file is only read. A snapshot that has been read is kept, and read
        again only when the file has changed.
        """
        if not path:
            return None
        file = Path(path)
        try:
            found = file.stat()
            stamp = (found.st_mtime_ns, found.st_size)
            kept = self._snapshots.get(path)
            if kept is None or kept[0] != stamp:
                kept = (stamp, Snapshot.model_validate_json(file.read_text(encoding="utf-8")))
                if len(self._snapshots) >= SNAPSHOTS_KEPT:
                    self._snapshots.pop(next(iter(self._snapshots)))      # the one read longest ago
                self._snapshots[path] = kept
            return kept[1]
        except (OSError, ValueError):
            return None

    @staticmethod
    def _snapshot_path(db: sqlite3.Connection, run: sqlite3.Row) -> str | None:
        """Where a run's data snapshot was saved, or None when that is not on record for this run.

        A run writes it down when it ends. While it is still going, the
        place is on record in the session's data checkpoint, which the
        pipeline saves before any model is called and which all model
        configurations share. That checkpoint is the session's, not the
        run's: once a run is no longer going (it failed before it had data,
        or was killed and the session was run again with fresh data) the
        checkpoint may hold another run's data, so it is not used.
        """
        if run["snapshot_path"]:
            return run["snapshot_path"]
        if _status(run) != "running":
            return None
        try:
            saved = db.execute(
                "SELECT value_json FROM checkpoints WHERE arm = '*' AND session_date = ? AND key = 'data'",
                (run["session_date"],)).fetchone()
            return json.loads(saved["value_json"])["snapshot_path"] if saved else None
        except (sqlite3.OperationalError, ValueError, KeyError, TypeError):
            return None

    def _after(self, ticker: str, last_day: date, last_close: float, later: list[str]) -> dict | None:
        """The bars a later session's snapshot holds for the days after `last_day`, newest snapshot first.

        Taken only from a snapshot that has the same close for `last_day`, so
        the two series are on one scale: a split in between changes the
        earlier prices, and such a snapshot is passed over.
        """
        for path in later:
            snapshot = self._snapshot(path)
            item = snapshot.tickers.get(ticker) if snapshot else None
            if item is None or last_day not in item.bars.dates:
                continue
            at = item.bars.dates.index(last_day)
            same_scale = abs(item.bars.close[at] - last_close) <= SAME_PRICE * abs(last_close)
            if not same_scale or at + 1 >= len(item.bars.dates):
                continue
            return {"through": snapshot.session_date.isoformat(), **_bars(item.bars, at + 1, at + 1 + AFTER_SHOWN)}
        return None

    def market(self, arm: str, session: str | None = None, run_id: str | None = None) -> dict:
        """The prices and market figures one run worked from, as its own data snapshot recorded them.

        Named like `meeting`: one run by `run_id`, else the run shown for
        `session`, else for the latest session. For each ticker in the
        snapshot: the daily bars up to that session, the price summary, the
        indicators and the company figures the agents were shown. `after` is
        what later sessions' snapshots recorded for the days that followed,
        which nobody in that run could see; it is None when no later session
        has that ticker. `available` is false, with the reason, when the run
        left no snapshot or its file can no longer be read.
        """
        with self._db() as db:
            asked = self._named_run(db, arm, session, run_id)
            run = asked
            if asked is not None and _kind(asked) != "close":
                # A check-in worked from the data of the close meeting it asked about: the bars up to
                # that session. What it saw of its own day is in its lines, not in a snapshot.
                run = db.execute(
                    "SELECT * FROM runs WHERE arm = ? AND session_date = ? AND status IN ('ok', 'degraded') "
                    "ORDER BY started_at DESC, rowid DESC LIMIT 1", (arm, asked["built_on"])).fetchone()
            path = None if run is None else self._snapshot_path(db, run)
            later = [] if run is None else [row["snapshot_path"] for row in db.execute(
                "SELECT snapshot_path FROM runs WHERE arm = ? AND session_date > ? AND status IN ('ok', 'degraded') "
                "AND snapshot_path IS NOT NULL ORDER BY session_date DESC, started_at DESC, rowid DESC LIMIT ?",
                (arm, run["session_date"], SESSIONS_SHOWN))]
        answer = {
            "arm": arm, "session": None if run is None else run["session_date"],
            "run_id": None if asked is None else asked["run_id"], "available": False, "reason": "no_run",
            "captured_at": None, "tickers": {}, "market": [], "macro": [],
        }
        if asked is None:
            return answer
        if run is None:
            return {**answer, "reason": "no_snapshot"}
        snapshot = self._snapshot(path)
        # Found through the session's checkpoint (the run has not written its own snapshot down yet): a run's
        # data is always collected before the run starts, so a snapshot captured later is some other
        # invocation's, saved over the checkpoint meanwhile. It is not shown as this run's.
        if snapshot is not None and not run["snapshot_path"] and snapshot.created_at > datetime.fromisoformat(run["started_at"]):
            snapshot = None
        if snapshot is None:
            return {**answer, "reason": "no_snapshot"}
        tickers = {}
        for ticker, item in snapshot.tickers.items():
            count = len(item.bars.dates)
            tickers[ticker] = {
                "name": item.fundamentals.name if item.fundamentals else "",
                "sector": item.fundamentals.sector if item.fundamentals else "",
                "industry": item.fundamentals.industry if item.fundamentals else "",
                "usable": item.usable,
                "issues": [issue.model_dump(mode="json") for issue in item.issues],
                "price": item.price.model_dump(mode="json") if item.price else None,
                "technicals": item.technicals.model_dump(mode="json") if item.technicals else None,
                "fundamentals": item.fundamentals.model_dump(
                    mode="json", exclude={"name", "quote_type", "sector", "industry"}) if item.fundamentals else None,
                "next_earnings": item.next_earnings.isoformat() if item.next_earnings else None,
                "bars": _bars(item.bars, max(0, count - BARS_SHOWN), count),
                "after": self._after(ticker, item.bars.dates[-1], item.bars.close[-1], later) if count else None,
            }
        return {
            **answer, "available": True, "reason": None, "captured_at": snapshot.created_at.isoformat(),
            "tickers": tickers,
            "market": [self._market_point(point, snapshot) for point in snapshot.market],
            "macro": [point.model_dump(mode="json") for point in snapshot.macro],
        }

    @staticmethod
    def _market_point(point, snapshot) -> dict:
        """A market series as the snapshot recorded it, with its latest daily closes where the snapshot kept its
        bars (the index and the sector funds, since the forecast engine needed them). The others have none."""
        answer = point.model_dump(mode="json")
        bars = snapshot.market_bars.get(point.symbol)
        if bars is not None and len(bars.close) >= 2:
            answer["closes"] = [round(value, 4) for value in bars.close[-TREND_SHOWN:]]
        return answer

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

def _bars(bars: Bars, start: int, stop: int) -> dict:
    """Daily bars from `start` up to `stop`, as the snapshot recorded them: the prices as they traded, not adjusted."""
    return {
        "dates": [day.isoformat() for day in bars.dates[start:stop]],
        **{column: getattr(bars, column)[start:stop] for column in ("open", "high", "low", "close", "volume")},
    }


def _version(plan: Plan) -> dict:
    """One version of a plan, as the page shows it."""
    return {key: value for key, value in plan.model_dump(mode="json").items() if key in PLAN_FIELDS}


def _conditions(versions: list[Plan], triggers: dict[str, Trigger]) -> list[dict]:
    """A plan's conditions and where each stands: the latest version's first, then the dropped ones that held.

    A condition is matched to what was found of it by its identity, which is
    the same in every version: it fires once in a plan's life.
    """
    def entry(item: Contingency, version: int, dropped: bool) -> dict:
        trigger = triggers.get(identity(item))
        return {
            # Whether code carries it out is asked of the same rule the desk asks, not read off the record alone.
            **item.model_dump(mode="json"), "automatic": acts(item),
            # A sale on a day is carried out only for a trade that is not ahead. Said by the setting as it stands
            # now, which is what the desk will go by when the day comes: not a record of what was done.
            "unless_ahead": acts(item) and unless_ahead(item), "in_version": version, "dropped": dropped,
            "state": "waiting" if trigger is None else trigger.status,
            "trigger": None if trigger is None else trigger.model_dump(mode="json", include={
                "version", "fired_at", "session_date", "found_by", "evidence", "result", "handled_by", "handled_on"}),
        }

    latest = versions[-1]
    listed = [entry(item, latest.version, False) for item in latest.contingencies]
    seen = {identity(item) for item in latest.contingencies}
    for plan in reversed(versions[:-1]):
        for item in plan.contingencies:
            name = identity(item)
            if name not in seen and name in triggers:
                seen.add(name)
                listed.append(entry(item, plan.version, True))
    # A trigger whose condition no version on record has (it cannot happen through the desk) is still shown.
    for name, trigger in triggers.items():
        if name not in seen:
            seen.add(name)
            listed.append(entry(trigger.contingency, trigger.version, True))
    return listed


def _run(row: sqlite3.Row, as_of: str | None = None) -> dict:
    """A run, or a check-in read as one. `kind` says which meeting it is; `as_of` is its cut-off when it has one."""
    seconds = None
    if row["finished_at"]:
        seconds = (datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()
    status = _status(row)
    return {
        "run_id": row["run_id"], "status": status, "started_at": row["started_at"],
        "finished_at": row["finished_at"], "seconds": seconds, "error": row["error"],
        "live": status == "running", "kind": _kind(row), "as_of": as_of,
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

    # A check-in of the desk: the analyst's updates, the manager's decisions, and what was left for the close.
    updates = of("ticker_analyst", "view_update")
    if updates:
        count = {name: sum(1 for event in updates if event["payload"].get("effect") == name)
                 for name in ("unchanged", "strengthened", "weakened", "broken")}
        lines["ticker_analyst"] = (
            f"{len(updates)} 条更新：不变 {count['unchanged']}，加强 {count['strengthened']}，"
            f"削弱 {count['weakened']}，不成立 {count['broken']}"
        )
    for event in of("portfolio_manager", "checkin_decisions"):
        items = event["payload"].get("decisions", [])
        count = {name: sum(1 for item in items if item.get("action") == name)
                 for name in ("keep", "trim", "sell", "withdraw")}
        lines["portfolio_manager"] = (
            f"{len(items)} 条决定：保留 {count['keep']}，减仓 {count['trim']}，卖出 {count['sell']}，"
            f"撤回买单 {count['withdraw']}"
        )
    noted = of("trader", "noted")
    taken_back = of("trader", "order_withdrawn")
    if noted or (updates and taken_back):
        lines["trader"] = (
            f"留给收盘会 {len(noted)} 条，撤回买单 {len(taken_back)} 张，"
            f"下单 {sum(1 for event in traded if event['kind'] == 'order_placed')} 张"
        )

    # The seats the desk adds to its meetings. Each of these kinds is one line on the record per name (or per
    # thing that could not be fetched, or per condition of a plan found to hold), so the lines are what is counted.
    if of("news_desk", "briefing"):
        lines["news_desk"] = (
            f"{len(of('news_desk', 'news'))} 只股票有新情况，数据缺口 {len(of('news_desk', 'data_gap'))} 处，"
            f"计划条件成立 {len(of('news_desk', 'plan_call'))} 条"
        )
    elif of("news_desk"):
        # A close meeting: the news desk does not open with the book there. It speaks of filings with the SEC,
        # one line a name, and of those it could not read.
        lines["news_desk"] = (
            f"{len(of('news_desk', 'filings'))} 只股票有新公告，数据缺口 {len(of('news_desk', 'data_gap'))} 处"
        )
    if of("quant_engineer"):
        lines["quant_engineer"] = (
            f"预测 {len(of('quant_engineer', 'forecast'))} 只，倒推 {len(of('quant_engineer', 'backward'))} 只，"
            f"推演 {len(of('quant_engineer', 'rehearsal'))} 只"
        )
    asked = of("scenario_analyst", "proposed_events")
    if asked:
        kept = sum(len(event["payload"].get("events") or []) for event in asked)
        refused = sum(len(event["payload"].get("refused") or []) for event in asked)
        lines["scenario_analyst"] = f"看了 {len(asked)} 只股票，提出事件 {kept} 个，另有 {refused} 个被退回"
    return lines
