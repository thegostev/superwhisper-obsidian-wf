"""Shared test fixtures for SuperwhisperObsidianWF.

Test config is always written to a temp location and config.py is forced to
load it via the SWOWF_CONFIG_PATH env var, so tests never depend on the
user's real config.yaml (which may have personal paths / category names).
"""

import json
import os
import time
from pathlib import Path

import pytest
import yaml

_PROJECT_DIR = Path(__file__).parent.parent

# Pin the local timezone for the whole session. get_audio_timestamp() converts
# UTC mdls output to local time via datetime.astimezone() — tests assert CEST
# (+02:00) expectations, but CI runners default to UTC. Setting TZ before any
# test module imports makes astimezone() deterministic across Linux/macOS
# runners and dev machines. Must run before config.py loads at import time.
os.environ["TZ"] = "Europe/Oslo"
time.tzset()

# Build a deterministic test config and point config.py at it before any
# test module imports config.py (which loads at import time).
_TEST_CFG = {
    "watch_folder": "/tmp/test-watch-folder",
    "folders": {
        "WORK": "/tmp/test-work",
        "TEAM": "/tmp/test-team",
        "PERSONAL": "/tmp/test-personal",
        "INTERVIEWS": "/tmp/test-interviews",
        "DEFAULT": "/tmp/test-default",
    },
    "state_file": "/tmp/test-state.json",
    "failed_analysis_log": "/tmp/test-failed.log",
    "superwhisper_mode_key": "test-mode-key",
}
_TEST_CONFIG_PATH = _PROJECT_DIR / "tests" / "_test_config.yaml"
_TEST_CONFIG_PATH.write_text(yaml.dump(_TEST_CFG))
os.environ["SWOWF_CONFIG_PATH"] = str(_TEST_CONFIG_PATH)


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Temporary directory structure mimicking Obsidian vault output.

    Files land directly in the category folder (no transcripts/ or analysis/ subfolders).
    """
    categories = ["WORK", "PERSONAL", "DEFAULT"]
    for cat in categories:
        (tmp_path / cat).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def sample_state():
    """Sample processing state dict."""
    return {
        "processed": {
            "/path/to/test_audio.m4a": {
                "status": "complete",
                "category": "WORK",
                "timestamp": "2026-02-21T14:30:00",
                "processed_at": "2026-02-21T14:35:00",
                "attempts": 1,
            }
        }
    }


@pytest.fixture
def state_file(tmp_path, sample_state):
    """Temporary state file with sample data."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(sample_state))
    return state_path


@pytest.fixture
def mock_superwhisper_output():
    """Factory fixture for mock Superwhisper Custom Mode output strings."""

    def _make_output(
        category="PERSONAL",
        filename="Test Meeting",
        analysis="## Summary\nTest analysis content.",
    ):
        return f"CATEGORY: {category}\nFILENAME: {filename}\n\n{analysis}"

    return _make_output
