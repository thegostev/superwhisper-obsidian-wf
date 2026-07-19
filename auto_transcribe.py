"""Auto-transcription daemon: continuously scans for new audio files and processes them.

Runs as a launchd service. See README.md for setup instructions.
"""

import itertools, sys, time
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


def discover_audio_files(watch_folder, state, transcript_index):
    """Find unprocessed .m4a files; return (path, timestamp) tuples oldest-first, capped at MAX_FILES_PER_CYCLE."""
    audio_files, state_dirty = [], False

    for folder_path in discover_recent_folders(watch_folder, SCAN_DAYS_BACK):
        for fp in Path(folder_path).glob("*.m4a"):
            if ".icloud" in (file_path := str(fp)) or ".tmp" in file_path or fp.name.startswith("."):
                continue

            if state.get("processed", {}).get(file_path, {}).get("status") in ("complete", "failed_permanent"):
                continue

            timestamp = get_audio_timestamp(file_path)
            if (existing := transcript_index.get(timestamp.strftime(TIMESTAMP_FORMAT))) and existing.get("output_path"):
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

            if is_file_stable(file_path, wait_seconds=1):
                audio_files.append((file_path, timestamp))

    if state_dirty:
        save_state(state)

    if len(audio_files) > MAX_FILES_PER_CYCLE:
        print(f"   📋 {len(audio_files)} files found, processing {MAX_FILES_PER_CYCLE} this cycle ({len(audio_files) - MAX_FILES_PER_CYCLE} deferred to next cycle)", flush=True)
    return sorted(audio_files, key=lambda x: x[1])[:MAX_FILES_PER_CYCLE]


def run_scan_cycle(state, transcript_index, cycle_number):
    new_files = discover_audio_files(WATCH_FOLDER, state, transcript_index)

    if not new_files:
        if cycle_number % IDLE_HEARTBEAT_EVERY_N_CYCLES == 0:
            print(f"[Cycle {cycle_number}] {datetime.now().strftime('%H:%M:%S')} - No new files", flush=True)
        return 0

    print(f"\n[Cycle {cycle_number}] {datetime.now().strftime('%H:%M:%S')} - Found {len(new_files)} file(s) to process", flush=True)

    success_count = 0
    for i, (audio_path, timestamp) in enumerate(new_files, 1):
        print(f"\n📂 [{i}/{len(new_files)}] {Path(audio_path).parent.name}/{Path(audio_path).name}", flush=True)

        success, category = process_audio(audio_path, timestamp, state)

        success_count += success
        if success:
            transcript_index[timestamp.strftime(TIMESTAMP_FORMAT)] = {"category": category}

        if i < len(new_files):
            print(f"   ⏸️  Pausing {DELAY_BETWEEN_FILES}s before next file...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES)

    print(f"\n{'=' * 50}\nCycle {cycle_number} complete: {success_count} succeeded, {len(new_files) - success_count} failed\n{'=' * 50}", flush=True)

    return success_count


def main():
    print(f"{'=' * 60}\n🎙️  Auto-Transcription Service (Superwhisper)\n{'=' * 60}\n"
          f"Watch folder: {WATCH_FOLDER}\nScanning last {SCAN_DAYS_BACK} days of recordings\nScan interval: {SCAN_INTERVAL}s | Delay between files: {DELAY_BETWEEN_FILES}s\n"
          f"Max retries: {MAX_RETRIES} per file | Max files/cycle: {MAX_FILES_PER_CYCLE}\nSuperwhisper timeout: {SUPERWHISPER_TIMEOUT}s | State file: {STATE_FILE}\n{'=' * 60}",
          flush=True)

    state = load_state()
    statuses = [v.get("status") for v in state.get("processed", {}).values()]
    print(f"\n📚 Loaded state: {statuses.count('complete')} completed, {statuses.count('failed_permanent')} permanently failed\n📚 Building transcript index...", flush=True)
    transcript_index = build_transcript_index(FOLDERS)
    print(f"Found {len(transcript_index)} existing transcripts\n\n🔄 Starting scan loop (every {SCAN_INTERVAL}s)...\n", flush=True)

    for cycle in itertools.count(1):
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
