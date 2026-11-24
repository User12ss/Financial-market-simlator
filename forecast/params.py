"""The week's parameters: everything the forecast needs that is estimated from history.

Estimated once a week on data up to the last session of the week before, and
kept as one numbered file. Within a week the parameters do not move. Only
two things follow the new daily bars: the probability of each market state,
and each name's volatility. So a forecast that changes during a week changes
because of what happened, never because the estimate was run again.

For each name the daily log return is taken apart into

    its share of the market's move              beta x market
    its share of its sector's own move          loading x (sector fund's move beyond its own share of the market)
    a move of its own                           everything left

and the days of the last part are sorted into three kinds, each counted once:

    report days       the session the price reacted to an earnings report
    jump days         an own move beyond FORECAST_JUMP_SIGMAS of what was expected that day
    ordinary days     the rest: the sample the day-to-day noise is redrawn from

A report day or a jump day is in neither the ordinary sample nor the
volatility estimate, and a report day is never counted as a jump. Otherwise
one large move would be put into the forecast two or three times over.
"""

from __future__ import annotations

import bisect
import math
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

import config
from data.market_calendar import previous_trading_day, session_close
from data.providers import DataError
from execution.paper import SECTOR_FUNDS
from forecast import regime
from forecast.regime import RegimeModel

MARKET = "^GSPC"
NEW_YORK = ZoneInfo("America/New_York")
# The first days of a sample only set the starting volatility.
BURN_IN = 20
MIN_DAYS = 250
MIN_MARKET_DAYS = 500
# A state seen on fewer days than this within the names' own history says too little about them.
MIN_STATE_DAYS = 20.0
FUND_OF_SECTOR = {sector: fund for fund, sector in SECTOR_FUNDS.items()}


class ForecastError(RuntimeError):
    """The parameters could not be estimated, or there are none to forecast with."""


class Settings(BaseModel):
    """The settings an estimate was made with, kept beside it."""

    states: int
    market_years: int
    history_years: int
    ewma_lambda: float
    jump_sigmas: float
    min_earnings: int
    drift_shrink_days: int

    @classmethod
    def from_config(cls) -> Settings:
        return cls(
            states=config.FORECAST_STATES, market_years=config.FORECAST_MARKET_YEARS,
            history_years=config.FORECAST_HISTORY_YEARS, ewma_lambda=config.FORECAST_EWMA_LAMBDA,
            jump_sigmas=config.FORECAST_JUMP_SIGMAS, min_earnings=config.FORECAST_MIN_EARNINGS,
            drift_shrink_days=config.FORECAST_DRIFT_SHRINK_DAYS,
        )


class SeriesParams(BaseModel):
    """One name, or one sector fund, against the market."""

    ticker: str
    quote_type: str = ""
    sector: str = Field(default="", description="The sector fund that is its second factor; empty when it has none")
    sector_known: bool = Field(default=False, description="Whether the source was asked and answered")
    first_day: date
    days: int = Field(description="Daily returns the estimate rests on")
    beta: float
    sector_loading: float = 0.0
    drift: float = Field(description="Its own daily drift beyond the two factors, after shrinking")
    raw_drift: float = Field(description="The same before shrinking, as the sample has it")
    vol: float = Field(description="Standard deviation of its own move on ordinary days")
    residuals: list[float] = Field(description="Own moves on ordinary days, each divided by that day's expected volatility")
    jump_rate: float = Field(description="Share of days that are neither ordinary nor report days")
    jumps: list[float] = Field(default_factory=list, description="Own log moves on jump days")
    reports: list[datetime] = Field(default_factory=list, description="Report times as the source gave them")
    reports_known: bool = Field(default=False, description="False when the source could not say when it reported")
    report_days: list[date] = Field(default_factory=list, description="The session the price reacted on, for each report in the sample")
    report_lag: int = Field(default=0, description="Sessions from a report's date to the session that reacts: 0 or 1")
    earnings: list[float] = Field(default_factory=list, description="Own moves on report days, divided by expected volatility")
    earnings_from: Literal["own", "sector", "none"] = "none"
    notes: list[str] = Field(default_factory=list)


class Params(BaseModel):
    version: int
    cutoff: date = Field(description="The last session whose bar the estimate read")
    created_at: datetime
    market: str = MARKET
    market_days: int
    market_first_day: date
    regime: RegimeModel
    state_at_cutoff: list[float] = Field(description="Probability of each state on the cutoff day, from the returns up to it")
    log_likelihood: float
    state_vol: list[float] = Field(
        description="How much larger or smaller a name's own moves are in each state than on average"
    )
    series: dict[str, SeriesParams] = Field(default_factory=dict)
    settings: Settings
    problems: list[str] = Field(default_factory=list)


# ── Pieces ───────────────────────────────────────────────────────────────────

def log_returns(frame: pd.DataFrame | None) -> pd.Series:
    """Daily log returns of the dividend-adjusted close, by session. Empty when there are no usable bars."""
    if frame is None or frame.empty:
        return pd.Series(dtype=float)
    close = frame["adj_close"].astype(float)
    close = close[np.isfinite(close) & (close > 0)]
    return np.log(close).diff().dropna()


def ewma(moves: np.ndarray, skip: np.ndarray, weight: float, jump_sigmas: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Each day's expected volatility from the days before it, which days were jumps, and tomorrow's volatility.

    `skip` marks days that are left out of everything (report days). A day
    whose move is beyond `jump_sigmas` of what was expected is a jump: it is
    flagged, and does not raise the volatility expected afterwards. The first
    BURN_IN days only set the starting level.
    """
    count = len(moves)
    start = moves[:BURN_IN][~skip[:BURN_IN]]
    if len(start) < 5:
        start = moves[~skip]
    variance = float(np.mean(start ** 2)) if len(start) else 0.0
    sigma, jumps = np.empty(count), np.zeros(count, dtype=bool)
    for day in range(count):
        sigma[day] = math.sqrt(variance)
        if day < BURN_IN or skip[day]:
            continue
        if abs(moves[day]) > jump_sigmas * sigma[day]:
            jumps[day] = True
            continue
        variance = weight * variance + (1 - weight) * moves[day] ** 2
    return sigma, jumps, math.sqrt(variance)


def after_close(stamp: datetime, session: date) -> bool:
    """Whether a report made at `stamp` came at or after that session's close: 16:00 in New York, 13:00 on a half day."""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=NEW_YORK)      # a time that names no zone is taken as the exchange's own
    return stamp >= session_close(session)


def reaction_days(reports: list[datetime], sessions: list[date], moves: np.ndarray) -> tuple[list[date], int]:
    """The session the price reacted to each report, and how many sessions after a report's date that usually is.

    A report at or after its session's close moves the next session; one
    before it moves its own; one dated on a day without a session moves the
    next session there is. Where the source gives no hour (midnight), the
    larger own move of the two decides. Reports outside the sample are left
    out.
    """
    found: list[date] = []
    lags: list[int] = []
    for stamp in reports:
        if not sessions or stamp.date() < sessions[0]:
            continue
        on = bisect.bisect_left(sessions, stamp.date())      # first session on or after the report's date
        if on >= len(sessions):
            continue
        after = on + 1 if on + 1 < len(sessions) else None
        clock = stamp.timetz().replace(tzinfo=None)
        if sessions[on] != stamp.date():
            lag = 0             # reported on a day without a session: the next one reacts
        elif clock == time(0, 0):
            lag = 1 if after is not None and abs(moves[after]) > abs(moves[on]) else 0
        else:
            lag = 1 if after_close(stamp, sessions[on]) else 0
        if lag and after is None:
            continue            # the reaction lies beyond the sample
        found.append(sessions[on + lag])
        lags.append(lag)
    return sorted(set(found)), (1 if sum(lags) * 2 > len(lags) else 0)


def _fit_factors(own: np.ndarray, columns: list[np.ndarray], keep: np.ndarray) -> np.ndarray:
    design = np.column_stack([np.ones(len(own))] + columns)
    return np.linalg.lstsq(design[keep], own[keep], rcond=None)[0]


def estimate_series(
    ticker: str, own: pd.Series, market: pd.Series, factor: pd.Series | None, sector: str,
    reports: list[datetime] | None, settings: Settings,
) -> tuple[SeriesParams, pd.Series, np.ndarray]:
    """One name's parameters, its own daily moves by session, and which of those days were ordinary.

    `factor` is the sector fund's own move (beyond its share of the market),
    or None for a name with no sector factor. `reports` is None when the
    source could not say when the company reported.
    """
    parts = {"own": own, "market": market}
    if factor is not None:
        parts["factor"] = factor
    joined = pd.concat(parts, axis=1, join="inner").dropna()
    if len(joined) < MIN_DAYS:
        raise ForecastError(f"{ticker} has {len(joined)} daily returns in common with the market; {MIN_DAYS} are needed.")
    sessions = list(joined.index)
    r = joined["own"].to_numpy()
    columns = [joined["market"].to_numpy()] + ([joined["factor"].to_numpy()] if factor is not None else [])
    everything = np.ones(len(r), dtype=bool)

    # Twice: where a report's hour is unknown, the day that reacted is told by the size of
    # the own move, which needs the factors taken out first.
    design = np.column_stack([np.ones(len(r))] + columns)
    days, lag = reaction_days(reports or [], sessions, r - design @ _fit_factors(r, columns, everything))
    chosen = set(days)
    reported = np.array([day in chosen for day in sessions], dtype=bool)
    weights = _fit_factors(r, columns, ~reported)
    centred = r - design @ weights      # the name's own move, its average on days without a report taken out

    sigma, jumped, _ = ewma(centred, reported, settings.ewma_lambda, settings.jump_sigmas)
    settled = np.arange(len(r)) >= BURN_IN
    ordinary = settled & ~reported & ~jumped
    if ordinary.sum() < MIN_DAYS // 2 or not np.all(sigma[ordinary] > 0):
        raise ForecastError(f"{ticker}: its own daily moves do not vary enough to estimate anything from.")
    unreported = int((settled & ~reported).sum())
    count = len(r)
    params = SeriesParams(
        ticker=ticker, sector=sector if factor is not None else "", first_day=sessions[0], days=count,
        beta=float(weights[1]), sector_loading=float(weights[2]) if factor is not None else 0.0,
        raw_drift=float(weights[0]), drift=float(weights[0]) * count / (count + settings.drift_shrink_days),
        vol=float(np.sqrt(np.mean(centred[ordinary] ** 2))),
        residuals=[round(float(v), 6) for v in centred[ordinary] / sigma[ordinary]],
        jump_rate=float(jumped.sum()) / unreported if unreported else 0.0,
        jumps=[round(float(v), 6) for v in centred[jumped]],
        reports=sorted(reports or []), reports_known=reports is not None,
        report_days=[sessions[index] for index in np.flatnonzero(reported)], report_lag=lag,
        earnings=[round(float(v), 6) for v in centred[reported & settled] / sigma[reported & settled]],
    )
    return params, pd.Series(centred, index=sessions), ordinary


def state_multipliers(
    model: RegimeModel, smoothed: pd.DataFrame, own_moves: dict[str, tuple[pd.Series, np.ndarray]],
) -> tuple[list[float], list[str]]:
    """How much larger a name's own moves are in each market state than on its average day, pooled over all names.

    A state the names' own history hardly saw is given the index's own ratio
    instead, and that is said.
    """
    if model.states == 1:
        return [1.0], []
    above = np.zeros(model.states)
    below = np.zeros(model.states)
    for moves, ordinary in own_moves.values():
        kept = moves[ordinary]
        states = smoothed.reindex(kept.index).dropna()
        kept = kept.reindex(states.index).to_numpy()
        if len(kept) == 0:
            continue
        squared = kept ** 2 / float(np.mean(kept ** 2))
        above += states.to_numpy().T @ squared
        below += states.to_numpy().sum(axis=0)
    notes = []
    shares = below / below.sum() if below.sum() > 0 else np.array(model.stationary())
    index_ratio = np.array(model.vols) / math.sqrt(float(shares @ np.array(model.vols) ** 2))
    result = []
    for state in range(model.states):
        if below[state] >= MIN_STATE_DAYS and above[state] > 0:
            result.append(float(math.sqrt(above[state] / below[state])))
        else:
            result.append(float(index_ratio[state]))
            notes.append(
                f"The names' own history has too few {model.names[state]} days to say how much their own moves "
                "grow then: the index's own ratio is used for that state."
            )
    return result, notes


# ── The estimate ─────────────────────────────────────────────────────────────

def _years_before(day: date, years: int) -> date:
    return day - timedelta(days=round(365.25 * years))


def estimate(
    provider, tickers: list[str], cutoff: date, *, version: int, now: datetime,
    previous: Params | None = None, known: dict[str, tuple[str, str]] | None = None,
    settings: Settings | None = None,
) -> Params:
    """Estimate everything from bars up to `cutoff`.

    `known` gives the sector and quote type of names the caller already has
    (from the day's snapshot), so the source is not asked again. What the
    previous estimate learned about a name (its sector, when it reported) is
    carried over, and the source is asked only where something is missing.
    A name whose history cannot be used is left out, and why is recorded:
    nothing stands in for it.
    """
    settings = settings or Settings.from_config()
    known = known or {}
    try:
        bars = provider.daily_bars([MARKET], _years_before(cutoff, settings.market_years), cutoff)
    except DataError as e:
        raise ForecastError(f"The index's history could not be fetched: {e}") from e
    market = log_returns(bars.get(MARKET))
    market = market[market.index <= cutoff]
    if len(market) < MIN_MARKET_DAYS:
        raise ForecastError(
            f"The index ({MARKET}) has {len(market)} daily returns up to {cutoff}; {MIN_MARKET_DAYS} are needed."
        )
    # Where the history does not hold the states asked for (one freak day can stand in the way of a
    # second state), a plainer model is estimated and that is said: one state fewer, down to one.
    fewer: list[str] = []
    for count in range(settings.states, 0, -1):
        try:
            fitted = regime.fit(market.to_numpy(), states=count)
            break
        except ValueError as e:
            if count == 1:
                raise ForecastError(f"The market's states could not be estimated: {e}") from e
            fewer.append(
                f"{count} states of the market could not be estimated from the index's history ({e}) "
                f"{count - 1} {'is' if count == 2 else 'are'} used instead."
            )
    smoothed = pd.DataFrame(fitted.smoothed, index=market.index)

    funds = sorted(SECTOR_FUNDS)
    names = sorted(set(tickers) - set(funds))
    try:
        bars = provider.daily_bars(funds + names, _years_before(cutoff, settings.history_years), cutoff)
    except DataError as e:
        raise ForecastError(f"The names' history could not be fetched: {e}") from e
    returns = {symbol: log_returns(frame)[lambda s: s.index <= cutoff] for symbol, frame in bars.items()}

    problems: list[str] = []
    series: dict[str, SeriesParams] = {}
    own_moves: dict[str, tuple[pd.Series, np.ndarray]] = {}
    for fund in funds:
        try:
            params, moves, ordinary = estimate_series(fund, returns.get(fund, pd.Series(dtype=float)), market, None, "",
                                                      [], settings)
        except ForecastError as e:
            problems.append(str(e))
            continue
        series[fund] = params.model_copy(update={"quote_type": "ETF", "sector_known": True})
        own_moves[fund] = (moves, ordinary)

    for ticker in names:
        if len(returns.get(ticker, ())) < MIN_DAYS:
            # Said before the source is asked anything more about a name that cannot be used anyway.
            problems.append(f"{ticker} has {len(returns.get(ticker, ()))} daily returns up to {cutoff}; {MIN_DAYS} are needed.")
            continue
        before = previous.series.get(ticker) if previous else None
        notes: list[str] = []
        sector_name, quote_type, sector_known = "", "", False
        if ticker in known:
            sector_name, quote_type, sector_known = known[ticker][0], known[ticker][1], True
        elif before is not None and before.sector_known:
            sector_name, quote_type, sector_known = _sector_name(before.sector), before.quote_type, True
        else:
            try:
                facts = provider.fundamentals(ticker)
                sector_name, quote_type, sector_known = facts.sector, facts.quote_type, True
            except DataError as e:
                notes.append(f"Its sector is not known ({e}): only the market factor is used.")
        fund = FUND_OF_SECTOR.get(sector_name, "")
        if fund and fund not in own_moves:
            notes.append(f"Its sector fund {fund} has no usable history: only the market factor is used.")
            fund = ""

        reports: list[datetime] | None = []
        if quote_type.upper() != "ETF":
            reports = _reports(provider, ticker, before, cutoff, notes)
        try:
            params, moves, ordinary = estimate_series(
                ticker, returns.get(ticker, pd.Series(dtype=float)), market,
                own_moves[fund][0] if fund else None, fund, reports, settings,
            )
        except ForecastError as e:
            problems.append(str(e))
            continue
        series[ticker] = params.model_copy(update={
            "quote_type": quote_type, "sector_known": sector_known, "notes": notes,
        })
        own_moves[ticker] = (moves, ordinary)

    _pool_earnings(series, settings.min_earnings)
    multipliers, notes = state_multipliers(fitted.model, smoothed, own_moves)
    return Params(
        version=version, cutoff=cutoff, created_at=now, market_days=len(market), market_first_day=market.index[0],
        regime=fitted.model, state_at_cutoff=fitted.last, log_likelihood=fitted.log_likelihood,
        state_vol=multipliers, series=series, settings=settings, problems=fewer + problems + notes,
    )


def _sector_name(fund: str) -> str:
    return SECTOR_FUNDS.get(fund, "")


def _reports(provider, ticker: str, before: SeriesParams | None, cutoff: date, notes: list[str]) -> list[datetime] | None:
    """When the company reported. Kept from the last estimate while it still names a report to come."""
    if before is not None and before.reports_known and any(stamp.date() > cutoff for stamp in before.reports):
        return before.reports
    ask = getattr(provider, "earnings_reports", None)
    try:
        if ask is None:
            raise DataError("the price source has no record of report dates")
        return ask(ticker)
    except DataError as e:
        if before is not None and before.reports_known:
            notes.append(f"Its report dates could not be brought up to date ({e}): those known last week are used.")
            return before.reports
        notes.append(
            f"When it reported is not known ({e}): its report days are not told apart from other days, "
            "and a report inside a forecast's horizon is not modelled."
        )
        return None


def _pool_earnings(series: dict[str, SeriesParams], minimum: int) -> None:
    """Where a name has too few report days of its own, its sector's stand in. Where the sector has too few, none do."""
    by_sector: dict[str, list[float]] = {}
    for params in series.values():
        if params.sector:
            by_sector.setdefault(params.sector, []).extend(params.earnings)
    for ticker, params in series.items():
        if not params.reports_known or params.quote_type.upper() == "ETF":
            series[ticker] = params.model_copy(update={"earnings": [], "earnings_from": "none"})
        elif len(params.earnings) >= minimum:
            series[ticker] = params.model_copy(update={"earnings_from": "own"})
        elif len(by_sector.get(params.sector, [])) >= minimum:
            series[ticker] = params.model_copy(update={
                "earnings": sorted(by_sector[params.sector]), "earnings_from": "sector",
                "notes": params.notes + [
                    f"Only {len(params.earnings)} report day(s) of its own in the sample: "
                    f"its sector's ({params.sector}) are drawn from instead."
                ],
            })
        else:
            series[ticker] = params.model_copy(update={
                "earnings": [], "earnings_from": "none",
                "notes": params.notes + [
                    "Too few report days, its own and its sector's, to draw from: "
                    "a report inside a forecast's horizon is not modelled."
                ],
            })


# ── Keeping them ─────────────────────────────────────────────────────────────

def due_cutoff(session: date) -> date:
    """The data cut-off of the parameters a session should be forecast with: the last session of the week before."""
    monday = session - timedelta(days=session.weekday())
    return previous_trading_day(monday)


class ParamStore:
    """The estimates on disk, one file per version."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory or config.FORECAST_DIR)

    def _files(self) -> list[tuple[int, date, Path]]:
        """Every estimate on disk as (version, cut-off, file), by version. The name says both."""
        found = []
        for path in self.directory.glob("params_*.json"):
            try:
                _, version, cutoff = path.stem.split("_")
                found.append((int(version), date.fromisoformat(cutoff), path))
            except ValueError:
                continue
        return sorted(found)

    def versions(self) -> list[int]:
        return [version for version, _, _ in self._files()]

    def load(self, version: int) -> Params | None:
        for number, _, path in self._files():
            if number == version:
                try:
                    return Params.model_validate_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
        return None

    def latest(self, cutoff_by: date | None = None) -> Params | None:
        """The estimate on the newest data, or on the newest data ending no later than `cutoff_by`.

        By cut-off first and version second: an estimate made later for an
        earlier week (a session looked at again) does not hide the one for
        this week.
        """
        files = [item for item in self._files() if cutoff_by is None or item[1] <= cutoff_by]
        for version, _, _ in sorted(files, key=lambda item: (item[1], item[0]), reverse=True):
            params = self.load(version)
            if params is not None:
                return params
        return None

    def save(self, params: Params) -> Path:
        """Keep an estimate under its version. Raises FileExistsError when that version is already there:
        two estimates made at the same moment must not write over each other, since a forecast names
        the version it was made from."""
        self.directory.mkdir(parents=True, exist_ok=True)
        if params.version in self.versions():
            raise FileExistsError(f"Version {params.version} of the parameters is already on disk.")
        path = self.directory / f"params_{params.version:04d}_{params.cutoff.isoformat()}.json"
        partial = path.with_suffix(f".json.{os.getpid()}.part")
        partial.write_text(params.model_dump_json(), encoding="utf-8")
        try:
            os.link(partial, path)              # fails if the file exists, where a rename would replace it
        finally:
            partial.unlink()
        return path

    def next_version(self) -> int:
        versions = self.versions()
        return versions[-1] + 1 if versions else 1


def for_session(
    store: ParamStore, provider, tickers: list[str], session: date, *, now: datetime,
    known: dict[str, tuple[str, str]] | None = None,
) -> tuple[Params | None, bool, str]:
    """The parameters to forecast a session with, whether they were estimated just now, and what went wrong.

    A new estimate is made when none on record reaches the last session of
    the week before. If that fails, the newest older one is used and the
    failure is told; with none at all there is nothing to forecast with.
    """
    wanted = due_cutoff(session)
    have = store.latest(cutoff_by=wanted)
    if have is not None and have.cutoff == wanted:
        return have, False, ""
    try:
        params = estimate(provider, tickers, wanted, version=store.next_version(), now=now, previous=have, known=known)
    except ForecastError as e:
        if have is None:
            return None, False, f"No parameters could be estimated on data to {wanted}. {e}"
        return have, False, (
            f"This week's parameters could not be estimated on data to {wanted} ({e}) "
            f"Version {have.version}, on data to {have.cutoff}, is used instead."
        )
    try:
        store.save(params)
    except FileExistsError:
        # Another process estimated at the same moment. Its estimate stands, and this one is dropped.
        there = store.latest(cutoff_by=wanted)
        if there is not None and there.cutoff == wanted:
            return there, False, ""
        params = params.model_copy(update={"version": store.next_version()})
        store.save(params)
    return params, True, ""
