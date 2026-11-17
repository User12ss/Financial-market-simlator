#!/bin/bash
# The schedule that runs scripts/daily_run.sh on this Mac, through launchd.
#
#   scripts/schedule.sh install     run every day at the times below
#   scripts/schedule.sh uninstall   stop the schedule; nothing else is touched
#   scripts/schedule.sh status      whether it is loaded, how the last run ended, the last log lines
#   scripts/schedule.sh run         start one run now, the way the schedule would
#
# The times are this Mac's local time and were chosen for US Eastern: several
# tries from the evening on, because closing prices can be published hours
# after the close, and one in the morning before the open. Trying again costs
# nothing: a session that already has signals is skipped. Set DAILY_RUN_TIMES
# ("HH:MM HH:MM ...") before `install` to use other times.
#
# launchd does not wake a sleeping Mac. A run that was due while it slept is
# made once when it wakes; if that is after the next session has opened, the
# pipeline refuses it and that session is missed. The schedule belongs to your
# login session: nothing runs while you are logged out.
#
# macOS does not let scheduled jobs read the Desktop, Documents or Downloads
# folders, so the project has to live somewhere else for this to work.

set -eu

LABEL="com.financial-market-sim.daily"
PROJECT="$(cd "$(dirname "$0")/.." && pwd -P)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
TIMES="${DAILY_RUN_TIMES:-18:00 20:00 22:00 23:45 07:00}"
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
        <string>$(xml "$PROJECT/scripts/daily_run.sh")</string>
    </array>
    <key>WorkingDirectory</key><string>$(xml "$PROJECT")</string>
    <key>StartCalendarInterval</key>
    <array>
$intervals    </array>
    <key>StandardOutPath</key><string>$(xml "$LOG_DIR/launchd.log")</string>
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
    echo "Installed. The daily pipeline runs every day at: ${shown% } (this Mac's local time, $(date +%Z))."
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
    echo "Removed. Nothing runs on a schedule now; records and logs are untouched."
}

status() {
    if ! loaded; then
        echo "Not installed."
        return 0
    fi
    echo "Installed: $PLIST"
    launchctl print "$DOMAIN/$LABEL" | grep -E $'^\t(state|runs|last exit code) =' | sed $'s/^\t/  /'
    echo "  times (local): $(sed -n 's|.*<key>Hour</key><integer>\([0-9]*\)</integer><key>Minute</key><integer>\([0-9]*\)</integer>.*|\1:\2|p' "$PLIST" \
        | awk -F: '{printf "%02d:%02d ", $1, $2}')"
    latest="$(ls -t "$LOG_DIR"/daily_*.log 2>/dev/null | head -n 1 || true)"
    if [ -n "$latest" ]; then
        echo "Last lines of $latest:"
        tail -n 8 "$latest" | cut -c1-200 | sed 's/^/  /'
    else
        echo "No run has been logged yet."
    fi
}

run() {
    loaded || { echo "Not installed: run 'scripts/schedule.sh install' first." >&2; exit 1; }
    if running; then
        echo "A run is already going."
    else
        launchctl kickstart "$DOMAIN/$LABEL"
        echo "Started."
    fi
    echo "Follow it with: tail -f \"$LOG_DIR/daily_$(date +%Y-%m-%d).log\""
}

case "${1:-}" in
    install) install ;;
    uninstall) uninstall ;;
    status) status ;;
    run) run ;;
    *) echo "Usage: scripts/schedule.sh install | uninstall | status | run" >&2; exit 2 ;;
esac
