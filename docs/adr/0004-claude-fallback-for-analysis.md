# ADR 0004 — Claude API Fallback for Analysis Stage

**Date**: 2026-03-10
**Status**: Accepted
**Deciders**: Owner/operator

---

## Context

`analyze_with_retry()` calls Gemini Pro (analysis model) up to 3 times with
[60, 180, 300]s backoff. When Gemini Pro is overloaded (503 errors), all three
attempts can fail and the file is logged to `failed_analysis.log` with no
analysis written — even though the transcript was successfully saved.

The owner has an active Anthropic (Claude) subscription with independent
capacity from Gemini. Using it as a fallback on Gemini failure allows most
recordings to get analysis without manual reprocessing.

---

## Decision

After the **first** Gemini Pro failure, immediately switch to Claude Sonnet as
fallback. If Claude also fails, retry Claude once more, then return to Gemini
for two final attempts before giving up.

**Attempt sequence (Claude configured)**:

| # | Provider | Wait before |
|---|----------|-------------|
| 1 | Gemini Pro | — |
| 2 | Claude Sonnet | 0s (different provider) |
| 3 | Claude Sonnet | 10s (`CLAUDE_RETRY_BACKOFF`) |
| 4 | Gemini Pro | 0s (different provider) |
| 5 | Gemini Pro | 60s (`ANALYSIS_RETRY_BACKOFF[0]`) |

If `ANTHROPIC_API_KEY` is not set, the original Gemini-only chain
([60, 180, 300]s) is used unchanged.

**Scope**: Analysis stage only. Transcription remains Gemini-only because the
Anthropic API does not accept audio file inputs.

---

## Implementation

- `configure_claude()` in `pipeline.py`: initialises `anthropic.Anthropic`
  client at startup from `ANTHROPIC_API_KEY` env var. If key is absent or
  `anthropic` package is not installed, logs a one-time info/warning and
  sets `_claude_client = None` (service continues normally).
- `analyze_with_claude()` in `pipeline.py`: single attempt using
  `client.messages.create()` with the same `ANALYSIS_PROMPT` as Gemini.
  Catches all exceptions and returns `None` — never raises.
- `analyze_with_retry()` refactored to a schedule-based loop that selects
  provider per attempt. Claude errors are non-fatal; `FatalAPIError` from
  Gemini still stops the service.
- `ANTHROPIC_API_KEY` read from environment only (constitution §IV).
- `CLAUDE_FALLBACK_MODEL` defaults to `claude-sonnet-4-6`; overridable via
  `config.yaml`.
- `anthropic>=0.40.0` added to `pyproject.toml` dependencies.

**Launchd setup (manual)**: Add `ANTHROPIC_API_KEY` to the
`EnvironmentVariables` block in `~/Library/LaunchAgents/com.meetingtranscriber.plist`.

---

## Consequences

**Positive**:
- Most recordings now get analysis even during Gemini Pro outages
- No manual reprocessing from `failed_analysis.log` for transient failures
- Fully opt-in: no key = no change to existing behaviour

**Negative / Trade-offs**:
- Adds a second API dependency and billing surface (Anthropic)
- Total analysis wait time increases in worst case (all 5 attempts fail):
  ~70s extra compared to old 3-attempt chain (worth it given the higher
  success probability)
- `anthropic` package must be installed in the venv

**Risks mitigated**:
- Claude errors can never stop the service (all caught, return None)
- Key absence is a graceful no-op, not an error condition
