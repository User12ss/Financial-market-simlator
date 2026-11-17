"""The evaluation as a page someone can read. The charts come with the dashboard."""

from __future__ import annotations

import config
from evaluation.trades import how_it_ended
from models.evaluation import ArmEvaluation, Curve, Evaluation, Slice, Tally, Trade


def _pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+,.2f}"


def _tally_rows(tallies: list[Tally]) -> list[str]:
    return [
        f"| {tally.label} | {tally.views} | {_rate(tally.hit_rate)} | {_rate(tally.beat_rate)} | "
        f"{_pct(tally.mean_edge_pct)} | {'n/a' if tally.mean_confidence is None else format(tally.mean_confidence, '.2f')} | "
        f"{'n/a' if tally.margin is None else '±' + format(tally.margin * 100, '.0f')} |"
        for tally in tallies
    ]


def _tally_table(title: str, tallies: list[Tally], benchmark: str) -> list[str]:
    if not tallies:
        return []
    return [
        f"**{title}**", "",
        f"| Group | Views | Direction right | Beat {benchmark} | Mean edge | Mean confidence | Margin, points |",
        "|---|---|---|---|---|---|---|",
        *_tally_rows(tallies), "",
    ]


def _curve_row(curve: Curve) -> str:
    end = f"{curve.points[-1].equity:,.2f}" if curve.points else "n/a"
    return f"| {curve.name} | {_pct(curve.return_pct)} | {_pct(curve.max_drawdown_pct, signed=False)} | {end} |"


def _trade_row(trade: Trade) -> str:
    if trade.status == "closed":
        ended = f"{how_it_ended(trade)} {trade.exits[-1].exit_date}"
    else:
        ended = f"still open, {trade.open_shares} shares"
    held = "n/a" if trade.sessions_held is None else str(trade.sessions_held)
    return (
        f"| {trade.ticker} | {trade.entry_date} | {trade.shares} | {trade.entry_price:,.2f} | {ended} | {held} | "
        f"{_usd(trade.realized_usd)} | {_usd(trade.unrealized_usd)} |"
    )


def _slices(title: str, slices: list[Slice]) -> list[str]:
    if not slices:
        return []
    parts = ", ".join(f"{item.label} {_usd(item.pnl_usd)} ({item.trades})" for item in slices)
    return [f"- {title}: {parts}"]


def _arm(arm: ArmEvaluation, benchmark: str) -> list[str]:
    out = [f"## {arm.arm}", ""]
    if not arm.sessions_run:
        return out + ["No finished run to evaluate.", ""]
    out += [f"{arm.sessions_run} session(s) run, from {arm.first_session} to {arm.last_session}.", ""]

    if arm.notes:
        out += ["### What these numbers cannot say yet", ""] + [f"- {note}" for note in arm.notes] + [""]

    out += ["### Account and benchmarks", ""]
    drawn = [curve for curve in arm.curves if curve.points]
    if drawn:
        out += ["| | Return | Largest fall from a high | Ends at |", "|---|---|---|---|"]
        out += [_curve_row(curve) for curve in drawn] + [""]
        out += [f"- {curve.name}: {curve.description}" for curve in drawn] + [""]
        out += [
            "The two benchmarks are fully invested from the first day. The account never is: it keeps cash and "
            "opens a few positions a day. They are references, not like-for-like rivals.", "",
        ]
    else:
        out += ["The paper account has no recorded equity yet.", ""]

    card = arm.scorecard
    out += ["### Views", ""]
    out += [
        f"{card.scored} of {card.total} view(s) scored; {card.pending} not yet due, {card.unpriced} without a price. "
        f"{card.neutral} of the scored views were neutral and took no side.", "",
        "A view is measured from the open of the session after it was written to the close that ends its own "
        f"horizon, dividends included. \"Edge\" is its return less {benchmark}'s, with the sign turned for a bearish "
        "view, so that a positive number always means the view helped.", "",
    ]
    if card.overall:
        out += _tally_table("All views that took a side", [card.overall], benchmark)
        out += _tally_table(
            "By confidence. If the confidence means anything, the surer views are right more often",
            card.by_confidence, benchmark,
        )
        out += _tally_table("By direction", card.by_direction, benchmark)
        out += _tally_table("By sector", card.by_sector, benchmark)
        out += _tally_table("By the desk's macro view that day", card.by_regime, benchmark)
        out += _tally_table("Candidates against positions already held", card.by_position, benchmark)
    else:
        out += ["No view that took a side has reached the end of its horizon yet.", ""]

    trades = arm.trades
    out += ["### Trades", ""]
    if trades.trades:
        out += [
            f"{trades.closed} closed ({trades.winners} made money, {trades.losers} lost, {trades.stopped_out} "
            f"stopped out; win rate {_rate(trades.win_rate)}), {trades.open} open. Realized "
            f"{_usd(trades.realized_usd)}, unrealized {_usd(trades.unrealized_usd)}. Average win "
            f"{_usd(trades.average_win_usd)}, average loss {_usd(trades.average_loss_usd)}. Dividends are paid to "
            "the account as cash and are not in these figures.", "",
            "| Ticker | Bought | Shares | Cost | Ended | Sessions held | Realized | Unrealized |",
            "|---|---|---|---|---|---|---|---|",
            *[_trade_row(trade) for trade in trades.trades], "",
            "Where the profit and loss came from, realized and unrealized together (trades in brackets):", "",
            *_slices("By ticker", trades.by_ticker),
            *_slices("By sector", trades.by_sector),
            *_slices("By how it ended", trades.by_exit), "",
        ]
    else:
        out += ["The account has not traded yet.", ""]

    random = arm.random
    out += ["### Against random picks from the same candidates", ""]
    left_out = (
        f"{random.skipped} buy(s) were left out: no candidate list for the day, no usable prices, or the pick "
        "itself could not be replayed under today's limits."
    )
    if random.percentile is None:
        out += ["Nothing to compare yet: the account has no buy that can be replayed.", ""]
        if random.skipped:
            out += [left_out, ""]
    else:
        quantiles = random.quantiles_usd
        out += [
            f"Each of the account's {random.entries} buy(s) was replaced {random.simulations:,} times by a ticker "
            "drawn at random from that day's candidates the analyst gave a view on, the only ones the pipeline "
            "could have bought. The account's own ticker and every other such candidate go through one and the "
            "same replay: sized by the sizing rule and the risk gate, bought at the same open, protected by the "
            "rule's own stop, sold when the account sold its own pick, and valued on "
            f"{random.valued_on}, where the account's books stand. The ticker is what differs. (The moment of each "
            "sale was decided by what happened to the account's ticker, which is the one thing a random pick "
            "inherits that was not about itself.)", "",
            f"- The account's picks, replayed this way, made **{_usd(random.replayed_pnl_usd)}**.",
            f"- Random picks made {_usd(random.mean_pnl_usd)} on average; half of the outcomes fell between "
            f"{_usd(quantiles['25'])} and {_usd(quantiles['75'])}, and nine in ten between {_usd(quantiles['5'])} "
            f"and {_usd(quantiles['95'])}.",
            f"- The account's picks did better than **{random.percentile:.0f}%** of the random outcomes. Around 50% "
            "is what picking at random would give; only a figure that stays high as the number of buys grows says "
            "anything.",
            f"- The account's books have the same buys at {_usd(random.booked_pnl_usd)}, dividends included. That "
            "figure is not what is compared. It differs from the replay by everything other than the choice of "
            "ticker: a stop the analyst proposed in place of the rule's, a size cut because of another order placed "
            "the same day, and the limits in force at the time.",
            "",
            "| Bought | Decided | Replayed, it made | The books say | Candidates it could have bought | Their average | Its rank among them |",
            "|---|---|---|---|---|---|---|",
            *[
                f"| {pick.ticker} | {pick.decided_on} | {_usd(pick.replayed_usd)} | {_usd(pick.booked_usd)} | "
                f"{pick.pool} | {_usd(pick.pool_mean_usd)} | {pick.rank} |"
                for pick in random.picks
            ], "",
        ]
        if random.skipped:
            out += [left_out, ""]
        out += [
            "Each candidate is sized and gated on its own, against the account and its positions as they stood "
            "after that day's close. The other orders the account placed that day are not counted, and the limits "
            "are today's settings, not necessarily those of the day.", "",
        ]

    usage = arm.usage
    cost = "no per-token cost (local model)" if usage.cost_usd is None else f"about ${usage.cost_usd:,.2f}"
    out += [
        "### Model use", "",
        f"{usage.calls:,} model call(s), {usage.input_tokens:,} tokens in and {usage.output_tokens:,} out, "
        f"{usage.model_seconds / 60:,.0f} minute(s) of model time, {cost}. Runs that failed or were replaced are "
        "included.", "",
        "### Lessons", "",
        f"{arm.lessons_written} written by this evaluation, {arm.lessons_total} in the journal. They are "
        + ("shown" if config.PIPELINE_USE_LESSONS else "not shown")
        + " to the ticker analyst (PIPELINE_USE_LESSONS).", "",
    ]
    return out


def to_markdown(evaluation: Evaluation) -> str:
    out = [
        f"# Evaluation as of the close of {evaluation.as_of}",
        "",
        f"Measured against {evaluation.benchmark}. Every number here was worked out by code, from what the journal "
        "recorded and from prices that came after each decision. Paper trading: no real money is involved. "
        "These are measurements of model outputs, not financial advice.",
        "",
    ]
    if not evaluation.arms:
        out += ["No runs recorded yet.", ""]
    for arm in evaluation.arms:
        out += _arm(arm, evaluation.benchmark)
    return "\n".join(out)
