"""The forecast engine by hand. It places and changes no order.

    python -m forecast estimate   estimate the coming session's parameters now
    python -m forecast status     the parameters on disk, and the forecasts each group has on record
    python -m forecast verify     work each group's latest forecast out again and compare it with the record

The parameters are estimated by themselves at the first close meeting of
each week, on data up to the last session of the week before. `estimate` does
the same ahead of time, on a weekend say, so that Monday's meeting finds them
ready. It is the only command here that fetches anything.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
from data.market_calendar import last_completed_session, next_trading_day
from forecast.params import ForecastError, ParamStore, due_cutoff, for_session
from forecast.scoring import card, in_words
from journal.store import Journal, JournalError


def _journal() -> Journal | None:
    """The journal, or None when there is none: opening one creates it, and a mistyped path should not."""
    if not Path(config.JOURNAL_PATH).is_file():
        return None
    return Journal()


def estimate(store: ParamStore, provider, now: datetime, out=print) -> int:
    from pipeline import universe as universe_rules

    coming = next_trading_day(last_completed_session(now))
    journal = _journal()
    book = journal.book_tickers(journal.arms()) if journal else []
    tickers = sorted(set(universe_rules.load_universe()) | set(book))
    params, new, trouble = for_session(store, provider, tickers, coming, now=now)
    if trouble:
        out(trouble)
    if params is None:
        return 1
    model = params.regime
    out(("Estimated" if new else "Already on disk") + f": parameters version {params.version}, on data to "
        f"{params.cutoff}, for the session of {coming}. {len(params.series)} name(s) and sector fund(s).")
    out("  The index: " + ", ".join(f"{vol * 100:.2f}% a day when {name}" for name, vol in zip(model.names, model.vols))
        + f"; on {params.cutoff} it was " + ", ".join(f"{name} {share:.2f}" for name, share in zip(model.names, params.state_at_cutoff)) + ".")
    for problem in params.problems:
        out(f"  note: {problem}")
    return 1 if trouble else 0


def status(store: ParamStore, now: datetime, out=print) -> int:
    versions = store.versions()
    wanted = due_cutoff(next_trading_day(last_completed_session(now)))
    if not versions:
        out(f"No parameters on disk ({store.directory}). The coming session needs an estimate on data to {wanted}.")
    for version in versions:
        params = store.load(version)
        if params is None:
            out(f"Version {version}: the file cannot be read.")
            continue
        out(f"Version {params.version}: data to {params.cutoff}, {len(params.series)} name(s), {params.regime.states} "
            f"state(s), {len(params.problems)} note(s)." + ("  <- the coming session's" if params.cutoff == wanted else ""))
    journal = _journal()
    for arm in (journal.arms() if journal else []):
        sets = journal.forecast_sets(arm)
        if not sets:
            continue
        summary = card(arm, journal.forecast_scores(arm))
        out(f"\n{arm}: forecasts on {len(sets)} session(s), the last for {sets[-1].session_date} "
            f"(parameters version {sets[-1].params_version}). {summary.scored} reconciled, {summary.pending} not yet "
            f"due, {summary.unpriced} without a price.")
        for item in summary.by_horizon:
            out(f"  {in_words(item)}")
    return 0


def verify(store: ParamStore, arms: list[str], out=print) -> int:
    from pipeline.quant import recompute

    journal = _journal()
    if journal is None:
        out(f"No journal at {config.JOURNAL_PATH}: there is nothing to verify.")
        return 0
    failed = 0
    for arm in arms or journal.arms():
        sets = journal.forecast_sets(arm)
        if not sets:
            continue
        recorded = sets[-1]
        try:
            again = recompute(journal, recorded.run_id, store)
        except ForecastError as e:
            out(f"{arm}, {recorded.session_date}: cannot be worked out again. {e}")
            failed += 1
            continue
        same = again.model_dump_json() == recorded.model_dump_json()
        out(f"{arm}, {recorded.session_date}: worked out again from the snapshot, parameters version "
            f"{recorded.params_version} and seed {recorded.seed}: "
            + ("the same, digit for digit." if same else "NOT the same as the record."))
        failed += 0 if same else 1
    return 1 if failed else 0


def main(argv: list[str] | None = None, provider=None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="The forecast engine by hand. It places and changes no order.")
    parser.add_argument("command", choices=["estimate", "status", "verify"])
    parser.add_argument("--arm", action="append", default=[], metavar="NAME", help="For verify: a group as the journal names it")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    store = ParamStore()
    try:
        if args.command == "status":
            return status(store, now)
        if args.command == "verify":
            return verify(store, args.arm)
        if provider is None:
            from data.providers import YFinanceProvider

            provider = YFinanceProvider()
        return estimate(store, provider, now)
    except (JournalError, sqlite3.OperationalError, OSError) as e:
        print(f"Cannot go on: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
