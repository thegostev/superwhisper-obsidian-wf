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

The 2026-09-16 incident (59h down via an unclosed redrive session) is encoded
here too: on service_not_loaded the ladder re-bootstraps the daemon from the
plist passed at install time (--plist, WD-9 revision, LAG-673) — without a
plist it stays escalate-only, because `kickstart` cannot undo a `bootout`.
A stale heartbeat beside a PID that is still running is the other shape the
plain verb cannot move, and escalates to `kickstart -k` (WD-17, LAG-753).

The 2026-10-08 incident (49h zombie Superwhisper instance, 5,998 consecutive
"verdict: healthy" observations at zero throughput; LAG-799/LAG-800, spec TP-*)
is encoded here too: two heartbeat-diff rules run on healthy ticks — a
completions plateau (state_complete unchanged too long) and a permanent-failure
stall (failed_permanent rising while completions are flat). They are
notification-only: the daemon itself is alive, so they never touch the restart
ladder and never count as launchd failures.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

# Duplicated from config.py on purpose: config.py imports PyYAML, which does not
# exist under /usr/bin/python3 (HC-11). The launchd watchdog plist passes the
# real paths explicitly at install time; these are the human-facing defaults.
HEARTBEAT_SCHEMA_VERSION = 1
DEFAULT_MAX_AGE = 300.0
DEFAULT_HEARTBEAT_PATH = "~/.superwhisper_transcriber_heartbeat.json"
DEFAULT_LABEL = "com.alex.transcriber"
DEFAULT_SELF_LABEL = "com.alex.transcriber.watchdog"

# HB-12/HC-17 (LAG-675): ops scripts share the daemon's heartbeat writer, so a
# manual run refreshes the file the watchdog reads. Heartbeats written before
# HB-12 carry no writer field; reading those as the daemon keeps the upgrade
# backwards compatible, because every post-HB-12 writer sets the field and
# defaults to "manual".
DAEMON_WRITER = "daemon"
# `launchctl print` reports a pid only while the job is actually running, unlike
# the sampled `launchctl list` PID column that lies in both directions (HC-4/HC-5).
_LAUNCHCTL_PRINT_PID = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)

# WD-8: production always uses the absolute path. The env hook exists only so
# shell-level test harnesses can shim launchctl (an absolute path cannot be
# intercepted via PATH).
LAUNCHCTL_BIN = os.environ.get("HEALTH_CHECK_LAUNCHCTL", "/bin/launchctl")

# Watchdog constants (spec WD-*, PF-14, ES-3).
WD_STATE_PATH = "~/.superwhisper_transcriber_watchdog.json"
PAUSE_SENTINEL_PATH = "~/.superwhisper_transcriber_watchdog.pause"
PAUSE_TTL = 4 * 3600.0  # WD-10 revision (LAG-674): an older sentinel is a forgotten one
REBUILD_MARKER_PATH = "~/.superwhisper_transcriber_rebuild.json"  # PF-14 (written by preflight.sh)
REPOINT_MARKER_PATH = "~/.superwhisper_transcriber_repoint.json"  # PF-15 (written by preflight.sh)
SCAN_CYCLE = 30.0  # daemon scan interval — the WD-4 grace allowance
RESTARTS_PER_EPISODE = 2  # WD-6: consecutive kickstarts before escalating
SLOW_RETRY_INTERVAL = 3600.0  # WD-6: at most one restart per hour while unhealthy
NOTIFY_COOLDOWN = 3600.0  # ES-3: one unhealthy notification per hour, shared with preflight
REBUILD_BUDGET = 1800.0  # PF-10/PF-14: a rebuild marker older than this is stale
SUBPROCESS_TIMEOUT = 30.0  # WD-7: every spawned subprocess gets a timeout
MAINTENANCE_END_TIMEOUT = 60.0  # MT-1 (LAG-680): max wait for a post-gate heartbeat
MAINTENANCE_END_POLL = 2.0
# Allowance for coarse filesystem mtime resolution when comparing a heartbeat's
# mtime against the gate start — far below the daemon's 30 s scan cycle.
MTIME_SLACK = 1.0

# WD-14 (LAG-684): the committed template is the deployment source of truth, and
# the installed plist drifted away from it — it stopped routing through
# preflight.sh, which is the whole of ADR 0010's self-heal story. The repo path
# is the directory this module lives in, which is also what __REPO__ expands to
# in the template.
DEFAULT_REPO_DIR = str(Path(__file__).resolve().parent)
DEFAULT_PLIST_TEMPLATE = str(
    Path(__file__).resolve().parent / "docs" / "launchd" / "com.alex.transcriber.plist.template"
)
PREFLIGHT_WRAPPER = "preflight.sh"

# Throughput rules (LAG-799/LAG-800, spec TP-*, post-mortem 26-10-08 rec 1a/1b).
# All thresholds are seconds of flat-time measured on the persisted tracker, and
# are explicit configuration inputs (--plateau-processing-age, --plateau-any-age,
# --fp-stall-flat-age) like --max-age (HC-14).
PLATEAU_ALERT = "throughput-plateau"  # TP-7: distinct alert label for downstream surfaces (LAG-676)
FP_STALL_ALERT = "permanent-failure-stall"  # TP-7: "dependency converting failures to permanent exclusion"
DEFAULT_PLATEAU_PROCESSING_AGE = 5.0 * 3600.0  # TP-4: PM left 4–6 h open; 5 h paged 26-10-08 on Oct 7 ~10:00
DEFAULT_PLATEAU_ANY_AGE = 12.0 * 3600.0  # TP-4: any phase — an idle weekend raises this via config
DEFAULT_FP_STALL_FLAT_AGE = 2.0 * 3600.0  # TP-2: healthy completion spacing is bounded by the 3600 s attempt deadline


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


def pause_sentinel_status(path: str | Path, now: float, ttl: float = PAUSE_TTL) -> str:
    """Classify the pause sentinel (WD-10 revision, LAG-674): "absent",
    "paused" (mtime within ttl) or "expired" (older than ttl).

    Pure apart from one stat — never deletes; removal is the acting tick's job
    so --dry-run can report an expiry without touching the file. The 26-09-16
    outage ran 59h because a sentinel left behind after maintenance had no TTL
    and silently disabled healing.
    """
    try:
        mtime = Path(path).expanduser().stat().st_mtime
    except FileNotFoundError:
        return "absent"
    except OSError:
        # Unstat-able but present: honour it as a pause, like the old bare
        # existence check did, rather than guessing its age.
        return "paused"
    return "expired" if now - mtime > ttl else "paused"


def expire_pause_sentinel(path: str | Path) -> bool:
    """Remove an expired sentinel. Returns True only when this call removed it,
    so the one-time notice cannot repeat every tick. Never raises (ES-6)."""
    try:
        Path(path).expanduser().unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        print(f"⚠️ watchdog: could not remove expired pause sentinel {path}: {error}", file=sys.stderr)
        return False
    return True


def retire_expired_pause() -> None:
    """Acting half of the WD-10 revision (LAG-674): remove the expired sentinel
    and send one notice, bound to the removal itself so it cannot repeat."""
    if not expire_pause_sentinel(PAUSE_SENTINEL_PATH):
        return
    try:
        notify(
            "Transcriber watchdog",
            f"Pause expired after {PAUSE_TTL / 3600:g}h — sentinel removed, healing resumed."
            " Check no maintenance is running.",
        )
    except Exception as error:  # noqa: BLE001 — ES-6: never abort on notification failure
        print(f"⚠️ watchdog: notification failed: {error}", file=sys.stderr)


def run_launchctl_list() -> str:
    """Read-only `launchctl list` (HC-7: the only I/O the report path performs)."""
    result = subprocess.run(
        [LAUNCHCTL_BIN, "list"], capture_output=True, text=True, check=False, timeout=SUBPROCESS_TIMEOUT
    )
    return result.stdout


def heartbeat_writer(payload: dict | None) -> str:
    """Which process wrote this heartbeat (HB-12). Pure.

    An absent, non-string or missing value reads as ``"daemon"``: only
    pre-HB-12 heartbeats lack the field, and every writer since sets it (and
    defaults to "manual"), so the permissive default cannot hide a manual run.
    """
    if not isinstance(payload, dict):
        return DAEMON_WRITER
    writer = payload.get("writer")
    return writer if isinstance(writer, str) else DAEMON_WRITER


def parse_launchctl_print_pid(output: str) -> int | None:
    """First ``pid = <n>`` line of `launchctl print` output, else None. Pure (HC-7).

    The key must be exactly ``pid`` — ``active count`` and ``last exit code``
    are numeric neighbours in the same block and must never be mistaken for it.
    A job that is loaded but not running prints no ``pid`` line at all, which is
    precisely the signal HC-17 needs.
    """
    match = _LAUNCHCTL_PRINT_PID.search(output)
    return int(match.group(1)) if match else None


def probe_daemon_pid(label: str) -> int | None:
    """`launchctl print gui/<uid>/<label>` → the running PID, or None (HC-17).

    Called only for a non-daemon heartbeat, so the healthy steady state costs no
    extra subprocess. A non-zero exit (unknown service) or any subprocess
    failure reads as no live PID — never raises (ES-6, WD-7, WD-8).
    """
    try:
        result = subprocess.run(
            [LAUNCHCTL_BIN, "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"⚠️ watchdog: launchctl print failed: {error}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    return parse_launchctl_print_pid(result.stdout)


def cross_check_daemon_pid(payload: dict | None, label: str) -> int | None:
    """HC-17 gate: probe `launchctl print` only when the daemon did not write
    this heartbeat, so the healthy steady state spends no extra subprocess."""
    if heartbeat_writer(payload) == DAEMON_WRITER:
        return None
    return probe_daemon_pid(label)


def daemon_frozen_alive(report: dict, label: str) -> bool:
    """WD-17: is this a stale heartbeat beside a daemon PID that is still there?

    The frozen-but-alive shape (SIGSTOP, App Nap before WD-15) that plain
    `kickstart` cannot move. The probe is HC-17's `launchctl print` PID, never
    the sampled `launchctl list` column (HC-4/HC-5), and it runs only on a
    stale heartbeat, so the healthy steady state still costs no extra
    subprocess. A failed probe reads as not-alive (ES-6).
    """
    if report.get("reason") != "heartbeat_stale":
        return False
    return probe_daemon_pid(label) is not None


def assess_health(
    payload: dict | None,
    age: float | None,
    launchctl_status: dict | None,
    max_age: float,
    daemon_pid: int | None = None,
) -> dict:
    """Pure decision function (HC-7). Verdict per HC-15/HC-13/HC-17.

    Unhealthy iff: service not loaded, heartbeat missing/unreadable/unknown
    schema/stale, a FRESH phase="fatal" heartbeat (PF-16 — escalate without
    restarting), or a fresh heartbeat that the daemon did not write with no live
    daemon PID to corroborate it (HC-17). A fresh heartbeat beside a missing
    `launchctl list` PID is only a warning (HC-5 crash-loop sampling race); a
    nonzero last_exit_status never decides (HC-4).

    Args:
        payload: Heartbeat contents, or None when it could not be read.
        age: Heartbeat file age in seconds (HC-1), or None.
        launchctl_status: Parsed `launchctl list` row, or None when the label is absent.
        max_age: Staleness threshold in seconds (HC-14).
        daemon_pid: Result of the `launchctl print` probe (HC-17): the running
            daemon PID, or None for no live PID. Consulted only for a heartbeat
            whose writer is not the daemon, so callers may leave it None for the
            ordinary daemon-written case. The sampled `launchctl list` PID never
            substitutes for it (HC-5).
    """
    reason = None
    fatal_reason = None
    writer = heartbeat_writer(payload)
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
    elif writer != DAEMON_WRITER and daemon_pid is None:
        # HC-17: an ops run refreshed the file the watchdog reads. Freshness
        # here proves the ops run, not the daemon — and no live PID backs it.
        reason = "heartbeat_not_daemon"

    pid_missing_warning = launchctl_status is not None and launchctl_status.get("pid") is None and payload is not None
    return {
        "verdict": "healthy" if reason is None else "unhealthy",
        "reason": reason,
        "fatal_reason": fatal_reason,
        "pid_missing_warning": pid_missing_warning,
        "manual_heartbeat_warning": reason is None and writer != DAEMON_WRITER,
        "writer": writer,
        "age": age,
        "heartbeat": payload,
        "launchctl": launchctl_status,
    }


def render_plist_template(text: str, *, repo: str, home: str) -> str:
    """Expand the template's __REPO__/__HOME__ placeholders (WD-14).

    The committed template is not a plist until these are filled in, so every
    comparison against an installed plist has to render first.
    """
    return text.replace("__REPO__", repo).replace("__HOME__", home)


def plist_fingerprint(text: str) -> str | None:
    """SHA-256 of a plist's *meaning*, or None when it does not parse (WD-14).

    Hashing the raw bytes would report drift for a reflow or a changed comment,
    and a warning that cries wolf gets ignored — which is how the installed
    plist drifted unnoticed in the first place. So the hash is taken over the
    parsed structure with keys sorted: same settings, same hash, whatever the
    formatting.
    """
    try:
        parsed = plistlib.loads(text.encode("utf-8"))
    except Exception:  # noqa: BLE001 — any malformed plist is simply not fingerprintable
        return None
    canonical = json.dumps(parsed, sort_keys=True, default=repr, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _routes_through_preflight(text: str) -> bool:
    """True when the plist's ProgramArguments run the preflight wrapper (PF-1/ADR 0010)."""
    try:
        parsed = plistlib.loads(text.encode("utf-8"))
        arguments = parsed.get("ProgramArguments") or []
        return any(PREFLIGHT_WRAPPER in str(argument) for argument in arguments)
    except Exception:  # noqa: BLE001 — fall back to the raw text for an unparsable plist
        return PREFLIGHT_WRAPPER in text


def check_plist_drift(
    installed_path: str | Path,
    template_path: str | Path,
    *,
    repo: str,
    home: str,
) -> dict:
    """Hash-compare the installed plist against the rendered template (WD-14).

    Read-only by construction — no writes, no subprocesses — so --dry-run needs
    no special case (HC-8/HC-9). Every failure mode is reported rather than
    raised: a drift check that can crash the watchdog is worse than no drift
    check at all (ES-6).

    Args:
        installed_path: The plist launchd actually loaded (~/Library/LaunchAgents/...).
        template_path: The committed template under docs/launchd/.
        repo: Expansion for the template's __REPO__ placeholder.
        home: Expansion for the template's __HOME__ placeholder.

    Returns:
        A dict with `status` (clean | drift | installed_missing | template_missing
        | unreadable), the two short hashes, `preflight_routed` (None when the
        installed plist is absent), and a human-readable `detail` for the
        non-comparable cases.
    """
    installed = Path(installed_path)
    template = Path(template_path)
    result: dict = {
        "status": "unreadable",
        "installed_hash": None,
        "template_hash": None,
        "preflight_routed": None,
        "detail": None,
    }

    try:
        installed_text = installed.read_text(encoding="utf-8")
    except OSError as error:
        result["status"] = "installed_missing"
        result["detail"] = f"{installed}: {error.strerror or error}"
        return result

    result["preflight_routed"] = _routes_through_preflight(installed_text)

    try:
        template_text = template.read_text(encoding="utf-8")
    except OSError as error:
        result["status"] = "template_missing"
        result["detail"] = f"{template}: {error.strerror or error}"
        return result

    installed_hash = plist_fingerprint(installed_text)
    template_hash = plist_fingerprint(render_plist_template(template_text, repo=repo, home=home))
    result["installed_hash"] = installed_hash
    result["template_hash"] = template_hash
    if installed_hash is None or template_hash is None:
        result["status"] = "unreadable"
        unreadable = installed if installed_hash is None else template
        result["detail"] = f"{unreadable}: not a parsable plist"
        return result

    result["status"] = "clean" if installed_hash == template_hash else "drift"
    return result


def print_plist_drift(drift: dict | None) -> None:
    """Warning-only drift reporting (WD-14).

    A drifted plist says nothing about whether the daemon is alive, so it never
    touches the verdict — it would otherwise make the watchdog restart a
    perfectly healthy daemon over a deployment mismatch.
    """
    if not drift or drift["status"] == "clean":
        return
    status = drift["status"]
    if status == "drift":
        print(
            "warning: installed plist has drifted from the committed template (WD-14) —"
            f" installed {str(drift['installed_hash'])[:12]} != template {str(drift['template_hash'])[:12]}"
        )
    else:
        print(f"warning: plist drift check inconclusive ({status}): {drift.get('detail')}")
    if drift.get("preflight_routed") is False:
        print(f"  installed plist does not run {PREFLIGHT_WRAPPER} — ADR 0010 self-heal is not in effect")


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
    if report.get("manual_heartbeat_warning"):
        print(
            "warning: heartbeat was not written by the daemon (an ops run refreshed it) —"
            " liveness confirmed by the launchctl print PID instead (HC-17)"
        )

    heartbeat = report.get("heartbeat")
    if heartbeat:
        print(
            f"heartbeat: phase={heartbeat.get('phase')} writer={heartbeat_writer(heartbeat)}"
            f" cycle={heartbeat.get('cycle')} age={report['age']:.0f}s"
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
    print_plist_drift(report.get("plist_drift"))  # WD-14: warning only, never the verdict
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
        "throughput": None,  # TP-1: heartbeat-diff tracker {state_complete, failed_permanent, phase, since_at}
        "last_throughput_notify_at": None,  # TP-4: throughput cooldown, independent of ES-3
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


def _counter_or_none(value: int | None) -> int | None:
    """A heartbeat counter: a real int (bool excluded) or nothing."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def throughput_tracker_shift(tracker: dict | None, gap: float | None) -> dict | None:
    """TP-5: an unobservable period must not count toward the plateau clock.

    Called on every tick that does not observe a fresh healthy heartbeat —
    outage, suspension, pause sentinel. Shifting `since_at` (not just
    suspending) is what stops a recovery tick from attributing the outage to a
    stalled dependency. Returns a fresh dict; the input is never mutated.
    """
    if tracker is None or not gap or gap <= 0:
        return tracker
    shifted = dict(tracker)
    shifted["since_at"] = float(tracker["since_at"]) + gap
    return shifted


def throughput_diff(
    tracker: dict | None,
    payload: dict | None,
    now: float,
    *,
    plateau_processing_age: float = DEFAULT_PLATEAU_PROCESSING_AGE,
    plateau_any_age: float = DEFAULT_PLATEAU_ANY_AGE,
    fp_stall_flat_age: float = DEFAULT_FP_STALL_FLAT_AGE,
) -> tuple[dict | None, str | None]:
    """Pure heartbeat-diff on consecutive healthy ticks (LAG-799/LAG-800, TP-6).

    Returns (successor tracker, alert label or None). `since_at` is when the
    currently tracked `state_complete` value was first observed (re-baselined on
    change or gap shift). The alert order matters: a failed_permanent rise names
    the precise failure mode (LAG-800) and wins over the bare plateau label;
    both are only evaluated while completions are flat (TP-3).
    """
    if not isinstance(payload, dict):
        return tracker, None
    completions = _counter_or_none(payload.get("state_complete"))
    permanent = _counter_or_none(payload.get("failed_permanent"))
    phase = payload.get("phase")
    if completions is None or permanent is None:
        return tracker, None  # TP-8: an unusable sample never alerts
    if tracker is None:
        return {"state_complete": completions, "failed_permanent": permanent, "phase": phase, "since_at": now}, None
    tracked = dict(tracker)
    since = tracked.get("since_at")
    base = _counter_or_none(tracked.get("state_complete"))
    base_permanent = _counter_or_none(tracked.get("failed_permanent"))
    if base is None or base_permanent is None or not isinstance(since, (int, float)):
        # TP-8: a malformed tracker is re-baselined from this sample, silently.
        return {"state_complete": completions, "failed_permanent": permanent, "phase": phase, "since_at": now}, None
    flat_for = now - since
    permanent_delta = permanent > base_permanent
    if completions != base:
        # TP-3: completions advanced (or regressed) — fresh baseline; a
        # failed_permanent moving alongside is the dependency producing AND
        # excluding at once, which is sane severity, not an alarm.
        return {"state_complete": completions, "failed_permanent": permanent, "phase": phase, "since_at": now}, None
    alert = None
    if permanent_delta and flat_for > fp_stall_flat_age:
        # LAG-800 first, while flat: names "dependency converting failures to
        # permanent exclusion" rather than the generic plateau.
        alert = FP_STALL_ALERT
    elif flat_for > plateau_any_age or (phase == "processing" and flat_for > plateau_processing_age):
        # LAG-799: the 26-10-08 signature — state_complete pinned while phase=processing.
        alert = PLATEAU_ALERT
    tracked["failed_permanent"] = permanent
    tracked["phase"] = phase
    return tracked, alert


def _healthy_tick(
    state: dict,
    report: dict,
    now: float,
    *,
    plateau_processing_age: float,
    plateau_any_age: float,
    fp_stall_flat_age: float,
) -> tuple[str, str | None]:
    """Healthy-tick bookkeeping (TP-1..TP-5): diff the heartbeat counters, reset
    the health counters, and return which action the tick takes —

    ("notify_recovery", None) once an escalated episode ends (ES-4: the recovery
    notification wins over any throughput alert); ("notify_throughput", label)
    when a throughput rule pages (TP-4: one page per hour on its own cooldown,
    independent of ES-3); or ("record", None).
    """
    tracked, alert = throughput_diff(
        state.get("throughput"),
        report.get("heartbeat"),
        now,
        plateau_processing_age=plateau_processing_age,
        plateau_any_age=plateau_any_age,
        fp_stall_flat_age=fp_stall_flat_age,
    )
    state["throughput"] = tracked
    if state.get("escalated"):
        # ES-4: recovery notification; the sender resets counters and episode.
        state["escalated"] = False
        state["escalated_at"] = None
        state["consecutive_failures"] = 0
        return "notify_recovery", None
    state["consecutive_failures"] = 0
    if alert and (now - (state.get("last_throughput_notify_at") or 0.0)) >= NOTIFY_COOLDOWN:
        state["last_throughput_notify_at"] = now
        return "notify_throughput", alert
    return "record", None


def _failure_ladder(
    state: dict,
    now: float,
    *,
    failures: int,
    bootstrap_repair: bool,
    frozen_alive: bool,
    escalate_only: bool,
) -> tuple[str, bool, bool]:
    """Unhealthy ladder (WD-6, WD-9, WD-17, PF-16, ES-3): choose the repair,
    cap it, and pick the escalation moment. Returns (action, restart, do_notify)
    and records the kickstart/escalation timestamps on `state`."""
    if escalate_only:
        restart = False  # PF-16 / WD-9: restarting cannot help (or cannot work)
    elif failures <= RESTARTS_PER_EPISODE:
        restart = True
    else:
        restart = (now - (state.get("last_kickstart_at") or 0.0)) >= SLOW_RETRY_INTERVAL

    # ES-3: at most one unhealthy notification per hour. The first two kickstart
    # ticks stay silent — the escalation itself is the notification moment (WD-6).
    # Bootstrap repair is exempt: an unbootstrapped service is visible harm in
    # progress, so the first tick notifies too (WD-9 revision, LAG-673).
    cooldown_ok = (not state.get("escalated")) or (now - (state.get("last_notify_at") or 0.0)) >= NOTIFY_COOLDOWN
    do_notify = cooldown_ok and (
        bootstrap_repair or not (restart and failures <= RESTARTS_PER_EPISODE and not escalate_only)
    )
    if restart:
        state["last_kickstart_at"] = now
    if do_notify:
        state["escalated"] = True
        state["escalated_at"] = now
        state["last_notify_at"] = now  # ES-3 shared cooldown
    if restart:
        if bootstrap_repair:
            action = "bootstrap"
        elif frozen_alive:
            action = "kickstart_kill"
        else:
            action = "kickstart"
    else:
        action = "escalate" if do_notify else "wait"
    return action, restart, do_notify


def decide_action(
    report: dict,
    wd_state: dict,
    now: float,
    *,
    max_age: float,
    paused: bool = False,
    rebuild_busy: bool = False,
    bootstrap_available: bool = False,
    daemon_alive: bool = False,
    plateau_processing_age: float = DEFAULT_PLATEAU_PROCESSING_AGE,
    plateau_any_age: float = DEFAULT_PLATEAU_ANY_AGE,
    fp_stall_flat_age: float = DEFAULT_FP_STALL_FLAT_AGE,
) -> dict:
    """Pure decision ladder (WD-1..WD-12, PF-14, PF-16 reader half, ES-3/ES-4, TP-1..TP-8).

    Returns {"action", "restart", "notify", "alert", "state"} where "state" is
    the successor state — main() never computes state itself. "alert" carries
    the distinct throughput alert label (TP-7) when one fired, otherwise None.

    Ladder: pause sentinel (WD-10) → first run (WD-5) → healthy (record /
    recovery notification ES-4 / throughput alert TP-1) → rebuild-marker busy
    (PF-14) → sleep-gap observational grace (WD-4) → restart ladder (WD-6).
    Fatal heartbeats (PF-16) and service_not_loaded (WD-9) escalate without a
    restart — except that a service_not_loaded with a plist passed at install
    time (WD-9 revision, LAG-673) is repairable: the ladder re-bootstraps the
    service, capped like kickstarts, and notifies on the first tick (a bootout
    is deliberate human action, not a crash loop — the first-tick silence must
    not apply).

    `daemon_alive` is the caller's WD-17 liveness answer (LAG-753). A stale
    heartbeat beside a live PID is frozen-but-alive, and its restart becomes
    `kickstart_kill`; the ladder order, the WD-6 capping and every other
    reason are unchanged.
    """
    state = dict(wd_state)
    first_run = wd_state.get("last_run_at") is None
    gap = None if first_run else now - wd_state["last_run_at"]
    state["last_run_at"] = now
    state["last_action_at"] = now

    def finish(action: str, *, restart: bool = False, notify: bool = False, alert: str | None = None) -> dict:
        state["last_action"] = action
        return {"action": action, "restart": restart, "notify": notify, "alert": alert, "state": state}

    if paused:
        # TP-5: maintenance does not produce observations — shift, don't count.
        state["throughput"] = throughput_tracker_shift(state.get("throughput"), gap)
        return finish("pause")
    if first_run:
        # TP-1/TP-8: take a baseline sample; the plateau clock starts here.
        state["throughput"], _ = throughput_diff(None, report.get("heartbeat"), now)
        return finish("first_run")

    verdict, reason, age = report.get("verdict"), report.get("reason"), report.get("age")
    if verdict == "healthy":
        # TP-1: sample the heartbeat-diff first. A throughput alert rides on an
        # otherwise-healthy tick — no restart, no failure counter, verdict stays
        # healthy: kickstarting a live daemon mid-wait could interrupt a
        # legitimate wait (the 26-10-08 zombie kept every such signal green).
        kind, alert = _healthy_tick(
            state,
            report,
            now,
            plateau_processing_age=plateau_processing_age,
            plateau_any_age=plateau_any_age,
            fp_stall_flat_age=fp_stall_flat_age,
        )
        if kind == "notify_recovery":
            return finish("notify_recovery", notify=True)
        if kind == "notify_throughput":
            return finish("notify_throughput", notify=True, alert=alert)
        return finish("record")

    # TP-5: every tick that does not observe a fresh healthy heartbeat is an
    # unobservable period for throughput — neither an outage nor a lid-close
    # suspension counts as plateau time.
    state["throughput"] = throughput_tracker_shift(state.get("throughput"), gap)

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
    bootstrap_repair = reason == "service_not_loaded" and bootstrap_available
    # WD-17: only the stale-heartbeat reason can be frozen-alive; a fatal or
    # absent job is a different failure with a different repair.
    frozen_alive = reason == "heartbeat_stale" and daemon_alive
    escalate_only = reason == "heartbeat_fatal" or (reason == "service_not_loaded" and not bootstrap_repair)
    action, restart, do_notify = _failure_ladder(
        state,
        now,
        failures=state["consecutive_failures"],
        bootstrap_repair=bootstrap_repair,
        frozen_alive=frozen_alive,
        escalate_only=escalate_only,
    )
    return finish(action, restart=restart, notify=do_notify)


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


def kickstart(label: str, *, kill: bool = False) -> bool:
    """Tier-1 repair: `launchctl kickstart gui/<uid>/<label>` (WD-7, WD-8).

    With ``kill`` the verb becomes `kickstart -k`, which terminates the job
    before restarting it (WD-17). Plain `kickstart` is a no-op against a PID
    that is alive but frozen, so the ladder needs `-k` for that one case; it
    stays off by default, because `-k` on a healthy-but-slow daemon would cut a
    transcription short.
    """
    argv = [LAUNCHCTL_BIN, "kickstart"]
    if kill:
        argv.append("-k")
    argv.append(f"gui/{os.getuid()}/{label}")
    try:
        result = subprocess.run(
            argv,
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


def bootstrap_repair(label: str, plist_path: str) -> bool:
    """Tier-0 repair for service_not_loaded (WD-9 revision, LAG-673):
    `launchctl bootstrap gui/<uid>/<label> <plist>` — the only repair for a
    `launchctl bootout`, which removes the job from the domain entirely
    (`kickstart` cannot undo it; the 26-09-16 outage ran 63h on exactly that).

    The plist path is passed at install time (--plist); the watchdog never
    guesses it. Failure returns False, never raises (ES-6).
    """
    try:
        result = subprocess.run(
            [LAUNCHCTL_BIN, "bootstrap", f"gui/{os.getuid()}/{label}", plist_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"⚠️ watchdog: bootstrap failed: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"⚠️ watchdog: bootstrap failed: {error}", file=sys.stderr)
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


def attach_plist_drift(report: dict, args: argparse.Namespace) -> None:
    """Add the WD-14 drift result to a report, when the installed plist path is known.

    --plist is the path launchd was bootstrapped from (WD-9), so it is also the
    only path worth comparing. Without it there is nothing to compare and the
    report simply carries no `plist_drift` key.
    """
    if not getattr(args, "plist", None):
        return
    report["plist_drift"] = check_plist_drift(
        os.path.expanduser(args.plist),
        os.path.expanduser(getattr(args, "plist_template", DEFAULT_PLIST_TEMPLATE)),
        repo=getattr(args, "repo", DEFAULT_REPO_DIR),
        home=getattr(args, "home", os.path.expanduser("~")),
    )


def perform_repair(action: str, label: str, plist_path: str | None) -> None:
    """Run the repair the ladder chose. WD-2 is enforced in main(); every repair
    failure is logged inside its wrapper and never fatal (ES-6).

    Three verbs, one per failure shape: `bootstrap` for a job that is gone
    (WD-9), `kickstart -k` for one that is alive but frozen (WD-17), and the
    plain `kickstart` for the ordinary dead-but-loaded case (WD-6).
    """
    if action == "bootstrap":
        if plist_path:  # the bootstrap action implies the plist was found
            bootstrap_repair(label, plist_path)
    elif action == "kickstart_kill":
        kickstart(label, kill=True)
    else:
        kickstart(label)


def _throughput_message(decision: dict, now: float) -> str:
    """TP-7: compose the throughput alert text. The message names the failure
    mode and carries the numbers the operator needs to act on it."""
    tracker = (decision.get("state") or {}).get("throughput") or {}
    hours = (now - tracker.get("since_at", now)) / 3600.0
    flat = tracker.get("state_complete")
    phase = tracker.get("phase") or "unknown-phase"
    if decision.get("alert") == FP_STALL_ALERT:
        return (
            f"Transcriber dependency fault: failed_permanent is now at {tracker.get('failed_permanent')} while "
            f"state_complete was flat at {flat} for {hours:.1f}h (phase={phase}) — the dependency is converting "
            "failures into permanent exclusion. Check Superwhisper."
        )
    return (
        f"Transcriber throughput plateau: state_complete flat at {flat} for {hours:.1f}h (phase={phase}) — no note "
        "produced. Check Superwhisper: a wedged instance keeps the daemon looking busy."
    )


def _escalation_message(decision: dict, report: dict, now: float) -> str:
    """Compose the notification text (ES-8, TP-7): state the reason, name the
    failure mode, and record any interpreter repoint."""
    if decision["action"] == "notify_recovery":
        return "Transcriber health restored — heartbeat is fresh again."
    if decision.get("alert") in (PLATEAU_ALERT, FP_STALL_ALERT):
        # The 26-10-08 page would have said exactly what to try (relaunch the app).
        return _throughput_message(decision, now)
    message = f"Transcriber unhealthy: {report['reason']}"
    if report.get("fatal_reason"):
        message += f" ({report['fatal_reason']})"
    return message + read_repoint_note(os.path.expanduser(REPOINT_MARKER_PATH))


def run_watchdog_tick(args: argparse.Namespace) -> int:
    """--heal: assess, decide, act (kickstart/notify), persist state."""
    now = time.time()
    wd_state = load_wd_state(os.path.expanduser(args.state))
    pause_status = pause_sentinel_status(PAUSE_SENTINEL_PATH, now)
    paused = pause_status == "paused"
    if pause_status == "expired":
        print(f"watchdog: pause sentinel expired (older than {PAUSE_TTL / 3600:g}h) — healing resumes")

    try:
        status = parse_launchctl_list(run_launchctl_list(), args.label)
    except Exception as error:  # noqa: BLE001 — any launchctl failure is an internal error (exit 3)
        print(f"internal error: launchctl read failed: {error}", file=sys.stderr)
        return 3
    payload, age, read_reason = read_heartbeat(os.path.expanduser(args.heartbeat))
    # HC-17: a heartbeat an ops run refreshed needs a live daemon PID to count.
    report = assess_health(payload, age, status, args.max_age, daemon_pid=cross_check_daemon_pid(payload, args.label))
    if read_reason == "heartbeat_unreadable":
        report["reason"] = "heartbeat_unreadable"
        report["verdict"] = "unhealthy"

    rebuild_busy = load_rebuild_marker(os.path.expanduser(REBUILD_MARKER_PATH))
    # WD-9 revision (LAG-673): the daemon plist path, passed at install time,
    # unlocks bootstrap repair. A declared-but-missing plist degrades to the
    # old escalate-only behaviour — visibly, never silently.
    plist_path = os.path.expanduser(args.plist) if getattr(args, "plist", None) else None
    bootstrap_available = bool(plist_path and os.path.exists(plist_path))
    if args.plist and not bootstrap_available:
        print(f"⚠️ watchdog: --plist not found at {args.plist} — bootstrap repair unavailable", file=sys.stderr)
    decision = decide_action(
        report,
        wd_state,
        now,
        max_age=args.max_age,
        paused=paused,
        rebuild_busy=rebuild_busy,
        bootstrap_available=bootstrap_available,
        # WD-17: read-only, and gated on the stale reason (LAG-753).
        daemon_alive=daemon_frozen_alive(report, args.label),
        plateau_processing_age=args.plateau_processing_age,
        plateau_any_age=args.plateau_any_age,
        fp_stall_flat_age=args.fp_stall_flat_age,
    )
    print(f"watchdog: {decision['action']}")
    attach_plist_drift(report, args)  # WD-14: reported every cycle, never acted on
    print_report(report)
    if decision.get("alert"):
        print(f"alert: {decision['alert']}")
        print(_throughput_message(decision, now))

    if args.dry_run:
        print("dry-run: no actions taken")
        return 0 if report["verdict"] == "healthy" else 1

    if decision["action"] == "pause":
        # WD-10: the sentinel means manual maintenance — exit 0, no actions.
        return 0

    if pause_status == "expired":
        retire_expired_pause()

    if decision["restart"]:
        perform_repair(decision["action"], args.label, plist_path)
    if decision["notify"]:
        message = _escalation_message(decision, report, now)
        try:
            notify("Transcriber watchdog", message)
        except Exception as error:  # noqa: BLE001 — ES-6: never abort on notification failure
            print(f"⚠️ watchdog: notification failed: {error}", file=sys.stderr)

    write_wd_state(os.path.expanduser(args.state), decision["state"])  # WD-3/WD-12
    return 0 if report["verdict"] == "healthy" else 1


def remove_pause_sentinel(path: str | Path) -> str:
    """MT-1 step 1 (LAG-680): "removed", "absent" (idempotent re-run) or "failed"."""
    try:
        Path(path).expanduser().unlink()
    except FileNotFoundError:
        return "absent"
    except OSError as error:
        print(f"⚠️ maintenance-end: could not remove pause sentinel {path}: {error}", file=sys.stderr)
        return "failed"
    return "removed"


def wait_for_fresh_heartbeat(
    path: str | Path,
    since: float,
    *,
    timeout: float,
    poll_interval: float,
) -> tuple[dict | None, str]:
    """MT-1 step 3: poll until a heartbeat written AFTER `since` appears.

    A heartbeat that predates the gate proves nothing — a manual run or the
    pre-maintenance daemon may have left it (the 26-09-16 forensics trap).
    Returns (payload, outcome) with outcome "fresh", "fatal" or "timeout".
    """
    target = Path(path).expanduser()
    deadline = time.time() + timeout
    while True:
        try:
            mtime = target.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and mtime >= since - MTIME_SLACK:
            payload, _age, reason = read_heartbeat(str(target))
            if reason is None and payload is not None and payload.get("schema") == HEARTBEAT_SCHEMA_VERSION:
                return payload, "fatal" if payload.get("phase") == "fatal" else "fresh"
        if time.time() >= deadline:
            return None, "timeout"
        time.sleep(poll_interval)


def _maintenance_fail(step: str, detail: str, label: str, heartbeat_path: str) -> int:
    """FAIL banner plus the diagnostics a human needs for the next step."""
    print(f"maintenance-end: FAIL at {step}: {detail}")
    try:
        status = parse_launchctl_list(run_launchctl_list(), label)
    except Exception as error:  # noqa: BLE001 — diagnostics are best-effort
        status = None
        print(f"  launchctl: read failed ({error})")
    else:
        print(f"  launchctl: {status if status else 'service not loaded'}")
    payload, age, reason = read_heartbeat(heartbeat_path)
    if payload is None:
        print(f"  heartbeat: {reason}")
    else:
        print(f"  heartbeat: phase={payload.get('phase')} age={age:.0f}s fatal_reason={payload.get('fatal_reason')}")
    print(f"  next: launchctl print gui/{os.getuid()}/{label}  and check locations/logs/transcriber.log")
    return 1


def run_maintenance_end(args: argparse.Namespace) -> int:
    """--maintenance-end (MT-1, LAG-680): gated completion of manual maintenance.

    In order: remove the pause sentinel → bootstrap the daemon unless already
    loaded → poll for a post-gate heartbeat. Exit 0 PASS, 1 FAIL (with
    diagnostics), 3 internal error. Idempotent; never kickstarts, never sudo.
    """
    since = time.time()
    label: str = args.label
    heartbeat_path = os.path.expanduser(args.heartbeat)
    plist = Path(args.plist).expanduser() if args.plist else None
    sentinel = Path(PAUSE_SENTINEL_PATH).expanduser()

    try:
        loaded = parse_launchctl_list(run_launchctl_list(), label) is not None
    except Exception as error:  # noqa: BLE001 — any launchctl failure is an internal error (exit 3)
        print(f"internal error: launchctl read failed: {error}", file=sys.stderr)
        return 3

    if args.dry_run:
        print("maintenance-end plan (dry-run): no changes made")
        print(f"  1. pause sentinel {sentinel}: {'would remove' if sentinel.exists() else 'absent'}")
        if loaded:
            print(f"  2. {label}: already loaded — would skip bootstrap")
        else:
            state = "found" if plist and plist.exists() else "MISSING"
            print(f"  2. {label}: not loaded — would bootstrap from plist {plist} ({state})")
        print(f"  3. would wait ≤{args.timeout:g}s for a heartbeat at {heartbeat_path}")
        return 0

    # Step 1: sentinel first, so the watchdog is live again the moment we finish.
    removal = remove_pause_sentinel(sentinel)
    if removal == "failed":
        return _maintenance_fail("pause sentinel", f"could not remove {sentinel}", label, heartbeat_path)
    print(f"maintenance-end: 1/3 pause sentinel {removal}")

    # Step 2: bootstrap only when needed — a re-run against a loaded service is a no-op.
    if loaded:
        print(f"maintenance-end: 2/3 {label} already loaded — bootstrap skipped")
    else:
        if plist is None or not plist.exists():
            return _maintenance_fail("bootstrap", f"daemon plist not found: {plist}", label, heartbeat_path)
        if not bootstrap_repair(label, str(plist)):
            # The watchdog may have won the race after step 1; loaded is loaded.
            try:
                loaded = parse_launchctl_list(run_launchctl_list(), label) is not None
            except Exception:  # noqa: BLE001 — treated as not loaded
                loaded = False
            if not loaded:
                return _maintenance_fail("bootstrap", f"launchctl bootstrap of {plist} failed", label, heartbeat_path)
        print(f"maintenance-end: 2/3 {label} bootstrapped")

    # Step 3: proof of life written after the gate started.
    payload, outcome = wait_for_fresh_heartbeat(
        heartbeat_path, since, timeout=args.timeout, poll_interval=args.poll_interval
    )
    if outcome == "fatal":
        reason = payload.get("fatal_reason") if payload else None
        return _maintenance_fail("heartbeat", f"daemon reported phase=fatal ({reason})", label, heartbeat_path)
    if outcome == "timeout":
        return _maintenance_fail("heartbeat", f"no fresh heartbeat within {args.timeout:g}s", label, heartbeat_path)
    phase = payload.get("phase") if payload else None
    print(f"maintenance-end: 3/3 fresh heartbeat (phase={phase})")
    print("maintenance-end: PASS")
    return 0


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
        "--plateau-processing-age",
        type=float,
        default=DEFAULT_PLATEAU_PROCESSING_AGE,
        help=(
            "seconds state_complete may stay flat under phase=processing before a throughput-plateau"
            " alert (TP-4, LAG-799)"
        ),
    )
    parser.add_argument(
        "--plateau-any-age",
        type=float,
        default=DEFAULT_PLATEAU_ANY_AGE,
        help="seconds state_complete may stay flat in any phase before the alert (TP-4, LAG-799)",
    )
    parser.add_argument(
        "--fp-stall-flat-age",
        type=float,
        default=DEFAULT_FP_STALL_FLAT_AGE,
        help="flatness required before a failed_permanent rise counts as the stall (TP-2, LAG-800)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report only; never restart, write state, or notify (HC-9)"
    )
    parser.add_argument("--heal", action="store_true", help="watchdog mode: decide and act on the verdict")
    parser.add_argument("--state", default=WD_STATE_PATH, help="watchdog state file (WD-3)")
    parser.add_argument(
        "--plist",
        default=None,
        help=(
            "daemon plist path, passed at install time; enables bootstrap repair"
            " of a booted-out service (WD-9 revision, LAG-673)"
        ),
    )
    parser.add_argument(
        "--plist-template",
        default=DEFAULT_PLIST_TEMPLATE,
        help="committed plist template to hash-compare --plist against (WD-14, LAG-684)",
    )
    parser.add_argument("--repo", default=DEFAULT_REPO_DIR, help="expansion for the template's __REPO__ (WD-14)")
    parser.add_argument("--home", default=os.path.expanduser("~"), help="expansion for the template's __HOME__ (WD-14)")
    parser.add_argument("--self-label", default=DEFAULT_SELF_LABEL, help="this watchdog's own label (WD-2)")
    parser.add_argument("--notify-test", action="store_true", help="send one test notification and exit (ES-9)")
    parser.add_argument(
        "--maintenance-end",
        action="store_true",
        help="end manual maintenance: remove pause sentinel, bootstrap, wait for a fresh heartbeat (MT-1, LAG-680)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=MAINTENANCE_END_TIMEOUT,
        help="--maintenance-end: seconds to wait for a fresh heartbeat",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=MAINTENANCE_END_POLL,
        help="--maintenance-end: seconds between heartbeat polls",
    )
    args = parser.parse_args(argv)

    if args.notify_test:
        notify("Transcriber watchdog", "Test notification — delivery path OK")
        print("test notification sent")
        return 0

    if args.maintenance_end:
        return run_maintenance_end(args)

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
    # HC-17: a heartbeat an ops run refreshed needs a live daemon PID to count.
    report = assess_health(payload, age, status, args.max_age, daemon_pid=cross_check_daemon_pid(payload, args.label))
    if read_reason == "heartbeat_unreadable":
        report["reason"] = "heartbeat_unreadable"
        report["verdict"] = "unhealthy"
    attach_plist_drift(report, args)  # WD-14
    print_report(report, dry_run=args.dry_run)
    return 0 if report["verdict"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
