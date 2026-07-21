#!/bin/zsh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCATIONS_DIR="$SCRIPT_DIR/locations"
PID_FILE="$LOCATIONS_DIR/.transcriber.pid"
LOG_FILE="$LOCATIONS_DIR/logs/transcriber.log"
ENV_FILE="$LOCATIONS_DIR/.env"

mkdir -p "$LOCATIONS_DIR/logs"

# Disable XetHub download protocol — incompatible with Python 3.9 threading
export HF_HUB_DISABLE_XET=1

start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Already running (PID $(cat "$PID_FILE"))"
        return 1
    fi

    [ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a

    cd "$SCRIPT_DIR"
    nohup "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/auto_transcribe.py" >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Started (PID $!, log: $LOG_FILE)"
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "Not running (no PID file)"
        return 1
    fi

    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        rm -f "$PID_FILE"
        echo "Stopped (PID $PID)"
    else
        rm -f "$PID_FILE"
        echo "Was not running (stale PID file removed)"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "Running (PID $(cat "$PID_FILE"))"
        echo "Log tail:"
        tail -5 "$LOG_FILE" 2>/dev/null
    else
        echo "Not running"
        [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
    fi
}

logs() {
    tail -f "$LOG_FILE"
}

catchup() {
    DAYS=${1:-7}
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/ondemand_transcribe.py" --catchup $DAYS
}

catchup_preview() {
    DAYS=${1:-7}
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/ondemand_transcribe.py" --catchup $DAYS --dry-run
}

reprocess() {
    DAYS=${1:-7}
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/ondemand_transcribe.py" --catchup $DAYS --reprocess-partial
}

fix_analysis() {
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/reclassify_and_fix.py" --generate-missing-analysis "$@"
}

fix_categories() {
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/reclassify_and_fix.py" --reclassify "$@"
}

fix_all() {
    cd "$SCRIPT_DIR"
    "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/reclassify_and_fix.py" --generate-missing-analysis --reclassify "$@"
}

case "${1:-start}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    logs)   logs ;;
    restart) stop; sleep 1; start ;;
    catchup) catchup "$2" ;;
    catchup-preview) catchup_preview "$2" ;;
    reprocess) reprocess "$2" ;;
    fix-analysis) fix_analysis "${@:2}" ;;
    fix-categories) fix_categories "${@:2}" ;;
    fix-all) fix_all "${@:2}" ;;
    *)
        echo "Usage: $0 {start|stop|status|logs|restart|catchup|catchup-preview|reprocess|fix-analysis|fix-categories|fix-all}"
        echo "Service Management:"
        echo "  start          - Launch auto-transcriber in background (default)"
        echo "  stop           - Stop the running transcriber"
        echo "  status         - Check if running + last 5 log lines"
        echo "  logs           - Tail the log file (Ctrl+C to exit)"
        echo "  restart        - Stop then start"
        echo ""
        echo "Catchup Operations:"
        echo "  catchup [days]         - Process last N days (default: 7)"
        echo "  catchup-preview [days] - Preview what would be processed (default: 7)"
        echo "  reprocess [days]       - Catchup + regenerate missing analysis (default: 7)"
        echo ""
        echo "Maintenance Operations:"
        echo "  fix-analysis           - Generate missing analysis files"
        echo "  fix-categories         - Reclassify and move files to correct folders"
        echo "  fix-all                - Do both (generate analysis + reclassify)"
        echo "  Add --dry-run to preview changes"
        exit 1
        ;;
esac
