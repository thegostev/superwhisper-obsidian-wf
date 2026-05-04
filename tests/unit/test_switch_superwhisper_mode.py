"""Unit tests for switch_superwhisper_mode()."""

import subprocess
from unittest.mock import call, patch

import pytest

from pipeline import FatalAPIError, switch_superwhisper_mode


def test_happy_path_opens_deep_link(monkeypatch):
    monkeypatch.setattr("pipeline.SUPERWHISPER_MODE_KEY", "meeting")
    with patch("subprocess.run") as mock_run, patch("time.sleep"):
        switch_superwhisper_mode()

    mock_run.assert_called_once_with(
        ["open", "superwhisper://mode?key=meeting"],
        check=True,
    )


def test_sleeps_after_opening(monkeypatch):
    monkeypatch.setattr("pipeline.SUPERWHISPER_MODE_KEY", "meeting")
    with patch("subprocess.run"), patch("time.sleep") as mock_sleep:
        switch_superwhisper_mode()

    mock_sleep.assert_called_once_with(1.0)


def test_empty_mode_key_raises_fatal(monkeypatch):
    monkeypatch.setattr("pipeline.SUPERWHISPER_MODE_KEY", "")
    with pytest.raises(FatalAPIError, match="superwhisper_mode_key"):
        switch_superwhisper_mode()


def test_custom_mode_key_used_in_url(monkeypatch):
    monkeypatch.setattr("pipeline.SUPERWHISPER_MODE_KEY", "custom-mode")
    with patch("subprocess.run") as mock_run, patch("time.sleep"):
        switch_superwhisper_mode()

    args = mock_run.call_args[0][0]
    assert "superwhisper://mode?key=custom-mode" in args
