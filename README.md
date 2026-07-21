# SuperwhisperObsidianWF

[![License: MIT](https://img.shields.io/github/license/thegostev/recording-analyser?color=green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![macOS](https://img.shields.io/badge/platform-macOS-000000?logo=apple&logoColor=white)](https://support.apple.com/guide/launchd)
[![Obsidian](https://img.shields.io/badge/output-Obsidian%20Markdown-7C3AED?logo=obsidian&logoColor=white)](https://obsidian.md)

<img width="2814" height="1536" alt="Gemini_Generated_Image_588w7j588w7j588w" src="https://github.com/user-attachments/assets/ff60d7a8-9d9e-41e3-bddb-de8b4fc30a65" />

> The service works stable and without issues after 100 test recordings over a last month.

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

### Architecture

The daemon watches a folder for new `.m4a` files, hands each to Superwhisper's Custom Mode (which transcribes + classifies + analyzes in one pass), polls `meta.json.llmResult` for the structured output, and writes a single Markdown file to the matching Obsidian vault folder. State in `~/.superwhisper_transcriber_state.json` guarantees no file is processed twice.

Three thin entry points share one `pipeline.py` module:
- `auto_transcribe.py` — long-running daemon (scan loop, 30s default)
- `ondemand_transcribe.py` — CLI for catchup against a backlog
- `reclassify_and_fix.py` — maintenance: regenerate missing analysis, reclassify misrouted files

See `ARCHITECTURE.md` for the full WBS decomposition (subsystems S1–S3, modules, components) and `docs/adr/` for the decision history — ADR 0007 documents the switch to Superwhisper.

### Output format

Each recording produces a single Markdown file named `YY-MM-DD HH.MM - <meeting title>.md`, placed directly in the category's output folder (no subfolders). Example:

```markdown
# 26-04-20 12.00 - Team Delta Sync

## Summary
...

## Decisions
- ...

## Action items
- [ ] ...
```

The title comes from the Custom Mode's `FILENAME:` header. The body is whatever the Custom Mode prompt produces — you control the structure entirely through the prompt.

## Install

**Prerequisites:**
- macOS
- Python 3.12+
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
CATEGORY: <one of your category names>
FILENAME: <meeting title — no date, no .md extension, no slashes>

<full analysis in Markdown>
```

Define your categories and what kind of recording each one matches inside the prompt. The mode key (the filename in `~/Documents/superwhisper/modes/`, without extension) goes into `config.yaml`. The category names in the prompt must match the `folders` keys in `config.yaml` exactly. The parser strips non-ASCII characters (emoji) from the `CATEGORY:` value before lookup, so `WORK 🍎` resolves to `WORK`.

**3. Configure the service:**

```bash
mkdir -p locations
cp config.example.yaml locations/config.yaml
```

Edit `locations/config.yaml`:
- `superwhisper_mode_key` — the Custom Mode you just created (e.g. `meeting`)
- `watch_folder` — where Just Press Record saves audio
- `folders` — map each `CATEGORY` value to an Obsidian vault folder (categories must match the prompt exactly). A `DEFAULT` entry is required as the fallback.

> **Path resolution**: `~/...` paths expand against the home dir. `/abs/...` paths are used unchanged. Paths starting with `Obsidian/...` are resolved against the Obsidian vault root (found by walking up from `config.py`'s location until an `Obsidian` folder is found), so the same config works on any Mac regardless of where Obsidian lives.

## Use

Run the service via the shell wrapper:

```bash
./run_transcriber.sh start     # launch in background
./run_transcriber.sh stop      # stop
./run_transcriber.sh restart   # stop + start
./run_transcriber.sh status    # running? + last 5 log lines
./run_transcriber.sh logs      # tail live log (Ctrl+C exits the tail, not the service)
```

To make it start automatically on login, register with launchd by creating `~/Library/LaunchAgents/com.yourdomain.transcriber.plist` pointing at `venv/bin/python3` and `auto_transcribe.py`, then `launchctl load` it.

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

All settings live in `locations/config.yaml` (gitignored). Copy `config.example.yaml` to start. Key fields:

| Setting | Default | Description |
|---|---|---|
| `superwhisper_mode_key` | `custom` | Custom Mode filename in `~/Documents/superwhisper/modes/` (no extension) |
| `superwhisper_timeout` | `3600` | Seconds to wait for Superwhisper result before giving up |
| `superwhisper_recordings_dir` | `~/Documents/superwhisper/recordings` | Where Superwhisper writes results |
| `superwhisper_poll_interval` | `3` | Seconds between result polls |
| `watch_folder` | — | Where audio files land (`YYYY-MM-DD/*.m4a` subfolders) |
| `folders` | — | Category name → output vault folder. Must include a `DEFAULT` entry |
| `state_file` | `~/.superwhisper_transcriber_state.json` | Tracks processed files (per-machine, do not commit) |
| `failed_analysis_log` | `~/.superwhisper_transcriber_failed.log` | Log for permanently-failed files |
| `scan_interval` | `30` | Seconds between scan cycles |
| `scan_days_back` | `7` | How many days back to scan |
| `delay_between_files` | `5` | Seconds between files (Superwhisper is local, no API rate limit) |
| `max_files_per_cycle` | `5` | Files per scan cycle (prevents backlog stampede) |
| `max_retries` | `3` | Retry attempts per stage |
| `retry_backoff` | `[10, 30, 60]` | Seconds between retries |

## Privacy & data flow

**No audio, transcripts, or analysis leave your Mac.** Superwhisper runs on-device. The only network traffic from this service is `git push` if you choose to publish the repo. State and logs stay on your machine.

The service reads audio from your watch folder, hands the file path to Superwhisper via `open -a Superwhisper`, polls `~/Documents/superwhisper/recordings/<id>/meta.json` for the result, and writes one Markdown file to your Obsidian vault. No telemetry, no analytics, no third-party calls.

## Troubleshooting

**`FatalAPIError` on startup** — `superwhisper_mode_key` is empty in `config.yaml`, or `~/Documents/superwhisper/recordings/` doesn't exist (Superwhisper not yet used). The default key is `custom` — the mode file is `~/Documents/superwhisper/modes/custom.json` (its `name` field is `Meeting`).

**`failed_retry` (empty recording stub)** — Superwhisper creates an empty `meta.json` stub (`duration: 0, processingTime: 0, result: ""`) when `open file -a Superwhisper` fires faster than it can process — the file-open is received but the transcription/LLM pipeline never runs for that stub. The poller detects this after 5 consecutive unchanged-mtime polls (~15s) and raises `TimeoutError` so `process_audio` records `failed_retry` and re-queues the file on the next scan cycle. Re-opening the audio file later (when Superwhisper is idle) processes it correctly. If a file accumulates 3 `failed_retry` attempts it becomes `failed_permanent` — at that point, either Superwhisper genuinely can't process that file, or the backlog is large enough that it never gets a free slot. Catchup with `./run_transcriber.sh catchup` re-opens them in a paced single pass.

**`failed_permanent` (LLM refused the contract)** — If `llmResult` is present but does NOT start with `CATEGORY:`, the Custom Mode LLM refused to produce the contract format — usually because the audio isn't a meeting (e.g. a 5-second connectivity check from Just Press Record). The poller raises `PermanentFileError` immediately (not transient — the audio content won't change on retry). Check the recording's `llmResult` in `meta.json` to see the refusal reason.

**Wrong output format / DEFAULT fallback** — Custom Mode prompt changed or Superwhisper used a different mode. Check `meta.json` of the latest recording to verify `llmResult` starts with `CATEGORY:`. The parser strips non-ASCII characters (emoji) from the `CATEGORY:` value before lookup, so `TEAM 🍎` resolves to `TEAM`. If DEFAULT is still appearing, the category label itself is unrecognised — check the prompt's category definitions match `config.yaml` `folders` keys exactly.

**Timeout (file marked `failed_retry`)** — Superwhisper didn't finish within `superwhisper_timeout` (default 3600s). Re-drop the audio file or clear the state entry to retry:

```python
python3 -c "
import json, os
path = os.path.expanduser('~/.superwhisper_transcriber_state.json')
with open(path) as f: state = json.load(f)
removed = [k for k, v in state['processed'].items() if v.get('status') != 'complete']
for k in removed: del state['processed'][k]
with open(path, 'w') as f: json.dump(state, f, indent=2)
print(f'Cleared {len(removed)} entries')
"
```

## Gotchas & constraints

- **Superwhisper must be running**: `open -a Superwhisper` launches it if closed, but first-run latency adds to processing time.
- **`meta.json` schema is internal**: `llmResult` field name is not a public API and could change across Superwhisper versions.
- **Time-based correlation**: the daemon matches results to handoffs by timestamp. If you dictate manually while the daemon is processing a file, that dictation is ignored (no `CATEGORY:` in output). Extremely unlikely to cause false positives.
- **iCloud sync latency**: output paths are iCloud-synced. The 2-second stability check (`is_file_stable`) may be insufficient on slow connections.
- **5 files per cycle cap**: `MAX_FILES_PER_CYCLE=5` prevents backlog stampede.
- **Superwhisper file-open concurrency**: `open file -a Superwhisper` creates an empty recording stub every time, but Superwhisper only processes one file at a time. Rapid successive opens (faster than `delay_between_files=5s`) cause earlier stubs to be abandoned. The poller's stability-based fast-fail detects these as `failed_retry` and re-queues them on the next cycle.

## Development

```bash
source venv/bin/activate
python -m pytest tests/ -v          # tests
ruff check .                        # lint
mypy pipeline.py config.py          # typecheck
```

The test suite mirrors the source layout: `tests/unit/`, `tests/integration/`, `tests/contract/`. Integration and contract tests are currently stubs — unit tests cover the pipeline.

## Project structure

```
auto_transcribe.py        Long-running daemon (scan loop)
ondemand_transcribe.py    Manual catchup CLI
reclassify_and_fix.py     Maintenance: fix missing analysis, reclassify files
pipeline.py               Shared pipeline: handoff, poll, parse, write, state
config.py                 Loads config.yaml (resolves ~/..., /abs/..., Obsidian/...)
config.example.yaml       Template — copy to locations/config.yaml
run_transcriber.sh        Shell wrapper for all common operations
audit_coverage.py         One-shot audit: audio files vs. processed state
ARCHITECTURE.md           WBS decomposition (S1–S3)
docs/adr/                 Architectural decision records (ADR 0007 = Superwhisper switch)
tests/                    Test suite
LICENSE                   MIT

locations/                Machine-specific (gitignored — see .gitignore)
├── config.yaml           Active configuration with your real paths
├── logs/                 transcriber.log, catchup.log, failed_analysis.log
├── state/                (optional) relocated state file
└── launchd/              (optional) your plist lives here
```

## Notes

- Superwhisper must be running. If it's closed, `open -a Superwhisper` launches it; first-run latency adds to processing time.
- Output paths are typically iCloud-synced — large bursts may take a moment to appear in Obsidian.
- The daemon polls `meta.json.llmResult` from Superwhisper's recordings folder and correlates results to handoffs by timestamp. If you dictate manually while a file is processing, that dictation is ignored (no `CATEGORY:` header).
- File naming: `YY-MM-DD HH.MM - Meeting Title.md`.

## Acknowledgements

- [Just Press Record](https://www.openplanetsoftware.com/just-press-record/) — audio capture
- [Superwhisper](https://superwhisper.com) — on-device transcription + Custom Mode analysis
- [Obsidian](https://obsidian.md) — Markdown knowledge base

## Disclaimer

Use at your own risk. Back up your Obsidian vault before running `catchup` against a large backlog. The service writes files to iCloud-synced folders — large bursts may take a moment to propagate.

## License

MIT — see [LICENSE](LICENSE).