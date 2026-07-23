# 0008 Increase Superwhisper timeout for long meeting recordings

**Status:** Accepted
**Date:** 2026-05-03
**Project:** RecordingAnalyser

## Context

The `superwhisper_timeout` setting controls how long the pipeline waits for Superwhisper to complete transcription + AI analysis before marking the file `failed_retry`. The original value of 300 seconds (5 minutes) was sized for short dictations. In practice, the pipeline is also used for full meeting recordings — sessions ranging from 60 minutes to 3 hours.

On Apple Silicon, Whisper transcription runs at roughly 10–20× real-time. A 3-hour recording therefore takes 9–18 minutes to transcribe, plus additional time for the Custom Mode LLM analysis pass. Total processing time can easily reach 20–35 minutes, well beyond 300 s. The pipeline was marking long recordings as `failed_retry` on every attempt, triggering retries that also timed out, and eventually promoting files to `failed_permanent` — meaning they were silently dropped.

## Considered Options

### Option 1: Keep 300 s (status quo)
Leave the timeout unchanged and require users to manually re-drop long recordings.
- Pros: none — the problem is observable and reproducible
- Cons: every recording over ~25 minutes reliably fails

### Option 2: 1800 s (30 minutes)
A moderate increase that covers most 60-minute meetings on fast hardware.
- Pros: smaller blast radius if Superwhisper genuinely hangs
- Cons: still insufficient for 3-hour meetings on slower inference paths

### Option 3: 3600 s (60 minutes)
One hour per file. Covers a 3-hour recording with substantial headroom on any realistic Apple Silicon hardware.
- Pros: handles all current meeting lengths; round number; still acts as a safety net for genuine hangs
- Cons: a hung Superwhisper process would hold a pipeline slot for up to 60 minutes before the retry machinery kicks in (acceptable given `max_files_per_cycle = 5` and `max_retries = 3`)

## Decision Outcome

Chosen option: **3600 s (60 minutes)**, because it reliably covers the longest recorded meeting length (3 hours) across the expected hardware performance range, while still bounding the worst-case hang duration at 1 hour. No code changes are required — `config.py` reads the value from `config.yaml` at startup and passes it directly to `pipeline.py`.

## Consequences

### Positive
- Long meeting recordings (60 min – 3 h) complete successfully instead of exhausting retries
- No code changes — the fix is a single config line

### Negative
- A genuinely stuck Superwhisper process delays the retry cycle by up to 60 minutes instead of 5

### Neutral
- `config.example.yaml` updated to document the new default
- CLAUDE.md recovery section updated to reference 3600 s
