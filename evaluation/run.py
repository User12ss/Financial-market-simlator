"""One command that measures what the pipeline's views and trades turned out to be worth.

    python -m evaluation                every model configuration in the journal
    python -m evaluation --lessons      also have each model review a few of its own views
    python -m evaluation --if-changed   only when something has been recorded since the last report

It reads the journal and prices that came after each decision, and writes a
report. It decides nothing: the only things it adds to the journal are the
scores, and lessons when they are asked for. A lesson changes what the
pipeline does only if the pipeline is told to show lessons to the analyst
(PIPELINE_USE_LESSONS), which is off unless you turn it on.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config
from data.market_calendar import last_completed_session, trading_days
from data.providers import DataError, MarketDataProvider, YFinanceProvider
from data.snapshot import HISTORY_DAYS
from evaluation.baselines import account_curve, benchmark_curve, candidate_curve, random_benchmark
from evaluation.lessons import write_lessons
from evaluation.prices import Prices
from evaluation.report import to_markdown
from evaluation.scoring import score_views, scorecard, standing_runs
from evaluation.trades import summarize, trades_of
from data.ledger import Ledger
from forecast import outcomes
from forecast.scoring import card as forecast_card
from forecast.scoring import reconcile
from journal.store import Journal, JournalError
from models.evaluation import ArmEvaluation, Evaluation, RandomBenchmark, Scorecard, TradeSummary
from models.signals import SignalTable, UsageSummary
from utils.llm_client import LLMClient, LLMError

# Prices are fetched from this many days before the first decision: as much
# history as the pipeline's own snapshot holds, so that the ATR a random
# pick's stop is set from is the ATR the pipeline would have had that day.
PRICE_LEAD_DAYS = HISTORY_DAYS


def usage_of(journal: Journal, arm: str) -> UsageSummary:
    """What every run of an arm cost, the failed and the replaced ones included."""
    rows = journal.runs(arm)
    costs = [row["cost_usd"] for row in rows if row["cost_usd"] is not None]
    return UsageSummary(
        calls=sum(row["llm_calls"] or 0 for row in rows),
        input_tokens=sum(row["input_tokens"] or 0 for row in rows),
        output_tokens=sum(row["output_tokens"] or 0 for row in rows),
        model_seconds=sum(row["model_seconds"] or 0.0 for row in rows),
        cost_usd=sum(costs) if costs else None,
    )


def caveats(card: Scorecard, trades: TradeSummary, random: RandomBenchmark) -> list[str]:
    """What the numbers cannot yet say. A report that leaves this out invites reading too much into it."""
    notes = []
    if card.pending:
        notes.append(f"{card.pending} view(s) have not reached the end of their horizon and are not scored yet.")
    if card.unpriced:
        notes.append(f"{card.unpriced} view(s) could not be scored: a price was missing.")
    if card.overall and card.overall.views < 400:
        notes.append(
            f"{card.overall.views} scored view(s) taking a side, written on {card.sessions} session(s). A hit rate "
            f"from that many is uncertain by roughly {card.overall.margin * 100:.0f} points either way, and by more "
            "than that in truth, because views written on the same day rise and fall together."
        )
    if trades.closed < 100:
        notes.append(f"{trades.closed} closed trade(s): far too few to judge the trading by.")
    if random.entries and random.entries < 100:
        notes.append(
            f"The random-pick comparison rests on {random.entries} buy(s). With so few, where the account falls "
            "among the random outcomes is mostly luck."
        )
    return notes


def evaluate(
    journal: Journal, provider: MarketDataProvider, *, now: datetime | None = None, arms: list[str] | None = None,
    simulations: int | None = None, seed: int | None = None, reviewers: dict[str, LLMClient] | None = None,
    rescore: bool = False, benchmark: str | None = None, ledger: Ledger | None = None,
) -> Evaluation:
    """Evaluate every arm (or the ones named). `reviewers` holds a model for each arm that is to write lessons."""
    now = now or datetime.now(timezone.utc)
    as_of = last_completed_session(now)
    benchmark = benchmark or config.EVALUATION_BENCHMARK
    reviewers = reviewers or {}

    work: dict[str, tuple[list, list[SignalTable]]] = {}
    symbols, first = {benchmark}, None
    for arm in arms or journal.arms():
        rows = standing_runs(journal, arm)
        tables = [SignalTable.model_validate_json(row["signals_json"]) for row in rows]
        work[arm] = (rows, tables)
        for table in tables:
            symbols.update(view.ticker for view in table.views)
            symbols.update(table.candidates)
        symbols.update(row["ticker"] for row in journal.rows(
            "SELECT DISTINCT ticker FROM paper_fills WHERE account = ?", (arm,)))
        symbols.update(forecast.ticker for forecasts in journal.forecast_sets(arm) for forecast in forecasts.tickers)
        symbols.update(table.ticker for tables_ in journal.backward_sets(arm) for table in tables_.measured)
        if tables and (first is None or tables[0].session_date < first):
            first = tables[0].session_date
    prices = Prices.fetch(provider, symbols, first - timedelta(days=PRICE_LEAD_DAYS), as_of) if first else Prices({})
    if first and prices.frame(benchmark) is None:
        # The source does not always fail out loud: it can hand back nothing at all. Going on
        # would mark every view as unpriced and, with rescore, write that over the scores on record.
        raise DataError(f"The price source returned nothing for {benchmark}, which everything is measured against.")

    result = Evaluation(as_of=as_of, created_at=now, benchmark=benchmark)
    for arm, (rows, tables) in work.items():
        scores = score_views(
            journal, arm, prices=prices, benchmark=benchmark, as_of=as_of,
            earlier=[] if rescore else journal.view_scores(arm),
        )
        journal.save_view_scores(arm, scores)
        card = scorecard(arm, scores)
        # Every ticker the account traded was a candidate or a holding first, so it has a view.
        sectors = {score.ticker: score.sector for score in scores if score.sector}
        trades = trades_of(journal, arm, sectors)
        summary = summarize(arm, trades)

        curve, initial = account_curve(journal, arm)
        curves = [curve]
        books_at = curve.points[-1].session_date if curve.points else None
        if initial is not None and curve.points:
            days = trading_days(curve.points[0].session_date, books_at)
            curves += [benchmark_curve(prices, benchmark, days, initial), candidate_curve(tables, prices, days, initial)]
        random = random_benchmark(tables, trades, prices, as_of=as_of, valued_on=books_at, sectors=sectors,
                                  simulations=simulations, seed=seed)

        notes = caveats(card, summary, random)
        # Forecasts are reconciled at each close meeting from that day's snapshot, which holds only the
        # names under discussion that day. Here every name's prices are at hand, so none stays unpriced.
        forecasts = None
        if journal.forecast_sets(arm):
            forecasts = forecast_card(arm, reconcile(journal, arm, prices.frame, as_of)[0])
            if 0 < forecasts.scored < 400:
                days = max(item.sessions for item in forecasts.by_horizon)
                notes.append(
                    f"{forecasts.scored} forecast(s) reconciled, made on {days} day(s). Forecasts made on the same "
                    "day rise and fall together, so they say less than their number suggests."
                )
        # The trades worked back, and the events proposed for them. A proposed event checked by a headline is
        # judged from the ledger, where there is one: without it such an event stays beyond judging.
        worked_back = None
        if journal.backward_sets(arm):
            endings, events, _ = outcomes.reconcile(
                journal, arm, prices.frame, ledger.read_between if ledger is not None else None, as_of)
            worked_back = {
                **outcomes.tally(endings, events),
                "endings": {status: sum(1 for score in endings if score.status == status)
                            for status in ("scored", "pending", "unpriced")},
            }
        written = []
        if arm in reviewers:
            written, problems = write_lessons(
                journal, arm, reviewers[arm], scores=scores, tables=tables, trades=trades, benchmark=benchmark,
                limit=config.EVALUATION_MAX_LESSONS, now=now,
            )
            notes += problems

        result.arms.append(ArmEvaluation(
            arm=arm,
            first_session=tables[0].session_date if tables else None,
            last_session=tables[-1].session_date if tables else None,
            sessions_run=len(tables), curves=curves, scorecard=card, trades=summary, random=random,
            forecasts=forecasts, worked_back=worked_back,
            usage=usage_of(journal, arm), lessons_written=len(written), lessons_total=len(journal.lessons(arm)),
            notes=notes,
        ))
    return result


def save(evaluation: Evaluation, directory: str | Path | None = None) -> tuple[Path, Path]:
    folder = Path(directory or config.EVALUATION_DIR) / evaluation.as_of.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    markdown, data = folder / "evaluation.md", folder / "evaluation.json"
    markdown.write_text(to_markdown(evaluation), encoding="utf-8")
    data.write_text(evaluation.model_dump_json(indent=2), encoding="utf-8")
    return markdown, data


def _moment(text: str) -> datetime:
    """A recorded time as a moment. The journal keeps UTC, and a time with no zone beside it is the journal's."""
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def up_to_date(journal: Journal, arms: list[str], as_of: date, directory: str | Path | None = None) -> bool:
    """Whether the report on disk for this session already says everything there is to say.

    It does when it holds every arm asked for and was written after the last run and the last meeting on
    record ended: nothing has been recorded since that it could have left out. A session that has closed
    since has no report of its own yet, so the answer for it is no without looking further. A report that
    cannot be read is no report, and an ending that cannot be read as a time is not guessed at: both mean
    measuring again, which costs a minute and can do no harm.
    """
    report = Path(directory or config.EVALUATION_DIR) / as_of.isoformat() / "evaluation.json"
    try:
        held = json.loads(report.read_text(encoding="utf-8"))
        written = _moment(held["created_at"])
        if not set(arms) <= {arm["arm"] for arm in held["arms"]}:
            return False
        endings = [row[0] for table in ("runs", "meetings")
                   for row in journal.rows(f"SELECT finished_at FROM {table} WHERE finished_at IS NOT NULL")]
        return all(_moment(text) < written for text in endings)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def reviewer_for(arm: str) -> LLMClient:
    """The model behind an arm's name, which is `provider/model`."""
    provider, _, model = arm.partition("/")
    return LLMClient(provider=provider, model=model or None)


def _at_least_one(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("has to be 1 or more")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure what the daily pipeline's views and trades turned out to be worth. Decides nothing.")
    parser.add_argument("--arm", action="append", default=[], metavar="NAME",
                        help="A model configuration as the journal names it. Default: all of them.")
    parser.add_argument("--simulations", type=_at_least_one, default=None,
                        help="Random-pick simulations, 1 or more (EVALUATION_SIMULATIONS)")
    parser.add_argument("--seed", type=int, default=None, help="Seed for the random picks (EVALUATION_SEED)")
    parser.add_argument("--lessons", action="store_true",
                        help="Have each model review a few of its own views against the real outcome. Uses the model.")
    parser.add_argument("--rescore", action="store_true", help="Score every view again, the already scored ones included")
    parser.add_argument("--out", default=None, help="Where to write the report (EVALUATION_DIR)")
    parser.add_argument("--if-changed", action="store_true",
                        help="Do nothing when the report on disk was written after the last run and the last meeting "
                             "on record ended, and no session has closed since. What the scheduled run asks for.")
    args = parser.parse_args(argv)

    # Opening a journal creates it. A mistyped path should not leave an empty one behind.
    if not Path(config.JOURNAL_PATH).is_file():
        print(f"No journal at {config.JOURNAL_PATH}: there is nothing to evaluate.")
        return 0
    try:
        journal = Journal()
    except (JournalError, sqlite3.OperationalError) as e:
        print(f"Cannot open the journal: {e}", file=sys.stderr)
        return 1
    known = journal.arms()
    if not known:
        print("No runs recorded yet: there is nothing to evaluate.")
        return 0
    unknown = [arm for arm in args.arm if arm not in known]
    if unknown:
        print(f"Not in the journal: {', '.join(unknown)}. It holds: {', '.join(known)}.", file=sys.stderr)
        return 1
    arms = args.arm or known

    # One reading of the clock for both: what is found up to date and what is measured are the same session.
    now = datetime.now(timezone.utc)
    if args.if_changed and up_to_date(journal, arms, last_completed_session(now), args.out):
        print(f"The evaluation of {last_completed_session(now)} is up to date: nothing has been recorded since "
              "it was written.")
        return 0

    reviewers = {}
    if args.lessons:
        for arm in arms:
            try:
                reviewers[arm] = reviewer_for(arm)
            except LLMError as e:
                print(f"{arm}: no lessons will be written. {e}", file=sys.stderr)

    # The ledger is the desk's. An evaluation of journals that never had a desk has none, and needs none.
    ledger = Ledger(read_only=True) if Path(config.LEDGER_PATH).is_file() else None
    try:
        evaluation = evaluate(journal, YFinanceProvider(), now=now, arms=arms, simulations=args.simulations,
                              seed=args.seed, reviewers=reviewers, rescore=args.rescore, ledger=ledger)
    except DataError as e:
        print(f"Cannot evaluate: {e}", file=sys.stderr)
        return 1
    except (JournalError, sqlite3.OperationalError) as e:
        print(f"Cannot evaluate: the journal could not be written to. {e}", file=sys.stderr)
        return 1
    markdown, data = save(evaluation, args.out)

    print(f"Evaluation as of the close of {evaluation.as_of}, against {evaluation.benchmark}.")
    for arm in evaluation.arms:
        card, overall = arm.scorecard, arm.scorecard.overall
        print(f"\n{arm.arm}: {arm.sessions_run} session(s) run")
        print(f"  views: {card.scored} scored, {card.pending} not yet due, {card.unpriced} without a price")
        if overall:
            print(f"  direction right {overall.hit_rate * 100:.0f}%, beat {evaluation.benchmark} "
                  f"{overall.beat_rate * 100:.0f}%, of {overall.views} view(s) taking a side")
        print(f"  trades: {arm.trades.closed} closed, {arm.trades.open} open, "
              f"realized {arm.trades.realized_usd:+,.2f}, unrealized {arm.trades.unrealized_usd:+,.2f}")
        if arm.random.percentile is not None:
            print(f"  its picks against random picks from the same candidates: better than "
                  f"{arm.random.percentile:.0f}% of {arm.random.simulations} outcomes, over {arm.random.entries} buy(s)")
        if arm.forecasts is not None:
            print(f"  forecasts: {arm.forecasts.scored} reconciled, {arm.forecasts.pending} not yet due, "
                  f"{arm.forecasts.unpriced} without a price")
        if arm.worked_back is not None:
            counts, proposed = arm.worked_back["endings"], arm.worked_back["events"]
            print(f"  trades worked back: {counts['scored']} ending(s) reconciled, {counts['pending']} not yet due, "
                  f"{counts['unpriced']} without a price; {proposed['scored']} of {proposed['proposed']} proposed "
                  f"event(s) judged, {proposed['unread']} with no reading of the headlines to judge by")
        if arm.lessons_written:
            print(f"  {arm.lessons_written} lesson(s) written")
    print(f"\n  {markdown}\n  {data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
