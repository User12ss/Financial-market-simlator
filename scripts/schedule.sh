#!/bin/bash
# The schedules on this Mac, through launchd. There are two, installed and
# removed separately:
#
#   the daily pipeline   scripts/daily_run.sh, at set times of day
#   the desk             scripts/desk_tick.sh, every few minutes
#
#   scripts/schedule.sh install [desk]     load the schedule
#   scripts/schedule.sh uninstall [desk]   stop it; nothing else is touched
#   scripts/schedule.sh status [desk]      whether it is loaded, how the last run ended, the last log lines
#   scripts/schedule.sh run [desk]         start one run now, the way the schedule would
#
# Without "desk" a command is about the daily pipeline.
#
# The daily pipeline's times are this Mac's local time and were chosen for US
# Eastern: several tries from the evening on, because closing prices can be
# published hours after the close, and one in the morning before the open.
# Trying again costs nothing: a session that already has signals is skipped.
# Set DAILY_RUN_TIMES ("HH:MM HH:MM ...") before `install` to use other times.
#
# The desk runs a round every DESK_WATCH_MINUTES minutes (15 unless set before
# `install desk`), day and night. A round outside trading hours looks at the
# clock and ends; it works out New York time itself, so this Mac's time zone
# does not matter to it.
#
# launchd does not wake a sleeping Mac. A run that was due while it slept is
# made once when it wakes; if that is after the next session has opened, the
# pipeline refuses it and that session is missed. During trading hours the
# desk keeps the Mac from going to sleep on its own, but a closed lid still
# puts it to sleep. The schedules belong to your login session: nothing runs
# while you are logged out.
#
# macOS does not let scheduled jobs read the Desktop, Documents or Downloads
# folders, so the project has to live somewhere else for this to work.

set -eu

usage() { echo "Usage: scripts/schedule.sh install | uninstall | status | run [desk]" >&2; exit 2; }

case "${2:-daily}" in
    daily) JOB="daily"; LABEL="com.financial-market-sim.daily"; SCRIPT="daily_run.sh" ;;
    desk) JOB="desk"; LABEL="com.financial-market-sim.desk"; SCRIPT="desk_tick.sh" ;;
    *) usage ;;
esac
PROJECT="$(cd "$(dirname "$0")/.." && pwd -P)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
TIMES="${DAILY_RUN_TIMES:-18:00 20:00 22:00 23:45 07:00}"
MINUTES="${DESK_WATCH_MINUTES:-15}"
LOG_DIR="$PROJECT/data_store/logs"

xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

# The job's own lines in `launchctl print` are indented by one tab; deeper ones
# belong to its sockets and the like.
running() { launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -q $'^\tstate = running'; }

# Unloading the job stops whatever it is doing, so it is never done to a run in progress.
refuse_while_running() {
    if loaded && running; then
        echo "A run is going right now. Wait for it to end (scripts/schedule.sh status), then try again." >&2
        exit 1
    fi
}

unload() {
    loaded || return 0
    # launchd can report an error here and let go of the job all the same, and it
    # takes a moment either way: what counts is whether the job is still loaded.
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        loaded || return 0
        sleep 1
    done
    echo "launchd did not unload the schedule." >&2
    exit 1
}

install() {
    case "$PROJECT/" in
        "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
            echo "The project is in $PROJECT." >&2
            echo "macOS does not let scheduled jobs read Desktop, Documents or Downloads: move the project first." >&2
            exit 1 ;;
    esac
    if [ "$JOB" = "desk" ]; then
        if ! [[ "$MINUTES" =~ ^[0-9]{1,2}$ ]] || [ "$((10#$MINUTES))" -lt 1 ] || [ "$((10#$MINUTES))" -gt 60 ]; then
            echo "Not a number of minutes from 1 to 60: $MINUTES" >&2
            exit 1
        fi
        every=$((10#$MINUTES))
        # A round leaves a short-lived process behind that keeps the Mac awake until the
        # next one. Without AbandonProcessGroup launchd would end it with the round.
        when="    <key>StartInterval</key><integer>$((every * 60))</integer>
    <key>AbandonProcessGroup</key><true/>
"
    else
        intervals=""
        shown=""
        for time in $TIMES; do
            if ! [[ "$time" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]]; then
                echo "Not a time of day (HH:MM): $time" >&2
                exit 1
            fi
            hour=$((10#${BASH_REMATCH[1]})); minute=$((10#${BASH_REMATCH[2]}))
            intervals="$intervals        <dict><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>
"
            shown="$shown$(printf '%02d:%02d' "$hour" "$minute") "
        done
        [ -n "$intervals" ] || { echo "No times given." >&2; exit 1; }
        when="    <key>StartCalendarInterval</key>
    <array>
$intervals    </array>
"
    fi
    refuse_while_running

    mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
    unload
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$(xml "$PROJECT/scripts/$SCRIPT")</string>
    </array>
    <key>WorkingDirectory</key><string>$(xml "$PROJECT")</string>
$when    <key>StandardOutPath</key><string>$(xml "$LOG_DIR/launchd.log")</string>
    <key>StandardErrorPath</key><string>$(xml "$LOG_DIR/launchd.log")</string>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" >/dev/null
    for _ in 1 2 3; do
        if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then break; fi
        sleep 2
    done
    if ! loaded; then
        echo "launchd did not load the schedule. The file is at $PLIST; run install again." >&2
        exit 1
    fi
    if [ "$JOB" = "desk" ]; then
        echo "Installed. The desk runs a round every $every minute(s). It fetches and meets on trading days only."
        echo "Keep this Mac on, with the lid open, from before the open to after the close."
    else
        echo "Installed. The daily pipeline runs every day at: ${shown% } (this Mac's local time, $(date +%Z))."
    fi
    echo "Logs: $LOG_DIR"
}

uninstall() {
    if ! loaded && [ ! -e "$PLIST" ]; then
        echo "Not installed."
        return 0
    fi
    refuse_while_running
    unload
    rm -f "$PLIST"
    echo "Removed. Nothing runs on this schedule now; records and logs are untouched."
}

status() {
    if ! loaded; then
        echo "Not installed."
        return 0
    fi
    echo "Installed: $PLIST"
    launchctl print "$DOMAIN/$LABEL" | grep -E $'^\t(state|runs|last exit code) =' | sed $'s/^\t/  /'
    if [ "$JOB" = "desk" ]; then
        seconds="$(sed -n 's|.*<key>StartInterval</key><integer>\([0-9]*\)</integer>.*|\1|p' "$PLIST")"
        echo "  a round every $(( ${seconds:-0} / 60 )) minute(s)"
    else
        echo "  times (local): $(sed -n 's|.*<key>Hour</key><integer>\([0-9]*\)</integer><key>Minute</key><integer>\([0-9]*\)</integer>.*|\1:\2|p' "$PLIST" \
            | awk -F: '{printf "%02d:%02d ", $1, $2}')"
    fi
    latest="$(ls -t "$LOG_DIR"/"$JOB"_*.log 2>/dev/null | head -n 1 || true)"
    if [ -n "$latest" ]; then
        echo "Last lines of $latest:"
        tail -n 8 "$latest" | cut -c1-200 | sed 's/^/  /'
    else
        echo "No run has been logged yet."
    fi
}

run() {
    loaded || { echo "Not installed: run 'scripts/schedule.sh install${2:+ $2}' first." >&2; exit 1; }
    if running; then
        echo "A run is already going."
    else
        launchctl kickstart "$DOMAIN/$LABEL"
        echo "Started."
    fi
    echo "Follow it with: tail -f \"$LOG_DIR/${JOB}_$(date +%Y-%m-%d).log\""
}

case "${1:-}" in
    install) install ;;
    uninstall) uninstall ;;
    status) status ;;
    run) run "$@" ;;
    *) usage ;;
esac
