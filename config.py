"""Configuration loader for RecordingAnalyser.

Loads settings from config.yaml. No external API keys required — processing
is handled by Superwhisper running locally on the same machine.
"""

import os
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------- Locate config file ----------
_CONFIG_DIR = Path(__file__).parent
_CONFIG_FILE = _CONFIG_DIR / "config.yaml"


def load_config(config_path: Path = _CONFIG_FILE) -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Configuration dictionary with all settings.
    """
    if not config_path.exists():
        print(
            f"ERROR: Config file not found: {config_path}\n"
            f"Copy config.example.yaml to config.yaml and fill in your values.",
            flush=True,
        )
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg: dict[str, Any] = yaml.safe_load(f)

    # Expand ~ in path values
    cfg["watch_folder"] = os.path.expanduser(cfg["watch_folder"])
    cfg["state_file"] = os.path.expanduser(cfg.get("state_file", "~/.meeting_transcriber_state.json"))
    cfg["failed_analysis_log"] = os.path.expanduser(cfg.get("failed_analysis_log", "failed_analysis.log"))
    if "superwhisper_recordings_dir" in cfg:
        cfg["superwhisper_recordings_dir"] = os.path.expanduser(cfg["superwhisper_recordings_dir"])

    for cat in cfg.get("folders", {}):
        cfg["folders"][cat] = os.path.expanduser(cfg["folders"][cat])

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
SUPERWHISPER_RECORDINGS_DIR: str = os.path.expanduser(
    _cfg.get("superwhisper_recordings_dir", "~/Documents/superwhisper/recordings")
)
SUPERWHISPER_POLL_INTERVAL: int = _cfg.get("superwhisper_poll_interval", 3)

# Service behavior
SCAN_INTERVAL: int = _cfg.get("scan_interval", 30)
SCAN_DAYS_BACK: int = _cfg.get("scan_days_back", 7)
DELAY_BETWEEN_FILES: int = _cfg.get("delay_between_files", 30)
MAX_RETRIES: int = _cfg.get("max_retries", 3)
MAX_FILES_PER_CYCLE: int = _cfg.get("max_files_per_cycle", 5)
RETRY_BACKOFF: list[int] = _cfg.get("retry_backoff", [10, 30, 60])
