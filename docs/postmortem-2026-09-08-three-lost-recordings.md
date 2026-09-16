# Postmortem: three long recordings lost to the empty-stub race

**Date:** 2026-09-08 · **Service:** SuperwhisperObsidianWF (`com.alex.transcriber`)
**Impact:** 3 long recordings never produced vault notes. Re-drive on 2026-09-08 recovered 2 of 3 (`16-38-20.m4a` → `26-07-24 16.38 - Sex - Good Day, Sex.md`; `19-16-32.m4a` → `26-08-27 19.16 - Untitled - No substantive topic.md`). The third (`09-49-10.m4a`) is unprocessable through Superwhisper: a deterministic, file-specific cloud-transcription failure — not a stub race. Audio intact in iCloud.
**Related:** 2026-07-24 incident (4 misrouted/lost meetings). This is the same root cause, larger amplitude.

## Timeline

| Date | Event |
|---|---|
| 2026-07-24 19:35 | `2026-07-24/16-38-20.m4a` (3h22m, 95 MB) fails attempt 1: empty Superwhisper stub, meta.json stable 120 polls. Sibling file `20-16-27.m4a` in the same 5-file batch succeeds seconds later (result dir 1785001553). |
| 2026-07-24 (same cycle+) | Attempts 2–3 fail identically (new stubs each time, dirs 1785005998 / 1785006468). Entry becomes `failed_permanent`. The 3h22m recording is silently done. |
| 2026-08-27 19:16 | `2026-08-27/19-16-32.m4a` (86 min) hands off; Superwhisper does not return a result within 3600 s. Marked `failed_retry`, attempts=1. |
| ≤ 2026-09-03 | Aug 27 falls out of `scan_days_back=7`. The scanner never retries it again; the salvage pass covers `failed_permanent` only. Entry is orphaned. |
| 2026-08-31 ~09:49 | `2026-08-31/09-49-10.m4a` (84 min, 39 MB) fails 3× with the same empty-stub signature → `failed_permanent`. |
| 2026-09-08 07:01 | Health check surfaces stale-code daemon + the 3 orphans. Daemon restarted on fresh code (health → healthy), then stopped for a paced re-drive. |
| 2026-09-08 07:19–07:29 | Re-drive pass 1: `16-38-20` recovered in ~2 min (3h22m audio); `09-49-10` empty-stub failure again; `19-16-32` recovered. |
| 2026-09-08 07:29–07:35 | Re-drive pass 2, Superwhisper idle 10+ min: `09-49-10` fails identically — the "busy with prior handoff" theory is dead for this file. |
| 2026-09-08 07:37 | Stub `meta.json` forensics: duration 5066000 ms fully registered, `segments: []`, `rawResult` empty — Deepgram Nova 3 (cloud) returned nothing. ffmpeg volumedetect: mean −33.6 dB, max −0.6 dB, 75 silence gaps ≥3 s — speech-like audio. Disposition: deterministic cloud-transcription failure. State error field corrected; recorded log message ("file-open likely arrived while busy") was a misdiagnosis. |

## Root causes

**C1 — empty-stub handoff race (known debt R3/R5, still unfixed).**
`open file -a Superwhisper` while Superwhisper is busy (or just launched) creates an abandoned stub: `duration: 0`, no `llmResult`. The poller's stability fast-fail then declares the handoff dead. Each retry created a *new* stub, so the file was effectively racing a scheduler that never gave it a slot. The Jul 24 batch proves it: the sibling file succeeded immediately after — Superwhisper had capacity for one file, and it went to whichever handoff arrived second.

**C4 — empty transcription result is indistinguishable from an abandoned stub (new finding, no R-number).**
When Deepgram Nova 3 (cloud) returns nothing for a file, Superwhisper writes a recording dir with the full duration registered but `segments: []` and empty `result` — byte-for-byte the shape the poller reads as "abandoned stub". The log message blames the handoff race, which sent triage down the wrong path for 9 days (Aug 31 → Sep 8). The poller cannot currently distinguish "stub never processed" from "transcription produced empty output", so a file-specific cloud-model failure is misfiled as a concurrency bug and retried forever. A speech-level check of the source audio (volumedetect) is what separated the two.

**C2 — `failed_retry` entries can age out of the scan window (new finding, no R-number).**
`scan_days_back=7` bounds scanner retries, but `recover_failed_permanent()` (R8 salvage) only re-checks `failed_permanent`. A `failed_retry` entry that leaves the window with attempts < 3 is orphaned silently. This is how `19-16-32.m4a` (Aug 27, timed out once, attempts=1) got stranded — 12 days with zero attempts made.

**C3 — detection gap.** Both losses were invisible: `failed_permanent` entries printed one log line every ~5 min but nothing escalated, and nothing at all surfaced `failed_retry` + out-of-window. The heartbeat/health work (2026-09-07) now counts `failed_permanent` in the heartbeat, but does not yet flag the orphaned-`failed_retry` case.

## Contributing factors

- Long audio raises the race probability: a 3h22m file holds Superwhisper busy far past the 5 s file pacing, so any concurrent handoff lands as a stub.
- The Jul 24 batch ran with `MAX_FILES_PER_CYCLE=5` — 5 paced handoffs against a single-slot Superwhisper.
- 3600 s timeout (ADR 0008) is generous but the Aug 27 timeout consumed the only retry window that day.

## What worked

- R8 salvage + R6 scaled poll budgets + state-backed dedupe behaved as designed; no wrong-vault writes, no duplicates, no double-processing.
- Post-incident health tooling immediately exposed the stale daemon (`heartbeat_missing`) and the stuck backlog counts.
- Manual triage of the 9-entry `failed_permanent` backlog (2026-07-25) correctly ruled 6 entries unprocessable — the re-drive was scoped to 3 files to avoid re-opening triaged clips.

## Action items

| # | Action | Status |
|---|---|---|
| A1 | Re-drive the 3 files sequentially via `redrive_ops.py` (file-list based; window-based `--catchup` would re-open triaged clips) | done — 2/3 recovered, 3rd dispositioned (C4) |
| A2 | Salvage pass must also pick up `failed_retry` entries that fell out of `scan_days_back` (extend `recover_failed_permanent()` or add window-independent retry sweeper) | open |
| A3 | Verify Superwhisper idle (`processingTime > 0` on latest recording, or app-running + no fresh stub) before each handoff — cheap fix for C1 | open |
| A4 | Heartbeat should report `failed_retry` count and flag out-of-window `failed_retry` as unhealthy | open |
| A5 | Long files (>1h) should get an explicit "handoff when idle" gate: skip-and-defer rather than burn an attempt on a stub | open |
| A6 | Poller: when a stub has `duration > 0` but `segments == []` and empty `result` after the stability window, classify as permanent *empty-transcription* error with a distinct log line — don't mislabel as stub race (fixes C4's misdiagnosis) | open |
| A7 | Optional local-whisper fallback for files the cloud model returns empty on (no whisper tooling installed at present) | open |

## Residual risk

The empty-stub race is still live for concurrent handoffs (daemon cycles with multiple files). Until A3/A5 land, any batch containing a long file can lose the rest of the batch to stubs — exactly the Jul 24 and Aug 31 pattern.