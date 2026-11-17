"""The deterministic parts of the daily pipeline: screen, macro view, output checks, shadow book."""

from datetime import date

import pytest

from data.models import CorporateAction
from data.snapshot import SnapshotBuilder
from journal.store import Journal, JournalError
from models.signals import DomainAssessment, Holding, SectorCall, TradeProposal
from pipeline import universe as universe_rules
from pipeline.checks import check_assessment, check_proposals, check_ruling, check_view
from pipeline.macro import combine, render_regime
from pipeline.shadow import ShadowBook
from tests.pipeline_fakes import (
    Clock, NoNews, SteadyMacro, SyntheticMarket, assessment, day, proposals, ruling, session, view,
)


def snapshot_for(tickers, on=13, market=None, **options):
    builder = SnapshotBuilder(market=market or SyntheticMarket(), macro=SteadyMacro(), news=NoNews(), now=Clock(day(on)))
    return builder.build(tickers, include_market=False, include_macro=False, **options)


# ── Universe and screen ──────────────────────────────────────────────────────

def test_universe_file_is_read_with_comments_and_duplicates_removed(tmp_path):
    path = tmp_path / "universe.txt"
    path.write_text("# my list\naapl\nMSFT  # software\n\nAAPL\n")
    assert universe_rules.load_universe(path) == ["AAPL", "MSFT"]


def test_empty_universe_file_is_an_error(tmp_path):
    path = tmp_path / "universe.txt"
    path.write_text("# nothing yet\n")
    with pytest.raises(ValueError, match="lists no tickers"):
        universe_rules.load_universe(path)


def test_builtin_universe_has_no_duplicates():
    tickers = universe_rules.load_universe("")
    assert len(tickers) == len(set(tickers)) and len(tickers) > 30


def test_screen_keeps_uptrends_ranked_by_three_month_return():
    rows = universe_rules.screen(snapshot_for(["UPC", "DOWN", "UPA", "GONE", "UPB"]))
    by_ticker = {row.ticker: row for row in rows}

    assert universe_rules.candidates(rows) == ["UPA", "UPB", "UPC"]
    assert [by_ticker[t].rank for t in ("UPA", "UPB", "UPC")] == [1, 2, 3]
    assert not by_ticker["DOWN"].passed and "Not in an uptrend" in by_ticker["DOWN"].reason
    assert not by_ticker["GONE"].passed and "Data not usable" in by_ticker["GONE"].reason


def test_screen_limit_drops_the_weakest_and_says_so():
    rows = universe_rules.screen(snapshot_for(["UPA", "UPB", "UPC"]), max_candidates=2)
    dropped = next(row for row in rows if row.ticker == "UPC")

    assert universe_rules.candidates(rows) == ["UPA", "UPB"]
    assert not dropped.passed and dropped.rank is None and "ranked 3, outside the top 2" in dropped.reason


def test_screen_refuses_cheap_and_illiquid_tickers():
    market = SyntheticMarket(base={"UPA": 1.0}, volume={"UPB": 1000.0})
    by_ticker = {row.ticker: row for row in universe_rules.screen(snapshot_for(["UPA", "UPB", "UPC"], market=market))}

    assert "below the minimum" in by_ticker["UPA"].reason and not by_ticker["UPA"].passed
    assert "dollar volume" in by_ticker["UPB"].reason and not by_ticker["UPB"].passed
    assert by_ticker["UPC"].passed


def test_screening_does_not_fetch_fundamentals_or_news():
    class Strict(SyntheticMarket):
        def fundamentals(self, symbol):
            raise AssertionError("the screen must not look up fundamentals")

        def next_earnings(self, symbol, after):
            raise AssertionError("the screen must not look up earnings dates")

    snapshot = snapshot_for(["UPA"], market=Strict(), include_news=False, include_fundamentals=False)
    assert snapshot.tickers["UPA"].usable and snapshot.tickers["UPA"].fundamentals is None


def test_a_sector_etf_that_is_also_a_ticker_is_requested_once():
    market = SyntheticMarket()
    SnapshotBuilder(market=market, macro=SteadyMacro(), news=NoNews(), now=Clock(day(13))).build(["XLF"])
    assert market.requested[0].count("XLF") == 1


# ── Macro view ───────────────────────────────────────────────────────────────

def kept(agent, stance, confidence, tilts=()):
    return DomainAssessment(
        agent_id=agent, agent_name=agent.title(), stance=stance, summary="s", key_risk="r",
        sector_tilts=[SectorCall(sector=s, tilt=t) for s, t in tilts], confidence=confidence, remark="x",
    )


def test_macro_view_takes_the_stance_with_most_confidence_behind_it():
    regime = combine([
        kept("a", "supportive", 0.8), kept("b", "supportive", 0.7), kept("c", "restrictive", 0.9),
        kept("d", "neutral", 0.6),
    ], session(13))

    assert regime.risk_appetite == "risk_on"
    assert regime.confidence == 0.5          # 1.5 of 3.0
    assert len(regime.key_risks) == 4 and regime.key_risks[0].startswith("A: ")


def test_macro_view_is_neutral_on_a_tie():
    regime = combine([
        kept("a", "supportive", 0.5), kept("b", "restrictive", 0.5), kept("c", "neutral", 0.2),
        kept("d", "neutral", 0.2),
    ], session(13))
    assert regime.risk_appetite == "neutral" and regime.confidence == 0.29


def test_agents_with_no_confidence_at_all_have_not_formed_a_view():
    assert combine([kept(name, "supportive", 0.0) for name in "abcd"], session(13)) is None


def test_a_tie_that_floating_point_would_hide_is_still_neutral():
    regime = combine([
        kept("a", "supportive", 0.1), kept("b", "supportive", 0.2), kept("c", "restrictive", 0.3),
        kept("d", "neutral", 0.1),
    ], session(13))
    assert 0.1 + 0.2 != 0.3 and regime.risk_appetite == "neutral"


def test_too_few_assessments_give_no_macro_view():
    assert combine([kept(name, "supportive", 0.9) for name in "abc"], session(13)) is None
    assert render_regime(None) == "MACRO VIEW: not available today."


def test_sector_tilt_needs_more_than_one_agent_and_opposing_calls_cancel():
    regime = combine([
        kept("a", "neutral", 0.9, [("Technology", "overweight"), ("Energy", "overweight"), ("Materials", "overweight")]),
        kept("b", "neutral", 0.7, [("Technology", "overweight"), ("Energy", "underweight"), ("Utilities", "underweight")]),
        kept("c", "neutral", 0.3, [("Utilities", "underweight")]),
        kept("d", "neutral", 0.6, [("Energy", "underweight")]),
    ], session(13))
    tilts = {tilt.sector: tilt for tilt in regime.sector_tilts}

    # Materials has one agent behind it (0.9). Energy nets 0.4 against. Utilities sums to exactly 1.0.
    assert set(tilts) == {"Technology", "Utilities"}
    assert tilts["Technology"].tilt == "overweight" and tilts["Technology"].supporters == ["a", "b"]
    assert tilts["Utilities"].tilt == "underweight" and tilts["Utilities"].supporters == ["b", "c"]
    text = render_regime(regime)
    assert "Favoured sectors: Technology" in text and "Sectors to avoid: Utilities" in text


def test_assessment_with_confidence_outside_its_range_is_refused():
    item, reason = check_assessment("central_bank", "Central Bank Agent", assessment(confidence=75), ["data"])
    assert item is None and "outside 0 to 1" in reason


def test_assessment_keeps_one_call_per_sector_and_flags_untraceable_figures():
    draft = assessment(tilts=[("Energy", "overweight"), ("Energy", "underweight")],
                       summary="The policy rate stands at 9.75% and the index sits at 4,321.")
    item, _ = check_assessment("central_bank", "Central Bank Agent", draft, ["The policy rate is 9.75%."])

    assert [(c.sector, c.tilt) for c in item.sector_tilts] == [("Energy", "overweight")]
    assert len(item.data_warnings) == 1 and "4,321" in item.data_warnings[0] and "9.75" not in item.data_warnings[0]


# ── Ticker views ─────────────────────────────────────────────────────────────

def test_view_citations_are_compared_with_the_snapshot():
    item = snapshot_for(["UPA"]).tickers["UPA"]
    close, rsi = item.price.close, item.technicals.rsi14
    draft = view(evidence=[("close", round(close, 1)), ("rsi14", round(rsi)), ("sma50", close * 2), ("beta", 9.0)])

    checked, reason = check_view("UPA", session(13), False, draft, item, [])

    assert reason == ""
    assert [c.matches for c in checked.evidence] == [True, True, False, False]
    assert checked.evidence[2].actual == item.technicals.sma50
    assert "2 of 4 cited data point(s) do not match" in checked.data_warnings[0]


def test_citing_a_value_the_snapshot_does_not_have_is_a_mismatch():
    item = snapshot_for(["UPA"], include_fundamentals=False).tickers["UPA"]
    checked, _ = check_view("UPA", session(13), False, view(evidence=[("trailing_pe", 24.0)]), item, [])

    assert checked.evidence[0].actual is None and not checked.evidence[0].matches
    assert "data has no value" in checked.data_warnings[0]


def test_view_figures_are_traced_but_the_invalidation_level_is_not():
    item = snapshot_for(["UPA"]).tickers["UPA"]
    draft = view(thesis="Revenue grew 37% and the stock trades at 24 times earnings.")
    draft.invalidation = "A close below 55.25."

    checked, _ = check_view("UPA", session(13), True, draft, item, ["P/E trailing 24.0"])

    assert checked.held
    assert len(checked.data_warnings) == 1 and "37%" in checked.data_warnings[0]
    assert "55.25" not in checked.data_warnings[0] and "24" not in checked.data_warnings[0].split(":")[1]


@pytest.mark.parametrize("draft, expected", [
    (view(confidence=1.2), "confidence 1.2 is outside 0 to 1"),
    (view(confidence=-0.1), "outside 0 to 1"),
    (view(horizon=0), "horizon of 0 days is outside 1 to 60"),
    (view(horizon=250), "horizon of 250 days"),
])
def test_view_outside_its_ranges_is_refused_not_clamped(draft, expected):
    item = snapshot_for(["UPA"]).tickers["UPA"]
    checked, reason = check_view("UPA", session(13), False, draft, item, [])
    assert checked is None and expected in reason


def test_numbers_that_are_not_numbers_never_reach_the_checks():
    from pydantic import ValidationError
    from models.signals import Citation, ProposalDraft

    for bad in ("NaN", "Infinity", "-Infinity", "1e400"):
        with pytest.raises(ValidationError):
            ProposalDraft.model_validate_json('{"ticker":"A","action":"buy","confidence":%s,"reason":"r"}' % bad)
        with pytest.raises(ValidationError):
            Citation.model_validate_json('{"metric":"close","value":%s}' % bad)


def test_a_view_built_with_a_non_finite_number_is_still_refused():
    item = snapshot_for(["UPA"]).tickers["UPA"]
    draft = view()
    object.__setattr__(draft, "confidence", float("nan"))                 # as if a parser had let it through
    assert check_view("UPA", session(13), False, draft, item, [])[0] is None

    draft = view(evidence=[("close", 1.0)])
    object.__setattr__(draft.evidence[0], "value", float("inf"))
    checked, reason = check_view("UPA", session(13), False, draft, item, [])
    assert checked is None and "a cited value is not a number" in reason


def test_ruling_with_bad_confidence_is_refused():
    record, reason = check_ruling("UPA", "top_candidate", [], ruling(confidence=3))
    assert record is None and "judge's confidence" in reason
    record, _ = check_ruling("UPA", "exit_signal", [], ruling(stance="do_not_own", confidence=0.7))
    assert record.stance == "do_not_own" and record.trigger == "exit_signal"


# ── Proposals ────────────────────────────────────────────────────────────────

def holding(ticker):
    return Holding(ticker=ticker, shares=10, entry_price=100.0, entry_date=session(10))


def checked_view(ticker, direction="bullish"):
    item = snapshot_for([ticker]).tickers[ticker]
    return check_view(ticker, session(13), False, view(direction), item, [])[0]


def judged(ticker, stance):
    return check_ruling(ticker, "top_candidate", [], ruling(stance=stance))[0]


def decide(draft, held=(), waiting=None, views=("UPA", "UPB", "UPC"), regime=True, limit=10,
           directions=None, rulings=None):
    directions = directions or {}
    results = check_proposals(
        draft, candidates=["UPA", "UPB", "UPC"],
        views={t: checked_view(t, directions.get(t, "bullish")) for t in views},
        rulings={t: judged(t, stance) for t, stance in (rulings or {}).items()},
        holdings={t: holding(t) for t in held}, waiting=waiting or {}, regime_available=regime,
        max_positions=limit,
    )
    return {(p.ticker, p.action): p for p in results}


def test_valid_proposals_are_accepted():
    results = decide(proposals(("UPA", "buy", 0.7), ("DOWN", "sell", 0.8)), held=["DOWN"])
    assert all(p.status == "accepted" for p in results.values()) and len(results) == 2


def test_ticker_is_normalised_before_it_is_checked():
    results = decide(proposals((" upa ", "buy", 0.7)))
    assert results[("UPA", "buy")].status == "accepted"


@pytest.mark.parametrize("item, setup, note", [
    (("TSLA", "buy", 0.9), {}, "Not one of today's candidates or holdings"),
    (("UPA", "buy", 1.5), {}, "Confidence 1.5 is not a number from 0 to 1"),
    (("UPA", "buy", 0.9), {"held": ["UPA"]}, "Already held"),
    (("UPA", "buy", 0.9), {"waiting": {"UPA": "buy"}}, "still waiting to fill"),
    (("UPA", "buy", 0.9), {"views": ("UPB",)}, "No checked analyst view"),
    (("UPA", "buy", 0.9), {"regime": False}, "No current macro view"),
    (("UPA", "buy", 0.9), {"directions": {"UPA": "bearish"}}, "The analyst's view of this ticker is bearish, not bullish"),
    (("UPA", "buy", 0.9), {"directions": {"UPA": "neutral"}}, "is neutral, not bullish"),
    (("UPA", "buy", 0.9), {"rulings": {"UPA": "do_not_own"}}, "The judge ruled against owning it"),
    (("DOWN", "sell", 0.9), {"held": ["DOWN"], "waiting": {"DOWN": "sell"}}, "A sell from an earlier session is still waiting"),
    (("DOWN", "trim", 0.9), {"held": ["DOWN"], "waiting": {"DOWN": "sell"}}, "A sell from an earlier session is still waiting"),
    (("UPA", "sell", 0.9), {}, "Cannot sell a ticker that is not held"),
    (("UPA", "trim", 0.9), {}, "Cannot trim a ticker that is not held"),
    (("UPA", "hold", 0.9), {}, "Cannot hold a ticker that is not held"),
])
def test_proposals_the_rules_do_not_allow_are_rejected_with_the_reason(item, setup, note):
    results = decide(proposals(item), **setup)
    proposal = results[(item[0], item[1])]
    assert proposal.status == "rejected" and note in proposal.note


def test_a_buy_the_judge_ruled_for_is_accepted():
    results = decide(proposals(("UPA", "buy", 0.9)), rulings={"UPA": "own"})
    assert results[("UPA", "buy")].status == "accepted"


def test_a_holding_with_an_exit_already_waiting_is_not_given_a_second_order():
    results = check_proposals(
        proposals(("DOWN", "trim", 0.9)), candidates=[], views={}, rulings={},
        holdings={"DOWN": holding("DOWN")}, waiting={"DOWN": "sell"}, regime_available=True, max_positions=10,
    )
    assert [(p.action, p.status, p.source) for p in results] == [
        ("trim", "rejected", "portfolio_manager"), ("hold", "accepted", "default"),
    ]
    assert results[1].note == "A sell from an earlier session is still waiting to fill."


def test_exits_are_allowed_without_a_macro_view_or_an_analyst_view():
    results = decide(proposals(("DOWN", "sell", 0.6)), held=["DOWN"], views=(), regime=False)
    assert results[("DOWN", "sell")].status == "accepted"


def test_second_decision_for_a_ticker_is_rejected():
    results = check_proposals(
        proposals(("DOWN", "sell", 0.6), ("DOWN", "hold", 0.9)), candidates=[], views={}, rulings={},
        holdings={"DOWN": holding("DOWN")}, waiting={}, regime_available=True, max_positions=10,
    )
    assert [(p.action, p.status) for p in results] == [("sell", "accepted"), ("hold", "rejected")]
    assert "already given" in results[1].note


def test_only_the_first_decision_counts_even_when_it_was_rejected():
    results = check_proposals(
        proposals(("DOWN", "buy", 0.6), ("DOWN", "sell", 0.9)), candidates=[], views={}, rulings={},
        holdings={"DOWN": holding("DOWN")}, waiting={}, regime_available=True, max_positions=10,
    )
    assert [(p.action, p.status, p.source) for p in results] == [
        ("buy", "rejected", "portfolio_manager"), ("sell", "rejected", "portfolio_manager"),
        ("hold", "accepted", "default"),
    ]


def test_every_holding_gets_a_decision_and_missing_ones_are_held():
    results = check_proposals(
        proposals(("UPA", "buy", 0.6)), candidates=["UPA"], views={"UPA": checked_view("UPA")}, rulings={},
        holdings={"DOWN": holding("DOWN"), "UPB": holding("UPB")}, waiting={}, regime_available=True,
        max_positions=10,
    )
    defaults = [p for p in results if p.source == "default"]

    assert [(p.ticker, p.action, p.status) for p in defaults] == [("DOWN", "hold", "accepted"), ("UPB", "hold", "accepted")]
    assert all(p.note == "Held by default." for p in defaults)


def test_no_answer_from_the_portfolio_manager_holds_everything():
    results = check_proposals(
        None, candidates=["UPA"], views={}, rulings={}, holdings={"DOWN": holding("DOWN")}, waiting={},
        regime_available=True, max_positions=10,
    )
    assert [(p.ticker, p.action, p.source) for p in results] == [("DOWN", "hold", "default")]


def test_buys_fill_the_book_in_order_of_confidence():
    results = decide(proposals(("UPA", "buy", 0.5), ("UPB", "buy", 0.9), ("UPC", "buy", 0.7)), held=["DOWN"], limit=3)

    assert results[("UPB", "buy")].status == "accepted" and results[("UPC", "buy")].status == "accepted"
    assert results[("UPA", "buy")].status == "rejected" and "limited to 3 positions" in results[("UPA", "buy")].note


def test_a_position_being_sold_and_a_waiting_buy_both_still_take_room():
    # The sale may never fill, so its place is not given away the same day.
    selling = decide(proposals(("UPA", "buy", 0.5), ("DOWN", "sell", 0.9)), held=["DOWN"], limit=1)
    assert selling[("DOWN", "sell")].status == "accepted" and selling[("UPA", "buy")].status == "rejected"

    waiting = decide(proposals(("UPA", "buy", 0.5)), waiting={"UPB": "buy"}, limit=1)
    assert waiting[("UPA", "buy")].status == "rejected"

    room = decide(proposals(("UPA", "buy", 0.5)), held=["DOWN"], limit=2)
    assert room[("UPA", "buy")].status == "accepted"


# ── Shadow book ──────────────────────────────────────────────────────────────

@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.sqlite3")


def order(ticker, action):
    return TradeProposal(ticker=ticker, action=action, confidence=0.6, reason="r", status="accepted")


def open_on(snapshot, ticker, when):
    bars = snapshot.tickers[ticker].bars
    return bars.open[bars.dates.index(when)]


def test_buy_fills_at_the_next_open_for_a_fixed_amount(journal):
    book = ShadowBook(journal, "a", position_usd=10_000)
    book.queue([order("UPA", "buy")], session(13), "run1")

    assert book.settle(snapshot_for(["UPA"], on=13)) == ([], [])       # its fill price does not exist yet
    snapshot = snapshot_for(["UPA"], on=16)
    fills, notes = book.settle(snapshot)

    price = open_on(snapshot, "UPA", session(16))
    assert notes == [] and len(fills) == 1
    assert (fills[0].action, fills[0].trade_date, fills[0].price, fills[0].pnl_pct) == ("buy", session(16), price, None)
    assert fills[0].shares == pytest.approx(10_000 / price)
    held = book.holdings(snapshot)[0]
    assert held.entry_date == session(16) and held.entry_price == pytest.approx(price, abs=1e-4)
    assert held.sessions_held == 1 and held.unrealized_pct == pytest.approx(0.3, abs=0.01)
    assert book.settle(snapshot) == ([], [])                           # nothing fills twice


def test_a_skipped_day_still_fills_at_the_first_open_after_the_proposal(journal):
    book = ShadowBook(journal, "a")
    book.queue([order("UPA", "buy")], session(13), "run1")

    fills, _ = book.settle(snapshot_for(["UPA"], on=18))
    assert fills[0].trade_date == session(16)


def test_sell_closes_the_position_and_trim_halves_it(journal):
    book = ShadowBook(journal, "a", position_usd=10_000)
    book.queue([order("UPA", "buy"), order("UPB", "buy")], session(13), "run1")
    book.settle(snapshot_for(["UPA", "UPB"], on=16))
    book.queue([order("UPA", "sell"), order("UPB", "trim")], session(16), "run2")

    snapshot = snapshot_for(["UPA", "UPB"], on=17)
    fills, _ = book.settle(snapshot)
    by_ticker = {fill.ticker: fill for fill in fills}

    assert by_ticker["UPA"].action == "sell" and by_ticker["UPA"].pnl_pct == pytest.approx(0.3, abs=0.01)
    assert by_ticker["UPB"].action == "trim" and by_ticker["UPB"].pnl_pct == pytest.approx(0.2, abs=0.01)
    held = book.holdings(snapshot)
    assert [h.ticker for h in held] == ["UPB"]
    assert held[0].shares == pytest.approx(by_ticker["UPB"].shares)       # half was sold, half remains
    assert [row["action"] for row in journal.trades("a")] == ["buy", "buy", "sell", "trim"]


def test_order_waits_when_there_is_no_usable_price_and_is_dropped_after_the_limit(journal):
    book = ShadowBook(journal, "a", pending_max_days=5)
    book.queue([order("GONE", "buy")], session(13), "run1")

    assert book.settle(snapshot_for(["GONE"], on=17)) == ([], [])
    assert book.waiting(before=session(17)) == {"GONE": "buy"}
    fills, notes = book.settle(snapshot_for(["GONE"], on=19))

    assert fills == [] and "was dropped, no usable opening price" in notes[0]
    assert book.waiting(before=session(20)) == {}


def test_nothing_is_filled_from_data_the_quality_checks_rejected(journal):
    book = ShadowBook(journal, "a")
    book.queue([order("UPA", "buy")], session(13), "run1")

    broken = snapshot_for(["UPA"], on=16, market=SyntheticMarket(volume={"UPA": 0.0}))
    assert not broken.tickers["UPA"].usable and session(16) in broken.tickers["UPA"].bars.dates
    assert book.settle(broken) == ([], [])
    assert book.waiting(before=session(17)) == {"UPA": "buy"}

    fills, _ = book.settle(snapshot_for(["UPA"], on=17))            # the data is good again the next day
    assert fills[0].trade_date == session(16)


def test_orders_that_no_longer_make_sense_are_dropped_with_a_note(journal):
    book = ShadowBook(journal, "a")
    book.queue([order("UPA", "sell")], session(13), "run1")
    fills, notes = book.settle(snapshot_for(["UPA"], on=16))
    assert fills == [] and "there is no position" in notes[0]


def bought_on_16(journal):
    book = ShadowBook(journal, "a", position_usd=10_000)
    book.queue([order("UPA", "buy")], session(13), "run1")
    snapshot = snapshot_for(["UPA"], on=16)
    book.settle(snapshot)
    return book, book.holdings(snapshot)[0]


def split_on(number, ratio=2.0):
    return SyntheticMarket(splits={("UPA", session(number)): ratio})


def test_fake_prices_are_restated_after_a_split_as_the_real_source_does():
    before = snapshot_for(["UPA"], on=16).tickers["UPA"].bars
    after = snapshot_for(["UPA"], on=17, market=split_on(17)).tickers["UPA"].bars

    assert after.close[-2] == pytest.approx(before.close[-1] / 2, abs=1e-3)     # the 16th, restated
    assert after.close[-1] / after.close[-2] == pytest.approx(1.003)             # no gap on the split day


def test_split_changes_share_count_and_entry_price_but_not_the_result(journal):
    book, before = bought_on_16(journal)

    snapshot = snapshot_for(["UPA"], on=17, market=split_on(17))
    assert CorporateAction(date=session(17), kind="split", value=2.0) in snapshot.tickers["UPA"].actions
    book.settle(snapshot)
    after = book.holdings(snapshot)[0]

    assert after.shares == pytest.approx(before.shares * 2)
    assert after.entry_price == pytest.approx(before.entry_price / 2, abs=1e-3)
    assert after.unrealized_pct == pytest.approx(0.6, abs=0.02)          # two sessions of +0.3%, not -50%
    book.settle(snapshot)
    assert book.holdings(snapshot)[0].shares == pytest.approx(after.shares)   # applied once

    # The day after, the position is checked against its close on the split day, on the new scale.
    following = snapshot_for(["UPA"], on=18, market=split_on(17))
    assert book.settle(following) == ([], [])
    later = book.holdings(following)[0]
    assert later.shares == pytest.approx(after.shares) and later.unrealized_pct == pytest.approx(0.9, abs=0.03)


def test_a_buy_filled_from_a_bar_before_the_split_is_not_adjusted_twice(journal):
    # Proposed on the 13th, no run on the 16th, settled on the 17th, the day of a split.
    # The 16th's open in that snapshot is already on the new share scale.
    split_book = ShadowBook(journal, "split", position_usd=10_000)
    plain_book = ShadowBook(journal, "plain", position_usd=10_000)
    for book in (split_book, plain_book):
        book.queue([order("UPA", "buy")], session(13), "run1")
    split_snapshot = snapshot_for(["UPA"], on=17, market=split_on(17))
    plain_snapshot = snapshot_for(["UPA"], on=17)
    split_fills, _ = split_book.settle(split_snapshot)
    plain_book.settle(plain_snapshot)

    with_split, without = split_book.holdings(split_snapshot)[0], plain_book.holdings(plain_snapshot)[0]
    assert split_fills[0].trade_date == session(16)
    assert with_split.shares == pytest.approx(without.shares * 2)
    assert with_split.entry_price == pytest.approx(without.entry_price / 2, abs=1e-3)
    assert with_split.unrealized_pct == pytest.approx(0.6, abs=0.02) == pytest.approx(without.unrealized_pct, abs=0.01)
    assert with_split.shares * with_split.entry_price == pytest.approx(10_000, rel=1e-4)

    # The next day must not apply the split a second time.
    split_next, plain_next = snapshot_for(["UPA"], on=18, market=split_on(17)), snapshot_for(["UPA"], on=18)
    split_book.settle(split_next)
    plain_book.settle(plain_next)
    with_split, without = split_book.holdings(split_next)[0], plain_book.holdings(plain_next)[0]
    assert with_split.shares == pytest.approx(without.shares * 2)
    assert with_split.unrealized_pct == pytest.approx(0.9, abs=0.03) == pytest.approx(without.unrealized_pct, abs=0.01)


def test_sale_on_the_day_of_a_split_uses_the_new_share_count(journal):
    book, before = bought_on_16(journal)
    book.queue([order("UPA", "sell")], session(16), "run2")

    fills, _ = book.settle(snapshot_for(["UPA"], on=17, market=split_on(17)))

    assert fills[0].shares == pytest.approx(before.shares * 2)
    assert fills[0].pnl_pct == pytest.approx(0.3, abs=0.01)


def test_a_sale_filled_from_a_bar_before_the_split_keeps_its_true_result(journal):
    # Sell proposed on the 16th, no run on the 17th, settled on the 18th, the day of a split.
    book, before = bought_on_16(journal)
    book.queue([order("UPA", "sell")], session(16), "run2")

    fills, _ = book.settle(snapshot_for(["UPA"], on=18, market=split_on(18)))

    assert fills[0].trade_date == session(17)
    assert fills[0].pnl_pct == pytest.approx(0.3, abs=0.01)               # not about -50%
    assert fills[0].shares == pytest.approx(before.shares * 2)


def test_trim_then_split_leaves_half_the_position_on_the_new_scale(journal):
    book, before = bought_on_16(journal)
    book.queue([order("UPA", "trim")], session(16), "run2")
    book.settle(snapshot_for(["UPA"], on=17))

    snapshot = snapshot_for(["UPA"], on=18, market=split_on(18))
    book.settle(snapshot)
    after = book.holdings(snapshot)[0]

    assert after.shares == pytest.approx(before.shares / 2 * 2)
    assert after.unrealized_pct == pytest.approx(0.9, abs=0.03)


def test_a_split_on_a_day_the_data_was_unusable_is_applied_when_the_data_returns(journal):
    book, before = bought_on_16(journal)

    # The 17th: the split has happened but the ticker's data is rejected.
    broken = snapshot_for(["UPA"], on=17, market=SyntheticMarket(splits={("UPA", session(17)): 2.0}, volume={"UPA": 0.0}))
    assert not broken.tickers["UPA"].usable
    book.settle(broken)
    unpriced = book.holdings(broken)[0]
    assert unpriced.shares == pytest.approx(before.shares)                # untouched, and not valued
    assert unpriced.last_close is None and unpriced.unrealized_pct is None

    snapshot = snapshot_for(["UPA"], on=18, market=split_on(17))
    book.settle(snapshot)
    after = book.holdings(snapshot)[0]
    assert after.shares == pytest.approx(before.shares * 2)
    assert after.unrealized_pct == pytest.approx(0.9, abs=0.03)


def test_two_splits_between_runs_are_both_applied(journal):
    book, before = bought_on_16(journal)
    market = SyntheticMarket(splits={("UPA", session(17)): 2.0, ("UPA", session(18)): 3.0})

    snapshot = snapshot_for(["UPA"], on=18, market=market)                # no run on the 17th
    assert book.settle(snapshot) == ([], [])
    after = book.holdings(snapshot)[0]

    assert after.shares == pytest.approx(before.shares * 6)
    assert after.entry_price == pytest.approx(before.entry_price / 6, abs=1e-3)
    assert after.unrealized_pct == pytest.approx(0.9, abs=0.03)


def test_a_reverse_split_shrinks_the_share_count(journal):
    book, before = bought_on_16(journal)

    snapshot = snapshot_for(["UPA"], on=17, market=split_on(17, ratio=0.25))
    assert book.settle(snapshot) == ([], [])
    after = book.holdings(snapshot)[0]

    assert after.shares == pytest.approx(before.shares / 4)
    assert after.entry_price == pytest.approx(before.entry_price * 4, abs=1e-2)
    assert after.unrealized_pct == pytest.approx(0.6, abs=0.02)


def test_an_exit_waiting_through_unusable_days_and_two_splits_fills_with_its_true_result(journal):
    book, before = bought_on_16(journal)
    book.queue([order("UPA", "trim")], session(16), "run2")
    splits = {("UPA", session(17)): 2.0, ("UPA", session(18)): 3.0}

    for number in (17, 18):                                               # data rejected on both split days
        broken = snapshot_for(["UPA"], on=number, market=SyntheticMarket(splits=splits, volume={"UPA": 0.0}))
        assert book.settle(broken) == ([], [])

    snapshot = snapshot_for(["UPA"], on=19, market=SyntheticMarket(splits=splits))
    fills, notes = book.settle(snapshot)

    assert notes == [] and fills[0].trade_date == session(17)
    assert fills[0].pnl_pct == pytest.approx(0.3, abs=0.01)
    assert fills[0].shares == pytest.approx(before.shares * 6 / 2)
    assert book.holdings(snapshot)[0].shares == pytest.approx(before.shares * 6 / 2)


def test_prices_restated_without_a_listed_split_leave_the_position_unvalued(journal):
    book, before = bought_on_16(journal)
    book.queue([order("UPA", "sell")], session(16), "run2")
    bad = SyntheticMarket(unlisted_splits={("UPA", session(17)): 2.0})

    snapshot = snapshot_for(["UPA"], on=17, market=bad)
    assert snapshot.tickers["UPA"].usable and snapshot.tickers["UPA"].actions == []
    fills, notes = book.settle(snapshot)
    held = book.holdings(snapshot)[0]

    assert fills == []                                                    # the sale would have shown about -50%
    assert len(notes) == 1 and "listed splits and its restated prices do not agree" in notes[0]
    assert held.shares == pytest.approx(before.shares) and held.unrealized_pct is None
    assert book.waiting(before=session(18)) == {"UPA": "sell"}

    # Once the source lists the split, the position moves to the new scale and the sale fills.
    fixed = snapshot_for(["UPA"], on=18, market=split_on(17))
    fills, notes = book.settle(fixed)
    assert notes == [] and fills[0].pnl_pct == pytest.approx(0.3, abs=0.01)
    assert fills[0].shares == pytest.approx(before.shares * 2)


def test_a_listed_split_the_prices_do_not_reflect_is_also_refused(journal):
    book, before = bought_on_16(journal)

    class ListedOnly(SyntheticMarket):
        def path(self, symbol, end):
            frame = super().path(symbol, end)
            if symbol == "UPA" and session(17) in frame.index:
                frame.loc[session(17), "splits"] = 2.0                    # listed, but no price was restated
            return frame

    snapshot = snapshot_for(["UPA"], on=17, market=ListedOnly())
    fills, notes = book.settle(snapshot)

    assert "do not agree" in notes[0] and "split factor of 2" in notes[0]
    assert book.holdings(snapshot)[0].shares == pytest.approx(before.shares)


def test_a_position_out_of_step_for_too_long_is_closed_at_its_last_trusted_close(journal):
    book, before = bought_on_16(journal)
    book.pending_max_days = 2
    book.queue([order("UPA", "trim")], session(18), "run2")
    bad = SyntheticMarket(unlisted_splits={("UPA", session(17)): 2.0})

    for number in (17, 18, 19):                                           # first seen on the 17th; two days is not too long
        fills, notes = book.settle(snapshot_for(["UPA"], on=number, market=bad))
        assert fills == [] and len(notes) == 1 and "do not agree" in notes[0]
    snapshot = snapshot_for(["UPA"], on=20, market=bad)
    fills, notes = book.settle(snapshot)

    assert len(fills) == 1 and fills[0].action == "close"
    assert fills[0].trade_date == session(16) and fills[0].shares == pytest.approx(before.shares)
    assert fills[0].price == pytest.approx(before.last_close)                 # the close of the 16th, old scale
    assert fills[0].pnl_pct == pytest.approx(0.3, abs=0.01)                    # not the -50% the bad data implies
    assert len(notes) == 2 and "do not agree" in notes[0]                 # the waiting trim goes without a third note
    assert "in any run since 2026-11-17" in notes[1] and "closed at its 2026-11-16 close" in notes[1]
    assert book.holdings(snapshot) == [] and book.waiting(before=session(21)) == {}

    assert book.settle(snapshot_for(["UPA"], on=23, market=bad)) == ([], [])   # and the matter is over


def test_days_without_a_run_do_not_count_against_a_position(journal):
    # Bought on the 16th, then the pipeline is off for two weeks. On the first
    # run back, the ticker's data happens to be unusable for one evening.
    book, before = bought_on_16(journal)
    late = snapshot_for(["UPA"], on=30, market=SyntheticMarket(volume={"UPA": 0.0}))
    assert not late.tickers["UPA"].usable
    assert book.settle(late) == ([], [])                                  # not closed at a two-week-old price
    assert book.holdings(late)[0].shares == pytest.approx(before.shares)

    back = snapshot_for(["UPA"], on=30)                                   # the same session, data now fine
    assert book.settle(back) == ([], [])
    held = book.holdings(back)[0]
    assert held.unrealized_pct == pytest.approx(3.04, abs=0.05)           # ten sessions of +0.3% since entry
    assert journal.positions("a")[0]["out_of_sync_since"] is None         # and the count starts afresh


def test_an_exit_waiting_on_an_unmatched_position_is_dropped_with_the_real_reason(journal):
    book, _ = bought_on_16(journal)
    book.pending_max_days = 2
    book.queue([order("UPA", "sell")], session(16), "run2")
    bad = SyntheticMarket(unlisted_splits={("UPA", session(17)): 2.0})

    book.settle(snapshot_for(["UPA"], on=17, market=bad))
    fills, notes = book.settle(snapshot_for(["UPA"], on=19, market=bad))

    assert fills == [] and len(notes) == 2
    assert "sell proposed on 2026-11-16 was dropped, its position could not be matched to current prices" in notes[1]
    assert len(journal.positions("a")) == 1                               # the position itself has two more days


def test_a_position_whose_data_stops_coming_is_closed_the_same_way(journal):
    book, before = bought_on_16(journal)
    book.pending_max_days = 3
    gone = SyntheticMarket(missing={"UPA"})

    assert book.settle(snapshot_for(["UPA"], on=17, market=gone)) == ([], [])  # first run to find it missing
    assert book.settle(snapshot_for(["UPA"], on=20, market=gone)) == ([], [])  # three days on: not yet
    fills, notes = book.settle(snapshot_for(["UPA"], on=23, market=gone))

    assert [(f.action, f.trade_date) for f in fills] == [("close", session(16))]
    assert "no longer tracked" in notes[0] and journal.positions("a") == []


def test_a_small_revision_of_an_old_price_is_accepted_as_a_correction(journal):
    book, before = bought_on_16(journal)

    class Revised(SyntheticMarket):
        def path(self, symbol, end):
            frame = super().path(symbol, end)
            if end > session(16):                                          # the 16th is restated 2% higher
                for column in ("open", "high", "low", "close", "adj_close"):
                    frame.loc[session(16), column] *= 1.02
            return frame

    snapshot = snapshot_for(["UPA"], on=17, market=Revised())
    fills, notes = book.settle(snapshot)
    held = book.holdings(snapshot)[0]

    assert fills == [] and len(notes) == 1
    assert "Taken as a price correction by the source" in notes[0] and "2.0% apart" in notes[0]
    assert held.shares == pytest.approx(before.shares) and held.unrealized_pct == pytest.approx(0.6, abs=0.02)
    assert book.settle(snapshot_for(["UPA"], on=18, market=Revised())) == ([], [])     # nothing to say the next day


def test_a_revision_too_large_to_be_a_correction_is_refused(journal):
    book, before = bought_on_16(journal)

    class Revised(SyntheticMarket):
        def path(self, symbol, end):
            frame = super().path(symbol, end)
            if end > session(16):
                for column in ("open", "high", "low", "close", "adj_close"):
                    frame.loc[session(16), column] *= 1.08
            return frame

    snapshot = snapshot_for(["UPA"], on=17, market=Revised())
    _, notes = book.settle(snapshot)
    assert "do not agree" in notes[0] and book.holdings(snapshot)[0].unrealized_pct is None


def test_a_position_with_no_data_at_all_is_left_alone(journal):
    book, before = bought_on_16(journal)
    book.settle(snapshot_for(["UPB"], on=17))                             # UPA is not in the snapshot

    snapshot = snapshot_for(["UPA"], on=18, market=split_on(17))
    book.settle(snapshot)
    assert book.holdings(snapshot)[0].shares == pytest.approx(before.shares * 2)


# ── What a finished run reports ──────────────────────────────────────────────

def test_fills_stay_unreported_until_a_run_claims_them(journal):
    book, _ = bought_on_16(journal)
    assert [(f.ticker, f.action) for f in book.unreported_fills()] == [("UPA", "buy")]

    with journal.transaction():
        journal.mark_trades_reported("a", "run2")
    assert book.unreported_fills() == []
    assert journal.trades("a")[0]["reported_run_id"] == "run2"


def positions_on_disk(journal):
    """What another process would see: only what has been committed."""
    other = Journal(journal.path)
    try:
        return [row["ticker"] for row in other.positions("a")]
    finally:
        other.close()


def put(journal, ticker):
    journal.put_position("a", ticker, 1, 1, session(13), session(13), 1.0)


def test_nested_transactions_commit_once_and_roll_back_together(journal):
    with journal.transaction():
        put(journal, "UPA")
        with journal.transaction():
            put(journal, "UPB")
        assert positions_on_disk(journal) == []                           # the inner block did not commit
    assert positions_on_disk(journal) == ["UPA", "UPB"]

    with pytest.raises(RuntimeError, match="outer block fails"):
        with journal.transaction():
            put(journal, "UPC")
            with journal.transaction():
                journal.drop_position("a", "UPA")
            raise RuntimeError("the outer block fails after the inner one finished")
    assert positions_on_disk(journal) == ["UPA", "UPB"]
    assert [row["ticker"] for row in journal.positions("a")] == ["UPA", "UPB"]


def test_an_inner_failure_that_is_caught_still_saves_nothing(journal):
    with pytest.raises(JournalError, match="none of it was saved"):
        with journal.transaction():
            put(journal, "UPA")
            try:
                with journal.transaction():
                    put(journal, "UPB")
                    raise ValueError("inner step fails")
            except ValueError:
                pass                                                      # swallowed, as careless code might
            put(journal, "UPC")
    assert positions_on_disk(journal) == [] and journal.positions("a") == []

    with journal.transaction():                                           # the journal is usable afterwards
        put(journal, "UPA")
    assert positions_on_disk(journal) == ["UPA"]


def test_a_transaction_holds_the_write_lock_from_its_first_line(journal):
    import sqlite3

    other = Journal(journal.path, timeout=0.05)
    with journal.transaction():                                           # nothing written yet
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with other.transaction():
                other.put_position("a", "UPB", 1, 1, session(13), session(13), 1.0)
    with other.transaction():
        other.put_position("a", "UPB", 1, 1, session(13), session(13), 1.0)
    other.close()
    assert [row["ticker"] for row in journal.positions("a")] == ["UPB"]


def test_a_failed_commit_saves_nothing_and_leaves_the_journal_usable(journal, monkeypatch):
    def locked():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(journal, "_commit", locked)
    with pytest.raises(RuntimeError, match="database is locked"):
        with journal.transaction():
            put(journal, "UPA")
    monkeypatch.undo()

    assert journal.positions("a") == [] and positions_on_disk(journal) == []
    with journal.transaction():                                           # no transaction was left open
        put(journal, "UPB")
    assert positions_on_disk(journal) == ["UPB"]


def test_the_shadow_book_cannot_be_changed_outside_a_transaction(journal):
    for write in (
        lambda: put(journal, "UPA"),
        lambda: journal.drop_position("a", "UPA"),
        lambda: journal.put_pending("a", "UPA", "buy", session(13), "run"),
        lambda: journal.drop_pending("a", "UPA"),
        lambda: journal.drop_pending_from("a", session(13)),
        lambda: journal.add_trade("a", "UPA", "buy", 1, 1, session(13), session(13), None),
        lambda: journal.mark_trades_reported("a", "run"),
    ):
        with pytest.raises(JournalError, match="only be changed inside"):
            write()
    with journal.transaction():                                           # and nothing was left half open
        put(journal, "UPA")
    assert positions_on_disk(journal) == ["UPA"]


def test_a_reader_does_not_block_the_journal_from_saving(journal):
    import sqlite3

    assert journal._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    reader = sqlite3.connect(journal.path, timeout=0.05)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM shadow_positions").fetchall()          # a read transaction left open
    with journal.transaction():
        put(journal, "UPA")
    assert reader.execute("SELECT COUNT(*) FROM shadow_positions").fetchone()[0] == 0    # its own snapshot
    reader.close()
    assert positions_on_disk(journal) == ["UPA"]


def test_a_journal_in_another_format_is_refused_not_misread(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE shadow_trades (id INTEGER PRIMARY KEY, arm TEXT)")
    old.commit()
    old.close()
    with pytest.raises(JournalError, match="journal format 0; this program uses format 1"):
        Journal(path)

    fresh = tmp_path / "new.sqlite3"
    Journal(fresh).close()
    assert Journal(fresh).positions("a") == []                            # reopening its own format is fine


