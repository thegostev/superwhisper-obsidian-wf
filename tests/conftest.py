"""Shared test fixtures for RecordingAnalyser."""

import json
from pathlib import Path

import pytest

# Create a test config.yaml BEFORE config.py is imported by any test module.
# config.py loads at import time, so the file must exist first.
_PROJECT_DIR = Path(__file__).parent.parent
_TEST_CONFIG = _PROJECT_DIR / "config.yaml"

if not _TEST_CONFIG.exists():
    import yaml

    _test_cfg = {
        "watch_folder": "/tmp/test-watch-folder",
        "folders": {
            "WORK": "/tmp/test-work",
            "PERSONAL": "/tmp/test-personal",
            "DEFAULT": "/tmp/test-default",
        },
        "state_file": "/tmp/test-state.json",
        "failed_analysis_log": "/tmp/test-failed.log",
        "superwhisper_mode_key": "test-mode-key",
    }
    _TEST_CONFIG.write_text(yaml.dump(_test_cfg))
    _CREATED_TEST_CONFIG = True
else:
    _CREATED_TEST_CONFIG = False


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
        category="PERSONLIG",
        filename="Test Meeting",
        analysis="## Summary\nTest analysis content.",
    ):
        return f"CATEGORY: {category}\nFILENAME: {filename}\n\n{analysis}"

    return _make_output
