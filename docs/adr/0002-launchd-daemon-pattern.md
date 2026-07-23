# 0002 launchd daemon for continuous transcription service

**Status:** Accepted
**Date:** 2026-02-21
**Project:** RecordingAnalyser

## Context

RecordingAnalyser must run continuously to detect and process new audio recordings as they appear in the Just Press Record iCloud folder. The service needs to:
- Start automatically at macOS login
- Survive crashes (auto-restart)
- Run with minimal resource overhead during idle periods
- Support clean stop/start without data loss

## Considered Options

### Option 1: macOS launchd agent
Native macOS service manager. Plist-based configuration at `~/Library/LaunchAgents/`. Auto-starts at login, auto-respawns on crash.
- Pros: native to macOS, zero additional dependencies, handles lifecycle management, integrates with system logging
- Cons: plist XML format is verbose, debugging requires launchctl commands, no built-in health checks

### Option 2: cron job
Schedule the script to run every N minutes via crontab.
- Pros: simple, universal, well-understood
- Cons: no state between runs (must re-initialize each time), cold start overhead per invocation, gaps between runs (minimum 1 minute), no crash recovery

### Option 3: Python daemon with systemd-style wrapper
Use a Python daemonization library (e.g., `python-daemon`) with a custom wrapper script.
- Pros: portable to Linux, more control over daemon lifecycle
- Cons: additional dependency, reinvents what launchd provides natively, more complex than needed for a single-user macOS setup

## Decision Outcome

Chosen option: **macOS launchd agent**, because it provides exactly the lifecycle management needed (auto-start, respawn, clean stop) with zero additional dependencies. The scan loop in `auto_transcribe.py` runs every 30 seconds, which is more responsive than cron's minimum 1-minute interval. Crash recovery is handled by launchd without any application-level code.

## Consequences

### Positive
- Service starts automatically at login — no manual intervention
- Crashes are recovered automatically (launchd respawns the process)
- Clean stop via `launchctl unload` allows graceful shutdown
- Log routing to `/tmp/transcriber.out` and `/tmp/transcriber.err` via plist

### Negative
- macOS-only — cannot run on Linux servers without rewriting service management
- Plist file at `~/Library/LaunchAgents/com.yourdomain.transcriber.plist` contains hardcoded paths (known technical debt)
- Debugging requires `launchctl list | grep transcriber` rather than simple `ps` checks

### Neutral
- The Python script's internal 30s sleep loop is independent of launchd — launchd only handles start/stop/restart, not scheduling
