"""The decision journal: one SQLite file that records what every run did.

It holds the runs themselves, every event (who said what to whom, in order),
the macro view each model configuration last formed, and each configuration's
shadow book. The dashboard and the evaluation read this file and nothing else.

Rows are keyed by "arm": the name of a model configuration. Two arms can read
the same data on the same day and keep entirely separate records.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import config
from models.signals import Event, MacroRegime, SignalTable, UsageSummary

# Bumped whenever a table changes. A journal written under another number is
# refused rather than read wrongly.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    arm           TEXT NOT NULL,
    session_date  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- running, ok, degraded, failed
    error         TEXT,
    snapshot_path TEXT,
    llm_calls     INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    model_seconds REAL,
    cost_usd      REAL,
    signals_json  TEXT
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

CREATE TABLE IF NOT EXISTS shadow_positions (
    arm           TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    shares        REAL NOT NULL,
    entry_price   REAL NOT NULL,
    entry_date    TEXT NOT NULL,
    last_adjusted TEXT NOT NULL,          -- the session whose share scale the position is on
    anchor_close  REAL NOT NULL,          -- the ticker's close on that session, on that scale
    out_of_sync_since TEXT,               -- first run that could not bring it onto the current scale
    PRIMARY KEY (arm, ticker)
);

CREATE TABLE IF NOT EXISTS shadow_pending (
    arm              TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    action           TEXT NOT NULL,       -- buy, sell, trim
    proposed_session TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    PRIMARY KEY (arm, ticker)
);

CREATE TABLE IF NOT EXISTS shadow_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    arm              TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    action           TEXT NOT NULL,       -- buy, sell, trim, or close (closed by the book itself)
    shares           REAL NOT NULL,
    price            REAL NOT NULL,
    trade_date       TEXT NOT NULL,
    proposed_session TEXT NOT NULL,
    pnl_pct          REAL,
    reported_run_id  TEXT                 -- the run whose signal table listed this fill
);
"""

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

    def _require_transaction(self) -> None:
        if self._depth == 0:
            raise JournalError("The shadow book may only be changed inside Journal.transaction().")

    # ── Runs ─────────────────────────────────────────────────────────────

    def start_run(self, run_id: str, arm: str, session: date, started_at: datetime) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT INTO runs (run_id, arm, session_date, started_at, status) VALUES (?, ?, ?, ?, 'running')",
                (run_id, arm, session.isoformat(), started_at.isoformat()),
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

    def running_run(self, arm: str, session: date) -> sqlite3.Row | None:
        """The latest run for this arm and session that started and never finished, if any."""
        return self._db.execute(
            "SELECT run_id, started_at FROM runs WHERE arm = ? AND session_date = ? AND status = 'running' "
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

    # ── Shadow book ──────────────────────────────────────────────────────
    # The writes below do not commit on their own: the shadow book wraps each
    # of its updates in one transaction so a crash cannot leave it half applied.

    def positions(self, arm: str) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM shadow_positions WHERE arm = ? ORDER BY ticker", (arm,)).fetchall()

    def put_position(
        self, arm: str, ticker: str, shares: float, entry_price: float, entry_date: date,
        last_adjusted: date, anchor_close: float, out_of_sync_since: date | None = None,
    ) -> None:
        self._require_transaction()
        self._db.execute(
            "INSERT OR REPLACE INTO shadow_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (arm, ticker, shares, entry_price, entry_date.isoformat(), last_adjusted.isoformat(), anchor_close,
             out_of_sync_since.isoformat() if out_of_sync_since else None),
        )

    def drop_position(self, arm: str, ticker: str) -> None:
        self._require_transaction()
        self._db.execute("DELETE FROM shadow_positions WHERE arm = ? AND ticker = ?", (arm, ticker))

    def pending(self, arm: str) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM shadow_pending WHERE arm = ? ORDER BY proposed_session, ticker", (arm,)
        ).fetchall()

    def put_pending(self, arm: str, ticker: str, action: str, proposed_session: date, run_id: str) -> None:
        self._require_transaction()
        self._db.execute(
            "INSERT OR REPLACE INTO shadow_pending VALUES (?, ?, ?, ?, ?)",
            (arm, ticker, action, proposed_session.isoformat(), run_id),
        )

    def drop_pending(self, arm: str, ticker: str) -> None:
        self._require_transaction()
        self._db.execute("DELETE FROM shadow_pending WHERE arm = ? AND ticker = ?", (arm, ticker))

    def drop_pending_from(self, arm: str, proposed_session: date) -> None:
        self._require_transaction()
        self._db.execute(
            "DELETE FROM shadow_pending WHERE arm = ? AND proposed_session = ?", (arm, proposed_session.isoformat())
        )

    def add_trade(
        self, arm: str, ticker: str, action: str, shares: float, price: float, trade_date: date,
        proposed_session: date, pnl_pct: float | None,
    ) -> None:
        self._require_transaction()
        self._db.execute(
            "INSERT INTO shadow_trades (arm, ticker, action, shares, price, trade_date, proposed_session, pnl_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (arm, ticker, action, shares, price, trade_date.isoformat(), proposed_session.isoformat(), pnl_pct),
        )

    def trades(self, arm: str) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM shadow_trades WHERE arm = ? ORDER BY id", (arm,)).fetchall()

    def unreported_trades(self, arm: str) -> list[sqlite3.Row]:
        """Fills that no finished run has listed yet."""
        return self._db.execute(
            "SELECT * FROM shadow_trades WHERE arm = ? AND reported_run_id IS NULL ORDER BY id", (arm,)
        ).fetchall()

    def mark_trades_reported(self, arm: str, run_id: str) -> None:
        self._require_transaction()
        self._db.execute(
            "UPDATE shadow_trades SET reported_run_id = ? WHERE arm = ? AND reported_run_id IS NULL", (run_id, arm)
        )

    def book_tickers(self, arms: list[str]) -> list[str]:
        """Every ticker any of these arms holds or has an order waiting on."""
        if not arms:
            return []
        marks = ",".join("?" * len(arms))
        rows = self._db.execute(
            f"SELECT ticker FROM shadow_positions WHERE arm IN ({marks}) "
            f"UNION SELECT ticker FROM shadow_pending WHERE arm IN ({marks}) ORDER BY ticker",
            (*arms, *arms),
        )
        return [row["ticker"] for row in rows]
