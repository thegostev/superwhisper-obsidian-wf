#!/bin/bash
# Preflight wrapper for the launchd daemon (ADR 0010, spec PF-*).
#
# launchd runs this instead of the daemon directly. It validates the venv
# interpreter BY EXECUTING it (PF-2), rebuilds the venv from the first working
# >=3.12 interpreter when the probe fails (PF-4..PF-8, PF-14, PF-15), and
# exec's the daemon so launchd tracks the daemon's own PID (PF-1).
#
# Anti-storm rules: when no working interpreter can be produced it exits 0 so
# KeepAlive{SuccessfulExit:false} does not respawn it (PF-9); it honours a
# fresh phase="fatal" heartbeat by exiting 0 (PF-16); it defers during a
# Homebrew operation (PF-15); it rebuilds at most once per hour (PF-8) under
# an atomic lock (PF-10) with a rebuild-in-progress marker the watchdog reads
# as "busy" (PF-14).
#
# Verbs (PF-12): probe <interpreter> | candidate-probe <interpreter> |
# select | candidates — everything else is wrapper mode. --dry-run (PF-11)
# prints the decision without acting.
#
# Test hooks (env): PREFLIGHT_CANDIDATES (colon-separated candidate list),
# PREFLIGHT_NOTIFY=0 (suppress osascript, print instead), PREFLIGHT_PIP_CMD
# (override the pip install step), PREFLIGHT_HEARTBEAT (heartbeat path).
# All persistent state lives under $HOME, outside the repository.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
VENV_NEW="$SCRIPT_DIR/venv.new"

HOME_DIR="${HOME:?HOME must be set}"
LOCK_FILE="$HOME_DIR/.superwhisper_transcriber_rebuild.lock"
REBUILD_MARKER="$HOME_DIR/.superwhisper_transcriber_rebuild.json"
REPOINT_MARKER="$HOME_DIR/.superwhisper_transcriber_repoint.json"
REBUILD_COOLDOWN="$HOME_DIR/.superwhisper_transcriber_lastrebuild"
HEARTBEAT_FILE="${PREFLIGHT_HEARTBEAT:-$HOME_DIR/.superwhisper_transcriber_heartbeat.json}"

REBUILD_COOLDOWN_SECS=3600   # PF-8
REBUILD_BUDGET_SECS=1800     # PF-10/PF-14
FATAL_FRESH_SECS=300         # PF-16: a fatal heartbeat older than this is history

PF13_CANDIDATE_PROBE='import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
PF13_FULL_PROBE='import sys, yaml; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'

# PF-4a: candidate order is fixed. The system interpreter is last and rejected
# by the version gate, not special-cased. Override via PREFLIGHT_CANDIDATES
# (colon-separated) for tests.
DEFAULT_CANDIDATES="/opt/homebrew/bin/python3.13:/opt/homebrew/bin/python3.12:/opt/homebrew/opt/python@3.13/bin/python3.13:/opt/homebrew/opt/python@3.12/bin/python3.12:/opt/homebrew/opt/python@3.11/bin/python3.11:/usr/local/bin/python3.13:/usr/local/bin/python3.12:/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13:/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12:/usr/bin/python3"
CANDIDATES="${PREFLIGHT_CANDIDATES:-$DEFAULT_CANDIDATES}"

HELD_LOCK=0
# PF-10: release the lock on SIGTERM/EXIT — `kickstart -k` during a rebuild
# signals exactly this process (PF-14). Set before dispatch so every exit path
# is covered.
trap release_lock EXIT INT TERM

now_epoch() { date +%s; }

say() { printf '[preflight %s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

candidate_probe() {
    # PF-13: version gate only — MUST NOT import config.py (PF-3).
    "$1" -c "$PF13_CANDIDATE_PROBE" >/dev/null 2>&1
}

full_probe() {
    # PF-13: candidate gate + the declared runtime dependency (PyYAML).
    "$1" -c "$PF13_FULL_PROBE" >/dev/null 2>&1
}

venv_probe() { full_probe "$VENV_DIR/bin/python3"; }

escalate() {
    # PF-9/ES-1/ES-2/ES-5: notification via osascript argv handler (never
    # interpolated into AppleScript), logged to the inherited daemon log via
    # stderr. ES-3: shared cooldown — the preflight reads and updates ONLY the
    # watchdog state file's last_notify_at field.
    local title="$1" message="$2"
    say "ESCALATION: $message"
    if [ "${PREFLIGHT_NOTIFY:-1}" = "0" ]; then
        say "[notify suppressed]"
        return 0
    fi
    if /usr/bin/python3 - "$HOME_DIR/.superwhisper_transcriber_watchdog.json" <<'PY' 2>/dev/null; then
import json, os, sys, time
path = sys.argv[1]
now = time.time()
try:
    with open(path) as handle:
        state = json.load(handle)
    last = state.get("last_notify_at") or 0
    if not isinstance(last, (int, float)) or now - last < 3600:
        sys.exit(1)  # cooldown active — stay silent
    state["last_notify_at"] = now
except Exception:
    state = {"last_notify_at": now}  # no watchdog state yet: touch only our field
tmp = path + ".tmp"
try:
    with open(tmp, "w") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)
except OSError:
    pass
sys.exit(0)
PY
        /usr/bin/osascript -e 'on run argv' \
            -e 'display notification (item 1 of argv) with title (item 2 of argv)' \
            -e 'end run' "$message" "$title" >/dev/null 2>&1 || true
    fi
}

es8_repoint_note() {
    # ES-8: escalation text states venv rebuilds and interpreter repoints.
    if [ -f "$REPOINT_MARKER" ]; then
        sed -n 's/.*"message"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REPOINT_MARKER" | head -1
    fi
}

fatal_heartbeat_fresh() {
    # PF-16 reader half: fresh phase="fatal" heartbeat → unrecoverable by restart.
    /usr/bin/python3 - "$HEARTBEAT_FILE" "$FATAL_FRESH_SECS" <<'PY' 2>/dev/null
import json, os, sys, time
path, max_age = sys.argv[1], float(sys.argv[2])
try:
    age = time.time() - os.stat(path).st_mtime
    with open(path) as handle:
        payload = json.load(handle)
except (OSError, ValueError):
    sys.exit(1)
if isinstance(payload, dict) and payload.get("phase") == "fatal" and age <= max_age:
    sys.exit(0)
sys.exit(1)
PY
}

brew_operation_active() {
    # PF-15: a running brew process, or a Homebrew lock actively held (try-lock
    # probe — never inferred from lock-directory presence).
    if pgrep -x brew >/dev/null 2>&1; then
        return 0
    fi
    /usr/bin/python3 - "${HOMEBREW_PREFIX:-/opt/homebrew}/var/run/homebrew" <<'PY' 2>/dev/null
import fcntl, os, sys
lockdir = sys.argv[1]
if os.path.isdir(lockdir):
    for name in os.listdir(lockdir):
        path = os.path.join(lockdir, name)
        if not os.path.isfile(path):
            continue
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            os.close(fd)
            sys.exit(0)  # actively held
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
sys.exit(1)
PY
}

rebuild_in_cooldown() {
    # PF-8: at most one rebuild attempt per hour, tracked outside the repository.
    [ -f "$REBUILD_COOLDOWN" ] || return 1
    local last now
    last="$(cat "$REBUILD_COOLDOWN" 2>/dev/null)" || return 1
    [ -n "$last" ] || return 1
    now="$(now_epoch)"
    [ $(( now - last )) -lt $REBUILD_COOLDOWN_SECS ]
}

acquire_lock() {
    # PF-10: atomic lock; skip if actively held; break if stale (dead PID or
    # age beyond the rebuild budget). Released on EXIT/TERM (trap below).
    if [ -f "$LOCK_FILE" ]; then
        local lpid lstart
        read -r lpid lstart < "$LOCK_FILE" 2>/dev/null || true
        if [ -n "${lpid:-}" ] && kill -0 "$lpid" 2>/dev/null; then
            local now_age
            now_age="$(now_epoch)"
            if [ $(( now_age - ${lstart:-0} )) -le $REBUILD_BUDGET_SECS ]; then
                say "rebuild lock held by PID $lpid — skipping"
                return 1
            fi
        fi
        say "breaking stale rebuild lock (pid=${lpid:-?})"
        rm -f "$LOCK_FILE"
    fi
    printf '%s %s\n' "$$" "$(now_epoch)" > "$LOCK_FILE.tmp"
    mv "$LOCK_FILE.tmp" "$LOCK_FILE"
    HELD_LOCK=1
    return 0
}

release_lock() {
    if [ "$HELD_LOCK" = "1" ]; then
        rm -f "$LOCK_FILE"
        HELD_LOCK=0
    fi
}

write_rebuild_marker() {
    # PF-14: atomic rebuild-in-progress marker the watchdog reads as "busy".
    printf '{"pid": %s, "started_at": %s}\n' "$$" "$(now_epoch)" > "$REBUILD_MARKER.tmp"
    mv "$REBUILD_MARKER.tmp" "$REBUILD_MARKER"
}

clear_rebuild_marker() { rm -f "$REBUILD_MARKER"; }

write_repoint_marker() {
    # PF-15/ES-8: persistent repoint record the watchdog surfaces in escalations.
    printf '{"message": "venv rebuilt; python repointed to %s", "interpreter": "%s", "at": %s}\n' \
        "$1" "$1" "$(now_epoch)" > "$REPOINT_MARKER.tmp"
    mv "$REPOINT_MARKER.tmp" "$REPOINT_MARKER"
}

select_candidate() {
    # PF-4: first candidate whose own candidate probe passes (PF-13). Health is
    # judged by execution only — never file existence (PF-2).
    local candidate
    IFS=':' read -r -a candidate_list <<< "$CANDIDATES"
    for candidate in "${candidate_list[@]}"; do
        [ -n "$candidate" ] || continue
        if candidate_probe "$candidate"; then
            SELECTED_CANDIDATE="$candidate"
            return 0
        fi
    done
    return 1
}

do_rebuild() {
    # PF-6: build at a sibling path, validate with the full probe, then move.
    local candidate="$1"
    say "rebuilding venv with $candidate"
    rm -rf "$VENV_NEW"
    if ! "$candidate" -m venv "$VENV_NEW" >/dev/null 2>&1; then
        say "venv creation failed with $candidate"
        return 1
    fi
    # PF-5: pip installing the declared runtime dependency is the only
    # package-manager action permitted here.
    if ! "${PREFLIGHT_PIP_CMD:-$VENV_NEW/bin/python3 -m pip install --quiet PyYAML}" \
            >/dev/null 2>&1; then
        say "pip install failed"
        return 1
    fi
    if ! full_probe "$VENV_NEW/bin/python3"; then
        say "full probe failed against the rebuilt environment"
        return 1
    fi
    return 0
}

swap_in_venv() {
    # PF-7: rename the failed environment aside; keep only the most recent copy.
    local ts broken
    ts="$(date +%Y%m%d-%H%M%S)"
    broken="$SCRIPT_DIR/venv.broken.$ts"
    if [ -d "$VENV_DIR" ]; then
        mv "$VENV_DIR" "$broken"
        say "previous venv moved aside: $broken"
    fi
    mv "$VENV_NEW" "$VENV_DIR"
    # retain only the most recent broken copy
    ls -d "$SCRIPT_DIR"/venv.broken.* 2>/dev/null | sort -r | tail -n +2 | xargs rm -rf 2>/dev/null || true
}

rebuild_flow() {
    if rebuild_in_cooldown; then
        say "rebuild cooldown active (PF-8) — exiting 0 for a later tick"
        exit 0
    fi
    if ! acquire_lock; then
        exit 0  # another preflight is rebuilding; launchd will tick again
    fi
    if ! select_candidate; then
        # PF-9: exit 0 — the sole defence against an unbounded respawn loop.
        local note
        note="$(es8_repoint_note)"
        escalate "Transcriber preflight" \
            "No working Python >=3.12 found — venv rebuild failed.${note:+ $note}"
        release_lock
        exit 0
    fi
    printf '%s\n' "$(now_epoch)" > "$REBUILD_COOLDOWN.tmp" && mv "$REBUILD_COOLDOWN.tmp" "$REBUILD_COOLDOWN"
    write_rebuild_marker
    if do_rebuild "$SELECTED_CANDIDATE"; then
        swap_in_venv
        write_repoint_marker "$SELECTED_CANDIDATE"
        clear_rebuild_marker
        release_lock
        say "venv rebuilt; handing off to daemon"
        exec "$VENV_DIR/bin/python3" "$SCRIPT_DIR/auto_transcribe.py"
    fi
    clear_rebuild_marker
    release_lock
    rm -rf "$VENV_NEW"  # a failed build is discarded, not renamed aside (PF-7)
    # PF-9 even after a failed rebuild attempt: never let KeepAlive spin us.
    local note
    note="$(es8_repoint_note)"
    escalate "Transcriber preflight" \
        "venv rebuild with $SELECTED_CANDIDATE failed — exiting for a later tick.${note:+ $note}"
    exit 0
}

wrapper() {
    local dry_run=0
    [ "${1:-}" = "--dry-run" ] && dry_run=1

    # PF-16: a fresh fatal heartbeat means a deterministic post-exec fault —
    # restarting cannot help. Escalate and exit 0.
    if fatal_heartbeat_fresh; then
        if [ "$dry_run" = "1" ]; then
            say "dry-run: fresh fatal heartbeat — would escalate and exit 0"
            exit 0
        fi
        escalate "Transcriber preflight" \
            "Daemon exited on a fatal error — not restarting (PF-16).$(es8_repoint_note | sed 's/^/ /')"
        exit 0
    fi

    if venv_probe; then
        if [ "$dry_run" = "1" ]; then
            say "dry-run: venv probe passes — would exec the daemon"
            exit 0
        fi
        # PF-1: exec so launchd tracks the daemon PID and signals reach it.
        exec "$VENV_DIR/bin/python3" "$SCRIPT_DIR/auto_transcribe.py"
    fi
    say "venv interpreter failed the full probe"

    if [ "$dry_run" = "1" ]; then
        if select_candidate; then
            say "dry-run: would rebuild venv with $SELECTED_CANDIDATE"
        else
            say "dry-run: no candidate interpreter passes the probe — would escalate"
        fi
        exit 0
    fi

    if brew_operation_active; then
        # PF-15: defer — healing during a brew upgrade would permanently repoint
        # the daemon at a fallback interpreter. Does not consume the PF-8 cooldown.
        say "Homebrew operation active — deferring rebuild to a later tick"
        exit 0
    fi

    rebuild_flow
}

case "${1:-}" in
    probe)
        full_probe "${2:?usage: preflight.sh probe <interpreter>}" ;;
    candidate-probe)
        candidate_probe "${2:?usage: preflight.sh candidate-probe <interpreter>}" ;;
    select)
        if select_candidate; then
            printf '%s\n' "$SELECTED_CANDIDATE"
            exit 0
        fi
        exit 1 ;;
    candidates)
        IFS=':' read -r -a candidate_list <<< "$CANDIDATES"
        for candidate in "${candidate_list[@]}"; do
            [ -n "$candidate" ] || continue
            if candidate_probe "$candidate"; then
                printf 'PASS %s\n' "$candidate"
            else
                printf 'FAIL %s\n' "$candidate"
            fi
        done
        exit 0 ;;
    --dry-run)
        wrapper "$@" ;;
    *)
        wrapper "$@" ;;
esac