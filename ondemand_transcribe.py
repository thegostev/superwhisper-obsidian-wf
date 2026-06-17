"""On-demand batch processing CLI for catchup after downtime.

Usage:
    python ondemand_transcribe.py --catchup --dry-run       # Preview last 7 days
    python ondemand_transcribe.py --catchup 14              # Process last 14 days
"""

import argparse, sys, time
from pathlib import Path

from config import DELAY_BETWEEN_FILES, FOLDERS, WATCH_FOLDER
from pipeline import (
    TIMESTAMP_FORMAT,
    FatalAPIError,
    build_transcript_index,
    discover_recent_folders,
    get_audio_timestamp,
    is_file_stable,
    load_state,
    process_audio,
)

# ============================================================================
# DISCOVERY (on-demand specific: explicit subfolder list, no state filtering)
# ============================================================================


def discover_audio_files(watch_folder, scan_subfolders, verbose=False):
    """Scan specific subfolders for .m4a files. Returns list of (path, timestamp) tuples."""
    if not scan_subfolders:
        raise ValueError("scan_subfolders must contain at least one subfolder. Use --catchup to auto-discover date folders.")

    audio_files = []
    nonexistent = []

    for subfolder in scan_subfolders:
        subfolder_path = Path(watch_folder) / subfolder

        if not subfolder_path.is_dir():
            nonexistent.append(subfolder)
            if verbose:
                print(f"⚠️  Warning: Not found: {subfolder_path}", flush=True)
            continue

        for fp in subfolder_path.glob("*.m4a"):
            file_path = str(fp)  # str boundary: is_file_stable, get_audio_timestamp expect str
            if ".icloud" in file_path or ".tmp" in file_path or fp.name.startswith("."):
                continue
            if not is_file_stable(file_path, wait_seconds=1):
                continue
            audio_files.append((file_path, get_audio_timestamp(file_path)))

    if nonexistent:
        print(f"\n⚠️  {len(nonexistent)} subfolder(s) not found: {', '.join(nonexistent)}", flush=True)

    return sorted(audio_files, key=lambda x: x[1])


def check_processing_status(audio_file, timestamp, transcript_index):
    """Check if audio file has been processed.

    Returns: (status, category, output_path, _unused)
    where status is "complete" | "unprocessed"

    Single-file output: presence in the index implies the analysis file exists.
    """
    if (entry := transcript_index.get(timestamp.strftime(TIMESTAMP_FORMAT))):
        return ("complete", entry["category"], entry["output_path"], None)
    return ("unprocessed", None, None, None)


# ============================================================================
# BATCH PROCESSING
# ============================================================================


def process_batch(unprocessed_files, state, dry_run=False):
    """Process list of audio files. Returns {"success": int, "failed": list}."""
    success_count = 0
    failed_files = []
    total = len(unprocessed_files)

    for i, (audio_path, timestamp) in enumerate(unprocessed_files, 1):
        filename = Path(audio_path).name
        print(f"\n[{i}/{total}] Processing {filename}...", flush=True)

        if dry_run:
            print(f"  📁 Path: {audio_path}\n  🕐 Timestamp: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
                  "  ⚠️  DRY RUN - Would process this file", flush=True)
            success_count += 1
            continue

        try:
            success, category = process_audio(audio_path, timestamp, state)
            if success:
                success_count += 1
            else:
                failed_files.append(audio_path)
        except FatalAPIError as e:
            print(f"\n🛑 FATAL: {e}", flush=True)
            failed_files.append(audio_path)
            break
        except Exception as e:
            print(f"❌ Exception processing {filename}: {e}", flush=True)
            failed_files.append(audio_path)

        if i < total:
            print(f"   ⏸️  Pausing {DELAY_BETWEEN_FILES}s before next file...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES)

    return {"success": success_count, "failed": failed_files, "total": total}


def reprocess_analysis_only(transcript_only_files, dry_run=False):
    """Regenerate analysis for files with transcripts but no analysis.

    TODO: With Superwhisper, transcription and analysis are a single pass.
    Re-running analysis in isolation is not supported — re-process the original
    audio file through the full pipeline instead (use --catchup without --reprocess-partial).
    """
    print(
        "⚠️  --reprocess-partial is not supported with the Superwhisper pipeline.\n"
        "   Superwhisper combines transcription + analysis in one pass.\n"
        "   To regenerate analysis, re-process the original audio file via --catchup.",
        flush=True,
    )
    return {"success": 0, "failed": [f for f, *_ in transcript_only_files], "total": len(transcript_only_files)}


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Process unprocessed audio recordings on-demand",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ondemand_transcribe.py --catchup --dry-run          # Preview last 7 days
  python ondemand_transcribe.py --catchup 14                 # Process last 14 days
        """,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be processed without actually processing"
    )
    parser.add_argument(
        "--reprocess-partial",
        action="store_true",
        help="(Not supported with Superwhisper — re-process audio files instead)",
    )
    parser.add_argument("--verbose", action="store_true", help="Show detailed progress")
    parser.add_argument(
        "--catchup",
        type=int,
        metavar="DAYS",
        nargs="?",
        const=7,
        help="Auto-discover date folders from last N days (default: 7)",
    )

    args = parser.parse_args()

    if args.catchup is None:
        parser.print_help()
        print("\n⚠️  Please specify --catchup [DAYS] to auto-discover folders.")
        sys.exit(1)

    print(f"{'=' * 60}\n📼 On-Demand Audio Transcription & Analysis (Superwhisper)\n{'=' * 60}")

    # Discover folders and audio files
    scan_subfolders = [Path(p).name for p in discover_recent_folders(WATCH_FOLDER, days_back=args.catchup)]
    print(f"\n🔄 Catchup mode: scanning last {args.catchup} days", flush=True)
    print(f"📁 Target subfolders: {', '.join(scan_subfolders)}", flush=True)

    if not scan_subfolders:
        print("No date folders found in the specified range.", flush=True)
        return

    all_audio_files = discover_audio_files(WATCH_FOLDER, scan_subfolders, verbose=args.verbose)
    print(f"Found {len(all_audio_files)} audio files", flush=True)

    # Build index and load state
    print("\n📚 Building transcript index...", flush=True)
    transcript_index = build_transcript_index(FOLDERS)
    print(f"Found {len(transcript_index)} existing transcripts", flush=True)

    state = load_state()

    # Check processing status
    print("\n🔎 Checking processing status...", flush=True)
    unprocessed = []
    transcript_only = []
    complete = 0

    for audio_path, timestamp in all_audio_files:
        status, category, transcript_path, analysis_path = check_processing_status(
            audio_path, timestamp, transcript_index
        )
        if status == "unprocessed":
            unprocessed.append((audio_path, timestamp))
        elif status == "transcript_only":
            transcript_only.append((audio_path, timestamp, category, transcript_path))
        else:
            complete += 1

    # Summary
    print(f"\n{'=' * 60}\n📊 Status Summary\n{'=' * 60}")
    print(f"   ✅ Complete (transcript + analysis):  {complete}")
    print(f"   📝 Transcript only (missing analysis): {len(transcript_only)}")
    print(f"   🆕 Unprocessed:                        {len(unprocessed)}")
    print(f"   📁 Total audio files:                  {len(all_audio_files)}\n{'=' * 60}")

    if not unprocessed and not transcript_only:
        print("\n✨ All files are fully processed!")
        return

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No files will be processed")

    # Process unprocessed files
    if unprocessed:
        print(f"\n🚀 Processing {len(unprocessed)} unprocessed files...\n{'-' * 60}")
        results = process_batch(unprocessed, state, dry_run=args.dry_run)
        print(f"\n{'-' * 60}\n  ✅ Success: {results['success']}\n  ❌ Failed:  {len(results['failed'])}")

    # Reprocess partial (not supported)
    if transcript_only and args.reprocess_partial:
        reprocess_analysis_only(transcript_only, dry_run=args.dry_run)
    elif transcript_only:
        print(f"\n💡 Tip: {len(transcript_only)} file(s) have transcripts but no analysis.\n   Re-process the original audio files to regenerate analysis via Superwhisper.")

    print(f"\n{'=' * 60}\n✅ Done!\n{'=' * 60}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user. Exiting...")
        sys.exit(0)
