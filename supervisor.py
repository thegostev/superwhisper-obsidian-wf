"""System-domain absence supervisor for the transcriber daemon (LAG-906, spec SUP-*).

The 2026-10-30 → 11-03 outage (3.7 days) had one root cause: the transcriber
and its watchdog are per-user LaunchAgents (`RunAtLoad`). After a reboot with
nobody logged in, neither job runs — no scan, no watchdog, no alert — and
nothing in the system can observe that "the job was never loaded".

This script is the missing observer. It is installed as a LaunchDaemon
(`com.alex.transcriber.supervisor`) in the system domain, so it starts at boot
before any user logs in. Every tick it answers one question: is the daemon
loaded for the current console user, and is it alive? It observes only — it
never tries to run Superwhisper headless, because Superwhisper needs a GUI
session and the daemon needs the user's TCC grants.

States (SUP-2):

  NO_CONSOLE_USER  booted to the login window, or nobody logged in
  ABSENT           a console user exists but the daemon job is not loaded
  LOADED_STALE     the job is loaded but its heartbeat is older than --max-age
  HEALTHY          the job is loaded and its heartbeat is fresh

Two consecutive NO_CONSOLE_USER/ABSENT observations raise one notification and
set `alerting` in the status file (SUP-3/SUP-4); the alert is not repeated until
the episode clears (a HEALTHY or LOADED_STALE tick). The status file is the
machine-readable record the rest of the maintenance tooling reads (SUP-5).

Designed to run under /usr/bin/python3 (stdlib-only, like health_check.py), so
the supervisor's interpreter is the one Homebrew cannot break. Hence
`from __future__ import annotations`, no PyYAML, and absolute paths for every
spawned subprocess (with a timeout).

Exit codes: 0 observed and recorded (healthy or not), 2 argparse error,
3 internal error (probe or state I/O failure).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Defaults match the launchd plist template's arguments (SUP-6). The template
# passes them explicitly, so these exist for manual runs and for tests.
DEFAULT_LABEL = "com.alex.transcriber"
DEFAULT_HEARTBEAT_PATH = "~/.superwhisper_transcriber_heartbeat.json"
DEFAULT_STATE_PATH = "~/.superwhisper_transcriber_supervisor.json"
DEFAULT_MAX_AGE = 600.0  # 10 min: the staleness threshold in the post-mortem recommendation

SCHEMA_VERSION = 1
ABSENCE_ALERT_THRESHOLD = 2  # SUP-3: two consecutive checks, not one, so a reboot race is not an alert
SUBPROCESS_TIMEOUT = 30.0  # WD-7 discipline: every spawned subprocess gets a timeout

STATE_NO_CONSOLE_USER = "NO_CONSOLE_USER"
STATE_ABSENT = "ABSENT"
STATE_LOADED_STALE = "LOADED_STALE"
STATE_HEALTHY = "HEALTHY"
ABSENCE_STATES = frozenset({STATE_NO_CONSOLE_USER, STATE_ABSENT})

# WD-8 discipline: production always uses absolute paths. The env hooks exist so
# tests can shim scutil/launchctl without touching a real Mac's launchd.
SCUTIL_BIN = os.environ.get("SUPERVISOR_SCUTIL", "/usr/sbin/scutil")
LAUNCHCTL_BIN = os.environ.get("SUPERVISOR_LAUNCHCTL", "/bin/launchctl")

# `scutil <<< "show State:/Users/ConsoleUser"` prints a dictionary. At the login
# window the name is `loginwindow`; during first boot it is `_mbsetupuser`.
_NAME_RE = re.compile(r"^\s*Name\s*:\s*(\S+)\s*$", re.MULTILINE)
_UID_RE = re.compile(r"^\s*UID\s*:\s*(\d+)\s*$", re.MULTILINE)
NON_USER_NAMES = frozenset({"loginwindow", "_mbsetupuser", "root", "_windowserver"})


def parse_console_user(output: str) -> dict | None:
    """Parse `scutil show State:/Users/ConsoleUser` output (SUP-2).

    Returns `{"name", "uid"}` only for a real logged-in user. A login window,
    setup user, root, or malformed output means there is no console user for
    the purposes of a per-user LaunchAgent.
    """
    name_match = _NAME_RE.search(output)
    uid_match = _UID_RE.search(output)
    if not name_match or not uid_match:
        return None
    name = name_match.group(1)
    uid = int(uid_match.group(1))
    if name in NON_USER_NAMES or uid <= 0:
        return None
    return {"name": name, "uid": uid}


def read_console_user() -> tuple[dict | None, str | None]:
    """Read the console user from scutil. Returns `(user, error)`; never raises."""
    try:
        result = subprocess.run(
            [SCUTIL_BIN],
            input="show State:/Users/ConsoleUser\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)
    if result.returncode != 0:
        return None, f"scutil exited {result.returncode}: {result.stderr.strip()}"
    return parse_console_user(result.stdout), None


def probe_job_loaded(uid: int, label: str) -> bool:
    """Return True when `launchctl print gui/<uid>/<label>` finds the job (SUP-2).

    A non-zero exit is the normal "Could not find service" absence signal. An
    unspawnable launchctl is an internal error, surfaced as RuntimeError so the
    tick can exit 3 instead of misreporting the job as absent.
    """
    try:
        result = subprocess.run(
            [LAUNCHCTL_BIN, "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"launchctl print failed: {error}") from error
    return result.returncode == 0


def classify(
    console_user: dict | None,
    job_loaded: bool,
    heartbeat_age_seconds: float | None,
    max_age: float = DEFAULT_MAX_AGE,
) -> str:
    """Map one observation to one of the four states (SUP-2). Pure."""
    if console_user is None:
        return STATE_NO_CONSOLE_USER
    if not job_loaded:
        return STATE_ABSENT
    if heartbeat_age_seconds is None or heartbeat_age_seconds > max_age:
        return STATE_LOADED_STALE
    return STATE_HEALTHY


def heartbeat_age(path: str | Path, now: float | None = None) -> float | None:
    """Seconds since the heartbeat file was written, or None when it is missing.

    The heartbeat's mtime is the only honest liveness signal (HC-1/HC-4/HC-5).
    A future mtime — clock skew after sleep — is clamped to zero rather than
    reported as a negative age.
    """
    try:
        mtime = os.stat(os.path.expanduser(str(path))).st_mtime
    except OSError:
        return None
    current = time.time() if now is None else now
    return max(0.0, current - mtime)


def load_state(path: str | Path) -> dict:
    """Load the previous status, tolerating a missing or corrupt file (SUP-5)."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(path: str | Path, payload: dict) -> bool:
    """Write the status file atomically (temp file + os.replace). Never raises."""
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        return True
    except OSError as error:
        print(f"⚠️ supervisor: could not write status {target}: {error}", file=sys.stderr)
        with contextlib.suppress(OSError):
            temporary.unlink()
        return False


def decide_absence(
    previous: dict,
    state: str,
    now: float,
    threshold: int = ABSENCE_ALERT_THRESHOLD,
) -> dict:
    """Advance the alert counter for this observation (SUP-3/SUP-4). Pure.

    NO_CONSOLE_USER and ABSENT share one consecutive counter: a boot that
    starts at the login window and later observes a missing job is one episode.
    LOADED_STALE and HEALTHY end the episode — a loaded job is not absent, and
    staleness is the per-user watchdog's job, not the supervisor's.
    """
    is_absence = state in ABSENCE_STATES
    count = int(previous.get("consecutive_absence_checks") or 0) + 1 if is_absence else 0
    was_alerting = bool(previous.get("alerting"))
    notify_now = is_absence and count >= threshold and not was_alerting
    return {
        "notify": notify_now,
        "consecutive_absence_checks": count,
        "alerting": is_absence and (was_alerting or notify_now),
        "last_alert_at": now if notify_now else previous.get("last_alert_at"),
    }


def _iso(value: float | str | None) -> str | None:
    """Render a stored timestamp as RFC 3339 UTC (HB-9 convention)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_status(
    *,
    state: str,
    label: str,
    console_user: dict | None,
    job_loaded: bool | None,
    age: float | None,
    decision: dict,
    now: float,
) -> dict:
    """Build the status-file payload for this tick (SUP-5)."""
    return {
        "schema": SCHEMA_VERSION,
        "state": state,
        "label": label,
        "console_user": console_user["name"] if console_user else None,
        "console_uid": console_user["uid"] if console_user else None,
        "job_loaded": job_loaded,
        "heartbeat_age_seconds": None if age is None else round(age, 1),
        "consecutive_absence_checks": decision["consecutive_absence_checks"],
        "alerting": decision["alerting"],
        "last_alert_at": _iso(decision["last_alert_at"]),
        "observed_at": _iso(now),
    }


def alert_message(state: str, label: str, console_user: dict | None) -> str:
    """The operator-facing notification body for the two absence states (SUP-3)."""
    if state == STATE_NO_CONSOLE_USER:
        return (
            f"{label} cannot run: no console user is logged in (login window). "
            "Two consecutive supervisor checks saw no GUI session."
        )
    user = console_user["name"] if console_user else "unknown"
    return f"{label} is not loaded for {user}. No transcription is running."


def notify(title: str, message: str) -> bool:
    """Notification via osascript, text passed as argv — never interpolated (ES-2).

    Failure returns False, never raises: the supervisor's observation must
    survive a broken notification path.
    """
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
        print(f"⚠️ supervisor: notification failed: {error}", file=sys.stderr)
        return False


def run_tick(args: argparse.Namespace) -> int:
    """Observe once, decide, notify if warranted, and persist the status (SUP-1)."""
    console_user, error = read_console_user()
    if error:
        print(f"internal error: console user probe failed: {error}", file=sys.stderr)
        return 3

    job_loaded: bool | None = None
    if console_user is not None:
        try:
            job_loaded = probe_job_loaded(console_user["uid"], args.label)
        except RuntimeError as probe_error:
            print(f"internal error: {probe_error}", file=sys.stderr)
            return 3

    age = heartbeat_age(os.path.expanduser(args.heartbeat))
    state = classify(console_user, bool(job_loaded), age, args.max_age)
    now = time.time()
    state_path = os.path.expanduser(args.state)
    decision = decide_absence(load_state(state_path), state, now)
    status = build_status(
        state=state,
        label=args.label,
        console_user=console_user,
        job_loaded=job_loaded,
        age=age,
        decision=decision,
        now=now,
    )

    console = f"{console_user['name']}({console_user['uid']})" if console_user else "none"
    print(
        f"supervisor: state={state} console={console} job_loaded={job_loaded} "
        f"heartbeat_age={'n/a' if age is None else f'{age:.0f}s'} "
        f"alerting={str(decision['alerting']).lower()} observed_at={status['observed_at']}"
    )

    if args.dry_run:
        if decision["notify"]:
            print("dry-run: would send absence notification")
        print(f"dry-run: would write {state_path}")
        return 0

    if decision["notify"]:
        notify("Transcriber supervisor", alert_message(state, args.label, console_user))
    write_state(state_path, status)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Observe whether the transcriber LaunchAgent is loaded for the console user (LAG-906)."
    )
    parser.add_argument("--label", default=DEFAULT_LABEL, help="GUI-domain label to observe")
    parser.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT_PATH, help="heartbeat file path (SUP-2)")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="status file path (SUP-5)")
    parser.add_argument(
        "--max-age",
        type=float,
        default=DEFAULT_MAX_AGE,
        help="heartbeat staleness threshold in seconds (SUP-2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="observe and report only; never notify or write the status file",
    )
    args = parser.parse_args(argv)
    return run_tick(args)


if __name__ == "__main__":
    sys.exit(main())
