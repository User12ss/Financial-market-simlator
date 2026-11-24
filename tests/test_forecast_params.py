"""The week's parameters: what was planted in a made-up market is found, and every large move is counted once."""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from forecast import params as P
from forecast.params import (
    BURN_IN, ForecastError, ParamStore, Settings, due_cutoff, estimate, estimate_series, ewma, for_session,
    log_returns, reaction_days, state_multipliers,
)
from forecast.regime import RegimeModel
from tests.forecast_fakes import END, TRUE_REGIME, MadeUpMarket, Name, at

NOW = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)
SETTINGS = Settings(states=2, market_years=20, history_years=5, ewma_lambda=0.94, jump_sigmas=4.0,
                    min_earnings=8, drift_shrink_days=5040)


def quarterly(market: MadeUpMarket, count: int, hour: int = 16, minute: int = 5) -> list[datetime]:
    """`count` reports a quarter apart, the last about a month before the end, at that New York time."""
    return [at(market.session_before(END, back=22 + 63 * index), hour, minute) for index in range(count)][::-1]


def is_report_move(move: float) -> bool:
    """One of the planted 8% report-day moves, give or take what taking the factors out leaves."""
    return 0.07 < abs(move) < 0.09


def planted() -> MadeUpMarket:
    """ACME reports after the close, twelve times, and each report moves the next session by 8%.
    One ordinary day it fell 12% for no stated reason."""
    blank = MadeUpMarket()
    reports = quarterly(blank, 12)
    reacted = {blank.sessions[blank.sessions.index(stamp.date()) + 1]: (0.08 if index % 2 else -0.08)
               for index, stamp in enumerate(reports)}
    jump_day = blank.session_before(END, back=200)
    assert jump_day not in reacted
    return MadeUpMarket({
        "ACME": Name(beta=1.3, loading=0.8, fund="XLK", vol=0.012, reports=reports + [at(date(2027, 1, 28), 16, 5)],
                     report_moves=reacted, jumps={jump_day: -0.12}),
        "CALM": Name(beta=0.6, fund="XLP", loading=0.5, vol=0.008, reports=quarterly(blank, 3, hour=7, minute=0)),
        "SPDR": Name(beta=1.0, vol=0.002, quote_type="ETF"),
    })


@pytest.fixture(scope="module")
def market():
    return planted()


@pytest.fixture(scope="module")
def estimated(market):
    return estimate(market, ["ACME", "CALM", "SPDR"], END, version=1, now=NOW, settings=SETTINGS)


# ── What was planted is found ────────────────────────────────────────────────

def test_the_market_states_and_each_names_factors_are_found(estimated):
    assert estimated.cutoff == END and estimated.version == 1
    assert np.abs(np.array(estimated.regime.transition) - np.array(TRUE_REGIME.transition)).max() <= 0.05
    assert all(abs(found / true - 1) <= 0.10 for found, true in zip(estimated.regime.vols, TRUE_REGIME.vols))
    acme = estimated.series["ACME"]
    assert acme.beta == pytest.approx(1.3, abs=0.05)
    # Three standard errors: own noise 0.012 against a fund move of 0.008 over 1250 days gives 0.042 each.
    assert acme.sector == "XLK" and acme.sector_loading == pytest.approx(0.8, abs=0.13)
    assert acme.vol == pytest.approx(0.012, rel=0.08)
    calm = estimated.series["CALM"]
    assert calm.beta == pytest.approx(0.6, abs=0.05) and calm.sector == "XLP"
    assert estimated.series["SPDR"].sector == "" and estimated.series["SPDR"].sector_loading == 0.0
    # Every sector fund is a series of its own, with the market as its only factor.
    assert estimated.series["XLK"].beta == pytest.approx(1.0, abs=0.03) and estimated.series["XLK"].sector == ""


def test_report_days_and_jump_days_are_in_neither_the_ordinary_sample_nor_each_others_count(market, estimated):
    """Completion standard: the residual sample holds no report day and no jump day, and the jump rate no report day."""
    acme, name = estimated.series["ACME"], market.names["ACME"]
    assert acme.report_days == sorted(name.report_moves)
    assert len(acme.earnings) == 12
    assert all(abs(z) > 4 for z in acme.earnings)                    # the 8% moves, in units of an ordinary day
    # The unexplained 12% fall is a jump, and no report day is. (A stray ordinary day can cross the line too.)
    assert sum(1 for move in acme.jumps if move == pytest.approx(-0.12, abs=0.01)) == 1
    assert not any(is_report_move(move) for move in acme.jumps) and 1 <= len(acme.jumps) <= 3
    # The sample is everything else, and nothing in it is as large as either kind.
    assert len(acme.residuals) == acme.days - BURN_IN - 12 - len(acme.jumps)
    assert max(abs(z) for z in acme.residuals) < SETTINGS.jump_sigmas
    # Each is in units of the volatility expected that day: together they have a spread of about one.
    assert float(np.std(acme.residuals)) == pytest.approx(1.0, abs=0.12)
    assert acme.jump_rate == pytest.approx(len(acme.jumps) / (acme.days - BURN_IN - 12))
    assert acme.report_lag == 1 and acme.earnings_from == "own" and acme.reports_known


def test_without_report_dates_the_same_days_are_counted_as_jumps(market):
    """What the separation is for: told nothing about reports, the estimate takes them for jumps out of nowhere."""
    index = log_returns(market.frame("^GSPC", END))
    fund = estimate_series("XLK", log_returns(market.frame("XLK", END)), index, None, "", [], SETTINGS)[1]
    own = log_returns(market.frame("ACME", END))
    told = estimate_series("ACME", own, index, fund, "XLK", market.names["ACME"].reports, SETTINGS)[0]
    untold = estimate_series("ACME", own, index, fund, "XLK", None, SETTINGS)[0]
    assert sum(1 for move in told.jumps if is_report_move(move)) == 0
    assert sum(1 for move in untold.jumps if is_report_move(move)) == 12
    assert len(untold.jumps) == len(told.jumps) + 12
    assert untold.jump_rate > 4 * told.jump_rate
    assert untold.report_days == [] and untold.earnings == [] and not untold.reports_known


def test_a_large_day_does_not_raise_the_volatility_expected_afterwards():
    rng = np.random.default_rng(3)
    moves = rng.normal(0, 0.01, 300)
    moves[150], moves[200] = 0.09, -0.07
    skip = np.zeros(300, dtype=bool)
    skip[200] = True                                   # a report day
    sigma, jumps, tomorrow = ewma(moves, skip, 0.94, 4.0)
    assert jumps[150] and not jumps[200]                # the report day is left out, not called a jump
    assert sigma[151] == sigma[150] and sigma[201] == sigma[200]
    assert jumps.sum() == 1
    assert tomorrow == pytest.approx(0.01, rel=0.35)
    # An ordinary day does move it.
    assert sigma[101] != sigma[100]


def test_the_first_days_only_set_the_starting_level():
    moves = np.concatenate([np.full(BURN_IN, 0.02) * np.resize([1, -1], BURN_IN), np.full(10, 0.0)])
    sigma, jumps, _ = ewma(moves, np.zeros(len(moves), dtype=bool), 0.94, 4.0)
    assert np.allclose(sigma[:BURN_IN + 1], 0.02)
    assert sigma[BURN_IN + 1] == pytest.approx(0.02 * np.sqrt(0.94))
    assert not jumps.any()


def test_which_session_reacts_to_a_report():
    sessions = [date(2026, 11, day) for day in (16, 17, 18, 19, 20, 23, 24)]
    moves = np.array([0.0, 0.001, 0.05, 0.002, 0.001, 0.06, 0.0])
    after_close = at(date(2026, 11, 17), 16, 5)
    before_open = at(date(2026, 11, 19), 7, 0)
    assert reaction_days([after_close], sessions, moves) == ([date(2026, 11, 18)], 1)
    assert reaction_days([before_open], sessions, moves) == ([date(2026, 11, 19)], 0)
    assert reaction_days([at(date(2026, 11, 19), 16, 0)], sessions, moves) == ([date(2026, 11, 20)], 1)   # at the bell
    # No hour given: the larger own move of the two days decides.
    assert reaction_days([at(date(2026, 11, 17))], sessions, moves) == ([date(2026, 11, 18)], 1)
    assert reaction_days([at(date(2026, 11, 18))], sessions, moves) == ([date(2026, 11, 18)], 0)
    # Dated on a Saturday: Monday reacts.
    assert reaction_days([at(date(2026, 11, 21), 9, 0)], sessions, moves) == ([date(2026, 11, 23)], 0)
    # Before the sample, after it, or reacting beyond its last day: left out.
    assert reaction_days([at(date(2026, 11, 2), 16, 5), at(date(2027, 1, 28), 16, 5), at(date(2026, 11, 24), 17, 0)],
                         sessions, moves) == ([], 0)
    # A time that names no zone is read as New York's, not refused: one name's odd record must not stop the rest.
    assert reaction_days([datetime(2026, 11, 17, 16, 5)], sessions, moves) == ([date(2026, 11, 18)], 1)
    assert reaction_days([datetime(2026, 11, 19, 12, 0)], sessions, moves) == ([date(2026, 11, 19)], 0)
    # A half day closes at 13:00 (the Friday after Thanksgiving): five past one is after the close.
    week = [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    assert reaction_days([at(date(2026, 11, 27), 13, 5)], week, np.zeros(3)) == ([date(2026, 11, 30)], 1)
    assert reaction_days([at(date(2026, 11, 27), 12, 55)], week, np.zeros(3)) == ([date(2026, 11, 27)], 0)
    assert reaction_days([at(date(2026, 11, 25), 13, 5)], week, np.zeros(3)) == ([date(2026, 11, 25)], 0)     # a full day
    # Dated on the holiday itself: the next session there is.
    assert reaction_days([at(date(2026, 11, 26), 17, 0)], week, np.zeros(3)) == ([date(2026, 11, 27)], 0)
    # The usual lag is the majority's; a tie counts as the same day.
    assert reaction_days([after_close, before_open], sessions, moves)[1] == 0
    assert reaction_days([after_close, before_open, at(date(2026, 11, 20), 18, 0)], sessions, moves)[1] == 1


def test_a_names_own_average_return_counts_for_a_fifth_of_itself(estimated):
    acme = estimated.series["ACME"]
    assert acme.drift == pytest.approx(acme.raw_drift * acme.days / (acme.days + 5040))
    assert abs(acme.drift) < abs(acme.raw_drift) / 4


# ── Report days too few, or unknown ──────────────────────────────────────────

def test_too_few_report_days_of_its_own_and_the_sectors_stand_in_or_none_do(market, estimated):
    calm = estimated.series["CALM"]
    assert calm.report_lag == 0 and len(calm.report_days) == 3
    # Its sector has no other name: three report days in all, fewer than eight.
    assert calm.earnings == [] and calm.earnings_from == "none"
    assert any("Too few report days" in note for note in calm.notes)
    fund = estimated.series["SPDR"]
    assert fund.earnings_from == "none" and fund.report_days == [] and fund.notes == []
    assert market.asked["reports"].count("SPDR") == 0       # a fund has no reports to ask for

    peer = planted()
    peer.names["PEER"] = Name(beta=1.1, fund="XLK", loading=0.7, reports=quarterly(peer, 2))
    peer = MadeUpMarket(peer.names)
    pooled = estimate(peer, ["ACME", "PEER"], END, version=1, now=NOW, settings=SETTINGS).series["PEER"]
    assert pooled.earnings_from == "sector" and len(pooled.earnings) == 12 + 2
    assert pooled.earnings == sorted(pooled.earnings)
    assert any("its sector's (XLK) are drawn from instead" in note for note in pooled.notes)


def test_when_the_source_cannot_say_when_a_name_reported_that_is_recorded_and_nothing_stands_in(market):
    quiet = planted()
    quiet.no_reports = {"ACME"}
    acme = estimate(quiet, ["ACME"], END, version=1, now=NOW, settings=SETTINGS).series["ACME"]
    assert not acme.reports_known and acme.reports == [] and acme.report_days == []
    assert acme.earnings == [] and acme.earnings_from == "none"
    assert any("When it reported is not known" in note and "scripted outage" in note for note in acme.notes)
    assert sum(1 for move in acme.jumps if is_report_move(move)) == 12


def test_a_source_with_no_record_of_report_dates_at_all_is_the_same_case(market):
    class Bare:
        def __init__(self, inner):
            self.daily_bars, self.fundamentals = inner.daily_bars, inner.fundamentals

    acme = estimate(Bare(planted()), ["ACME"], END, version=1, now=NOW, settings=SETTINGS).series["ACME"]
    assert not acme.reports_known
    assert any("has no record of report dates" in note for note in acme.notes)


# ── The states and a name's own moves ────────────────────────────────────────

def test_own_moves_that_grow_in_the_turbulent_state_are_measured_as_growing():
    market = MadeUpMarket({"WILD": Name(vol=0.01, turbulent=2.0), "MILD": Name(vol=0.01, turbulent=2.0)},
                          fund_turbulent=2.0)
    found = estimate(market, ["WILD", "MILD"], END, version=1, now=NOW, settings=SETTINGS)
    calm, turbulent = found.state_vol
    # Pooled over the two names and the sector funds, all planted at twice the size. The days
    # are sorted by a probability, not by the state itself, which blurs the ratio a little.
    assert turbulent / calm == pytest.approx(2.0, rel=0.15)
    # On an average day the two put together are the name's own volatility: neither is simply 1.
    assert calm < 1 < turbulent


def test_names_that_do_not_care_about_the_state_get_multipliers_near_one(estimated):
    assert estimated.state_vol == pytest.approx([1.0, 1.0], abs=0.12)


def test_one_state_has_nothing_to_multiply_and_a_state_hardly_seen_borrows_the_indexs_ratio():
    single = RegimeModel(means=[0.0], vols=[0.01], transition=[[1.0]])
    assert state_multipliers(single, pd.DataFrame(), {}) == ([1.0], [])

    model = RegimeModel(means=[0.0, 0.0], vols=[0.01, 0.03], transition=[[0.99, 0.01], [0.5, 0.5]])
    sessions = [date(2026, 1, 1 + index) for index in range(30)]
    states = pd.DataFrame({0: 1.0, 1: 0.0}, index=sessions)        # the names' history never saw the second state
    moves = pd.Series(np.resize([0.01, -0.01], 30), index=sessions)
    multipliers, notes = state_multipliers(model, states, {"X": (moves, np.ones(30, dtype=bool))})
    assert multipliers[0] == pytest.approx(1.0)
    assert multipliers[1] == pytest.approx(3.0)                     # 0.03 against an average day of 0.01
    assert len(notes) == 1 and "too few turbulent days" in notes[0]


# ── What cannot be estimated is left out, in words ───────────────────────────

def test_a_name_without_usable_history_is_left_out_and_said(market):
    short = planted()
    short.missing = {"CALM"}
    found = estimate(short, ["ACME", "CALM", "NEVER"], END, version=1, now=NOW, settings=SETTINGS)
    assert "ACME" in found.series and "CALM" not in found.series and "NEVER" not in found.series
    assert any(problem.startswith("CALM has 0 daily returns") for problem in found.problems)
    assert any(problem.startswith("NEVER has 0 daily returns") for problem in found.problems)
    # And the source is asked nothing more about a name that cannot be used anyway.
    assert not {"CALM", "NEVER"} & set(short.asked["facts"] + short.asked["reports"])


def test_an_unknown_sector_leaves_the_market_as_the_only_factor(market):
    blind = planted()
    blind.no_facts = {"ACME"}
    acme = estimate(blind, ["ACME"], END, version=1, now=NOW, settings=SETTINGS).series["ACME"]
    assert acme.sector == "" and acme.sector_loading == 0.0 and not acme.sector_known
    assert any("Its sector is not known" in note for note in acme.notes)


def test_no_index_no_estimate():
    down = planted()
    down.fail = True
    with pytest.raises(ForecastError, match="index's history could not be fetched"):
        estimate(down, ["ACME"], END, version=1, now=NOW, settings=SETTINGS)
    gone = planted()
    gone.missing = {"^GSPC"}
    with pytest.raises(ForecastError, match="has 0 daily returns"):
        estimate(gone, ["ACME"], END, version=1, now=NOW, settings=SETTINGS)


def test_nothing_after_the_cutoff_is_read(market):
    earlier = date(2026, 6, 26)
    asked = planted()
    found = estimate(asked, ["ACME"], earlier, version=1, now=NOW, settings=SETTINGS)
    assert found.cutoff == earlier
    assert asked.asked["ends"] == [earlier, earlier]             # the index, then the names: neither past the cut-off
    assert max(found.series["ACME"].report_days) <= earlier
    assert found.series["ACME"].days < 1250
    # A source that hands back later bars all the same is not believed.
    class Generous:
        def __init__(self, inner):
            self.inner, self.fundamentals, self.earnings_reports = inner, inner.fundamentals, inner.earnings_reports

        def daily_bars(self, symbols, start, end):
            return self.inner.daily_bars(symbols, start, END)

    assert estimate(Generous(planted()), ["ACME"], earlier, version=1, now=NOW, settings=SETTINGS).model_dump_json() \
        == found.model_dump_json()


def test_a_names_history_is_counted_up_to_the_cutoff_and_no_further():
    """A source that hands back later bars as well: a name with too little history by the cut-off is still left out."""
    class Generous:
        def __init__(self, inner):
            self.inner, self.asked = inner, inner.asked
            self.fundamentals, self.earnings_reports = inner.fundamentals, inner.earnings_reports

        def daily_bars(self, symbols, start, end):
            return self.inner.daily_bars(symbols, start, END)

    young = planted()
    cutoff = young.sessions[-(young.days + 1) + 200]             # the names' two-hundredth session
    found = estimate(Generous(young), ["ACME"], cutoff, version=1, now=NOW, settings=SETTINGS)
    assert "ACME" not in found.series
    assert f"ACME has 200 daily returns up to {cutoff}; 250 are needed." in found.problems
    assert young.asked["facts"] == [] and young.asked["reports"] == []


def test_the_same_data_gives_the_same_parameters_to_the_last_digit(market, estimated):
    again = estimate(planted(), ["ACME", "CALM", "SPDR"], END, version=1, now=NOW, settings=SETTINGS)
    assert again.model_dump_json() == estimated.model_dump_json()


# ── What is already known is not asked again ─────────────────────────────────

def test_what_the_snapshot_and_last_weeks_estimate_know_is_not_fetched_again(market, estimated):
    fresh = planted()
    estimate(fresh, ["ACME", "CALM", "SPDR"], END, version=2, now=NOW, settings=SETTINGS, previous=estimated)
    assert fresh.asked["facts"] == []                           # sectors carried over
    assert fresh.asked["reports"] == ["CALM"]                    # ACME still has a report to come on record; CALM has none

    told = planted()
    estimate(told, ["ACME", "CALM"], END, version=1, now=NOW, settings=SETTINGS,
             known={"ACME": ("Technology", "EQUITY"), "CALM": ("Consumer Defensive", "EQUITY")})
    assert told.asked["facts"] == []


def test_report_dates_that_cannot_be_refreshed_stay_as_they_were_and_that_is_said(estimated):
    stale = planted()
    stale.no_reports = {"CALM"}
    calm = estimate(stale, ["CALM"], END, version=2, now=NOW, settings=SETTINGS, previous=estimated).series["CALM"]
    assert calm.reports_known and len(calm.report_days) == 3
    assert any("could not be brought up to date" in note for note in calm.notes)


# ── Versions on disk, and which one a session uses ───────────────────────────

def test_the_cutoff_is_the_last_session_of_the_week_before():
    assert due_cutoff(date(2026, 11, 23)) == date(2026, 11, 20)      # Monday
    assert due_cutoff(date(2026, 11, 27)) == date(2026, 11, 20)      # the Friday of the same week
    assert due_cutoff(date(2026, 11, 30)) == date(2026, 11, 27)      # the half day after Thanksgiving is a session
    assert due_cutoff(date(2026, 4, 6)) == date(2026, 4, 2)          # Good Friday was no session


def test_versions_are_kept_one_file_each_and_a_broken_one_is_passed_over(tmp_path, estimated):
    store = ParamStore(tmp_path / "forecast")
    assert store.latest() is None and store.versions() == [] and store.next_version() == 1
    path = store.save(estimated)
    assert path.name == "params_0001_2026-11-20.json" and not list(path.parent.glob("*.part"))
    second = estimated.model_copy(update={"version": 2, "cutoff": date(2026, 11, 27)})
    store.save(second)
    assert store.versions() == [1, 2] and store.next_version() == 3
    assert store.latest().version == 2
    assert store.latest(cutoff_by=date(2026, 11, 26)).version == 1
    assert store.latest(cutoff_by=date(2026, 11, 19)) is None
    assert store.load(1) == estimated and store.load(7) is None
    (tmp_path / "forecast" / "params_0003_2026-12-04.json").write_text("{not json")
    (tmp_path / "forecast" / "params_notes.json").write_text("{}")
    assert store.latest().version == 2 and store.next_version() == 4


def test_a_session_estimates_once_a_week_and_uses_that_all_week(tmp_path):
    store, market = ParamStore(tmp_path), planted()
    params, new, problem = for_session(store, market, ["ACME", "CALM"], date(2026, 11, 23), now=NOW)
    assert new and problem == "" and params.version == 1 and params.cutoff == END
    bars = len(market.asked["bars"])

    for day in (23, 24, 25, 27):
        again, new, problem = for_session(store, market, ["ACME", "CALM"], date(2026, 11, day), now=NOW)
        assert not new and problem == "" and again == params
    assert len(market.asked["bars"]) == bars                     # nothing was fetched again

    later = MadeUpMarket(market.names, end=date(2026, 11, 27))
    third, new, problem = for_session(store, later, ["ACME", "CALM"], date(2026, 11, 30), now=NOW)
    assert new and third.version == 2 and third.cutoff == date(2026, 11, 27)
    assert later.asked["facts"] == []                             # carried over from version 1
    # An earlier session, looked at again, still gets the parameters of its own week.
    assert for_session(store, market, ["ACME"], date(2026, 11, 24), now=NOW)[0].version == 1


def test_when_this_weeks_estimate_fails_last_weeks_is_used_and_that_is_said(tmp_path):
    store, market = ParamStore(tmp_path), planted()
    first = for_session(store, market, ["ACME"], date(2026, 11, 23), now=NOW)[0]
    market.fail = True
    params, new, problem = for_session(store, market, ["ACME"], date(2026, 11, 30), now=NOW)
    assert params == first and not new
    assert "could not be estimated on data to 2026-11-27" in problem and "Version 1, on data to 2026-11-20" in problem
    assert store.versions() == [1]

    empty = ParamStore(tmp_path / "none")
    params, new, problem = for_session(empty, market, ["ACME"], date(2026, 11, 30), now=NOW)
    assert params is None and not new and problem.startswith("No parameters could be estimated on data to 2026-11-27.")


def test_settings_come_from_the_configuration(monkeypatch):
    monkeypatch.setattr(P.config, "FORECAST_STATES", 3)
    monkeypatch.setattr(P.config, "FORECAST_JUMP_SIGMAS", 5.0)
    settings = Settings.from_config()
    assert settings.states == 3 and settings.jump_sigmas == 5.0 and settings.ewma_lambda == 0.94


def test_the_estimate_on_the_newest_data_is_the_one_found_whatever_its_number(tmp_path, estimated):
    """A session looked at again can leave a higher number on an older week. It must not hide this week's."""
    store = ParamStore(tmp_path)
    store.save(estimated.model_copy(update={"version": 1, "cutoff": date(2026, 11, 27)}))
    store.save(estimated.model_copy(update={"version": 2, "cutoff": date(2026, 11, 20)}))
    assert store.latest(cutoff_by=date(2026, 11, 27)).version == 1
    assert store.latest().version == 1
    assert store.latest(cutoff_by=date(2026, 11, 26)).version == 2
    market = planted()
    params, new, problem = for_session(store, market, ["ACME"], date(2026, 11, 30), now=NOW)
    assert (params.version, new, problem) == (1, False, "") and market.asked["bars"] == []


def test_a_version_on_disk_is_never_written_over(tmp_path, estimated):
    store = ParamStore(tmp_path)
    store.save(estimated)
    before = (tmp_path / "params_0001_2026-11-20.json").read_bytes()
    with pytest.raises(FileExistsError, match="Version 1 of the parameters is already on disk"):
        store.save(estimated.model_copy(update={"cutoff": date(2026, 11, 27)}))
    with pytest.raises(FileExistsError):
        store.save(estimated)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["params_0001_2026-11-20.json"]
    assert (tmp_path / "params_0001_2026-11-20.json").read_bytes() == before


class Raced(ParamStore):
    """A store that another process writes to between this one's look and its save."""

    def __init__(self, directory, other):
        super().__init__(directory)
        self.other, self.looked = other, 0

    def latest(self, cutoff_by=None):
        self.looked += 1
        return None if self.looked == 1 else super().latest(cutoff_by)

    def next_version(self):
        version = super().next_version()
        if self.other is not None:
            ParamStore(self.directory).save(self.other.model_copy(update={"version": version}))
            self.other = None
        return version


def test_two_estimates_at_the_same_moment_end_as_one_version(tmp_path, estimated):
    """A forecast names the version it was made from, so the file under that number must be the one it read."""
    theirs = estimated.model_copy(update={"problems": ["written by the other process"]})
    store = Raced(tmp_path, theirs)
    params, new, problem = for_session(store, planted(), ["ACME", "CALM"], date(2026, 11, 23), now=NOW)
    assert (params.version, new, problem) == (1, False, "") and params.problems == ["written by the other process"]
    assert ParamStore(tmp_path).versions() == [1]
    # Where the other process estimated for another week, this estimate is kept too, under the next number.
    apart = tmp_path / "apart"
    store = Raced(apart, estimated.model_copy(update={"cutoff": date(2026, 11, 13)}))
    params, new, problem = for_session(store, planted(), ["ACME"], date(2026, 11, 23), now=NOW)
    assert (params.version, params.cutoff, new) == (2, END, True)
    assert ParamStore(apart).versions() == [1, 2] and ParamStore(apart).load(2) == params


class OneFreakDay:
    """A market whose index is one even regime with a single day far outside it."""

    def __init__(self, inner):
        self.inner, self.fundamentals, self.earnings_reports = inner, inner.fundamentals, inner.earnings_reports

    def daily_bars(self, symbols, start, end):
        frames = self.inner.daily_bars(symbols, start, end)
        if "^GSPC" in frames:
            sessions = frames["^GSPC"].index
            steps = np.random.default_rng(8).normal(0.0003, 0.009, len(sessions))
            steps[len(steps) // 2] = -0.25
            frame = frames["^GSPC"].copy()
            frame["close"] = frame["adj_close"] = 100.0 * np.exp(np.cumsum(steps))
            frames["^GSPC"] = frame
        return frames


def test_where_two_states_cannot_be_told_apart_one_is_used_and_that_is_said():
    """One crash day in an otherwise even history gives no second regime. The estimate goes on with one state."""
    found = estimate(OneFreakDay(planted()), ["ACME"], END, version=1, now=NOW, settings=SETTINGS)
    assert found.regime.states == 1 and found.regime.names == ["single"] and found.regime.transition == [[1.0]]
    assert found.state_at_cutoff == [1.0] and found.state_vol == [1.0]
    assert found.problems[0].startswith(
        "2 states of the market could not be estimated from the index's history (No 2 states could be told apart")
    assert found.problems[0].endswith("1 is used instead.")
    assert "ACME" in found.series and found.settings.states == 2      # what was asked for is still on record
