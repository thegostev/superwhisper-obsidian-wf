# 0009 Heartbeat file as the transcriber liveness signal

**Status:** Accepted — implemented (post-mortem 26-09-07 items #4–#10)
**Date:** 2026-09-07
**Project:** SuperwhisperObsidianWF

## Context

On 2026-09-07 the transcriber daemon was dead for approximately eight hours with no visible signal. A `brew upgrade` on 2026-08-17 had half-completed, leaving a Python 3.13.15 executable inside the 3.13.13 Cellar keg whose `install_name` referenced a framework that did not exist. Because dyld resolves shared libraries at exec time, the already-running daemon survived the breakage untouched. When the process ended at 04:31, every launchd `KeepAlive` respawn aborted immediately with `dyld: Library not loaded`. A meeting recorded at 10:31 and synced at 11:28 was never picked up. The outage was discovered only because an expected transcript did not appear in the vault.

ADR 0002 recorded "no built-in health checks" as a known consequence of the launchd daemon pattern. The 2026-07-23 post-mortem sharpened this into Lesson 3: *"A process supervisor's view of health is necessary but not sufficient. `launchctl` saw a live PID and a zero exit code for 21 hours while data was being lost. Process-level liveness and data-level correctness are different axes."*

Before any self-healing mechanism can be built, one question must be answered: **what signal reliably distinguishes a working daemon from a dead one?** The 2026-09-07 incident is an unusually good test case, because the most obvious candidate signals were all actively lying during it.

## Considered Options

### Option 1: Log file mtime
Treat `~/Library/Logs/superwhisper-transcriber/transcriber.log` as fresh if it was written recently.
- Pros: zero code changes; the file already exists and is already written by every cycle
- Cons: catastrophically wrong. During the outage the log was being written *faster than at any point in normal operation* — 486 dyld abort lines (162 abort blocks) over the 8h19m window, ≈ one respawn attempt every 3 minutes, produced by the failure itself. Log freshness proves the supervisor is alive, not the daemon. Any check built on this signal would have reported the service healthy for the entire outage.

### Option 2: Tail-parse the log for the idle heartbeat line
Read the tail of the log and look for a recent `[Cycle N] HH:MM:SS - No new files` line emitted by `IDLE_HEARTBEAT_EVERY_N_CYCLES`.
- Pros: no daemon changes; the line is already emitted every ten idle cycles
- Cons: the line prints only when idle, never while a file is being processed, so a busy daemon looks dead; the timestamp carries no date, making comparisons ambiguous across midnight; it requires seeking the tail of an unbounded, unrotated file; and it re-couples health to the log, which is the exact channel that produced the false-healthy signal in Option 1.

### Option 3: State file mtime
Treat `~/.superwhisper_transcriber_state.json` as a liveness signal.
- Pros: trivially available; already written atomically
- Cons: the state file changes only when a recording is processed. A weekend with no recordings is indistinguishable from an eight-hour outage. Guaranteed false positives, and the false-positive rate is highest exactly when the user is least likely to be watching.

### Option 4: `launchctl list` PID and last exit status
Treat an absent PID or a non-zero last exit status as unhealthy.
- Pros: no daemon changes; directly reflects the supervisor's own view
- Cons: both columns mislead. The last exit status is stale — a healthy running daemon reports `8078  -15`, a live PID beside a non-zero exit code left over from the previous run, so any check keyed on it would flag a working service indefinitely after a single `kickstart -k`. The PID column is equally unreliable in the other direction: under `ThrottleInterval 10` a crash-looping job has a live process for a few milliseconds every ten seconds, so a sampling check can catch a PID that is about to abort. Both problems are structural, not tuning issues.

### Option 5: Heartbeat file written by the daemon
Have the daemon write a small JSON file at known intervals, and treat staleness of that file as the health signal.
- Pros: written only by Python code executing inside the running daemon, so a dyld-aborting interpreter can never produce one; independent of whether any recordings exist, eliminating the quiet-weekend false positive; carries structured diagnostic context (phase, cycle number, PID, counts) rather than a bare timestamp; is the data-level axis the 2026-07-23 post-mortem asked for
- Cons: requires touching the daemon, including the hot Superwhisper poll loop; introduces a new file whose schema must stay compatible between a 3.13 writer and a 3.9 reader; adds one more machine-local file that must not be committed

## Decision Outcome

Chosen option: **heartbeat file written by the daemon**, because it is the only candidate that was not actively lying during the 2026-09-07 outage. Options 1 and 4 would have reported the service healthy throughout; Option 3 produces false positives whenever recording activity is low; Option 2 inherits the defects of Option 1 while adding parsing fragility.

The heartbeat is written to `~/.superwhisper_transcriber_heartbeat.json` — a sibling of `state_file`, deliberately outside the repository working tree so that a venv rebuild can never clobber it. A new `heartbeat_file` config key follows the existing `state_file` pattern through `config.py`.

```json
{
  "schema": 1,
  "pid": 8078,
  "phase": "starting" | "scanning" | "processing" | "fatal",
  "cycle": 4321,
  "updated_at": "2026-09-07T14:31:02Z",
  "started_at": "2026-09-07T02:31:10Z",
  "state_complete": 316,
  "failed_permanent": 0,
  "fatal_reason": "FatalAPIError: superwhisper_mode_key is empty"
}
```

`updated_at` and `started_at` are RFC 3339 UTC with explicit offset. Naive local timestamps would repeat, inside this schema, the exact defect used to reject Option 2 — an ambiguous comparison across midnight — and add a DST one: a spring-forward change makes a 1-second-old heartbeat look an hour old. Staleness is nevertheless judged on the file's mtime (kernel-written, immune to the writer's clock formatting); the embedded timestamps are diagnostic. `fatal_reason` is present only for `phase: "fatal"`, which marks the service unrecoverable by restart (ADR 0010, PF-16).

Including `failed_permanent` closes open action item #6 from the 2026-07-23 post-mortem ("Emit `failed_permanent` count in idle heartbeat") at a cost of two lines.

**Write cadence is the load-bearing detail.** A heartbeat written once per scan cycle would leave a legitimately busy daemon silent for up to `SUPERWHISPER_TIMEOUT (3600) × MAX_FILES_PER_CYCLE (5)`, roughly five hours. That forces a choice between a uselessly long staleness threshold and a watchdog that kills the daemon mid-transcription — the latter reproducing the 2026-07-23 "recovery action destroys in-flight work" failure at a higher level. The heartbeat is therefore also written from inside `wait_for_superwhisper_result`'s poll loop, which already runs every `SUPERWHISPER_POLL_INTERVAL` (3 s). Two further busy windows get call sites so the 300 s threshold cannot be breached by a healthy daemon: a forced write at the top of each `handoff_to_superwhisper` and inside any inter-file idle-wait loop (`_wait_for_superwhisper_idle`), and throttled writes inside the `build_transcript_index()` walk so a slow iCloud-backed index build cannot straddle its surrounding startup milestones. A single staleness threshold then covers idle, busy, handoff, and slow-start states, and the watchdog needs no phase-dependent staleness logic — the sole exception is `phase: "fatal"`, which drives a restart decision, not a staleness one (PF-16).

The writer is self-throttling (`write_heartbeat(phase, min_interval=15.0, force=False)`) so call sites need not reason about write amplification. Four forced writes during startup — configuration loaded, state loaded, transcript index built, and scan loop entered — cover the window in which `build_transcript_index()` walks iCloud-backed vault folders and may stall, so a slow start is not mistaken for a dead daemon. There is no `interpreter start` milestone: the writer runs under the daemon's venv interpreter and cannot execute before the preflight has validated it (PF-2).

The writer lives in `pipeline.py` beside `save_state()`, reusing its temp-file-plus-`os.replace` atomicity idiom and its "a failed state write must never kill the daemon" error handling. The reader deliberately shares no code with it (see ADR 0010); the JSON schema is the contract between them, pinned by a round-trip test.

Staleness threshold: 300 seconds, roughly ten times the 30 s idle cycle and twenty times the throttle interval.

## Consequences

### Positive
- The signal cannot be forged by the failure mode it is meant to detect — an interpreter that cannot start never writes a heartbeat
- No false positives during quiet periods, unlike any state- or output-derived signal
- Worst-case detection latency drops from eight hours to roughly ten minutes
- Closes post-mortem action item #6 and addresses the gap ADR 0002 recorded
- The `phase` and `cycle` fields make "wedged mid-transcription" distinguishable from "never started" during diagnosis

### Negative
- Adds a write to the hot Superwhisper poll loop, mitigated by the 15-second throttle
- Introduces a schema contract spanning two Python versions, since the reader must run on the system interpreter (ADR 0010)
- One further machine-local JSON file that is not in version control and must be kept out of it
- **The heartbeat proves an instance of the daemon is running, not that the launchd-managed instance is.** A manually started second daemon (the documented `run_transcriber.sh start` dual-daemon hazard) shares `heartbeat_file` through `config.py` and would keep the heartbeat fresh while the launchd service is dead. The reader deliberately does not compare the heartbeat's `pid` against `launchctl list`: that couples detection back to the launchctl column this ADR rejects as unreliable. The accepted mitigations are the PID-file guard in `run_transcriber.sh` and the WD-10 pause sentinel; the residual risk is recorded rather than engineered away.
- A daemon that persistently cannot write the heartbeat (disk full, permission change) is indistinguishable from a dead one; HB-10 requires a distinct warning surface rather than a new signal.

### Neutral
- `config.example.yaml` gains a documented `heartbeat_file` key
- The existing `IDLE_HEARTBEAT_EVERY_N_CYCLES` log line is unchanged and remains a human-facing convenience, no longer load-bearing for health
- The heartbeat file is safe to delete at any time; the daemon recreates it on the next cycle and the watchdog treats a missing file as unhealthy for at most one tick after a restart
