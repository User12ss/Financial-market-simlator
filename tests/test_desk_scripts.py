"""scripts/desk_tick.sh and the desk's schedule, run against the same stand-ins as the daily scripts.

Nothing here runs the desk, starts Ollama or touches launchd.
"""

import os
import plistlib
import subprocess
import sys
import time

import pytest

from tests.test_scripts import Sandbox, lock_held_by

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the scripts are for macOS")

OUTSIDE = "Watch: outside its hours, nothing fetched.\n"
WATCHED = "Watch: 2 name(s) in the book, 1 new headline(s), 4 price(s)\n"
CHECKIN = """Paper trading: simulated orders only. Desk meeting: midday for 2026-11-17. Arm: desk/fake/a
  briefing  News Desk -> all: Since the last meeting: 1 new headline on 1 of the 2 names in the book.

desk/fake/a: ok
  midday check-in: 1 update(s), 0 noted for the close
  2 model calls, 1s of model time
"""
CLOSE = """Paper trading: simulated orders only. Desk meeting: close for 2026-11-17. Arm: desk/fake/a

desk/fake/a: degraded
  3 views, 3 debates, 2 accepted proposal(s), 2 order(s) placed
  warning: something was missing
"""


@pytest.fixture
def box(tmp_path):
    sandbox = Sandbox(tmp_path)
    sandbox.pipeline_prints(CHECKIN)
    (sandbox.fake / "watch.txt").write_text(OUTSIDE)
    sandbox.desk_plist = sandbox.home / "Library" / "LaunchAgents" / "com.financial-market-sim.desk.plist"
    return sandbox


def runs(box) -> list[str]:
    path = box.fake / "python_runs"
    return path.read_text().splitlines() if path.exists() else []


def desk_log(box) -> str:
    return "".join(path.read_text() for path in sorted(box.logs.glob("desk_*.log")))


def caffeinate(box, expected: int = 0) -> list[str]:
    """What the stand-in for caffeinate was called with. The round does not wait for it, so this does, briefly."""
    path = box.fake / "caffeinate_calls"
    for _ in range(30):
        if path.exists() and len(path.read_text().splitlines()) >= expected:
            break
        time.sleep(0.1 if expected else 0.02)
    return path.read_text().splitlines() if path.exists() else []


# ── desk_tick.sh ─────────────────────────────────────────────────────────────

def test_a_round_outside_the_watchs_hours_asks_twice_and_leaves_no_trace(box):
    done = box.run("desk_tick.sh")
    assert done.returncode == 0
    assert runs(box) == ["-m pipeline.desk watch", "-m pipeline.desk due"]
    assert desk_log(box) == "" and caffeinate(box) == [] and box.notifications() == []
    assert not (box.logs / "run.lock").exists()


def test_a_round_of_the_watch_is_logged_and_keeps_the_mac_awake_until_the_next_one(box):
    (box.fake / "watch.txt").write_text(WATCHED)
    done = box.run("desk_tick.sh")
    assert done.returncode == 0
    assert desk_log(box).rstrip().endswith("Watch: 2 name(s) in the book, 1 new headline(s), 4 price(s)")
    assert desk_log(box).startswith("=== 20")
    assert caffeinate(box, 1) == ["-i -t 1200"]         # the 15 minutes to the next round, and 5 more
    assert runs(box) == ["-m pipeline.desk watch", "-m pipeline.desk due"] and box.notifications() == []


@pytest.mark.parametrize("env, calls", [
    ({"DESK_WATCH_MINUTES": "30"}, ["-i -t 2100"]),
    ({"DESK_WATCH_MINUTES": "soon"}, ["-i -t 1200"]),
    ({"DESK_WATCH_MINUTES": ""}, ["-i -t 1200"]),
    ({"DESK_KEEP_AWAKE": "0"}, []),
])
def test_how_long_the_mac_is_kept_awake_follows_the_settings(box, env, calls):
    (box.fake / "watch.txt").write_text(WATCHED)
    assert box.run("desk_tick.sh", **env).returncode == 0
    assert caffeinate(box, len(calls)) == calls


def test_a_watch_that_did_not_finish_is_said_and_the_round_goes_on(box):
    (box.fake / "watch.txt").write_text("")
    (box.fake / "watch_exit").write_text("1")
    (box.fake / "due.txt").write_text("midday\n")
    done = box.run("desk_tick.sh")
    assert done.returncode == 0
    assert "the watch did not finish: see the lines above" in desk_log(box)
    assert runs(box)[-1] == "-m pipeline.desk meet" and caffeinate(box, 1) == [f"-i -w {pid(box)}"]


def pid(box) -> str:
    return (box.fake / "python_pid").read_text().strip()


def test_a_meeting_that_is_due_is_held_logged_and_announced(box):
    (box.fake / "watch.txt").write_text(WATCHED)
    (box.fake / "due.txt").write_text("midday\n")
    done = box.run("desk_tick.sh")
    assert done.returncode == 0
    assert runs(box) == ["-m pipeline.desk watch", "-m pipeline.desk due", "-m pipeline.desk meet"]
    log = desk_log(box)
    assert "desk midday meeting starts" in log and "desk/fake/a: ok" in log and "desk midday meeting ends, exit 0" in log
    assert box.notifications() == [("Desk meeting finished", "desk/fake/a: ok")]
    assert list(box.logs.glob("daily_*.log")) == [], "the daily pipeline's log is not written to"
    assert not (box.logs / "run.lock").exists()


def test_a_close_meeting_says_how_it_ended_and_how_many_orders_it_placed(box):
    box.pipeline_prints(CLOSE)
    (box.fake / "due.txt").write_text("close\n")
    assert box.run("desk_tick.sh").returncode == 0
    assert box.notifications() == [("Desk meeting finished with problems", "desk/fake/a: degraded, 2 order(s) placed")]
    assert "desk close meeting starts" in desk_log(box)


def test_a_meeting_that_failed_passes_on_the_exit_code_and_says_why(box):
    box.pipeline_prints("\ndesk/fake/a: failed. There is no close meeting for 2026-11-16 to build on.\n", exit_code=1)
    (box.fake / "due.txt").write_text("premarket\n")
    done = box.run("desk_tick.sh")
    assert done.returncode == 1
    assert box.notifications() == [(
        "Desk meeting did not finish",
        "desk/fake/a: failed. There is no close meeting for 2026-11-16 to build on. The next scheduled run tries again.",
    )]


@pytest.mark.parametrize("answer", ["", "Cannot start: no model\n", "lunch\n", "close meeting\n"])
def test_only_one_of_the_four_kinds_of_meeting_starts_one(box, answer):
    (box.fake / "due.txt").write_text(answer)
    assert box.run("desk_tick.sh").returncode == 0
    assert "-m pipeline.desk meet" not in runs(box) and box.notifications() == []


@pytest.mark.parametrize("kind", ["premarket", "midday", "afternoon", "close"])
def test_each_kind_of_meeting_is_handed_over_under_its_own_name(box, kind):
    (box.fake / "due.txt").write_text(f"{kind}\n")
    assert box.run("desk_tick.sh").returncode == 0
    assert f"desk {kind} meeting starts" in desk_log(box) and runs(box)[-1] == "-m pipeline.desk meet"


def test_a_meeting_does_not_start_beside_a_run_of_the_daily_pipeline(box, tmp_path):
    other = tmp_path / "other" / "daily_run.sh"
    other.parent.mkdir()
    other.write_text("sleep 30\n")
    going = subprocess.Popen(["/bin/bash", str(other)])
    try:
        lock = lock_held_by(box, going.pid)
        (box.fake / "watch.txt").write_text(WATCHED)
        (box.fake / "due.txt").write_text("midday\n")
        done = box.run("desk_tick.sh")
    finally:
        going.kill()
        going.wait()
    assert done.returncode == 0
    assert runs(box) == ["-m pipeline.desk watch", "-m pipeline.desk due"], "the watch needs no lock; the meeting waits"
    assert f"not started: another run (process {going.pid}) is still going" in desk_log(box)
    assert (lock / "pid").read_text().strip() == str(going.pid) and box.notifications() == []


def test_the_daily_pipeline_does_not_start_beside_a_desk_meeting(box):
    (box.fake / "due.txt").write_text("midday\n")
    (box.fake / "hang").touch()
    meeting = subprocess.Popen(["/bin/bash", str(box.project / "scripts" / "desk_tick.sh")], env=box.env)
    try:
        for _ in range(100):
            if (box.fake / "python_pid").exists() and (box.logs / "run.lock" / "pid").exists():
                break
            time.sleep(0.1)
        holder = (box.logs / "run.lock" / "pid").read_text().strip()
        assert holder == str(meeting.pid), "the meeting holds the lock under the script's own process"
        done = box.run("daily_run.sh")
        assert done.returncode == 0
        log = "".join(path.read_text() for path in box.logs.glob("daily_*.log"))
        assert f"not started: another run (process {holder}) is still going" in log
        assert "-m pipeline.daily" not in runs(box)
    finally:
        meeting.terminate()
        meeting.wait(timeout=30)
    assert not (box.logs / "run.lock").exists()


def test_a_missing_python_is_logged_and_nothing_is_asked(box):
    (box.project / "venv" / "bin" / "python").unlink()
    done = box.run("desk_tick.sh")
    assert done.returncode == 1
    assert "desk round not started: no Python at" in desk_log(box) and runs(box) == []


# ── The desk's schedule ──────────────────────────────────────────────────────

def job(box) -> dict:
    with box.desk_plist.open("rb") as file:
        return plistlib.load(file)


def test_install_desk_writes_a_job_that_runs_a_round_every_fifteen_minutes(box):
    done = box.run("schedule.sh", "install", "desk")
    assert done.returncode == 0, done.stderr
    written = job(box)
    assert written["Label"] == "com.financial-market-sim.desk"
    assert written["ProgramArguments"] == ["/bin/bash", str(box.project / "scripts" / "desk_tick.sh")]
    assert written["WorkingDirectory"] == str(box.project)
    assert written["StartInterval"] == 900 and written["AbandonProcessGroup"] is True
    assert "StartCalendarInterval" not in written
    assert written["StandardOutPath"] == written["StandardErrorPath"] == str(box.logs / "launchd.log")
    assert "The desk runs a round every 15 minute(s)" in done.stdout and "lid open" in done.stdout
    assert not box.plist.exists(), "the daily pipeline's schedule is not touched"


@pytest.mark.parametrize("minutes, seconds", [("1", 60), ("5", 300), ("08", 480), ("60", 3600)])
def test_the_length_of_a_round_can_be_set(box, minutes, seconds):
    done = box.run("schedule.sh", "install", "desk", DESK_WATCH_MINUTES=minutes)
    assert done.returncode == 0 and job(box)["StartInterval"] == seconds
    assert f"every {int(minutes)} minute(s)" in done.stdout


@pytest.mark.parametrize("minutes", ["0", "61", "100", "1.5", "abc", "-5", " "])
def test_a_length_that_is_not_one_to_sixty_minutes_installs_nothing(box, minutes):
    done = box.run("schedule.sh", "install", "desk", DESK_WATCH_MINUTES=minutes)
    assert done.returncode == 1 and "Not a number of minutes from 1 to 60" in done.stderr
    assert not box.desk_plist.exists() and box.launchctl_calls() == []


def test_the_two_schedules_are_installed_and_removed_apart(box):
    assert box.run("schedule.sh", "install").returncode == 0
    assert box.run("schedule.sh", "install", "desk").returncode == 0
    assert box.plist.exists() and box.desk_plist.exists()
    with box.plist.open("rb") as file:
        daily = plistlib.load(file)
    assert "StartCalendarInterval" in daily and "StartInterval" not in daily and "AbandonProcessGroup" not in daily

    done = box.run("schedule.sh", "uninstall", "desk")
    assert done.returncode == 0 and "Removed." in done.stdout
    assert box.plist.exists() and not box.desk_plist.exists()
    # The stand-in for launchctl keeps one "loaded" mark for every job, so the desk's install
    # unloads too. What matters is which job each unload names: never the daily pipeline's.
    unloads = [call for call in (box.fake / "launchctl_calls").read_text().splitlines() if call.startswith("bootout")]
    assert unloads and set(unloads) == {f"bootout gui/{os.getuid()}/com.financial-market-sim.desk"}


def test_status_desk_shows_the_desks_own_job_and_log(box):
    assert box.run("schedule.sh", "status", "desk").stdout.strip() == "Not installed."
    box.run("schedule.sh", "install", "desk", DESK_WATCH_MINUTES="10")
    assert "No run has been logged yet." in box.run("schedule.sh", "status", "desk").stdout
    (box.logs / "daily_2026-11-16.log").write_text("fake/a: ok\n")
    (box.logs / "desk_2026-11-16.log").write_text("desk/fake/a: ok\n=== desk midday meeting ends, exit 0\n")
    out = box.run("schedule.sh", "status", "desk").stdout
    assert "com.financial-market-sim.desk.plist" in out and "a round every 10 minute(s)" in out
    assert "  desk/fake/a: ok\n  === desk midday meeting ends, exit 0" in out
    assert "times (local)" not in out and "  fake/a: ok" not in out


def test_run_desk_starts_the_desks_job(box):
    done = box.run("schedule.sh", "run", "desk")
    assert done.returncode == 1 and "run 'scripts/schedule.sh install desk' first" in done.stderr
    box.run("schedule.sh", "install", "desk")
    done = box.run("schedule.sh", "run", "desk")
    assert "Started." in done.stdout and "/desk_20" in done.stdout
    calls = (box.fake / "launchctl_calls").read_text().splitlines()
    assert calls[-1].startswith("kickstart ") and calls[-1].endswith("/com.financial-market-sim.desk")


def test_a_schedule_that_does_not_exist_prints_the_usage(box):
    done = box.run("schedule.sh", "install", "weekly")
    assert done.returncode == 2 and "Usage:" in done.stderr and "[desk]" in done.stderr
    assert box.launchctl_calls() == [] and not box.plist.exists()
