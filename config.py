"""Configuration loader for RecordingAnalyser.

Loads settings from config.yaml. No external API keys required — processing
is handled by Superwhisper running locally on the same machine.
"""

import sys
from pathlib import Path
from typing import Any

import yaml

# Keys without which the service cannot run. The README documents these as
# required; validation enforces that contract at load time instead of letting
# a missing key surface as a cryptic KeyError deep inside the pipeline.
REQUIRED_KEYS = ("watch_folder", "folders")
DEFAULT_CATEGORY = "DEFAULT"  # the fallback folder for unknown/unparsed categories


def _config_error(message: str) -> None:
    """Print a config error pointing at the template, then exit."""
    print(f"ERROR: {message}\nSee config.example.yaml for the expected format.", flush=True)
    sys.exit(1)


def validate_config(cfg: Any, config_path: Path) -> dict[str, Any]:
    """Fail fast (with a helpful message) if the config is missing required fields."""
    if not isinstance(cfg, dict):
        _config_error(f"Config file is empty or malformed: {config_path}")

    if missing := [k for k in REQUIRED_KEYS if not cfg.get(k)]:
        _config_error(f"Config is missing required field(s): {', '.join(missing)}")

    if DEFAULT_CATEGORY not in cfg["folders"]:
        _config_error(
            f"Config 'folders' must include a '{DEFAULT_CATEGORY}' entry — "
            "it is the fallback folder for unknown or unparsed categories"
        )

    return cfg


def load_config(config_path: Path = Path(__file__).parent / "config.yaml") -> dict[str, Any]:
    """Load, validate, and return configuration from YAML file."""
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}\nCopy config.example.yaml to config.yaml and fill in your values.", flush=True)
        sys.exit(1)

    cfg: dict[str, Any] = validate_config(yaml.safe_load(config_path.read_text(encoding="utf-8")), config_path)

    # Expand ~ in path values
    cfg["watch_folder"] = str(Path(cfg["watch_folder"]).expanduser())
    cfg["state_file"] = str(Path(cfg.get("state_file", "~/.meeting_transcriber_state.json")).expanduser())
    cfg["failed_analysis_log"] = str(Path(cfg.get("failed_analysis_log", "failed_analysis.log")).expanduser())
    if "superwhisper_recordings_dir" in cfg:
        cfg["superwhisper_recordings_dir"] = str(Path(cfg["superwhisper_recordings_dir"]).expanduser())

    cfg["folders"] = {cat: str(Path(p).expanduser()) for cat, p in cfg.get("folders", {}).items()}

    return cfg


# ---------- Load once at import time ----------
_cfg = load_config()

# ---------- Export as module-level constants ----------
WATCH_FOLDER: str = _cfg["watch_folder"]
FOLDERS: dict[str, str] = _cfg["folders"]
STATE_FILE: str = _cfg["state_file"]
FAILED_ANALYSIS_LOG: str = _cfg["failed_analysis_log"]

# Superwhisper integration
SUPERWHISPER_MODE_KEY: str = _cfg.get("superwhisper_mode_key", "meeting")
SUPERWHISPER_TIMEOUT: int = _cfg.get("superwhisper_timeout", 300)
SUPERWHISPER_RECORDINGS_DIR: str = _cfg.get("superwhisper_recordings_dir", str(Path("~/Documents/superwhisper/recordings").expanduser()))
SUPERWHISPER_POLL_INTERVAL: int = _cfg.get("superwhisper_poll_interval", 3)

# Service behavior
SCAN_INTERVAL: int = _cfg.get("scan_interval", 30)
SCAN_DAYS_BACK: int = _cfg.get("scan_days_back", 7)
DELAY_BETWEEN_FILES: int = _cfg.get("delay_between_files", 30)
MAX_RETRIES: int = _cfg.get("max_retries", 3)
MAX_FILES_PER_CYCLE: int = _cfg.get("max_files_per_cycle", 5)
