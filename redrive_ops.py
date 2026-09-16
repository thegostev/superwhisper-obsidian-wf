"""One-shot re-drive of specified audio files through the Superwhisper pipeline.

Temporary ops tool (2026-09-08): re-processes 3 long recordings that were lost to
the empty-stub race. Deliberately file-list based (not --catchup window based) so
the 4 deliberately-triaged short clips in failed_permanent are NOT reprocessed.

Usage: venv/bin/python3 redrive_ops.py [--dry-run]
"""

import sys
from datetime import datetime

from pipeline import get_audio_timestamp, load_state, process_audio

REDRIVE = [
    "/Users/harald/Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents/2026-09-05/18-01-23.m4a",
    "/Users/harald/Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents/2026-09-05/20-36-16.m4a",
]

dry_run = "--dry-run" in sys.argv

state = load_state()
for path in REDRIVE:
    if state.get("processed", {}).get(path, {}).get("status") == "complete":
        print(f"SKIP (complete): {path.rsplit('/', 1)[-1]}", flush=True)
        continue
    ts = get_audio_timestamp(path)
    status = "WOULD PROCESS" if dry_run else "PROCESSING"
    print(f"[{datetime.now():%H:%M:%S}] {status}: {path.rsplit('/', 2)[-2:]} start={ts:%Y-%m-%d %H:%M:%S}", flush=True)
    if dry_run:
        continue
    ok, category = process_audio(path, ts, state := load_state())
    print(f"[{datetime.now():%H:%M:%S}]   -> {'OK' if ok else 'FAILED'} category={category}", flush=True)

print("DONE", flush=True)
