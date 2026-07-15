"""Configuration loader for RecordingAnalyser.

Loads settings from config.yaml. No external API keys required — audio processing
is handled by Superwhisper running locally on the same machine.
"""

import sys
from pathlib import Path

import yaml

DEFAULT_CATEGORY = "DEFAULT"


def _fail(message: str) -> None:
    """Abort startup with a clear, actionable error instead of a deep runtime traceback."""
    print(f"ERROR: {message}", flush=True)
    sys.exit(1)


def _validate(cfg: dict) -> None:
    """Fail fast at the config boundary so misconfiguration never reaches the pipeline.

    Without this, a missing `watch_folder` raises a bare KeyError at import time, and a
    `folders` map lacking a DEFAULT entry burns every recording as a "failed" file when
    save_output() does FOLDERS[DEFAULT] deep inside the daemon loop.
    """
    if not isinstance(cfg, dict):
        _fail("config.yaml is empty or malformed. Copy config.example.yaml and fill in your values.")

    if not cfg.get("watch_folder"):
        _fail("Required setting 'watch_folder' is missing or empty in config.yaml (where audio files land).")

    folders = cfg.get("folders")
    if not isinstance(folders, dict) or not folders:
        _fail("Required setting 'folders' is missing or empty in config.yaml (map each CATEGORY to an output folder).")
    if DEFAULT_CATEGORY not in folders:
        _fail(
            f"'folders' must include a '{DEFAULT_CATEGORY}' entry — it is the fallback for unknown or unclassified categories."
        )


def load_config(config_path: Path = Path(__file__).parent / "config.yaml") -> dict:
    if not config_path.exists():
        print(
            f"ERROR: Config file not found: {config_path}\nCopy config.example.yaml to config.yaml and fill in your values.",
            flush=True,
        )
        sys.exit(1)

    cfg: dict = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    _validate(cfg)

    cfg["watch_folder"] = str(Path(cfg["watch_folder"]).expanduser())
    cfg["state_file"] = str(Path(cfg.get("state_file", "~/.meeting_transcriber_state.json")).expanduser())
    cfg["superwhisper_recordings_dir"] = str(
        Path(cfg.get("superwhisper_recordings_dir", "~/Documents/superwhisper/recordings")).expanduser()
    )

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
SUPERWHISPER_TIMEOUT: int = _cfg.get("superwhisper_timeout", 3600)
SUPERWHISPER_RECORDINGS_DIR: str = _cfg["superwhisper_recordings_dir"]
SUPERWHISPER_POLL_INTERVAL: int = _cfg.get("superwhisper_poll_interval", 3)

# Service behavior
SCAN_INTERVAL: int = _cfg.get("scan_interval", 30)
SCAN_DAYS_BACK: int = _cfg.get("scan_days_back", 7)
DELAY_BETWEEN_FILES: int = _cfg.get("delay_between_files", 30)
MAX_RETRIES: int = _cfg.get("max_retries", 3)
MAX_FILES_PER_CYCLE: int = _cfg.get("max_files_per_cycle", 5)
