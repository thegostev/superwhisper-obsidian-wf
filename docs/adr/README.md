# Architectural Decision Records — SuperwhisperObsidianWF

ADRs capture significant technology and design decisions so they don't get re-debated and so future sessions (human or AI) understand *why* things are the way they are. (Older ADRs predate the project's rename from **RecordingAnalyser**.)

## Current ADRs

- [0001 — Gemini API for audio transcription and analysis](0001-gemini-api-for-transcription.md) *(superseded by 0007, via 0005)*
- [0002 — launchd daemon for continuous transcription service](0002-launchd-daemon-pattern.md) ✅ **current** *(its recorded con, "no built-in health checks", is to be addressed by 0009 and 0010 — decision accepted, implementation still open, tracked as post-mortem 26-09-07 items #4–#10)*
- [0003 — Category-based routing to separate Obsidian vaults](0003-category-based-vault-routing.md) ✅ **current**
- [0004 — Claude fallback for analysis](0004-claude-fallback-for-analysis.md) *(superseded by 0007)*
- [0005 — Gemini Flash primary with local Whisper fallback](0005-gemini-flash-primary-whisper-fallback.md) *(superseded by 0007)*
- [0006 — Classification in analysis stage](0006-classification-in-analysis-stage.md) *(superseded by 0007)*
- [0007 — Superwhisper for transcription and analysis](0007-superwhisper-for-transcription-and-analysis.md) ✅ **current** *(supersedes the un-numbered [ollama-integration.md](ollama-integration.md) proposal)*
- [0008 — Increase Superwhisper timeout for long meeting recordings](0008-increase-superwhisper-timeout-for-long-meetings.md) ✅ **current**
- [0009 — Heartbeat file as the transcriber liveness signal](0009-heartbeat-file-as-liveness-signal.md) ✅ **accepted, not yet implemented**
- [0010 — Preflight wrapper and watchdog agent for launchd self-healing](0010-preflight-wrapper-and-watchdog-self-heal.md) ✅ **accepted, not yet implemented**

Requirements derived from 0009 and 0010 are specified in [`../specs/self-healing-health-check.md`](../specs/self-healing-health-check.md), using RFC 2119 / RFC 8174 (BCP 14) keywords.

> **Implementation status:** 0009/0010 and the spec are accepted decisions. Until post-mortem 26-09-07 items #4–#10 land, the service still has **no liveness signal** — treat it as unmonitored.

## When to create an ADR

- Choosing a new library, API, or service
- Changing a data model or storage approach
- Picking an architectural pattern (e.g., daemon vs cron, sync vs async)
- Any decision you'd want to explain to your future self in 6 months

## How to create one

1. Copy `0000-template.md` to `NNNN-short-title.md` (next sequential number)
2. Fill in all sections — especially Consequences (the part most often skipped)
3. Set Status to `Proposed` while under consideration; flip to `Accepted` once decided

## Statuses

- **Proposed** — under consideration, not yet in effect
- **Accepted** — decided and in effect
- **Deprecated** — abandoned without a replacement; no longer in effect
- **Superseded by [NNNN]** — replaced by a newer ADR (link to the replacement)
