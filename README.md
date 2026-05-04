# SuperwhisperObsidianWF

[![License: MIT](https://img.shields.io/github/license/thegostev/recording-analyser?color=green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![macOS](https://img.shields.io/badge/platform-macOS-000000?logo=apple&logoColor=white)](https://support.apple.com/guide/launchd)
[![Obsidian](https://img.shields.io/badge/output-Obsidian%20Markdown-7C3AED?logo=obsidian&logoColor=white)](https://obsidian.md)

> [!STABLE]
> The service works stable and without issues after 100 test recordings over a last month.

> [!WALK_AND_TALK]
> If you make voice -> text notes while walking - this app is for you.

## Why

Voice notes are easy to make then they should be analysed and stored properly for reuse. For example: after a morning walk where you clean up your head by talking thoughts out with [Just Press Record](https://www.openplanetsoftware.com/just-press-record/) app, you need them be available immediately in a folder.

This tool removes any tech-related friction. You hit record, you stop record, you forget about it. A few minutes later a structured Markdown analysis lands in the correct (Obsidian vault) folder — already titled, categorized, summarized.

**Benefits:**
- **Zero friction capture** — recording on your phone is the only manual action
- **Right vault, right folder** — recordings auto-route by topic (family, career, ideas, etc.) into the matching Obsidian vault
- **Structured output** — each note has a meeting title, summary, decisions, action items — not a wall of raw transcript
- **Local & private** — Superwhisper runs on-device; nothing leaves your Mac
- **Set and forget** — runs as a background service; survives reboots when registered with launchd

## What

A macOS daemon that bridges Just Press Record → Superwhisper → Obsidian.

```
.m4a file appears  →  Superwhisper Custom Mode  →  parse output  →  Markdown in Obsidian
                      (transcribe + classify        (CATEGORY,        (routed by category)
                       + analyze in one pass)        FILENAME,
                                                     ANALYSIS)
```

The Python service handles file discovery, handoff to Superwhisper, polling for the result, parsing the structured output, and writing the file to the matching vault folder. Superwhisper does the heavy lifting (transcription, classification, analysis) in a single Custom Mode pass — no separate Whisper or LLM step on the Python side.

State is tracked in `~/.superwhisper_transcriber_state.json` — files are never reprocessed.

## Install

**Prerequisites:**
- macOS
- Python 3.9+
- [Superwhisper](https://superwhisper.com) installed and launched at least once
- [Just Press Record](https://www.openplanetsoftware.com/just-press-record/) (or any app that drops `.m4a` files into `YYYY-MM-DD/` subfolders)

**1. Clone and set up the venv:**

```bash
git clone <repo-url> superwhisper-obsidian-wf
cd superwhisper-obsidian-wf

python3 -m venv venv
source venv/bin/activate
pip install -e .
```

**2. Configure Superwhisper Custom Mode:**

In Superwhisper → Settings → Modes, create a Custom Mode (e.g. `meeting`) with a prompt that outputs exactly this structure:

```
---CATEGORY---
<one of your category names>
---FILENAME---
<meeting title — no date, no .md extension>
---ANALYSIS---
<full analysis in Markdown>
```

Define your categories and what kind of recording each one matches inside the prompt. The mode key (the filename in `~/Documents/superwhisper/modes/`, without extension) goes into `config.yaml`.

**3. Configure the service:**

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
- `superwhisper_mode_key` — the Custom Mode you just created (e.g. `meeting`)
- `watch_folder` — where Just Press Record saves audio
- `folders` — map each `CATEGORY` value to an Obsidian vault folder (categories must match the prompt exactly)

## Use

Run the service via the shell wrapper:

```bash
./run_transcriber.sh start     # launch in background
./run_transcriber.sh stop      # stop
./run_transcriber.sh restart   # stop + start
./run_transcriber.sh status    # running? + last 5 log lines
./run_transcriber.sh logs      # tail live log (Ctrl+C exits the tail, not the service)
```

To make it start automatically on login, register with launchd by creating `~/Library/LaunchAgents/com.necessaire.transcriber.plist` pointing at `venv/bin/python3` and `auto_transcribe.py`, then `launchctl load` it.

### Catchup

Process recordings the daemon missed (downtime, new install, etc.):

```bash
./run_transcriber.sh catchup-preview        # dry run, last 7 days
./run_transcriber.sh catchup                # process last 7 days
./run_transcriber.sh catchup 14             # process last 14 days
```

### Maintenance

Fix gaps without re-recording:

```bash
./run_transcriber.sh fix-analysis --dry-run   # preview missing analysis
./run_transcriber.sh fix-analysis             # generate missing analysis files
./run_transcriber.sh fix-categories --dry-run # preview reclassification
./run_transcriber.sh fix-all --dry-run        # both at once
```

### Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

## Configuration reference

All settings live in `config.yaml` (gitignored). Key fields:

| Setting | Default | Description |
|---|---|---|
| `superwhisper_mode_key` | `meeting` | Custom Mode filename in `~/Documents/superwhisper/modes/` (no extension) |
| `superwhisper_timeout` | `3600` | Seconds to wait for Superwhisper result before giving up |
| `superwhisper_recordings_dir` | `~/Documents/superwhisper/recordings` | Where Superwhisper writes results |
| `superwhisper_poll_interval` | `3` | Seconds between result polls |
| `watch_folder` | — | Where audio files land (`YYYY-MM-DD/*.m4a` subfolders) |
| `folders` | — | Category name → output vault folder. Must include a `DEFAULT` entry |
| `state_file` | `~/.meeting_transcriber_state.json` | Tracks processed files |
| `scan_interval` | `30` | Seconds between scan cycles |
| `scan_days_back` | `7` | How many days back to scan |
| `max_files_per_cycle` | `5` | Files per scan cycle (prevents backlog stampede) |
| `max_retries` | `3` | Retry attempts per stage |
| `retry_backoff` | `[10, 30, 60]` | Seconds between retries |

## Project structure

```
auto_transcribe.py     Long-running daemon (scan loop)
ondemand_transcribe.py Manual catchup CLI
reclassify_and_fix.py  Maintenance: fix missing analysis, reclassify files
pipeline.py            Shared pipeline: handoff, poll, parse, write, state
run_transcriber.sh     Shell wrapper for all common operations
config.py              Loads config.yaml
config.yaml            Active configuration (gitignored)
config.example.yaml    Template
ARCHITECTURE.md        WBS decomposition (S1–S3)
docs/adr/              Architectural decision records (ADR 0007 = Superwhisper switch)
tests/                 Test suite
```

## Notes

- Superwhisper must be running. If it's closed, `open -a Superwhisper` launches it; first-run latency adds to processing time.
- Output paths are typically iCloud-synced — large bursts may take a moment to appear in Obsidian.
- The daemon polls `meta.json.llmResult` from Superwhisper's recordings folder and correlates results to handoffs by timestamp. If you dictate manually while a file is processing, that dictation is ignored (no `CATEGORY:` header).
- File naming: `YY-MM-DD HH.MM - Meeting Title.md`.
