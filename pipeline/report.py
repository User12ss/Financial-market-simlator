"""Text forms of a run: the briefings agents read and the Markdown signal table people read."""

from __future__ import annotations

from models.signals import DebateRecord, Holding, SignalTable, TickerView


def _cell(text: str) -> str:
    """Text made safe for one Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def render_view(view: TickerView) -> str:
    text = (
        f"{view.ticker} [{'held' if view.held else 'candidate'}]: {view.direction}, confidence "
        f"{view.confidence:.2f}, horizon {view.horizon_days} trading days. Thesis: {view.thesis} "
        f"Catalysts: {view.catalysts} Invalidation: {view.invalidation}"
    )
    if view.data_warnings:
        text += f" (Automatic data check: {' '.join(view.data_warnings)})"
    return text


def render_holding(holding: Holding) -> str:
    if holding.last_close is None:
        return (
            f"{holding.ticker}: entered {holding.entry_date} at {holding.entry_price:,.2f}; "
            "no usable price data today"
        )
    return (
        f"{holding.ticker}: entered {holding.entry_date} at {holding.entry_price:,.2f}, last close "
        f"{holding.last_close:,.2f}, unrealized {holding.unrealized_pct:+.2f}%, "
        f"held {holding.sessions_held} session(s)"
    )


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
        "Shadow mode: nothing here was sent to a broker. These are model outputs, not financial advice.",
        "",
    ]

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

    out += ["## Shadow book", ""]
    if table.fills:
        out.append("Filled or closed at this run's start:")
        out += [
            f"- {'closed by the book,' if f.action == 'close' else f.action} {f.ticker}: "
            f"{f.shares:,.4f} shares at {f.price:,.2f} on {f.trade_date}"
            + (f" ({f.pnl_pct:+.2f}% against entry)" if f.pnl_pct is not None else "")
            for f in table.fills
        ]
        out.append("")
    if table.holdings:
        out.append("Positions the portfolio manager saw:")
        out += [f"- {render_holding(h)}" for h in table.holdings]
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
            "| Ticker | Held | Direction | Confidence | Horizon | Thesis | Invalidation | Data check |",
            "|---|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {v.ticker} | {'yes' if v.held else 'no'} | {v.direction} | {v.confidence:.2f} | "
            f"{v.horizon_days}d | {_cell(v.thesis)} | {_cell(v.invalidation)} | "
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
