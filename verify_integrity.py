#!/usr/bin/env python3
"""Pipeline integrity verification: cross-check state vs vault vs watch folder.

Reports four classes of drift that the 2026-07-24 incident exposed:
  - missing_md:       state status=complete but no .md in FOLDERS[category] with matching
                      timestamp prefix (file got lost between state update and vault write,
                      or was later deleted/moved out).
  - wrong_folder_md:  state status=complete, .md exists but in a different category's
                      folder — a misroute. The 2026-07-24 incident was this class.
  - orphan_md:        .md in a FOLDERS[*] folder with no state entry claiming that
                      timestamp — manual moves, pre-state files, or pipeline-state wipes.
  - untracked_audio:  audio file in watch folder not in state["processed"] — will be
                      picked up next scan, but useful for diffing "what's pending".

Exit code 0 if clean, 1 if any issues found. Read-only — safe to run anytime.

Usage:
    python verify_integrity.py [--watch-folder PATH] [--scan-days-back N] [--verbose]
    ./run_transcriber.sh verify          # when wired into the wrapper
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from config import FOLDERS, SCAN_DAYS_BACK, WATCH_FOLDER
from pipeline import (
    ANALYSIS_SUFFIX,
    DEFAULT_CATEGORY,
    MARKDOWN_EXT,
    TIMESTAMP_FORMAT,
    TIMESTAMP_KEY_LENGTH,
    discover_recent_folders,
    load_state,
)


def _state_timestamp_key(entry: dict) -> str | None:
    """Convert a state entry's ISO timestamp to the "YY-MM-DD HH.MM" filename-prefix key."""
    ts = entry.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).strftime(TIMESTAMP_FORMAT)
    except ValueError:
        return None


def _scan_vault_by_category(folders: dict[str, str]) -> dict[str, dict[str, Path]]:
    """Walk vault folders; return {category: {timestamp_key: Path}} for non-legacy .md files."""
    vault_by_category: dict[str, dict[str, Path]] = {}
    for category, base_path in folders.items():
        files: dict[str, Path] = {}
        base = Path(base_path)
        if base.exists():
            for fp in base.glob(f"*{MARKDOWN_EXT}"):
                name = fp.name
                if name.endswith(ANALYSIS_SUFFIX) or len(name) < TIMESTAMP_KEY_LENGTH:
                    continue
                files[name[:TIMESTAMP_KEY_LENGTH]] = fp
        vault_by_category[category] = files
    return vault_by_category


def _check_state_completeness(
    state: dict, vault_by_category: dict[str, dict[str, Path]]
) -> tuple[list, list, int, set[str]]:
    """Cross-check state status=complete entries against the vault.

    Returns (missing_md, wrong_folder_md, state_complete_count, state_claims_by_key).
    `state_claims_by_key` is the set of timestamp keys claimed by any complete entry —
    used by the orphan check to suppress .md files that ARE claimed (possibly misplaced,
    already reported in wrong_folder_md).
    """
    missing_md, wrong_folder_md = [], []
    state_claims_by_key: set[str] = set()
    state_complete_count = 0
    for audio_path, entry in state.get("processed", {}).items():
        if entry.get("status") != "complete":
            continue
        state_complete_count += 1
        key = _state_timestamp_key(entry)
        if key is None:
            continue
        state_claims_by_key.add(key)
        category = entry.get("category", DEFAULT_CATEGORY)
        if key in vault_by_category.get(category, {}):
            continue
        # Not in expected folder — check if it's misplaced elsewhere.
        actual_category = next(
            (
                other_cat
                for other_cat, other_files in vault_by_category.items()
                if other_cat != category and key in other_files
            ),
            None,
        )
        if actual_category is not None:
            wrong_folder_md.append(
                {
                    "audio_path": audio_path,
                    "timestamp_key": key,
                    "expected_category": category,
                    "actual_category": actual_category,
                }
            )
        else:
            missing_md.append({"audio_path": audio_path, "timestamp_key": key, "expected_category": category})
    return missing_md, wrong_folder_md, state_complete_count, state_claims_by_key


def _find_orphans(folders: dict[str, str], state_claims_by_key: set[str]) -> tuple[list, set[Path]]:
    """Find .md files in vault folders that no state entry claims.

    Returns (orphan_md, seen_paths). `seen_paths` is the dedup set of all .md files
    seen (used by the summary's vault_md count); dedup matters because FOLDERS may
    have aliases (DEFAULT and PERSONLIG often point to the same path).
    """
    orphan_md: list = []
    seen_paths: set[Path] = set()
    for category, base_path in folders.items():
        base = Path(base_path)
        if not base.exists():
            continue
        for fp in base.glob(f"*{MARKDOWN_EXT}"):
            if fp in seen_paths:
                continue
            seen_paths.add(fp)
            name = fp.name
            if name.endswith(ANALYSIS_SUFFIX) or len(name) < TIMESTAMP_KEY_LENGTH:
                continue
            key = name[:TIMESTAMP_KEY_LENGTH]
            if key in state_claims_by_key:
                continue
            orphan_md.append({"path": str(fp), "category": category, "timestamp_key": key})
    return orphan_md, seen_paths


def _find_untracked_audio(watch_folder: str, scan_days_back: int, processed_paths: set[str]) -> list[str]:
    """Find .m4a files in recent watch-folder date dirs not in state["processed"]."""
    untracked_audio: list[str] = []
    for folder_path in discover_recent_folders(watch_folder, scan_days_back):
        for fp in Path(folder_path).glob("*.m4a"):
            p = str(fp)
            if ".icloud" in p or ".tmp" in p or fp.name.startswith("."):
                continue
            if p not in processed_paths:
                untracked_audio.append(p)
    return untracked_audio


def verify_integrity(
    state: dict,
    folders: dict[str, str],
    watch_folder: str,
    scan_days_back: int = 7,
) -> dict:
    """Cross-check state vs vault vs watch folder; return a report dict.

    Args:
        state: pipeline state dict (as loaded by load_state()).
        folders: category → base path mapping (typically config.FOLDERS).
        watch_folder: JPR root to scan for untracked audio.
        scan_days_back: how many date-subfolders back to scan for audio.

    Returns:
        {
            "missing_md":       [{audio_path, timestamp_key, expected_category}, ...],
            "wrong_folder_md":   [{audio_path, timestamp_key, expected_category, actual_category}, ...],
            "orphan_md":         [{path, category, timestamp_key}, ...],
            "untracked_audio":   [path, ...],
            "summary":           {state_complete, vault_md, watch_audio_untracked},
        }
    """
    vault_by_category = _scan_vault_by_category(folders)
    missing_md, wrong_folder_md, state_complete_count, state_claims_by_key = _check_state_completeness(
        state, vault_by_category
    )
    orphan_md, seen_paths = _find_orphans(folders, state_claims_by_key)
    processed_paths = set(state.get("processed", {}).keys())
    untracked_audio = _find_untracked_audio(watch_folder, scan_days_back, processed_paths)

    return {
        "missing_md": missing_md,
        "wrong_folder_md": wrong_folder_md,
        "orphan_md": orphan_md,
        "untracked_audio": untracked_audio,
        "summary": {
            "state_complete": state_complete_count,
            "vault_md": len(seen_paths),
            "watch_audio_untracked": len(untracked_audio),
        },
    }


def print_report(report: dict, file=sys.stdout) -> None:
    """Pretty-print the verify report; returns nothing."""
    s = report["summary"]
    print(
        f"\n📚 State: {s['state_complete']} complete | Vault: {s['vault_md']} .md files | "
        f"Untracked audio: {s['watch_audio_untracked']}",
        file=file,
    )

    def _section(title: str, items: list, formatter) -> None:
        if not items:
            return
        print(f"\n❌ {title} ({len(items)}):", file=file)
        for item in items:
            print(f"  - {formatter(item)}", file=file)

    _section(
        "Missing .md (state says complete, no file in vault)",
        report["missing_md"],
        lambda i: f"[{i['expected_category']}] {i['timestamp_key']} ← {i['audio_path']}",
    )
    _section(
        "Wrong-folder .md (misroute)",
        report["wrong_folder_md"],
        lambda i: f"[{i['expected_category']}→{i['actual_category']}] {i['timestamp_key']} ← {i['audio_path']}",
    )
    _section(
        "Orphan .md (in vault, no state entry)",
        report["orphan_md"],
        lambda i: f"[{i['category']}] {i['timestamp_key']} — {i['path']}",
    )
    _section(
        "Untracked audio (in watch folder, not in state)",
        report["untracked_audio"],
        lambda i: i,
    )

    issues = sum(len(report[k]) for k in ("missing_md", "wrong_folder_md", "orphan_md", "untracked_audio"))
    print(f"\n{'=' * 50}", file=file)
    print("✅ No issues found." if issues == 0 else f"⚠️  {issues} issue(s) found.", file=file)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify pipeline integrity: state vs vault vs watch folder.")
    parser.add_argument("--watch-folder", default=WATCH_FOLDER, help="JPR root to scan")
    parser.add_argument(
        "--scan-days-back",
        type=int,
        default=SCAN_DAYS_BACK,
        help="Recent date-subfolders to scan for audio (default: %(default)s)",
    )
    args = parser.parse_args()

    state = load_state()
    print(f"{'=' * 50}\n🔍 Pipeline Integrity Check\n{'=' * 50}")
    report = verify_integrity(state, FOLDERS, args.watch_folder, args.scan_days_back)
    print_report(report)
    sys.exit(1 if any(report[k] for k in ("missing_md", "wrong_folder_md", "orphan_md", "untracked_audio")) else 0)


if __name__ == "__main__":
    main()
