#!/usr/bin/env python3
"""Reclassify and fix MeetingTranscriber files.

1. Generate missing analysis files for existing transcripts
2. Re-classify "Unknown Meeting" files and move them to correct category folders

Usage:
    python reclassify_and_fix.py --generate-missing-analysis [--dry-run] [--verbose]
    python reclassify_and_fix.py --reclassify [--dry-run] [--verbose]
    python reclassify_and_fix.py --generate-missing-analysis --reclassify [--dry-run]

NOTE: Both operations depend on Superwhisper re-processing. They are stubbed until
the Superwhisper pipeline (switch_superwhisper_mode / handoff_to_superwhisper /
wait_for_superwhisper_result / parse_superwhisper_output) is implemented in pipeline.py.
"""

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

from config import DELAY_BETWEEN_FILES, FOLDERS


def find_missing_analysis(folders, verbose=False):
    """Scan all category folders, return list of transcripts without analysis."""
    missing = []
    for category, base_path in folders.items():
        if (transcripts_dir := Path(base_path) / "transcripts").exists():
            for fp in transcripts_dir.glob("*.md"):
                if not (transcripts_dir.parent / "analysis" / f"{fp.stem} - Analysis.md").exists():
                    missing.append((str(fp), category))
                    if verbose:
                        print(f"  Missing analysis: {category}/{fp.name}", flush=True)

    return missing


def generate_missing_analysis(transcript_path, category, dry_run=False, verbose=False):
    """TODO: not yet implemented; re-process the original audio file to regenerate analysis."""
    if dry_run:
        print(f"  [DRY RUN] Would generate analysis for: {category}/{Path(transcript_path).name}", flush=True)
        return True

    print(
        f"  ⚠️  Cannot generate analysis for {Path(transcript_path).name} — Superwhisper pipeline not yet implemented.\n"
        "     Re-process the original audio file to regenerate transcript + analysis together.",
        flush=True,
    )
    return False


def reclassify_transcript(transcript_path, dry_run=False, verbose=False):
    """TODO: not yet implemented; returns (new_category, new_filename) or None when complete."""
    print(
        f"  ⚠️  Cannot reclassify {Path(transcript_path).name} — Superwhisper pipeline not yet implemented.\n"
        "     Re-process the original audio file to get updated category + filename.",
        flush=True,
    )
    return None


def move_transcript_and_analysis(old_transcript_path, new_category, new_filename, dry_run=False, verbose=False):
    """Move both transcript and analysis files to new category folder."""
    if not (match := re.match(r"^(\d{2}-\d{2}-\d{2}\s+\d{2}\.\d{2})", Path(old_transcript_path).name)):
        print(f"  ❌ Could not extract timestamp from: {Path(old_transcript_path).name}", flush=True)
        return False
    new_full_filename = f"{match.group(1)} - {new_filename.removesuffix('.md')}.md"

    transcripts_dir = Path(FOLDERS.get(new_category, FOLDERS["DEFAULT"])) / "transcripts"
    analysis_dir = transcripts_dir.parent / "analysis"
    new_transcript_path = transcripts_dir / new_full_filename

    if new_transcript_path.exists():
        print(f"  ⚠️  File already exists at destination: {new_transcript_path}", flush=True)
        stem, counter = new_full_filename.removesuffix(".md"), 2
        while (transcripts_dir / f"{stem} ({counter}).md").exists():
            counter += 1
        new_transcript_path = transcripts_dir / (new_full_filename := f"{stem} ({counter}).md")

    new_analysis_path = analysis_dir / f"{new_full_filename.removesuffix('.md')} - Analysis.md"
    has_analysis = Path(
        old_analysis_path := old_transcript_path.replace("/transcripts/", "/analysis/").replace(".md", " - Analysis.md")
    ).exists()

    if dry_run:
        print(f"  [DRY RUN] Would move:\n    FROM: {old_transcript_path}\n    TO:   {new_transcript_path}", flush=True)
        if has_analysis:
            print(f"    AND:  {old_analysis_path}\n    TO:   {new_analysis_path}", flush=True)
        return True

    try:
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        analysis_dir.mkdir(parents=True, exist_ok=True)

        shutil.move(old_transcript_path, new_transcript_path)
        if verbose:
            print(f"  ✅ Moved transcript: {new_full_filename}", flush=True)

        if has_analysis:
            shutil.move(old_analysis_path, new_analysis_path)
            if verbose:
                print(f"  ✅ Moved analysis: {new_analysis_path.name}", flush=True)
        elif verbose:
            print("  ⚠️  No analysis file to move", flush=True)

        return True

    except Exception as e:
        print(f"  ❌ Error moving files: {e}", flush=True)
        try:
            if new_transcript_path.exists() and not Path(old_transcript_path).exists():
                shutil.move(new_transcript_path, old_transcript_path)
                print("  🔄 Rolled back transcript move", flush=True)
        except Exception:
            pass
        return False


def scan_default_folder(verbose=False):
    if not (transcripts_dir := Path(FOLDERS["DEFAULT"]) / "transcripts").exists():
        print(f"⚠️  DEFAULT transcripts folder not found: {transcripts_dir}", flush=True)
        return []

    transcripts = [str(f) for f in transcripts_dir.glob("*.md")]
    if verbose:
        print(f"Found {len(transcripts)} transcripts in DEFAULT folder", flush=True)
    return transcripts


def main():
    parser = argparse.ArgumentParser(description="Reclassify and fix RecordingAnalyser files")
    parser.add_argument("--generate-missing-analysis", action="store_true", help="Generate missing analysis files")
    parser.add_argument("--reclassify", action="store_true", help="Reclassify and move Unknown Meeting files")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without executing")
    parser.add_argument("--verbose", action="store_true", help="Show detailed progress")

    args = parser.parse_args()

    if not args.generate_missing_analysis and not args.reclassify:
        parser.print_help()
        print("\n⚠️  Please specify at least one operation: --generate-missing-analysis or --reclassify")
        sys.exit(1)

    print(f"{'=' * 60}\n🔧 RecordingAnalyser Maintenance\n{'=' * 60}")

    if args.dry_run:
        print("⚠️  DRY RUN MODE - No files will be modified\n")

    if args.generate_missing_analysis:
        print(f"\n📊 Task 1: Generating Missing Analysis Files\n{'-' * 60}")

        missing = find_missing_analysis(FOLDERS, verbose=args.verbose)

        if not missing:
            print("✅ No missing analysis files found!")
        else:
            print(f"Found {len(missing)} transcripts without analysis\n")
            success_count = 0

            for i, (transcript_path, category) in enumerate(missing, 1):
                print(f"[{i}/{len(missing)}] {category}/{Path(transcript_path).name}")

                success_count += generate_missing_analysis(transcript_path, category, args.dry_run, args.verbose)

                if i < len(missing) and not args.dry_run:
                    print(f"  ⏸️  Pausing {DELAY_BETWEEN_FILES}s between files...", flush=True)
                    time.sleep(DELAY_BETWEEN_FILES)

            print(f"\n📊 Results: ✅ {success_count} success, ❌ {len(missing) - success_count} failed")

    if args.reclassify:
        print(f"\n📁 Task 2: Reclassifying and Moving Files\n{'-' * 60}")

        unknown_meetings = [t for t in scan_default_folder(verbose=args.verbose) if "Unknown Meeting" in Path(t).name]

        if not unknown_meetings:
            print("✅ No 'Unknown Meeting' files found in DEFAULT folder!")
        else:
            print(f"Found {len(unknown_meetings)} 'Unknown Meeting' files\n")
            moved_count = skipped_count = 0

            for i, transcript_path in enumerate(unknown_meetings, 1):
                print(f"[{i}/{len(unknown_meetings)}] {Path(transcript_path).name}")

                if (result := reclassify_transcript(transcript_path, args.dry_run, args.verbose)) is None:
                    print("  ❌ Classification failed")
                    continue

                new_category, new_filename = result
                if new_category == "DEFAULT":
                    print("  ⚠️  Still classified as DEFAULT, skipping move")
                    skipped_count += 1
                    continue

                moved_count += move_transcript_and_analysis(
                    transcript_path, new_category, new_filename, args.dry_run, args.verbose
                )

            print(
                f"\n📊 Results: ✅ {moved_count} moved, ⚠️ {skipped_count} skipped, ❌ {len(unknown_meetings) - moved_count - skipped_count} failed"
            )

    print(f"\n{'=' * 60}\n✅ Done!\n{'=' * 60}")


if __name__ == "__main__":
    main()
