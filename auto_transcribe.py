"""Auto-transcription daemon: continuously scans for new audio files and processes them.

Runs as a launchd service. See README.md for setup instructions.
"""

import sys, time
from datetime import datetime
from pathlib import Path

from config import (
    DELAY_BETWEEN_FILES,
    FOLDERS,
    MAX_FILES_PER_CYCLE,
    MAX_RETRIES,
    SCAN_DAYS_BACK,
    SCAN_INTERVAL,
    STATE_FILE,
    SUPERWHISPER_TIMEOUT,
    WATCH_FOLDER,
)
from pipeline import (
    TIMESTAMP_FORMAT,
    FatalAPIError,
    build_transcript_index,
    discover_recent_folders,
    get_audio_timestamp,
    is_file_stable,
    load_state,
    process_audio,
    save_state,
)

# Emit a heartbeat log line every Nth idle cycle (keeps logs quiet but alive).
IDLE_HEARTBEAT_EVERY_N_CYCLES = 10


# ============================================================================
# AUDIO DISCOVERY (daemon-specific: state filtering, index dedup, per-cycle cap)
# ============================================================================


def discover_audio_files(watch_folder, state, transcript_index):
    """Find unprocessed .m4a files in recent date folders.

    Returns list of (audio_path, timestamp) tuples, oldest first.
    Capped at MAX_FILES_PER_CYCLE to prevent stampedes.
    """
    audio_files = []
    state_dirty = False

    for folder_path in discover_recent_folders(watch_folder, SCAN_DAYS_BACK):
        for fp in Path(folder_path).glob("*.m4a"):
            file_path = str(fp)  # str boundary: state dict key

            # Skip iCloud placeholders, temp files, hidden files
            if ".icloud" in file_path or ".tmp" in file_path or fp.name.startswith("."):
                continue

            # Skip if already processed or permanently failed
            if state.get("processed", {}).get(file_path, {}).get("status") in ("complete", "failed_permanent"):
                continue

            # Check transcript index before stability check
            timestamp = get_audio_timestamp(file_path)
            existing = transcript_index.get(timestamp.strftime(TIMESTAMP_FORMAT))
            if existing and existing.get("output_path"):
                state.setdefault("processed", {})[file_path] = {
                    "status": "complete",
                    "category": existing["category"],
                    "timestamp": timestamp.isoformat(),
                    "processed_at": datetime.now().isoformat(),
                    "attempts": 0,
                    "note": "found in transcript index",
                }
                state_dirty = True
                continue

            # Check file stability (only for genuinely new files)
            if not is_file_stable(file_path, wait_seconds=1):
                continue

            audio_files.append((file_path, timestamp))

    if state_dirty:
        save_state(state)

    audio_files.sort(key=lambda x: x[1])

    if len(audio_files) > MAX_FILES_PER_CYCLE:
        print(f"   📋 {len(audio_files)} files found, processing {MAX_FILES_PER_CYCLE} "
              f"this cycle ({len(audio_files) - MAX_FILES_PER_CYCLE} deferred to next cycle)", flush=True)
        return audio_files[:MAX_FILES_PER_CYCLE]
    return audio_files


# ============================================================================
# SCAN CYCLE & MAIN LOOP
# ============================================================================


def run_scan_cycle(state, transcript_index, cycle_number):
    """Run one scan-and-process cycle. Returns count of files processed."""
    new_files = discover_audio_files(WATCH_FOLDER, state, transcript_index)

    if not new_files:
        if cycle_number % IDLE_HEARTBEAT_EVERY_N_CYCLES == 0:
            print(f"[Cycle {cycle_number}] {datetime.now().strftime('%H:%M:%S')} - No new files", flush=True)
        return 0

    print(f"\n[Cycle {cycle_number}] {datetime.now().strftime('%H:%M:%S')} - Found {len(new_files)} file(s) to process", flush=True)

    success_count = 0
    fail_count = 0

    for i, (audio_path, timestamp) in enumerate(new_files, 1):
        p = Path(audio_path)
        print(f"\n📂 [{i}/{len(new_files)}] {p.parent.name}/{p.name}", flush=True)

        success, category = process_audio(audio_path, timestamp, state)

        if success:
            success_count += 1
            transcript_index[timestamp.strftime(TIMESTAMP_FORMAT)] = {"category": category}
        else:
            fail_count += 1

        if i < len(new_files):
            print(f"   ⏸️  Pausing {DELAY_BETWEEN_FILES}s before next file...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES)

    print(f"\n{'=' * 50}\nCycle {cycle_number} complete: {success_count} succeeded, {fail_count} failed\n{'=' * 50}", flush=True)

    return success_count


def main():
    print(f"{'=' * 60}\n🎙️  Auto-Transcription Service (Superwhisper)\n{'=' * 60}", flush=True)
    print(f"Watch folder: {WATCH_FOLDER}", flush=True)
    print(f"Scanning last {SCAN_DAYS_BACK} days of recordings", flush=True)
    print(f"Scan interval: {SCAN_INTERVAL}s | Delay between files: {DELAY_BETWEEN_FILES}s", flush=True)
    print(f"Max retries: {MAX_RETRIES} per file | Max files/cycle: {MAX_FILES_PER_CYCLE}", flush=True)
    print(f"Superwhisper timeout: {SUPERWHISPER_TIMEOUT}s | State file: {STATE_FILE}\n{'=' * 60}", flush=True)

    state = load_state()
    statuses = [v.get("status") for v in state.get("processed", {}).values()]
    print(f"\n📚 Loaded state: {statuses.count('complete')} completed, {statuses.count('failed_permanent')} permanently failed", flush=True)

    print("📚 Building transcript index...", flush=True)
    transcript_index = build_transcript_index(FOLDERS)
    print(f"Found {len(transcript_index)} existing transcripts", flush=True)

    print(f"\n🔄 Starting scan loop (every {SCAN_INTERVAL}s)...\n", flush=True)

    cycle = 0
    while True:
        cycle += 1
        try:
            run_scan_cycle(state, transcript_index, cycle)
        except FatalAPIError as e:
            print(f"\n🛑 FATAL: {e}\n   Unrecoverable error. Service stopping.", flush=True)
            sys.exit(1)
        except Exception as e:
            print(f"\n❌ Scan cycle {cycle} error: {e}\n   Continuing to next cycle...", flush=True)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Stopping auto-transcription service...", flush=True)
        sys.exit(0)
