# 0010 Preflight wrapper and watchdog agent for launchd self-healing

**Status:** Accepted — decision recorded; **not yet implemented** (post-mortem 26-09-07 items #4–#10)
**Date:** 2026-09-07
**Project:** SuperwhisperObsidianWF

## Context

ADR 0009 established *how to detect* that the transcriber daemon has stopped working. This ADR covers *what to do about it* without human intervention.

The motivating incident is the 2026-09-07 outage described in ADR 0009: a half-completed `brew upgrade` left a Python 3.13.15 executable inside the 3.13.13 keg, referencing a framework that did not exist. Every launchd `KeepAlive` respawn aborted instantly for eight hours. Manual recovery required `brew reinstall python@3.13` followed by `launchctl kickstart -k`.

The decisive constraint follows directly from that incident: **restarting a process whose interpreter is broken fails forever.** launchd already restarts the daemon — `KeepAlive{SuccessfulExit:false}` with `ThrottleInterval 10` retried 162 recorded times over eight hours, ≈ one every 3 minutes (launchd backed off well beyond its configured floor), and every attempt failed identically. Any self-healing design whose only repair action is "restart it again" would have added nothing on the day it was most needed.

Genuine self-healing therefore requires repairing the interpreter itself. The user constraint is that repair must not invoke `brew`, `port`, or any other package manager: an unattended agent mutating system-wide package state is unbounded risk, and `brew` can hang, prompt, or require network at arbitrary times.

Two facts about the local environment make an alternative repair path viable. Runtime dependencies are a single package (`PyYAML>=6.0`), so rebuilding the virtual environment is cheap. And `/opt/homebrew/bin/python3.12` (3.12.13) exists as a **separate Homebrew keg**, entirely unaffected by the `python@3.13` breakage — so a working interpreter satisfying `requires-python >= 3.12` remained available on the machine throughout the outage.

## Considered Options

### Option 1: Watchdog agent only, with `launchctl kickstart` as the sole repair
A periodic agent detects staleness and restarts the service.
- Pros: smallest change; touches nothing in the existing daemon launch path; easy to reason about
- Cons: would not have fixed the motivating incident. The interpreter was broken; restarting it produces the same abort. The watchdog would have kickstarted, escalated, and — restart being its only verb — left the service dead — an improvement on eight silent hours, but detection dressed up as recovery, not self-healing.

### Option 2: Preflight wrapper only, no watchdog
The plist calls a wrapper that validates and repairs the interpreter before exec'ing the daemon.
- Pros: fixes the incident class at its source, within seconds rather than minutes; no second launchd job; no cross-invocation state to persist
- Cons: only runs when launchd starts the job. Nothing detects a daemon that dies while the interpreter is healthy, a job accidentally left unloaded, or a process wedged with a live PID. It repairs one failure mode and is blind to the rest.

### Option 3: Preflight wrapper plus a separate watchdog agent
The plist calls a self-healing wrapper; an independent periodic agent detects staleness and triggers a restart, which re-enters the wrapper.
- Pros: covers both axes — the wrapper repairs the interpreter, the watchdog notices everything else and re-invokes the wrapper by restarting the job; the repair ladder composes, so Tier 2 is reached *through* Tier 1 rather than duplicated beside it; no venv-rebuild logic in the watchdog, and no possibility of two components rebuilding concurrently
- Cons: two new launchd artefacts to install and keep in sync; a second job introduces a label-prefix collision with existing tooling; nothing watches the watchdog

### Option 4: Run `brew reinstall python@3.13` from the healer
Have the repair path fix the Homebrew installation directly, mirroring the manual fix.
- Pros: repairs the actual root cause rather than routing around it; leaves the system in the state the user would have chosen manually
- Cons: excluded by user constraint. `brew` requires network, can take minutes, may prompt, and mutates state shared with every other project on the machine. Running it unattended from a background agent on a five-minute timer is an unbounded blast radius for a service that transcribes meeting recordings.

## Decision Outcome

Chosen option: **preflight wrapper plus a separate watchdog agent**, with the repair ladder composed rather than duplicated:

```
stale heartbeat
  → watchdog: launchctl kickstart -k          (Tier 1)
  → launchd re-runs the plist → preflight.sh
  → probe fails → rebuild venv against the
    first working interpreter >= 3.12          (Tier 2)
  → exec daemon → heartbeat resumes
  → next watchdog tick sees healthy, resets
```

The watchdog's only repair verb is `kickstart`. It heals a broken interpreter *because the plist now points at the wrapper*. The two changes are load-bearing together, which is why they are one decision rather than two.

**Interpreter validation is by execution, not inspection — with two distinct probes.** The **candidate probe** selects a ladder interpreter and covers dyld resolution and the version floor:

```zsh
"$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'
```

The **full probe** — the same command plus `import yaml` — is used only to validate `venv.new` after the rebuild. The split is load-bearing: PyYAML exists only inside the project virtual environment (it is what the rebuild installs), so a yaml-inclusive probe fails every bare candidate — `/opt/homebrew/bin/python3.12` raises `ModuleNotFoundError: No module named 'yaml'` — and the ladder would select nothing, leaving the motivating incident unhealed by exactly the mechanism written to heal it. Both probes deliberately do not `import config`: a missing or malformed `config.yaml` is a configuration fault that no venv rebuild can repair, and treating it as one would burn the rebuild cooldown on a typo.

**Candidate ladder**, probed in order with the candidate probe: `/opt/homebrew/bin/python3.13`, `/opt/homebrew/bin/python3.12`, the corresponding `opt/python@3.x` paths, `/usr/local/bin/*`, `/Library/Frameworks/*`, and finally `/usr/bin/python3`. The system interpreter is kept in the list and rejected by the version gate rather than special-cased — self-documenting, and one fewer branch. Traced against the actual incident with the two-probe scheme: candidate 1 aborts on dyld, the `opt/python@3.13` symlink resolves into the same broken keg, and **candidate 2 passes** the candidate probe; the daemon then rebuilds the venv against it and `venv.new` passes the full probe. The daemon would have recovered on the second candidate without `brew`.

**Rebuilds are atomic**, following the discipline `save_state()` already uses. The environment is built at `venv.new`, validated with the full probe, and only then swapped in by renaming `venv` aside to `venv.broken.<timestamp>`. Validation precedes the swap, and the previous environment is renamed rather than deleted, so a mid-rebuild failure escalates cleanly instead of destroying the only working environment. The rebuild uses stdlib `venv`, not `uv`: adding a dependency to the recovery path that is absent from the happy path is the wrong trade.

**Rebuilds are protected from the restart ladder.** While the preflight rebuilds, it *is* the main job — a watchdog `kickstart -k` on a stale heartbeat would kill the heal in progress. The preflight therefore writes an atomic rebuild-in-progress marker (PID + start time) before rebuilding and removes it on completion, the watchdog treats a fresh marker as busy (no restart, no counter increment), and the rebuild lock carries a staleness rule (holding PID dead or beyond a 30-minute budget → break and retry) so a killed rebuild can never wedge future heals behind a dead lock. The preflight releases its lock on SIGTERM/EXIT.

**Escaping the `KeepAlive` loop is what makes the wrapper safe.** `KeepAlive{SuccessfulExit:false}` respawns only on a non-zero exit, so **a preflight that cannot produce a working interpreter must exit 0**. This is the entire loop defence and costs no additional machinery — without it, a failing wrapper would be restarted every ten seconds indefinitely. The wrapper must `exec` the daemon rather than call it, so launchd tracks the daemon's own PID and `kickstart -k` signals the right process.

This has a deliberate corollary: a job stopped this way reports `-  0` in `launchctl list`, which reads as healthy. The design is self-consistent only because detection is heartbeat-based (ADR 0009) rather than launchctl-based. The wrapper intentionally makes launchd's view quieter and moves all signal into the heartbeat and notification channels.

**The watchdog must not depend on what it monitors.** `health_check.py` runs under `/usr/bin/python3` (3.9.6) — the one interpreter Homebrew cannot break. Had it run on `venv/bin/python3`, it would have been dead throughout the exact outage it exists to catch. This forces three rules: the module is stdlib-only, it imports nothing from the project (the repo's `Path | None` annotation style raises `TypeError` on 3.9, so `config.py` is unimportable there), and it uses `from __future__ import annotations` so the house style still passes ruff under `target-version = "py313"`. A subprocess test enforces this permanently.

**The watchdog is itself watched — cheaply, from inside the thing it monitors.** Deferring watchdog liveness entirely would leave the sole detection path able to die silently and return the system to the exact 2026-09-07 state. The daemon's scan loop therefore checks the watchdog state file's `last_run_at` and logs a `⚠️` warning when it exceeds twice the watchdog interval. This is detection only — the daemon cannot restart the watchdog, and the escalation remains manual — but it converts a silent death of the monitor into a visible log line.

The watchdog is scheduled with `StartInterval 300` and **no `KeepAlive`**, which on a run-once script would be an immediate infinite loop. It refuses to kickstart its own label. Escalation is a macOS notification via `osascript`, with arguments passed as `argv` rather than interpolated into AppleScript source. Escalations are also appended to a log file, because Notification Center can silently suppress banners and PF-9's exit-0 hides a preflight failure from launchd entirely: the watchdog's log is `~/Library/Logs/superwhisper-transcriber/watchdog.log`, pinned in the plist template via `StandardOutPath`/`StandardErrorPath` so a watchdog crash is also durable, while the preflight escalates into the daemon log it already inherits — it runs under the main plist and cannot write to the watchdog's.

**Anti-flap** uses five independent, cheap mechanisms: kickstart on unhealthy ticks 1 and 2 only and escalate thereafter, followed by a bounded slow retry of at most one kickstart per hour while unhealthy — "never restart again" is reserved for the pause sentinel, because health can only return through a restart, and a hard stop after two failed heals would leave a transient outage (an offline rebuild, say) permanently unrecoverable; a one-hour rebuild cooldown in the preflight; a one-hour notification cooldown shared by the watchdog and the preflight, so an eight-hour outage yields eight banners rather than ninety-six; and an observational sleep-gap grace — a stale heartbeat whose age does not exceed the gap since the watchdog's previous run by more than one scan cycle is assessed but never acted on, because `StartInterval` does not fire while the Mac sleeps, launchd fires the missed run immediately on wake, and a merely suspended daemon's heartbeat is then exactly as old as the gap itself; a heartbeat substantially older than the gap proves the daemon was already failing before the machine slept and is acted on normally. Escalation state survives the sleep-gap reset; only a healthy tick clears it. A single recovery notification fires when health returns after an escalation — sent by the watchdog, which can observe the recovery (ES-4) — without it, "the problem was fixed" is indistinguishable from "alerting broke". The consequence of this ladder is a bounded blast radius: the worst case for any false positive is two restarts of an idempotent daemon, an escalation notification and its paired recovery notification, and possibly one bounded slow-retry kickstart.

**Two guards stop the healer from healing the wrong thing.** Before proposing a rebuild, the preflight checks for an active Homebrew operation — a running `brew` process, or a Homebrew lock actively held (probed with a try-lock, not inferred from directory presence) — and defers: a keg broken mid-upgrade is often transient, and a rebuild during the upgrade would permanently repoint the daemon onto the fallback interpreter for a problem that would have fixed itself. Every interpreter repoint is recorded persistently and surfaced in escalation text, so drift from 3.13 to 3.12 is visible and reversible. And a heartbeat with `phase: "fatal"` (ADR 0009's schema) — a deterministic failure *after* a successful exec, such as `FatalAPIError` — is treated as unrecoverable by restart: the watchdog escalates instead of kickstarting, and the preflight exits 0, extending the exit-0 loop defence to the post-exec deterministic faults that `KeepAlive` would otherwise respawn forever.

## Consequences

### Positive
- The 2026-09-07 failure class now recovers without human intervention, verified by tracing the candidate ladder against the actual broken keg
- Detection latency for every other failure mode drops from unbounded to roughly ten minutes
- The repair ladder composes: the watchdog stays trivially testable, contains no filesystem mutation, and cannot contend with the preflight over the venv
- The wrapper gives a permanent home for future pre-exec invariants
- Closes the gap ADR 0002 recorded as "no built-in health checks"; ADR 0002 itself remains accepted and unsuperseded, as launchd is still the pattern

### Negative
- A rebuilt venv contains only `PyYAML` — `ruff`, `mypy`, `pytest`, and `pre-commit` are lost, so `./run_transcriber.sh verify` breaks silently after a successful self-heal until `pip install -e '.[dev]'` is run. This must appear in the escalation notification text.
- The rebuild requires network for `pip install PyYAML`; the pip cache is empty and uv's cache holds only a `cp311` wheel. Offline, the heal fails cleanly and escalates rather than leaving a half-built environment.
- The watchdog depends on `/usr/bin/python3`, which requires Command Line Tools to remain installed
- Nothing restarts the watchdog. The daemon-side check of the watchdog's `last_run_at` (above) only makes its death visible as a log warning; if the watchdog dies, the system returns to unmonitored operation until a human notices. A second-level watchdog was considered and rejected as over-engineering for a single-user laptop service.
- A rebuild that survives a watchdog kill leaves the rebuild lock behind; without the PF-10 staleness rule and the PF-14 busy marker, the ladder could starve the healer it serves. Both are now required, not incidental.
- Two launchd artefacts must now be kept in sync with the repository; plist templates are committed under `docs/launchd/` to reduce drift
- `health_check.py` cannot import `config.py`, so its defaults are duplicated; mitigated by passing paths explicitly from the plist at install time and by a writer/reader round-trip test

### Neutral
- The main plist changes from executing the venv interpreter directly to executing the wrapper. `launchctl kickstart` does **not** reload a changed plist — this requires `bootout` followed by `bootstrap`, or the old direct-exec job silently continues running.
- The existing dual-daemon guard in `run_transcriber.sh` matches the service label by substring, which the new `com.alex.transcriber.watchdog` label would satisfy; it is changed to exact field matching in the same change set
- `run_transcriber.sh` gains a `health` verb; `verify` was already taken by the local CI gate
- `.gitignore` gains `venv.new/` and `venv.broken.*/`, which the existing `venv/` entry does not cover
