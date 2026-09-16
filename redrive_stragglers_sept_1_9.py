"""Second-pass redrive for stragglers from redrive_sept_1_9.py (ops tool, 2026-09-13).

09-33-18 and 12-43-57 both failed 4/4 attempts in the main pass with the
same signature: transcription completes, LLM pass never starts. Main-pass
files before/after them succeeded, so it is file- or session-specific, not
a busy race. Strategy here: quit+relaunch Superwhisper between attempts,
longer warm-ups, and a WAV transcode fallback (afconvert) on the final
attempt in case the m4a container itself trips Superwhisper's LLM stage.

Run ONLY with the daemon stopped (it still is, from the main redrive).

Usage: venv/bin/python3 redrive_stragglers_sept_1_9.py [--dry-run]
"""

import subprocess
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

STRAGGLERS = [
    JPR / "2026-09-02/09-33-18.m4a",
    JPR / "2026-09-08/12-43-57.m4a",
]

WARMUP_SECONDS = 120
MAX_ATTEMPTS = 3

dry_run = "--dry-run" in sys.argv


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def restart_superwhisper() -> None:
    log("  🔄 quitting Superwhisper")
    subprocess.run(["osascript", "-e", 'tell application "Superwhisper" to quit'], capture_output=True, timeout=30)
    time.sleep(10)
    subprocess.run(["open", "-a", "Superwhisper"], check=True)
    time.sleep(20)
    log("  🔄 Superwhisper relaunched")


def transcode_to_wav(audio: Path) -> Path | None:
    out = Path("/tmp") / f"{audio.stem}.wav"
    cmd = ["afconvert", "-f", "WAVE", "-d", "LEI16@22050", "-c", "1", str(audio), str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        log(f"  🎧 transcoded to {out} ({out.stat().st_size} bytes)")
        return out
    except Exception as e:
        log(f"  ⚠️  transcode failed: {e}")
        return None


for audio in STRAGGLERS:
    path = str(audio)
    log(f"=== {audio.parent.name}/{audio.name} ===")
    if dry_run:
        log("  WOULD process with restart-between-attempts + wav fallback")
        continue

    ok = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            restart_superwhisper()
            time.sleep(WARMUP_SECONDS)
        target, ts = path, get_audio_timestamp(path)
        if attempt == MAX_ATTEMPTS:
            wav = transcode_to_wav(audio)
            if wav:
                target, ts = str(wav), get_audio_timestamp(path)
        log(f"PROCESSING (attempt {attempt}/{MAX_ATTEMPTS}): {Path(target).name}")
        state = load_state()
        state["processed"].pop(path, None)
        save_state(state)
        try:
            ok, category = process_audio(target, ts, state)
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
        log(f"  ⏳ attempt {attempt} failed (state: {entry.get('status', 'none')})")

    if not ok:
        log(f"❌ STILL FAILING after {MAX_ATTEMPTS} attempts — manual review needed")

log("DONE")
