# Self-healing health check — requirements

**Status:** Accepted
**Date:** 2026-09-07
**Project:** SuperwhisperObsidianWF
**Implements:** ADR 0009 (heartbeat liveness signal), ADR 0010 (preflight wrapper and watchdog agent)

**Implementation status:** decision accepted, **not yet implemented** — no artefact of this specification (writer calls, `health_check.py`, `preflight.sh`, plist templates, watchdog agent) exists yet. Tracked as post-mortem 26-09-07 action items #4–#10. The traceability table below is therefore *planned* verification.

## Conventions

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in BCP 14 [RFC 2119] [RFC 8174] when, and only when, they appear in all capitals, as shown here.

Requirement identifiers are stable. Tests SHOULD reference them by identifier so that a requirement change surfaces as a test change.

## Scope

This document specifies the behaviour of three cooperating components:

| Component | Artefact | Runs under |
|---|---|---|
| Heartbeat writer | `pipeline.py`, `auto_transcribe.py` | the daemon's venv interpreter |
| Health check and watchdog | `health_check.py` | `/usr/bin/python3` (system, 3.9.6) |
| Preflight guard | `preflight.sh` | `/bin/zsh`, invoked by launchd |

Out of scope: rotation of the daemon log, reconciliation of vault outputs against state (covered by `verify_integrity.py`), and any repair of the Homebrew installation itself.

---

## HB — Heartbeat

- **HB-1** The daemon MUST attempt a heartbeat write at least once per scan cycle and on every Superwhisper poll iteration while awaiting a result. The HB-8 throttle MAY suppress the underlying file write, provided the file never exceeds the staleness threshold (HC-14) in either the idle or the busy state.
- **HB-2** The heartbeat write MUST be atomic: a temporary file in the same directory, then `os.replace`.
- **HB-3** A failed heartbeat write MUST NOT terminate or interrupt the daemon. It MUST emit a `⚠️` warning and continue.
- **HB-4** The heartbeat file MUST reside outside the repository working tree.
- **HB-5** The heartbeat MUST carry a `schema` integer. A reader encountering an unrecognised `schema` MUST report `heartbeat_schema_unknown` and MUST NOT treat the service as healthy.
- **HB-6** The heartbeat MUST include the current `failed_permanent` count.
- **HB-7** The daemon SHOULD write a forced heartbeat at each of the fixed startup milestones — configuration loaded, state loaded, transcript index built, scan loop entered — before the scan loop begins, so that a slow `build_transcript_index()` over iCloud-backed folders is not mistaken for a dead daemon. There is deliberately no `interpreter start` milestone: the writer runs under the daemon's venv interpreter, which by construction cannot execute until the preflight (PF-2) has validated or rebuilt it. The index build SHOULD also emit throttled heartbeats during the walk (HB-8), so a build longer than the staleness threshold cannot straddle the surrounding milestones.
- **HB-8** The writer SHOULD throttle itself to at most one write per 15 seconds unless the caller forces the write, so that call sites need not reason about write amplification.
- **HB-9** The heartbeat MUST record `updated_at` and `started_at` as RFC 3339 timestamps in UTC with an explicit offset (e.g. `2026-09-07T14:31:02Z`). Naive local timestamps are forbidden: they are ambiguous across DST transitions.
- **HB-10** A failed heartbeat write MUST NOT terminate or interrupt the daemon beyond HB-3's warning. The writer MUST count consecutive write failures, and after a threshold of 4 consecutive failures SHOULD log a `⚠️` warning naming the likely cause (disk full, permission denied): a daemon that cannot write its heartbeat is indistinguishable from a dead one to the reader, and the runbook response is to check disk and permissions before trusting a restart as the fix.
- **HB-11** The daemon SHOULD write a forced heartbeat (phase `processing`) at the top of each file handoff and inside any inter-file idle-wait loop, so the handoff and idle-wait windows cannot breach the staleness threshold on a healthy busy daemon.

### Heartbeat schema, version 1

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

`fatal_reason` is present only when `phase` is `fatal`. `phase` and `cycle` are otherwise diagnostic only, and readers MUST NOT vary their staleness threshold by `phase` — except that `phase: "fatal"` carries normative meaning: it marks the service unrecoverable by restart (PF-16).

---

## HC — Health assessment

- **HC-1** The health check MUST classify the service unhealthy when the heartbeat file is absent, unreadable, of unknown schema, or older than the configured maximum age. Staleness MUST be judged on the heartbeat file's modification time (kernel-written, immune to the writer's clock formatting); the embedded `updated_at` is diagnostic only (HB-9).
- **HC-2** The health check **MUST NOT** use the daemon log file's mtime, size, or contents as a health signal. During the 2026-09-07 outage the log was written at a higher rate than in normal operation, by the failure itself.
- **HC-3** The health check **MUST NOT** use the state file's mtime as a health signal. The state file changes only when a recording is processed, so a period without recordings is indistinguishable from an outage.
- **HC-4** The health check MUST NOT classify the service unhealthy solely because `launchctl list` reports a non-zero last exit status. That column is stale: a healthy running daemon can report a live PID beside a non-zero exit from a previous run.
- **HC-5** The health check MUST NOT classify the service healthy solely because `launchctl list` reports a live PID. Under `ThrottleInterval`, a crash-looping job briefly has a live process on each respawn.
- **HC-6** The health check MUST match the service label by exact equality against the label column of `launchctl list`, never by substring.
- **HC-7** `assess_health` and `decide_action` MUST be pure functions of their arguments. They MUST perform no I/O and MUST NOT execute subprocesses.
- **HC-8** With no action flag supplied, the tool MUST be read-only: it MUST NOT restart the service, write state, or send notifications.
- **HC-9** The tool MUST support `--dry-run`, which MUST suppress restart, notification, and all state writes while printing the decision that would have been taken.
- **HC-10** The tool MUST exit `0` when healthy or when no action was warranted, `1` when unhealthy with a repair attempted or suppressed, `2` when escalated, and `3` on internal error.
- **HC-11** `health_check.py` MUST import and execute under the macOS system Python (3.9.6). It MUST NOT import `config.py`, `pipeline.py`, or any third-party package.
- **HC-12** `health_check.py` MUST use `from __future__ import annotations` so that the project's `X | None` annotation style does not evaluate at runtime under 3.9.
- **HC-13** The health report MUST distinguish `service_not_loaded` from other unhealthy reasons, because it maps to a different action (WD-9).
- **HC-14** The maximum heartbeat age MUST default to 300 seconds and MUST be an explicit configuration input to `health_check.py`. The default derives from the busy-state heartbeat cadence: several multiples of the HB-8 write throttle (15 s) plus the inter-call-site gap (HB-11), rounded up to a whole scan cycle (30 s).
- **HC-15** The verdict MUST be *unhealthy* if and only if the heartbeat is missing, unreadable, of unknown schema, or stale (HC-1), the service label is absent (`service_not_loaded`, HC-13), or the heartbeat is fresh (age below the staleness threshold) with `phase: "fatal"` (`heartbeat_fatal` — PF-16). No other `launchctl list` observation MAY affect the verdict; launchctl data is corroborating and diagnostic only. A fresh heartbeat beside a missing PID MUST be reported as a `pid_missing_warning` in the report but MUST NOT by itself make the verdict unhealthy (HC-5).
- **HC-16** The daemon's scan loop MUST check the watchdog state file's `last_run_at` (WD-3) and MUST emit a `⚠️` warning when it exceeds twice the watchdog interval (WD-11). This is detection only: the daemon MUST NOT restart or bootstrap the watchdog, and the check MUST NOT feed into the heartbeat, the health verdict, or any escalation.

### Reason codes

`heartbeat_missing`, `heartbeat_unreadable`, `heartbeat_schema_unknown`, `heartbeat_stale`, `service_not_loaded`, `heartbeat_fatal`.

`heartbeat_fatal` denotes a fresh heartbeat with `phase: "fatal"` (PF-16). It maps to escalate-without-restart, not to the restart ladder.

---

## WD — Watchdog

- **WD-1** The watchdog agent MUST be scheduled with `StartInterval` and MUST NOT declare `KeepAlive`. `KeepAlive` on a run-once script is an immediate infinite loop.
- **WD-2** The watchdog MUST NOT target its own label for restart. It MUST raise an error if the target label equals its own.
- **WD-3** The watchdog MUST persist consecutive-failure counters and the last action and notification timestamps across invocations, in an atomically written JSON file outside the repository. The state file MUST also hold the notification-cooldown timestamp shared with the preflight (ES-3).
- **WD-4** A tick that observes a stale heartbeat (HC-1) MUST be observational — the watchdog MUST NOT restart or escalate, MUST reset its consecutive-failure counters, and MUST record the run — when the elapsed time since the watchdog's previous run exceeds the staleness threshold AND the heartbeat's age does not exceed that gap by more than one scan cycle (30 s): staleness fully explained by the gap itself means the daemon was suspended alongside the watchdog, because `StartInterval` does not fire while the machine sleeps and launchd fires the missed run immediately on wake — acting on that first tick would kickstart a merely suspended daemon, possibly mid-transcription. A stale heartbeat older than the gap by more than one scan cycle proves the daemon was already failing before the machine slept, and the tick decides normally. A fresh heartbeat always decides normally, so the recovery path (ES-4) is unaffected by jitter. The reset MUST NOT clear an escalation already sent in the current episode — only a healthy tick clears escalation state (ES-4).
- **WD-5** On the watchdog's first run, when no persisted state exists, it MUST skip the tick and record the run.
- **WD-6** The watchdog MUST attempt at most two consecutive restarts per continuous unhealthy episode, after which it MUST escalate. It MUST then enter a bounded slow retry: at most one restart per hour while unhealthy, so recovery resumes automatically once a transient cause (for example an offline venv rebuild) clears — the PF-8 rebuild cooldown and ES-3 notification cooldown already bound the storm. An indefinite stop is reserved for the pause sentinel (WD-10); `service_not_loaded` remains escalate-only (WD-9).
- **WD-7** Every subprocess the watchdog spawns MUST be given an explicit timeout. launchd will not start a second instance of the same job, so a hung watchdog never ticks again.
- **WD-8** All subprocess executables MUST be referenced by absolute path.
- **WD-9** On `service_not_loaded` the watchdog MUST NOT attempt a restart; it MUST escalate. `launchctl kickstart` against an unbootstrapped label fails.
- **WD-10** The watchdog MUST honour a pause sentinel file, and when it is present MUST exit `0` without taking any action. Manual service maintenance (for example `bootout`/`bootstrap` during deployment) MUST place the sentinel, because WD-9's escalate-only rule would otherwise turn every planned unload into a false escalation.
- **WD-11** The watchdog MUST run with `StartInterval` 300 seconds, equal to the default staleness threshold (HC-14), for a worst-case detection latency of interval + threshold ≈ 10 minutes.
- **WD-12** If the persisted watchdog state file is unreadable or corrupt, the watchdog MUST treat the invocation as a first run (WD-5) and overwrite the state at the end of the tick. A failed state write MUST NOT abort the tick and MUST be logged.

---

## PF — Preflight

- **PF-1** The main launchd job MUST invoke the preflight wrapper, and the wrapper MUST replace itself with the daemon via `exec`, so that launchd tracks the daemon's own PID and signals reach it.
- **PF-2** The preflight MUST validate the interpreter by executing it, and MUST NOT infer health from file existence, permissions, or the contents of `pyvenv.cfg`.
- **PF-3** The preflight's probe MUST NOT import `config.py`. A missing or malformed configuration is not an interpreter fault and MUST NOT trigger a rebuild.
- **PF-4** On probe failure the preflight MUST select the first candidate interpreter that itself passes the candidate probe (PF-13), in the fixed order of PF-4a. The system interpreter is included in the list and rejected by the version gate rather than special-cased.
- **PF-4a** The candidate order MUST be, exactly: `/opt/homebrew/bin/python3.13`; `/opt/homebrew/bin/python3.12`; the corresponding `/opt/homebrew/opt/python@3.x/bin/python3.x` paths; `/usr/local/bin/python3.x`; `/Library/Frameworks/Python.framework/Versions/3.x/bin/python3.x`; `/usr/bin/python3`.
- **PF-5** The preflight **MUST NOT** invoke `brew`, `port`, `softwareupdate`, or any package manager other than `pip` installing the declared runtime dependency.
- **PF-6** The preflight MUST build the replacement environment at a sibling path, MUST validate it with the full probe (PF-13), and MUST only then move it into place.
- **PF-7** The preflight MUST rename the failed environment aside rather than delete it, and SHOULD retain only the most recent such copy.
- **PF-8** The preflight MUST NOT attempt a rebuild more than once per hour, tracked in a persisted marker outside the repository.
- **PF-9** When the preflight cannot produce a working interpreter it **MUST exit `0`**, so that `KeepAlive{SuccessfulExit:false}` does not respawn it, and it MUST emit an escalation notification. This is the sole mechanism preventing an unbounded respawn loop.
- **PF-10** The preflight MUST hold an atomic lock for the duration of a rebuild and MUST skip the rebuild if the lock is held — unless the lock is stale: a lock whose holding PID no longer exists, or whose recorded start time exceeds the rebuild budget (30 minutes), MUST be broken and the rebuild retried. The preflight MUST release the lock on SIGTERM/EXIT, because `kickstart -k` during a rebuild signals exactly this process (PF-14).
- **PF-11** The preflight MUST support `--dry-run`.
- **PF-12** The preflight MUST expose its probes and interpreter-selection logic as separately invocable verbs, so they are testable without launchd.
- **PF-13** The candidate probe MUST be:

```zsh
"$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
```

Executing the interpreter covers dyld resolution; the version check covers the floor required by the project's runtime-evaluated annotations. The probe MUST NOT import `yaml` or any third-party package: PyYAML exists only inside the project virtual environment, installed during the rebuild, so a yaml-inclusive probe fails every bare candidate interpreter and would defeat the ladder (PF-4) entirely. The full probe — the candidate probe plus `import yaml` — MUST be used only to validate `venv.new` after the rebuild (PF-6).
- **PF-14** The preflight MUST write an atomic rebuild-in-progress marker (holding PID and start time) outside the repository before starting a rebuild and MUST remove it when the rebuild completes or aborts. Readers — the watchdog's `decide_action` above all — MUST treat a fresh marker as *busy*: no restart, no failure-counter increment, because `kickstart -k` signals the main job, which *is* the preflight while it rebuilds. A stale marker (holding PID dead, or age beyond the PF-10 rebuild budget) MUST be ignored and removed.
- **PF-15** Before selecting a rebuild, the preflight MUST detect an active Homebrew operation — a running `brew` process, or a Homebrew lock that is actively held, probed with a try-lock (for example `flock`) rather than inferred from lock-directory presence — and, if found, defer the rebuild to a later invocation without consuming the PF-8 cooldown: a keg broken mid-upgrade may be transient, and healing during the upgrade would permanently repoint the daemon at a fallback interpreter. Every venv repoint MUST be recorded in a persistent marker surfaced in escalation text (ES-8), so interpreter drift is visible and reversible.
- **PF-16** When the daemon exits because of a fatal error after a successful interpreter start (for example `FatalAPIError`), it MUST first write a heartbeat with `phase: "fatal"` and a `fatal_reason` field. A heartbeat whose `phase` is `fatal` and whose age is below the staleness threshold MUST be treated as unrecoverable by restart: the watchdog MUST NOT kickstart and MUST escalate, and the preflight MUST exit `0` with an escalation notification. This extends the exit-0 loop defence (PF-9) to deterministic faults that occur *after* exec — the class `KeepAlive` would otherwise respawn forever.

---

## ES — Escalation

- **ES-1** Escalation MUST be delivered via `/usr/bin/osascript` `display notification`.
- **ES-2** Message and title MUST be passed to `osascript` as arguments, never interpolated into AppleScript source.
- **ES-3** The system MUST NOT send more than one unhealthy notification per hour across all components: the watchdog's and the preflight's escalation paths share one cooldown state, because a single episode can surface through either. The shared cooldown MUST live in the watchdog's persisted state file (WD-3); the preflight MUST read and update only that file's notification-cooldown fields and MUST NOT write any other field, so a divergent preflight write cannot trigger WD-12's first-run reset.
- **ES-4** Exactly one recovery notification MUST be sent when health is restored after an episode that escalated. It MUST be sent by the watchdog — the only component that observes health on a recurring tick, since a preflight escalation (PF-9, PF-16) is emitted by a process that exits immediately and can never see recovery — and the component that sends it resets its counters. Without the recovery notification, a resolved incident is indistinguishable from a broken alerting path.
- **ES-5** Every escalation — watchdog or preflight — MUST also be appended to a log file, because notification delivery can be suppressed by Notification Center. The watchdog's log MUST be `~/Library/Logs/superwhisper-transcriber/watchdog.log`, pinned via `StandardOutPath`/`StandardErrorPath` in the committed plist template so a watchdog crash is also durable; the preflight escalates into the daemon log it already inherits.
- **ES-6** A failed notification MUST NOT change the exit code or abort remaining logic.
- **ES-7** The system MUST NOT write to `session.md` and MUST NOT make network calls for escalation.
- **ES-8** The escalation message MUST state when a venv rebuild has occurred, because a rebuilt environment lacks the development extras that `run_transcriber.sh verify` depends on. It MUST also state any interpreter repoint recorded under PF-15.
- **ES-9** The tool MUST provide a means of sending a test notification, so the delivery path can be verified at deployment time rather than during an outage.

---

## Traceability

The "Verified by (planned)" column names the test or review that will verify each requirement once implemented (post-mortem 26-09-07 items #4–#10); none of the named artefacts exist yet. Every requirement in this document has a row.

| Requirement group | Verified by (planned) |
|---|---|
| HB-1 … HB-11 | `tests/unit/test_heartbeat.py` |
| HC-1 … HC-6, HC-13, HC-15 | `tests/unit/test_health_check.py` — `assess_health`, `parse_launchctl_list` |
| HC-7 | structural: the pure functions take no path or handle arguments |
| HC-2, HC-3 | structural: `assess_health` has no log-path or state-path parameter |
| HC-8, HC-9, HC-10 | `tests/unit/test_health_check.py` — CLI behaviour, `--dry-run`, exit codes |
| HC-11, HC-12 | `test_health_check_imports_under_system_python` |
| HC-14, WD-11 | pinned-default test asserting the threshold and interval constants |
| HC-16 | code review of the scan loop — the check only logs |
| WD-1 | plist template review — `StartInterval` present, `KeepAlive` absent |
| WD-2 … WD-6, WD-9, WD-12 | `tests/unit/test_health_check.py` — `decide_action` table, `test_kickstart_refuses_to_target_its_own_label` |
| WD-7, WD-8 | plist template review and code review |
| WD-10 | `decide_action` table — pause-sentinel case |
| PF-2, PF-4, PF-4a, PF-12 | `test_preflight_probe_rejects_system_python`, `test_preflight_select_prints_a_working_interpreter` (in `tests/unit/test_preflight.py`) |
| PF-3 | `test_preflight_probe_does_not_import_config` |
| PF-1, PF-5 … PF-11, PF-13 … PF-16 | code review and the fault-injection drill in the deployment checklist |
| ES-2 | `test_notification_arguments_are_passed_as_argv` |
| ES-1, ES-9 | manual verification via the test-notification flag at deployment |
| ES-3, ES-4 | `decide_action` table — notification-cooldown and recovery cases |
| ES-5 … ES-8 | code review |

## References

- RFC 2119 — Key words for use in RFCs to Indicate Requirement Levels
- RFC 8174 — Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words
- ADR 0002 — launchd daemon pattern (records "no built-in health checks" as a known consequence)
- ADR 0009 — Heartbeat file as the transcriber liveness signal
- ADR 0010 — Preflight wrapper and watchdog agent for launchd self-healing
- Post-mortem 26-07-23 — Superwhisper silent work loss via stability fast-fail (Lesson 3, action item #6)
- Post-mortem 26-09-07 — Transcriber dead 8h via half-completed Homebrew Python upgrade
