"""scripts/daily_run.sh and scripts/schedule.sh, run against stand-ins.

Each test copies the script into a throwaway project whose `venv/bin/python`
prints a canned pipeline output, and puts stand-ins for osascript, caffeinate,
curl, ollama and launchctl first on PATH. Nothing here runs the pipeline,
starts Ollama or touches launchd.
"""

import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the scripts are for macOS")

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

STUBS = {
    "python": """#!/bin/bash
if [ "$1" = "-c" ]; then
    echo "${FAKE_SETTINGS:-fake fake http://localhost:11434}"
    exit 0
fi
echo "$*" >> "$FAKE_DIR/python_runs"
echo $$ > "$FAKE_DIR/python_pid"
cat "$FAKE_DIR/output.txt"
[ ! -f "$FAKE_DIR/stubborn" ] || trap '' TERM
[ ! -f "$FAKE_DIR/hang" ] || exec sleep 300
exit "$(cat "$FAKE_DIR/exit_code")"
""",
    "osascript": """#!/bin/bash
printf '%s\\t%s\\n' "${@: -2:1}" "${@: -1}" >> "$FAKE_DIR/notifications"
""",
    "caffeinate": """#!/bin/bash
echo "$*" >> "$FAKE_DIR/caffeinate_calls"
""",
    "curl": """#!/bin/bash
[ -f "$FAKE_DIR/ollama_up" ]
""",
    "ollama": """#!/bin/bash
echo $$ > "$FAKE_DIR/ollama_pid"
touch "$FAKE_DIR/ollama_up"
exec sleep 300
""",
    "launchctl": """#!/bin/bash
echo "$*" >> "$FAKE_DIR/launchctl_calls"
case "$1" in
    print)
        [ -f "$FAKE_DIR/loaded" ] || exit 113
        printf '%s = {\\n\\tstate = %s\\n\\truns = 2\\n\\tlast exit code = 0\\n\\tsockets = {\\n\\t\\tstate = active\\n\\t}\\n}\\n' \\
            "$2" "$(cat "$FAKE_DIR/state" 2>/dev/null || echo 'not running')" ;;
    bootstrap)
        left="$(cat "$FAKE_DIR/bootstrap_failures" 2>/dev/null || echo 0)"
        if [ "$left" -gt 0 ]; then
            echo $((left - 1)) > "$FAKE_DIR/bootstrap_failures"
            echo "Bootstrap failed: 5: Input/output error" >&2
            exit 5
        fi
        touch "$FAKE_DIR/loaded" ;;
    bootout) rm -f "$FAKE_DIR/loaded" ;;
esac
""",
}

OK = """Paper trading: simulated orders only. Arms: fake/a
  views     Ticker Analyst -> portfolio_manager [UPA]: Rating: ok. I would own this one.

fake/a: ok
  3 views, 3 debates, 2 accepted proposal(s), 2 order(s) placed
  paper account: equity 100,000.00, cash 100,000.00
"""


class Sandbox:
    def __init__(self, tmp_path: Path, project: str = "Projects/sim") -> None:
        self.home = (tmp_path / "home").resolve()
        self.project = self.home / project
        self.fake = tmp_path / "fake"
        bin_dir = tmp_path / "bin"
        for folder in (self.project / "scripts", self.project / "venv" / "bin", self.fake, bin_dir):
            folder.mkdir(parents=True, exist_ok=True)
        for name in ("daily_run.sh", "schedule.sh"):
            shutil.copy(SCRIPTS / name, self.project / "scripts" / name)
        for name, body in STUBS.items():
            target = (self.project / "venv" / "bin" if name == "python" else bin_dir) / name
            target.write_text(body)
            target.chmod(0o755)
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.home), "FAKE_DIR": str(self.fake)}
        self.logs = self.project / "data_store" / "logs"
        self.plist = self.home / "Library" / "LaunchAgents" / "com.financial-market-sim.daily.plist"

    def pipeline_prints(self, output: str, exit_code: int = 0) -> None:
        (self.fake / "output.txt").write_text(output)
        (self.fake / "exit_code").write_text(str(exit_code))

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", str(self.project / "scripts" / script), *args],
            env={**self.env, **env}, capture_output=True, text=True, timeout=60,
        )

    def notifications(self) -> list[tuple[str, str]]:
        path = self.fake / "notifications"
        if not path.exists():
            return []
        return [tuple(line.split("\t", 1)) for line in path.read_text().splitlines()]

    def log(self) -> str:
        return "".join(path.read_text() for path in sorted(self.logs.glob("daily_*.log")))

    def launchctl_calls(self) -> list[str]:
        path = self.fake / "launchctl_calls"
        return [line.split()[0] for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def box(tmp_path):
    sandbox = Sandbox(tmp_path)
    sandbox.pipeline_prints(OK)
    return sandbox


# ── daily_run.sh ─────────────────────────────────────────────────────────────

def test_a_finished_run_is_logged_and_announced(box):
    done = box.run("daily_run.sh")

    assert done.returncode == 0
    assert box.notifications() == [("Daily pipeline finished", "fake/a: ok, 2 order(s) placed")]
    log = box.log()
    assert "daily run starts" in log and "fake/a: ok" in log and "daily run ends, exit 0" in log
    assert (box.fake / "python_runs").read_text() == "-m pipeline.daily\n"
    # The Mac is kept awake for as long as the pipeline's own process lives.
    assert (box.fake / "caffeinate_calls").read_text() == f"-i -w {(box.fake / 'python_pid').read_text().strip()}\n"
    # Nothing is left behind that would stop or confuse the next run.
    assert sorted(path.name for path in box.logs.iterdir()) == [path.name for path in box.logs.glob("daily_*.log")]


def test_a_failed_run_passes_on_the_exit_code_and_says_why(box):
    box.pipeline_prints(
        "Paper trading: simulated orders only. Arms: fake/a\n\n"
        "fake/a: failed. Closing prices for 2026-11-16 are not published yet. Run again later.\n", exit_code=1,
    )
    done = box.run("daily_run.sh")

    assert done.returncode == 1
    assert box.notifications() == [(
        "Daily pipeline did not finish",
        "fake/a: failed. Closing prices for 2026-11-16 are not published yet. Run again later. "
        "The next scheduled run tries again.",
    )]
    assert "daily run ends, exit 1" in box.log()


def test_a_session_that_was_skipped_is_not_announced(box):
    box.pipeline_prints("Paper trading: simulated orders only. Arms: fake/a\n\n"
                        "fake/a: skipped. Signals for 2026-11-16 already exist.\n")
    assert box.run("daily_run.sh").returncode == 0
    assert box.notifications() == []


def test_a_run_that_finished_with_problems_says_so(box):
    box.pipeline_prints(OK.replace("fake/a: ok", "fake/a: degraded"))
    box.run("daily_run.sh")
    assert box.notifications() == [("Daily pipeline finished with problems", "fake/a: degraded, 2 order(s) placed")]


def test_the_reason_given_is_the_one_from_the_configuration_that_failed(box):
    # Text that only looks like a result: an indented event, and a line with no provider/model in front.
    box.pipeline_prints(
        "Paper trading: simulated orders only. Arms: fake/a, fake/b\n"
        "  views     Ticker Analyst -> portfolio_manager [UPA]: fake/z: failed. Said by a model.\n"
        "Rating: failed.\n\n"
        "fake/a: ok\n  3 views, 3 debates, 1 accepted proposal(s), 1 order(s) placed\n\n"
        "fake/b: failed. The model is not available: connection refused.\n", exit_code=1,
    )
    box.run("daily_run.sh")
    [(title, text)] = box.notifications()
    assert title == "Daily pipeline did not finish"
    assert text.startswith("fake/b: failed. The model is not available: connection refused.")


def test_only_real_results_decide_whether_a_run_is_announced(box):
    box.pipeline_prints(
        "Paper trading: simulated orders only. Arms: fake/a\n"
        "  views     Ticker Analyst -> portfolio_manager [UPA]: fake/z: ok\n"
        "Rating: ok\n\n"
        "fake/a: skipped. Signals for 2026-11-16 already exist.\n",
    )
    box.run("daily_run.sh")
    assert box.notifications() == []


def test_orders_are_counted_over_every_configuration_that_ran(box):
    box.pipeline_prints(
        "Paper trading: simulated orders only. Arms: fake/a, fake/b, ollama/qwen2.5-coder:7b\n\n"
        "fake/a: skipped. Signals for 2026-11-16 already exist.\n\n"
        "fake/b: ok\n  3 views, 3 debates, 3 accepted proposal(s), 3 order(s) placed\n\n"
        "ollama/qwen2.5-coder:7b: ok\n  3 views, 3 debates, 1 accepted proposal(s), 1 order(s) placed\n",
    )
    box.run("daily_run.sh")
    assert box.notifications() == [
        ("Daily pipeline finished", "fake/b: ok; ollama/qwen2.5-coder:7b: ok, 4 order(s) placed"),
    ]


def test_a_long_reason_is_cut_by_character_not_by_byte(box):
    box.pipeline_prints(f"fake/a: failed. {'é' * 400}\n", exit_code=1)
    box.run("daily_run.sh")
    [(_, text)] = box.notifications()                  # reading it back would fail on half a character
    assert text == f"fake/a: failed. {'é' * 204} The next scheduled run tries again."


def test_notifications_can_be_turned_off(box):
    assert box.run("daily_run.sh", DAILY_NOTIFY="0").returncode == 0
    assert box.notifications() == [] and "daily run ends, exit 0" in box.log()


def test_a_missing_python_is_reported_and_nothing_is_left_behind(box):
    (box.project / "venv" / "bin" / "python").unlink()
    done = box.run("daily_run.sh")
    assert done.returncode == 1
    assert box.notifications()[0][0] == "Daily pipeline did not run"
    assert "Cannot start: no Python" in box.log()
    assert not (box.logs / "run.lock").exists()


def test_each_run_reads_only_what_it_added_to_the_days_log(box):
    box.run("daily_run.sh")                            # finishes, and says so
    box.pipeline_prints("fake/a: skipped. Signals for 2026-11-16 already exist.\n")
    box.run("daily_run.sh")
    box.pipeline_prints(OK.replace("2 order(s)", "1 order(s)"))
    box.run("daily_run.sh")

    # The skipped run announces nothing, and the third counts its own order only.
    assert [text for _, text in box.notifications()] == ["fake/a: ok, 2 order(s) placed", "fake/a: ok, 1 order(s) placed"]
    assert box.log().count("daily run starts") == 3


def lock_held_by(box, pid):
    lock = box.logs / "run.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{pid}\n")
    return lock


def test_a_second_run_does_not_start_beside_one_that_is_going(box, tmp_path):
    other = tmp_path / "other" / "daily_run.sh"        # a run of this script, as `ps` will show it
    other.parent.mkdir()
    other.write_text("sleep 30\necho done\n")
    going = subprocess.Popen(["/bin/bash", str(other)])
    try:
        lock = lock_held_by(box, going.pid)
        done = box.run("daily_run.sh")
    finally:
        going.kill()
        going.wait()

    assert done.returncode == 0
    assert not (box.fake / "python_runs").exists() and box.notifications() == []
    assert f"not started: another run (process {going.pid}) is still going" in box.log()
    assert (lock / "pid").read_text().strip() == str(going.pid)     # the other run's lock is left alone


def test_a_lock_left_by_a_run_that_was_killed_is_taken_over(box):
    gone = subprocess.Popen(["/usr/bin/true"])
    gone.wait()
    lock = lock_held_by(box, gone.pid)

    assert box.run("daily_run.sh").returncode == 0
    assert (box.fake / "python_runs").exists() and not lock.exists()


def test_a_lock_whose_pid_now_belongs_to_something_else_does_not_stop_the_run(box):
    # After a crash or a restart the number in the lock can be any process at all: here, this test.
    lock = lock_held_by(box, os.getpid())

    assert box.run("daily_run.sh").returncode == 0
    assert (box.fake / "python_runs").exists() and not lock.exists()
    assert "not started" not in box.log()


def test_stopping_the_script_stops_the_pipeline_and_the_ollama_it_started(box):
    (box.fake / "hang").touch()
    script = subprocess.Popen(
        ["/bin/bash", str(box.project / "scripts" / "daily_run.sh")],
        env={**box.env, "FAKE_SETTINGS": "ollama ollama http://localhost:11434"},
    )
    try:
        for _ in range(100):
            if (box.fake / "python_pid").exists() and (box.fake / "python_pid").read_text().strip():
                break
            time.sleep(0.1)
        pipeline, ollama = (int((box.fake / name).read_text()) for name in ("python_pid", "ollama_pid"))
        os.kill(pipeline, 0)                           # both are running
        os.kill(ollama, 0)
        script.send_signal(signal.SIGTERM)
        assert script.wait(timeout=30) == 143
    finally:
        script.kill()

    for pid in (pipeline, ollama):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not (box.logs / "run.lock").exists()
    assert "stopped by a signal" in box.log() and box.notifications() == []


def test_a_second_signal_does_not_cut_the_clean_up_short(box):
    # A pipeline that does not end when asked: the script waits for it, then ends it for good.
    for name in ("hang", "stubborn"):
        (box.fake / name).touch()
    script = subprocess.Popen(
        ["/bin/bash", str(box.project / "scripts" / "daily_run.sh")],
        env={**box.env, "FAKE_SETTINGS": "ollama ollama http://localhost:11434"},
    )
    try:
        for _ in range(100):
            if (box.fake / "python_pid").exists() and (box.fake / "python_pid").read_text().strip():
                break
            time.sleep(0.1)
        pipeline, ollama = (int((box.fake / name).read_text()) for name in ("python_pid", "ollama_pid"))
        script.send_signal(signal.SIGTERM)
        time.sleep(2)                                  # the script is now waiting for the pipeline to end
        script.send_signal(signal.SIGHUP)
        assert script.wait(timeout=40) == 143
    finally:
        script.kill()

    for pid in (pipeline, ollama):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not (box.logs / "run.lock").exists()


def test_ollama_is_started_when_the_model_needs_it_and_stopped_afterwards(box):
    done = box.run("daily_run.sh", FAKE_SETTINGS="ollama ollama http://localhost:11434")

    assert done.returncode == 0
    assert "Ollama is not running: starting it for this run." in box.log()
    pid = int((box.fake / "ollama_pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert (box.fake / "python_runs").exists()


def test_an_ollama_that_is_already_running_is_used_and_left_running(box):
    (box.fake / "ollama_up").touch()
    box.run("daily_run.sh", FAKE_SETTINGS="ollama ollama http://localhost:11434")
    assert not (box.fake / "ollama_pid").exists() and "starting it" not in box.log()


@pytest.mark.parametrize("settings, said", [
    ("fake fake http://localhost:11434", ""),                                   # the model is not on Ollama
    ("ollama ollama http://elsewhere:11434", "cannot be started from here"),    # not this machine's default
])
def test_ollama_is_not_started_when_it_is_not_this_machines_to_start(box, settings, said):
    box.run("daily_run.sh", FAKE_SETTINGS=settings)
    assert not (box.fake / "ollama_pid").exists()
    assert said in box.log() and (box.fake / "python_runs").exists()


def test_only_the_fast_model_being_on_ollama_is_enough_to_start_it(box):
    box.run("daily_run.sh", FAKE_SETTINGS="anthropic ollama http://127.0.0.1:11434")
    assert (box.fake / "ollama_pid").exists()


def test_anything_printed_before_the_settings_does_not_shift_them(box):
    # A notice some library prints while the settings are imported.
    box.run("daily_run.sh", FAKE_SETTINGS="a notice about something\nollama ollama http://localhost:11434")
    assert (box.fake / "ollama_pid").exists()


# ── schedule.sh ──────────────────────────────────────────────────────────────

def intervals(box):
    with box.plist.open("rb") as file:
        return [(item["Hour"], item["Minute"]) for item in plistlib.load(file)["StartCalendarInterval"]]


def test_install_writes_the_job_and_loads_it(box):
    done = box.run("schedule.sh", "install")

    assert done.returncode == 0, done.stderr
    with box.plist.open("rb") as file:
        job = plistlib.load(file)
    assert job["Label"] == "com.financial-market-sim.daily"
    assert job["ProgramArguments"] == ["/bin/bash", str(box.project / "scripts" / "daily_run.sh")]
    assert job["WorkingDirectory"] == str(box.project)
    assert job["StandardOutPath"] == job["StandardErrorPath"] == str(box.logs / "launchd.log")
    assert intervals(box) == [(18, 0), (20, 0), (22, 0), (23, 45), (7, 0)]
    assert "bootstrap" in box.launchctl_calls() and "bootout" not in box.launchctl_calls()
    assert "runs every day at: 18:00 20:00 22:00 23:45 07:00 (this Mac's local time" in done.stdout
    assert box.logs.is_dir()


def test_times_are_read_as_hours_and_minutes_leading_zeros_included(box):
    done = box.run("schedule.sh", "install", DAILY_RUN_TIMES="7:05 08:09 0:00 23:59")
    assert intervals(box) == [(7, 5), (8, 9), (0, 0), (23, 59)]
    assert "07:05 08:09 00:00 23:59" in done.stdout


@pytest.mark.parametrize("times", ["24:00", "8:60", "7:5", "abc", "007:05", "18:00 25:00", "   "])
def test_a_time_that_is_not_a_time_of_day_installs_nothing(box, times):
    done = box.run("schedule.sh", "install", DAILY_RUN_TIMES=times)
    assert done.returncode == 1 and done.stderr
    assert not box.plist.exists() and box.launchctl_calls() == []


@pytest.mark.parametrize("folder", ["Desktop", "Documents", "Downloads"])
def test_install_refuses_a_project_macos_will_not_let_a_scheduled_job_read(tmp_path, folder):
    sandbox = Sandbox(tmp_path, project=f"{folder}/work/sim")
    done = sandbox.run("schedule.sh", "install")
    assert done.returncode == 1 and "move the project first" in done.stderr
    assert not sandbox.plist.exists() and sandbox.launchctl_calls() == []


def test_a_project_path_with_awkward_characters_survives_into_the_job(tmp_path):
    sandbox = Sandbox(tmp_path, project="My <Work> & Projects/sim")
    assert sandbox.run("schedule.sh", "install").returncode == 0
    with sandbox.plist.open("rb") as file:
        assert plistlib.load(file)["ProgramArguments"][1] == str(sandbox.project / "scripts" / "daily_run.sh")


def test_installing_again_replaces_the_job_that_was_loaded(box):
    box.run("schedule.sh", "install")
    done = box.run("schedule.sh", "install", DAILY_RUN_TIMES="19:30")
    assert done.returncode == 0 and intervals(box) == [(19, 30)]
    calls = [call for call in box.launchctl_calls() if call != "print"]
    assert calls == ["bootstrap", "bootout", "bootstrap"]


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_a_run_in_progress_is_never_cut_off_by_changing_the_schedule(box, command):
    box.run("schedule.sh", "install")
    before = box.plist.read_text()
    (box.fake / "state").write_text("running")

    done = box.run("schedule.sh", command, DAILY_RUN_TIMES="19:30")

    assert done.returncode == 1 and "A run is going right now" in done.stderr
    assert "bootout" not in box.launchctl_calls() and box.plist.read_text() == before


def test_install_tries_again_when_launchd_is_not_ready_for_the_job(box):
    (box.fake / "bootstrap_failures").write_text("1")
    done = box.run("schedule.sh", "install")
    assert done.returncode == 0 and "Installed." in done.stdout
    assert box.launchctl_calls().count("bootstrap") == 2


def test_install_says_so_when_launchd_never_takes_the_job(box):
    (box.fake / "bootstrap_failures").write_text("9")
    done = box.run("schedule.sh", "install")
    assert done.returncode == 1 and "launchd did not load the schedule" in done.stderr
    assert "Installed." not in done.stdout


def test_uninstall_unloads_the_job_and_removes_only_its_file(box):
    box.run("schedule.sh", "install")
    (box.logs / "daily_2026-11-16.log").write_text("kept\n")

    done = box.run("schedule.sh", "uninstall")

    assert done.returncode == 0 and "Removed." in done.stdout
    assert not box.plist.exists() and "bootout" in box.launchctl_calls()
    assert (box.logs / "daily_2026-11-16.log").read_text() == "kept\n"
    assert box.run("schedule.sh", "uninstall").stdout.strip() == "Not installed."


def test_status_shows_the_jobs_own_state_its_times_and_the_last_log_lines(box):
    assert box.run("schedule.sh", "status").stdout.strip() == "Not installed."

    box.run("schedule.sh", "install", DAILY_RUN_TIMES="18:00 7:05")
    assert "No run has been logged yet." in box.run("schedule.sh", "status").stdout

    (box.logs / "daily_2026-11-16.log").write_text("fake/a: ok\n=== daily run ends, exit 0\n")
    out = box.run("schedule.sh", "status").stdout
    assert "  state = not running\n  runs = 2\n  last exit code = 0\n" in out and "state = active" not in out
    assert "times (local): 18:00 07:05" in out
    assert "  fake/a: ok\n  === daily run ends, exit 0" in out


def test_run_starts_the_job_unless_it_is_not_installed_or_already_going(box):
    done = box.run("schedule.sh", "run")
    assert done.returncode == 1 and "Not installed" in done.stderr

    box.run("schedule.sh", "install")
    assert "Started." in box.run("schedule.sh", "run").stdout
    assert box.launchctl_calls().count("kickstart") == 1

    (box.fake / "state").write_text("running")
    assert "A run is already going." in box.run("schedule.sh", "run").stdout
    assert box.launchctl_calls().count("kickstart") == 1


def test_an_unknown_command_prints_the_usage(box):
    done = box.run("schedule.sh", "sometimes")
    assert done.returncode == 2 and "Usage:" in done.stderr and box.launchctl_calls() == []
