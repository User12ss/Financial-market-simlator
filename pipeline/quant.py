"""The quant engineer's seat at the desk's close meeting. It is code speaking: no model is called.

After the analyst has given its views, this seat says what the forecast
engine makes of each name under discussion and of the book, keeps what it
said, and sets the forecasts that came due against what happened.

Nothing it says is shown to a model and nothing it says changes an order.
Until its forecasts have a record, they are there to be read and to be
checked.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

import config
from data.market_calendar import trading_day_after
from data.models import Bars, Snapshot
from forecast import backward, engine, outcomes
from forecast.engine import Book, Subject
from forecast.params import MARKET, ParamStore, Params, for_session
from forecast.scoring import card, frame_of, in_words, reconcile, score_one
from forecast.weights import lean, track
from journal.store import Journal
from models.forecast import Backward, ForecastSet, TickerForecast
from models.planning import BackwardSet, ProposedEvent
from models.signals import TickerView
from models.trading import Account, Position
from pipeline.scenario import for_engine
from portfolio.sizing import usable_stop

QUANT = ("quant_engineer", "Quant Engineer")
STAGE = "forecast"
# The horizon spoken of when a name has no view to take one from.
DEFAULT_HORIZON = 10
# How many of a table's most telling events the seat names aloud. The rest are in what it keeps.
EVENTS_SAID = 2
_WHOSE = {"position": "on the books", "analyst": "the analyst's", "rule": "the sizing rule's"}


@dataclass
class Line:
    """One thing the seat says."""

    kind: str
    remark: str
    payload: dict = field(default_factory=dict)
    ticker: str | None = None


@dataclass
class Setup:
    """What drawing a meeting's paths a second time needs: the rehearsal runs the same simulation with events added."""

    params: Params
    session: date
    market: pd.Series
    funds: dict[str, pd.Series]
    names: dict[str, Subject]
    seed: int


@dataclass
class Turn:
    """The seat's turn at one meeting: what it says, and what to keep if the meeting finishes."""

    lines: list[Line]
    forecasts: ForecastSet | None = None
    backward: BackwardSet | None = None
    setup: Setup | None = None


def seed_of(run_id: str) -> int:
    """The meeting's seed: fixed by the meeting's id, so its forecasts can be worked out again."""
    return int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16)


def returns_of(bars: Bars) -> pd.Series:
    """Daily log returns of the dividend-adjusted close, by session."""
    close = pd.Series(bars.adj_close, index=bars.dates, dtype=float)
    close = close[np.isfinite(close) & (close > 0)]
    return np.log(close).diff().dropna()


def stop_of(close: float, atr: float | None, view: TickerView | None, position: Position | None) -> tuple[float | None, str]:
    """The stop a forecast is asked about, and whose it is.

    For a holding, the one on the books. For a name not held, the stop a buy
    would be given today: the analyst's when sizing would accept it, else the
    sizing rule's own.
    """
    if position is not None:
        return (position.stop_price, "position") if position.stop_price else (None, "")
    if not atr or atr <= 0:
        return None, ""
    stop, _ = usable_stop(close, atr, view.proposed_stop if view else None)
    if stop is not None:
        return stop, "analyst"
    distance = max(config.SIZING_STOP_ATR_MULTIPLE * atr, close * config.SIZING_MIN_STOP_PCT / 100)
    stop = round(close - distance, 2)
    return (stop, "rule") if 0 < stop < close else (None, "")


def subjects(
    journal: Journal, arm: str, snapshot: Snapshot, positions: dict[str, Position], views: dict[str, TickerView],
) -> tuple[list[Subject], list[str]]:
    """Every name with a view or a position, as the engine needs it, and those the snapshot cannot supply."""
    from evaluation.scoring import confidence_bucket

    session = snapshot.session_date
    scores = journal.view_scores(arm)
    found, missing = [], []
    for ticker in sorted(set(views) | set(positions)):
        item = snapshot.tickers.get(ticker)
        if item is None or not item.usable or item.price is None:
            missing.append(f"{ticker}: the snapshot has no usable price for it.")
            continue
        view, position = views.get(ticker), positions.get(ticker)
        close = item.price.close
        stop, whose = stop_of(close, item.technicals.atr14 if item.technicals else None, view, position)
        tilt, basis = 0.0, ""
        if view is not None and view.direction in ("bullish", "bearish"):
            bucket = confidence_bucket(view.confidence)
            scored, hits = track(scores, bucket, known_by=session)
            tilt = lean(scored, hits)
            basis = (f"{scored} scored view(s) held with confidence {bucket}, {hits} of them beat the benchmark: "
                     + (f"a lean of {tilt:.2f}." if tilt else "no lean yet."))
        elif view is not None:
            basis = "A neutral view leans neither way."
        found.append(Subject(
            ticker=ticker, close=close, returns=returns_of(item.bars), stop=stop, stop_from=whose,
            next_report=item.next_earnings, direction=view.direction if view else "",
            confidence=view.confidence if view else None, view_horizon=view.horizon_days if view else None,
            lean=tilt, lean_basis=basis, shares=position.shares if position else 0,
        ))
    return found, missing


def unpriced(names: list[Subject], held: list[Position]) -> dict[str, float]:
    """The holdings that are not among the names to forecast, each with the value the account carries it at:
    the snapshot had no usable price for them."""
    priced = {subject.ticker for subject in names}
    return {position.ticker: position.value for position in held if position.ticker not in priced}


# ── What it says ─────────────────────────────────────────────────────────────

def _horizon(forecast: TickerForecast):
    wanted = forecast.view_horizon or DEFAULT_HORIZON
    return min(forecast.horizons, key=lambda item: (abs(item.sessions - wanted), item.sessions))


def _sessions(count: int) -> str:
    return "1 session" if count == 1 else f"{count} sessions"


def parameters_line(params: Params) -> Line:
    model = params.regime
    states = ", ".join(f"{vol * 100:.2f}% a day when {name}" for name, vol in zip(model.names, model.vols))
    own = " and ".join(f"{value:.2f}" for value in params.state_vol)
    text = (f"The week's parameters were estimated: version {params.version}, on data to {params.cutoff}, "
            f"for {len(params.series)} name(s) and sector fund(s). The index moves {states}; a name's own moves "
            f"are {own} times their usual size in those states.")
    if params.problems:
        text += f" {len(params.problems)} note(s) on what could not be estimated are kept with them."
    return Line("forecast_parameters", text, {
        "version": params.version, "cutoff": params.cutoff.isoformat(), "names": len(params.series),
        "states": model.names, "regime": model.model_dump(), "state_vol": params.state_vol,
        "market_days": params.market_days, "problems": params.problems,
    })


def state_line(forecasts: ForecastSet) -> Line:
    def spread(shares: list[float]) -> str:
        return ", ".join(f"{name} {share:.2f}" for name, share in zip(forecasts.states, shares))

    text = (f"After today's close the market's state: {spread(forecasts.state_now)}. "
            f"{_sessions(DEFAULT_HORIZON).capitalize()} on: {spread(forecasts.state_ahead[str(DEFAULT_HORIZON)])}. "
            f"Parameters version {forecasts.params_version}, on data to {forecasts.params_cutoff}.")
    return Line("market_state", text, {
        "states": forecasts.states, "now": forecasts.state_now, "ahead": forecasts.state_ahead,
        "params_version": forecasts.params_version, "params_cutoff": forecasts.params_cutoff.isoformat(),
        "seed": forecasts.seed, "paths": forecasts.paths,
    })


def forecast_line(forecast: TickerForecast) -> Line:
    at = _horizon(forecast)
    bands = at.quantiles
    text = (f"{forecast.ticker}, {_sessions(at.sessions)} on: half the simulated paths end between "
            f"{bands['25']:,.2f} and {bands['75']:,.2f}, nine in ten between {bands['5']:,.2f} and {bands['95']:,.2f}.")
    if forecast.stop is not None:
        text += (f" The stop at {forecast.stop:,.2f} ({_WHOSE[forecast.stop_from]}) is touched on the way in "
                 f"{at.stop_touch * 100:.0f}% of them; a notional target at {forecast.target:,.2f} comes first in "
                 f"{at.target_first * 100:.0f}%.")
    if forecast.lean:
        text += (f" The {forecast.direction} view tilts the drift by {abs(forecast.lean):.2f} of the horizon's "
                 "volatility, earned by the record of such views.")
    if forecast.report_on is not None:
        text += (f" An earnings report moves the session of {forecast.report_on}"
                 + (", drawn from past report days." if forecast.report_modelled else ", and is not modelled."))
    # The kept percentiles are for scoring and stay in the forecast on record: a page has no use for them.
    payload = forecast.model_dump(mode="json", exclude={"horizons": {"__all__": {"grid", "naive_grid"}}})
    return Line("forecast", text, payload, ticker=forecast.ticker)


def book_line(forecasts: ForecastSet) -> Line:
    book = forecasts.book
    at = next(item for item in book.horizons if item.sessions == DEFAULT_HORIZON)
    text = (f"The book as it stands, {book.positions} position(s) and no trading after today, "
            f"{_sessions(at.sessions)} on: nine paths in ten leave equity between {at.equity['5']:,.0f} and "
            f"{at.equity['95']:,.0f}. Two or more stops are touched on one day in {at.stops_together * 100:.0f}% "
            f"of paths; the drawdown breaker is touched in {at.drawdown_breaker * 100:.1f}%, the daily loss "
            f"breaker in {at.daily_loss_breaker * 100:.1f}%.")
    if book.notes:
        text += " " + " ".join(book.notes)
    return Line("book_forecast", text, book.model_dump(mode="json"))


def scores_line(arm: str, scores: list, fresh: list) -> Line:
    summary = card(arm, scores)
    text = (f"{len(fresh)} forecast(s) came due and were set against what happened. "
            + " ".join(in_words(item) for item in summary.by_horizon))
    return Line("forecast_scores", text.strip(), {"due_now": len(fresh), "card": summary.model_dump(mode="json")})


def problem_line(text: str) -> Line:
    return Line("forecast_problem", text)


# ── Working back from how a trade ends ───────────────────────────────────────

def horizon_of(subject: Subject) -> int:
    """The trade's horizon in sessions: the view's, held within the simulated days."""
    return max(1, min(subject.view_horizon or DEFAULT_HORIZON, engine.DAYS))


def worked_back(subject: Subject) -> bool:
    """Whether a name is a trade to work back from: a holding, or a name the analyst would own."""
    return subject.shares > 0 or subject.direction == "bullish"


def work_back(
    result: engine.Result, session: date, names: list[Subject], states: list[str],
    labels: dict[str, dict[str, str]] | None = None,
) -> tuple[list[Backward], list[str]]:
    """Each trade's paths read backwards from its three endings, and a sentence for each that cannot be."""
    forecasts = {forecast.ticker: forecast for forecast in result.tickers}
    tables, skipped = [], []
    for subject in sorted(names, key=lambda item: item.ticker):
        forecast = forecasts.get(subject.ticker)
        if forecast is None or not worked_back(subject):
            continue        # not forecast (said already, with the reason), or not a trade anyone is considering
        if forecast.stop is None:
            skipped.append(f"{subject.ticker}: it has no stop, so there is no ending at the stop to work back from.")
            continue
        sessions = horizon_of(subject)
        tables.append(backward.read(
            result.traces[subject.ticker], result.states, result.market, forecast, sessions=sessions,
            matures_on=trading_day_after(session, sessions), state_names=states, state_now=result.state_now,
            labels=(labels or {}).get(subject.ticker),
        ))
    return tables, skipped


def backward_line(table: Backward) -> Line:
    ways = table.ways
    text = (f"{table.ticker}, worked back from {_sessions(table.sessions)} on: the target at {table.target:,.2f} comes "
            f"first on {ways['good'].share * 100:.0f}% of paths, the stop at {table.stop:,.2f} on "
            f"{ways['bad'].share * 100:.0f}%, neither on {ways['middle'].share * 100:.0f}%.")
    shaken = f"{table.shaken_out * 100:.0f}"
    if shaken != "0":                                   # said when it comes to a whole percent
        text += f" On {shaken}% the stop sells and the price still ends above today's close."
    said = [event for event in table.events
            if backward.telling(event) > 0 and event.given["bad"] is not None and event.given["good"] is not None]
    if said:
        text += " What tells the endings apart most: " + "; ".join(
            f"{event.label} (on {event.given['bad'] * 100:.0f}% of the paths that end at the stop, "
            f"{event.given['good'] * 100:.0f}% of those that reach the target)" for event in said[:EVENTS_SAID]
        ) + "."
    if table.proposed:
        text = "With the scenario analyst's events put in, which are judgement and not measurement: " + text
        if table.notes:
            # What was left out, or cut to the largest past jump, is part of what this reading is: said, not only kept.
            text += " " + " ".join(table.notes)
    return Line("rehearsal" if table.proposed else "backward", text, table.model_dump(mode="json"), ticker=table.ticker)


def rehearse(setup: Setup, states: list[str], events: list[ProposedEvent]) -> tuple[list[Backward], list[str]]:
    """The same paths drawn again with the proposed events on them, read backwards, for the names that have any.

    The market, the sector funds and every draw of a name's own are what they
    were in the forecast: each event draws from a stream of its own. A name
    with no event is not run again, since nothing about it would differ.
    """
    by_name: dict[str, list[ProposedEvent]] = {}
    for event in events:
        by_name.setdefault(event.ticker, []).append(event)
    subjects = [
        dataclasses.replace(setup.names[ticker], events=[for_engine(event) for event in group])
        for ticker, group in sorted(by_name.items()) if ticker in setup.names
    ]
    if not subjects:
        return [], []
    result = engine.run(setup.params, setup.session, setup.market, setup.funds, subjects, seed=setup.seed, trace=True)
    labels = {ticker: {event.key: event.label for event in group} for ticker, group in by_name.items()}
    # A name none of whose events could be put on its paths has nothing to rehearse: its table would be the measured one again.
    idle = [f"{ticker}: {labels[ticker].get(key, key)!r} is not simulated, {reason}"
            for ticker, trace in sorted(result.traces.items()) if not trace.events
            for key, reason in trace.left_out.items()]
    tables, skipped = work_back(
        result, setup.session, [subject for subject in subjects if result.traces.get(subject.ticker) is None
                                or result.traces[subject.ticker].events], states, labels,
    )
    return tables, result.problems + idle + skipped


def outcomes_line(scores: list, events: list, fresh: list) -> Line:
    summary = outcomes.tally(scores, events)
    text = (f"{len(fresh)} of the endings and events given chances at earlier meetings came due. "
            + outcomes.in_words(summary)).strip()
    return Line("outcome_scores", text, {"due_now": len(fresh), "tally": summary})


# ── Working a forecast out again ─────────────────────────────────────────────

def recompute(journal: Journal, run_id: str, store: ParamStore | None = None) -> ForecastSet:
    """A meeting's forecast, worked out again from what is on record: its snapshot, its parameters and its seed.

    The result is the recorded one digit for digit, as long as the settings
    the stops and the breakers come from are what they were. The lean given
    to each view is taken from the record: it rested on the scores known at
    the meeting, and more are known now.
    """
    from data.snapshot import load_snapshot

    table = journal.signal_table(run_id)
    row = journal.rows("SELECT snapshot_path FROM runs WHERE run_id = ?", (run_id,))
    if table is None or not row:
        raise engine.ForecastError(f"There is no finished close meeting {run_id} on record.")
    recorded = journal.forecast_set(table.arm, table.session_date)
    if recorded is None or recorded.run_id != run_id:
        raise engine.ForecastError(f"The forecast on record for {table.session_date} was not made by meeting {run_id}.")
    try:
        snapshot = load_snapshot(row[0]["snapshot_path"])
    except (OSError, ValueError, TypeError) as e:
        raise engine.ForecastError(f"The meeting's snapshot cannot be read: {e}") from e
    params = (store or ParamStore()).load(recorded.params_version)
    if params is None:
        raise engine.ForecastError(f"Version {recorded.params_version} of the parameters is not on disk any more.")

    names, missing = subjects(
        journal, table.arm, snapshot, {holding.ticker: holding for holding in table.holdings},
        {view.ticker: view for view in table.views},
    )
    said = {forecast.ticker: forecast for forecast in recorded.tickers}
    for subject in names:
        before = said.get(subject.ticker)
        subject.lean, subject.lean_basis = (abs(before.lean), before.lean_basis) if before else (0.0, "")
    account = table.account
    result = engine.run(
        params, table.session_date, returns_of(snapshot.market_bars[MARKET]),
        {symbol: returns_of(bars) for symbol, bars in snapshot.market_bars.items() if symbol != MARKET},
        names, seed=recorded.seed, paths=recorded.paths,
        book=Book(cash=account.cash, equity=account.equity, high_water=account.high_water,
                  unpriced=unpriced(names, table.holdings)),
    )
    return recorded.model_copy(update={
        "state_now": result.state_now, "state_ahead": result.state_ahead, "tickers": result.tickers,
        "book": result.book, "problems": missing + result.problems,
    })


# ── The turn ─────────────────────────────────────────────────────────────────

def waiting(forecasts: ForecastSet) -> list:
    """A row for each forecast of a set, waiting for its horizon to end."""
    return [
        score_one(forecasts.arm, forecasts.session_date, forecast, horizon, None, forecasts.session_date)
        for forecast in forecasts.tickers for horizon in forecast.horizons
    ]


def ready(forecasts: ForecastSet, tables: BackwardSet | None = None) -> tuple:
    """A forecast, what was worked back from it, and the rows that wait for both, made and proved writable at the seat's own turn.

    Everything that could go wrong short of the journal itself (making the
    rows, turning them and the forecast into text) is done here, where a
    failure costs the meeting one line. What is left for `keep` is the
    journal's own writing.
    """
    rows = waiting(forecasts)
    endings, events = outcomes.pending(tables) if tables is not None else ([], [])
    for item in (forecasts, *rows, *([tables] if tables is not None else []), *endings, *events):
        item.model_dump_json()
    return forecasts, rows, tables, endings, events


def keep(
    journal: Journal, forecasts: ForecastSet, rows: list, tables: BackwardSet | None = None,
    endings: list | None = None, events: list | None = None,
) -> None:
    """Put a meeting's forecast on record, and what was worked back from it, in place of earlier ones for the same session.

    Called as the meeting is recorded as finished, in the same transaction,
    and only then: a meeting that fails after the seat has spoken leaves
    nothing of this on record, and what a finished meeting of the same
    session left stays as it was. It writes what `ready` made and does
    nothing else: by then the orders are placed, and only a journal that
    cannot be written to (which would stop the meeting's own ending from
    being recorded just the same) may stand in the way.
    """
    with journal.transaction():
        journal.save_forecast_set(forecasts)
        journal.save_forecast_scores(rows)
        if tables is not None:
            journal.save_backward_set(tables)
            journal.save_outcome_scores(endings or [])
            journal.save_event_scores(events or [])
        else:
            # This meeting worked no trade back. A reading an earlier meeting of the session left would stand
            # beside this meeting's forecast as if it were its own.
            journal.drop_backward_set(forecasts.arm, forecasts.session_date)


def speak(
    journal: Journal, arm: str, run_id: str, snapshot: Snapshot, account: Account,
    positions: dict[str, Position], views: dict[str, TickerView], *, provider, universe: list[str],
    now: datetime, store: ParamStore | None = None, headlines: outcomes.Headlines | None = None,
) -> Turn:
    """Forecast, work each trade back from its endings, reconcile what came due, and return what the seat says with what to keep.

    Nothing is put on record here: the caller keeps it (`keep`) once the
    meeting has finished. Anything that cannot be forecast is said in words,
    with the reason. Nothing is put in its place. `headlines` says what the
    ledger held about a name in given days: it is what a proposed event with
    a headline check is judged by.
    """
    session = snapshot.session_date
    index = snapshot.market_bars.get(MARKET)
    if index is None or len(index.dates) < 2:
        return Turn([problem_line("No forecast at this meeting: the index's daily bars are not in the snapshot.")])
    names, missing = subjects(journal, arm, snapshot, positions, views)
    known = {
        ticker: (item.fundamentals.sector, item.fundamentals.quote_type)
        for ticker, item in snapshot.tickers.items() if item.fundamentals is not None
    }
    tickers = sorted(set(universe) | {subject.ticker for subject in names})
    params, estimated, trouble = for_session(store or ParamStore(), provider, tickers, session, now=now, known=known)
    lines: list[Line] = []
    if trouble:
        lines.append(problem_line(trouble if params is not None else f"No forecast at this meeting. {trouble}"))
    if params is None:
        return Turn(lines)
    if estimated:
        lines.append(parameters_line(params))

    funds = {symbol: returns_of(bars) for symbol, bars in snapshot.market_bars.items() if symbol != MARKET}
    seed = seed_of(run_id)
    market = returns_of(index)
    try:
        result = engine.run(
            params, session, market, funds, names, seed=seed,
            book=Book(cash=account.cash, equity=account.equity, high_water=account.high_water,
                      unpriced=unpriced(names, list(positions.values()))), trace=True,
        )
    except engine.ForecastError as e:
        return Turn(lines + [problem_line(f"No forecast at this meeting: {e}")])
    forecasts = ForecastSet(
        arm=arm, session_date=session, run_id=run_id, created_at=now, params_version=params.version,
        params_cutoff=params.cutoff, seed=seed, paths=config.FORECAST_PATHS, states=params.regime.names,
        state_now=result.state_now, state_ahead=result.state_ahead, tickers=result.tickers, book=result.book,
        problems=missing + result.problems,
    )

    lines.append(state_line(forecasts))
    lines += [forecast_line(forecast) for forecast in forecasts.tickers]
    if forecasts.book is not None and forecasts.book.positions:
        lines.append(book_line(forecasts))
    if forecasts.problems:
        lines.append(Line("forecast_problem", "Not forecast. " + " ".join(forecasts.problems),
                          {"problems": forecasts.problems}))

    # The same paths read backwards. A failure here costs the meeting this part and leaves the forecast standing.
    tables = None
    try:
        read, skipped = work_back(result, session, names, forecasts.states)
        tables = BackwardSet(
            arm=arm, session_date=session, run_id=run_id, created_at=now, params_version=params.version, seed=seed,
            paths=config.FORECAST_PATHS, measured=read, problems=skipped,
        )
        lines += [backward_line(table) for table in read]
        if skipped:
            lines.append(Line("backward_problem", "Not worked back. " + " ".join(skipped), {"problems": skipped}))
    except Exception as e:
        lines.append(problem_line(f"The trades could not be worked back at this meeting: {type(e).__name__}: {e}"))

    def bars(ticker: str):
        return frame_of(snapshot.tickers[ticker].bars) if ticker in snapshot.tickers else None

    # Reconciling is about earlier meetings' forecasts. If it fails, that is said, and today's forecast stands.
    try:
        scores, fresh = reconcile(journal, arm, bars, as_of=session)
        if fresh:
            # Today's own forecasts are counted as waiting: they go on record when the meeting finishes.
            earlier = [score for score in scores if score.session_date != session]
            lines.append(scores_line(arm, earlier + waiting(forecasts), fresh))
    except Exception as e:
        lines.append(problem_line(
            f"The forecasts that came due could not be reconciled at this meeting: {type(e).__name__}: {e}"))
    try:
        endings, events, due = outcomes.reconcile(journal, arm, bars, headlines, as_of=session)
        if due:
            lines.append(outcomes_line(endings, events, due))
    except Exception as e:
        lines.append(problem_line(
            f"The endings and events that came due could not be reconciled at this meeting: {type(e).__name__}: {e}"))
    setup = Setup(params, session, market, funds, {subject.ticker: subject for subject in names}, seed)
    return Turn(lines, forecasts, tables, setup)
