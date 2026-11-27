"""The desk's plans and their triggers as the journal keeps them."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from journal.store import Journal
from models.signals import UsageSummary
from pipeline import plans
from pipeline.plans import Facts
from tests.test_plans import AVERAGES, draft, item, made

MONDAY, TUESDAY, WEDNESDAY = date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)
ARM = "desk/fake/a"
NOW = datetime(2026, 11, 24, 23, 30, tzinfo=timezone.utc)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.sqlite3")


def plan_for(ticker, given=None, **options):
    return made(given, **options).model_copy(update={"ticker": ticker})


def fired(plan, by="close", day=TUESDAY, at=NOW, **facts):
    seen = Facts(day=day, closed=by == "close", averages=AVERAGES, **facts)
    return plans.found(plan, seen, set(), at=at, by=by)


def test_a_journal_written_before_plans_existed_gains_the_tables_and_keeps_what_it_held(tmp_path):
    first = Journal(tmp_path / "journal.sqlite3")
    first.start_run("r1", ARM, MONDAY, NOW)
    with first.transaction():
        first.write("DROP TABLE plans")
        first.write("DROP TABLE plan_triggers")
    first.close()
    again = Journal(tmp_path / "journal.sqlite3")
    assert again.plans(ARM) == [] and again.plan_triggers(ARM) == [] and len(again.runs(ARM)) == 1


def test_every_version_of_a_plan_is_kept_as_it_was_made(journal):
    first = plan_for("ACME", draft(item()))
    second = made(None, before=first, session=TUESDAY, reasons=["The analyst's view turned."]).model_copy(
        update={"ticker": "ACME"})
    other = plan_for("BETA")
    journal.save_plans([second, other, first])
    assert journal.plans(ARM) == [first, second, other]             # by name, then by version
    assert journal.plans("desk/other") == []
    assert journal.standing_plans(ARM) == {"ACME": second, "BETA": other}


def test_saving_a_version_again_replaces_it(journal):
    first = plan_for("ACME")
    journal.save_plans([first])
    journal.save_plans([first.model_copy(update={"thesis": "Said again."})])
    assert [plan.thesis for plan in journal.plans(ARM)] == ["Said again."]


def test_a_plan_found_over_stands_no_longer_and_a_later_one_for_the_name_does(journal):
    first = plan_for("ACME")
    second = made(None, before=first, session=TUESDAY).model_copy(update={"ticker": "ACME"})
    journal.save_plans([first, second, plan_for("BETA")])
    journal.close_plan(ARM, "ACME", MONDAY, WEDNESDAY, "Sold at the stop on 2026-11-25.")
    kept = journal.plans(ARM)
    assert [(plan.ticker, plan.version, plan.closed_on, plan.closed_why) for plan in kept] == [
        ("ACME", 1, WEDNESDAY, "Sold at the stop on 2026-11-25."),
        ("ACME", 2, WEDNESDAY, "Sold at the stop on 2026-11-25."), ("BETA", 1, None, "")]
    assert list(journal.standing_plans(ARM)) == ["BETA"]
    later = plan_for("ACME", session=date(2026, 11, 30))
    journal.save_plans([later])
    assert journal.standing_plans(ARM)["ACME"] == later and later.opened == date(2026, 11, 30)


def test_a_trigger_is_kept_once_however_often_it_is_found(journal):
    plan = plan_for("ACME", draft(item(), item("price_above", "trim")))
    [wrong] = fired(plan, by="watch", price=96.0)
    assert journal.add_triggers([wrong]) == [wrong]
    later = wrong.model_copy(update={"fired_at": NOW + timedelta(minutes=15), "evidence": "It traded at 95.00."})
    [high] = fired(plan, by="watch", price=109.0, at=NOW + timedelta(minutes=30))
    assert journal.add_triggers([later, high]) == [high]
    assert journal.plan_triggers(ARM) == [wrong, high]
    assert journal.plan_triggers(ARM, status="fired") == [wrong, high] and journal.plan_triggers(ARM, "done") == []
    assert journal.plan_triggers("desk/other") == []


def test_the_same_condition_in_another_plan_for_the_name_is_another_trigger(journal):
    first = plan_for("ACME", draft(item()))
    second = plan_for("ACME", draft(item()), session=date(2026, 11, 30))
    [one], [two] = fired(first, by="watch", price=96.0), fired(second, by="watch", price=96.0, day=date(2026, 12, 1))
    assert one.identity == two.identity and one.opened != two.opened
    assert journal.add_triggers([one, two]) == [one, two]


def test_what_became_of_a_trigger_is_written_over_it(journal):
    plan = plan_for("ACME", draft(item()))
    [wrong] = fired(plan, low=96.0)
    journal.add_triggers([wrong])
    done = wrong.model_copy(update={"status": "done", "result": "Sold 40 at the next open.", "handled_by": "run-2",
                                    "handled_on": TUESDAY})
    journal.save_trigger(done)
    assert journal.plan_triggers(ARM) == [done] and journal.plan_triggers(ARM, "fired") == []
    assert journal.plan_triggers(ARM, "done") == [done]


def test_a_close_meeting_held_again_takes_back_what_the_earlier_one_left(journal):
    journal.start_run("monday", ARM, MONDAY, NOW - timedelta(days=1))
    journal.finish_run("monday", "ok", NOW - timedelta(days=1), UsageSummary())
    journal.start_run("tuesday", ARM, TUESDAY, NOW)
    journal.finish_run("tuesday", "ok", NOW, UsageSummary())
    acme = plan_for("ACME", draft(item(), item("headline", "call_meeting"), item("price_above", "trim")))
    beta, gone = plan_for("BETA"), plan_for("GONE")
    journal.save_plans([acme, beta, gone])
    # Monday's meeting made the three plans. On Tuesday the watch found two conditions, a check-in heard one of
    # them, and Tuesday's close meeting found a third, sold on the first, made a new version and found GONE over.
    [by_watch] = fired(acme, by="watch", price=96.0, at=NOW - timedelta(hours=8))
    [heard] = fired(acme, by="watch", titles=["A subpoena for Acme"], at=NOW - timedelta(hours=7))
    [by_close] = fired(acme, high=109.0)
    second = made(None, before=acme, session=TUESDAY).model_copy(update={"ticker": "ACME", "run_id": "tuesday"})
    journal.add_triggers([by_watch, heard, by_close])
    journal.save_trigger(by_watch.model_copy(update={"status": "done", "result": "Sold.", "handled_by": "tuesday",
                                                     "handled_on": TUESDAY}))
    at_checkin = heard.model_copy(update={"status": "heard", "handled_by": "2026-11-24_desk_called_1", "handled_on": TUESDAY})
    journal.save_trigger(at_checkin)
    journal.save_trigger(by_close.model_copy(update={"status": "done", "handled_by": "tuesday", "handled_on": TUESDAY}))
    journal.save_plans([second])
    journal.close_plan(ARM, "GONE", MONDAY, TUESDAY, "Sold.")

    journal.forget_plans_of(ARM, TUESDAY)

    assert [(plan.ticker, plan.version, plan.closed_on) for plan in journal.plans(ARM)] == [
        ("ACME", 1, None), ("BETA", 1, None), ("GONE", 1, None)]
    # The watch's finding is to be handled again; the check-in's hearing stands; the close meeting's own is gone.
    assert journal.plan_triggers(ARM) == [by_watch, at_checkin]
    assert journal.plan_triggers(ARM, "fired") == [by_watch]


def test_taking_back_one_sessions_meeting_leaves_every_other_sessions_alone(journal):
    journal.start_run("monday", ARM, MONDAY, NOW)
    journal.finish_run("monday", "ok", NOW, UsageSummary())
    acme = plan_for("ACME", draft(item()))
    over = plan_for("OVER")
    journal.save_plans([acme, over])
    journal.close_plan(ARM, "OVER", MONDAY, MONDAY, "The buy did not fill.")
    [found_monday] = fired(acme, day=MONDAY, low=96.0)
    done = found_monday.model_copy(update={"status": "done", "handled_by": "monday", "handled_on": MONDAY})
    journal.add_triggers([done])
    other_arm = plan_for("ACME").model_copy(update={"arm": "desk/other", "session_date": TUESDAY})
    journal.save_plans([other_arm])

    journal.forget_plans_of(ARM, TUESDAY)

    assert [(plan.ticker, plan.closed_on) for plan in journal.plans(ARM)] == [("ACME", None), ("OVER", MONDAY)]
    assert journal.plan_triggers(ARM) == [done]
    assert journal.plans("desk/other") == [other_arm]
