# SuperwhisperObsidianWF — Architecture & WBS Decomposition

Adapted from NASA/SP-20210023927 Work Breakdown Structure standard.

Three rules at every level:
- **100% Rule**: every piece of work appears somewhere in the decomposition
- **Mutual Exclusion Rule**: no work appears in two places
- **80-Hour Rule**: no atomic task (L4) exceeds ~30 minutes of AI execution time

---

## Level 0 — System

**SuperwhisperObsidianWF**: Automated audio transcription and analysis service
that ingests recordings from Just Press Record, hands them to Superwhisper
for transcription + AI analysis in a single pass, and outputs structured
Markdown notes to categorized Obsidian vaults.

**System boundary**:
- Inside: audio discovery, Superwhisper handoff, result polling, output
  parsing, file output, state tracking, maintenance/repair tools
- Outside: Just Press Record app (audio capture), Superwhisper (transcription
  + AI analysis via Custom Mode), Obsidian (rendering/search), iCloud (file
  sync), launchd (process management)

**Stakeholders**:
- Owner/operator: single user (TPM) who records meetings and reviews
  outputs in Obsidian
- Maintainer: same user, assisted by Claude Code

**Quality goals** (from ISO 25010, cascading to all levels):
1. **Reliability** — service runs 24/7 via launchd; must recover from
   crashes and iCloud sync issues without data loss
2. **Observability** — all operations produce timestamped structured logs;
   failures must be diagnosable from log files alone
3. **Data integrity** — no audio recording is silently skipped; every file
   is either processed successfully or explicitly logged as failed with reason
4. **Functional correctness** — analysis faithfully represents audio content;
   classification places notes in correct category vault folder
5. **Maintainability** — single-developer codebase; changes must not require
   understanding unrelated modules

**Architectural style**: long-running daemon (auto-transcription) +
CLI tools (on-demand processing, maintenance) sharing a common library

> **Quality coverage checkpoint (L0)**:
> Availability: service auto-restarts within 30s of crash (launchd).
> Data loss: 0 silently dropped recordings per month.
> Log coverage: every file processing attempt produces ≥1 log entry.
> Classification accuracy: <5% files in DEFAULT (miscategorized).

---

## Level 1 — Subsystems

### S1: Transcription Pipeline

Core processing engine shared by all entry points. Takes an audio file
path as input, hands it to Superwhisper, reads and parses the result,
and writes a single analysis Markdown file to the correct Obsidian vault
folder.

- **Interface**: `process_audio(file_path, timestamp, state)` →
  writes one `.md` analysis file, updates state
- **Data ownership**: analysis Markdown files directly in `FOLDERS[category]/`

### S2: Service Orchestration

Controls when and how the pipeline runs. Includes the continuous daemon
loop and CLI entry points for catchup/maintenance.

- **Interface**: launchd plist → `auto_transcribe.py` main loop;
  CLI args → `ondemand_transcribe.py`, `reclassify_and_fix.py`
- **Data ownership**: service lifecycle, scan scheduling, batch coordination

### S3: State & Persistence

Tracks which files have been processed, their status, and provides
deduplication to prevent re-processing.

- **Interface**: `load_state()` / `save_state(state)`;
  `build_transcript_index(folders)` for deduplication
- **Data ownership**: `~/.superwhisper_transcriber_state.json`,
  `~/.superwhisper_transcriber_failed.log`

> **Quality coverage checkpoint (L1)**:
> S1: Error handling via 3-tier classification (fatal/permanent/transient).
> S2: Failover via launchd respawn + per-file error isolation.
> S3: Atomic state writes prevent corruption on crash.

---

## Level 2 — Modules

### S1.M1: Audio Discovery

Scans date-based folder structure under the Just Press Record watch
folder to find unprocessed `.m4a` files.

- **Public interface**: `discover_recent_folders(watch_folder, days_back)`,
  `is_file_stable(path, wait_seconds)`
- **Internal data model**: list of `(file_path, timestamp)` tuples
- **Dependencies**: S3 (state + output index for filtering)

### S1.M2: Superwhisper Integration

Manages the full handoff to Superwhisper: mode switching, file open,
result polling, and output parsing. All transcription and AI analysis
happens inside Superwhisper; this module is the bridge.

- **Public interface**:
  - `switch_superwhisper_mode()` — opens `superwhisper://mode?key=<key>` deep link
  - `handoff_to_superwhisper(file_path)` — opens `.m4a` via `open -a Superwhisper`
  - `wait_for_superwhisper_result(file_path, since)` — polls `~/Documents/superwhisper/recordings/` for new `meta.json` with `CATEGORY:` in `llmResult`
  - `parse_superwhisper_output(raw_output)` → `(category, filename, analysis)`
  - `_read_superwhisper_entry(path)` — reads `llmResult` from recording directory
- **Internal data model**: Superwhisper recording directories
  (`<id>/meta.json` + `output.wav`); `llmResult` field contains Custom
  Mode output
- **Dependencies**: `SUPERWHISPER_MODE_KEY`, `SUPERWHISPER_RECORDINGS_DIR`,
  `SUPERWHISPER_TIMEOUT`, `SUPERWHISPER_POLL_INTERVAL` from config;
  Superwhisper app installed and running

**Output contract** (defined in Custom Mode prompt):
```
CATEGORY: <WORK | TEAM | PERSONAL | INTERVIEWS | DEFAULT>
FILENAME: <meeting title>

<analysis body in Markdown>
```

### S1.M3: File Output

Writes the analysis Markdown to the correct Obsidian vault folder based
on category. Single file per recording; no subfolders.

- **Public interface**: `save_output(category, filename, content)` →
  writes `.md` to `FOLDERS[category]/`; `save_analysis` aliased to same
- **Internal data model**: category-to-path mapping in `FOLDERS`
- **Dependencies**: S1.M2 (category + filename from parsed output),
  iCloud-synced filesystem

### S2.M1: Daemon Loop

Infinite scan-process-sleep cycle with per-cycle caps and inter-file delays.

- **Public interface**: `main()` → runs until killed;
  `run_scan_cycle(state, transcript_index, cycle_number)`
- **Internal data model**: cycle counter, scan interval (30s),
  max files per cycle (5), inter-file delay (5s)
- **Dependencies**: S1, S3

### S2.M2: CLI Entry Points

argparse-based CLIs for on-demand processing and maintenance.

- **Public interface**: `ondemand_transcribe.py` CLI (`--catchup`, `--dry-run`);
  `reclassify_and_fix.py` CLI (`--generate-missing-analysis`, `--reclassify`)
- **Note**: `--reprocess-partial` is not supported — Superwhisper requires
  the original audio file; analysis cannot be regenerated from transcript alone
- **Dependencies**: S1 pipeline functions

### S2.M3: Maintenance & Repair

File-level operations for fixing misclassified files and moving outputs.
Classification and analysis regeneration are stubbed pending future
Superwhisper integration improvements.

- **Public interface**: `find_missing_analysis()`,
  `move_transcript_and_analysis()`, `scan_default_folder()`
- **Dependencies**: filesystem only (no AI calls in current implementation)

### S3.M1: Processing State

JSON-backed persistent state tracking which audio files have been processed.

- **Public interface**: `load_state()`, `save_state(state)`
- **Internal data model**: `{"processed": {path: {status, category, timestamp, attempts, error}}}`
- **Dependencies**: `~/.superwhisper_transcriber_state.json`

### S3.M2: Output Index

In-memory deduplication index built by scanning existing output files.
With single-file output, scans category root folders directly.

- **Public interface**: `build_transcript_index(folders)` → dict keyed by
  `YY-MM-DD HH.MM` timestamp prefix
- **Internal data model**: `{timestamp_key: {category, output_path}}`
- **Dependencies**: `FOLDERS[category]/` directories

> **Quality coverage checkpoint (L2)**:
> S1.M1: Filters `.icloud`, `.tmp`, hidden files; 2s stability check.
> S1.M2: `FatalAPIError` if mode key empty or recordings folder missing;
> `PermanentFileError` on missing `CATEGORY:`/analysis body;
> `TimeoutError` after SUPERWHISPER_TIMEOUT seconds (→ retry).
> S1.M3: Creates directories on demand; no collision suffix needed
> (timestamp prefix ensures uniqueness within a minute).
> S2.M1: Rate-limited (5s between files, 5 files/cycle cap).
> S2.M3: All operations support `--dry-run`.
> S3.M1: Returns empty dict on corrupt state file (self-healing).
> S3.M2: Rebuilt from filesystem each startup.

---

## Level 3 — Components

### S1.M1.C1: Folder Scanner
- **File**: `pipeline.py` — `discover_recent_folders()`
- **Interface**: `(watch_folder, days_back)` → list of date folder paths
  sorted chronologically
- **Error taxonomy**: `OSError` if watch folder missing; silently skips
  non-date folders

### S1.M1.C2: File Filter & Stability Check
- **File**: `auto_transcribe.py` — `discover_audio_files()`;
  `pipeline.py` — `is_file_stable()`
- **Interface**: `(watch_folder, state, transcript_index)` → list of
  unprocessed, stable `.m4a` file paths
- **Error taxonomy**: `OSError` on inaccessible files; logs and skips

### S1.M1.C3: Timestamp Extractor
- **File**: `pipeline.py` — `get_audio_timestamp()`
- **Interface**: `(audio_path)` → `datetime` via 3-strategy fallback:
  macOS `mdls` → directory/filename parse → `os.path.getctime`
- **Error taxonomy**: returns `datetime.now()` if all strategies fail

### S1.M2.C1: Mode Switcher
- **File**: `pipeline.py` — `switch_superwhisper_mode()`
- **Interface**: opens `superwhisper://mode?key=<SUPERWHISPER_MODE_KEY>`,
  sleeps 1s to let the mode switch settle
- **Error taxonomy**: `FatalAPIError` if `SUPERWHISPER_MODE_KEY` is empty;
  `subprocess.CalledProcessError` if `open` command fails

### S1.M2.C2: File Handoff
- **File**: `pipeline.py` — `handoff_to_superwhisper()`
- **Interface**: `(file_path)` → opens file in Superwhisper via
  `open file_path -a Superwhisper`
- **Error taxonomy**: `PermanentFileError` if file does not exist;
  `.m4a` accepted as MPEG-4 audio; convert to WAV with ffmpeg if needed

### S1.M2.C3: Result Poller
- **File**: `pipeline.py` — `wait_for_superwhisper_result()`,
  `_read_superwhisper_entry()`
- **Interface**: `(file_path, since: float)` → raw `llmResult` string;
  polls `SUPERWHISPER_RECORDINGS_DIR` every `SUPERWHISPER_POLL_INTERVAL`s
  for directories newer than `since` whose `meta.json` contains `CATEGORY:`
- **Error taxonomy**: `FatalAPIError` if recordings folder missing;
  `TimeoutError` after `SUPERWHISPER_TIMEOUT`s (→ transient retry)

### S1.M2.C4: Output Parser
- **File**: `pipeline.py` — `parse_superwhisper_output()`
- **Interface**: `(raw_output)` → `(category, filename, analysis)`;
  parses `CATEGORY: X` / `FILENAME: X` header lines; analysis body is
  everything after the headers
- **Error taxonomy**: non-ASCII/emoji stripped from category value before
  lookup (`TEAM 🍎` → `TEAM`); unknown category → DEFAULT
  with warning logged; empty filename → "Unknown Meeting"; missing analysis
  body → `PermanentFileError`

### S1.M3.C1: Markdown Writer
- **File**: `pipeline.py` — `save_output()`
- **Interface**: `(category, filename, content)` → writes `.md` to
  `FOLDERS[category]/`; creates directory if missing
- **Error taxonomy**: `OSError` on write failure; falls back to DEFAULT
  folder if category unknown

### S3.M1.C1: State File Handler
- **File**: `pipeline.py` — `load_state()`, `save_state()`
- **Interface**: `load_state()` → dict (empty if file missing or corrupt);
  `save_state(state)` → writes JSON
- **Error taxonomy**: `json.JSONDecodeError` → returns empty state
  (self-healing); `OSError` on write → logged, not fatal

> **Quality coverage checkpoint (L3)**:
> S1.M2.C1: Empty mode key is a startup-time `FatalAPIError` — service
> does not process files until config is corrected.
> S1.M2.C3: Time-based correlation (`since` timestamp) is sufficient for
> sequential single-file processing; concurrent user dictation is filtered
> by requiring `CATEGORY:` in the result.
> S1.M2.C4: Falls back gracefully — never raises on parse failure.
> S1.M3.C1: Directory created on demand; no collision handling needed
> (timestamp uniqueness within-minute guaranteed by JPR naming).
> S3.M1.C1: Self-healing on corrupt state.

---

## Dependency Map

```
 ┌─────────────────────────────────────────────────┐
 │           Superwhisper (Custom Mode)             │
 │  transcription + AI analysis in a single pass   │
 └──────────────────────┬──────────────────────────┘
                        │ llmResult (meta.json)
                        ▼
          ┌─────────────────────────────────────────┐
          │         S2: Service Orchestration        │
          │                                         │
          │  S2.M1 Daemon Loop                      │
          │  S2.M2 CLI Entry Points                 │
          │  S2.M3 Maintenance & Repair             │
          └──────────────┬──────────────────────────┘
                         │ invokes
                         ▼
┌────────────────────────────────────────────────────────┐
│                S1: Transcription Pipeline               │
│                                                        │
│  S1.M1 Audio          S1.M2 Superwhisper               │
│  Discovery       ───► Integration          ───►        │
│  (discover,           (switch mode,         S1.M3      │
│   filter,              handoff,             File       │
│   timestamp)           poll, parse)         Output     │
└──────────────────────────┬─────────────────────────────┘
                           │ reads/writes
                           ▼
                  ┌──────────────────┐
                  │ S3: State &      │
                  │ Persistence      │
                  │                  │
                  │ S3.M1 State      │
                  │ S3.M2 Index      │
                  └──────────────────┘
```

---

## Notes

- **Level 4 (Atomic Tasks)** are tracked in `TASKS.md`, not in this file
- Update this document when modules are added, split, or merged
- Cross-reference ADRs in `docs/adr/` for technology decisions — ADR 0007
  documents the switch from Whisper+Ollama/Gemini to Superwhisper
- **Known dependency**: `meta.json` schema is internal to Superwhisper and
  not a public API — field name `llmResult` could change across app versions
- **Known limitation**: `--reprocess-partial` and AI-driven reclassification
  are stubbed — re-processing requires the original audio file
