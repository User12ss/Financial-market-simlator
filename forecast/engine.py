"""The simulation: many possible paths for the market and every name together, and what is read off them.

Each simulated day:

    the market's state moves along its chain, and the market's return is drawn from that state
    each sector fund moves beyond the market by a draw from its own past, scaled to today's volatility
    each name moves with the market and with its sector's own move, and then by one of three things:
        an ordinary day     a draw from its own ordinary days, scaled to today's volatility
        a jump              with the frequency of its past jump days, a draw from those
        a report day        on the session that reacts to its next earnings report, a draw from its past report days

All names share the same market and sector paths, so they rise and fall
together as they do in fact. Within a day the price is taken to move
continuously, apart from a jump or a report, which is taken at the open: that
is what says whether a stop between yesterday's close and today's was touched.
How far a name dips or spikes inside a day is tied to the market's own day by
the share of the name's daily move that comes from the market and its sector:
two names that are nothing but the market touch their stops on the same day.

The random numbers come from one seed. The market, each fund and each name
draw from streams of their own, named after them: the same seed, snapshot and
parameters give the same forecast to the last digit, and leaving a name out
does not change another's.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field
from datetime import date, time
from statistics import NormalDist

import numpy as np
import pandas as pd

import config
from data.market_calendar import is_trading_day, next_trading_day, trading_day_after, trading_days
from forecast import regime
from forecast.params import BURN_IN, ForecastError, Params, SeriesParams, after_close, ewma, reaction_days
from models.forecast import BANDS, GRID, HORIZONS, BookForecast, BookHorizon, HorizonForecast, TickerForecast

_NORMAL = NormalDist()
DAYS = max(HORIZONS)
# A name needs this many daily returns in the snapshot for its volatility today to mean anything.
MIN_BARS = BURN_IN + 40
NEVER = DAYS + 1
_NEVER = NEVER
# A proposed event is sized by the name's own past jumps when it has this many, else by its sector's too.
MIN_EVENT_POOL = 10


@dataclass
class Proposed:
    """An event a seat proposed for one name. A judgement, not a measurement: it is simulated only when asked for."""

    key: str
    first: int              # the first simulated day it may fall on: 0 is the next session
    last: int               # the last
    probability: float
    direction: int          # +1 up, -1 down
    low: float              # the smallest move it would make, as a log return above zero
    high: float             # the largest


@dataclass
class Subject:
    """One name to forecast, as the meeting has it."""

    ticker: str
    close: float
    returns: pd.Series                      # daily log returns by session, up to the meeting's session
    stop: float | None = None
    stop_from: str = ""
    next_report: date | None = None         # the next earnings report's date, as the source gives it
    direction: str = ""
    confidence: float | None = None
    view_horizon: int | None = None
    lean: float = 0.0                       # see forecast.weights.lean; signed by the direction here
    lean_basis: str = ""
    shares: int = 0
    target: float | None = None             # a target someone named: without one, a notional one stands in
    events: list[Proposed] = field(default_factory=list)


@dataclass
class Trace:
    """One name's simulated paths, kept so they can be read backwards from how they end (forecast.backward)."""

    ticker: str
    path: np.ndarray                # log price at each day's close, from the meeting's close
    lowest: np.ndarray              # the lowest point reached by each day's close
    stopped: np.ndarray             # the first day the stop was touched, or NEVER
    reached: np.ndarray             # the first day the target was reached, or NEVER
    sold: np.ndarray | None         # where a touched stop sold: the stop, or the open that passed it
    jumps: np.ndarray               # its own unannounced jump on each day, 0 where there was none
    report_index: int | None        # the simulated day that reacts to its earnings report, when modelled
    report_move: np.ndarray | None  # that day's move at the open, in units of the volatility expected for it
    fund_jumps: np.ndarray | None   # its sector fund's own jump on each day
    events: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)   # by key: the day it fell on or NEVER, its size
    left_out: dict[str, str] = field(default_factory=dict)     # proposed events that could not be simulated, and why
    capped: dict[str, float] = field(default_factory=dict)     # those whose range was cut to the largest past jump: that jump


@dataclass
class Book:
    cash: float
    equity: float
    high_water: float
    # Names held that the snapshot has no usable price for, and the value the account carries each at. They
    # cannot be subjects, and still count in the book.
    unpriced: dict[str, float] = field(default_factory=dict)


@dataclass
class _Fund:
    history: pd.Series          # its own daily moves in the snapshot's window
    move: np.ndarray            # its own simulated ordinary moves: nothing on a day it jumps
    gap: np.ndarray             # its own simulated jumps, taken at the open like a name's
    sigma: np.ndarray           # the volatility each ordinary move was drawn with: nothing on a day it jumps
    vol: float                  # its own volatility expected for the next session


@dataclass
class _Shared:
    """What every name is simulated against."""

    market: np.ndarray          # the market's simulated returns
    market_vol: np.ndarray      # the volatility each was drawn with
    market_vol_next: float
    scale: np.ndarray           # how much larger own moves are in each path-day's state than today
    dip: np.ndarray             # the market's own day, as it bears on every name's low
    spike: np.ndarray           # and on every name's high


@dataclass
class Result:
    state_now: list[float]
    state_ahead: dict[str, list[float]]
    tickers: list[TickerForecast] = field(default_factory=list)
    book: BookForecast | None = None
    problems: list[str] = field(default_factory=list)
    # Kept only when asked for (`trace`): the paths behind the forecasts.
    traces: dict[str, Trace] = field(default_factory=dict)
    states: np.ndarray | None = None        # the market's state on each simulated day
    market: np.ndarray | None = None        # the market's log level at each day's close, from the meeting's close


def stream(seed: int, label: str) -> np.random.Generator:
    """The random numbers of one part of the simulation: the same for the same seed and name, whatever else is drawn."""
    return np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(zlib.crc32(label.encode()),)))


def state_today(params: Params, market: pd.Series, session: date) -> np.ndarray:
    """The probability of each market state after `session`'s close: the estimate's last day, carried on by the returns since."""
    if session < params.cutoff:
        raise ForecastError(
            f"The parameters (version {params.version}) were estimated on data to {params.cutoff}, after {session}."
        )
    since = trading_days(next_trading_day(params.cutoff), session)
    missing = [day for day in since if day not in market.index]
    if missing:
        raise ForecastError(
            f"The index's return for {missing[0]}" + (f" and {len(missing) - 1} more session(s)" if len(missing) > 1 else "")
            + f" is not in the snapshot, so the market's state cannot be carried on from {params.cutoff}."
        )
    path = regime.filtered(params.regime, market.reindex(since).to_numpy(), params.state_at_cutoff)
    return path[-1] if len(path) else np.array(params.state_at_cutoff)


def own_moves(series: SeriesParams, returns: pd.Series, market: pd.Series, fund: pd.Series | None) -> pd.Series:
    """A name's own daily moves in the snapshot's window: its return less the factors' shares and its usual average."""
    parts = {"own": returns, "market": market}
    if fund is not None:
        parts["fund"] = fund
    joined = pd.concat(parts, axis=1, join="inner").dropna()
    moves = joined["own"] - series.raw_drift - series.beta * joined["market"]
    if fund is not None:
        moves = moves - series.sector_loading * joined["fund"]
    return moves


def expected_vol(series: SeriesParams, moves: pd.Series, params: Params) -> float:
    """The name's own volatility expected for the next session. Report days and jump days do not raise it.

    The report days kept with the parameters end at the estimate's cut-off.
    A report since then (earlier this week, say) is found from the report
    times kept with them, by the same rule the estimate uses.
    """
    reported = set(series.report_days)
    if series.reports:
        reported |= set(reaction_days(series.reports, list(moves.index), moves.to_numpy())[0])
    skip = np.array([day in reported for day in moves.index], dtype=bool)
    return ewma(moves.to_numpy(), skip, params.settings.ewma_lambda, params.settings.jump_sigmas)[2]


def _state_paths(model: regime.RegimeModel, now: np.ndarray, paths: int, rng: np.random.Generator) -> np.ndarray:
    matrix = model.matrix()
    edges = np.cumsum(matrix, axis=1)
    draws = rng.random((paths, DAYS))
    states = np.empty((paths, DAYS), dtype=np.int64)
    top = model.states - 1
    states[:, 0] = np.minimum(np.searchsorted(np.cumsum(now @ matrix), draws[:, 0], side="right"), top)
    for day in range(1, DAYS):
        states[:, day] = np.minimum((draws[:, day, None] >= edges[states[:, day - 1]]).sum(axis=1), top)
    return states


def _normal_cdf(z: np.ndarray) -> np.ndarray:
    """The standard normal's distribution function, to seven places (Abramowitz and Stegun 7.1.26)."""
    x = np.abs(z) / math.sqrt(2)
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = 1.0 - poly * np.exp(-x * x)
    return 0.5 * (1.0 + np.where(z >= 0, erf, -erf))


def _extreme(opened: np.ndarray, closed: np.ndarray, spread: np.ndarray, draw: np.ndarray, sign: float) -> np.ndarray:
    """The lowest (sign -1) or highest (sign +1) point of a continuous path from the open to the close.

    `spread` is the variance of the day's continuous move and `draw` a
    standard normal: the further out it is, the further the path strays
    beyond its two ends.
    """
    stray = -2.0 * spread * np.log(np.clip(_normal_cdf(draw), 1e-300, 1.0))
    return (opened + closed + sign * np.sqrt((closed - opened) ** 2 + stray)) / 2


def _first(reached: np.ndarray) -> np.ndarray:
    """The first day each path reached something, as an index, or _NEVER."""
    return np.where(reached.any(axis=1), reached.argmax(axis=1), _NEVER)


def _report_day(series: SeriesParams, subject: Subject, session: date) -> tuple[date | None, int | None]:
    """The session that reacts to the next report, and its place among the simulated days when within them.

    The snapshot names the next report when its date lies after the session.
    A report made on the session's own day after the close moves the next
    session, and the snapshot cannot show that one: it is looked for among the
    report times kept with the parameters.
    """
    coming: list[date] = []
    for stamp in series.reports:
        if stamp.date() != session:
            continue
        clock = stamp.timetz().replace(tzinfo=None)
        if after_close(stamp, session) if clock != time(0, 0) else bool(series.report_lag):
            coming.append(next_trading_day(session))
    if subject.next_report is not None and subject.next_report > session:
        if not is_trading_day(subject.next_report):
            # Dated on a day without a session: the next session reacts, as the estimate has it too.
            coming.append(next_trading_day(subject.next_report))
        else:
            coming.append(next_trading_day(subject.next_report) if series.report_lag else subject.next_report)
    if not coming:
        return None, None
    on = min(coming)
    index = len(trading_days(session, on)) - 2
    return (on, index) if 0 <= index < DAYS else (None, None)


def naive_grid(vol: float, sessions: int) -> list[float]:
    """The plain forecast: no drift, no states, no jumps, the same volatility every day."""
    return [round(vol * math.sqrt(sessions) * _NORMAL.inv_cdf(percent / 100), 6) for percent in GRID]


def naive_stop_touch(vol: float, sessions: int, level: float) -> float:
    """Chance that a driftless path with that daily volatility falls to `level` (a log return below zero) on the way."""
    if vol <= 0:
        return 0.0
    return min(1.0, 2 * _NORMAL.cdf(level / (vol * math.sqrt(sessions))))


def run(
    params: Params, session: date, market: pd.Series, funds: dict[str, pd.Series], subjects: list[Subject], *,
    seed: int, paths: int | None = None, book: Book | None = None, target_r: float | None = None,
    trace: bool = False,
) -> Result:
    """Forecast every subject, and the book when one is given.

    `market` and `funds` are daily log returns by session from the meeting's
    snapshot. A subject that cannot be forecast is named in the result's
    problems with the reason, and nothing stands in for it. With `trace` the
    simulated paths are kept on the result, for reading them backwards.
    """
    paths = config.FORECAST_PATHS if paths is None else paths
    target_r = config.FORECAST_TARGET_R if target_r is None else target_r
    now = state_today(params, market, session)
    model = params.regime
    result = Result(
        state_now=[round(float(v), 6) for v in now],
        state_ahead={str(h): [round(v, 6) for v in regime.ahead(model, now, h)] for h in HORIZONS},
    )

    states = _state_paths(model, now, paths, stream(seed, "market states"))
    market_vol = np.array(model.vols)[states]
    market_path = np.array(model.means)[states] + market_vol * stream(seed, "market").standard_normal((paths, DAYS))
    multiplier = np.array(params.state_vol)
    scale = multiplier[states] / math.sqrt(float(now @ multiplier ** 2))
    next_state = now @ model.matrix()
    market_vol_next = math.sqrt(float(next_state @ np.array(model.vols) ** 2))
    inside = stream(seed, "market intraday")
    shared = _Shared(market_path, market_vol, market_vol_next, scale,
                     inside.standard_normal((paths, DAYS)), inside.standard_normal((paths, DAYS)))

    fund_paths: dict[str, _Fund] = {}

    def fund_path(symbol: str) -> _Fund:
        if symbol not in fund_paths:
            series = params.series.get(symbol)
            if series is None or symbol not in funds:
                raise ForecastError(f"its sector fund {symbol} is not in the "
                                    + ("parameters" if series is None else "snapshot") + ".")
            history = own_moves(series, funds[symbol], market, None)
            if len(history) < MIN_BARS:
                raise ForecastError(
                    f"its sector fund {symbol} has {len(history)} daily returns in the snapshot; {MIN_BARS} are needed."
                )
            vol = expected_vol(series, history, params)
            rng = stream(seed, f"fund {symbol}")
            pool = np.array(series.residuals)
            sigma = vol * scale
            move = sigma * pool[rng.integers(0, len(pool), (paths, DAYS))]
            gap = np.zeros((paths, DAYS))
            if series.jump_rate > 0 and series.jumps:
                # A fund jumps too, with the frequency and the sizes of its own past jumps.
                jumped = rng.random((paths, DAYS)) < series.jump_rate
                gap = np.where(jumped, np.array(series.jumps)[rng.integers(0, len(series.jumps), (paths, DAYS))], 0.0)
                move, sigma = np.where(jumped, 0.0, move), np.where(jumped, 0.0, sigma)
            fund_paths[symbol] = _Fund(history, move, gap, sigma, vol)
        return fund_paths[symbol]

    held: list[tuple[Subject, np.ndarray, np.ndarray, np.ndarray]] = []   # log price paths, opens, first stop day
    unsimulated: list[Subject] = []
    for subject in sorted(subjects, key=lambda s: s.ticker):
        try:
            forecast, path, opened, stopped, kept = _one(
                params, session, subject, market, fund_path, shared, seed=seed, paths=paths, target_r=target_r,
            )
        except ForecastError as e:
            result.problems.append(f"{subject.ticker}: {e}")
            if subject.shares > 0:
                unsimulated.append(subject)
            continue
        result.tickers.append(forecast)
        if trace:
            result.traces[subject.ticker] = kept
        if subject.shares > 0:
            held.append((subject, path, opened, stopped))

    if book is not None:
        result.book = _book(book, held, unsimulated, paths)
    if trace:
        result.states, result.market = states, np.cumsum(market_path, axis=1)
    return result


def _event_pool(params: Params, series: SeriesParams) -> np.ndarray:
    """The sizes a proposed event is drawn from: the name's own past jumps, with its sector's when it has few.

    A name and a sector that never jumped in the sample are sized by every
    name's jumps in the estimate: a quiet past is no reason to think the
    name cannot jump, and nothing else says how far.
    """
    pool = [abs(move) for move in series.jumps]
    if len(pool) < MIN_EVENT_POOL and series.sector:
        pool += [abs(move) for other in params.series.values()
                 if other.sector == series.sector and other.ticker != series.ticker for move in other.jumps]
    if not pool:
        pool = [abs(move) for other in params.series.values() for move in other.jumps]
    return np.array(sorted(pool))


def _one(
    params: Params, session: date, subject: Subject, market: pd.Series, fund_path, shared: _Shared, *,
    seed: int, paths: int, target_r: float,
) -> tuple[TickerForecast, np.ndarray, np.ndarray, np.ndarray]:
    series = params.series.get(subject.ticker)
    if series is None:
        raise ForecastError(
            f"version {params.version} of the parameters has nothing on it: it was not in the estimate, "
            "or its history could not be used."
        )
    if not (subject.close and subject.close > 0):
        raise ForecastError("it has no usable close.")
    fund = fund_path(series.sector) if series.sector else None
    moves = own_moves(series, subject.returns, market, fund.history if fund else None)
    if len(moves) < MIN_BARS:
        raise ForecastError(f"it has {len(moves)} daily returns in the snapshot in common with the market; {MIN_BARS} are needed.")
    if moves.index[-1] != session:
        raise ForecastError(f"its last bar in the snapshot is {moves.index[-1]}, not {session}.")
    vol = expected_vol(series, moves, params)
    if not vol > 0:
        raise ForecastError("its own moves in the snapshot do not vary.")

    notes = list(series.notes)
    rng = stream(seed, f"name {subject.ticker}")
    pool = np.array(series.residuals)
    sigma = vol * shared.scale
    own = sigma * pool[rng.integers(0, len(pool), (paths, DAYS))]
    gap = np.zeros((paths, DAYS))
    gapped = np.zeros((paths, DAYS), dtype=bool)
    if series.jump_rate > 0 and series.jumps:
        gapped = rng.random((paths, DAYS)) < series.jump_rate
        gap = np.where(gapped, np.array(series.jumps)[rng.integers(0, len(series.jumps), (paths, DAYS))], 0.0)
    report_on, report_index = _report_day(series, subject, session)
    modelled = report_index is not None and bool(series.earnings)
    report_move = None
    if modelled:
        report_move = np.array(series.earnings)[rng.integers(0, len(series.earnings), paths)]
        gap[:, report_index] = vol * report_move
        gapped[:, report_index] = True
    elif report_index is not None:
        notes.append(f"An earnings report moves the session of {report_on}, inside the horizon, and is not modelled.")
    jumps = gap.copy()
    if modelled:
        jumps[:, report_index] = 0.0
    events: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    left_out: dict[str, str] = {}
    capped: dict[str, float] = {}
    if subject.events:
        gap, gapped, jumps, events, left_out, capped = _proposed(
            params, series, subject, gap, gapped, jumps, report_index if modelled else None, seed, paths,
        )
    own = np.where(gapped, 0.0, own)

    fund_vol = fund.vol if fund else 0.0
    daily_vol = math.sqrt((series.beta * shared.market_vol_next) ** 2 + (series.sector_loading * fund_vol) ** 2 + vol ** 2)
    drift = np.full(DAYS, series.drift)
    lean = 0.0
    if subject.lean and subject.view_horizon and subject.direction in ("bullish", "bearish"):
        lean = subject.lean if subject.direction == "bullish" else -subject.lean
        days = min(subject.view_horizon, DAYS)
        # The whole lean is spread over the view's horizon: each day carries its share and no more.
        drift[:days] += lean * daily_vol * math.sqrt(subject.view_horizon) / subject.view_horizon

    smooth = drift + series.beta * shared.market + own
    common = (series.beta * shared.market_vol) ** 2
    if fund is not None:
        smooth = smooth + series.sector_loading * fund.move
        common = common + (series.sector_loading * fund.sigma) ** 2
        gap = gap + series.sector_loading * fund.gap        # its sector's jump reaches it at the open too
    spread = common + np.where(gapped, 0.0, sigma ** 2)
    path = np.cumsum(smooth + gap, axis=1)
    opened = np.concatenate([np.zeros((paths, 1)), path[:, :-1]], axis=1) + gap
    # Inside the day the name strays with the market by the share of its move that is the market's.
    tie = np.sqrt(np.divide(common, spread, out=np.zeros_like(spread), where=spread > 0))
    apart = np.sqrt(1.0 - tie ** 2)
    low = _extreme(opened, path, spread, tie * shared.dip + apart * rng.standard_normal((paths, DAYS)), -1.0)
    high = _extreme(opened, path, spread, tie * shared.spike + apart * rng.standard_normal((paths, DAYS)), 1.0)

    stop_level = target_level = None
    stop = subject.stop if subject.stop and 0 < subject.stop < subject.close else None
    if subject.stop and stop is None:
        notes.append(f"The stop {subject.stop:,.2f} is not below the close {subject.close:,.2f}: nothing is said about it.")
    target = break_even = named = sold = None
    stopped = np.full(paths, _NEVER)
    reached = np.full(paths, _NEVER)
    if stop is not None:
        stop_level = math.log(stop / subject.close)
        stopped = _first(low <= stop_level)
        named = subject.target if subject.target and subject.target > subject.close else None
        if subject.target and named is None:
            notes.append(f"The target {subject.target:,.2f} is not above the close {subject.close:,.2f}: "
                         "a notional one stands in.")
        target = round(named if named else subject.close + target_r * (subject.close - stop), 2)
        target_level = math.log(target / subject.close)
        reached = _first(high >= target_level)
        break_even = (subject.close - stop) / (target - stop)
        # A touched stop sells at the stop, or at the open when the price opened through it.
        sold = np.minimum(stop_level, opened[np.arange(paths), np.minimum(stopped, DAYS - 1)])

    history = subject.returns.to_numpy()
    naive_vol = float(np.std(history))
    lowest = np.minimum.accumulate(low, axis=1)
    horizons = []
    for sessions in HORIZONS:
        grid = np.percentile(path[:, sessions - 1], GRID)
        fall = (1.0 - np.exp(np.minimum(lowest[:, sessions - 1], 0.0))) * 100
        horizons.append(HorizonForecast(
            sessions=sessions, matures_on=trading_day_after(session, sessions),
            quantiles={band: round(subject.close * math.exp(grid[int(band) - 1]), 4) for band in BANDS},
            grid=[round(float(v), 6) for v in grid],
            stop_touch=None if stop is None else float((stopped < sessions).mean()),
            # On a day that reaches both, the stop is taken to have come first.
            target_first=None if stop is None else float(((reached < sessions) & (reached < stopped)).mean()),
            stop_first=None if stop is None else float(((stopped < sessions) & (stopped <= reached)).mean()),
            adverse={mark: round(float(np.percentile(fall, int(mark))), 2) for mark in ("50", "75", "95")},
            naive_grid=naive_grid(naive_vol, sessions),
            naive_stop_touch=None if stop is None else naive_stop_touch(naive_vol, sessions, stop_level),
        ))
    forecast = TickerForecast(
        ticker=subject.ticker, close=subject.close, beta=round(series.beta, 4), sector=series.sector,
        own_vol=vol, daily_vol=daily_vol, naive_vol=naive_vol, stop=stop, stop_from=subject.stop_from if stop else "",
        target=target, target_from="named" if named else "", break_even=break_even, direction=subject.direction,
        confidence=subject.confidence, view_horizon=subject.view_horizon, lean=lean, lean_basis=subject.lean_basis,
        report_on=report_on, report_modelled=modelled, notes=notes, horizons=horizons,
    )
    kept = Trace(
        ticker=subject.ticker, path=path, lowest=lowest, stopped=stopped, reached=reached, sold=sold, jumps=jumps,
        report_index=report_index if modelled else None, report_move=report_move,
        fund_jumps=fund.gap if fund is not None else None, events=events, left_out=left_out, capped=capped,
    )
    return forecast, path, opened, stopped, kept


def _proposed(
    params: Params, series: SeriesParams, subject: Subject, gap: np.ndarray, gapped: np.ndarray, jumps: np.ndarray,
    report_index: int | None, seed: int, paths: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict, dict, dict]:
    """Put the events a seat proposed for a name on its paths.

    Each happens or not on each path by its own stated probability, on a day
    drawn evenly from its dates. Its size is drawn from past jumps, in the
    direction stated, and held inside the stated range; a range that reaches
    beyond the largest past jump is cut to it, and that is noted. On a day
    such an event falls, no unannounced jump is drawn as well, so one day
    does not carry two; an earnings report on that day still counts.

    The events are taken to be independent of one another and of everything
    else. Each draws from a stream named after it, so a name's other draws
    are what they are without it.
    """
    pool = _event_pool(params, series)
    events: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    left_out: dict[str, str] = {}
    capped: dict[str, float] = {}
    seat = np.zeros((paths, DAYS))
    hit = np.zeros((paths, DAYS), dtype=bool)
    rows = np.arange(paths)
    for event in subject.events:
        first, last = max(event.first, 0), min(event.last, DAYS - 1)
        if first > last:
            left_out[event.key] = "its dates lie outside the simulated days."
            continue
        if not len(pool):
            left_out[event.key] = "no name in the estimate has a past jump to size it by."
            continue
        rng = stream(seed, f"event {subject.ticker} {event.key}")
        happens = rng.random(paths) < event.probability
        day = rng.integers(first, last + 1, paths)
        largest = float(pool[-1])
        if event.high > largest:
            capped[event.key] = largest
        ceiling = min(event.high, largest)
        size = event.direction * np.clip(pool[rng.integers(0, len(pool), paths)], min(event.low, ceiling), ceiling)
        seat[rows[happens], day[happens]] += size[happens]
        hit[rows[happens], day[happens]] = True
        events[event.key] = (np.where(happens, day, NEVER), np.where(happens, size, 0.0))
    report = np.zeros((1, DAYS), dtype=bool)
    if report_index is not None:
        report[0, report_index] = True
    gap = np.where(hit, np.where(report, gap, 0.0) + seat, gap)
    return gap, gapped | hit, np.where(hit, 0.0, jumps), events, left_out, capped


def _book(book: Book, held: list, unsimulated: list[Subject], paths: int) -> BookForecast:
    """What the book as it stands may be worth, taking each stop as it is touched and trading nothing else.

    A holding that could not be simulated is counted at its last close
    throughout, and named: leaving it out would understate the book. So is
    one the snapshot has no usable price for, at the value the account
    carries it at.
    """
    limits_drawdown = book.high_water * (1 - config.RISK_MAX_DRAWDOWN_PCT / 100)
    frozen = sum(subject.shares * subject.close for subject in unsimulated) + sum(book.unpriced.values())
    values = np.full((paths, DAYS), book.cash + frozen)
    stops_on = np.zeros((paths, DAYS), dtype=np.int64)
    for subject, path, opened, stopped in held:
        start = subject.shares * subject.close
        value = start * np.exp(path)
        if subject.stop:
            touched = stopped < DAYS
            day = np.minimum(stopped, DAYS - 1)
            rows = np.arange(paths)
            # Sold at the stop, or at the open when the price opened through it.
            out = np.minimum(subject.shares * subject.stop, start * np.exp(opened[rows, day]))
            after = np.arange(DAYS)[None, :] >= stopped[:, None]
            value = np.where(after, out[:, None], value)
            stops_on[rows[touched], day[touched]] += 1
        values += value
    before = np.concatenate([np.full((paths, 1), book.cash + frozen + sum(s.shares * s.close for s, *_ in held)),
                             values[:, :-1]], axis=1)
    lost = values / before - 1 <= -config.RISK_DAILY_LOSS_LIMIT_PCT / 100
    horizons = []
    for sessions in HORIZONS:
        grid = np.percentile(values[:, sessions - 1], [int(band) for band in BANDS])
        horizons.append(BookHorizon(
            sessions=sessions, equity={band: round(float(value), 2) for band, value in zip(BANDS, grid)},
            stops_together=float((stops_on[:, :sessions] >= 2).any(axis=1).mean()),
            drawdown_breaker=float((values[:, :sessions] <= limits_drawdown).any(axis=1).mean()),
            daily_loss_breaker=float(lost[:, :sessions].any(axis=1).mean()),
        ))
    return BookForecast(
        equity=book.equity, cash=book.cash, high_water=book.high_water,
        positions=len(held) + len(unsimulated) + len(book.unpriced), horizons=horizons,
        notes=[f"{subject.ticker} is held but could not be simulated: its value is taken as fixed at its last close."
               for subject in unsimulated]
        + [f"{ticker} is held but the snapshot has no usable price for it: its value is taken as fixed at the "
           f"{value:,.2f} the account carries it at." for ticker, value in sorted(book.unpriced.items())],
    )
