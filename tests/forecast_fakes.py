"""A made-up market with known parameters, to check that the forecast engine finds what was put in.

The index follows two states. Each sector fund moves with the index plus a
move of its own; each name moves with the index, with its sector fund's own
move, and with noise of its own. Report days and jumps are planted on known
dates, so a test can say exactly which days must be left out of what.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from data.market_calendar import trading_days
from data.models import Fundamentals
from data.providers import BAR_COLUMNS, DataError
from data.snapshot import MARKET_SERIES
from execution.paper import SECTOR_FUNDS
from forecast.regime import RegimeModel, simulate

NEW_YORK = ZoneInfo("America/New_York")
END = date(2026, 11, 20)            # a Friday: the cut-off a session of the week after is forecast from
TRUE_REGIME = RegimeModel(
    means=[0.0005, -0.0006], vols=[0.007, 0.019],
    transition=[[0.98, 0.02], [0.06, 0.94]],
)
SECTOR_NAMES = {"XLK": "Technology", "XLV": "Healthcare", "XLP": "Consumer Defensive"}
FUND_VOL = 0.008                    # a sector fund's own daily move, beyond the index's


@dataclass
class Name:
    beta: float = 1.0
    loading: float = 0.0
    fund: str = ""                              # its sector fund, "" for none
    vol: float = 0.012                          # own daily noise
    turbulent: float = 1.0                      # own noise is this many times larger in the index's turbulent state
    drift: float = 0.0
    quote_type: str = "EQUITY"
    reports: list[datetime] = field(default_factory=list)     # when it reported
    report_moves: dict[date, float] = field(default_factory=dict)   # the session that reacted: its own move
    jumps: dict[date, float] = field(default_factory=dict)


def at(day: date, hour: int = 0, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=NEW_YORK)


class MadeUpMarket:
    def __init__(self, names: dict[str, Name] | None = None, end: date = END, days: int = 1250,
                 market_days: int = 2600, seed: int = 9, fund_turbulent: float = 1.0) -> None:
        self.end = end
        self.sessions = trading_days(end - timedelta(days=int(market_days * 1.6)), end)[-(market_days + 1):]
        self.days = days
        self.names = names or {}
        self.missing: set[str] = set()
        self.no_reports: set[str] = set()       # the source cannot say when these reported
        self.no_facts: set[str] = set()
        self.fail = False
        self.asked: dict[str, list] = {"bars": [], "ends": [], "facts": [], "reports": []}
        rng = np.random.default_rng(seed)
        self.states, index = simulate(TRUE_REGIME, market_days, seed=seed)
        self.returns: dict[str, np.ndarray] = {"^GSPC": index}
        self.own: dict[str, np.ndarray] = {}
        for fund in sorted(set(SECTOR_NAMES) | {name.fund for name in self.names.values() if name.fund}):
            self.own[fund] = rng.normal(0.0, FUND_VOL, market_days) * np.where(self.states == 1, fund_turbulent, 1.0)
            self.returns[fund] = index + self.own[fund]
        dates = self.sessions[1:]
        for ticker in sorted(self.names):
            name = self.names[ticker]
            noise = rng.normal(0.0, name.vol, market_days) * np.where(self.states == 1, name.turbulent, 1.0)
            for day, move in {**name.report_moves, **name.jumps}.items():
                noise[dates.index(day)] = move
            self.own[ticker] = noise
            self.returns[ticker] = (name.drift + name.beta * index
                                    + (name.loading * self.own[name.fund] if name.fund else 0.0) + noise)
        # Every other series a snapshot asks for, so that a run on this market has nothing missing.
        # Each draws from a stream of its own: what was generated above is the same with or without them.
        for _, _, symbol in MARKET_SERIES:
            if symbol in self.returns:
                continue
            extra = np.random.default_rng([seed, zlib.crc32(symbol.encode())])
            if symbol in SECTOR_FUNDS:
                self.own[symbol] = (extra.normal(0.0, FUND_VOL, market_days)
                                    * np.where(self.states == 1, fund_turbulent, 1.0))
                self.returns[symbol] = index + self.own[symbol]
            else:
                self.returns[symbol] = extra.normal(0.0, 0.01, market_days)

    def session_before(self, day: date, back: int = 0) -> date:
        """The last session on or before `day`, or `back` sessions before that."""
        known = [session for session in self.sessions if session <= day]
        return known[-1 - back]

    def frame(self, symbol: str, end: date) -> pd.DataFrame:
        returns = self.returns[symbol]
        length = len(returns) if symbol == "^GSPC" else self.days
        sessions = self.sessions[-(length + 1):]
        close = 100.0 * np.exp(np.concatenate([[0.0], np.cumsum(returns[-length:])]))
        opens = np.concatenate([[close[0]], close[:-1]])
        frame = pd.DataFrame({
            "open": opens, "high": np.maximum(opens, close) * 1.002, "low": np.minimum(opens, close) * 0.998,
            "close": close, "adj_close": close, "volume": 5_000_000.0, "dividends": 0.0, "splits": 0.0,
        }, index=sessions, columns=BAR_COLUMNS)
        return frame[frame.index <= end]

    # ── The provider's interface ─────────────────────────────────────────

    def daily_bars(self, symbols, start, end):
        self.asked["bars"].append(list(symbols))
        self.asked["ends"].append(end)
        if self.fail:
            raise DataError("Yahoo Finance price download failed: scripted outage")
        frames = {}
        for symbol in symbols:
            if symbol in self.missing or symbol not in self.returns:
                continue
            frame = self.frame(symbol, end)
            frames[symbol] = frame[frame.index >= start]
        return frames

    def fundamentals(self, symbol):
        self.asked["facts"].append(symbol)
        if symbol in self.no_facts or symbol not in self.names:
            raise DataError(f"Yahoo Finance returned no fundamentals for {symbol}.")
        name = self.names[symbol]
        return Fundamentals(name=f"{symbol} Corp", quote_type=name.quote_type, sector=SECTOR_NAMES.get(name.fund, ""))

    def next_earnings(self, symbol, after):
        coming = [stamp.date() for stamp in self.names[symbol].reports if stamp.date() > after] if symbol in self.names else []
        return min(coming) if coming else None

    def earnings_reports(self, symbol):
        self.asked["reports"].append(symbol)
        if symbol in self.no_reports:
            raise DataError(f"Yahoo Finance earnings dates failed for {symbol}: scripted outage")
        return sorted(self.names[symbol].reports) if symbol in self.names else []


# ── Hand-made parameters, for checking the simulation against what can be worked out ──

MONDAY = date(2026, 11, 23)         # the first session forecast from parameters estimated to END


def normal_pool(count: int = 2001) -> list[float]:
    """A sample that is as normal as a sample can be: the quantiles of the standard normal, evenly spaced."""
    from statistics import NormalDist

    normal = NormalDist()
    return [normal.inv_cdf((index + 0.5) / count) for index in range(count)]


def plain(ticker: str, **changes):
    """A name with nothing special about it: beta 1, no sector, no drift, no jumps, no reports."""
    from forecast.params import SeriesParams

    fields = dict(ticker=ticker, first_day=date(2021, 11, 22), days=1250, beta=1.0, drift=0.0, raw_drift=0.0,
                  vol=0.01, residuals=normal_pool(), jump_rate=0.0, reports_known=True)
    return SeriesParams(**{**fields, **changes})


def hand_params(series: list, regime_model: RegimeModel | None = None, state_vol: list[float] | None = None,
                state_at_cutoff: list[float] | None = None, cutoff: date = END, version: int = 1):
    from datetime import timezone

    from forecast.params import Params, Settings

    model = regime_model or RegimeModel(means=[0.0], vols=[0.01], transition=[[1.0]])
    return Params(
        version=version, cutoff=cutoff, created_at=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
        market_days=5000, market_first_day=date(2006, 11, 20), regime=model,
        state_at_cutoff=state_at_cutoff or [1.0] + [0.0] * (model.states - 1), log_likelihood=0.0,
        state_vol=state_vol or [1.0] * model.states, series={item.ticker: item for item in series},
        settings=Settings(states=model.states, market_years=20, history_years=5, ewma_lambda=0.94, jump_sigmas=4.0,
                          min_earnings=8, drift_shrink_days=5040),
    )


def noise(vol: float, seed: int, end: date = MONDAY, days: int = 160) -> pd.Series:
    """Daily returns for the sessions up to `end`: normal, with that volatility."""
    sessions = trading_days(end - timedelta(days=days * 2), end)[-days:]
    return pd.Series(np.random.default_rng(seed).normal(0.0, vol, days), index=sessions)


# ── A desk on the made-up market ─────────────────────────────────────────────

FAR = date(2026, 12, 31)            # the made-up market is generated to here, and shown up to the day asked for


def rising_names() -> dict[str, Name]:
    """Four names. Three climb, so the screen picks them; one reports after the close on 2 December."""
    blank = MadeUpMarket(end=FAR)
    reports = [at(blank.session_before(date(2026, 9, 2), back=63 * index), 16, 5) for index in range(12)][::-1]
    moves = {blank.sessions[blank.sessions.index(stamp.date()) + 1]: (0.06 if index % 2 else -0.06)
             for index, stamp in enumerate(reports)}
    # Low betas and a strong drift of their own: in an uptrend whatever the made-up index is doing.
    return {
        "RISE": Name(beta=0.5, fund="XLK", loading=0.6, vol=0.010, drift=0.0032),
        "GAIN": Name(beta=0.4, fund="XLP", loading=0.5, vol=0.008, drift=0.0028),
        "CLMB": Name(beta=0.5, vol=0.009, drift=0.0030, reports=reports + [at(date(2026, 12, 2), 16, 5)],
                     report_moves=moves),
        "SINK": Name(beta=1.0, vol=0.012, drift=-0.0030),
    }


def forecast_desk(tmp_path, moment: datetime, names: dict[str, Name] | None = None, market=None, llm=None):
    """A desk whose market has noise in it, so the forecast engine has something to estimate."""
    from data.ledger import Ledger
    from data.snapshot import SnapshotBuilder
    from journal.store import Journal
    from models.signals import ProposalSetDraft
    from pipeline.daily import Arm
    from pipeline.desk import Desk
    from tests.desk_fakes import ARM, ScriptedNews, desk_llm
    from tests.pipeline_fakes import Clock, SteadyMacro, proposals

    market = market or MadeUpMarket(names or rising_names(), end=FAR)
    clock, news = Clock(moment), ScriptedNews()
    if llm is None:
        llm = desk_llm()
        llm.replies[ProposalSetDraft] = proposals(("RISE", "buy", 0.7), ("GAIN", "buy", 0.6))
    desk = Desk(
        Arm(ARM, llm), journal=Journal(tmp_path / "journal.sqlite3"), ledger=Ledger(tmp_path / "ledger.sqlite3"),
        builder=SnapshotBuilder(market=market, macro=SteadyMacro(), news=news, now=clock),
        universe=sorted(market.names), runs_dir=tmp_path / "runs", snapshot_dir=tmp_path / "snapshots", now=clock,
        forecast_dir=tmp_path / "forecast",
    )
    desk.clock, desk.news, desk.llm, desk.market = clock, news, llm, market
    return desk
