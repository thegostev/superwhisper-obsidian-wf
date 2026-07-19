"""Configuration loader for RecordingAnalyser.

Loads settings from config.yaml. No external API keys required — audio processing
is handled by Superwhisper running locally on the same machine.
"""

import sys
from pathlib import Path

import yaml


def find_obsidian_base(start: Path = Path(__file__).resolve()) -> Path:
    """Return the path *before* the enclosing 'Obsidian' folder.

    This script lives inside the Obsidian vault tree, so we walk up from its own
    location until we hit a folder named 'Obsidian' and return that folder's
    parent. All relative folder paths in config are resolved against this base,
    which keeps them identical across machines even though the absolute path
    (e.g. ~/Documents vs an iCloud location) differs per Mac.
    """
    for parent in start.parents:
        if parent.name == "Obsidian":
            return parent.parent
    raise RuntimeError(
        f"Could not find an 'Obsidian' folder in the parents of {start}. "
        "config.py must live inside the Obsidian vault tree."
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
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}\nCopy config.example.yaml to config.yaml and fill in your values.", flush=True)
        sys.exit(1)

    cfg: dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    cfg["watch_folder"] = resolve_path(cfg["watch_folder"])
    cfg["state_file"] = resolve_path(cfg.get("state_file", "~/.meeting_transcriber_state.json"))
    cfg["superwhisper_recordings_dir"] = resolve_path(cfg.get("superwhisper_recordings_dir", "~/Documents/superwhisper/recordings"))
    cfg["folders"] = {cat: resolve_path(p) for cat, p in cfg.get("folders", {}).items()}

    return cfg


_cfg = load_config()

WATCH_FOLDER: str = _cfg["watch_folder"]
FOLDERS: dict[str, str] = _cfg["folders"]
STATE_FILE: str = _cfg["state_file"]

# Superwhisper integration
SUPERWHISPER_MODE_KEY: str = _cfg.get("superwhisper_mode_key", "meeting")
SUPERWHISPER_TIMEOUT: int = _cfg.get("superwhisper_timeout", 300)
SUPERWHISPER_RECORDINGS_DIR: str = _cfg["superwhisper_recordings_dir"]
SUPERWHISPER_POLL_INTERVAL: int = _cfg.get("superwhisper_poll_interval", 3)

# Service behavior
SCAN_INTERVAL: int = _cfg.get("scan_interval", 30)
SCAN_DAYS_BACK: int = _cfg.get("scan_days_back", 7)
DELAY_BETWEEN_FILES: int = _cfg.get("delay_between_files", 30)
MAX_RETRIES: int = _cfg.get("max_retries", 3)
MAX_FILES_PER_CYCLE: int = _cfg.get("max_files_per_cycle", 5)
