# 0003 Category-based routing to separate Obsidian vaults

**Status:** Accepted
**Date:** 2026-02-21
**Project:** RecordingAnalyser

## Context

Transcribed meetings need to be stored where the user can find them in context. The user maintains multiple Obsidian vaults organized by life domain (personal, work teams, music projects). Recordings from different contexts should appear in the relevant vault rather than a single dump folder.

The system needs to:
- Automatically classify recordings by content
- Route output files to the correct Obsidian vault
- Handle misclassification gracefully (reclassification tools)
- Support a fallback for unclassifiable content

## Considered Options

### Option 1: AI-driven classification with keyword-guided prompts
Include category definitions and keywords in the Gemini transcription prompt. The model classifies as part of the transcription call. Output format: `CATEGORY: <name>` in the response.
- Pros: no second API call, classification uses full audio context, keyword hints improve accuracy
- Cons: classification quality depends on prompt engineering, model may hallucinate categories

### Option 2: Post-transcription keyword matching
Transcribe first, then scan the text for category keywords programmatically.
- Pros: deterministic, no AI uncertainty, fast
- Cons: keyword overlap between categories causes false positives, can't use audio context (speaker identification, tone), misses meetings about new topics not in keyword list

### Option 3: Single vault with tag-based organization
Put all files in one vault, use Obsidian tags or folder structure within a single vault.
- Pros: simpler code (no routing logic), no misclassification risk
- Cons: doesn't match user's existing vault structure, forces context-switching between domains in one workspace

## Decision Outcome

Chosen option: **AI-driven classification with keyword-guided prompts**, because it leverages the audio context available during transcription (speaker names, topics, organizational references) to make classification decisions that simple keyword matching can't. The DEFAULT fallback category catches unclassifiable content. Maintenance tools (`reclassify_and_fix.py`, `fix_unknown_meetings.py`) handle misclassification after the fact.

## Consequences

### Positive
- Transcripts appear in the correct Obsidian vault automatically — no manual sorting
- Classification happens in the same API call as transcription — zero latency overhead
- Five categories (PERSONAL, TEAM, WORK, INTERVIEWS, DEFAULT) cover the user's current vault structure

### Negative
- Misclassification rate is non-zero — maintenance tools exist but require manual intervention
- Adding a new category requires updating the `FOLDERS` dict, the transcription prompt keywords, and the vault folder paths
- Category keywords in the prompt are tightly coupled to specific people/projects — when teams change, keywords must be updated

### Neutral
- DEFAULT category acts as a safety net — unclassified files are never lost, just filed in a catch-all location
- The `FOLDERS` dict maps category names to absolute filesystem paths, creating a hard dependency on the vault layout
