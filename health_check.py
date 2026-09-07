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
    """Read-only `launchctl list` (HC-7: the only I/O this module performs)."""
    result = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True, check=False)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only heartbeat-based health assessment of the transcriber daemon."
    )
    parser.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT_PATH, help="heartbeat file path")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="exact launchd service label")
    parser.add_argument(
        "--max-age", type=float, default=DEFAULT_MAX_AGE, help="maximum heartbeat age in seconds (HC-14)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; never restart, write state, or notify (HC-9)"
    )
    args = parser.parse_args(argv)

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
