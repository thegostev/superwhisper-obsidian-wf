"""Heartbeat-based health assessment for the transcriber daemon (ADR 0009/0010, spec HC-*).

Read-only by default (HC-8): no restarts, no state writes, no notifications.
Designed to run under /usr/bin/python3 (3.9.6, stdlib-only) so the watchdog's
interpreter is the one Homebrew cannot break — hence `from __future__ import
annotations` and NO imports of config.py/pipeline.py (they need PyYAML).

The 2026-09-07 incident (transcriber dead 8h) is encoded here:
  - launchctl's PID and Status columns lie in both directions (live PID beside
    a stale nonzero exit; crash-loop respawn shows a live PID for a few ms), so
    the heartbeat file's mtime is the only honest liveness signal (HC-1/HC-4/HC-5).
  - the daemon log is written by the failure itself, so the watchdog never
    trusts it as a liveness source (HC-2/HC-3: no log/state-path parameters).

Exit codes: 0 healthy, 1 unhealthy, 2 argparse error, 3 internal error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time

# Duplicated from config.py on purpose: config.py imports PyYAML, which does not
# exist under /usr/bin/python3 (HC-11). The launchd watchdog plist passes the
# real paths explicitly at install time; these are the human-facing defaults.
HEARTBEAT_SCHEMA_VERSION = 1
DEFAULT_MAX_AGE = 300.0
DEFAULT_HEARTBEAT_PATH = "~/.superwhisper_transcriber_heartbeat.json"
DEFAULT_LABEL = "com.alex.transcriber"
DEFAULT_SELF_LABEL = "com.alex.transcriber.watchdog"

# WD-8: production always uses the absolute path. The env hook exists only so
# shell-level test harnesses can shim launchctl (an absolute path cannot be
# intercepted via PATH).
LAUNCHCTL_BIN = os.environ.get("HEALTH_CHECK_LAUNCHCTL", "/bin/launchctl")

# Watchdog constants (spec WD-*, PF-14, ES-3).
WD_STATE_PATH = "~/.superwhisper_transcriber_watchdog.json"
PAUSE_SENTINEL_PATH = "~/.superwhisper_transcriber_watchdog.pause"
REBUILD_MARKER_PATH = "~/.superwhisper_transcriber_rebuild.json"  # PF-14 (written by preflight.sh)
REPOINT_MARKER_PATH = "~/.superwhisper_transcriber_repoint.json"  # PF-15 (written by preflight.sh)
SCAN_CYCLE = 30.0  # daemon scan interval — the WD-4 grace allowance
RESTARTS_PER_EPISODE = 2  # WD-6: consecutive kickstarts before escalating
SLOW_RETRY_INTERVAL = 3600.0  # WD-6: at most one restart per hour while unhealthy
NOTIFY_COOLDOWN = 3600.0  # ES-3: one unhealthy notification per hour, shared with preflight
REBUILD_BUDGET = 1800.0  # PF-10/PF-14: a rebuild marker older than this is stale
SUBPROCESS_TIMEOUT = 30.0  # WD-7: every spawned subprocess gets a timeout


def _to_int(field: str) -> int | None:
    try:
        return int(field)
    except ValueError:
        return None


def parse_launchctl_list(output: str, label: str) -> dict | None:
    """Extract one service's row from `launchctl list` output (HC-6).

    Matches the label by exact field equality — substring matching would
    confuse com.alex.transcriber with com.alex.transcriber.watchdog, which is
    how run_transcriber.sh's status verb once misreported. Returns
    {"label", "pid", "last_exit_status"} (None for "-") or None if absent.
    """
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        pid_field, status_field, *label_fields = fields
        row_label = " ".join(label_fields)
        if row_label != label:
            continue
        return {
            "label": label,
            "pid": None if pid_field == "-" else _to_int(pid_field),
            "last_exit_status": None if status_field == "-" else _to_int(status_field),
        }
    return None


def _heartbeat_age(path: str) -> float:
    """Seconds since the heartbeat file's mtime (HC-1)."""
    return time.time() - os.stat(path).st_mtime


def read_heartbeat(path: str) -> tuple[dict | None, float | None, str | None]:
    """Read the heartbeat file. Returns (payload, age, reason).

    reason is None on success, otherwise "heartbeat_missing" or
    "heartbeat_unreadable" (unreadable JSON and non-object payloads both mean
    the writer never got to a valid write — treat identically, but keep the
    distinct code for diagnosis).
    """
    try:
        age = _heartbeat_age(path)
    except FileNotFoundError:
        return None, None, "heartbeat_missing"
    except OSError:
        return None, None, "heartbeat_unreadable"
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None, None, "heartbeat_unreadable"
    if not isinstance(payload, dict):
        return None, None, "heartbeat_unreadable"
    return payload, age, None


def run_launchctl_list() -> str:
    """Read-only `launchctl list` (HC-7: the only I/O the report path performs)."""
    result = subprocess.run(
        [LAUNCHCTL_BIN, "list"], capture_output=True, text=True, check=False, timeout=SUBPROCESS_TIMEOUT
    )
    return result.stdout


def assess_health(
    payload: dict | None,
    age: float | None,
    launchctl_status: dict | None,
    max_age: float,
) -> dict:
    """Pure decision function (HC-7). Verdict per HC-15/HC-13.

    Unhealthy iff: service not loaded, heartbeat missing/unreadable/unknown
    schema/stale, or a FRESH phase="fatal" heartbeat (PF-16 — escalate without
    restarting). A fresh heartbeat beside a missing PID is only a warning (HC-5
    crash-loop sampling race); a nonzero last_exit_status never decides (HC-4).
    """
    reason = None
    fatal_reason = None
    if launchctl_status is None:
        reason = "service_not_loaded"
    elif payload is None:
        reason = "heartbeat_missing"
    elif payload.get("schema") != HEARTBEAT_SCHEMA_VERSION:
        reason = "heartbeat_schema_unknown"
    elif age is not None and age > max_age:
        reason = "heartbeat_stale"
    elif payload.get("phase") == "fatal":
        reason = "heartbeat_fatal"
        fatal_reason = payload.get("fatal_reason")

    pid_missing_warning = launchctl_status is not None and launchctl_status.get("pid") is None and payload is not None
    return {
        "verdict": "healthy" if reason is None else "unhealthy",
        "reason": reason,
        "fatal_reason": fatal_reason,
        "pid_missing_warning": pid_missing_warning,
        "age": age,
        "heartbeat": payload,
        "launchctl": launchctl_status,
    }


def print_report(report: dict, *, dry_run: bool = False) -> None:
    """Human-readable one-screen summary."""
    line = f"verdict: {report['verdict']}"
    if report["reason"]:
        line += f" ({report['reason']})"
    print(line)
    if report["fatal_reason"]:
        print(f"fatal_reason: {report['fatal_reason']}")
    if report["pid_missing_warning"]:
        print("warning: launchctl shows no live PID — heartbeat is the authoritative signal")

    heartbeat = report.get("heartbeat")
    if heartbeat:
        print(
            f"heartbeat: phase={heartbeat.get('phase')} cycle={heartbeat.get('cycle')} age={report['age']:.0f}s"
            if report["age"] is not None
            else "heartbeat: (no age)"
        )
        print(
            f"  state_complete={heartbeat.get('state_complete')} "
            f"failed_permanent={heartbeat.get('failed_permanent')} "
            f"updated_at={heartbeat.get('updated_at')}"
        )
    launchctl_status = report.get("launchctl")
    if launchctl_status:
        print(
            f"launchctl: pid={launchctl_status.get('pid')} last_exit_status={launchctl_status.get('last_exit_status')}"
        )
    else:
        print("launchctl: service not loaded")
    if dry_run:
        print("dry-run: no actions taken")


def default_wd_state() -> dict:
    """Cross-invocation watchdog state (WD-3). `last_notify_at` is the ES-3
    cooldown shared with the preflight, which may update only that field."""
    return {
        "schema": 1,
        "last_run_at": None,
        "consecutive_failures": 0,
        "escalated": False,
        "escalated_at": None,
        "last_notify_at": None,
        "last_kickstart_at": None,
        "last_action": None,
        "last_action_at": None,
    }


def write_wd_state(path: str, state: dict) -> bool:
    """Atomic state write (WD-3). Returns False on failure; callers log, never abort."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as error:
        print(f"⚠️ watchdog: state write failed: {error}", file=sys.stderr)
        return False


def load_wd_state(path: str) -> dict:
    """Load persisted state; corrupt or missing → first-run defaults (WD-12/WD-5)."""
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return default_wd_state()
    if not isinstance(state, dict) or state.get("schema") != 1:
        return default_wd_state()
    merged = default_wd_state()
    merged.update(state)
    return merged


def decide_action(
    report: dict,
    wd_state: dict,
    now: float,
    *,
    max_age: float,
    paused: bool = False,
    rebuild_busy: bool = False,
) -> dict:
    """Pure decision ladder (WD-1..WD-12, PF-14, PF-16 reader half, ES-3/ES-4).

    Returns {"action", "restart", "notify", "state"} where "state" is the
    successor state — main() never computes state itself.

    Ladder: pause sentinel (WD-10) → first run (WD-5) → healthy (record /
    recovery notification ES-4) → rebuild-marker busy (PF-14) → sleep-gap
    observational grace (WD-4) → restart ladder (WD-6), with fatal heartbeats
    (PF-16) and service_not_loaded (WD-9) escalating without a restart.
    """
    state = dict(wd_state)
    first_run = wd_state.get("last_run_at") is None
    gap = None if first_run else now - wd_state["last_run_at"]
    state["last_run_at"] = now
    state["last_action_at"] = now

    def finish(action: str, *, restart: bool = False, notify: bool = False) -> dict:
        state["last_action"] = action
        return {"action": action, "restart": restart, "notify": notify, "state": state}

    if paused:
        return finish("pause")
    if first_run:
        return finish("first_run")

    verdict, reason, age = report.get("verdict"), report.get("reason"), report.get("age")
    if verdict == "healthy":
        if state.get("escalated"):
            # ES-4: recovery notification; the sender resets counters and episode.
            state["escalated"] = False
            state["escalated_at"] = None
            state["consecutive_failures"] = 0
            return finish("notify_recovery", notify=True)
        state["consecutive_failures"] = 0
        return finish("record")

    if rebuild_busy:
        # PF-14: kickstart -k during a rebuild signals the preflight itself —
        # observe, and do not count a failure.
        return finish("observe_busy")

    if (
        reason == "heartbeat_stale"
        and gap is not None
        and gap > max_age
        and age is not None
        and age <= gap + SCAN_CYCLE
    ):
        # WD-4: staleness fully explained by the gap — daemon was suspended
        # alongside the watchdog (StartInterval does not fire while asleep).
        # Counters reset; escalation state survives (only a healthy tick clears it).
        state["consecutive_failures"] = 0
        return finish("observational")

    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    failures = state["consecutive_failures"]
    escalate_only = reason in ("heartbeat_fatal", "service_not_loaded")
    if escalate_only:
        restart = False  # PF-16 / WD-9: restarting cannot help (or cannot work)
    elif failures <= RESTARTS_PER_EPISODE:
        restart = True
    else:
        restart = (now - (state.get("last_kickstart_at") or 0.0)) >= SLOW_RETRY_INTERVAL

    # ES-3: at most one unhealthy notification per hour. The first two kickstart
    # ticks stay silent — the escalation itself is the notification moment (WD-6).
    cooldown_ok = (not state.get("escalated")) or (now - (state.get("last_notify_at") or 0.0)) >= NOTIFY_COOLDOWN
    do_notify = cooldown_ok and not (restart and failures <= RESTARTS_PER_EPISODE and not escalate_only)
    if restart:
        state["last_kickstart_at"] = now
    if do_notify:
        state["escalated"] = True
        state["escalated_at"] = now
        state["last_notify_at"] = now  # ES-3 shared cooldown
    if restart:
        return finish("kickstart", restart=True, notify=do_notify)
    return finish("escalate" if do_notify else "wait", restart=False, notify=do_notify)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def load_rebuild_marker(path: str) -> bool:
    """PF-14 reader half: fresh marker → busy; stale marker → ignored and removed.

    Fresh means the holding PID is alive AND the marker is within the rebuild
    budget (PF-10). Corrupt/missing → not busy.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    pid, started = data.get("pid"), data.get("started_at")
    if not isinstance(pid, int) or not isinstance(started, (int, float)):
        return False
    if _pid_alive(pid) and (time.time() - started) <= REBUILD_BUDGET:
        return True
    with contextlib.suppress(OSError):
        os.remove(path)
    return False


def read_repoint_note(path: str) -> str:
    """PF-15/ES-8: preflight's interpreter-repoint record, surfaced in escalation text."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    note = data.get("message") if isinstance(data, dict) else None
    return f" {note}" if isinstance(note, str) and note else ""


def kickstart(label: str) -> bool:
    """Tier-1 repair: `launchctl kickstart gui/<uid>/<label>` (WD-7, WD-8)."""
    try:
        result = subprocess.run(
            ["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"⚠️ watchdog: kickstart failed: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"⚠️ watchdog: kickstart failed: {error}", file=sys.stderr)
        return False


def notify(title: str, message: str) -> bool:
    """ES-1/ES-2/ES-6: notification via osascript, text passed as argv — never
    interpolated into AppleScript source. Failure returns False, never raises."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e",
                "end run",
                message,
                title,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"⚠️ watchdog: notification failed: {error}", file=sys.stderr)
        return False


def run_watchdog_tick(args: argparse.Namespace) -> int:
    """--heal: assess, decide, act (kickstart/notify), persist state."""
    now = time.time()
    wd_state = load_wd_state(os.path.expanduser(args.state))
    paused = os.path.exists(os.path.expanduser(PAUSE_SENTINEL_PATH))

    try:
        status = parse_launchctl_list(run_launchctl_list(), args.label)
    except Exception as error:  # noqa: BLE001 — any launchctl failure is an internal error (exit 3)
        print(f"internal error: launchctl read failed: {error}", file=sys.stderr)
        return 3
    payload, age, read_reason = read_heartbeat(os.path.expanduser(args.heartbeat))
    report = assess_health(payload, age, status, args.max_age)
    if read_reason == "heartbeat_unreadable":
        report["reason"] = "heartbeat_unreadable"
        report["verdict"] = "unhealthy"

    rebuild_busy = load_rebuild_marker(os.path.expanduser(REBUILD_MARKER_PATH))
    decision = decide_action(report, wd_state, now, max_age=args.max_age, paused=paused, rebuild_busy=rebuild_busy)
    print(f"watchdog: {decision['action']}")
    print_report(report)

    if args.dry_run:
        print("dry-run: no actions taken")
        return 0 if report["verdict"] == "healthy" else 1

    if decision["action"] == "pause":
        # WD-10: the sentinel means manual maintenance — exit 0, no actions.
        return 0

    if decision["restart"]:
        # WD-2 is enforced in main(); kickstart failures are logged, never fatal (ES-6).
        kickstart(args.label)
    if decision["notify"]:
        if decision["action"] == "notify_recovery":
            message = "Transcriber health restored — heartbeat is fresh again."
        else:
            message = f"Transcriber unhealthy: {report['reason']}"
            if report.get("fatal_reason"):
                message += f" ({report['fatal_reason']})"
            message += read_repoint_note(os.path.expanduser(REPOINT_MARKER_PATH))  # ES-8
        try:
            notify("Transcriber watchdog", message)
        except Exception as error:  # noqa: BLE001 — ES-6: never abort on notification failure
            print(f"⚠️ watchdog: notification failed: {error}", file=sys.stderr)

    write_wd_state(os.path.expanduser(args.state), decision["state"])  # WD-3/WD-12
    return 0 if report["verdict"] == "healthy" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Heartbeat-based health assessment and self-healing watchdog for the transcriber daemon."
    )
    parser.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT_PATH, help="heartbeat file path")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="exact launchd service label to assess")
    parser.add_argument(
        "--max-age", type=float, default=DEFAULT_MAX_AGE, help="maximum heartbeat age in seconds (HC-14)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; never restart, write state, or notify (HC-9)"
    )
    parser.add_argument("--heal", action="store_true", help="watchdog mode: decide and act on the verdict")
    parser.add_argument("--state", default=WD_STATE_PATH, help="watchdog state file (WD-3)")
    parser.add_argument("--self-label", default=DEFAULT_SELF_LABEL, help="this watchdog's own label (WD-2)")
    parser.add_argument("--notify-test", action="store_true", help="send one test notification and exit (ES-9)")
    args = parser.parse_args(argv)

    if args.notify_test:
        notify("Transcriber watchdog", "Test notification — delivery path OK")
        print("test notification sent")
        return 0

    if args.heal:
        if args.label == args.self_label:
            # WD-2: the watchdog must never target its own label for restart.
            print(
                f"internal error: target label equals the watchdog's own label ({args.label})",
                file=sys.stderr,
            )
            return 3
        return run_watchdog_tick(args)

    try:
        output = run_launchctl_list()
    except Exception as error:  # noqa: BLE001 — any launchctl failure is an internal error (exit 3)
        print(f"internal error: launchctl read failed: {error}", file=sys.stderr)
        return 3

    status = parse_launchctl_list(output, args.label)
    payload, age, read_reason = read_heartbeat(os.path.expanduser(args.heartbeat))
    report = assess_health(payload, age, status, args.max_age)
    if read_reason == "heartbeat_unreadable":
        report["reason"] = "heartbeat_unreadable"
        report["verdict"] = "unhealthy"
    print_report(report, dry_run=args.dry_run)
    return 0 if report["verdict"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
