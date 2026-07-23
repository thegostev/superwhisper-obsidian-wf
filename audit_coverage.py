"""One-shot audit: match audio files to transcripts per category.

Reads WATCH_FOLDER, STATE_FILE, and FOLDERS from config so it stays in sync
with the active configuration. Run with:

    python audit_coverage.py
"""

import json
from collections import defaultdict
from pathlib import Path

from config import FOLDERS, WATCH_FOLDER

STATE = Path.home() / ".superwhisper_transcriber_state.json"
WATCH = Path(WATCH_FOLDER)

# Categories to tabulate (exclude DEFAULT — it's a routing fallback, not a real category).
CATEGORIES = [c for c in FOLDERS if c != "DEFAULT"]

processed = json.loads(STATE.read_text())["processed"]

# Build per-date breakdowns from state
by_date: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
fail_by_date = defaultdict(list)

for path, info in processed.items():
    *_, date, fname = path.split("/")
    if (status := info.get("status", "unknown")) == "complete":
        by_date[date][info.get("category") or "UNKNOWN"] += 1
    else:
        fail_by_date[date].append(f"{fname} [{status}]")

# Count audio files per day from watch folder
audio_by_date = {}
untracked = defaultdict(list)

for day_dir in sorted(WATCH.iterdir()):
    if not day_dir.is_dir() or not day_dir.name.startswith("2026"):
        continue
    if m4as := sorted(day_dir.glob("*.m4a")):
        audio_by_date[day_dir.name] = len(m4as)
        for m4a in m4as:
            if str(m4a) not in processed:
                untracked[day_dir.name].append(m4a.name)

all_dates = sorted({d for src in (audio_by_date, by_date, fail_by_date) for d in src if d >= "2026"})

# Print table — one column per category, plus UNKNOWN and Failed
cat_cols = CATEGORIES + ["UNKNOWN"]
header = f"| {'Date':<12} | {'Audio':>5} |" + "".join(f" {c:>9} |" for c in cat_cols) + f" {'Failed':>6} | {'Coverage':>8} |"
print(header)
print(f"|{'-' * 14}|{'-' * 7}" + "".join(f"{'-' * 11}" for _ in cat_cols) + f"|{'-' * 8}|{'-' * 10}|")

totals = {c: 0 for c in cat_cols}
total_failed = 0
total_audio = 0

for date in all_dates:
    counts = {c: by_date[date].get(c, 0) for c in cat_cols}
    failed = len(fail_by_date[date])
    audio = audio_by_date.get(date)
    processed_count = sum(counts.values()) + failed
    coverage = f"{processed_count / audio * 100:.0f}%" if audio else "?"
    for c in cat_cols:
        totals[c] += counts[c]
    total_failed += failed
    total_audio += audio or 0
    row = f"| {date:<12} | {audio or '?':>5} |" + "".join(f" {counts[c]:>9} |" for c in cat_cols) + f" {failed:>6} | {coverage:>8} |"
    print(row)

print(f"|{'-' * 14}|{'-' * 7}" + "".join(f"{'-' * 11}" for _ in cat_cols) + f"|{'-' * 8}|{'-' * 10}|")
totals_row = f"| {'TOTAL':<12} | {total_audio:>5} |" + "".join(f" {totals[c]:>9} |" for c in cat_cols) + f" {total_failed:>6} | {'':>8} |"
print(totals_row)

print(f"\n=== UNTRACKED AUDIO (in folder, not in state): {sum(len(v) for v in untracked.values())} files ===")
for date in sorted(untracked):
    print(f"  {date}: {', '.join(untracked[date])}")

print(f"\n=== FAILED ENTRIES: {sum(len(v) for v in fail_by_date.values())} files ===")
for date in sorted(fail_by_date):
    for entry in fail_by_date[date]:
        print(f"  {date}: {entry}")
