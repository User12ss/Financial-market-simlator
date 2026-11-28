"""The information ledger: what the desk has seen, and when it first saw it.

One SQLite file, apart from the decision journal, that is only ever added to.
Headlines, filings with the SEC and prices go in as they are fetched, each
with the moment this system first had it. A meeting is then shown only what
the ledger held at the meeting's cut-off: something fetched later cannot
reach it, whatever date its source put on it. "First seen" is the rule, not
"published", because a story is not known here before it has been fetched.

The ledger also keeps a line for every fetch, the failed ones included, and a
line for every round of the watch. A fetch that failed is recorded as failed:
nothing is put in its place.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

import config
from data.market_calendar import previous_trading_day, session_close
from data.models import Filing, NewsItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT NOT NULL,           -- one story is one item, however often it is fetched
    ticker       TEXT NOT NULL,           -- the symbol it was fetched for
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL,           -- headline
    published_at TEXT,                    -- as the source states it; NULL when it states none
    first_seen   TEXT NOT NULL,           -- when this system first had it
    title        TEXT NOT NULL,
    publisher    TEXT NOT NULL,
    link         TEXT NOT NULL,
    PRIMARY KEY (item_id, ticker)
);
CREATE INDEX IF NOT EXISTS items_by_ticker ON items (ticker, first_seen);

CREATE TABLE IF NOT EXISTS filings (
    accession   TEXT NOT NULL,            -- the SEC's own number for the filing
    ticker      TEXT NOT NULL,            -- the company it is about
    form        TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- event, insider, holder
    accepted_at TEXT NOT NULL,            -- when the SEC accepted it: public from then on
    first_seen  TEXT NOT NULL,            -- when this system first had it
    title       TEXT NOT NULL,
    excerpt     TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    link        TEXT NOT NULL,
    PRIMARY KEY (accession, ticker)
);
CREATE INDEX IF NOT EXISTS filings_by_ticker ON filings (ticker, first_seen);

CREATE TABLE IF NOT EXISTS quotes (
    ticker         TEXT NOT NULL,
    seen_at        TEXT NOT NULL,
    session        TEXT NOT NULL,         -- the trading day the price belongs to
    last           REAL NOT NULL,
    previous_close REAL NOT NULL,         -- from the same download, so on the same share scale
    PRIMARY KEY (ticker, seen_at)
);

CREATE TABLE IF NOT EXISTS fetches (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    what   TEXT NOT NULL,                 -- headlines, quotes
    ticker TEXT NOT NULL,                 -- "" for a fetch that covers several
    ok     INTEGER NOT NULL,
    found  INTEGER NOT NULL,
    added  INTEGER NOT NULL,
    error  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    arm          TEXT NOT NULL,
    day          TEXT NOT NULL,           -- the trading day the round belongs to
    summary_json TEXT NOT NULL
);
"""


def stamp(moment: datetime) -> str:
    """A moment as text that sorts the way time runs: UTC, always with microseconds."""
    if moment.tzinfo is None:
        raise ValueError("A moment in the ledger must carry a time zone.")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def item_id(source: str, headline: NewsItem) -> str:
    """The same story gets the same id each time it is fetched: by its link, or by its wording when it has none."""
    title = " ".join(headline.title.split())
    key = headline.link.strip() or f"{headline.publisher.strip()}|{title}"
    return hashlib.sha256(f"{source}|{key}".encode()).hexdigest()[:16]


# How many headlines one reading asks the source for (data.snapshot.HEADLINES_PER_TICKER; a test holds the two
# together). A reading that fills the page with headlines the ledger had never seen may have missed older ones.
PAGE = 5
# A reading this long after a session's close is still that evening's: time enough for the close meeting, which is
# held from two hours after the close, and not enough to reach the next morning.
EVENING_HOURS = 8


class LedgerItem(BaseModel):
    item_id: str
    ticker: str
    source: str
    kind: str = "headline"
    published_at: datetime | None = None
    first_seen: datetime
    title: str
    publisher: str = ""
    link: str = ""


class LedgerFiling(BaseModel):
    """A filing with the SEC as the ledger holds it: what was kept of it, and when this system first had it."""

    accession: str
    ticker: str
    form: str
    kind: str
    accepted_at: datetime
    first_seen: datetime
    title: str
    excerpt: str = ""
    detail: dict = Field(default_factory=dict)
    link: str = ""


class LiveQuote(BaseModel):
    """A price during the session. The source may run behind the market."""

    ticker: str
    seen_at: datetime
    session: date
    last: float
    previous_close: float

    @property
    def change_pct(self) -> float:
        return (self.last / self.previous_close - 1) * 100


class FetchFailure(BaseModel):
    at: datetime
    what: str
    ticker: str = Field(description="Empty for a fetch that covers several tickers")
    error: str


class Ledger:
    def __init__(self, path: str | Path | None = None, read_only: bool = False, timeout: float = 5.0) -> None:
        self.path = Path(path or config.LEDGER_PATH)
        if read_only:
            # Raises sqlite3.OperationalError when the file does not exist: a reader never creates one.
            self._db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, timeout=timeout)
            self._db.executescript(_SCHEMA)
            self._db.commit()
            # Someone reading the ledger (the dashboard) never blocks the watch from saving.
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.row_factory = sqlite3.Row

    def close(self) -> None:
        self._db.close()

    # ── Headlines ────────────────────────────────────────────────────────

    def add_headlines(self, ticker: str, headlines: list[NewsItem], seen_at: datetime, source: str = "yahoo") -> int:
        """Keep the headlines not already held for this ticker. Returns how many were new.

        One that is already held keeps the moment it was first seen: fetching
        it again does not make it newer.
        """
        added = 0
        with self._db:
            for headline in headlines:
                title = " ".join(headline.title.split())
                if not title:
                    continue
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO items VALUES (?, ?, ?, 'headline', ?, ?, ?, ?, ?)",
                    (item_id(source, headline), ticker, source,
                     stamp(headline.published) if headline.published else None, stamp(seen_at), title,
                     headline.publisher.strip(), headline.link.strip()),
                )
                added += cursor.rowcount
        return added

    def items(
        self, ticker: str, as_of: datetime, days: int, since: datetime | None = None,
    ) -> list[LedgerItem]:
        """What was known about a ticker at `as_of`, newest first.

        Only what this system had first seen by `as_of`, and that its source
        does not date after it. `days` is how far back to look, by the
        source's date where it gives one. With `since`, only what was first
        seen after that moment: what is new since an earlier cut-off.
        """
        sql = (
            "SELECT * FROM items WHERE ticker = ? AND first_seen <= ? "
            "AND (published_at IS NULL OR published_at <= ?) AND COALESCE(published_at, first_seen) >= ?"
        )
        params: list = [ticker, stamp(as_of), stamp(as_of), stamp(as_of - timedelta(days=days))]
        if since is not None:
            sql += " AND first_seen > ?"
            params.append(stamp(since))
        rows = self._db.execute(sql + " ORDER BY COALESCE(published_at, first_seen) DESC, item_id", params)
        return [self._item(row) for row in rows]

    @staticmethod
    def _item(row: sqlite3.Row) -> LedgerItem:
        return LedgerItem(
            item_id=row["item_id"], ticker=row["ticker"], source=row["source"], kind=row["kind"],
            published_at=_moment(row["published_at"]), first_seen=_moment(row["first_seen"]), title=row["title"],
            publisher=row["publisher"], link=row["link"],
        )

    # ── Filings with the SEC ─────────────────────────────────────────────

    def add_filings(self, ticker: str, filings: list[Filing], seen_at: datetime) -> int:
        """Keep the filings not already held for this ticker. Returns how many were new.

        One already held keeps the moment it was first seen, as a headline does.
        """
        added = 0
        with self._db:
            for filing in filings:
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO filings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (filing.accession, ticker, filing.form, filing.kind, stamp(filing.accepted), stamp(seen_at),
                     filing.title, filing.excerpt, json.dumps(filing.detail, sort_keys=True), filing.link),
                )
                added += cursor.rowcount
        return added

    def filings(
        self, ticker: str, as_of: datetime, days: dict[str, int], since: datetime | None = None,
        fresh_days: int | None = None,
    ) -> list[LedgerFiling]:
        """The filings about a ticker that were known at `as_of`, the latest accepted first.

        Only what this system had first seen by `as_of` and the SEC had
        accepted by then. `days` is how far back to look, by kind of filing.
        With `since`, only what was first seen after that moment. With
        `fresh_days` as well, only what the SEC had accepted no longer than
        that before it was first seen: a first reading goes months back, and
        what was on file all that time is not news.

        A ledger made before filings were read has no table of them. For a
        reader that cannot add one (the dashboard), it holds none.
        """
        sql = "SELECT * FROM filings WHERE ticker = ? AND first_seen <= ? AND accepted_at <= ?"
        params: list = [ticker, stamp(as_of), stamp(as_of)]
        if since is not None:
            sql += " AND first_seen > ?"
            params.append(stamp(since))
        try:
            rows = self._db.execute(sql + " ORDER BY accepted_at DESC, accession", params).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise
        found = []
        for row in rows:
            accepted, first_seen = _moment(row["accepted_at"]), _moment(row["first_seen"])
            if accepted < as_of - timedelta(days=days.get(row["kind"], 0)):
                continue
            if fresh_days is not None and first_seen - accepted > timedelta(days=fresh_days):
                continue
            found.append(LedgerFiling(
                accession=row["accession"], ticker=row["ticker"], form=row["form"], kind=row["kind"], accepted_at=accepted,
                first_seen=first_seen, title=row["title"], excerpt=row["excerpt"], detail=json.loads(row["detail_json"]),
                link=row["link"],
            ))
        return found

    def filing_accessions(self, ticker: str) -> set[str]:
        """The filings held for a ticker, by the SEC's number: a reading does not open these again."""
        try:
            return {row["accession"] for row in self._db.execute("SELECT accession FROM filings WHERE ticker = ?", (ticker,))}
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return set()
            raise

    # ── Prices during the session ────────────────────────────────────────

    def add_quote(self, quote: LiveQuote) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?, ?)",
                (quote.ticker, stamp(quote.seen_at), quote.session.isoformat(), quote.last, quote.previous_close),
            )

    def quote(self, ticker: str, as_of: datetime, session: date) -> LiveQuote | None:
        """The last price fetched for that trading day by `as_of`, if any."""
        row = self._db.execute(
            "SELECT * FROM quotes WHERE ticker = ? AND session = ? AND seen_at <= ? ORDER BY seen_at DESC LIMIT 1",
            (ticker, session.isoformat(), stamp(as_of)),
        ).fetchone()
        if row is None:
            return None
        return LiveQuote(
            ticker=row["ticker"], seen_at=_moment(row["seen_at"]), session=date.fromisoformat(row["session"]),
            last=row["last"], previous_close=row["previous_close"],
        )

    def read_between(
        self, ticker: str, first: date, last: date, after: datetime | None = None,
    ) -> tuple[list[str], bool]:
        """The headlines about a ticker that belong to the sessions from `first` to `last`, and whether the
        ledger's readings covered those days without a gap.

        A headline its source dates belongs to those days when the date lies from the close before `first` to
        the close of `last`. One its source does not date belongs to them when the ledger first saw it in that
        stretch, or at the first reading after it: a name the watch does not read is read by each evening's
        close meeting, after the close. With `after` (the cut-off of the meeting that asks) only what the ledger
        first saw later counts: what that meeting could have been shown is not news to it. The source's date
        is not held against the cut-off. A story dated an hour before the meeting that the ledger only came to
        see the next day was not known at the meeting.

        "Covered" is what allows saying that something did NOT appear. It takes a reading at or after the
        close of `last`, and no gap before it. A reading asks for the latest few headlines (`PAGE`). If it
        brings back a full page and the ledger had seen none of them, older ones may have gone by unseen: a
        gap. If it brings back fewer, or one the ledger already held, nothing between that one and now was
        missed, as long as the source hands out the latest first. A reading that brought nothing back, or
        that failed, is no reading.

        One more thing undoes it. The reading that closes the days is meant to be that evening's. When it comes
        more than `EVENING_HOURS` after the last close (the wire was down that evening, or no meeting was held),
        a headline without a date that it is the first to see may be of a later day, and nobody can say. It is
        still returned, and the days are not covered.
        """
        start, end = session_close(previous_trading_day(first)), session_close(last)
        if after is not None and after >= end:
            return [], False                              # asked once the days were over: nothing in them is news
        low, later = (after, ">") if after is not None and after >= start else (start, ">=")
        readings = self._db.execute(
            "SELECT at, found, added FROM fetches WHERE what IN ('headlines', 'headlines_snapshot') AND ticker = ? "
            f"AND ok = 1 AND found > 0 AND at {later} ? ORDER BY at, id", (ticker, stamp(low)),
        ).fetchall()
        closing = next((row for row in readings if _moment(row["at"]) >= end), None)
        covered = closing is not None and all(
            row["found"] < PAGE or row["added"] < row["found"] for row in readings if row["at"] <= closing["at"]
        )
        upto = _moment(closing["at"]) if closing is not None else end
        late = upto > end + timedelta(hours=EVENING_HOURS)
        rows = self._db.execute(
            f"SELECT title, published_at, first_seen FROM items WHERE ticker = ? AND first_seen {later} ? "
            "ORDER BY first_seen, item_id", (ticker, stamp(low)),
        ).fetchall()
        titles = []
        for row in rows:
            published, first_seen = _moment(row["published_at"]), _moment(row["first_seen"])
            if published is not None:
                belongs = start <= published <= end
            else:
                belongs = first_seen <= upto
                if belongs and late and first_seen > end:
                    covered = False                       # of those days or of a later one: there is no telling
            if belongs:
                titles.append(row["title"])
        return titles, covered

    # ── Fetches ──────────────────────────────────────────────────────────

    def log_fetch(
        self, at: datetime, what: str, ticker: str = "", ok: bool = True, found: int = 0, added: int = 0,
        error: str = "",
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO fetches (at, what, ticker, ok, found, added, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (stamp(at), what, ticker, int(ok), found, added, error),
            )

    def note_read(self, ticker: str, at: datetime, found: int, added: int) -> None:
        """Record that a ticker's headlines were read at `at` by a close meeting's snapshot. Once: the same
        reading noted again (a meeting held twice on one snapshot) adds nothing.

        It is logged under a name of its own, so that what the watch's own
        fetches did (`failures`) reads exactly as it did before there was
        such a note.
        """
        with self._db:
            seen = self._db.execute(
                "SELECT 1 FROM fetches WHERE what = 'headlines_snapshot' AND ticker = ? AND at = ? LIMIT 1",
                (ticker, stamp(at)),
            ).fetchone()
            if seen is None:
                self._db.execute(
                    "INSERT INTO fetches (at, what, ticker, ok, found, added, error) "
                    "VALUES (?, 'headlines_snapshot', ?, 1, ?, ?, '')", (stamp(at), ticker, found, added),
                )

    def failures(self, as_of: datetime, tickers: list[str]) -> list[FetchFailure]:
        """Fetches whose latest try by `as_of` failed, for these tickers and for the fetches that cover several.

        A fetch that failed and then worked is not listed: only what is still missing.
        """
        failed = []
        wanted = [("quotes", "")] + [("headlines", ticker) for ticker in tickers] + [("filings", ticker) for ticker in tickers]
        for what, ticker in wanted:
            row = self._db.execute(
                "SELECT * FROM fetches WHERE what = ? AND ticker = ? AND at <= ? ORDER BY at DESC, id DESC LIMIT 1",
                (what, ticker, stamp(as_of)),
            ).fetchone()
            if row is not None and not row["ok"]:
                failed.append(FetchFailure(at=_moment(row["at"]), what=what, ticker=ticker, error=row["error"]))
        return failed

    # ── The watch ────────────────────────────────────────────────────────

    def add_watch(self, at: datetime, arm: str, day: date, summary: dict) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO watches (at, arm, day, summary_json) VALUES (?, ?, ?, ?)",
                (stamp(at), arm, day.isoformat(), json.dumps(summary)),
            )

    def watches(self, arm: str, day: date | None = None, limit: int = 200, until: datetime | None = None) -> list[dict]:
        """Rounds of the watch, newest first: each one's summary, with `at` and `day` added.

        With `until`, only the rounds made by that moment.
        """
        sql, params = "SELECT * FROM watches WHERE arm = ?", [arm]
        if day is not None:
            sql, params = sql + " AND day = ?", params + [day.isoformat()]
        if until is not None:
            sql, params = sql + " AND at <= ?", params + [stamp(until)]
        rows = self._db.execute(sql + " ORDER BY at DESC, id DESC LIMIT ?", (*params, limit))
        return [{**json.loads(row["summary_json"]), "at": row["at"], "day": row["day"]} for row in rows]
