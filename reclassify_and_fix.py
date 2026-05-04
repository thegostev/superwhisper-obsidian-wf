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
import os
import re
import shutil
import sys
import time

from config import DELAY_BETWEEN_FILES, FOLDERS
from pipeline import (
    FatalAPIError,
    save_analysis,
)

# ============================================================================
# MISSING ANALYSIS
# ============================================================================


def find_missing_analysis(folders, verbose=False):
    """Scan all category folders, return list of transcripts without analysis."""
    missing = []

    for category, base_path in folders.items():
        transcripts_folder = os.path.join(base_path, "transcripts")
        analysis_folder = os.path.join(base_path, "analysis")
        if not os.path.exists(transcripts_folder):
            continue

        for f in os.listdir(transcripts_folder):
            if not f.endswith(".md"):
                continue
            analysis_file = f.replace(".md", " - Analysis.md")
            if not os.path.exists(os.path.join(analysis_folder, analysis_file)):
                missing.append((os.path.join(transcripts_folder, f), category))
                if verbose:
                    print(f"  Missing analysis: {category}/{f}", flush=True)

    return missing


def generate_missing_analysis(transcript_path, category, dry_run=False, verbose=False):
    """Generate analysis for a single transcript.

    TODO: Implement once Superwhisper pipeline is complete.
    With Superwhisper, analysis cannot be run in isolation on an existing transcript.
    The original audio file must be re-processed through the full pipeline.
    """
    filename = os.path.basename(transcript_path)

    if dry_run:
        print(f"  [DRY RUN] Would generate analysis for: {category}/{filename}", flush=True)
        return True

    print(
        f"  ⚠️  Cannot generate analysis for {filename} — Superwhisper pipeline not yet implemented.\n"
        f"     Re-process the original audio file to regenerate transcript + analysis together.",
        flush=True,
    )
    return False


# ============================================================================
# RECLASSIFICATION
# ============================================================================


def should_update_filename(filename):
    """Check if filename contains 'Unknown Meeting'."""
    return "Unknown Meeting" in filename


def reclassify_transcript(transcript_path, dry_run=False, verbose=False):
    """Re-classify a transcript. Returns (new_category, new_filename) or None.

    TODO: Implement once Superwhisper pipeline is complete.
    Reclassification requires re-running the full Superwhisper pipeline on the
    original audio file; it cannot be done from the transcript text alone.
    """
    filename = os.path.basename(transcript_path)
    print(
        f"  ⚠️  Cannot reclassify {filename} — Superwhisper pipeline not yet implemented.\n"
        f"     Re-process the original audio file to get updated category + filename.",
        flush=True,
    )
    return None


def extract_timestamp(filename):
    """Extract timestamp prefix from filename (YY-MM-DD HH.MM format)."""
    match = re.match(r"^(\d{2}-\d{2}-\d{2}\s+\d{2}\.\d{2})", filename)
    return match.group(1) if match else None


def move_transcript_and_analysis(old_transcript_path, new_category, new_filename, dry_run=False, verbose=False):
    """Move both transcript and analysis files to new category folder."""
    old_filename = os.path.basename(old_transcript_path)
    timestamp = extract_timestamp(old_filename)
    if not timestamp:
        print(f"  ❌ Could not extract timestamp from: {old_filename}", flush=True)
        return False

    # Build new filename
    if new_filename.endswith(".md"):
        new_full_filename = f"{timestamp} - {new_filename}"
    else:
        new_full_filename = f"{timestamp} - {new_filename}.md"

    dest_folder = FOLDERS.get(new_category, FOLDERS["DEFAULT"])
    transcripts_folder = os.path.join(dest_folder, "transcripts")
    analysis_folder = os.path.join(dest_folder, "analysis")

    new_transcript_path = os.path.join(transcripts_folder, new_full_filename)
    new_analysis_filename = new_full_filename.replace(".md", " - Analysis.md")
    new_analysis_path = os.path.join(analysis_folder, new_analysis_filename)

    # Handle collisions
    if os.path.exists(new_transcript_path):
        print(f"  ⚠️  File already exists at destination: {new_transcript_path}", flush=True)
        base, ext = os.path.splitext(new_full_filename)
        counter = 2
        while os.path.exists(os.path.join(transcripts_folder, f"{base} ({counter}){ext}")):
            counter += 1
        new_full_filename = f"{base} ({counter}){ext}"
        new_transcript_path = os.path.join(transcripts_folder, new_full_filename)
        new_analysis_filename = new_full_filename.replace(".md", " - Analysis.md")
        new_analysis_path = os.path.join(analysis_folder, new_analysis_filename)

    old_analysis_path = old_transcript_path.replace("/transcripts/", "/analysis/").replace(".md", " - Analysis.md")
    has_analysis = os.path.exists(old_analysis_path)

    if dry_run:
        print("  [DRY RUN] Would move:", flush=True)
        print(f"    FROM: {old_transcript_path}", flush=True)
        print(f"    TO:   {new_transcript_path}", flush=True)
        if has_analysis:
            print(f"    AND:  {old_analysis_path}", flush=True)
            print(f"    TO:   {new_analysis_path}", flush=True)
        return True

    try:
        os.makedirs(transcripts_folder, exist_ok=True)
        os.makedirs(analysis_folder, exist_ok=True)

        shutil.move(old_transcript_path, new_transcript_path)
        if verbose:
            print(f"  ✅ Moved transcript: {new_full_filename}", flush=True)

        if has_analysis:
            shutil.move(old_analysis_path, new_analysis_path)
            if verbose:
                print(f"  ✅ Moved analysis: {new_analysis_filename}", flush=True)
        elif verbose:
            print("  ⚠️  No analysis file to move", flush=True)

        return True

    except Exception as e:
        print(f"  ❌ Error moving files: {e}", flush=True)
        try:
            if os.path.exists(new_transcript_path) and not os.path.exists(old_transcript_path):
                shutil.move(new_transcript_path, old_transcript_path)
                print("  🔄 Rolled back transcript move", flush=True)
        except Exception:
            pass
        return False


def scan_default_folder(verbose=False):
    """Scan DEFAULT folder for transcript files."""
    default_folder = FOLDERS["DEFAULT"]
    transcripts_folder = os.path.join(default_folder, "transcripts")

    if not os.path.exists(transcripts_folder):
        print(f"⚠️  DEFAULT transcripts folder not found: {transcripts_folder}", flush=True)
        return []

    transcripts = [os.path.join(transcripts_folder, f) for f in os.listdir(transcripts_folder) if f.endswith(".md")]
    if verbose:
        print(f"Found {len(transcripts)} transcripts in DEFAULT folder", flush=True)
    return transcripts


# ============================================================================
# MAIN
# ============================================================================


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

    print("=" * 60)
    print("🔧 RecordingAnalyser Maintenance")
    print("=" * 60)

    if args.dry_run:
        print("⚠️  DRY RUN MODE - No files will be modified\n")

    # Task 1: Generate Missing Analysis
    if args.generate_missing_analysis:
        print("\n📊 Task 1: Generating Missing Analysis Files")
        print("-" * 60)

        missing = find_missing_analysis(FOLDERS, verbose=args.verbose)

        if not missing:
            print("✅ No missing analysis files found!")
        else:
            print(f"Found {len(missing)} transcripts without analysis\n")
            success_count = 0
            failed_count = 0

            for i, (transcript_path, category) in enumerate(missing, 1):
                filename = os.path.basename(transcript_path)
                print(f"[{i}/{len(missing)}] {category}/{filename}")

                if generate_missing_analysis(transcript_path, category, args.dry_run, args.verbose):
                    success_count += 1
                else:
                    failed_count += 1

                if i < len(missing) and not args.dry_run:
                    print(f"  ⏸️  Pausing {DELAY_BETWEEN_FILES}s between files...", flush=True)
                    time.sleep(DELAY_BETWEEN_FILES)

            print(f"\n📊 Results: ✅ {success_count} success, ❌ {failed_count} failed")

    # Task 2: Reclassify and Move
    if args.reclassify:
        print("\n📁 Task 2: Reclassifying and Moving Files")
        print("-" * 60)

        transcripts = scan_default_folder(verbose=args.verbose)
        unknown_meetings = [t for t in transcripts if should_update_filename(os.path.basename(t))]

        if not unknown_meetings:
            print("✅ No 'Unknown Meeting' files found in DEFAULT folder!")
        else:
            print(f"Found {len(unknown_meetings)} 'Unknown Meeting' files\n")
            moved_count = 0
            skipped_count = 0
            failed_count = 0

            for i, transcript_path in enumerate(unknown_meetings, 1):
                filename = os.path.basename(transcript_path)
                print(f"[{i}/{len(unknown_meetings)}] {filename}")

                result = reclassify_transcript(transcript_path, args.dry_run, args.verbose)
                if result is None:
                    print("  ❌ Classification failed")
                    failed_count += 1
                    continue

                new_category, new_filename = result
                if new_category == "DEFAULT":
                    print("  ⚠️  Still classified as DEFAULT, skipping move")
                    skipped_count += 1
                    continue

                if move_transcript_and_analysis(
                    transcript_path, new_category, new_filename, args.dry_run, args.verbose
                ):
                    moved_count += 1
                else:
                    failed_count += 1

            print(f"\n📊 Results: ✅ {moved_count} moved, ⚠️ {skipped_count} skipped, ❌ {failed_count} failed")

    print(f"\n{'=' * 60}")
    print("✅ Done!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
