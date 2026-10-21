"""Configuration loader for RecordingAnalyser.

Loads settings from config.yaml. No external API keys required — audio processing
is handled by Superwhisper running locally on the same machine.
"""

import os
import sys
from pathlib import Path

import yaml


def find_obsidian_base(start: Path | None = None) -> Path:
    """Return the path *before* the enclosing 'Obsidian' folder.

    This script lives inside the Obsidian vault tree, so we walk up from its own
    location until we hit a folder named 'Obsidian' and return that folder's
    parent. All relative folder paths in config are resolved against this base,
    which keeps them identical across machines even though the absolute path
    (e.g. ~/Documents vs an iCloud location) differs per Mac.

    The lookup is lazy: if no 'Obsidian' folder is found AND the SWOWF_CONFIG_PATH
    env var is set (test mode), the repo root is used as a fallback. This lets
    tests run on CI runners where the checkout is not inside an Obsidian vault.
    For production deployments, the SWOWF_OBSIDIAN_BASE env var can override the
    base path explicitly.
    """
    env_override = os.environ.get("SWOWF_OBSIDIAN_BASE")
    if env_override:
        return Path(env_override).expanduser()

    start = start or Path(__file__).resolve()
    for parent in start.parents:
        if parent.name == "Obsidian":
            return parent.parent

    if os.environ.get("SWOWF_CONFIG_PATH"):
        # Test mode: config will provide absolute paths, so OBSIDIAN_BASE is only
        # a fallback for relative paths. Use the repo root (config.py's parent).
        return start.parent

    raise RuntimeError(
        f"Could not find an 'Obsidian' folder in the parents of {start}. "
        "config.py must live inside the Obsidian vault tree, or set SWOWF_OBSIDIAN_BASE."
    )


OBSIDIAN_BASE: Path = find_obsidian_base()


def resolve_path(p: str) -> str:
    """Resolve a config path to an absolute path.

    - '~/...'      -> expanded against the home dir (portable as-is)
    - '/abs/...'   -> used unchanged
    - 'Obsidian/..' (or any relative path) -> joined onto OBSIDIAN_BASE
    """
    raw = Path(p).expanduser()
    if raw.is_absolute():
        return str(raw)
    return str(OBSIDIAN_BASE / raw)


def load_config(config_path: Path = Path(__file__).parent / "config.yaml") -> dict:
    # Tests point config.py at a temp config via env var (see tests/conftest.py).
    # Production: falls back to locations/config.yaml, then ./config.yaml.
    env_path = os.environ.get("SWOWF_CONFIG_PATH")
    if env_path:
        config_path = Path(env_path)
    elif not config_path.exists():
        alt = Path(__file__).parent / "locations" / "config.yaml"
        if alt.exists():
            config_path = alt

    if not config_path.exists():
        print(
            f"ERROR: Config file not found.\n"
            f"Tried: {config_path}\n"
            f"Also looked in: {Path(__file__).parent / 'locations' / 'config.yaml'}\n"
            f"Copy config.example.yaml to locations/config.yaml and fill in your values.",
            flush=True,
        )
        sys.exit(1)

    cfg: dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    cfg["watch_folder"] = resolve_path(cfg["watch_folder"])
    cfg["state_file"] = resolve_path(cfg.get("state_file", "~/.meeting_transcriber_state.json"))
    cfg["heartbeat_file"] = resolve_path(cfg.get("heartbeat_file", "~/.superwhisper_transcriber_heartbeat.json"))
    cfg["superwhisper_recordings_dir"] = resolve_path(
        cfg.get("superwhisper_recordings_dir", "~/Documents/superwhisper/recordings")
    )
    cfg["folders"] = {cat: resolve_path(p) for cat, p in cfg.get("folders", {}).items()}

    return cfg


_cfg = load_config()

WATCH_FOLDER: str = _cfg["watch_folder"]
FOLDERS: dict[str, str] = _cfg["folders"]
STATE_FILE: str = _cfg["state_file"]

# Daemon liveness signal (ADR 0009). A sibling of state_file, deliberately
# outside the repository so a venv rebuild can never clobber it.
HEARTBEAT_FILE: str = _cfg["heartbeat_file"]

# Circuit breaker (LAG-801, spec CB-*): after CIRCUIT_BREAKER_THRESHOLD
# identical consecutive timeout attempt outcomes with no completion in
# between, the daemon declares the Superwhisper dependency down — a state
# flag, a heartbeat field, and one notification per cooldown window.
CIRCUIT_BREAKER_THRESHOLD: int = _cfg.get("circuit_breaker_threshold", 3)
CIRCUIT_BREAKER_NOTIFY_COOLDOWN: float = _cfg.get("circuit_breaker_notify_cooldown", 3600.0)

# Superwhisper integration
SUPERWHISPER_MODE_KEY: str = _cfg.get("superwhisper_mode_key", "meeting")
SUPERWHISPER_TIMEOUT: int = _cfg.get("superwhisper_timeout", 300)
# Handoff fast-fail (LAG-802, spec CB-10): `open -a Superwhisper` exiting 0
# only means LaunchServices queued the event. If no new recordings/<epoch>
# session dir appears within this many seconds of handoff delivery, the
# attempt fails as `superwhisper-not-accepting` instead of burning the full
# SUPERWHISPER_TIMEOUT. 0 (or less) disables the fast-fail.
SUPERWHISPER_ACCEPT_TIMEOUT: float = _cfg.get("superwhisper_accept_timeout", 600)
# Guarded auto-relaunch (LAG-804, spec CB-14..CB-20): when a
# superwhisper-not-accepting streak trips the circuit breaker, the daemon quits
# and reopens Superwhisper itself - at most once per episode, only while the
# idle gate is clear, and never within the cooldown of the previous relaunch.
# false is the kill switch.
SUPERWHISPER_AUTO_RELAUNCH: bool = bool(_cfg.get("superwhisper_auto_relaunch", True))
SUPERWHISPER_RELAUNCH_COOLDOWN: float = _cfg.get("superwhisper_relaunch_cooldown", 3600.0)
SUPERWHISPER_RECORDINGS_DIR: str = _cfg["superwhisper_recordings_dir"]
SUPERWHISPER_POLL_INTERVAL: int = _cfg.get("superwhisper_poll_interval", 3)

# Service behavior
SCAN_INTERVAL: int = _cfg.get("scan_interval", 30)
SCAN_DAYS_BACK: int = _cfg.get("scan_days_back", 7)
DELAY_BETWEEN_FILES: int = _cfg.get("delay_between_files", 30)
MAX_RETRIES: int = _cfg.get("max_retries", 3)
MAX_FILES_PER_CYCLE: int = _cfg.get("max_files_per_cycle", 5)

# iCloud placeholder preflight (LAG-813, spec IC-*): before each scan, run
# `brctl download` on `.<name>.m4a.icloud` stubs inside the scan window so
# recordings still in iCloud land on disk instead of sitting invisible.
ICLOUD_PREFLIGHT_ENABLED: bool = _cfg.get("icloud_preflight_enabled", True)
ICLOUD_PREFLIGHT_DRY_RUN: bool = _cfg.get("icloud_preflight_dry_run", False)
ICLOUD_DOWNLOAD_TIMEOUT: float = _cfg.get("icloud_download_timeout", 10.0)
ICLOUD_DOWNLOAD_COOLDOWN: float = _cfg.get("icloud_download_cooldown", 300.0)
ICLOUD_MAX_DOWNLOADS_PER_CYCLE: int = _cfg.get("icloud_max_downloads_per_cycle", 10)
