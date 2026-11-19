"""The decision journal: one SQLite file that records what every run did.

It holds the runs themselves, every event (who said what to whom, in order),
the macro view each model configuration last formed, the paper broker's
books (accounts, positions, orders, fills, daily equity), the pipeline's log
of the orders it meant to place, and the checkpoints an interrupted run
resumes from. The dashboard and the evaluation read this file and nothing else.

Rows are keyed by "arm": the name of a model configuration. Two arms can read
the same data on the same day and keep entirely separate records.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import config
from models.evaluation import Lesson, ViewScore
from models.signals import Event, MacroRegime, SignalTable, UsageSummary

# Bumped whenever an existing table changes. A journal written under another
# number is refused rather than read wrongly. Adding a table is not such a
# change: see _ADDED below.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    arm           TEXT NOT NULL,
    session_date  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- running, ok, degraded, failed, superseded
    error         TEXT,
    snapshot_path TEXT,
    llm_calls     INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    model_seconds REAL,
    cost_usd      REAL,
    signals_json  TEXT,
    pid           INTEGER                 -- the process that ran it, to tell a live run from a dead one
);
CREATE INDEX IF NOT EXISTS runs_by_arm ON runs (arm, session_date);

CREATE TABLE IF NOT EXISTS events (
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    stage        TEXT NOT NULL,
    speaker      TEXT NOT NULL,
    speaker_name TEXT NOT NULL,
    is_llm       INTEGER NOT NULL,
    addressed_to TEXT NOT NULL,
    kind         TEXT NOT NULL,
    ticker       TEXT,
    remark       TEXT NOT NULL,
    event_json   TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS regimes (
    arm          TEXT NOT NULL,
    session_date TEXT NOT NULL,
    regime_json  TEXT NOT NULL,
    PRIMARY KEY (arm, session_date)
);

-- The paper broker's own books. One account per model configuration.
CREATE TABLE IF NOT EXISTS paper_accounts (
    account      TEXT PRIMARY KEY,
    cash         REAL NOT NULL,
    initial_cash REAL NOT NULL,
    high_water   REAL NOT NULL,           -- highest equity at any session's close
    last_session TEXT                     -- the last session the book was brought up to
);

CREATE TABLE IF NOT EXISTS paper_positions (
    account           TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    shares            INTEGER NOT NULL,
    avg_cost          REAL NOT NULL,
    entry_date        TEXT NOT NULL,
    sector            TEXT NOT NULL,
    last_adjusted     TEXT NOT NULL,      -- the session whose share scale the position is on
    anchor_close      REAL NOT NULL,      -- the ticker's close on that session, on that scale
    out_of_sync_since TEXT,               -- first sync that could not bring it onto the current scale
    last_close        REAL,               -- close at the last sync, NULL when the data was unusable
    sessions_held     INTEGER NOT NULL,
    PRIMARY KEY (account, ticker)
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id        TEXT PRIMARY KEY,
    account         TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    kind            TEXT NOT NULL,        -- market, limit, stop
    shares          INTEGER NOT NULL,
    limit_price     REAL,
    stop_price      REAL,
    protective_stop REAL,
    day_only        INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL,        -- open, filled, cancelled, expired, rejected
    created_session TEXT NOT NULL,
    filled_price    REAL,
    filled_date     TEXT,
    note            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS paper_orders_open ON paper_orders (account, status);

CREATE TABLE IF NOT EXISTS paper_fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    kind            TEXT NOT NULL,        -- market, limit, stop, close
    shares          INTEGER NOT NULL,
    price           REAL NOT NULL,
    fill_date       TEXT NOT NULL,
    pnl_pct         REAL,
    note            TEXT NOT NULL,
    reported_run_id TEXT                  -- the run whose signal table listed this fill
);

-- What each sync found, so that syncing again to the same session can say it again.
CREATE TABLE IF NOT EXISTS paper_syncs (
    account      TEXT NOT NULL,
    session_date TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    PRIMARY KEY (account, session_date)
);

CREATE TABLE IF NOT EXISTS paper_equity (
    account         TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity          REAL NOT NULL,
    PRIMARY KEY (account, session_date)
);

-- The pipeline's own record of each order it meant to place, kept apart from
-- the broker's books so the two can be compared after a crash.
CREATE TABLE IF NOT EXISTS order_log (
    order_id     TEXT PRIMARY KEY,
    account      TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    session_date TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state        TEXT NOT NULL            -- intent, submitted, confirmed
);

-- Outputs of finished stages, so an interrupted run can pick up where it stopped.
CREATE TABLE IF NOT EXISTS checkpoints (
    arm          TEXT NOT NULL,           -- "*" for what all arms share
    session_date TEXT NOT NULL,
    key          TEXT NOT NULL,
    value_json   TEXT NOT NULL,
    PRIMARY KEY (arm, session_date, key)
);
"""

# Tables that came after format 2 was fixed. They are created when a journal
# is opened and does not have them, so a journal that was written before they
# existed keeps everything in it and simply gains them. The program that
# wrote it can still read it afterwards: it never looks at these tables.
_ADDED = {
    # What each analyst view turned out to be worth, once its horizon had ended.
    "view_scores": """
CREATE TABLE view_scores (
    run_id       TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    arm          TEXT NOT NULL,
    session_date TEXT NOT NULL,
    status       TEXT NOT NULL,           -- scored, pending, unpriced
    score_json   TEXT NOT NULL,
    PRIMARY KEY (arm, run_id, ticker)
)""",
    # What a model wrote about one of its own views after the real outcome was known.
    "lessons": """
CREATE TABLE lessons (
    arm          TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    session_date TEXT NOT NULL,           -- when the view was written
    known_on     TEXT NOT NULL,           -- the session at whose close the outcome was known
    sector       TEXT NOT NULL,
    clean        INTEGER NOT NULL,        -- 0 when the text states figures it was not shown
    lesson_json  TEXT NOT NULL,
    PRIMARY KEY (arm, ticker, session_date)
)""",
    # The desk's meetings through the day. A check-in (before the open, or
    # during the session) lives only here, with its events under its id in
    # the events table. A close meeting is a run like any other and stays in
    # the runs table; its row here adds the day and the cut-off.
    "meetings": """
CREATE TABLE meetings (
    meeting_id    TEXT PRIMARY KEY,       -- for a close meeting, the id of its run
    arm           TEXT NOT NULL,
    day           TEXT NOT NULL,          -- the trading day the meeting belongs to
    kind          TEXT NOT NULL,          -- premarket, midday, afternoon, close
    session_date  TEXT NOT NULL,          -- the last completed session when it was held
    as_of         TEXT,                   -- cut-off: nothing first seen after it was shown
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- running, ok, degraded, failed
    error         TEXT,
    pid           INTEGER,
    llm_calls     INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    model_seconds REAL,
    cost_usd      REAL,
    record_json   TEXT
)""",
}

COMPLETED = ("ok", "degraded")


class JournalError(RuntimeError):
    """The journal cannot be used as it is, or a transaction in it could not be saved."""


class Journal:
    def __init__(self, path: str | Path | None = None, timeout: float = 5.0) -> None:
        self.path = Path(path or config.JOURNAL_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=timeout)
        self._db.row_factory = sqlite3.Row
        self._depth = 0
        self._failed = False

        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone() is None:
            self._db.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
        elif version != SCHEMA_VERSION:
            self._db.close()
            raise JournalError(
                f"{self.path} was written in journal format {version}; this program uses format "
                f"{SCHEMA_VERSION}. Move the file aside to start a new journal."
            )
        # Looked up first, and created only when missing: creating a table
        # takes the write lock, which a pipeline run in progress may hold.
        present = {row["name"] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for name, statement in _ADDED.items():
            if name not in present:
                self._db.execute(statement)
        self._db.commit()
        # Write-ahead logging: someone reading the journal (the dashboard) never
        # blocks the pipeline from saving.
        self._db.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Everything inside is saved together or not at all.

        Transactions nest. The writes are committed when the outermost one
        ends. If any part raises, none of it is saved, even when the
        exception is caught before the outermost block ends: that block then
        raises JournalError instead of committing half the work.

        The write lock is taken at the start, so what is read inside cannot
        be changed by another process before it is written back. For the same
        reason, never open a transaction on a second Journal for the same
        file while one is open here: it would wait for this one and time out.
        """
        if self._depth == 0:
            self._failed = False
            self._db.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            self._failed = True
            if self._depth == 0:
                self._db.rollback()
            raise
        self._depth -= 1
        if self._depth == 0:
            if self._failed:
                self._db.rollback()
                raise JournalError("A step inside this transaction failed, so none of it was saved.")
            try:
                self._commit()
            except BaseException:
                # A commit that fails leaves the transaction open. Without this
                # the journal could not start another one, even to record the failure.
                self._db.rollback()
                raise

    def _commit(self) -> None:
        self._db.commit()

    # ── Plain access, for the modules that keep their own tables here ────

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._db.execute(sql, params).fetchall()

    def write(self, sql: str, params: tuple = ()) -> None:
        """Run one changing statement. Only inside a transaction."""
        if self._depth == 0:
            raise JournalError("Books may only be changed inside Journal.transaction().")
        self._db.execute(sql, params)

    # ── Checkpoints ──────────────────────────────────────────────────────

    def checkpoint(self, arm: str, session: date, key: str) -> str | None:
        row = self._db.execute(
            "SELECT value_json FROM checkpoints WHERE arm = ? AND session_date = ? AND key = ?",
            (arm, session.isoformat(), key),
        ).fetchone()
        return row["value_json"] if row else None

    def save_checkpoint(self, arm: str, session: date, key: str, value_json: str) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?)",
                (arm, session.isoformat(), key, value_json),
            )

    def clear_checkpoints(self, arm: str, session: date) -> None:
        with self.transaction():
            self._db.execute(
                "DELETE FROM checkpoints WHERE arm = ? AND session_date = ?", (arm, session.isoformat())
            )

    def prune_checkpoints(self, before: date) -> None:
        """Drop checkpoints of sessions before `before`: nothing can resume them any more."""
        with self.transaction():
            self._db.execute("DELETE FROM checkpoints WHERE session_date < ?", (before.isoformat(),))

    # ── Runs ─────────────────────────────────────────────────────────────

    def start_run(
        self, run_id: str, arm: str, session: date, started_at: datetime, pid: int | None = None,
    ) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT INTO runs (run_id, arm, session_date, started_at, status, pid) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, arm, session.isoformat(), started_at.isoformat(), os.getpid() if pid is None else pid),
            )

    def finish_run(
        self, run_id: str, status: str, finished_at: datetime, usage: UsageSummary,
        table: SignalTable | None = None, snapshot_path: str | None = None, error: str | None = None,
    ) -> None:
        with self.transaction():
            self._db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ?, snapshot_path = ?, llm_calls = ?, "
                "input_tokens = ?, output_tokens = ?, model_seconds = ?, cost_usd = ?, signals_json = ? "
                "WHERE run_id = ?",
                (status, finished_at.isoformat(), error, snapshot_path, usage.calls, usage.input_tokens,
                 usage.output_tokens, usage.model_seconds, usage.cost_usd,
                 table.model_dump_json() if table else None, run_id),
            )

    def completed_run(self, arm: str, session: date) -> str | None:
        """The id of a run that already produced signals for this arm and session, if any."""
        row = self._db.execute(
            f"SELECT run_id FROM runs WHERE arm = ? AND session_date = ? AND status IN ({','.join('?' * len(COMPLETED))}) "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (arm, session.isoformat(), *COMPLETED),
        ).fetchone()
        return row["run_id"] if row else None

    def supersede_runs(self, arm: str, session: date) -> list[str]:
        """Mark the finished runs of a session as replaced, when that session is started over.

        From then on the session counts as not done, so if the run replacing
        them dies, the next one takes the session up instead of skipping it.
        Returns the ids of the runs replaced.
        """
        marks = ",".join("?" * len(COMPLETED))
        with self.transaction():
            replaced = [row["run_id"] for row in self._db.execute(
                f"SELECT run_id FROM runs WHERE arm = ? AND session_date = ? AND status IN ({marks})",
                (arm, session.isoformat(), *COMPLETED),
            )]
            self._db.execute(
                f"UPDATE runs SET status = 'superseded' WHERE arm = ? AND session_date = ? AND status IN ({marks})",
                (arm, session.isoformat(), *COMPLETED),
            )
        return replaced

    def running_run(self, arm: str, session: date) -> sqlite3.Row | None:
        """The latest run for this arm and session that started and never finished, if any."""
        return self._db.execute(
            "SELECT run_id, started_at, pid FROM runs WHERE arm = ? AND session_date = ? AND status = 'running' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (arm, session.isoformat()),
        ).fetchone()

    def runs(self, arm: str | None = None) -> list[sqlite3.Row]:
        """All runs, newest first. Rows carry every column of the runs table."""
        if arm is None:
            return self._db.execute("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC").fetchall()
        return self._db.execute("SELECT * FROM runs WHERE arm = ? ORDER BY started_at DESC, rowid DESC", (arm,)).fetchall()

    def arms(self) -> list[str]:
        return [row["arm"] for row in self._db.execute("SELECT DISTINCT arm FROM runs ORDER BY arm")]

    def signal_table(self, run_id: str) -> SignalTable | None:
        row = self._db.execute("SELECT signals_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return SignalTable.model_validate_json(row["signals_json"]) if row and row["signals_json"] else None

    # ── The desk's meetings ──────────────────────────────────────────────

    def start_meeting(
        self, meeting_id: str, arm: str, day: date, kind: str, session: date, as_of: datetime,
        started_at: datetime, pid: int | None = None,
    ) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT INTO meetings (meeting_id, arm, day, kind, session_date, as_of, started_at, status, pid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (meeting_id, arm, day.isoformat(), kind, session.isoformat(), as_of.isoformat(),
                 started_at.isoformat(), os.getpid() if pid is None else pid),
            )

    def finish_meeting(
        self, meeting_id: str, status: str, finished_at: datetime, usage: UsageSummary,
        record_json: str | None = None, error: str | None = None,
    ) -> None:
        with self.transaction():
            self._db.execute(
                "UPDATE meetings SET status = ?, finished_at = ?, error = ?, llm_calls = ?, input_tokens = ?, "
                "output_tokens = ?, model_seconds = ?, cost_usd = ?, record_json = ? WHERE meeting_id = ?",
                (status, finished_at.isoformat(), error, usage.calls, usage.input_tokens, usage.output_tokens,
                 usage.model_seconds, usage.cost_usd, record_json, meeting_id),
            )

    def note_close_meeting(self, run_id: str, as_of: datetime) -> None:
        """Enter a finished close run among the desk's meetings, with the cut-off it was held at."""
        with self.transaction():
            self._db.execute(
                "INSERT OR REPLACE INTO meetings (meeting_id, arm, day, kind, session_date, as_of, started_at, "
                "finished_at, status, error, pid, llm_calls, input_tokens, output_tokens, model_seconds, cost_usd) "
                "SELECT run_id, arm, session_date, 'close', session_date, ?, started_at, finished_at, status, error, "
                "pid, llm_calls, input_tokens, output_tokens, model_seconds, cost_usd FROM runs WHERE run_id = ?",
                (as_of.isoformat(), run_id),
            )

    def meeting(self, meeting_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM meetings WHERE meeting_id = ?", (meeting_id,)).fetchone()

    def meetings(self, arm: str, day: date | None = None) -> list[sqlite3.Row]:
        """An arm's meetings, newest first. Rows carry every column of the meetings table."""
        sql, params = "SELECT * FROM meetings WHERE arm = ?", [arm]
        if day is not None:
            sql, params = sql + " AND day = ?", params + [day.isoformat()]
        return self._db.execute(sql + " ORDER BY started_at DESC, rowid DESC", tuple(params)).fetchall()

    def completed_meeting(self, arm: str, day: date, kind: str) -> str | None:
        """The id of a meeting of this kind that the arm already held on this day, if any."""
        row = self._db.execute(
            f"SELECT meeting_id FROM meetings WHERE arm = ? AND day = ? AND kind = ? "
            f"AND status IN ({','.join('?' * len(COMPLETED))}) ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (arm, day.isoformat(), kind, *COMPLETED),
        ).fetchone()
        return row["meeting_id"] if row else None

    def running_meetings(self, arm: str) -> list[sqlite3.Row]:
        """The arm's meetings that started and never finished."""
        return self._db.execute(
            "SELECT * FROM meetings WHERE arm = ? AND status = 'running' ORDER BY started_at DESC, rowid DESC", (arm,)
        ).fetchall()

    # ── Events ───────────────────────────────────────────────────────────

    def add_event(self, run_id: str, event: Event) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, event.seq, event.ts.isoformat(), event.stage, event.speaker, event.speaker_name,
                 int(event.is_llm), event.addressed_to, event.kind, event.ticker, event.remark,
                 event.model_dump_json()),
            )

    def events(self, run_id: str) -> list[Event]:
        rows = self._db.execute("SELECT event_json FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
        return [Event.model_validate_json(row["event_json"]) for row in rows]

    # ── Macro view ───────────────────────────────────────────────────────

    def save_regime(self, arm: str, regime: MacroRegime) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT OR REPLACE INTO regimes VALUES (?, ?, ?)",
                (arm, regime.session_date.isoformat(), regime.model_dump_json()),
            )

    def latest_regime(self, arm: str, session: date) -> MacroRegime | None:
        """The arm's most recent macro view formed on or before the session."""
        row = self._db.execute(
            "SELECT regime_json FROM regimes WHERE arm = ? AND session_date <= ? ORDER BY session_date DESC LIMIT 1",
            (arm, session.isoformat()),
        ).fetchone()
        return MacroRegime.model_validate_json(row["regime_json"]) if row else None

    # ── Evaluation ───────────────────────────────────────────────────────

    def save_view_scores(self, arm: str, scores: list[ViewScore]) -> None:
        """Replace an arm's scores with these, all at once.

        Replaced, not added to: the scores of a run that has since been
        replaced by a forced rerun must not stay behind for a reader to find.
        """
        with self.transaction():
            self._db.execute("DELETE FROM view_scores WHERE arm = ?", (arm,))
            for score in scores:
                self._db.execute(
                    "INSERT INTO view_scores VALUES (?, ?, ?, ?, ?, ?)",
                    (score.run_id, score.ticker, arm, score.session_date.isoformat(), score.status,
                     score.model_dump_json()),
                )

    def view_scores(self, arm: str) -> list[ViewScore]:
        rows = self._db.execute(
            "SELECT score_json FROM view_scores WHERE arm = ? ORDER BY session_date, ticker", (arm,)
        )
        return [ViewScore.model_validate_json(row["score_json"]) for row in rows]

    def save_lesson(self, lesson: Lesson) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT OR REPLACE INTO lessons VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lesson.arm, lesson.ticker, lesson.session_date.isoformat(), lesson.known_on.isoformat(),
                 lesson.sector, 0 if lesson.data_warnings else 1, lesson.model_dump_json()),
            )

    def lessons(self, arm: str, known_by: date | None = None, clean_only: bool = False) -> list[Lesson]:
        """An arm's lessons, the most recently learned first.

        `known_by` leaves out every lesson whose outcome was not yet known at
        that session's close: a decision may only be shown what had happened
        by the time it is made.
        """
        sql, params = "SELECT lesson_json FROM lessons WHERE arm = ?", [arm]
        if known_by is not None:
            sql, params = sql + " AND known_on <= ?", params + [known_by.isoformat()]
        if clean_only:
            sql += " AND clean = 1"
        rows = self._db.execute(sql + " ORDER BY known_on DESC, session_date DESC, ticker", tuple(params))
        return [Lesson.model_validate_json(row["lesson_json"]) for row in rows]

    # ── What the books hold ──────────────────────────────────────────────

    def book_tickers(self, accounts: list[str]) -> list[str]:
        """Every ticker any of these paper accounts holds or has an open order on."""
        if not accounts:
            return []
        marks = ",".join("?" * len(accounts))
        rows = self._db.execute(
            f"SELECT ticker FROM paper_positions WHERE account IN ({marks}) "
            f"UNION SELECT ticker FROM paper_orders WHERE status = 'open' AND account IN ({marks}) ORDER BY ticker",
            (*accounts, *accounts),
        )
        return [row["ticker"] for row in rows]
