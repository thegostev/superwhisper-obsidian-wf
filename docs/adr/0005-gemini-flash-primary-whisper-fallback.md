# 0005 Gemini Flash primary transcription with local Whisper fallback

**Status:** Accepted
**Date:** 2026-04-24
**Project:** RecordingAnalyser

## Context

ADR 0001 chose Gemini Flash for transcription because it accepted audio files natively and produced high-quality, speaker-attributed transcripts without a separate speech-to-text step. Moving classification to the analysis stage (ADR 0006) turned transcription into a clean audio→text operation, which also made it possible to substitute a local model when the cloud is unavailable.

Two constraints shape the design:

1. **Gemini Flash is the best primary**: faster than local models, handles speaker diarization cues in the prompt, and quota issues are recoverable (next UTC day for free tier, or upgrade to paid).
2. **Local fallback is required for resilience**: quota exhaustion, transient network errors, and billing lapses should not block transcription entirely when audio files are sitting on disk ready to process.

## Decision Outcome

**Gemini Flash is the primary transcription model.** After 2 consecutive Gemini failures for a given file, the pipeline falls back to the local Parakeet → mlx-whisper chain already configured in `config.yaml`.

Failure types that bypass the fallback and stop immediately:
- `FatalAPIError` — bad API key or no permissions (retrying or falling back locally won't help; operator action required)
- `PermanentFileError` — Gemini rejected the file as invalid audio (local model will likely also fail)

All other errors (quota 429, 503 overload, network timeout) exhaust both Gemini attempts before triggering local fallback.

## Consequences

### Positive
- Transcripts use cloud quality (Gemini Flash) under normal conditions
- No transcription blockage from transient API failures or quota exhaustion
- Local model loaded on startup is available instantly when needed — no cold-start delay during a fallback

### Negative
- `GEMINI_API_KEY` environment variable is required; service exits at startup if absent
- Two Gemini failures add up to ~10 seconds of retry delay before local fallback begins
- Free-tier quota (20 RPD) can still be exhausted; once both Gemini attempts fail due to 429, local model handles remaining files until quota resets

### Neutral
- `google-generativeai` SDK and `faster-whisper`/`parakeet-mlx`/`mlx-whisper` packages are all required
- `whisper_backend`, `whisper_model`, `whisper_fallback_model` config keys remain — they now configure the fallback path, not the primary
- Analysis stage (Ollama) is unaffected by this decision
