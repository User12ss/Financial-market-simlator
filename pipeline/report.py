"""Text forms of a run: the briefings agents read and the Markdown signal table people read."""

from __future__ import annotations

from models.signals import DebateRecord, SignalTable, TickerView
from models.trading import Fill, Position


def _cell(text: str) -> str:
    """Text made safe for one Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def render_view(view: TickerView) -> str:
    text = (
        f"{view.ticker} [{'held' if view.held else 'candidate'}]: {view.direction}, confidence "
        f"{view.confidence:.2f}, horizon {view.horizon_days} trading days. Thesis: {view.thesis} "
        f"Catalysts: {view.catalysts} Invalidation: {view.invalidation}"
    )
    if view.proposed_stop is not None:
        text += f" Proposed stop for a new position: {view.proposed_stop:,.2f}. {view.stop_reason}".rstrip()
    if view.data_warnings:
        text += f" (Automatic data check: {' '.join(view.data_warnings)})"
    return text


def _stop_cell(view: TickerView) -> str:
    if view.proposed_stop is None:
        return "none"
    return f"{view.proposed_stop:,.2f}. {view.stop_reason}".rstrip()


def render_holding(position: Position) -> str:
    stop = f", stop at {position.stop_price:,.2f}" if position.stop_price is not None else ", no stop"
    if position.last_close is None:
        return (
            f"{position.ticker}: {position.shares} shares bought {position.entry_date} at "
            f"{position.avg_cost:,.2f}{stop}; no usable price data today"
        )
    return (
        f"{position.ticker}: {position.shares} shares bought {position.entry_date} at {position.avg_cost:,.2f}, "
        f"last close {position.last_close:,.2f}, unrealized {position.unrealized_pct:+.2f}%{stop}, "
        f"held {position.sessions_held} session(s)"
    )


def describe_fill(fill: Fill) -> str:
    result = "" if fill.pnl_pct is None else f", {fill.pnl_pct:+.2f}% against cost"
    how = {
        "market": "at the open", "limit": "at its limit", "stop": "by its stop",
        "close": "by the broker, at the last close it could be matched to",
    }[fill.kind]
    text = f"{fill.side.capitalize()} {fill.shares} {fill.ticker} {how} on {fill.fill_date}, {fill.price:,.2f}{result}."
    return f"{text} {fill.note}".strip()


def render_ruling(record: DebateRecord) -> str:
    return (
        f"{record.ticker}: {record.stance.replace('_', ' ')}, confidence {record.confidence:.2f}. "
        f"{record.reasoning} Invalidation: {record.invalidation}"
    )


def to_markdown(table: SignalTable) -> str:
    """The signal table as a document. Every word of analysis in it is a model's own output."""
    out = [
        f"# Signals for {table.session_date}",
        "",
        f"- Model configuration: `{table.arm}`",
        f"- Run: `{table.run_id}`, status **{table.status}**",
        f"- Data snapshot: `{table.snapshot_id}`",
        f"- Universe: {table.universe_size} tickers screened, {len(table.candidates)} candidate(s)",
        "",
        "Paper trading: every order here is simulated. Nothing was sent to a real broker, and no real money "
        "is involved. These are model outputs, not financial advice.",
        "",
    ]
    if table.halted:
        out += [f"**No order was placed.** {table.halted}", ""]

    out += ["## Proposals", ""]
    if table.proposals:
        out += ["| Ticker | Action | Confidence | Result | Reason | Note |", "|---|---|---|---|---|---|"]
        out += [
            f"| {p.ticker} | {p.action} | {p.confidence:.2f} | {p.status} | {_cell(p.reason)} | {_cell(p.note)} |"
            for p in table.proposals
        ]
    else:
        out.append("No proposals.")
    if table.pm_summary:
        out += ["", f"Portfolio manager: {table.pm_summary}"]
    out.append("")

    out += ["## Orders", ""]
    if table.risk:
        out += ["| Ticker | Side | Sized | Allowed | Verdict | Why |", "|---|---|---|---|---|---|"]
        out += [
            f"| {d.ticker} | {d.side} | {d.requested_shares} | {d.approved_shares} | {d.verdict} | "
            f"{_cell(' '.join(d.reasons))} |"
            for d in table.risk
        ]
        proposed = {v.ticker: v.proposed_stop for v in table.views}
        stops = [
            f"{o.ticker} at {o.protective_stop:,.2f} "
            + ("(the analyst's level)" if proposed.get(o.ticker) == o.protective_stop else "(set by the sizing rule)")
            for o in table.orders if o.protective_stop is not None
        ]
        if stops:
            out += ["", f"Protective stops attached on fill: {', '.join(stops)}."]
        out += ["", "Orders that passed fill at the next session's open."]
    else:
        out.append("No orders.")
    out.append("")

    out += ["## Paper account", ""]
    if table.account:
        account = table.account
        change = "n/a" if account.day_change_pct is None else f"{account.day_change_pct:+.2f}%"
        out.append(
            f"Equity {account.equity:,.2f} (cash {account.cash:,.2f}, positions {account.positions_value:,.2f}), "
            f"{change} on the day, {account.drawdown_pct:.2f}% below its high of {account.high_water:,.2f}. "
            f"Started with {account.initial_cash:,.2f}."
        )
        out.append("")
    if table.fills:
        out.append("Fills since the last run:")
        out += [f"- {describe_fill(fill)}" for fill in table.fills]
        out.append("")
    if table.holdings:
        out.append("Positions the portfolio manager saw:")
        out += [f"- {render_holding(position)}" for position in table.holdings]
    else:
        out.append("No positions.")
    out.append("")

    out += ["## Macro view", ""]
    if table.regime:
        regime = table.regime
        formed = f", reused from {table.regime_reused_from}" if table.regime_reused_from else ""
        out.append(
            f"Risk appetite **{regime.risk_appetite.replace('_', '-')}**, confidence {regime.confidence:.2f} "
            f"(formed {regime.session_date}{formed}). Computed from the agents' stances below."
        )
        for tilt in regime.sector_tilts:
            out.append(f"- {tilt.tilt.capitalize()} {tilt.sector} ({', '.join(tilt.supporters)})")
        out += ["", "| Agent | Stance | Confidence | Summary | Key risk |", "|---|---|---|---|---|"]
        out += [
            f"| {a.agent_name} | {a.stance} | {a.confidence:.2f} | {_cell(a.summary)} | {_cell(a.key_risk)} |"
            for a in regime.assessments
        ]
    else:
        out.append("No macro view today, so no new positions were allowed.")
    out.append("")

    out += ["## Analyst views", ""]
    if table.views:
        out += [
            "| Ticker | Held | Direction | Confidence | Horizon | Thesis | Invalidation | Proposed stop | Data check |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {v.ticker} | {'yes' if v.held else 'no'} | {v.direction} | {v.confidence:.2f} | "
            f"{v.horizon_days}d | {_cell(v.thesis)} | {_cell(v.invalidation)} | {_cell(_stop_cell(v))} | "
            f"{_cell(' '.join(v.data_warnings)) or 'clean'} |"
            for v in table.views
        ]
    else:
        out.append("No views.")
    out.append("")

    if table.debates:
        out += ["## Debates", ""]
        for record in table.debates:
            out.append(f"### {record.ticker} ({record.trigger.replace('_', ' ')})")
            out.append("")
            for turn in record.turns:
                out.append(f"- **{turn.side.capitalize()}, round {turn.round}:** {turn.argument}")
                out += [f"  - Data check: {warning}" for warning in turn.data_warnings]
            out.append(
                f"- **Judge:** {record.stance.replace('_', ' ')}, confidence {record.confidence:.2f}. "
                f"{record.reasoning} Invalidation: {record.invalidation}"
            )
            out.append("")

    out += ["## Candidate screen", ""]
    passed = sorted((r for r in table.screen if r.passed), key=lambda r: r.rank)
    if passed:
        out += ["| Rank | Ticker | Close | 3-month return |", "|---|---|---|---|"]
        out += [f"| {r.rank} | {r.ticker} | {r.close:,.2f} | {r.return_3mo_pct:+.2f}% |" for r in passed]
    else:
        out.append("No ticker passed the screen.")
    out.append("")

    if table.warnings:
        out += ["## Warnings", ""] + [f"- {warning}" for warning in table.warnings] + [""]

    usage = table.usage
    cost = "no per-token cost (local model)" if usage.cost_usd is None else f"about ${usage.cost_usd:.4f}"
    out += [
        "## Cost", "",
        f"{usage.calls} model calls, {usage.input_tokens:,} input and {usage.output_tokens:,} output tokens, "
        f"{usage.model_seconds:.0f}s of model time, {cost}.",
        "",
    ]
    return "\n".join(out)
