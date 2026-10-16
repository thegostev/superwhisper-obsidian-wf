"""iCloud placeholder preflight: ask iCloud to materialize recordings the daemon cannot see.

LAG-813 (spec IC-*), root cause LAG-756. Just Press Record syncs recordings into
an iCloud container. Until iCloud downloads a recording, the Mac only holds a
placeholder stub named ``.<name>.m4a.icloud``. The daemon's scan skips those
stubs, and nothing in the pipeline ever asks iCloud to download them, so a
recording can sit invisible for hours or days (≈20 h on Oct 7, ~2 days on
Sep 21-23).

This module finds the stubs under the scanned date folders and runs
``brctl download`` on each, so the real file lands and a later scan cycle picks
it up. It never raises into the caller (IC-4): every ``brctl`` failure mode is
logged and counted, and the scan cycle continues.

The module imports nothing from ``config``/``pipeline`` at import time, so the
CLI runs with ``--folder`` on any machine, including from a launchd context,
without a config file (IC-6).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_SUFFIX = ".icloud"
AUDIO_SUFFIX = ".m4a"
BRCTL = "/usr/bin/brctl"

#: IC-3 defaults: per-call timeout, re-request cooldown, per-cycle cap. The cap
#: and timeout together bound one pass to cap x timeout seconds of worst-case
#: wall time (100 s), well under the 300 s heartbeat staleness threshold (HC-14).
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_MAX_REQUESTS = 10

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class PreflightResult:
    """Outcome counters of one preflight pass (IC-5)."""

    found: int = 0
    requested: int = 0
    would_request: int = 0
    skipped_cooldown: int = 0
    deferred: int = 0
    timed_out: int = 0
    failed: int = 0
    brctl_missing: bool = False
    dry_run: bool = False

    def summary(self) -> str:
        parts = [f"{self.found} placeholder(s)"]
        if self.dry_run:
            parts.append(f"would request {self.would_request} download(s) [dry-run]")
        else:
            parts.append(f"requested {self.requested} download(s)")
        for label, value in (
            ("in cooldown", self.skipped_cooldown),
            ("deferred to next cycle", self.deferred),
            ("timed out", self.timed_out),
            ("failed", self.failed),
        ):
            if value:
                parts.append(f"{value} {label}")
        if self.brctl_missing:
            parts.append("brctl unavailable")
        return ", ".join(parts)


def placeholder_target(path: Path) -> Path | None:
    """Return the real file a ``.<name>.m4a.icloud`` stub stands for, else None (IC-1).

    Only audio stubs count: JPR's container also syncs non-audio files, and
    downloading those buys the pipeline nothing.
    """
    name = path.name
    if not (name.startswith(".") and name.endswith(PLACEHOLDER_SUFFIX)):
        return None
    real = name[1 : -len(PLACEHOLDER_SUFFIX)]
    if not real or real.startswith(".") or Path(real).suffix.lower() != AUDIO_SUFFIX:
        return None
    return path.with_name(real)


def find_placeholders(folders: Iterable[str | Path]) -> list[Path]:
    """Audio placeholder stubs anywhere under ``folders``, nested dirs included (IC-1).

    The caller passes the date folders inside the scan window, so stubs that
    "Optimize Mac Storage" left on old recordings are never mass-downloaded
    (IC-2). An unreadable folder is logged and skipped, never raised.
    """
    found: set[Path] = set()
    for folder in folders:
        root = Path(folder)
        try:
            for candidate in root.rglob(f".*{PLACEHOLDER_SUFFIX}"):
                if candidate.is_file() and placeholder_target(candidate) is not None:
                    found.add(candidate)
        except OSError as e:
            print(f"   ⚠️  iCloud preflight: cannot scan {root}: {e}", flush=True)
    return sorted(found)


def request_downloads(
    placeholders: Iterable[Path],
    *,
    dry_run: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    last_requested: dict[str, float] | None = None,
    brctl: str = BRCTL,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
) -> PreflightResult:
    """Run ``brctl download`` on each placeholder; never raises (IC-3, IC-4).

    Args:
        placeholders: Stub paths from ``find_placeholders``.
        dry_run: Log what would be requested; run nothing, stamp nothing.
        timeout: Seconds before one ``brctl`` call is abandoned.
        cooldown: Seconds before the same stub is requested again. iCloud may
            take minutes to finish a download; re-requesting every 30 s scan
            cycle only adds load.
        max_requests: Cap on ``brctl`` calls per pass; the rest wait for the
            next cycle.
        last_requested: Stub path -> clock time of its last request. Mutated.
        brctl: The ``brctl`` executable.
        runner: ``subprocess.run`` stand-in (tests).
        clock: Monotonic clock stand-in (tests).

    Returns:
        Counters for the pass.
    """
    stamps = last_requested if last_requested is not None else {}
    result = PreflightResult(dry_run=dry_run)
    attempts = 0
    for stub in placeholders:
        result.found += 1
        key = str(stub)
        now = clock()
        if key in stamps and now - stamps[key] < cooldown:
            result.skipped_cooldown += 1
            continue
        if dry_run:
            result.would_request += 1
            print(f"   ☁️  [dry-run] would run: {brctl} download {key}", flush=True)
            continue
        if result.brctl_missing or attempts >= max_requests:
            result.deferred += 1
            continue
        attempts += 1
        stamps[key] = now
        _request_one(key, result, timeout=timeout, brctl=brctl, runner=runner)
    return result


def _request_one(key: str, result: PreflightResult, *, timeout: float, brctl: str, runner: Runner) -> None:
    """One ``brctl download`` call; outcome recorded on ``result``."""
    try:
        completed = runner([brctl, "download", key], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        result.timed_out += 1
        print(f"   ⚠️  iCloud preflight: brctl download timed out after {timeout:g}s: {key}", flush=True)
        return
    except OSError as e:
        # Missing binary or permission denied: every later call fails the same
        # way, so stop calling for this pass.
        result.brctl_missing = True
        print(f"   ⚠️  iCloud preflight: cannot run {brctl}: {e}", flush=True)
        return
    if completed.returncode == 0:
        result.requested += 1
        return
    result.failed += 1
    detail = (completed.stderr or completed.stdout or "").strip()
    print(
        f"   ⚠️  iCloud preflight: brctl download exited {completed.returncode} for {key}"
        + (f": {detail}" if detail else ""),
        flush=True,
    )


def run_preflight(
    folders: Iterable[str | Path],
    *,
    dry_run: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    last_requested: dict[str, float] | None = None,
    brctl: str = BRCTL,
    runner: Runner = subprocess.run,
) -> PreflightResult:
    """Find placeholders under ``folders`` and request their download (IC-1..IC-5).

    Silent when no placeholder is present; one summary line otherwise.
    """
    result = request_downloads(
        find_placeholders(folders),
        dry_run=dry_run,
        timeout=timeout,
        cooldown=cooldown,
        max_requests=max_requests,
        last_requested=last_requested,
        brctl=brctl,
        runner=runner,
    )
    if result.found:
        print(f"☁️  iCloud preflight: {result.summary()}", flush=True)
    return result


def _default_folders(days_back: int | None) -> list[str]:
    """The daemon's scan window, from config (imported lazily — IC-6)."""
    from config import SCAN_DAYS_BACK, WATCH_FOLDER
    from pipeline import discover_recent_folders

    return discover_recent_folders(WATCH_FOLDER, SCAN_DAYS_BACK if days_back is None else days_back)


def main(argv: list[str] | None = None) -> int:
    """CLI for smoke tests and for checking ``brctl`` from a launchd context (IC-6).

    Exit codes: 0 = pass completed (including "nothing to do"); 2 = ``brctl``
    could not be run, so the daemon's preflight cannot work in this context.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--dry-run", action="store_true", help="list what would be downloaded; run nothing")
    parser.add_argument(
        "--folder",
        action="append",
        default=None,
        help="folder to scan (repeatable); default: the daemon's watch folder date dirs from config",
    )
    parser.add_argument("--days", type=int, default=None, help="scan window in days (default: scan_days_back)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="seconds per brctl call")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_REQUESTS, help="max brctl calls this pass")
    parser.add_argument("--brctl", default=BRCTL, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    folders = args.folder if args.folder else _default_folders(args.days)
    result = run_preflight(
        folders,
        dry_run=args.dry_run,
        timeout=args.timeout,
        max_requests=args.max,
        brctl=args.brctl,
    )
    if not result.found:
        print("☁️  iCloud preflight: no placeholders", flush=True)
    return 2 if result.brctl_missing else 0


if __name__ == "__main__":
    sys.exit(main())
