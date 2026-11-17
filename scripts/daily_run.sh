#!/bin/bash
# One unattended run of the daily pipeline (paper trading: simulated orders only).
#
# Starts Ollama when the configured model needs it and it is not already up,
# keeps the Mac awake while the run goes, writes everything to a log, and says
# how the run ended in a notification. Safe to call any number of times a day:
# a session that already has signals is skipped, and a run that could not
# finish (closing prices not published yet, say) is simply tried again by the
# next call. Only one copy runs at a time, and what it started ends with it.
#
# Everything the pipeline reads from the environment is passed through, so
# JOURNAL_PATH, RUNS_DIR and the rest work here as they do on the command line.
# DAILY_LOG_DIR moves the logs; DAILY_NOTIFY=0 turns the notification off.

set -u
# launchd starts jobs in the C locale, where text is cut by byte, not by character.
export LC_ALL=en_US.UTF-8
export PYTHONUNBUFFERED=1

PROJECT="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
cd "$PROJECT" || exit 1
PYTHON="$PROJECT/venv/bin/python"
LOG_DIR="${DAILY_LOG_DIR:-$PROJECT/data_store/logs}"
mkdir -p "$LOG_DIR" || exit 1
LOG="$LOG_DIR/daily_$(date +%Y-%m-%d).log"
LOCK="$LOG_DIR/run.lock"
now() { date '+%Y-%m-%d %H:%M:%S %Z'; }
say() { echo "$*" >> "$LOG"; }

# One run at a time. A second one would decide the same session beside the
# first, and whichever of them started Ollama would stop it under the other.
# The lock holds the pid of the run that took it. It counts only while that
# pid is still a run of this script: after a crash or a restart the number may
# belong to something else, and a lock nobody holds must not stop every run.
held_by_a_run() { [ -n "$1" ] && ps -p "$1" -o command= 2>/dev/null | grep -q 'daily_run\.sh'; }

if ! mkdir "$LOCK" 2>/dev/null; then
    OTHER="$(cat "$LOCK/pid" 2>/dev/null)"
    if [ -z "$OTHER" ]; then                 # a run that has only just taken the lock
        sleep 1
        OTHER="$(cat "$LOCK/pid" 2>/dev/null)"
    fi
    if held_by_a_run "$OTHER"; then
        say "=== $(now) not started: another run (process $OTHER) is still going"
        exit 0
    fi
    rm -f "$LOCK/pid"
    rmdir "$LOCK" 2>/dev/null
    if ! mkdir "$LOCK" 2>/dev/null; then
        say "=== $(now) not started: the lock at $LOCK could not be taken"
        exit 0
    fi
fi
echo $$ > "$LOCK/pid"
# Two runs that found the same abandoned lock in the same instant: only one keeps it.
[ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] || exit 0

PIPELINE_PID=""
OLLAMA_PID=""
stop() {  # end a process this script started, without waiting for ever for it
    kill "$1" 2>/dev/null || return 0
    for _ in $(seq 20); do
        kill -0 "$1" 2>/dev/null || return 0
        sleep 0.5
    done
    kill -9 "$1" 2>/dev/null
}
finish() {
    # Seen through to the end: a second signal (a logout sends more than one)
    # must not cut this off with something still running or the lock still held.
    trap '' TERM HUP INT
    # A pipeline left running would go on without its Ollama, and beside the next run.
    [ -z "$PIPELINE_PID" ] || stop "$PIPELINE_PID"
    [ -z "$OLLAMA_PID" ] || stop "$OLLAMA_PID"
    rm -f "$LOCK/pid"
    rmdir "$LOCK" 2>/dev/null
}
trap finish EXIT
trap 'say "=== $(now) stopped by a signal"; exit 143' TERM HUP INT

cut_to() { printf '%s' "$2" | cut -c1-"$1"; }

notify() {  # title, text
    [ "${DAILY_NOTIFY:-1}" = "1" ] || return 0
    osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv)' \
        -e 'end run' "$1" "$2" >/dev/null 2>&1 || true
}

# Everything goes to the day's log as it happens. What this run adds to it is
# read back at the end to say how it went.
touch "$LOG"
START="$(wc -c < "$LOG" | tr -d ' ')"
this_run() { tail -c +"$((START + 1))" "$LOG"; }

say "=== $(now) daily run starts"

if [ ! -x "$PYTHON" ]; then
    say "Cannot start: no Python at $PYTHON."
    notify "Daily pipeline did not run" "No Python at venv/bin/python. See $LOG"
    exit 1
fi

# Which providers the run will use, and where Ollama is expected, as the pipeline itself reads them.
SETTINGS="$("$PYTHON" -c 'import config; print(config.LLM_PROVIDER, config.LLM_PROVIDER_FAST, config.OLLAMA_BASE_URL)' 2>> "$LOG" | tail -n 1)"
read -r PROVIDER PROVIDER_FAST OLLAMA_URL <<< "$SETTINGS"
[ -n "$OLLAMA_URL" ] || say "Could not read the settings. The run below says why."

if [ "$PROVIDER" = "ollama" ] || [ "$PROVIDER_FAST" = "ollama" ]; then
    if ! curl -sf -m 5 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
        OLLAMA_BIN="$(PATH="$PATH:/usr/local/bin:/opt/homebrew/bin" command -v ollama)"
        case "$OLLAMA_URL" in
            http://localhost:11434|http://127.0.0.1:11434) LOCAL_DEFAULT=1 ;;
            *) LOCAL_DEFAULT=0 ;;
        esac
        if [ -n "$OLLAMA_BIN" ] && [ "$LOCAL_DEFAULT" = "1" ]; then
            say "Ollama is not running: starting it for this run."
            "$OLLAMA_BIN" serve > "$LOG_DIR/ollama.log" 2>&1 &
            OLLAMA_PID=$!
            READY=0
            for _ in $(seq 30); do
                if curl -sf -m 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
                    READY=1
                    break
                fi
                sleep 1
            done
            [ "$READY" = "1" ] || say "Ollama did not answer within 30 seconds."
        else
            say "Ollama does not answer at $OLLAMA_URL and cannot be started from here."
        fi
    fi
fi

# In the background, so that a signal to this script is acted on at once and
# the pipeline is stopped with it, not left running on its own.
"$PYTHON" -m pipeline.daily >> "$LOG" 2>&1 &
PIPELINE_PID=$!
caffeinate -i -w "$PIPELINE_PID" &           # the Mac does not go to sleep on its own while the run is going
wait "$PIPELINE_PID"
STATUS=$?
PIPELINE_PID=""
say "=== $(now) daily run ends, exit $STATUS"

# How each model configuration ended. The pipeline prints one line for each,
# "<provider>/<model>: <status>[. <message>]", and everything else it prints
# while working is indented, so nothing else in the output has this shape.
RESULTS="$(this_run | grep -E '^[^[:space:]/]+/[^[:space:]]+: (ok|degraded|failed|skipped|blocked)(\. .*)?$')"
ended() {  # the result lines of the configurations that ended with one of these statuses
    printf '%s\n' "$RESULTS" | grep -E "^[^[:space:]]+: ($1)(\. |\$)"
}
# Orders placed, over all configurations, from each one's summary line.
ORDERS="$(this_run | sed -nE 's/^  [0-9]+ views, .* ([0-9]+) order\(s\) placed$/\1/p' | awk '{n += $1} END {if (NR) print n}')"

if [ "$STATUS" -ne 0 ]; then
    REASON="$(ended 'failed|blocked' | head -n 1)"
    [ -n "$REASON" ] || REASON="$(this_run | grep -v '^===' | tail -n 1)"
    notify "Daily pipeline did not finish" "$(cut_to 220 "$REASON") The next scheduled run tries again."
elif [ -n "$(ended 'ok|degraded')" ]; then
    TITLE="Daily pipeline finished"
    [ -z "$(ended 'degraded')" ] || TITLE="Daily pipeline finished with problems"
    SUMMARY="$(ended 'ok|degraded' | sed -E 's/^([^[:space:]]+: [a-z]+).*$/\1/' | paste -sd ';' - | sed 's/;/; /g')"
    notify "$TITLE" "$(cut_to 160 "$SUMMARY")${ORDERS:+, $ORDERS order(s) placed}"
fi
# Skipped (the session already has signals, or there is no new session): nothing to say.

exit "$STATUS"
