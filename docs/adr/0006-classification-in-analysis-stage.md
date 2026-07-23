# 0006 Classification moved to analysis stage

**Status:** Accepted
**Date:** 2026-04-23
**Project:** RecordingAnalyser

## Context

The original pipeline (ADR 0001) had Gemini Flash perform three tasks in a single prompt: transcribe audio, classify into a category, and generate a filename. This was efficient for the Gemini-only architecture — one API call, one round trip.

Two problems emerged:

1. **Transcription backend lock-in**: Any replacement for Gemini Flash (e.g., local Whisper, ADR 0005) would need to perform classification too. Whisper is an STT model — it produces text, not structured metadata. Classification capability is a hard requirement that eliminated all local STT options.
2. **Prompt coupling**: The transcription prompt contained classification keywords (project names, team names, domain jargon). Adding a new category or updating keywords required touching the transcription prompt, which also affected output format and transcription quality.

## Considered Options

### Option 1: Move classification to the analysis stage
Transcription prompt outputs only raw text. Analysis prompt (Gemini Pro / Claude) additionally outputs `CATEGORY:` and `FILENAME:` headers before the analysis body.
- Pros: transcription is a pure audio→text operation (any STT can be used), classification is co-located with the full semantic understanding of the transcript (the analysis model sees the complete text before classifying), single source of truth for category logic
- Cons: transcript cannot be saved to the correct category folder until analysis completes (minor timing regression — see below)

### Option 2: Add a third classification step
Keep Flash for transcription (text only), add a separate classification call (Flash or Pro), then run analysis. Three sequential API calls per file.
- Pros: separation of concerns at the API level
- Cons: adds cost and latency, third call is another failure surface, still requires Gemini for classification (doesn't enable local STT)

### Option 3: Rule-based local classification
Replace LLM classification with keyword matching in Python code.
- Pros: no API cost, instant, no quota
- Cons: brittle for ambiguous meetings, keyword list becomes code (not config), misses semantic context (e.g. the same name appears in different teams)

## Decision Outcome

Chosen option: **Move classification to the analysis stage**, because it decouples transcription from classification without adding a new pipeline stage. The analysis model (Gemini Pro / Claude) already reads the full transcript — it has more context for classification than the Flash model did during transcription. The `CATEGORY:` and `FILENAME:` header block prepended to the analysis response is a clean output contract (`parse_analysis_response()` in S1.M3).

The timing regression (transcript saved after analysis, not before) is accepted. If analysis fails entirely, the transcript is saved to DEFAULT with a timestamp-only filename and logged for manual follow-up. The audio file remains on disk and will be reprocessed on the next cycle.

## Consequences

### Positive
- Any STT backend (Whisper, cloud STT, future models) can be used for transcription
- Classification benefits from the full transcript context, not just audio characteristics
- Category keyword definitions live in `analysis_prompt` in `config.yaml` — single update point
- `parse_transcription_response()` simplified to `parse_transcript()` (pure text extraction)

### Negative
- Transcript is saved to disk only after analysis succeeds — if the process is killed between transcription and analysis, the transcript text is not persisted (audio file is still present for reprocessing)
- Analysis failures now block both transcript AND analysis output (previously transcript was always saved)
- `analyze_with_retry()` return type changed from `str | None` to `tuple[str, str, str] | None` — a breaking change to any callers

### Neutral
- `reclassify_transcript()` (S2.M3.C1) updated to use `ANALYSIS_MODEL` instead of `TRANSCRIPTION_MODEL`
- `generate_missing_analysis()` updated to unpack the new tuple return type, preserving the existing on-disk category for file placement
- `ondemand_transcribe.py` `reprocess_analysis_only()` updated identically
