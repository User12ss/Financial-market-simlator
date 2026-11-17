"""Structured outputs of the trading pipeline.

Two kinds of model live here. A class whose name ends in `Draft` is what a
language model is asked to return: loose types, nothing the code relies on.
The class without that suffix is what the code keeps after checking the draft
against the data and the rules. Nothing downstream reads a draft.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The eleven sectors, named as the sector ETFs are in data.snapshot.MARKET_SERIES.
Sector = Literal[
    "Technology", "Healthcare", "Financials", "Energy", "Consumer Discretionary", "Consumer Staples",
    "Industrials", "Materials", "Utilities", "Real Estate", "Communication Services",
]
Stance = Literal["supportive", "neutral", "restrictive"]
RiskAppetite = Literal["risk_on", "neutral", "risk_off"]
Direction = Literal["bullish", "neutral", "bearish"]
Action = Literal["buy", "sell", "hold", "trim"]
Ownership = Literal["own", "do_not_own"]

# Figures of one ticker that a view may cite. Each maps to a value in the
# ticker's snapshot, so the code can compare what was cited with what was shown.
Metric = Literal[
    "close", "change_1d_pct", "return_5d_pct", "return_1mo_pct", "return_3mo_pct", "return_1y_pct",
    "high_52w", "low_52w", "sma20", "sma50", "sma200", "rsi14", "macd", "macd_signal", "atr14_pct",
    "trailing_pe", "forward_pe", "profit_margin_pct", "beta", "market_cap_billion",
]


class Draft(BaseModel):
    """Base of everything a model returns. NaN and infinity are refused when it is parsed."""

    model_config = ConfigDict(allow_inf_nan=False)


# ── Macro layer ──────────────────────────────────────────────────────────────

class SectorCall(BaseModel):
    sector: Sector
    tilt: Literal["overweight", "underweight"]


class DomainAssessmentDraft(Draft):
    """One macro agent's reading of the current state of its domain."""

    stance: Stance = Field(
        description="What the current state of your domain means for US equities over the next one to "
        "four weeks: supportive, neutral or restrictive"
    )
    summary: str = Field(description="What the current data says in your domain (2-3 sentences)")
    key_risk: str = Field(description="The one development in your domain most likely to hurt equities (1 sentence)")
    sector_tilts: list[SectorCall] = Field(
        description="Sectors your domain gives a clear reason to favour or avoid. An empty list is a normal answer."
    )
    confidence: float = Field(description="How sure you are of the stance, from 0 to 1")
    remark: str = Field(description="One short sentence, in your own voice, said to the desk")


class DomainAssessment(BaseModel):
    agent_id: str
    agent_name: str
    stance: Stance
    summary: str
    key_risk: str
    sector_tilts: list[SectorCall] = Field(default_factory=list)
    confidence: float
    remark: str
    data_warnings: list[str] = Field(default_factory=list)


class SectorTilt(BaseModel):
    sector: Sector
    tilt: Literal["overweight", "underweight"]
    supporters: list[str] = Field(description="Agents whose calls produced this tilt")


class MacroRegime(BaseModel):
    """The desk's macro view. Computed by code from the agents' assessments, not written by a model."""

    session_date: date
    risk_appetite: RiskAppetite
    confidence: float = Field(description="Share of the agents' confidence that stands behind risk_appetite")
    sector_tilts: list[SectorTilt] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)
    assessments: list[DomainAssessment] = Field(default_factory=list)


# ── Ticker layer ─────────────────────────────────────────────────────────────

class Citation(Draft):
    metric: Metric
    value: float = Field(description="The value exactly as shown in the data")


class TickerViewDraft(Draft):
    direction: Direction = Field(
        description="bullish: worth owning over your horizon. bearish: should not be owned. neutral: no edge."
    )
    confidence: float = Field(description="Probability that the direction is right, from 0 to 1")
    horizon_days: int = Field(description="Trading days you expect the view to play out in")
    thesis: str = Field(description="Why, weighing price action, fundamentals and headlines (2-4 sentences)")
    catalysts: str = Field(description="What could move the price within the horizon (1-2 sentences)")
    invalidation: str = Field(description="An observable condition that would prove the view wrong (1 sentence)")
    evidence: list[Citation] = Field(description="Two to five data points you relied on")
    remark: str = Field(description="One short sentence, in your own voice, said to the portfolio manager")


class CheckedCitation(Citation):
    actual: float | None = Field(description="The value in the snapshot, or None when it has none")
    matches: bool


class TickerView(BaseModel):
    ticker: str
    session_date: date
    held: bool = Field(description="Whether the desk held this ticker when the view was written")
    direction: Direction
    confidence: float
    horizon_days: int
    thesis: str
    catalysts: str
    invalidation: str
    evidence: list[CheckedCitation] = Field(default_factory=list)
    remark: str
    data_warnings: list[str] = Field(default_factory=list)


# ── Debate ───────────────────────────────────────────────────────────────────

class DebateArgumentDraft(Draft):
    argument: str = Field(description="Your case (3-5 sentences)")
    strongest_evidence: str = Field(description="The single data point that best supports your case (1 sentence)")
    remark: str = Field(description="One short sentence, in your own voice, said to the other side")


class DebateRulingDraft(Draft):
    stance: Ownership = Field(description="own: open or keep the position. do_not_own: do not buy, or exit.")
    confidence: float = Field(
        description="Probability, from 0 to 1, that the stance is right. 0.5 when the two cases balance."
    )
    invalidation: str = Field(description="An observable condition that would reverse the ruling (1 sentence)")
    reasoning: str = Field(description="Which arguments decided it and why (2-4 sentences)")
    remark: str = Field(description="One short sentence, in your own voice, said to the portfolio manager")


class DebateTurn(BaseModel):
    side: Literal["bull", "bear"]
    round: int
    argument: str
    strongest_evidence: str
    remark: str
    data_warnings: list[str] = Field(default_factory=list)


class DebateRecord(BaseModel):
    ticker: str
    trigger: Literal["top_candidate", "exit_signal"]
    turns: list[DebateTurn] = Field(default_factory=list)
    stance: Ownership
    confidence: float
    invalidation: str
    reasoning: str
    remark: str


# ── Proposals ────────────────────────────────────────────────────────────────

class ProposalDraft(Draft):
    ticker: str
    action: Action
    confidence: float = Field(description="How sure you are of this action, from 0 to 1")
    reason: str = Field(description="Why, naming the view or ruling it rests on (1-2 sentences)")


class ProposalSetDraft(Draft):
    proposals: list[ProposalDraft]
    summary: str = Field(description="The thinking behind today's proposals as a whole (2-3 sentences)")
    remark: str = Field(description="One short sentence, in your own voice, said to the desk")


class TradeProposal(BaseModel):
    ticker: str
    action: Action
    confidence: float
    reason: str
    source: Literal["portfolio_manager", "default"] = "portfolio_manager"
    status: Literal["accepted", "rejected"]
    note: str = Field(default="", description="Why it was rejected, or why a default was filled in")


# ── Shadow book ──────────────────────────────────────────────────────────────

class Holding(BaseModel):
    ticker: str
    shares: float
    entry_price: float
    entry_date: date
    last_close: float | None = None
    unrealized_pct: float | None = None
    sessions_held: int = 0


class ShadowFill(BaseModel):
    ticker: str
    # "close" is the book closing a position on its own, at the last price it
    # could trust, after the ticker's data stayed unusable for too long.
    action: Literal["buy", "sell", "trim", "close"]
    shares: float
    price: float
    trade_date: date
    proposed_session: date
    pnl_pct: float | None = Field(default=None, description="Return against the entry price, for sells and trims")


# ── Run record ───────────────────────────────────────────────────────────────

class ScreenRow(BaseModel):
    ticker: str
    passed: bool
    reason: str = ""
    rank: int | None = None
    close: float | None = None
    avg_dollar_volume_20d: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    return_3mo_pct: float | None = None


class UsageSummary(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_seconds: float = 0.0
    cost_usd: float | None = Field(default=None, description="None when the provider has no per-token price")


class Event(BaseModel):
    """One thing said or done during a run. The dashboard replays these in order."""

    seq: int
    ts: datetime
    stage: Literal["settle", "macro", "views", "debate", "proposals", "checks"]
    speaker: str
    speaker_name: str
    is_llm: bool
    addressed_to: str
    kind: str
    ticker: str | None = None
    remark: str = Field(description="For a model: its own words. For code: a plain statement of what was done.")
    payload: dict = Field(default_factory=dict)


class SignalTable(BaseModel):
    """Everything one model configuration produced for one session."""

    run_id: str
    arm: str = Field(description="The model configuration that produced these signals")
    session_date: date
    created_at: datetime
    status: Literal["ok", "degraded"]
    snapshot_id: str
    universe_size: int
    screen: list[ScreenRow] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    fills: list[ShadowFill] = Field(default_factory=list, description="Earlier proposals filled at this run's start")
    holdings: list[Holding] = Field(default_factory=list, description="The shadow book the portfolio manager saw")
    regime: MacroRegime | None = None
    regime_reused_from: date | None = None
    views: list[TickerView] = Field(default_factory=list)
    debates: list[DebateRecord] = Field(default_factory=list)
    proposals: list[TradeProposal] = Field(default_factory=list)
    pm_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    usage: UsageSummary = Field(default_factory=UsageSummary)
