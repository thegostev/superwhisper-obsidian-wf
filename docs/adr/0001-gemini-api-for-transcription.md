# 0001 Gemini API for audio transcription and analysis

**Status:** Superseded by 0005 (transcription), partially active (analysis)
**Date:** 2026-02-21
**Project:** RecordingAnalyser

## Context

RecordingAnalyser needs to convert audio recordings (.m4a from Just Press Record) into structured Markdown transcripts and meeting notes. The system must:
- Transcribe audio with speaker identification and timestamps
- Classify recordings into categories based on content
- Generate analytical summaries (decisions, action items, concerns)
- Run continuously as a background service processing ~5-15 files/day

A two-stage approach was needed: a fast model for transcription + classification, and a stronger model for analytical summaries.

## Considered Options

### Option 1: Google Gemini API (Flash + Pro models)
Native multimodal support — accepts audio files directly without a separate speech-to-text step. Flash model handles transcription + classification cheaply and quickly. Pro model handles deeper analysis. Single API, single SDK (`google-generativeai`).
- Pros: direct audio input, two model tiers match the two-stage pipeline, generous free/personal tier
- Cons: rate limits on Pro tier require spacing calls (90s between files), API still in preview (model names change)

### Option 2: OpenAI Whisper + GPT-4
Whisper for speech-to-text, then GPT-4 for classification + analysis. Well-documented, widely used.
- Pros: Whisper is accurate and fast, GPT-4 is strong at analysis
- Cons: two separate APIs/SDKs, Whisper is text-only (loses audio context for classification), higher cost at volume

### Option 3: Local Whisper + cloud LLM
Run Whisper locally for transcription, use any cloud LLM for analysis. No API costs for transcription.
- Pros: free transcription, works offline
- Cons: requires GPU or slow CPU inference, adds deployment complexity for a launchd service, still needs cloud for analysis

## Decision Outcome

Chosen option: **Google Gemini API (Flash + Pro)**, because the native multimodal audio support eliminates the need for a separate speech-to-text pipeline. A single API call to Flash handles transcription, classification, and filename generation simultaneously. The two-tier model approach (Flash for cheap/fast transcription, Pro for deeper analysis) maps naturally to the pipeline stages. The personal-tier pricing is adequate for the expected volume.

## Consequences

### Positive
- Single SDK dependency (`google-generativeai`)
- Audio classification happens in the same call as transcription — no second round-trip
- Flash model is fast enough for near-real-time processing (30s scan intervals)

### Negative
- Pro-tier rate limits require 90s delays between files and aggressive retry backoff [60, 180, 300]s
- Preview model names (`gemini-3-flash-preview`, `gemini-3-pro-preview`) may change, requiring code updates
- Vendor lock-in to Google's API — switching would require rewriting the entire pipeline

### Neutral
- API key management is the same regardless of provider choice
