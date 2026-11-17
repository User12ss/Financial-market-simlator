"""The deterministic parts of the daily pipeline: screen, macro view, output checks, journal."""

from datetime import date

import pytest

import config
from agents.ticker.analyst import TickerAnalyst
from data.snapshot import SnapshotBuilder
from journal.store import Journal, JournalError
from models import signals
from models.signals import DomainAssessment, Draft, SectorCall
from models.trading import Position
from pipeline import universe as universe_rules
from pipeline.checks import check_assessment, check_proposals, check_ruling, check_view
from pipeline.macro import combine, render_regime
from pipeline.report import render_view
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


def test_every_field_a_model_fills_in_is_required():
    # Hosts that enforce the schema strictly refuse one with optional fields, so "may be null" is
    # written as a required field that allows null.
    drafts = [c for c in vars(signals).values() if isinstance(c, type) and issubclass(c, Draft) and c is not Draft]
    assert len(drafts) >= 6
    for draft in drafts:
        schema = draft.model_json_schema()
        assert set(schema["required"]) == set(schema["properties"]), draft.__name__


def test_the_analyst_is_told_the_range_its_stop_has_to_be_in():
    low, high = config.SIZING_STOP_MIN_ATR_MULTIPLE, config.SIZING_STOP_MAX_ATR_MULTIPLE
    assert f"between {low:g} and {high:g} times the ATR(14) shown in the data" in TickerAnalyst.system_prompt
    assert "The one exception is stop_price" in TickerAnalyst.system_prompt


def stop_below(item, atrs):
    """A stop this many ATRs under the item's last close, as a model reading the data would give it."""
    return round(item.price.close - atrs * item.technicals.atr14, 2)


def test_a_proposed_stop_inside_the_range_is_kept_with_its_reason():
    item = snapshot_for(["UPA"]).tickers["UPA"]
    stop = stop_below(item, 3)

    checked, reason = check_view("UPA", session(13), False, view(stop=stop), item, [])

    assert reason == "" and checked.data_warnings == []
    assert (checked.proposed_stop, checked.stop_reason) == (stop, "Just below the 50-day average.")
    assert f"Proposed stop for a new position: {stop:,.2f}. Just below the 50-day average." in render_view(checked)


@pytest.mark.parametrize("atrs, why", [
    (0.5, "and the nearest allowed is"), (5, "and the furthest allowed is"), (-1, "it is not below the"),
])
def test_a_proposed_stop_outside_the_range_is_left_out_and_the_view_is_kept(atrs, why):
    item = snapshot_for(["UPA"]).tickers["UPA"]
    stop = stop_below(item, atrs)

    checked, reason = check_view("UPA", session(13), False, view(stop=stop), item, [])

    assert reason == "" and checked.direction == "bullish"
    assert (checked.proposed_stop, checked.stop_reason) == (None, "")
    [warning] = checked.data_warnings
    assert warning.startswith(f"The proposed stop of {stop:,.2f} is not used: ") and why in warning
    assert "Proposed stop" not in render_view(checked).split("(Automatic data check")[0]


@pytest.mark.parametrize("direction, held", [("bearish", False), ("neutral", False), ("bullish", True)])
def test_a_stop_is_only_for_opening_a_position(direction, held):
    # Not bullish: nothing will be bought. Already held: the position keeps the stop it was opened with.
    item = snapshot_for(["UPA"]).tickers["UPA"]

    checked, _ = check_view("UPA", session(13), held, view(direction, stop=stop_below(item, 3)), item, [])

    assert (checked.proposed_stop, checked.stop_reason, checked.data_warnings) == (None, "", [])
    assert "stop" not in render_view(checked).lower()


def test_a_proposed_stop_cannot_be_judged_without_an_atr():
    item = snapshot_for(["UPA"]).tickers["UPA"]
    stop = stop_below(item, 3)

    checked, _ = check_view("UPA", session(13), False, view(stop=stop), item.model_copy(update={"technicals": None}), [])

    assert checked.proposed_stop is None
    assert checked.data_warnings == [
        f"The proposed stop of {stop:,.2f} is not used: there is no usable close and ATR to judge it by."
    ]


def test_figures_in_the_reason_for_a_stop_are_traced_like_any_other_claim_about_the_data():
    # The reason reaches the debaters and the manager, who would otherwise take a made-up level as data.
    item = snapshot_for(["UPA"]).tickers["UPA"]
    stop = stop_below(item, 3)
    draft = view(stop=stop)
    draft.stop_reason = f"At {stop:.2f}, just below support at 31,415.92."

    checked, _ = check_view("UPA", session(13), False, draft, item, [])
    assert checked.proposed_stop == stop
    [warning] = checked.data_warnings
    assert "1 figure(s) not found" in warning and "31,415.92" in warning and f"{stop:.2f}" not in warning

    checked, _ = check_view("UPA", session(13), False, draft, item, ["a level of 31,415.92"])
    assert checked.data_warnings == []


def test_the_stop_is_the_analysts_own_number_only_while_it_is_kept():
    item = snapshot_for(["UPA"]).tickers["UPA"]
    kept, refused = stop_below(item, 3), stop_below(item, 0.5)

    checked, _ = check_view("UPA", session(13), False, view(thesis=f"I would be out at {kept:.2f}.", stop=kept), item, [])
    assert checked.data_warnings == []

    checked, _ = check_view("UPA", session(13), False, view(thesis=f"I would be out at {refused:.2f}.", stop=refused), item, [])
    assert len(checked.data_warnings) == 2
    assert f"{refused:.2f}" in checked.data_warnings[0] and "not found in the data" in checked.data_warnings[0]
    assert "is not used" in checked.data_warnings[1]


def test_a_view_without_a_stop_says_nothing_about_one():
    item = snapshot_for(["UPA"]).tickers["UPA"]
    checked, _ = check_view("UPA", session(13), False, view(), item, [])
    assert checked.proposed_stop is None and checked.data_warnings == [] and "stop" not in render_view(checked).lower()


def test_ruling_with_bad_confidence_is_refused():
    record, reason = check_ruling("UPA", "top_candidate", [], ruling(confidence=3))
    assert record is None and "judge's confidence" in reason
    record, _ = check_ruling("UPA", "exit_signal", [], ruling(stance="do_not_own", confidence=0.7))
    assert record.stance == "do_not_own" and record.trigger == "exit_signal"


# ── Proposals ────────────────────────────────────────────────────────────────

def holding(ticker):
    return Position(ticker=ticker, shares=10, avg_cost=100.0, entry_date=session(10), value=1000.0)


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


# ── The journal's guarantees ─────────────────────────────────────────────────

@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.sqlite3")


def put(journal, key):
    journal.write("INSERT INTO checkpoints VALUES ('a', '2026-11-13', ?, 'x')", (key,))


def keys_on_disk(journal):
    """What another process would see: only what has been committed."""
    other = Journal(journal.path)
    try:
        return [row["key"] for row in other.rows("SELECT key FROM checkpoints ORDER BY key")]
    finally:
        other.close()


def test_nested_transactions_commit_once_and_roll_back_together(journal):
    with journal.transaction():
        put(journal, "one")
        with journal.transaction():
            put(journal, "two")
        assert keys_on_disk(journal) == []                                # the inner block did not commit
    assert keys_on_disk(journal) == ["one", "two"]

    with pytest.raises(RuntimeError, match="outer block fails"):
        with journal.transaction():
            put(journal, "three")
            with journal.transaction():
                journal.write("DELETE FROM checkpoints WHERE key = 'one'")
            raise RuntimeError("the outer block fails after the inner one finished")
    assert keys_on_disk(journal) == ["one", "two"]


def test_an_inner_failure_that_is_caught_still_saves_nothing(journal):
    with pytest.raises(JournalError, match="none of it was saved"):
        with journal.transaction():
            put(journal, "one")
            try:
                with journal.transaction():
                    put(journal, "two")
                    raise ValueError("inner step fails")
            except ValueError:
                pass                                                      # swallowed, as careless code might
            put(journal, "three")
    assert keys_on_disk(journal) == []

    with journal.transaction():                                           # the journal is usable afterwards
        put(journal, "one")
    assert keys_on_disk(journal) == ["one"]


def test_a_transaction_holds_the_write_lock_from_its_first_line(journal):
    import sqlite3

    other = Journal(journal.path, timeout=0.05)
    with journal.transaction():                                           # nothing written yet
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with other.transaction():
                put(other, "theirs")
    with other.transaction():
        put(other, "theirs")
    other.close()
    assert keys_on_disk(journal) == ["theirs"]


def test_a_failed_commit_saves_nothing_and_leaves_the_journal_usable(journal, monkeypatch):
    def locked():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(journal, "_commit", locked)
    with pytest.raises(RuntimeError, match="database is locked"):
        with journal.transaction():
            put(journal, "one")
    monkeypatch.undo()

    assert keys_on_disk(journal) == []
    with journal.transaction():                                           # no transaction was left open
        put(journal, "two")
    assert keys_on_disk(journal) == ["two"]


def test_books_cannot_be_changed_outside_a_transaction(journal):
    with pytest.raises(JournalError, match="only be changed inside"):
        put(journal, "one")
    with journal.transaction():                                           # and nothing was left half open
        put(journal, "one")
    assert keys_on_disk(journal) == ["one"]


def test_a_reader_does_not_block_the_journal_from_saving(journal):
    import sqlite3

    assert journal.rows("PRAGMA journal_mode")[0][0] == "wal"
    reader = sqlite3.connect(journal.path, timeout=0.05)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM checkpoints").fetchall()               # a read transaction left open
    with journal.transaction():
        put(journal, "one")
    assert reader.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0     # its own snapshot
    reader.close()
    assert keys_on_disk(journal) == ["one"]


def test_a_journal_in_another_format_is_refused_not_misread(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    old.commit()
    old.close()
    with pytest.raises(JournalError, match="journal format 0; this program uses format 2"):
        Journal(path)

    fresh = tmp_path / "new.sqlite3"
    Journal(fresh).close()
    assert Journal(fresh).arms() == []                                    # reopening its own format is fine


def test_checkpoints_are_kept_per_arm_and_session_and_can_be_cleared(journal):
    journal.save_checkpoint("a", session(13), "view:UPA", "one")
    journal.save_checkpoint("a", session(13), "view:UPA", "two")          # saved again: the later one stands
    journal.save_checkpoint("b", session(13), "view:UPA", "theirs")
    journal.save_checkpoint("a", session(16), "view:UPA", "later")

    assert journal.checkpoint("a", session(13), "view:UPA") == "two"
    assert journal.checkpoint("a", session(13), "view:UPB") is None

    journal.clear_checkpoints("a", session(13))
    assert journal.checkpoint("a", session(13), "view:UPA") is None
    assert journal.checkpoint("b", session(13), "view:UPA") == "theirs"

    journal.prune_checkpoints(session(16))
    assert journal.checkpoint("b", session(13), "view:UPA") is None
    assert journal.checkpoint("a", session(16), "view:UPA") == "later"
