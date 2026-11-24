"""python -m forecast: estimate ahead of time, see what is on record, work a forecast out again."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import config
from forecast.__main__ import main
from forecast.params import ParamStore
from pipeline.quant import seed_of
from tests.desk_fakes import ARM, hold, ny
from tests.forecast_fakes import FAR, MadeUpMarket, forecast_desk, rising_names

SATURDAY = datetime(2026, 11, 21, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A project folder of its own: journal, parameters and a universe of made-up names."""
    universe = tmp_path / "universe.txt"
    universe.write_text("\n".join(sorted(rising_names())))
    monkeypatch.setattr(config, "JOURNAL_PATH", str(tmp_path / "journal.sqlite3"))
    monkeypatch.setattr(config, "FORECAST_DIR", str(tmp_path / "forecast"))
    monkeypatch.setattr(config, "UNIVERSE_FILE", str(universe))
    return tmp_path


def test_estimating_ahead_of_monday_and_finding_it_done(home, capsys):
    market = MadeUpMarket(rising_names(), end=FAR)
    assert main(["status"], now=SATURDAY) == 0
    out = capsys.readouterr().out
    assert "No parameters on disk" in out and "The coming session needs an estimate on data to 2026-11-20." in out

    assert main(["estimate"], provider=market, now=SATURDAY) == 0
    out = capsys.readouterr().out
    assert out.startswith("Estimated: parameters version 1, on data to 2026-11-20, for the session of 2026-11-23. "
                          "15 name(s) and sector fund(s).")
    assert "The index: 0.71% a day when calm, 1.90% a day when turbulent; on 2026-11-20 it was calm 0." in out
    assert ParamStore().versions() == [1]
    assert not (home / "journal.sqlite3").exists()               # looking for a book does not create a journal

    fetched = len(market.asked["bars"])
    assert main(["estimate"], provider=market, now=SATURDAY) == 0
    assert capsys.readouterr().out.startswith("Already on disk: parameters version 1, on data to 2026-11-20")
    assert len(market.asked["bars"]) == fetched

    assert main(["status"], now=SATURDAY) == 0
    out = capsys.readouterr().out
    assert "Version 1: data to 2026-11-20, 15 name(s), 2 state(s), 0 note(s).  <- the coming session's" in out


def test_an_estimate_that_fails_says_so_and_leaves_nothing_behind(home, capsys):
    market = MadeUpMarket(rising_names(), end=FAR)
    market.fail = True
    assert main(["estimate"], provider=market, now=SATURDAY) == 1
    out = capsys.readouterr().out
    assert out.startswith("No parameters could be estimated on data to 2026-11-20. The index's history could not be fetched")
    assert ParamStore().versions() == []


def test_mondays_meeting_uses_what_was_estimated_on_the_weekend(home, capsys):
    market = MadeUpMarket(rising_names(), end=FAR)
    main(["estimate"], provider=market, now=SATURDAY)
    capsys.readouterr()
    desk = forecast_desk(home, ny(23, 18, 30), market=market)
    result = hold(desk, ny(23, 18, 30))
    kinds = [event.kind for event in desk.journal.events(result.run_id) if event.stage == "forecast"]
    assert "forecast_parameters" not in kinds and kinds[0] == "market_state"
    assert desk.journal.forecast_set(ARM, result.table.session_date).params_version == 1


def test_a_forecast_on_record_is_worked_out_again_and_compared(home, capsys):
    desk = forecast_desk(home, ny(23, 18, 30))
    tuesday = [hold(desk, ny(day, 18, 30)) for day in (23, 24)][-1]
    desk.journal.close()

    assert main(["verify"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == (f"{ARM}, 2026-11-24: worked out again from the snapshot, parameters version 1 and seed "
                           f"{seed_of(tuesday.run_id)}: the same, digit for digit.")
    assert main(["verify", "--arm", "nobody/else"]) == 0 and capsys.readouterr().out == ""

    assert main(["status"], now=ny(24, 19, 0)) == 0
    out = capsys.readouterr().out
    assert f"{ARM}: forecasts on 2 session(s), the last for 2026-11-24 (parameters version 1). 2 reconciled, 14 not yet due" in out
    assert "  1 session ahead, 2 forecast(s) made on 1 day(s): the outcome fell " in out

    # A record that has been altered is found out.
    import sqlite3
    db = sqlite3.connect(home / "journal.sqlite3")
    text = db.execute("SELECT set_json FROM forecast_sets WHERE session_date = '2026-11-24'").fetchone()[0]
    db.execute("UPDATE forecast_sets SET set_json = ? WHERE session_date = '2026-11-24'",
               (text.replace('"paths":10000', '"paths":9000'),))
    db.commit()
    db.close()
    assert main(["verify"]) == 1
    assert "NOT the same as the record." in capsys.readouterr().out


def test_verifying_with_no_journal_or_no_parameters_says_so(home, capsys):
    assert main(["verify"]) == 0
    assert capsys.readouterr().out.startswith("No journal at ")
    desk = forecast_desk(home, ny(23, 18, 30))
    hold(desk, ny(23, 18, 30))
    desk.journal.close()
    for file in (home / "forecast").glob("params_*.json"):
        file.unlink()
    assert main(["verify"]) == 1
    assert "cannot be worked out again. Version 1 of the parameters is not on disk any more." in capsys.readouterr().out


def test_falling_back_on_last_weeks_parameters_is_reported_as_trouble(home, capsys):
    market = MadeUpMarket(rising_names(), end=FAR)
    assert main(["estimate"], provider=market, now=SATURDAY) == 0
    capsys.readouterr()
    market.fail = True
    week_later = datetime(2026, 11, 28, 17, 0, tzinfo=timezone.utc)
    assert main(["estimate"], provider=market, now=week_later) == 1
    out = capsys.readouterr().out
    assert out.startswith("This week's parameters could not be estimated on data to 2026-11-27")
    assert "Already on disk: parameters version 1, on data to 2026-11-20, for the session of 2026-11-30." in out
    assert ParamStore().versions() == [1]
