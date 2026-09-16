"""One-shot sequential re-drive of the 2026-09-10..13 backlog (ops tool, 2026-09-13).

7 files: 4 failed_permanent from Sep 10 + 2 from Sep 11 (empty-stub race, 3
attempts burned), plus 22-54-36 whose saved note was renamed "empty" by the
user (0-byte garbage llmResult). Deliberately file-list based, sequential, with
warm-up gaps between attempts so Superwhisper is never handed a burst of
file-opens (R2/R7 debt: no idle-gate, no sequencing in the daemon).

Run ONLY with the daemon stopped (launchctl bootout com.alex.transcriber) to
avoid double-processing. Restart with launchctl bootstrap afterwards.

Usage: venv/bin/python3 redrive_sept_10_13.py [--dry-run]
"""

import sys
import time
from datetime import datetime
from pathlib import Path

from pipeline import FatalAPIError, get_audio_timestamp, load_state, process_audio, save_state

JPR = Path.home() / "Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents"

REDRIVE = [
    JPR / "2026-09-10/12-06-15.m4a",
    JPR / "2026-09-10/12-45-28.m4a",
    JPR / "2026-09-10/14-00-12.m4a",
    JPR / "2026-09-11/06-29-08.m4a",
    JPR / "2026-09-11/14-55-41.m4a",
    JPR / "2026-09-12/14-27-08.m4a",
]

# 2026-09-13 second pass: replace ALL pre-existing notes in the window with
# fresh runs. The 7 files from the first pass today are already fresh products;
# these 6 predate the fresh run. Old note files are moved aside (see
# replace_existing_notes) so the fresh note does not coexist with the stale one.
REPLACE_NOTES_DIR = Path.home() / "Documents/Obsidian/Personlig/raw/6 - Møtenotater"

OLD_NOTES = [
    REPLACE_NOTES_DIR / "26-09-10 12.06 - Ingresses - Cluster Instances, Microservices.md",
    # 12-45-28 and 14-00-12 routed to the Minnesotere vault, not Personlig
    Path.home()
    / "Documents/Obsidian/Minnesotere/raw/4 - Møtenotater/26-09-10 12.45 - Delta - Hiring, Migrations, Security.md",
    Path.home()
    / "Documents/Obsidian/Minnesotere/raw/4 - Møtenotater/26-09-10 14.00 - New Certificates - Jenkins Migration, MCP Testing, PTO.md",
    REPLACE_NOTES_DIR / "26-09-11 07.29 - Social Settings - Fear, Language, Brazil Tour.md",
    REPLACE_NOTES_DIR / "26-09-11 14.55 - Renovation - Designer Apartment Costs.md",
    REPLACE_NOTES_DIR / "26-09-12 14.27 - Apartment Options - Income, Location, Renovation.md",
    # 0-byte garbage leftover from the 2026-09-12 bad llmResult; its fresh
    # replacement ("26-09-11 22.54 - Торг за жилье…") was already saved today.
    REPLACE_NOTES_DIR / "26-09-11 22.54 - empty.md",
]

BACKUP_DIR = Path.home() / "Documents/Obsidian/Personlig/raw/6 - Møtenotater/.replaced-2026-09-13"

WARMUP_SECONDS = 90  # gap after a stub failure before retrying the handoff
MAX_ATTEMPTS = 4

dry_run = "--dry-run" in sys.argv


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def move_old_notes_aside() -> None:
    """Move stale pre-run notes to a hidden backup dir (reversible, no deletion)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for note in OLD_NOTES:
        if note.exists():
            note.rename(BACKUP_DIR / note.name)
            log(f"  📦 moved aside: {note.name}")
        else:
            log(f"  ⚠️  old note not found: {note.name}")


for audio in REDRIVE:
    path = str(audio)
    name = audio.name
    state = load_state()
    if state.get("processed", {}).get(path, {}).get("status") == "complete":
        log(f"FORCE re-processing (replacing prior note): {name}")
        if not dry_run:
            move_old_notes_aside()

    ts = get_audio_timestamp(path)
    if dry_run:
        log(f"WOULD PROCESS: 2026-09-XX/{name} start={ts:%Y-%m-%d %H:%M:%S}")
        continue

    ok = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"PROCESSING (attempt {attempt}/{MAX_ATTEMPTS}): {name}")
        state = load_state()
        state["processed"].pop(path, None)
        save_state(state)
        try:
            ok, category = process_audio(path, ts, state)
        except FatalAPIError as e:
            log(f"  🛑 FATAL: {e}")
            break
        except Exception as e:
            log(f"  ❌ Exception: {e}")
            ok = False
        if ok:
            log(f"  ✅ OK category={category}")
            break
        entry = load_state().get("processed", {}).get(path, {})
        log(f"  ⏳ attempt {attempt} failed (state: {entry.get('status', 'none')}); warm-up {WARMUP_SECONDS}s")
        time.sleep(WARMUP_SECONDS)

    if not ok:
        log(f"❌ GIVING UP on {name} after {MAX_ATTEMPTS} attempts")
    time.sleep(WARMUP_SECONDS)  # warm-up gap between files

log("DONE")
