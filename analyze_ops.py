"""Analyze finished Superwhisper transcripts via Ollama → vault notes (ops tool, 2026-09-08).

For re-drives where the voice-model transcript is complete in a Superwhisper
recording dir but the LLM pass can't run (local S1-Language model not installed,
context too small for long transcripts). Runs the Custom Mode prompt against an
Ollama model, parses the CATEGORY/FILENAME contract, saves the note, updates
state, and marks the recording dirs consumed.

Usage: venv/bin/python3 analyze_ops.py [--dry-run]
"""

import json
import sys
import urllib.request
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

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "glm-5.3-flash:cloud"
PROMPT_FILE = Path.home() / "Documents/superwhisper/modes/custom.json"
TIMEOUT_SECONDS = 900

# (recording_dir, audio_path) — transcripts must already be complete in meta.json
JOBS = [
    (
        "1788860843",
        "/Users/harald/Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents/2026-09-05/18-01-23.m4a",
    ),
    (
        "1788857138",
        "/Users/harald/Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents/2026-09-05/20-36-16.m4a",
    ),
]


def call_ollama(prompt: str, transcript: str) -> str:
    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Transcript:\n\n" + transcript},
            ],
            "options": {"num_ctx": 65536},
        }
    ).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return str(json.load(resp)["message"]["content"])


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    prompt = json.loads(PROMPT_FILE.read_text(encoding="utf-8"))["prompt"]
    state = load_state()

    for recording_dir_name, audio_path in JOBS:
        meta = json.loads(
            (Path(SUPERWHISPER_RECORDINGS_DIR) / recording_dir_name / "meta.json").read_text(encoding="utf-8")
        )
        transcript = meta.get("rawResult") or ""
        if not transcript:
            print(f"No transcript in {recording_dir_name} — skipping.")
            continue

        print(f"[{audio_path.rsplit('/', 1)[-1]}] transcript {len(transcript)} chars → {OLLAMA_MODEL}...", flush=True)
        raw = call_ollama(prompt, transcript)
        try:
            category, ai_filename, analysis = parse_superwhisper_output(raw)
        except Exception as e:
            print(f"  ❌ Parse failed: {e}\n  First 300 chars of response:\n{raw[:300]}", flush=True)
            continue

        timestamp = get_audio_timestamp(audio_path)
        fname = f"{timestamp.strftime(TIMESTAMP_FORMAT)} - {ai_filename.removesuffix('.md')}.md"
        if dry_run:
            print(
                f"  DRY RUN — would save {fname} (category {category}); body preview:\n{analysis[:400]}\n", flush=True
            )
            continue

        out = save_output(category, fname, analysis)
        print(f"  ✅ Saved: {out}", flush=True)
        state.setdefault("processed", {})[audio_path] = {
            "status": "complete",
            "category": category,
            "timestamp": timestamp.isoformat(),
            "attempts": state["processed"].get(audio_path, {}).get("attempts", 0) + 1,
            "analysis_via": f"ollama:{OLLAMA_MODEL}",
            "transcript_from": recording_dir_name,
        }
        save_state(state)
        _mark_consumed(Path(SUPERWHISPER_RECORDINGS_DIR) / recording_dir_name)


if __name__ == "__main__":
    main()
