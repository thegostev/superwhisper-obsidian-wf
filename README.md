# superwhisper-obsidian-wf

[![License: MIT](https://img.shields.io/github/license/thegostev/superwhisper-obsidian-wf?color=green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![macOS](https://img.shields.io/badge/platform-macOS-000000?logo=apple&logoColor=white)](https://support.apple.com/guide/launchd)
[![Obsidian](https://img.shields.io/badge/output-Obsidian%20Markdown-7C3AED?logo=obsidian&logoColor=white)](https://obsidian.md)
[![Last commit](https://img.shields.io/github/last-commit/thegostev/superwhisper-obsidian-wf/main)](https://github.com/thegostev/superwhisper-obsidian-wf/commits/main)
[![Stars](https://img.shields.io/github/stars/thegostev/superwhisper-obsidian-wf)](https://github.com/thegostev/superwhisper-obsidian-wf/stargazers)
[![Dependencies](https://img.shields.io/badge/dependencies-PyYAML%20only-3fb554)]()

<img width="2814" height="1536" alt="Superwhisper to Obsidian workflow" src="https://github.com/user-attachments/assets/ff60d7a8-9d9e-41e3-bddb-de8b4fc30a65" />

A macOS service that turns voice recordings into organized Markdown notes in your Obsidian vault. You record with Just Press Record. Superwhisper transcribes, categorizes, and analyzes each recording — on-device or in the cloud, depending on your Superwhisper settings — and the service writes a titled note into the matching vault folder.

```
Just Press Record (.m4a) → Superwhisper Custom Mode → this service → Obsidian vault
```

The service itself runs locally on your Mac. Its only Python dependency is PyYAML.

## Requirements

- macOS (the service uses `launchd`, `afinfo`, and `open -a`; the service isn't portable)
- Python 3.12 or later
- [Superwhisper](https://superwhisper.com), launched at least once
- [Just Press Record](https://www.openplanetsoftware.com/just-press-record/), or another app that saves `.m4a` files into `YYYY-MM-DD/` subfolders

## Adapt the checkout to your machine

Some of the author's machine settings are in the code, not the config. If any of these don't match yours — a timezone outside Europe/Oslo, a different launchd label, a different secrets path — hand this prompt to your coding agent:

```text
Adapt this superwhisper-obsidian-wf checkout to my machine:

1. Timezone: pipeline.py hardcodes OSLO_TZ = ZoneInfo("Europe/Oslo") and
   JPR_FIXED_CET = timezone(timedelta(hours=1)) — a Just Press Record
   filename quirk. Replace Europe/Oslo with my timezone: <YOUR TZ>.
   Check get_audio_timestamp() and the note filename format for anything
   else that assumes Oslo wall-clock time.
2. tests/conftest.py pins TZ='Europe/Oslo' — change it to match.
3. The launchd label 'com.alex.transcriber' appears in run_transcriber.sh
   and in both templates in docs/launchd/ — rename it consistently.
4. The daemon template in docs/launchd/ sources
   $HOME/.secrets/koding-transcriber.env — point it at my env file or
   remove that line.
5. Delete the author's one-off ops scripts at the repo root
   (redrive_*.py, salvage_ops.py, analyze_ops.py, transcript-*.txt) —
   they reference the author's machine and data.
6. Run python -m pytest tests/unit -v and confirm everything passes.
```

The machine-specific config keys — watch folder, vault folders, mode key — stay in `locations/config.yaml` by design (see Install).

## Install

1. Clone the repo inside your Obsidian vault tree — an ancestor directory must contain a folder named `Obsidian`:

   ```bash
   git clone https://github.com/thegostev/superwhisper-obsidian-wf
   cd superwhisper-obsidian-wf
   python3 -m venv venv
   source venv/bin/activate
   pip install -e .
   ```

   If you can't clone inside the vault tree, set `SWOWF_OBSIDIAN_BASE` to your vault root path before running the service.

2. Create a Superwhisper Custom Mode: in Superwhisper, go to Settings → Modes, create the mode, and write a prompt that makes the model output exactly this structure:

   ```text
   CATEGORY: <one of your category names>
   FILENAME: <meeting title, no date, no .md extension, no slashes>

   <analysis in Markdown>
   ```

   **Use the `CATEGORY:`/`FILENAME:` header format shown here, not the `---CATEGORY---` format in `config.example.yaml`.** The parser reads only the header format, and the example file's outdated template routes every note to the DEFAULT folder as "Unknown Meeting".

   In the mode file under `~/Documents/superwhisper/modes/`, find the mode's `key` field — the service addresses modes by key, not by filename. A wrong key doesn't fail: Superwhisper silently keeps using whichever mode is active.

3. Create your configuration:

   ```bash
   mkdir -p locations
   cp config.example.yaml locations/config.yaml
   ```

   Edit `locations/config.yaml`:

   - `superwhisper_mode_key`: the mode key from step 2
   - `watch_folder`: where recordings appear
   - `folders`: uppercase category names mapped to vault folders, matching the categories in your prompt exactly. Include a `DEFAULT` entry as the fallback.

   Keep every key from the example file: its values differ from the code's built-in fallbacks (for example, timeout 3600 versus 300 seconds), so omitting keys silently changes behavior.

4. Start the service and check its status:

   ```bash
   ./run_transcriber.sh start
   ./run_transcriber.sh status
   ```

## Day-to-day commands

| Command | Purpose |
|---|---|
| `./run_transcriber.sh start` / `stop` / `restart` | Manage the background service |
| `./run_transcriber.sh status` | Show running state and the last log lines |
| `./run_transcriber.sh logs` | Follow the live log |
| `./run_transcriber.sh health` | Report liveness; exit code 0 means healthy, 1 means unhealthy |
| `./run_transcriber.sh maintenance-end` | Finish `launchctl` maintenance: remove the pause sentinel, bootstrap the daemon, wait up to 60 s for a fresh heartbeat. Exit 0 means PASS |
| `./preflight.sh availability` | Assert the availability contract: exit 0 = console session + daemon job loaded, 20 = no console user, 21 = job not loaded |
| `./run_transcriber.sh catchup [days]` | Process recordings the service missed — default 7 days, including recordings already marked permanently failed. Back up your vault before running this against a large backlog |
| `./run_transcriber.sh catchup-preview [days]` | Run catchup without processing files |
| `./run_transcriber.sh cleanup-recordings` | Preview deletion of expired consumed and orphan session dirs (dry run; add `--apply` to delete) |

## Start on login (optional)

The repo ships launchd templates in `docs/launchd/`. Fill in the `__REPO__` and `__HOME__` placeholders, copy the two LaunchAgent plists (daemon and watchdog) to `~/Library/LaunchAgents/`, and load them with `launchctl`.

The templates have two non-obvious requirements:

- Grant `/usr/bin/python3` Full Disk Access in System Settings, or the watchdog can't read the recordings under `~/Documents` and dies on its first run.
- Before planned maintenance with `launchctl`, touch the pause sentinel `~/.superwhisper_transcriber_watchdog.pause` so the watchdog doesn't mistake maintenance for an outage. The sentinel expires after four hours. When maintenance is done, run `./run_transcriber.sh maintenance-end`: it removes the sentinel, bootstraps the daemon if it isn't loaded, and exits 0 only once a fresh heartbeat appears (add `--dry-run` to preview).

The daemon template also sources `$HOME/.secrets/koding-transcriber.env` before starting — the author's personal secrets path. Edit or remove that line for your machine.

The service only runs inside a logged-in GUI session. If a deploy or reboot leaves the Mac at the login window, the LaunchAgents never load and the service is silently down. `preflight.sh` now checks that contract before starting the daemon and refuses without a partial start; for a deploy-time check run `./preflight.sh availability` — exit 20 means no console user, exit 21 means the job is not loaded (the message names the bootstrap command).

> When the launchd service is loaded, `./run_transcriber.sh start` won't start — this prevents two daemons from racing. Manage the service through one mechanism or the other, not both.

## Availability supervisor (LaunchDaemon, recommended)

The login-time agents above cannot help when nobody is logged in. After a reboot to the login window, neither `com.alex.transcriber` nor its watchdog is ever loaded, and no alert fires — the 2026-10-30 → 11-03 outage went unnoticed for 3.7 days for exactly that reason. `supervisor.py` closes the gap as a system-domain LaunchDaemon that starts at boot.

It only observes; it never tries to run Superwhisper headless. Every 60 seconds it records one of four states in `~/.superwhisper_transcriber_supervisor.json`:

| State | Meaning |
|---|---|
| `NO_CONSOLE_USER` | Nobody is logged in (login window or setup user) |
| `ABSENT` | A console user exists but `com.alex.transcriber` is not loaded |
| `LOADED_STALE` | The job is loaded, but its heartbeat is missing or older than 10 minutes |
| `HEALTHY` | The job is loaded and its heartbeat is fresh |

Two consecutive `NO_CONSOLE_USER`/`ABSENT` checks raise one notification; it is not repeated until the state recovers. Install with root (see the template header for the TCC caveat about running a script from a protected folder):

```bash
# Fill in __REPO__ and __HOME__ first.
sudo cp docs/launchd/com.alex.transcriber.supervisor.plist.template \
  /Library/LaunchDaemons/com.alex.transcriber.supervisor.plist
sudo chown root:wheel /Library/LaunchDaemons/com.alex.transcriber.supervisor.plist
sudo chmod 644 /Library/LaunchDaemons/com.alex.transcriber.supervisor.plist
sudo plutil -lint /Library/LaunchDaemons/com.alex.transcriber.supervisor.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.alex.transcriber.supervisor.plist
sudo launchctl print system/com.alex.transcriber.supervisor
```

For a one-shot observation, run `python3 supervisor.py --dry-run` (report only) or without `--dry-run` to update the status file.

## License

MIT. Use at your own risk.
