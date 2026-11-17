"""The daily trading pipeline, in shadow mode: it produces signals and places no orders.

    python -m pipeline.daily            run for the last completed session
    python -m pipeline.daily --status   show past runs and progress towards the stage's exit test

The order of the stages is fixed in code. Models work inside a stage and
never choose what happens next:

    1. settle     fill earlier proposals in the shadow book at the next open
    2. macro      seven agents assess the current state; code combines them
    3. views      one analyst view per candidate and per holding
    4. debate     bull, bear and judge on the strongest candidates and on
                  holdings whose view has turned
    5. proposals  the portfolio manager proposes; code accepts or rejects

Data is collected once, before any model is called. Several model
configurations ("arms") can then read the same snapshot, each keeping its own
signals and its own shadow book.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import config
from agents import ALL_AGENTS
from agents.ticker import Debater, Judge, PortfolioManager, TickerAnalyst
from data.market_calendar import (
    last_completed_session, next_trading_day, previous_trading_day, session_open,
)
from data.models import Snapshot
from data.render import render_market, render_market_brief, render_ticker
from data.snapshot import SnapshotBuilder, save_snapshot
from journal.store import Journal
from models.signals import (
    DebateRecord, DebateTurn, Event, Holding, MacroRegime, ScreenRow, SignalTable, TickerView,
    TradeProposal, UsageSummary,
)
from pipeline import universe as universe_rules
from pipeline.checks import check_assessment, check_proposals, check_ruling, check_view, figure_warning
from pipeline.macro import combine, render_regime
from pipeline.report import render_holding, render_ruling, render_view, to_markdown
from pipeline.shadow import ShadowBook
from utils.llm_client import LLMClient, LLMError

# The two parts of the pipeline that are rules, not models, but still speak in the record.
RISK_OFFICER = ("risk_officer", "Risk Officer")
TRADER = ("trader", "Trader")
PIPELINE = ("pipeline", "Pipeline")


# A run needs usable prices for at least this share of the universe. Below it
# the session's data is taken to be missing or not yet published.
MIN_USABLE_SHARE = 0.5


# A run that started this recently and has not finished is taken to be still
# going, and a second one for the same arm and session is not started.
RUNNING_GRACE = timedelta(hours=6)


class DataFailure(RuntimeError):
    """The data needed for a run could not be collected. Nothing is proposed."""


class TooLate(RuntimeError):
    """The next session opened before the run was done. Nothing is proposed."""


class BudgetExceeded(RuntimeError):
    """The run's model calls reached the cost limit. Nothing is proposed."""


@dataclass
class Arm:
    """One model configuration."""

    name: str
    llm: LLMClient


@dataclass
class RunResult:
    arm: str
    run_id: str
    status: str                      # ok, degraded, failed, skipped, blocked
    message: str = ""
    table: SignalTable | None = None
    json_path: Path | None = None
    markdown_path: Path | None = None


@dataclass
class _Data:
    snapshot: Snapshot
    snapshot_path: Path
    rows: list[ScreenRow]
    candidates: list[str]
    universe_size: int
    warnings: list[str]


@dataclass
class _Run:
    """What one arm's run accumulates as it goes."""

    arm: Arm
    run_id: str
    data: _Data
    first_call: int                                  # index into the arm's usage log where this run starts
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False
    seq: int = 0

    def problem(self, message: str) -> None:
        """Something went wrong that makes today's signals incomplete."""
        self.warnings.append(message)
        self.degraded = True


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def usage_since(llm: LLMClient, first_call: int) -> UsageSummary:
    calls = llm.usage.calls[first_call:]
    priced = [call.cost_usd for call in calls if call.cost_usd is not None]
    return UsageSummary(
        calls=len(calls),
        input_tokens=sum(call.input_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        model_seconds=round(sum(call.seconds for call in calls), 1),
        cost_usd=round(sum(priced), 4) if priced else None,
    )


class DailyPipeline:
    def __init__(
        self,
        arms: list[Arm],
        builder: SnapshotBuilder | None = None,
        journal: Journal | None = None,
        universe: list[str] | None = None,
        runs_dir: str | Path | None = None,
        snapshot_dir: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.arms = arms
        self.builder = builder or SnapshotBuilder()
        self.journal = journal or Journal()
        self.universe = universe if universe is not None else universe_rules.load_universe()
        self.runs_dir = Path(runs_dir or config.RUNS_DIR)
        self.snapshot_dir = snapshot_dir
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._on_event = on_event

    # ── Entry point ──────────────────────────────────────────────────────

    def run(self, force: bool = False, refresh_macro: bool = False) -> list[RunResult]:
        """Run every arm for the last completed session.

        An arm that already has signals for the session is skipped unless
        `force` is set, so the command can be scheduled daily and repeated
        safely: on weekends and holidays there is nothing new to do.
        """
        now = self._now()
        session = last_completed_session(now)
        results: list[RunResult] = []
        ready: list[Arm] = []
        for arm in self.arms:
            done = self.journal.completed_run(arm.name, session)
            if done and not force:
                results.append(RunResult(arm.name, done, "skipped", f"Signals for {session} already exist."))
                continue
            unfinished = self.journal.running_run(arm.name, session)
            if unfinished and not force:
                started = datetime.fromisoformat(unfinished["started_at"])
                if now - started < RUNNING_GRACE:
                    # Not "skipped": the session has no signals, and a scheduler should see that.
                    results.append(RunResult(arm.name, unfinished["run_id"], "blocked", (
                        f"A run for {session} started at {started:%Y-%m-%d %H:%M} UTC and has not finished. "
                        "If it crashed, run again with --force."
                    )))
                    continue
            try:
                self._require_before_open(session)
            except TooLate as e:
                results.append(self._fail(arm, session, f"{e} Run after today's close."))
                continue
            problems = arm.llm.problems()
            if problems:
                results.append(self._fail(arm, session, "The model is not available: " + " ".join(problems)))
                continue
            ready.append(arm)
        if not ready:
            return results

        try:
            data = self._collect(session, [arm.name for arm in ready])
        except Exception as e:  # no data, no run: recorded for every arm
            message = str(e) if isinstance(e, DataFailure) else f"{type(e).__name__}: {e}"
            return results + [self._fail(arm, session, message) for arm in ready]
        for arm in ready:
            try:
                results.append(self._run_arm(arm, data, refresh_macro))
            except Exception as e:  # the journal itself failed; the other arms still run
                results.append(RunResult(arm.name, "", "failed", f"The run could not be recorded. {type(e).__name__}: {e}"))
        return results

    # ── Data, collected once for all arms ────────────────────────────────

    def _collect(self, session: date, arm_names: list[str]) -> _Data:
        prices = self.builder.build(
            self.universe, include_market=False, include_macro=False,
            include_news=False, include_fundamentals=False,
        )
        self._require_usable(prices, session)
        unusable = [item for item in prices.tickers.values() if not item.usable]
        if len(prices.tickers) - len(unusable) < MIN_USABLE_SHARE * len(prices.tickers):
            example = next(issue.message for issue in unusable[0].issues if issue.severity == "block")
            raise DataFailure(
                f"Only {len(prices.tickers) - len(unusable)} of {len(prices.tickers)} tickers have usable prices "
                f"for {session}. For example: {example} If the session has only just closed, the source may "
                "not have published its closing prices yet: run again later."
            )
        rows = universe_rules.screen(prices)
        candidates = universe_rules.candidates(rows)

        # Holdings and waiting orders are fetched too, whatever the screen
        # says today: they need a view, and a price to fill or value them at.
        tickers = list(dict.fromkeys(candidates + self.journal.book_tickers(arm_names)))
        snapshot = self.builder.build(tickers)
        self._require_usable(snapshot, session)
        # Fundamentals and headlines are fetched live and take minutes. If the
        # next session opened meanwhile, some of them are from after its open.
        try:
            self._require_before_open(session)
        except TooLate as e:
            raise DataFailure(f"{e} The data was still being collected when it opened.") from e
        path = save_snapshot(snapshot, self.snapshot_dir)
        warnings = [f"Data: {issue.message}" for issue in snapshot.issues]
        return _Data(snapshot, path, rows, candidates, len(self.universe), warnings)

    def _require_before_open(self, session: date) -> None:
        """Refuse to go on once the session after `session` has opened.

        From then on a run would see that session's news and prices, and its
        orders would be filled at an open that has already passed.
        """
        following = next_trading_day(session)
        if self._now() >= session_open(following):
            raise TooLate(
                f"The session after {session} ({following}) is already trading. Signals made now would rest "
                "on that day's news and prices and be filled at an open that has passed."
            )

    @staticmethod
    def _require_usable(snapshot: Snapshot, session: date) -> None:
        if snapshot.session_date != session:
            raise DataFailure(
                f"The data is for session {snapshot.session_date}, but the run is for {session}. "
                "A session closed while the run was starting; run it again."
            )
        blocked = [issue.message for issue in snapshot.issues if issue.severity == "block"]
        if blocked:
            raise DataFailure("The price source failed: " + " ".join(blocked))

    # ── One arm ──────────────────────────────────────────────────────────

    def _new_run_id(self, arm: Arm, session: date) -> str:
        base = f"{session}_{_slug(arm.name)}_{self._now():%H%M%S}"
        taken = {row["run_id"] for row in self.journal.runs(arm.name)}
        run_id, attempt = base, 1
        while run_id in taken:
            attempt += 1
            run_id = f"{base}-{attempt}"
        return run_id

    def _fail(self, arm: Arm, session: date, message: str) -> RunResult:
        """Record a run that could not start."""
        run_id = self._new_run_id(arm, session)
        self.journal.start_run(run_id, arm.name, session, self._now())
        self.journal.finish_run(run_id, "failed", self._now(), UsageSummary(), error=message)
        return RunResult(arm.name, run_id, "failed", message)

    def _run_arm(self, arm: Arm, data: _Data, refresh_macro: bool) -> RunResult:
        session = data.snapshot.session_date
        run = _Run(arm, self._new_run_id(arm, session), data, len(arm.llm.usage.calls))
        self.journal.start_run(run.run_id, arm.name, session, self._now())
        try:
            table = self._stages(run, refresh_macro)
            folder = self.runs_dir / session.isoformat() / run.run_id
            folder.mkdir(parents=True, exist_ok=True)
            json_path, markdown_path = folder / "signals.json", folder / "signals.md"
            json_path.write_text(table.model_dump_json(indent=2), encoding="utf-8")
            markdown_path.write_text(to_markdown(table), encoding="utf-8")
            self._require_before_open(session)
            # Last, and all together: the orders, which fills this table
            # reported, and the run's status. A run that failed anywhere above
            # leaves no orders behind.
            with self.journal.transaction():
                ShadowBook(self.journal, arm.name).queue(table.proposals, session, run.run_id)
                self.journal.mark_trades_reported(arm.name, run.run_id)
                self.journal.finish_run(
                    run.run_id, table.status, self._now(), table.usage, table=table,
                    snapshot_path=str(data.snapshot_path),
                )
        except Exception as e:  # recorded, and the other arms still run
            message = f"{type(e).__name__}: {e}"
            self.journal.finish_run(
                run.run_id, "failed", self._now(), usage_since(arm.llm, run.first_call),
                snapshot_path=str(data.snapshot_path), error=message,
            )
            return RunResult(arm.name, run.run_id, "failed", message)
        return RunResult(arm.name, run.run_id, table.status, "", table, json_path, markdown_path)

    def _stages(self, run: _Run, refresh_macro: bool) -> SignalTable:
        snapshot, session = run.data.snapshot, run.data.snapshot.session_date
        book = ShadowBook(self.journal, run.arm.name)
        for warning in run.data.warnings:
            run.problem(warning)

        fills, notes = book.settle(snapshot)
        for note in notes:
            run.problem(note)
            self._say(run, "settle", TRADER, "all", "book_note", remark=note)
        for fill in fills:
            result = "" if fill.pnl_pct is None else f", {fill.pnl_pct:+.2f}% against entry"
            if fill.action == "close":
                remark = (f"{fill.ticker} closed by the book at its {fill.trade_date} close, "
                          f"{fill.price:,.2f}{result}.")
            else:
                remark = (f"{fill.action.capitalize()} {fill.ticker} filled at the {fill.trade_date} open, "
                          f"{fill.price:,.2f}{result}.")
            self._say(run, "settle", TRADER, "all", "fill", ticker=fill.ticker, remark=remark,
                      payload=fill.model_dump(mode="json"))
        holdings = {holding.ticker: holding for holding in book.holdings(snapshot)}
        waiting = book.waiting(before=session)

        regime, reused_from = self._macro(run, refresh_macro)
        views = self._views(run, holdings, regime)
        debates = self._debates(run, holdings, views, regime)
        proposals, pm_summary = self._proposals(run, holdings, waiting, views, debates, regime)

        return SignalTable(
            run_id=run.run_id, arm=run.arm.name, session_date=session, created_at=self._now(),
            status="degraded" if run.degraded else "ok", snapshot_id=snapshot.snapshot_id,
            universe_size=run.data.universe_size, screen=run.data.rows, candidates=run.data.candidates,
            fills=book.unreported_fills(), holdings=list(holdings.values()), regime=regime,
            regime_reused_from=reused_from,
            views=list(views.values()), debates=debates, proposals=proposals, pm_summary=pm_summary,
            warnings=run.warnings, usage=usage_since(run.arm.llm, run.first_call),
        )

    # ── Stage 2: macro ───────────────────────────────────────────────────

    def _macro(self, run: _Run, refresh: bool) -> tuple[MacroRegime | None, date | None]:
        session = run.data.snapshot.session_date
        cached = self.journal.latest_regime(run.arm.name, session)
        if cached and not refresh and (session - cached.session_date).days < config.PIPELINE_MACRO_MAX_AGE_DAYS:
            self._say(
                run, "macro", PIPELINE, "all", "macro_view_reused", payload=cached.model_dump(mode="json"),
                remark=f"The macro view formed on {cached.session_date} is reused: risk appetite "
                       f"{cached.risk_appetite.replace('_', '-')}, confidence {cached.confidence:.2f}.",
            )
            return cached, cached.session_date

        market = render_market(run.data.snapshot)
        if not market:
            run.problem("No market or macro data was collected, so there is no macro view and no new positions.")
            return None, None
        assessments = []
        for agent_class in ALL_AGENTS:
            agent = agent_class(run.arm.llm)
            self._check_budget(run)
            try:
                draft = agent.assess_current(market)
            except LLMError as e:
                run.problem(f"{agent.name}: the call failed. {e}")
                continue
            assessment, refusal = check_assessment(agent.id, agent.name, draft, [market, agent.current_state_prompt])
            if assessment is None:
                self._refuse(run, "macro", agent.id, refusal)
                continue
            assessments.append(assessment)
            self._say(run, "macro", (agent.id, agent.name), "all", "assessment", remark=draft.remark,
                      payload=assessment.model_dump(mode="json"), is_llm=True)

        regime = combine(assessments, session)
        if regime is None:
            run.problem(
                f"Only {len(assessments)} of {len(ALL_AGENTS)} macro assessments were usable, "
                "so there is no macro view and no new positions today."
            )
            return None, None
        self.journal.save_regime(run.arm.name, regime)
        self._say(
            run, "macro", PIPELINE, "all", "macro_view", payload=regime.model_dump(mode="json"),
            remark=f"Macro view computed from {len(assessments)} assessments: risk appetite "
                   f"{regime.risk_appetite.replace('_', '-')}, confidence {regime.confidence:.2f}.",
        )
        return regime, None

    # ── Stage 3: views ───────────────────────────────────────────────────

    def _views(self, run: _Run, holdings: dict[str, Holding], regime: MacroRegime | None) -> dict[str, TickerView]:
        snapshot = run.data.snapshot
        analyst = TickerAnalyst(run.arm.llm)
        macro_view, backdrop = render_regime(regime), render_market_brief(snapshot)
        views: dict[str, TickerView] = {}
        for ticker in dict.fromkeys(run.data.candidates + sorted(holdings)):
            item = snapshot.tickers.get(ticker)
            if item is None or not item.usable:
                reasons = " ".join(i.message for i in item.issues if i.severity == "block") if item else ""
                run.problem(f"{ticker}: no view today, its data is not usable. {reasons}".strip())
                continue
            ticker_data = render_ticker(item)
            self._check_budget(run)
            try:
                draft = analyst.view(ticker, ticker_data, backdrop, macro_view)
            except LLMError as e:
                run.problem(f"{ticker}: the analyst call failed. {e}")
                continue
            shown = [ticker_data, backdrop, macro_view, analyst.system_prompt]
            view, refusal = check_view(ticker, snapshot.session_date, ticker in holdings, draft, item, shown)
            if view is None:
                self._refuse(run, "views", analyst.id, refusal, ticker)
                continue
            views[ticker] = view
            self._say(run, "views", (analyst.id, analyst.name), PortfolioManager.id, "view", ticker=ticker,
                      remark=draft.remark, payload=view.model_dump(mode="json"), is_llm=True)
        return views

    # ── Stage 4: debate ──────────────────────────────────────────────────

    @staticmethod
    def debate_targets(
        candidates: list[str], holdings: dict[str, Holding], views: dict[str, TickerView],
    ) -> list[tuple[str, str]]:
        """Which tickers are debated, and why: the strongest new ideas, and holdings whose view has turned."""
        fresh = [v for ticker, v in views.items() if ticker not in holdings and v.direction == "bullish"]
        fresh.sort(key=lambda v: (-v.confidence, candidates.index(v.ticker) if v.ticker in candidates else 0))
        targets = [(v.ticker, "top_candidate") for v in fresh[:config.PIPELINE_DEBATE_TOP]]
        targets += [
            (ticker, "exit_signal") for ticker in sorted(holdings)
            if ticker in views and views[ticker].direction != "bullish"
        ]
        return targets

    def _debates(
        self, run: _Run, holdings: dict[str, Holding], views: dict[str, TickerView], regime: MacroRegime | None,
    ) -> list[DebateRecord]:
        snapshot = run.data.snapshot
        llm = run.arm.llm
        bull, bear, judge = Debater(llm, "bull"), Debater(llm, "bear"), Judge(llm)
        records = []
        for ticker, trigger in self.debate_targets(run.data.candidates, holdings, views):
            held = ticker in holdings
            parts = [
                render_regime(regime), render_market_brief(snapshot),
                f"TICKER DATA\n{render_ticker(snapshot.tickers[ticker])}",
                f"ANALYST VIEW\n{render_view(views[ticker])}",
                f"THE DESK'S POSITION\n{render_holding(holdings[ticker])}" if held else "",
            ]
            briefing = "\n\n".join(part for part in parts if part)
            turns: list[DebateTurn] = []
            try:
                for round_number in range(1, max(1, config.PIPELINE_DEBATE_ROUNDS) + 1):
                    for debater, other in ((bull, bear), (bear, bull)):
                        self._check_budget(run)
                        draft = debater.argue(ticker, held, briefing, turns)
                        turn = DebateTurn(
                            side=debater.side, round=round_number, argument=draft.argument,
                            strongest_evidence=draft.strongest_evidence, remark=draft.remark,
                            data_warnings=figure_warning(
                                f"{draft.argument}\n{draft.strongest_evidence}",
                                [briefing, debater.system_prompt(ticker, held)],
                            ),
                        )
                        turns.append(turn)
                        self._say(run, "debate", (debater.id, debater.name), other.id, "argument", ticker=ticker,
                                  remark=draft.remark, payload=turn.model_dump(mode="json"), is_llm=True)
                self._check_budget(run)
                ruling = judge.rule(ticker, held, briefing, turns)
            except LLMError as e:
                run.problem(f"{ticker}: the debate did not finish. {e}")
                continue
            record, refusal = check_ruling(ticker, trigger, turns, ruling)
            if record is None:
                self._refuse(run, "debate", judge.id, refusal, ticker)
                continue
            records.append(record)
            self._say(run, "debate", (judge.id, judge.name), PortfolioManager.id, "ruling", ticker=ticker,
                      remark=ruling.remark, payload=record.model_dump(mode="json", exclude={"turns"}), is_llm=True)
        return records

    # ── Stage 5: proposals ───────────────────────────────────────────────

    def _proposals(
        self, run: _Run, holdings: dict[str, Holding], waiting: dict[str, str], views: dict[str, TickerView],
        debates: list[DebateRecord], regime: MacroRegime | None,
    ) -> tuple[list[TradeProposal], str]:
        manager = PortfolioManager(run.arm.llm, config.SHADOW_MAX_POSITIONS)
        briefing = self.manager_briefing(run.data.candidates, holdings, waiting, views, debates, regime)
        self._check_budget(run)
        draft = None
        try:
            draft = manager.propose(briefing)
        except LLMError as e:
            run.problem(f"The portfolio manager's call failed, so every position is held. {e}")
        else:
            self._say(run, "proposals", (manager.id, manager.name), RISK_OFFICER[0], "proposals",
                      remark=draft.remark, payload=draft.model_dump(mode="json"), is_llm=True)

        proposals = check_proposals(
            draft, candidates=run.data.candidates, views=views,
            rulings={record.ticker: record for record in debates}, holdings=holdings, waiting=waiting,
            regime_available=regime is not None, max_positions=config.SHADOW_MAX_POSITIONS,
        )
        for proposal in proposals:
            accepted = proposal.status == "accepted"
            if proposal.source == "default":
                remark = f"{proposal.ticker} is held by default: the portfolio manager gave no valid decision for it."
            elif accepted:
                remark = f"{proposal.action.capitalize()} {proposal.ticker} is accepted."
            else:
                remark = f"{proposal.action.capitalize()} {proposal.ticker} is rejected. {proposal.note}"
            self._say(
                run, "checks", RISK_OFFICER, TRADER[0] if accepted else manager.id,
                "accepted" if accepted else "rejected", ticker=proposal.ticker, remark=remark,
                payload=proposal.model_dump(mode="json"),
            )
        return proposals, draft.summary if draft else ""

    @staticmethod
    def manager_briefing(
        candidates: list[str], holdings: dict[str, Holding], waiting: dict[str, str],
        views: dict[str, TickerView], debates: list[DebateRecord], regime: MacroRegime | None,
    ) -> str:
        """Everything the portfolio manager is shown."""
        lines = [render_regime(regime), "", "CURRENT HOLDINGS:"]
        lines += [f"- {render_holding(h)}" for h in holdings.values()] or ["- none"]
        if waiting:
            lines += ["", "ORDERS FROM EARLIER SESSIONS STILL WAITING TO FILL:"]
            lines += [f"- {ticker}: {action}" for ticker, action in sorted(waiting.items())]
        lines += ["", f"TODAY'S CANDIDATES, strongest trend first: {', '.join(candidates) or 'none'}"]
        lines += ["", "ANALYST VIEWS:"]
        lines += [f"- {render_view(view)}" for view in views.values()] or ["- none"]
        without = [t for t in dict.fromkeys(candidates + sorted(holdings)) if t not in views]
        if without:
            lines += ["", f"NO VIEW TODAY (data unusable or the analysis failed): {', '.join(without)}"]
        if debates:
            lines += ["", "JUDGE RULINGS:"] + [f"- {render_ruling(record)}" for record in debates]
        return "\n".join(lines)

    # ── Shared pieces ────────────────────────────────────────────────────

    def _check_budget(self, run: _Run) -> None:
        spent = usage_since(run.arm.llm, run.first_call).cost_usd or 0.0
        if spent >= config.PIPELINE_MAX_COST_USD:
            raise BudgetExceeded(
                f"Model calls for this run have cost ${spent:.2f}, at or over the limit of "
                f"${config.PIPELINE_MAX_COST_USD:.2f} (PIPELINE_MAX_COST_USD)."
            )

    def _refuse(self, run: _Run, stage: str, agent_id: str, reason: str, ticker: str | None = None) -> None:
        """An agent's output failed a check and is discarded."""
        run.problem(f"Output refused. {reason}")
        self._say(run, stage, RISK_OFFICER, agent_id, "output_refused", ticker=ticker,
                  remark=f"Output refused. {reason}")

    def _say(
        self, run: _Run, stage: str, speaker: tuple[str, str], to: str, kind: str, remark: str,
        payload: dict | None = None, ticker: str | None = None, is_llm: bool = False,
    ) -> None:
        run.seq += 1
        event = Event(
            seq=run.seq, ts=self._now(), stage=stage, speaker=speaker[0], speaker_name=speaker[1],
            is_llm=is_llm, addressed_to=to, kind=kind, ticker=ticker, remark=remark, payload=payload or {},
        )
        self.journal.add_event(run.run_id, event)
        if self._on_event:
            self._on_event(event)


# ── Progress towards the stage's exit test ───────────────────────────────────

def progress(journal: Journal, arm: str) -> tuple[int, bool, list[tuple[date, str]]]:
    """How an arm stands against "five sessions in a row without error, with an exit proposed".

    Returns the number of consecutive trading sessions, ending with the latest
    one run, that finished with status ok; whether a sell or trim was accepted
    during them; and the status of each session run, newest first.
    """
    by_session: dict[date, list] = {}
    for row in journal.runs(arm):
        by_session.setdefault(date.fromisoformat(row["session_date"]), []).append(row)
    history = []
    for session in sorted(by_session, reverse=True):
        rows = by_session[session]
        done = [row for row in rows if row["status"] in ("ok", "degraded")]
        history.append((session, (done or rows)[0]["status"], (done or rows)[0]["run_id"]))

    streak, exits, expected = 0, False, history[0][0] if history else None
    for session, status, run_id in history:
        if session != expected or status != "ok":
            break
        streak += 1
        expected = previous_trading_day(session)
        table = journal.signal_table(run_id)
        exits = exits or any(
            p.status == "accepted" and p.action in ("sell", "trim") for p in (table.proposals if table else [])
        )
    return streak, exits, [(session, status) for session, status, _ in history]


def status_lines(journal: Journal) -> list[str]:
    arms = journal.arms()
    if not arms:
        return ["No runs recorded yet."]
    lines = []
    for arm in arms:
        streak, exits, history = progress(journal, arm)
        lines.append(f"{arm}")
        lines.append(f"  Sessions in a row without error: {streak} of 5")
        lines.append(f"  A sell or trim accepted in that stretch: {'yes' if exits else 'no'}")
        lines.append("  Recent sessions: " + ", ".join(f"{day} {status}" for day, status in history[:10]))
    return lines


# ── Command line ─────────────────────────────────────────────────────────────

def parse_arm(spec: str) -> Arm:
    """`provider` or `provider:model`, for example `ollama:qwen2.5-coder:7b`."""
    provider, _, model = spec.partition(":")
    llm = LLMClient(provider=provider.strip().lower(), model=model.strip() or None)
    return Arm(f"{llm.provider_name}/{llm.model}", llm)


def default_arm() -> Arm:
    llm = LLMClient()
    return Arm(f"{llm.provider_name}/{llm.model}", llm)


def _print_event(event: Event) -> None:
    subject = f" [{event.ticker}]" if event.ticker else ""
    print(f"  {event.stage:<9} {event.speaker_name} -> {event.addressed_to}{subject}: {event.remark}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily trading pipeline in shadow mode: signals only, no orders.")
    parser.add_argument("--arm", action="append", default=[], metavar="PROVIDER[:MODEL]",
                        help="Model configuration to run. Repeat to compare several on the same data.")
    parser.add_argument("--force", action="store_true", help="Run again even if this session already has signals")
    parser.add_argument("--refresh-macro", action="store_true", help="Form a new macro view instead of reusing one")
    parser.add_argument("--universe", default=None, help="File with one ticker per line (default: UNIVERSE_FILE)")
    parser.add_argument("--status", action="store_true", help="Show past runs and exit")
    parser.add_argument("--quiet", action="store_true", help="Do not print each event as it happens")
    args = parser.parse_args(argv)

    journal = Journal()
    if args.status:
        print("\n".join(status_lines(journal)))
        return 0

    try:
        arms = [parse_arm(spec) for spec in args.arm] or [default_arm()]
        tickers = universe_rules.load_universe(args.universe)
    except (LLMError, OSError, ValueError) as e:
        print(f"Cannot start: {e}", file=sys.stderr)
        return 1

    print(f"Shadow mode: signals only, no orders. Arms: {', '.join(arm.name for arm in arms)}", flush=True)
    pipeline = DailyPipeline(arms, journal=journal, universe=tickers, on_event=None if args.quiet else _print_event)
    results = pipeline.run(force=args.force, refresh_macro=args.refresh_macro)

    for result in results:
        print(f"\n{result.arm}: {result.status}" + (f". {result.message}" if result.message else ""))
        if result.table:
            table = result.table
            accepted = [p for p in table.proposals if p.status == "accepted" and p.source == "portfolio_manager"]
            print(f"  {len(table.views)} views, {len(table.debates)} debates, {len(accepted)} accepted proposal(s)")
            for warning in table.warnings:
                print(f"  warning: {warning}")
            usage = table.usage
            cost = "no per-token cost" if usage.cost_usd is None else f"about ${usage.cost_usd:.4f}"
            print(f"  {usage.calls} model calls, {usage.model_seconds:.0f}s of model time, {cost}")
            print(f"  {result.markdown_path}")
            print(f"  {result.json_path}")
    return 1 if any(result.status in ("failed", "blocked") for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
