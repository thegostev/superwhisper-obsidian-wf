# Deploy preflight wrapper: installed daemon plist predates ADR 0010 (Tier 2 inactive)

Team: Lag · Project: Maintenance & Enablement · Priority: High · Estimate: 2
Labels: ready-for-agent, Improvement

Deployed the watchdog (LAG-682) and discovered the **installed daemon plist is stale**: `~/Library/LaunchAgents/com.alex.transcriber.plist` (mtime 2026-07-21) still execs `venv/bin/python3 auto_transcribe.py` directly — it predates ADR 0010 and does **not** exec `preflight.sh`.

Consequence: the watchdog's Tier-1 `kickstart` lands on a plist with no preflight wrapper, so **Tier 2 of the heal ladder (venv rebuild against a working interpreter ≥3.12) is not active on the live system**. The exact failure the heal ladder was designed for — a broken Homebrew python killing every respawn (the 2026-09-07 outage) — is still unrecoverable by the watchdog today.

## Fix

1. Touch `~/.superwhisper_transcriber_watchdog.pause` (WD-10 sentinel — watchdog must not escalate the planned unload).
2. Render `docs/launchd/com.alex.transcriber.plist.template` → installed plist (fill `__REPO__`/`__HOME__`).
3. `launchctl bootout gui/501/com.alex.transcriber` + `launchctl bootstrap gui/501 <plist>`.
4. Remove the pause sentinel; verify daemon PID + fresh heartbeat; confirm a watchdog tick records healthy.
5. Note: TCC (WD-13) already resolved for `/usr/bin/python3`; `preflight.sh` runs under `/bin/zsh` + venv python, both already granted.

## Acceptance

- Installed plist matches the template modulo placeholders (preflight wrapper exec present, env-file sourcing via `~/.secrets/koding-transcriber.env` intact).
- Daemon respawned through preflight (check transcriber.log for preflight lines), heartbeat fresh, watchdog tick healthy.
- ADR 0010 status notes the wrapper is live on the daemon plist.

Repo: `/Users/harald/Documents/Obsidian/Utvikling/1 - Code/SuperwhisperObsidianWF/superwhisper-obsidian-wf/`