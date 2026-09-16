"""One-shot full redo of the 2026-09-01..09 meeting window (ops tool, 2026-09-13).

24 recordings: 20 already complete (notes replaced with fresh runs) + 4
failed_permanent (09-02 09-33-18, 09-08 12-43-57, 09-08 14-37-23, 09-08
19-10-13 — empty-stub races plus one contract refusal). Deliberately
file-list based, sequential, with warm-up gaps so Superwhisper is never
handed a burst of file-opens (R2/R7 debt: no idle-gate, no sequencing).

Run ONLY with the daemon stopped (launchctl bootout com.alex.transcriber,
watchdog pause sentinel touched first) to avoid double-processing.
Restart with launchctl bootstrap and remove the sentinel afterwards.

Old notes are moved aside per file (timestamp-prefix match, reversible,
no deletion) just before that file's first attempt, so a stale note never
coexists with its fresh replacement. Notes for files that ultimately fail
stay in the backup dir — restore manually if you want the old version back.

Usage: venv/bin/python3 redrive_sept_1_9.py [--dry-run]
"""

import sys
import time
from datetime import datetime
from pathlib import Path

from pipeline import (
    FatalAPIError,
    get_audio_timestamp,
    load_state,
    process_audio,
    save_state,
)

JPR = Path.home() / "Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents"

DAYS = [
    "2026-09-01",
    "2026-09-02",
    "2026-09-03",
    "2026-09-04",
    "2026-09-05",
    "2026-09-06",
    "2026-09-07",
    "2026-09-08",
    "2026-09-09",
]

REDRIVE = sorted(f for day in DAYS for f in (JPR / day).glob("*.m4a"))

# All vault folders a redone note could have landed in (FOLDERS values).
NOTES_DIRS = [
    Path.home() / "Documents/Obsidian/Musikere/raw/2 - Møtenotater",
    Path.home() / "Documents/Obsidian/Minnesotere/raw/4 - Møtenotater",
    Path.home() / "Documents/Obsidian/Personlig/raw/6 - Møtenotater",
    Path.home() / "Documents/Obsidian/Jobbjakt/raw/3 - Intervjuer",
]

BACKUP_ROOT = Path.home() / "Documents/Obsidian/Personlig/raw/6 - Møtenotater/.replaced-2026-09-13-sept1-9"

WARMUP_SECONDS = 90  # gap between handoffs / after a stub failure
MAX_ATTEMPTS = 4

dry_run = "--dry-run" in sys.argv


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def note_prefix(audio: Path) -> str:
    """Note-name timestamp prefix for an audio file: '26-09-0D HH.MM'."""
    return get_audio_timestamp(str(audio)).strftime("%y-%m-%d %H.%M")


def move_old_notes_aside(audio: Path) -> list[str]:
    """Move stale notes for this recording's timestamp to the backup dir."""
    moved = []
    prefix = note_prefix(audio)
    for notes_dir in NOTES_DIRS:
        backup = BACKUP_ROOT / notes_dir.name
        for note in notes_dir.glob(f"{prefix} - *.md"):
            if dry_run:
                moved.append(f"WOULD move aside: {note.name}")
                continue
            backup.mkdir(parents=True, exist_ok=True)
            note.rename(backup / note.name)
            moved.append(f"moved aside: {note.name}")
    return moved


report: dict[str, str] = {}

for audio in REDRIVE:
    path = str(audio)
    name = f"{audio.parent.name}/{audio.name}"
    prefix = note_prefix(audio)
    log(f"=== {name} (note prefix {prefix}) ===")

    # Dry-run report: current state + which notes would be replaced.
    state = load_state()
    entry = state.get("processed", {}).get(path, {})
    if dry_run:
        log(f"  state: {entry.get('status', 'absent')} / {entry.get('category', '-')}")
        for line in move_old_notes_aside(audio):
            log(f"  {line}")
        continue

    if entry.get("status") == "complete":
        log("FORCE re-processing (replacing prior note)")
        for line in move_old_notes_aside(audio):
            log(f"  📦 {line}")

    ok = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"PROCESSING (attempt {attempt}/{MAX_ATTEMPTS})")
        state = load_state()
        state["processed"].pop(path, None)
        save_state(state)
        try:
            ok, category = process_audio(path, get_audio_timestamp(path), state)
        except FatalAPIError as e:
            log(f"  🛑 FATAL: {e}")
            break
        except Exception as e:
            log(f"  ❌ Exception: {e}")
            ok = False
        if ok:
            log(f"  ✅ OK category={category}")
            report[name] = f"OK ({category})"
            break
        entry = load_state().get("processed", {}).get(path, {})
        log(f"  ⏳ attempt {attempt} failed (state: {entry.get('status', 'none')}); warm-up {WARMUP_SECONDS}s")
        time.sleep(WARMUP_SECONDS)

    if not ok:
        log(f"❌ GIVING UP on {name} after {MAX_ATTEMPTS} attempts")
        report[name] = "FAILED"
    time.sleep(WARMUP_SECONDS)  # warm-up gap between files

log("DONE")
print("\n===== SUMMARY =====", flush=True)
for name, status in report.items():
    print(f"  {status:<30} {name}", flush=True)
