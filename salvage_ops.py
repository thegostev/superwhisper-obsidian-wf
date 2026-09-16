"""Salvage a finished Superwhisper recording dir → vault note (ops tool, 2026-09-08).

For re-drives where the poller fast-failed but Superwhisper finished anyway
(local-model transcriptions outlive STABILITY_POLLS_CEILING). Reads llmResult
from the recording dir, parses, saves the note, marks the dir consumed, and
flips the state entry to complete.

Usage: venv/bin/python3 salvage_ops.py <recording_dir_name> <audio_path>
"""

import sys
from pathlib import Path

from pipeline import (
    SUPERWHISPER_RECORDINGS_DIR,
    TIMESTAMP_FORMAT,
    _mark_consumed,
    get_audio_timestamp,
    load_state,
    parse_superwhisper_output,
    save_output,
    save_state,
)


def salvage(recording_dir_name: str, audio_path: str) -> bool:
    recording_dir = Path(SUPERWHISPER_RECORDINGS_DIR) / recording_dir_name
    meta_path = recording_dir / "meta.json"
    import json

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    llm = meta.get("llmResult") or ""
    if not llm:
        print(f"No llmResult in {recording_dir_name} yet — nothing to salvage.")
        return False

    category, ai_filename, analysis = parse_superwhisper_output(llm)
    timestamp = get_audio_timestamp(audio_path)
    fname = f"{timestamp.strftime(TIMESTAMP_FORMAT)} - {ai_filename.removesuffix('.md')}.md"
    out = save_output(category, fname, analysis)
    print(f"Saved: {out}")

    state = load_state()
    state.setdefault("processed", {})[audio_path] = {
        "status": "complete",
        "category": category,
        "timestamp": timestamp.isoformat(),
        "attempts": state["processed"].get(audio_path, {}).get("attempts", 0) + 1,
        "salvaged_from": recording_dir_name,
    }
    save_state(state)
    _mark_consumed(recording_dir)
    print(f"State → complete; recording {recording_dir_name} marked consumed.")
    return True


if __name__ == "__main__":
    ok = salvage(sys.argv[1], sys.argv[2])
    sys.exit(0 if ok else 1)
