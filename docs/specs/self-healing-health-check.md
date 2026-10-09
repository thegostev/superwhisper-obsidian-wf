# Self-healing health check — requirements

**Status:** Accepted
**Date:** 2026-09-07
**Project:** SuperwhisperObsidianWF
**Implements:** ADR 0009 (heartbeat liveness signal), ADR 0010 (preflight wrapper and watchdog agent)

**Implementation status:** implemented (post-mortem 26-09-07 action items #4–#10). The heartbeat writer, `health_check.py`, `preflight.sh`, the plist templates in `docs/launchd/`, and the `run_transcriber.sh` `health` verb all exist, with tests. **The watchdog agent itself was never deployed** — confirmed by the 2026-09-16 post-mortem (59 h outage); deployment is tracked as Linear LAG-682. The traceability table below maps each requirement to its verification.

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
- **HB-12** The heartbeat MUST carry a `writer` field naming the process that produced it: `"daemon"` for the launchd daemon entry point, `"manual"` for every other caller (on-demand runs, redrives, salvage and other ops scripts, which reach the writer through the shared pipeline functions). The writer identity MUST default to `"manual"`: only the daemon entry point declares `"daemon"`, so a caller that forgets to declare cannot masquerade as the daemon. A manual run refreshes the same file the watchdog reads, so without this field a freshly refreshed heartbeat is indistinguishable from proof of life — it was during the 26-09-16 outage forensics (LAG-675).

### Heartbeat schema, version 1

```json
{
  "schema": 1,
  "pid": 8078,
  "phase": "starting" | "scanning" | "processing" | "fatal",
  "writer": "daemon" | "manual",
  "cycle": 4321,
  "updated_at": "2026-09-07T14:31:02Z",
  "started_at": "2026-09-07T02:31:10Z",
  "state_complete": 316,
  "failed_permanent": 0,
  "fatal_reason": "FatalAPIError: superwhisper_mode_key is empty"
}
```

`writer` is absent only in heartbeats written before HB-12; readers MUST treat an absent `writer` as `daemon` (the pre-HB-12 behaviour), because every post-HB-12 writer sets the field and defaults to `manual`.

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
- **HC-15** The verdict MUST be *unhealthy* if and only if the heartbeat is missing, unreadable, of unknown schema, or stale (HC-1), the service label is absent (`service_not_loaded`, HC-13), the heartbeat is fresh (age below the staleness threshold) with `phase: "fatal"` (`heartbeat_fatal` — PF-16), or it is fresh but not written by the daemon with no live daemon PID to corroborate it (`heartbeat_not_daemon` — HC-17). No other `launchctl list` observation MAY affect the verdict; launchctl data is corroborating and diagnostic only. A fresh heartbeat beside a missing PID MUST be reported as a `pid_missing_warning` in the report but MUST NOT by itself make the verdict unhealthy (HC-5).
- **HC-16** The daemon's scan loop MUST check the watchdog state file's `last_run_at` (WD-3) and MUST emit a `⚠️` warning when it exceeds twice the watchdog interval (WD-11). This is detection only: the daemon MUST NOT restart or bootstrap the watchdog, and the check MUST NOT feed into the heartbeat, the health verdict, or any escalation.
- **HC-17** A fresh heartbeat whose `writer` is not `daemon` (HB-12) MUST NOT by itself count as proof of liveness: an ops script sharing the pipeline writer refreshes the same file the daemon does. Before concluding healthy on such a heartbeat the reader MUST cross-check the daemon's PID with `launchctl print gui/<uid>/<label>`, which reports a PID only for a job that is actually running — unlike the `launchctl list` PID column, which is sampled and lies in both directions (HC-4/HC-5). With a live PID the verdict stays healthy and the report MUST carry a `manual_heartbeat_warning`; with no live PID the verdict MUST be unhealthy with reason `heartbeat_not_daemon`, which maps to the ordinary restart ladder (WD-6) because the daemon really is gone. The cross-check MUST be performed only for non-daemon heartbeats, so the healthy steady state costs no extra subprocess, and a failed or timed-out probe MUST read as no live PID (ES-6).

### Reason codes

`heartbeat_missing`, `heartbeat_unreadable`, `heartbeat_schema_unknown`, `heartbeat_stale`, `service_not_loaded`, `heartbeat_fatal`, `heartbeat_not_daemon`.

`heartbeat_fatal` denotes a fresh heartbeat with `phase: "fatal"` (PF-16). It maps to escalate-without-restart, not to the restart ladder.

`heartbeat_not_daemon` denotes a fresh heartbeat written by an ops script (`writer: "manual"`, HB-12) with no live daemon PID to corroborate it (HC-17). It maps to the ordinary restart ladder: the freshness is an artefact of the manual run, and the daemon behind it is dead.

---

## WD — Watchdog

- **WD-1** The watchdog agent MUST be scheduled with `StartInterval` and MUST NOT declare `KeepAlive`. `KeepAlive` on a run-once script is an immediate infinite loop.
- **WD-2** The watchdog MUST NOT target its own label for restart. It MUST raise an error if the target label equals its own.
- **WD-3** The watchdog MUST persist consecutive-failure counters and the last action and notification timestamps across invocations, in an atomically written JSON file outside the repository. The state file MUST also hold the notification-cooldown timestamp shared with the preflight (ES-3).
- **WD-4** A tick that observes a stale heartbeat (HC-1) MUST be observational — the watchdog MUST NOT restart or escalate, MUST reset its consecutive-failure counters, and MUST record the run — when the elapsed time since the watchdog's previous run exceeds the staleness threshold AND the heartbeat's age does not exceed that gap by more than one scan cycle (30 s): staleness fully explained by the gap itself means the daemon was suspended alongside the watchdog, because `StartInterval` does not fire while the machine sleeps and launchd fires the missed run immediately on wake — acting on that first tick would kickstart a merely suspended daemon, possibly mid-transcription. A stale heartbeat older than the gap by more than one scan cycle proves the daemon was already failing before the machine slept, and the tick decides normally. A fresh heartbeat always decides normally, so the recovery path (ES-4) is unaffected by jitter. The reset MUST NOT clear an escalation already sent in the current episode — only a healthy tick clears escalation state (ES-4).
- **WD-5** On the watchdog's first run, when no persisted state exists, it MUST skip the tick and record the run.
- **WD-6** The watchdog MUST attempt at most two consecutive restarts per continuous unhealthy episode, after which it MUST escalate. It MUST then enter a bounded slow retry: at most one restart per hour while unhealthy, so recovery resumes automatically once a transient cause (for example an offline venv rebuild) clears — the PF-8 rebuild cooldown and ES-3 notification cooldown already bound the storm. The same capping applies to the bootstrap repair (WD-9). An indefinite stop is reserved for the pause sentinel (WD-10).
- **WD-7** Every subprocess the watchdog spawns MUST be given an explicit timeout. launchd will not start a second instance of the same job, so a hung watchdog never ticks again.
- **WD-8** All subprocess executables MUST be referenced by absolute path.
- **WD-9** On `service_not_loaded` the watchdog MUST NOT attempt a `launchctl kickstart` — against an unbootstrapped label it fails. If the daemon's plist path was passed at install time (`--plist`), the watchdog MUST instead run `launchctl bootstrap gui/<uid>/<label> <plist>` (WD-9 revision, LAG-673) — the only repair for a `bootout`, which removes the job from the domain entirely — capped like a restart (WD-6), and MUST notify on the first tick (a bootout is deliberate human action, not a crash loop; the WD-6 first-tick silence MUST NOT apply). Without a plist path the watchdog MUST escalate only, and a declared `--plist` that does not exist MUST degrade to escalate-only with a logged warning, never silently.
- **WD-10** The watchdog MUST honour a pause sentinel file, and when it is present MUST exit `0` without taking any action. Manual service maintenance (for example `bootout`/`bootstrap` during deployment) MUST place the sentinel, because WD-9's escalate-only rule would otherwise turn every planned unload into a false escalation. The sentinel MUST expire (WD-10 revision, LAG-674): one whose mtime is older than 4 hours MUST be treated as absent — the watchdog MUST remove it, send one notice that the pause expired and that no maintenance should still be running, and heal normally on the same tick; a `--dry-run` tick MUST report the expiry without removing the file or notifying. The notice MUST be bound to a successful removal so an unremovable sentinel cannot repeat it every tick. Without a TTL a sentinel forgotten after maintenance disables healing indefinitely — the 26-09-16 outage ran 59h on exactly that.
- **WD-11** The watchdog MUST run with `StartInterval` 300 seconds, equal to the default staleness threshold (HC-14), for a worst-case detection latency of interval + threshold ≈ 10 minutes.
- **WD-12** If the persisted watchdog state file is unreadable or corrupt, the watchdog MUST treat the invocation as a first run (WD-5) and overwrite the state at the end of the tick. A failed state write MUST NOT abort the tick and MUST be logged.
- **WD-18** (MT-1, LAG-680) Manual maintenance MUST end through a gated completion verb (`run_transcriber.sh maintenance-end` → `health_check.py --maintenance-end`) that, in order, removes the pause sentinel, runs `launchctl bootstrap` for the daemon unless it is already loaded, and waits at most 60 seconds for a heartbeat whose mtime is not older than the gate's start; it MUST exit `0` only on such a heartbeat with a non-fatal phase and MUST otherwise exit non-zero with diagnostics (service row, heartbeat state, next command). A heartbeat older than the gate MUST NOT count, because a pre-maintenance or manual-run heartbeat is not proof the bootstrapped daemon lives. The verb MUST be idempotent, MUST NOT kickstart or require `sudo`, MUST treat a bootstrap refused because the service is already loaded as success, and MUST support `--dry-run` with no file or service changes. The 26-09-16 recovery was done by hand and the sentinel removal was the step that got forgotten.
- **WD-13** The watchdog's interpreter (`/usr/bin/python3`, HC-11) MUST hold a Full Disk Access grant before the agent is bootstrapped. Without it a launchd-spawned process is denied reads of `~/Documents`, where `health_check.py` lives, and the first tick dies with `[Errno 1] Operation not permitted` before the module loads — so the tool cannot report the fault itself, and no wrapper routes around it: `/bin/zsh` and `/bin/cat` are denied equally. The grant is a one-time manual step (System Settings → Privacy & Security → Full Disk Access → add `/usr/bin/python3`), discovered at first deployment 2026-09-16. It MUST be verified at deployment alongside ES-1/ES-9; a TCC grant has no automatable test.
- **WD-14** When the installed plist path is known (`--plist`, WD-9), the watchdog MUST hash-compare that plist against the committed `docs/launchd/` template rendered with the real repo and home paths, and MUST report any difference as a warning. The comparison MUST be over the parsed plist with keys sorted, not the raw bytes, so a reflow or an edited comment is not reported as drift — a warning that cries wolf is the one nobody reads. Drift MUST NOT change the health verdict and MUST NOT trigger any healing action: a divergent plist says nothing about whether the daemon is alive, and restarting a healthy daemon over a deployment mismatch is the HC-4/HC-5 mistake one level up. The check MUST additionally report when the installed plist's `ProgramArguments` do not run `preflight.sh`, because ADR 0010's self-heal ladder is inert without the wrapper. Every failure mode — missing plist, missing template, unparsable either side — MUST be reported, never raised (ES-6). The whole check MUST be read-only, so `--dry-run` needs no special case (HC-8/HC-9). Drift is confirmed live (LAG-684): the installed `com.alex.transcriber` plist had diverged from the template and stopped routing through the wrapper, with nothing in the system looking.
- **WD-15** The daemon job MUST be exempt from App Nap: `ProgramArguments` MUST start with `/usr/bin/caffeinate -i` (preventing idle assertion), the wrapper inside it MUST still reach the daemon via `exec` through the preflight so PF-1's chain survives the wrap, and the plist MUST set `ProcessType` to `Interactive` — the two halves hold together, because either alone leaves the screen-locked suspend path open. The failure without it is silent and unhealable: macOS SIGSTOPs the daemon whenever the screen locks, the heartbeat freezes, meetings queue until unlock, each resume leaves `[Errno 4] Interrupted system call` in `transcriber.log`, and `kickstart` without `-k` has no effect on a frozen-but-alive PID (WD-6's restart ladder cannot act on it). The requirement is daemon-level (it constrains the daemon template), not watchdog-level; it lives in the WD section as the next identifier after WD-14 to keep identifiers stable and append-only. Confirmed live (LAG-694): the daemon froze across multi-hour screen-locked windows (heartbeat age up to 12,790 s) and recovered only on unlock, and the exemption — deployed with commit `7a23a18` — was the fix.
- **WD-16** The daemon wrapper MUST reach `preflight.sh` through the TCC-granted interpreter (WD-13), never through a bare shell exec or a shell redirect: the wrapper's zsh exec's the venv interpreter with a bridge that opens the script, hands it to `/bin/bash -s` over stdin, and preflight's own `exec` chain then carries PF-1 — so no binary without a grant ever opens a repository file. WD-13's denial applies to the whole launchd-spawned chain, not only the watchdog: a plain `exec '__REPO__/preflight.sh'` resolves the shebang in-kernel but then bash cannot open the script (exit 126, `Operation not permitted`), and a zsh-redirect variant fails identically on the redirect open — both leaving `KeepAlive{SuccessfulExit:false}` respawn-looping a wrapper that has no PF-9 anti-storm of its own. The requirement is deployment-blocking the same way WD-13 is: the first bootstrap through an unbridged wrapper cannot boot the daemon at all. Confirmed live (LAG-734, 2026-09-23): both unbridged variants failed at first repoint; the stdin bridge booted the daemon through the full preflight on the first try.
- **WD-17** A stale heartbeat (HC-1) beside a daemon PID that `launchctl print` still reports MUST be treated as frozen-but-alive and MUST escalate to `launchctl kickstart -k gui/<uid>/<label>`, which terminates the job before restarting it. Plain `kickstart` is a no-op against a live PID, so the whole restart ladder (WD-6) fires uselessly while the daemon stays stopped — the 2026-09-18 App Nap freeze ran on exactly that, the watchdog kickstarting through the whole window with no effect (WD-15, LAG-694). The liveness answer MUST come from the `launchctl print` PID (HC-17), never the sampled `launchctl list` column (HC-4/HC-5), and the probe MUST run only on a stale heartbeat, so the healthy steady state still costs no extra subprocess. The escalation keeps the ladder's existing position and capping: it replaces the plain kickstart for this one reason, counts as a restart under WD-6, and MUST NOT change the sleep-gap observational grace (WD-4), the fatal path (PF-16) or the bootstrap repair (WD-9), whose job is absent rather than frozen. A failed or timed-out probe MUST read as not-alive and fall back to the plain kickstart (ES-6). `-k` MUST NOT be the default verb: against a healthy daemon it would cut a transcription short. The App Nap exposure itself is closed by WD-15; this requirement covers any future freeze-by-other-cause (LAG-753).

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

## TP — Throughput

The 2026-10-08 post-mortem (49 h: a zombie Superwhisper instance pinned `state_complete` at 219 while the daemon stayed `phase=processing`, producing 5,998 consecutive "verdict: healthy" watchdog observations) exposed a blind spot in this spec: every HC/WD signal measures liveness, none measures progress. The heartbeat already carries the progress counters (`state_complete`, `failed_permanent` — see HB-6), so the watchdog diffs them between consecutive healthy ticks. Requirements TP-1 … TP-8 define those rules; LAG-799 is the plateau, LAG-800 the permanent-failure stall.

- **TP-1** On a healthy tick whose heartbeat shows `state_complete` unchanged from the previous healthy tick for longer than the plateau threshold — the processing threshold while `phase` is `processing`, the any-phase threshold otherwise — the watchdog MUST send a notification naming the stall (`throughput-plateau`). The rule is **notification-only**: the daemon is alive by every HC/WD measure, so the health verdict MUST stay healthy, the restart/bootstrap ladder MUST NOT be touched (kicking a live daemon can interrupt a legitimate long transcription), and `consecutive_failures` MUST NOT be incremented. A plateau is a warning to a human, not a fault in the daemon.
- **TP-2** On a healthy tick whose heartbeat shows `failed_permanent` increased since the previous healthy tick while `state_complete` is flat, the watchdog MUST — once the flat time exceeds the permanent-failure threshold — send a notification distinct from TP-1's (`permanent-failure-stall`) naming the failure mode: the dependency is converting transcription failures into permanent exclusion. Like TP-1 it is notification-only. When both rules fire on the same tick, the `failed_permanent` label MUST win: it names the precise failure mode, of which a bare plateau is the vaguer symptom.
- **TP-3** A heartbeat whose counters have moved — `state_complete` advanced or decreased, or in either direction — MUST silently re-baseline the tracker instead of alerting (a busy evening is the expected good case). A `failed_permanent` notification MUST only ever fire while completions are flat: a rise alongside advancing completions means the pipeline is working through problems, which is not a stall.
- **TP-4** The plateau and permanent-failure thresholds MUST be explicit configuration inputs (CLI flags with pinned defaults, like `--max-age` under HC-14): the post-mortem left the plateau window open (4–6 h), and an idle-weekend machine needs to raise the any-phase threshold via config, not an edit. The throughput notification MUST use its own cooldown field, independent of the ES-3 shared cooldown: health-axis traffic (outage pages, recovery notices) and throughput-axis pages MUST neither suppress nor be suppressed by each other, because they are different conversations (one is about a process being down, the other about a process being up yet useless). The processing threshold MUST be shorter than the any-phase threshold; the permanent-failure threshold shorter still, bounded below by the 3600 s per-attempt deadline a healthy completion cannot exceed.
- **TP-5** An unobservable period MUST NOT count toward the flat clock. Unhealthy ticks (the heartbeat is stale, so its counters may have moved unseen), sleep gaps (WD-4), and pause-sentinel ticks (WD-10) MUST shift the tracker's baseline timestamp forward by the gap instead — after an outage, recovery is not a plateau, and re-baselining on the first healthy tick after a sleep must not page on a stall the operator could not have observed.
- **TP-6** The heartbeat-diff MUST be pure like the rest of `decide_action` (HC-7): no I/O, no mutation of its inputs; the tracker is returned alongside the side-effect-free state.
- **TP-7** A throughput decision MUST carry its alert label in the decision dict (`alert` field) so downstream surfaces (LAG-676's ALARM marker) can name the failure mode, and MUST set the tick's action to `notify_throughput` with exit code `0` — a throughput notification is a warning, not a launchd failure, so it MUST NOT set `escalated` (which would send a bogus ES-4 recovery notification on the next healthy tick) and the throughput alert MUST lose to the ES-4 recovery notification when both are due on the same tick.
- **TP-8** A heartbeat sample that cannot be interpreted for the diff — missing or malformed counters, or a corrupt persisted tracker — MUST degrade to a silent re-baseline for safety: an unmeasurable tick MUST never page, because paging on parser noise is how an alerting system teaches its operator to ignore it.

---

## AF — Alarm marker

The 2026-10-08 post-mortem (49 h zombie Superwhisper instance) showed that every escalation surface the watchdog had was transient: a notification scrolls away or is suppressed by Notification Center (ES-3), and log lines must already be known to be worth reading to be worth reading. Meanwhile the only machine-local checks a human runs unprompted — `ps`, `launchctl list`, the notification history — reported nothing wrong, because the zombie was alive by every HC/WD measure, and a throughput page (TP-1) is exactly the escalation that leaves no trace on the machine (LAG-799/LAG-800). Requirements AF-1 … AF-7 (LAG-676) add one durable on-machine surface: a persistent marker file at `~/.superwhisper_transcriber_ALARM` whose existence answers "is anything wrong?" from a plain terminal — `[ -e ~/.superwhisper_transcriber_ALARM ]` — and whose contents name the failure mode for scripts.

- **AF-1** The watchdog MUST raise the marker on every tick that escalates with a notification — an unhealthy verdict that notifies (ES-3, the WD-6 ladder) — writing a JSON object carrying `reason` (the health failure reason or a TP-7 alert label), `created_at` and `updated_at` in epoch seconds. The marker MUST be written before the notification is attempted, so notification suppression or delivery failure never leaves an incident without its durable surface.
- **AF-2** A healthy tick with no alert MUST remove the marker — not only the ES-4 recovery notification: any healthy observation retires the surface, so a stranded marker can never outlive its incident (the WD-10/LAG-674 lesson — a left-behind flag must be self-correcting, not operator-remembered). Ticks with neither a healthy verdict nor a notification — kickstarts, waiting ladder states, WD-4 observational ticks, PF-14 busy observations — MUST leave the marker untouched: mid-incident ticks neither clear nor refresh it.
- **AF-3** A throughput page (TP-1, TP-2) MUST raise or refresh the marker with the TP-7 alert label as the `reason` — a throughput page is an alarm on an otherwise-healthy tick, and the 26-10-08 incident produced no unhealthy verdict at all in 49 h. The label MUST surface even on ticks whose TP-4 cooldown suppresses the repeat notification: a persisting stall keeps its marker alive, and the surface MUST NOT flicker off between pages.
- **AF-4** A marker refresh MUST preserve `created_at` while updating `updated_at`, so the marker dates the incident's start rather than its last tick, even when the mid-episode `reason` changes. An unparsable or absent previous marker starts a new episode at `updated_at` = `created_at` = now.
- **AF-5** `--dry-run` (HC-9) MUST neither write, refresh, nor remove the marker — on unhealthy and healthy dry-runs alike.
- **AF-6** A marker write or removal failure MUST NOT change the exit code or abort the tick's remaining logic (ES-6): the tick logs the failure and continues.
- **AF-7** A paused tick (WD-10) MUST not read, write, or remove the marker: maintenance must not silently retire or refresh an alarm.

---

## CB — Circuit breaker (daemon-side dependency-down detection)

The 26-10-08 post-mortem's other P0: nine sequential attempts (Oct 7 + Oct 8 pre-recovery) every one ending in the identical `TimeoutError("Superwhisper did not return a result within 3600s")`, and nothing escalated anywhere — `MAX_RETRIES` conflates "bad audio" with "dependency down", so on a dead Superwhisper three 1 h burns per file flip otherwise-good files to `failed_permanent`. The watchdog cannot see a streak of per-attempt outcomes (it only reads periodic heartbeat counters, TP-*), so the daemon itself classifies every attempt outcome and trips a flag when timeouts repeat. Scope note: LAG-801 covers classification, the flag, the notification and their persistence; the cheap pricing of dependency-down attempts (LAG-803) and the guarded auto-relaunch (LAG-804) are deliberate follow-ons, as is the watchdog consuming the flag (LAG-815).

- **CB-1** Every attempt outcome MUST be classified into exactly one class: `timeout` (the poll deadline in `wait_for_superwhisper_result` and the abandoned-stub fast-fail both raise `TimeoutError` — the class that signals a down dependency), `permanent` (`PermanentFileError` — the LLM refused the contract; content, not dependency), `transient` (any other failure), or `success` (a completion). The classification MUST be observable as a helper (`classify_outcome`) so the mapping is the same everywhere.
- **CB-2** The daemon MUST maintain a streak counter over classified outcomes: identical consecutive `timeout` outcomes increment it; ANY other outcome (a completion, a contract refusal, an arbitrary error) resets it to zero — a streak is only a streak while outcomes stay identical, and the abandoned-stub fast-fail case B counts alongside the 3600 s deadline case C so both shapes of "superwhisper produced nothing" drive the breaker.
- **CB-3** On the K-th identical consecutive timeout outcome (K = `circuit_breaker_threshold`, default 3 — the same K the post-mortem recommends) the daemon MUST set a `dependency_down` flag: persisted in state under `circuit_breaker`, and mirrored to the heartbeat (`dependency_down: true`, CB-6). The flag MUST be sticky: it MUST NOT be cleared by further attempts of any kind, only by an actual completion (a `success` outcome) — a dependency that is down does not recover because the daemon kept trying.
- **CB-4** On the trip the daemon MUST fire ONE notification ("Superwhisper not producing results — N files in retry queue"), not one per attempt; the notification goes through the same osascript-argv discipline as ES-1/ES-2 (text as argv, never interpolated) and a failed notification MUST NOT abort attempt bookkeeping.
- **CB-5** A non-timeout outcome MUST NOT count toward a new streak: after a reset the counter restarts from zero, so the breaker needs K fresh identical timeouts to re-trip.
- **CB-6** The heartbeat writer MUST carry a `dependency_down` boolean field in the schema-v1 payload (default `false`): call sites that know the flag pass it in; the rest emit the last known value, so the flag is visible from the next heartbeat onward. A completion that clears the flag MUST force a heartbeat write so the clear is visible promptly. This field is additive with schema version 1 (readers that ignore it lose nothing); the watchdog consuming it is a separate requirement (LAG-815).
- **CB-7** `process_audio` MUST record the outcome class in the per-file state record (`outcome` field) alongside `status`, so post-hoc review can tell a dependency-down streak from scattered failures without reconstructing it from timestamps. The daemon's `_heartbeat_counts` MUST pass the flag into every heartbeat and the startup banner MUST surface a carried-over `dependency_down` after a restart.
- **CB-8** The trip notification MUST be rate-limited by a shared cooldown (`circuit_breaker_notify_cooldown`, default 3600 s) persisted in state (`last_notified_epoch`): a re-trip inside the window MUST NOT page again, so a dead dependency produces roughly one page per hour for as long as it stays down — not one per attempt. The epoch MUST be stamped BEFORE the page so a restart cannot double-fire, and an expired window allows one reminder page. The cooldown is the daemon's own: it does not share the ES-3 watchdog cooldown (different processes, different conversations), mirroring TP-4's independence rule.
- **CB-9** The whole breaker sub-dict (`streak`, `last_outcome`, `dependency_down`, `flagged_by_outcome`, `flagged_at`, `last_notified_epoch`) MUST persist through `save_state`/`load_state` (atomic as usual), so a daemon restart neither resets the streak mid-incident nor re-pages from a stale cool state.

---

## Traceability

Every requirement in this document has a row naming the test or review that verifies it.

| Requirement group | Verified by |
|---|---|
| HB-1 … HB-11 | `tests/unit/test_heartbeat.py` |
| HB-12 | `tests/unit/test_heartbeat.py` — `TestWriterField` (LAG-675) |
| HC-1 … HC-6, HC-13, HC-15 | `tests/unit/test_health_check.py` — `assess_health`, `parse_launchctl_list` |
| HC-7 | structural: the pure functions take no path or handle arguments |
| HC-2, HC-3 | structural: `assess_health` has no log-path or state-path parameter |
| HC-8, HC-9, HC-10 | `tests/unit/test_health_check.py` — CLI behaviour, `--dry-run`, exit codes |
| HC-11, HC-12 | `test_health_check_imports_under_system_python` |
| HC-14, WD-11 | pinned-default test asserting the threshold and interval constants |
| HC-16 | code review of the scan loop — the check only logs |
| HC-17 | `tests/unit/test_health_check.py` — `TestHeartbeatWriterCrossCheck` (LAG-675) |
| WD-1 | plist template review — `StartInterval` present, `KeepAlive` absent |
| WD-15 | `tests/unit/test_launchd_templates.py` — `TestAppNapExemption` — caffeinate wrapper, exec chain intact, `ProcessType=Interactive` (LAG-733) |
| WD-2 … WD-6, WD-9, WD-12 | `tests/unit/test_health_check.py` — `decide_action` table, `test_kickstart_refuses_to_target_its_own_label` |
| WD-18 | `TestMaintenanceEnd` (in `tests/unit/test_health_check.py`), `test_maintenance_end_*` (in `tests/unit/test_run_transcriber_sh.py`) (LAG-680) |
| WD-7, WD-8 | plist template review and code review |
| WD-10 | `decide_action` table — pause-sentinel case; `TestPauseSentinelTtl`, `TestHealPauseSentinelTtl` — 4h expiry (LAG-674) |
| WD-13 | manual verification of the Full Disk Access grant at deployment, alongside ES-1/ES-9 — a TCC grant has no automatable test |
| WD-14 | `tests/unit/test_plist_drift.py` — fingerprint, drift, preflight routing, warning-only reporting (LAG-684) |
| WD-17 | `tests/unit/test_health_check.py` — `TestFrozenAliveEscalation`, `decide_action` table — `kickstart_kill` ladder case, `-k` argv, probe gated on the stale reason (LAG-753); the live heal against a really frozen daemon needs `launchctl` and stays manual |
| Identifier stability (Conventions) | `tests/unit/test_spec_traceability.py` — every identifier cited in the repository resolves to a spec entry (LAG-732) |
| PF-2, PF-4, PF-4a, PF-12 | `test_preflight_probe_rejects_system_python`, `test_preflight_select_prints_a_working_interpreter` (in `tests/unit/test_preflight.py`) |
| PF-3 | `test_preflight_probe_does_not_import_config` |
| PF-1, PF-5 … PF-11, PF-13 … PF-16 | code review and the fault-injection drill in the deployment checklist |
| ES-2 | `test_notification_arguments_are_passed_as_argv` |
| ES-1, ES-9 | manual verification via the test-notification flag at deployment |
| ES-3, ES-4 | `decide_action` table — notification-cooldown and recovery cases |
| ES-5 … ES-8 | code review |
| TP-1, TP-2, TP-3 | `tests/unit/test_health_check.py` — `TestThroughputPlateau`, `TestPermanentFailureStall` |
| TP-4 | pinned-default test and config-override cases in `TestThroughputPlateau`; separate-cooldown and hourly re-alert cases |
| TP-5 | unhealthy-period, sleep-gap, and pause cases in `TestThroughputPlateau` |
| TP-6 | purity test in `TestThroughputPlateau` (no I/O, no input mutation) |
| TP-7, TP-8 | `TestThroughputHealCli` (`alert` label, exit code, no bogus recovery, cooldown) and re-baseline cases in `TestThroughputPlateau` |
| TP-1 (defaults) | threshold constants asserted against LAG-799/LAG-800 defaults |
| AF-1, AF-4 | `tests/unit/test_health_check.py` — `TestAlarmMarker` (raise, contents, cooldown refresh, created-at continuity) |
| AF-2, AF-5 | `tests/unit/test_health_check.py` — `TestAlarmRecovery` (healthy-tick clear, recovery clear, `--dry-run` writes and removes nothing) |
| AF-3 | `tests/unit/test_health_check.py` — `TestAlarmThroughput` (TP-7 labels verbatim, marker outlives the TP-2 cooldown, retires when the plateau breaks) |
| AF-6 | `tests/unit/test_health_check.py` — `TestAlarmRecovery.test_alarm_write_failure_does_not_break_the_tick` (ES-6) |
| AF-7 | `tests/unit/test_health_check.py` — `TestAlarmRecovery.test_paused_tick_leaves_the_marker_alone` |
| CB-1, CB-5 | `tests/unit/test_circuit_breaker.py` — `TestClassifyOutcome`, reset cases in `TestStreakAndFlag` (LAG-801) |
| CB-2, CB-3 | `tests/unit/test_circuit_breaker.py` — `TestStreakAndFlag` (streak counts, trip-on-K, sticky flag, success-only clear) |
| CB-4, CB-8 | `tests/unit/test_circuit_breaker.py` — `TestNotification` (fire-once, retry-queue copy, cooldown windows, guarded sender, real osascript argv) |
| CB-6 | `tests/unit/test_circuit_breaker.py` — `TestHeartbeatFlag`; writer reset in `tests/unit/test_heartbeat.py` |
| CB-7 | `tests/unit/test_circuit_breaker.py` — `TestProcessAudioOutcomeRecording`, `TestDaemonWiring` |
| CB-9 | `tests/unit/test_circuit_breaker.py` — `TestStatePersistence` (round-trip, restart-safe streak/flag/cooldown) |
| CB-3, CB-8 (defaults) | threshold and cooldown constants asserted (`CIRCUIT_BREAKER_THRESHOLD=3`, `CIRCUIT_BREAKER_NOTIFY_COOLDOWN=3600.0`) |

## References

- RFC 2119 — Key words for use in RFCs to Indicate Requirement Levels
- RFC 8174 — Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words
- ADR 0002 — launchd daemon pattern (records "no built-in health checks" as a known consequence)
- ADR 0009 — Heartbeat file as the transcriber liveness signal
- ADR 0010 — Preflight wrapper and watchdog agent for launchd self-healing
- Post-mortem 26-07-23 — Superwhisper silent work loss via stability fast-fail (Lesson 3, action item #6)
- Post-mortem 26-09-07 — Transcriber dead 8h via half-completed Homebrew Python upgrade
- Post-mortem 26-10-08 — Transcriber silent 49h via zombie Superwhisper instance (recs 1a/1b → TP-*, LAG-799/LAG-800)
