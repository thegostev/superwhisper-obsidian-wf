# Architecture

A macOS daemon that bridges **Just Press Record → Superwhisper → Obsidian**.
Audio files land in a watched iCloud folder; Superwhisper (running locally, in a
Custom Mode) does transcription + classification + analysis in a single pass; the
Python service routes the structured result into the matching Obsidian vault folder.

There is **no separate Whisper or LLM step on the Python side** — the service is an
orchestrator around Superwhisper, not an inference engine.

## Data flow

```
YYYY-MM-DD/*.m4a            (Just Press Record, iCloud-synced)
        │
        ▼
discover_recent_folders ──► discover_audio_files      (scan + skip .icloud/.tmp/hidden/unstable)
        │                          │
        │                   build_transcript_index    (dedupe against files already in the vault)
        ▼
process_audio
   ├─ switch_superwhisper_mode      open superwhisper://mode?key=<mode>
   ├─ handoff_to_superwhisper       open <file> -a Superwhisper
   ├─ wait_for_superwhisper_result  poll recordings_dir/*/meta.json → llmResult
   ├─ parse_superwhisper_output     CATEGORY: / FILENAME: / <analysis body>
   └─ save_output                   write "YY-MM-DD HH.MM - <title>.md" to FOLDERS[category]
        │
        ▼
   state.json    (~/.meeting_transcriber_state.json — files are never reprocessed)
```

## Module responsibilities

| Module | Layer | Responsibility |
|---|---|---|
| `config.py` | Config | Load `config.yaml` once at import; expand `~`; export module-level constants. |
| `pipeline.py` | Core (shared) | State I/O, timestamp extraction, discovery/indexing, Superwhisper integration, parsing, and the `process_audio` orchestration. The single source of truth imported by all entry points. |
| `auto_transcribe.py` | Entry point | Long-running launchd daemon: scan loop, per-cycle file cap, idle heartbeat. |
| `ondemand_transcribe.py` | Entry point | Catchup CLI for backfilling missed recordings (dry-run + N-day window). |
| `reclassify_and_fix.py` | Entry point | Maintenance CLI (see *Known gaps*). |
| `audit_coverage.py` | Ops tool | One-shot coverage report: audio-in-folder vs. state vs. output. |
| `run_transcriber.sh` | Wrapper | zsh front door for start/stop/status/logs/catchup/fix-*. |

## Key design decisions

- **Config at import time.** `config.py` loads and validates once; every module imports
  ready-to-use constants. Simple, but means config errors surface as import-time exits and
  tests must stage a `config.yaml` before importing (see `tests/conftest.py`).
- **Timestamp is the correlation key.** `"YY-MM-DD HH.MM"` (14 chars) links an audio file,
  a state entry, and an output file. `build_transcript_index` uses it to skip already-written
  notes even if `state.json` is lost — the vault itself is a source of truth.
- **Error taxonomy drives retries.** `FatalAPIError` stops the service (misconfiguration);
  `PermanentFileError` fails one file forever (bad output); any other exception is retried up
  to `MAX_RETRIES` across scan cycles, then marked `failed_permanent`.
- **Result correlation by mtime.** `wait_for_superwhisper_result` polls the recordings dir and
  accepts the newest entry whose `mtime > handoff time` that also carries a `CATEGORY` header,
  which filters out unrelated manual dictations.

## State model

`state.json` maps each audio path to `{status, category, timestamp, processed_at, attempts}`.
Statuses: `complete`, `failed_retry` (re-tried next cycle), `failed_permanent` (skipped forever).
Discovery skips `complete` and `failed_permanent`; `failed_retry` is re-picked up automatically.

## Known gaps / improvement opportunities

These are the structural findings from the architectural review. They do **not** block the
core daemon (which is exercised by the test suite), but they are the highest-value cleanups.

1. **`reclassify_and_fix.py` targets a legacy vault layout.** It scans `transcripts/` and
   `analysis/` subfolders, but the current pipeline writes a single `.md` directly into the
   category folder (`save_output`). Its two core operations are also stubbed pending a
   Superwhisper reprocessing entry point, so `fix-analysis` / `fix-categories` / `fix-all`
   are effectively inert. Either realign it to the single-file layout or remove it.
2. **Duplicated discovery/filter logic.** The `.icloud` / `.tmp` / hidden / stability skip
   rules are reimplemented in both `auto_transcribe.discover_audio_files` and
   `ondemand_transcribe.discover_audio_files`. A shared `pipeline` helper would give one
   source of truth and prevent drift.
3. **Dead configuration and code paths.** `retry_backoff` is documented and present in
   `config.example.yaml` but never loaded or applied (retries fire every `scan_interval`
   with no backoff); `failed_analysis_log` is exported but unused; the `transcript_only`
   branch and `--reprocess-partial` flag in `ondemand_transcribe.py` can never trigger.
4. **Naming/identity drift.** The package is `meeting-transcriber`, docstrings say
   "RecordingAnalyser" / "MeetingTranscriber", and the repo is `superwhisper-obsidian-wf`.
   Consolidating on one name would reduce confusion.
5. **Empty `tests/contract/` and `tests/integration/`.** Scaffolding without content; the
   real coverage lives in `tests/unit/`.

## Quality gates

CI (`.github/workflows/tests.yml`) enforces three gates, all of which must stay green:

- `ruff check .` and `ruff format --check .` — lint + formatting
- `mypy .` — static types (with `types-PyYAML`)
- `pytest tests/ --cov` — unit tests

Run them locally before pushing:

```bash
ruff check . && ruff format --check . && mypy . && python -m pytest tests/ -q
```
