"""The rules that stand between what a model returned and what the system keeps.

Models propose; this code decides. Every function here takes a draft and
returns either the checked object or the reason it was refused. Nothing is
repaired silently: a value outside its range is refused, not clamped.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Iterable

from data.models import TickerSnapshot
from models.signals import (
    CheckedCitation, DebateRecord, DebateRulingDraft, DebateTurn, DomainAssessment, DomainAssessmentDraft,
    ProposalSetDraft, TickerView, TickerViewDraft, TradeProposal,
)
from models.trading import Position
from portfolio.sizing import usable_stop
from utils.grounding import unsupported_figures

MIN_HORIZON_DAYS, MAX_HORIZON_DAYS = 1, 60
FIGURES_LISTED = 6


def _valid_confidence(value: float) -> bool:
    return math.isfinite(value) and 0.0 <= value <= 1.0


def figure_warning(text: str, sources: Iterable[str]) -> list[str]:
    """A one-line warning when `text` states figures that appear in none of `sources`."""
    missing = unsupported_figures(text, sources)
    if not missing:
        return []
    shown = ", ".join(missing[:FIGURES_LISTED]) + (", ..." if len(missing) > FIGURES_LISTED else "")
    return [f"States {len(missing)} figure(s) not found in the data it was shown: {shown}."]


# ── Macro assessments ────────────────────────────────────────────────────────

def check_assessment(
    agent_id: str, agent_name: str, draft: DomainAssessmentDraft, shown: Iterable[str],
) -> tuple[DomainAssessment | None, str]:
    """The assessment to keep, or None and the reason it was refused."""
    if not _valid_confidence(draft.confidence):
        return None, f"{agent_name}: confidence {draft.confidence} is outside 0 to 1."
    # One call per sector: the first is kept if a model repeats a sector.
    tilts, seen = [], set()
    for call in draft.sector_tilts:
        if call.sector not in seen:
            seen.add(call.sector)
            tilts.append(call)
    return DomainAssessment(
        agent_id=agent_id, agent_name=agent_name, stance=draft.stance, summary=draft.summary,
        key_risk=draft.key_risk, sector_tilts=tilts, confidence=draft.confidence, remark=draft.remark,
        data_warnings=figure_warning(f"{draft.summary}\n{draft.key_risk}", shown),
    ), ""


# ── Ticker views ─────────────────────────────────────────────────────────────

def metric_values(item: TickerSnapshot) -> dict[str, float | None]:
    """The snapshot's value for every metric a view may cite."""
    price, tech, fund = item.price, item.technicals, item.fundamentals
    return {
        "close": price.close if price else None,
        "change_1d_pct": price.change_1d_pct if price else None,
        "return_5d_pct": price.return_5d_pct if price else None,
        "return_1mo_pct": price.return_1mo_pct if price else None,
        "return_3mo_pct": price.return_3mo_pct if price else None,
        "return_1y_pct": price.return_1y_pct if price else None,
        "high_52w": price.high_52w if price else None,
        "low_52w": price.low_52w if price else None,
        "sma20": tech.sma20 if tech else None,
        "sma50": tech.sma50 if tech else None,
        "sma200": tech.sma200 if tech else None,
        "rsi14": tech.rsi14 if tech else None,
        "macd": tech.macd if tech else None,
        "macd_signal": tech.macd_signal if tech else None,
        "atr14_pct": tech.atr14_pct if tech else None,
        "trailing_pe": fund.trailing_pe if fund else None,
        "forward_pe": fund.forward_pe if fund else None,
        "profit_margin_pct": fund.profit_margin * 100 if fund and fund.profit_margin is not None else None,
        "beta": fund.beta if fund else None,
        "market_cap_billion": fund.market_cap / 1e9 if fund and fund.market_cap else None,
    }


def _matches(cited: float, actual: float | None) -> bool:
    """Whether a cited value is the snapshot's value, allowing for rounding."""
    if actual is None:
        return False
    if abs(cited - actual) <= max(0.02 * abs(actual), 0.05):
        return True
    # "RSI 54" for 54.3: a whole number is accepted when the value rounds to it.
    return cited == round(cited) and round(actual) == cited


def check_view(
    ticker: str, session: date, held: bool, draft: TickerViewDraft, item: TickerSnapshot, shown: Iterable[str],
) -> tuple[TickerView | None, str]:
    """The view to keep, or None and the reason it was refused."""
    if not _valid_confidence(draft.confidence):
        return None, f"{ticker}: confidence {draft.confidence} is outside 0 to 1."
    if not MIN_HORIZON_DAYS <= draft.horizon_days <= MAX_HORIZON_DAYS:
        return None, (
            f"{ticker}: horizon of {draft.horizon_days} days is outside "
            f"{MIN_HORIZON_DAYS} to {MAX_HORIZON_DAYS}."
        )

    if any(not math.isfinite(c.value) for c in draft.evidence):
        return None, f"{ticker}: a cited value is not a number."

    actual = metric_values(item)
    evidence = [
        CheckedCitation(metric=c.metric, value=c.value, actual=actual[c.metric],
                        matches=_matches(c.value, actual[c.metric]))
        for c in draft.evidence
    ]
    warnings = []
    wrong = [c for c in evidence if not c.matches]
    if wrong:
        details = ", ".join(
            f"{c.metric} cited as {c.value:g}, data has {'no value' if c.actual is None else format(c.actual, 'g')}"
            for c in wrong
        )
        warnings.append(f"{len(wrong)} of {len(evidence)} cited data point(s) do not match the data: {details}.")
    # A stop is for opening a position: it means nothing on a view that is not
    # bullish, and a position already held keeps the stop it was opened with.
    stop, stop_refused = None, []
    if draft.stop_price is not None and draft.direction == "bullish" and not held:
        stop, refusal = usable_stop(
            item.price.close if item.price else None, item.technicals.atr14 if item.technicals else None,
            draft.stop_price,
        )
        if stop is None:
            stop_refused = [f"The proposed stop of {draft.stop_price:,.2f} is not used: {refusal}"]

    # The invalidation line is left out: it names a level to watch, which is
    # a condition the analyst sets, not a claim about the data. The reason for
    # a stop is checked, because it says which level in the data the stop sits
    # under; the stop itself is the analyst's own number and may be stated.
    stated, sources = f"{draft.thesis}\n{draft.catalysts}", list(shown)
    if stop is not None:
        stated, sources = f"{stated}\n{draft.stop_reason}", sources + [f"{stop:.2f}"]
    warnings += figure_warning(stated, sources) + stop_refused

    return TickerView(
        ticker=ticker, session_date=session, held=held, direction=draft.direction,
        confidence=draft.confidence, horizon_days=draft.horizon_days, thesis=draft.thesis,
        catalysts=draft.catalysts, invalidation=draft.invalidation, proposed_stop=stop,
        stop_reason=draft.stop_reason if stop is not None else "", evidence=evidence,
        remark=draft.remark, data_warnings=warnings,
    ), ""


# ── Debate rulings ───────────────────────────────────────────────────────────

def check_ruling(
    ticker: str, trigger: str, turns: list[DebateTurn], draft: DebateRulingDraft,
) -> tuple[DebateRecord | None, str]:
    if not _valid_confidence(draft.confidence):
        return None, f"{ticker}: the judge's confidence {draft.confidence} is outside 0 to 1."
    return DebateRecord(
        ticker=ticker, trigger=trigger, turns=turns, stance=draft.stance, confidence=draft.confidence,
        invalidation=draft.invalidation, reasoning=draft.reasoning, remark=draft.remark,
    ), ""


# ── Proposals ────────────────────────────────────────────────────────────────

def check_proposals(
    draft: ProposalSetDraft | None,
    *,
    candidates: Iterable[str],
    views: dict[str, TickerView],
    rulings: dict[str, DebateRecord],
    holdings: dict[str, Position],
    waiting: dict[str, str],
    regime_available: bool,
    max_positions: int,
) -> list[TradeProposal]:
    """Accept or reject each proposal, and give every holding a decision.

    `waiting` holds orders from earlier sessions that have not filled yet
    (ticker to action). Only the first proposal for a ticker counts. It is
    rejected, with the reason recorded, when:

    - the ticker is not one of today's candidates or holdings;
    - the confidence is not a number from 0 to 1;
    - it is a buy and the ticker is already held or already has a buy waiting,
      has no checked analyst view, has a view that is not bullish, was ruled
      against by the judge, or there is no current macro view;
    - it is a sell, trim or hold of a ticker that is not held;
    - it is a sell or trim of a holding that already has an exit waiting.

    Accepted buys are then taken in order of confidence until the book is
    full. Positions being sold today still count as held, so the book can
    never end up over its limit.

    A holding left without an accepted decision is held, with one exception.
    Where the manager answered and its answer says nothing of the holding at
    all, the analyst's view of it is bearish and the judge ruled against
    owning it, the holding is sold: two voices have argued for an exit and
    nobody for keeping it, so keeping it would be the one outcome no one
    argued for. A manager that named the holding, even in a proposal that
    was turned back, has spoken, and one whose call failed has decided
    nothing: in both cases it is held.
    """
    allowed = set(candidates) | set(holdings)
    results: list[TradeProposal] = []
    mentioned: set[str] = set()
    decided: set[str] = set()
    buys: list[TradeProposal] = []

    def reason_to_reject(ticker: str, item) -> str:
        if ticker not in allowed:
            return "Not one of today's candidates or holdings."
        if not _valid_confidence(item.confidence):
            return f"Confidence {item.confidence} is not a number from 0 to 1."
        held = ticker in holdings
        if item.action == "buy":
            if held:
                return "Already held. A position is sized once, when it is opened."
            if waiting.get(ticker) == "buy":
                return "A buy from an earlier session is still waiting to fill."
            if ticker not in views:
                return "No checked analyst view of this ticker today."
            if views[ticker].direction != "bullish":
                return f"The analyst's view of this ticker is {views[ticker].direction}, not bullish."
            if ticker in rulings and rulings[ticker].stance == "do_not_own":
                return "The judge ruled against owning it."
            if not regime_available:
                return "No current macro view, so no new positions."
            return ""
        if not held:
            return f"Cannot {item.action} a ticker that is not held."
        if item.action != "hold" and ticker in waiting:
            return f"A {waiting[ticker]} from an earlier session is still waiting to fill."
        return ""

    for item in (draft.proposals if draft else []):
        ticker = item.ticker.strip().upper()
        if ticker in mentioned:
            note = "A decision for this ticker was already given. Only the first counts."
        else:
            mentioned.add(ticker)
            note = reason_to_reject(ticker, item)
        proposal = TradeProposal(
            ticker=ticker, action=item.action,
            # A value that is not a number cannot be stored; the note says what was sent.
            confidence=item.confidence if math.isfinite(item.confidence) else 0.0,
            reason=item.reason, status="rejected" if note else "accepted", note=note,
        )
        results.append(proposal)
        if not note:
            decided.add(ticker)
            if item.action == "buy":
                buys.append(proposal)

    earlier_buys = sum(1 for ticker, action in waiting.items() if action == "buy" and ticker not in holdings)
    room = max_positions - len(holdings) - earlier_buys
    for proposal in sorted(buys, key=lambda p: (-p.confidence, p.ticker)):
        if room > 0:
            room -= 1
        else:
            proposal.status = "rejected"
            proposal.note = f"The book is limited to {max_positions} positions."

    def both_say_exit(ticker: str) -> bool:
        view, ruling = views.get(ticker), rulings.get(ticker)
        return (view is not None and view.direction == "bearish"
                and ruling is not None and ruling.stance == "do_not_own")

    for ticker in sorted(set(holdings) - decided):
        if draft is not None and ticker not in mentioned and ticker not in waiting and both_say_exit(ticker):
            results.append(TradeProposal(
                ticker=ticker, action="sell", confidence=0.0, source="default", status="accepted",
                reason="No decision from the portfolio manager.",
                note="Sold by default: the analyst's view is bearish and the judge ruled against owning it.",
            ))
            continue
        results.append(TradeProposal(
            ticker=ticker, action="hold", confidence=0.0, source="default", status="accepted",
            reason="No valid decision from the portfolio manager.",
            note=(f"A {waiting[ticker]} from an earlier session is still waiting to fill."
                  if ticker in waiting else "Held by default."),
        ))
    return results
