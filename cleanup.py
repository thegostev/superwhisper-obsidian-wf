"""Recordings-store retention and orphan cleanup (LAG-911).

Superwhisper's recordings store only grows: every handoff leaves a session dir
holding ``meta.json`` + ``output.wav``, and failed or crashed handoffs leave
dirs with no ``meta.json`` at all. This command deletes only:

* consumed session dirs (the pipeline's ``.pipeline-consumed`` sentinel)
  older than ``recordings_retention_days``;
* orphan dirs with no ``meta.json`` older than ``orphan_retention_hours``;

and never a dir referenced by a state entry that has not reached ``complete``.
Cleanup is always manual; ``--apply`` is required to delete anything.
"""

import argparse
import contextlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from config import ORPHAN_RETENTION_HOURS, RECORDINGS_RETENTION_DAYS, STATE_FILE, SUPERWHISPER_RECORDINGS_DIR
from pipeline import CONSUMED_SENTINEL

SECONDS_PER_HOUR = 3600.0
HOURS_PER_DAY = 24.0

REASON_CONSUMED = "consumed"
REASON_ORPHAN = "orphan"


@dataclass(frozen=True)
class CleanupCandidate:
    """One session dir that retention says may be deleted."""

    path: Path
    reason: str
    age_hours: float
    bytes: int


@dataclass(frozen=True)
class CleanupPlan:
    """The outcome of scanning the recordings store, before anything is touched."""

    candidates: tuple[CleanupCandidate, ...]
    kept_by_retention: int
    kept_protected: int

    @property
    def candidate_bytes(self) -> int:
        """Total bytes the plan would free."""
        return sum(candidate.bytes for candidate in self.candidates)


@dataclass(frozen=True)
class CleanupOutcome:
    """What an ``--apply`` run actually deleted."""

    deleted: int
    freed_bytes: int
    failures: tuple[tuple[Path, str], ...]


def _referenced_recording_dirs(state: dict) -> set[str]:
    """Session-dir names still owned by a non-terminal state entry.

    Failed attempts persist the dir under ``recovery.recording_dir``; a retry or
    in-flight attempt may carry it at the top level. Either way, deleting that
    dir would discard evidence a retry, salvage or triage still needs.
    """
    names: set[str] = set()
    for record in state.get("processed", {}).values():
        if record.get("status") == "complete":
            continue
        if name := record.get("recording_dir"):
            names.add(str(name))
        if name := (record.get("recovery") or {}).get("recording_dir"):
            names.add(str(name))
    return names


def _age_hours(path: Path, now: float) -> float | None:
    """Age of ``path``'s mtime in hours; None when it cannot be stat'ed."""
    try:
        return max(0.0, (now - path.stat().st_mtime) / SECONDS_PER_HOUR)
    except OSError:
        return None


def _tree_bytes(path: Path) -> int:
    """Total size of the regular files under ``path``."""
    total = 0
    for root, _dirs, files in path.walk():
        for name in files:
            with contextlib.suppress(OSError):
                total += (root / name).stat().st_size
    return total


def plan_cleanup(
    recordings_dir: Path,
    state: dict,
    *,
    now: float | None = None,
    recordings_retention_days: float = RECORDINGS_RETENTION_DAYS,
    orphan_retention_hours: float = ORPHAN_RETENTION_HOURS,
) -> CleanupPlan:
    """Classify the store's session dirs into deletable candidates and kept dirs.

    Args:
        recordings_dir: Superwhisper's recordings store; only its immediate
            child directories are considered.
        state: The pipeline state, used to protect dirs still owned by a
            non-terminal entry.
        now: Epoch seconds to judge age against; defaults to the current time.
        recordings_retention_days: Consumed dirs younger than this are kept.
        orphan_retention_hours: Meta-less dirs younger than this are kept.

    Returns:
        The plan, including counts of dirs kept by retention and protection.
    """
    current_time = time.time() if now is None else now
    protected = _referenced_recording_dirs(state)
    candidates: list[CleanupCandidate] = []
    kept_by_retention = 0
    kept_protected = 0

    for entry in sorted(Path(recordings_dir).iterdir(), key=lambda path: path.name):
        if entry.name.startswith(".") or not entry.is_dir() or entry.is_symlink():
            continue

        marker = entry / CONSUMED_SENTINEL
        if marker.exists():
            reason = REASON_CONSUMED
            age = _age_hours(marker, current_time)  # retention runs from consumption
            cutoff_hours = recordings_retention_days * HOURS_PER_DAY
        elif not (entry / "meta.json").exists():
            reason = REASON_ORPHAN
            age = _age_hours(entry, current_time)
            cutoff_hours = orphan_retention_hours
        else:
            continue  # completed-looking but unconsumed: never ours to delete

        if age is None:
            continue
        if entry.name in protected:
            kept_protected += 1
            continue
        if age < cutoff_hours:
            kept_by_retention += 1
            continue
        candidates.append(CleanupCandidate(entry, reason, age, _tree_bytes(entry)))

    return CleanupPlan(tuple(candidates), kept_by_retention, kept_protected)


def apply_cleanup(plan: CleanupPlan) -> CleanupOutcome:
    """Delete every dir in ``plan``; a failed deletion is reported, not raised."""
    deleted = 0
    freed_bytes = 0
    failures: list[tuple[Path, str]] = []
    for candidate in plan.candidates:
        try:
            shutil.rmtree(candidate.path)
        except OSError as error:
            failures.append((candidate.path, str(error)))
            continue
        deleted += 1
        freed_bytes += candidate.bytes
    return CleanupOutcome(deleted, freed_bytes, tuple(failures))


def _format_bytes(num_bytes: int) -> str:
    """Human-readable size, e.g. ``3.4 GiB``."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _load_state_strict() -> dict | None:
    """Load the pipeline state; None when a present file cannot be parsed.

    The daemon's ``load_state`` treats a corrupt file as empty, which is safe
    there (it re-queues). Here an empty state means no protection at all, so an
    unreadable file must abort cleanup instead of letting it delete.
    """
    state_path = Path(STATE_FILE)
    if not state_path.exists():
        return {"processed": {}}
    try:
        loaded: object = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _print_plan(plan: CleanupPlan, *, applying: bool) -> None:
    """One line per candidate, then the kept counts."""
    verb = "Deleting" if applying else "Would delete"
    for candidate in plan.candidates:
        print(
            f"  {verb} [{candidate.reason}] {candidate.path.name} — "
            f"{candidate.age_hours / HOURS_PER_DAY:.1f} days, {_format_bytes(candidate.bytes)}",
            flush=True,
        )
    print(
        f"Kept: {plan.kept_by_retention} within retention, "
        f"{plan.kept_protected} protected by non-complete state entries",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Deletes nothing unless ``--apply`` is passed."""
    parser = argparse.ArgumentParser(
        description="Delete expired consumed and orphan Superwhisper session dirs (LAG-911)."
    )
    parser.add_argument("--apply", action="store_true", help="Delete the listed dirs (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; this is the default")
    parser.add_argument("--recordings-dir", type=Path, default=None, help="Override the recordings store path")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    recordings_dir = args.recordings_dir or Path(SUPERWHISPER_RECORDINGS_DIR)
    if not recordings_dir.is_dir():
        print(f"Recordings store not found: {recordings_dir}", flush=True)
        return 1

    state = _load_state_strict()
    if state is None:
        print(f"Cannot read state file {STATE_FILE}; refusing to delete anything.", flush=True)
        return 1

    plan = plan_cleanup(recordings_dir, state)
    print(f"Recordings store: {recordings_dir}", flush=True)
    print(
        f"Retention: consumed ≥ {RECORDINGS_RETENTION_DAYS:g} days, "
        f"orphans (no meta.json) ≥ {ORPHAN_RETENTION_HOURS:g} hours",
        flush=True,
    )
    _print_plan(plan, applying=args.apply)

    if not args.apply:
        if plan.candidates:
            print(
                f"DRY RUN — nothing deleted. Would free {_format_bytes(plan.candidate_bytes)}; pass --apply to delete.",
                flush=True,
            )
        else:
            print("DRY RUN — nothing to delete.", flush=True)
        return 0

    outcome = apply_cleanup(plan)
    print(f"Deleted {outcome.deleted} dir(s), freed {_format_bytes(outcome.freed_bytes)}", flush=True)
    for path, error in outcome.failures:
        print(f"  ⚠️  Could not delete {path}: {error}", flush=True)
    return 1 if outcome.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
