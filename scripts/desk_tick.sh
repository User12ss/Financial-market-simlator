#!/bin/bash
# One round of the desk (paper trading: simulated orders only). Meant to be
# started every few minutes: scripts/schedule.sh install desk.
#
# A round does two things.
#
#   The watch. Plain code, a few seconds, no model. On a trading day, from
#   shortly before the first check-in until the close, it fetches headlines and
#   prices for the desk's book into the ledger. Outside those hours it does
#   nothing. While the watch is on, the Mac is kept from going to sleep on its
#   own until the next round is due (DESK_KEEP_AWAKE=0 turns that off; closing
#   the lid still puts it to sleep).
#
#   A meeting, when one is due. That part is handed to scripts/daily_run.sh,
#   which takes the same lock as the daily pipeline, starts Ollama when the
#   model needs it and stops it afterwards, writes the log and says how the
#   meeting ended. A meeting never starts beside a run of the daily pipeline:
#   it waits for the next round.
#
# The log is data_store/logs/desk_<date>.log. DAILY_LOG_DIR moves it, and
# everything the desk reads from the environment is passed through.

set -u
export LC_ALL=en_US.UTF-8
export PYTHONUNBUFFERED=1

PROJECT="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
cd "$PROJECT" || exit 1
PYTHON="$PROJECT/venv/bin/python"
LOG_DIR="${DAILY_LOG_DIR:-$PROJECT/data_store/logs}"
mkdir -p "$LOG_DIR" || exit 1
LOG="$LOG_DIR/desk_$(date +%Y-%m-%d).log"
now() { date '+%Y-%m-%d %H:%M:%S %Z'; }
say() { echo "$*" >> "$LOG"; }

if [ ! -x "$PYTHON" ]; then
    say "=== $(now) desk round not started: no Python at $PYTHON."
    exit 1
fi

MINUTES="${DESK_WATCH_MINUTES:-15}"
case "$MINUTES" in
    ''|*[!0-9]*) MINUTES=15 ;;
esac

# The watch prints one line saying what it did. Anything else it prints is an error, and goes to the log.
WATCH="$("$PYTHON" -m pipeline.desk watch 2>> "$LOG" | tail -n 1)"
case "$WATCH" in
    "Watch: outside its hours"*) ;;          # most rounds of a day: not worth a line each
    Watch:*)
        say "=== $(now) $WATCH"
        if [ "${DESK_KEEP_AWAKE:-1}" = "1" ]; then
            # Until a little after the next round, which renews it. It is not waited for.
            caffeinate -i -t "$(( (MINUTES + 5) * 60 ))" >/dev/null 2>&1 &
        fi ;;
    *) say "=== $(now) the watch did not finish: see the lines above" ;;
esac

DUE="$("$PYTHON" -m pipeline.desk due 2>> "$LOG" | tail -n 1)"
case "$DUE" in
    premarket|midday|afternoon|close) ;;
    *) exit 0 ;;
esac

export RUN_MODULE="pipeline.desk" RUN_ARGS="meet" RUN_NAME="Desk meeting" RUN_LABEL="desk $DUE meeting" RUN_LOG="desk"
exec /bin/bash "$PROJECT/scripts/daily_run.sh"
