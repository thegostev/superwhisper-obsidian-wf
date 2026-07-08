"""Configuration loader for RecordingAnalyser.

Loads settings from config.yaml. No external API keys required — processing
is handled by Superwhisper running locally on the same machine.
"""

import sys
from pathlib import Path

import yaml

# Keys the service cannot run without. `folders` must additionally contain a
# DEFAULT entry — it is the fallback destination for unknown/unclassified output.
REQUIRED_KEYS = ("watch_folder", "folders")
DEFAULT_CATEGORY = "DEFAULT"


def validate_config(cfg: dict) -> None:
    """Fail fast on config gaps that would otherwise crash deep in the pipeline.

    Without a DEFAULT folder, save_output() raises KeyError only *after* a full
    Superwhisper pass has been spent — this surfaces the problem at startup instead.
    """
    if missing := [k for k in REQUIRED_KEYS if not cfg.get(k)]:
        raise ValueError(f"config.yaml is missing required field(s): {', '.join(missing)}. See config.example.yaml.")
    if not isinstance(cfg["folders"], dict):
        raise ValueError("config.yaml 'folders' must be a mapping of CATEGORY → output folder. See config.example.yaml.")
    if DEFAULT_CATEGORY not in cfg["folders"]:
        raise ValueError(f"config.yaml 'folders' must include a '{DEFAULT_CATEGORY}' entry — it is the fallback for unknown categories. See config.example.yaml.")


def load_config(config_path: Path = Path(__file__).parent / "config.yaml") -> dict:
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}\nCopy config.example.yaml to config.yaml and fill in your values.", flush=True)
        sys.exit(1)

    cfg: dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    try:
        validate_config(cfg)
    except ValueError as e:
        print(f"ERROR: {e}", flush=True)
        sys.exit(1)

    cfg["watch_folder"] = str(Path(cfg["watch_folder"]).expanduser())
    cfg["state_file"] = str(Path(cfg.get("state_file", "~/.meeting_transcriber_state.json")).expanduser())
    cfg["superwhisper_recordings_dir"] = str(Path(cfg.get("superwhisper_recordings_dir", "~/Documents/superwhisper/recordings")).expanduser())

    cfg["folders"] = {cat: str(Path(p).expanduser()) for cat, p in cfg.get("folders", {}).items()}

    return cfg


# ---------- Load once at import time ----------
_cfg = load_config()

# ---------- Export as module-level constants ----------
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
