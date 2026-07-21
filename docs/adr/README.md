# Architectural Decision Records — RecordingAnalyser

ADRs capture significant technology and design decisions so they don't get re-debated and so future sessions (human or AI) understand *why* things are the way they are.

## Current ADRs

- [0001 — Gemini API for audio transcription and analysis](0001-gemini-api-for-transcription.md) *(superseded by 0007)*
- [0002 — launchd daemon for continuous transcription service](0002-launchd-daemon-pattern.md)
- [0003 — Category-based routing to separate Obsidian vaults](0003-category-based-vault-routing.md)
- [0004 — Claude fallback for analysis](0004-claude-fallback-for-analysis.md) *(superseded by 0007)*
- [0005 — Gemini Flash primary with local Whisper fallback](0005-gemini-flash-primary-whisper-fallback.md) *(superseded by 0007)*
- [0006 — Classification in analysis stage](0006-classification-in-analysis-stage.md) *(superseded by 0007)*
- [0007 — Superwhisper for transcription and analysis](0007-superwhisper-for-transcription-and-analysis.md) ✅ **current**

## When to create an ADR

- Choosing a new library, API, or service
- Changing a data model or storage approach
- Picking an architectural pattern (e.g., daemon vs cron, sync vs async)
- Any decision you'd want to explain to your future self in 6 months

## How to create one

1. Copy `0000-template.md` to `NNNN-short-title.md` (next sequential number)
2. Fill in all sections — especially Consequences (the part most often skipped)
3. Set Status to `Accepted`

## Statuses

- **Proposed** — under consideration
- **Accepted** — decided and in effect
- **Deprecated** — replaced by a newer ADR
- **Superseded by [NNNN]** — link to the replacement
