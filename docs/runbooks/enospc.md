# ENOSPC runbook — the volume is full

**Scope:** recovering the transcriber when its volume has less than 2 GiB free,
before or after the preflight's free-space hard stop (PF-18) has refused a
start. Written from the 2026-10-30 incident and the 2026-11-03 recovery
(LAG-909/LAG-910/LAG-911/LAG-912).

## What a full disk does

A full volume does not crash the daemon; it makes the daemon *unobservable*.

- Heartbeat and state writes fail with `Errno 28` (No space left on device).
  The daemon keeps running, but its heartbeat goes stale — indistinguishable
  from a dead process (HB-10). The log carries `event=heartbeat_disk_failed`.
- Restarting while the volume is still full cannot help: the new process fails
  the same writes. The watchdog's restart ladder is useless here (DS-9).
- Superwhisper may write a truncated session (`output.wav` with no `meta.json`),
  and handoffs can burn the full timeout budget before failing.
- Below 2 GiB, `preflight.sh` now refuses to start the daemon and escalates
  (wrapper mode exits 0 so launchd does not respawn in a storm, PF-9/PF-18).

## Detect

```bash
# Primary check: exit 0 healthy, 30 below the 2 GiB floor (names the volume).
./preflight.sh disk-space

# Raw numbers for the volume that holds heartbeat + state.
df -kP "$HOME"

# Stale heartbeat / unhealthy verdict.
./run_transcriber.sh health

# Corroborating log lines.
grep -E "Errno 28|heartbeat_disk_failed" ~/Library/Logs/superwhisper-transcriber/transcriber.log
```

The watchdog warns below 5 GiB and alerts critically below 2 GiB (DS-7).
Freeing to 5 GiB also clears the daemon's own handoff floor (DS-2), so aim
there rather than at exactly 2 GiB.

## Free space safely, in this order

1. **Retention cleanup first.** It is a dry run unless `--apply` is passed:

   ```bash
   ./run_transcriber.sh cleanup-recordings
   # Review the plan, then:
   ./run_transcriber.sh cleanup-recordings --apply
   ```

   It deletes only session dirs the pipeline has consumed (older than
   `recordings_retention_days`, default 14) and orphan dirs with no `meta.json`
   (older than `orphan_retention_hours`, default 24). It never deletes a dir
   referenced by a state entry that has not reached `complete`. If it reports
   `Cannot read state file ... refusing to delete anything`, fix that first —
   do not work around the guard.

2. **Old broken venv copies.** The preflight keeps the most recent
   `venv.broken.*` directory; older ones are safe to remove:

   ```bash
   ls -d venv.broken.* 2>/dev/null
   # Remove every copy except the newest one shown above.
   ```

3. **Old logs** under `~/Library/Logs/superwhisper-transcriber/` — optional,
   and usually small compared with the recordings store.

Do **not** delete the state file, the Just Press Record source recordings, or
session dirs newer than the orphan threshold to buy space. Those decisions
destroy work; retention cleanup already has the safe boundary.

## Recover, in this order

1. Confirm the floor is cleared:

   ```bash
   ./preflight.sh disk-space          # exit 0
   df -kP "$HOME"                     # ideally >= 5 GiB free
   ```

2. Restart through the gated verb. `maintenance-end` removes the watchdog pause
   sentinel, bootstraps the daemon if it is not loaded, and exits 0 only after a
   heartbeat written after the gate started appears:

   ```bash
   ./run_transcriber.sh maintenance-end
   ```

   If it exits 0, the daemon is running on a volume that can persist state. If
   it fails, it prints the reason; do not loop manual `launchctl kickstart`
   while disk is still tight.

3. Confirm health and recovery:

   ```bash
   ./run_transcriber.sh health         # exit 0
   ./preflight.sh availability         # console session + job loaded
   ```

4. Watch the next scan drain the backlog. The fresh heartbeat's
   `disk_free_gb`, `pending_count` and `oldest_pending_age_h` should move in the
   right direction (DS-6, HB-16). Recordings that aged past `scan_days_back`
   (7 days) before the volume was freed are not recoverable by the daemon —
   decide per file whether to redrive from the source recording.

## Drill the gate on a scratch volume

Executable end-to-end on a machine that is not low on space. The scratch image
is small, so it starts below the floor — no file needs to be written to it.

```bash
# 1. Create and mount a small scratch volume.
hdiutil create -size 8m -fs APFS -volname ENOSPCDrill /tmp/enospc-drill.dmg
hdiutil attach /tmp/enospc-drill.dmg

# 2. The reusable verb must refuse: exit 30, naming the mount point.
PREFLIGHT_DISK_PATH=/Volumes/ENOSPCDrill ./preflight.sh disk-space
echo "exit=$?"        # expect exit=30

# 3. The wrapper must refuse to start, without executing the daemon
#    (--dry-run changes nothing; availability is bypassed to isolate PF-18).
PREFLIGHT_DISK_PATH=/Volumes/ENOSPCDrill PREFLIGHT_NOTIFY=0 \
  PREFLIGHT_AVAILABILITY=0 /bin/bash preflight.sh --dry-run 2>&1 |
  grep "disk precondition unmet"

# 4. Tear down.
hdiutil detach /Volumes/ENOSPCDrill
rm -f /tmp/enospc-drill.dmg
```

## Related

- Spec: `docs/specs/self-healing-health-check.md` — DS-1…DS-9 (disk safety),
  HB-10 (unwritable heartbeat), PF-18 (preflight hard stop).
- Retention: LAG-911 and the `recordings_retention_days` /
  `orphan_retention_hours` keys in `config.example.yaml`.
- Incident: LAG-909 (pre-handoff gate), LAG-910 (disk telemetry, thresholds).
