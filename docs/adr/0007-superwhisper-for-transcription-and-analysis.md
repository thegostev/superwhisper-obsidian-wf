# 0007 Superwhisper for transcription and analysis

**Status:** Accepted — supersedes 0001, 0004, 0005, 0006 and the ollama-integration proposal
**Date:** 2026-05-03
**Project:** RecordingAnalyser

## Context

The pipeline previously used a multi-stage, multi-provider approach: local Parakeet/mlx-whisper for transcription, then Ollama (primary) or Gemini (fallback) for classification and analysis. This caused several problems:

- **Operational fragility:** Ollama required a warm daemon, GPU memory management, and long inference timeouts (up to 600s). Parakeet crashed the system on long recordings due to uncatchable C++ OOM errors (see post-mortem 26-04-24).
- **Two-stage complexity:** Transcript and analysis were separate pipeline stages with independent failure modes, retry logic, and state tracking.
- **Infrastructure overhead:** Multiple dependencies (`faster-whisper`, `parakeet-mlx`, `mlx-whisper`, `ollama`, `google-genai`, `anthropic`) each requiring separate installation and configuration.
- **API key management:** Gemini and Anthropic keys had to be kept in environment variables and rotated.

Superwhisper is already installed and in daily use on the same machine. It runs Apple Silicon-optimised transcription and supports Custom Modes that apply arbitrary AI instructions to the transcription output — collapsing both pipeline stages into a single handoff.

## Considered Options

### Option 1: Keep multi-stage pipeline (Parakeet + Ollama)
Retain the existing architecture with local transcription and local LLM analysis.
- Pros: fully offline, no dependency on a third-party app, all logic in Python
- Cons: OOM crashes on long recordings, Ollama warm-up and timeout issues, two failure modes, heavy dependency footprint

### Option 2: Superwhisper Custom Mode (single-pass)
Hand audio files to Superwhisper via `open file.m4a -a Superwhisper`. The active Custom Mode transcribes and applies AI instructions in one pass. Python reads the result from `~/Documents/superwhisper/recordings/<id>/meta.json`.
- Pros: eliminates transcription and LLM infrastructure entirely; no API keys; uses hardware-optimised models already running on the machine; single failure point instead of two
- Cons: pipeline depends on Superwhisper being installed and running; result polling is time-based (no direct callback); recording format is internal to Superwhisper and could change

### Option 3: Cloud-only (Gemini Flash for transcription + analysis in one prompt)
Send audio directly to Gemini Flash with a combined transcription + analysis prompt.
- Pros: single API call, no local infrastructure
- Cons: API key required, daily quota limits, 429 errors require manual recovery, vendor lock-in

## Decision Outcome

Chosen option: **Superwhisper Custom Mode (single-pass)**, because it eliminates the two heaviest pain points — local model OOM crashes and Ollama inference instability — without introducing new API dependencies. The machine already runs Superwhisper daily, so the operational cost is zero. Transcription and analysis collapse into one step, removing the partial-failure state where a transcript exists but analysis is missing.

The output contract between Superwhisper and Python is defined in the Custom Mode prompt:

```
CATEGORY: <WORK | TEAM | PERSONAL | INTERVIEWS | DEFAULT>
FILENAME: <meeting title>

<analysis body in Markdown>
```

Python polls `~/Documents/superwhisper/recordings/` for new directories with `meta.json` containing `CATEGORY:`, reads `llmResult`, and routes the file to the correct Obsidian vault folder based on category.

## Consequences

### Positive
- No local model infrastructure (no Parakeet, mlx-whisper, Ollama, GPU memory management)
- No API keys or quota limits
- Single output file per recording (analysis only) — simpler state tracking and vault structure
- Dependency footprint reduced to `PyYAML` only
- OOM crashes and Ollama timeout post-mortems become irrelevant

### Negative
- Pipeline depends on Superwhisper being installed and the "meeting" Custom Mode being configured
- Result detection is time-based polling (no direct callback from Superwhisper to Python)
- `meta.json` schema is internal to Superwhisper and not a public API — could change across app versions
- `--reprocess-partial` and `--reclassify` maintenance operations are no longer supported (re-processing requires the original audio file)

### Neutral
- `delay_between_files` reduced from 90s (Gemini rate limit) to 5s (no API rate limit)
- Output goes directly to category root folder, not a `transcripts/` or `analysis/` subfolder
