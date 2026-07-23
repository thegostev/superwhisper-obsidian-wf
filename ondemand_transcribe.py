"""On-demand batch processing CLI for catchup after downtime.

Usage:
    python ondemand_transcribe.py --catchup --dry-run       # Preview last 7 days
    python ondemand_transcribe.py --catchup 14              # Process last 14 days
"""

import argparse
import sys
import time
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
    recover_failed_permanent,
)


def discover_audio_files(watch_folder, scan_subfolders, verbose=False):
    """Scan specific subfolders for .m4a files. Returns list of (path, timestamp) tuples."""
    if not scan_subfolders:
        raise ValueError("scan_subfolders must contain at least one subfolder. Use --catchup to auto-discover date folders.")

    audio_files, nonexistent = [], []

    for subfolder in scan_subfolders:
        if not (subfolder_path := Path(watch_folder) / subfolder).is_dir():
            nonexistent.append(subfolder)
            if verbose:
                print(f"⚠️  Warning: Not found: {subfolder_path}", flush=True)
            continue

        for fp in subfolder_path.glob("*.m4a"):
            if ".icloud" in (file_path := str(fp)) or ".tmp" in file_path or fp.name.startswith(".") or not is_file_stable(file_path, wait_seconds=1):
                continue
            audio_files.append((file_path, get_audio_timestamp(file_path)))

    if nonexistent:
        print(f"\n⚠️  {len(nonexistent)} subfolder(s) not found: {', '.join(nonexistent)}", flush=True)

    return sorted(audio_files, key=lambda x: x[1])


def process_batch(unprocessed_files, state, dry_run=False):
    """Process list of audio files. Returns {"success": int, "failed": list}."""
    success_count, failed_files, total = 0, [], len(unprocessed_files)
    for i, (audio_path, timestamp) in enumerate(unprocessed_files, 1):
        print(f"\n[{i}/{total}] Processing {Path(audio_path).name}...", flush=True)

        if dry_run:
            print(f"  📁 Path: {audio_path}\n  🕐 Timestamp: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n  ⚠️  DRY RUN - Would process this file", flush=True)
            success_count += 1
            continue

        try:
            success_count += (success := process_audio(audio_path, timestamp, state)[0])
            if not success:
                failed_files.append(audio_path)
        except FatalAPIError as e:
            print(f"\n🛑 FATAL: {e}", flush=True)
            failed_files.append(audio_path)
            break
        except Exception as e:
            print(f"❌ Exception processing {Path(audio_path).name}: {e}", flush=True)
            failed_files.append(audio_path)

        if i < total:
            print(f"   ⏸️  Pausing {DELAY_BETWEEN_FILES}s before next file...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES)

    return {"success": success_count, "failed": failed_files, "total": total}


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
    parser.add_argument(
        "--recover-failed",
        action="store_true",
        help="Scan Superwhisper recordings for late-arriving llmResult to salvage failed_permanent entries, then exit.",
    )

    args = parser.parse_args()

    if args.catchup is None and not args.recover_failed:
        parser.print_help()
        print("\n⚠️  Please specify --catchup [DAYS] or --recover-failed.")
        sys.exit(1)

    print(f"{'=' * 60}\n📼 On-Demand Audio Transcription & Analysis (Superwhisper)\n{'=' * 60}")

    if args.recover_failed:
        state = load_state()
        failed = sum(1 for v in state.get("processed", {}).values() if v.get("status") == "failed_permanent")
        print(f"\n♻️  Recovery scan: {failed} failed_permanent entries in state", flush=True)
        if not args.dry_run:
            recovered = recover_failed_permanent(state)
            print(f"\n{'=' * 60}\n   ♻️  Recovered: {recovered}\n{'=' * 60}")
        else:
            print("⚠️  DRY RUN — would run recovery scan; no files will be modified")
        return

    scan_subfolders = [Path(p).name for p in discover_recent_folders(WATCH_FOLDER, days_back=args.catchup)]
    print(f"\n🔄 Catchup mode: scanning last {args.catchup} days\n📁 Target subfolders: {', '.join(scan_subfolders)}", flush=True)

    if not scan_subfolders:
        print("No date folders found in the specified range.", flush=True)
        return

    all_audio_files = discover_audio_files(WATCH_FOLDER, scan_subfolders, verbose=args.verbose)
    print(f"Found {len(all_audio_files)} audio files\n\n📚 Building transcript index...", flush=True)
    transcript_index = build_transcript_index(FOLDERS)
    print(f"Found {len(transcript_index)} existing transcripts", flush=True)

    state = load_state()

    print("\n🔎 Checking processing status...", flush=True)
    unprocessed = [(ap, ts) for ap, ts in all_audio_files if not transcript_index.get(ts.strftime(TIMESTAMP_FORMAT))]

    print(f"\n{'=' * 60}\n📊 Status Summary\n{'=' * 60}"
          f"\n   ✅ Complete (transcript + analysis):  {len(all_audio_files) - len(unprocessed)}\n   📝 Transcript only (missing analysis): 0"
          f"\n   🆕 Unprocessed:                        {len(unprocessed)}\n   📁 Total audio files:                  {len(all_audio_files)}\n{'=' * 60}")

    if not unprocessed:
        print("\n✨ All files are fully processed!")
        return

    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No files will be processed")

    print(f"\n🚀 Processing {len(unprocessed)} unprocessed files...\n{'-' * 60}")
    results = process_batch(unprocessed, state, dry_run=args.dry_run)
    print(f"\n{'-' * 60}\n  ✅ Success: {results['success']}\n  ❌ Failed:  {len(results['failed'])}\n\n{'=' * 60}\n✅ Done!\n{'=' * 60}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user. Exiting...")
        sys.exit(0)
