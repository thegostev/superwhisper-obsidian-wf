"""One-shot audit: match audio files to transcripts per category."""

import json
from collections import defaultdict
from pathlib import Path

STATE = Path.home() / ".meeting_transcriber_state.json"
WATCH = Path.home() / "Library/Mobile Documents/iCloud~com~openplanetsoftware~just-press-record/Documents"

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
    m4as = sorted(day_dir.glob("*.m4a"))
    if m4as:
        audio_by_date[day_dir.name] = len(m4as)
        for m4a in m4as:
            if str(m4a) not in processed:
                untracked[day_dir.name].append(m4a.name)

all_dates = sorted({d for src in (audio_by_date, by_date, fail_by_date) for d in src if d >= "2026"})

# Print table
print(f"| {'Date':<12} | {'Audio':>5} | {'PERSONLIG':>9} | {'MINNESOTERE':>11} | {'MUSIKKERE':>9} | {'UNKNOWN':>7} | {'Failed':>6} | {'Coverage':>8} |")
print(f"|{'-' * 14}|{'-' * 7}|{'-' * 11}|{'-' * 13}|{'-' * 11}|{'-' * 9}|{'-' * 8}|{'-' * 10}|")

tp = tm = tmu = tu = tf = ta = 0

for date in all_dates:
    pe, mn, mu, un = (by_date[date].get(k, 0) for k in ("PERSONLIG", "MINNESOTERE", "MUSIKKERE", "UNKNOWN"))
    fa = len(fail_by_date[date])
    audio = audio_by_date.get(date)
    coverage = f"{(pe + mn + mu + un + fa) / audio * 100:.0f}%" if audio else "?"
    tp, tm, tmu, tu, tf, ta = tp+pe, tm+mn, tmu+mu, tu+un, tf+fa, ta+(audio or 0)
    print(f"| {date:<12} | {audio or '?':>5} | {pe:>9} | {mn:>11} | {mu:>9} | {un:>7} | {fa:>6} | {coverage:>8} |")

print(f"|{'-' * 14}|{'-' * 7}|{'-' * 11}|{'-' * 13}|{'-' * 11}|{'-' * 9}|{'-' * 8}|{'-' * 10}|")
print(f"| {'TOTAL':<12} | {ta:>5} | {tp:>9} | {tm:>11} | {tmu:>9} | {tu:>7} | {tf:>6} | {'':>8} |")

print(f"\n=== UNTRACKED AUDIO (in folder, not in state): {sum(len(v) for v in untracked.values())} files ===")
for date in sorted(untracked):
    print(f"  {date}: {', '.join(untracked[date])}")

print(f"\n=== FAILED ENTRIES: {sum(len(v) for v in fail_by_date.values())} files ===")
for date in sorted(fail_by_date):
    for entry in fail_by_date[date]:
        print(f"  {date}: {entry}")
