"""Unit tests for config.validate_config — the startup fail-fast guard.

These pin the contract that a config missing required fields (or the mandatory
DEFAULT folder) is rejected at load time with a clear message, rather than
crashing deep in the pipeline after a Superwhisper pass has been spent.
"""

import pytest

from config import validate_config

_VALID = {
    "watch_folder": "/tmp/watch",
    "folders": {"WORK": "/tmp/work", "DEFAULT": "/tmp/default"},
}


def test_valid_config_passes():
    validate_config(_VALID)  # should not raise


def test_missing_watch_folder_rejected():
    with pytest.raises(ValueError, match="watch_folder"):
        validate_config({"folders": {"DEFAULT": "/tmp/default"}})


def test_missing_folders_rejected():
    with pytest.raises(ValueError, match="folders"):
        validate_config({"watch_folder": "/tmp/watch"})


def test_empty_folders_rejected():
    with pytest.raises(ValueError, match="folders"):
        validate_config({"watch_folder": "/tmp/watch", "folders": {}})


def test_folders_not_a_mapping_rejected():
    with pytest.raises(ValueError, match="mapping"):
        validate_config({"watch_folder": "/tmp/watch", "folders": ["WORK", "DEFAULT"]})


def test_missing_default_category_rejected():
    with pytest.raises(ValueError, match="DEFAULT"):
        validate_config({"watch_folder": "/tmp/watch", "folders": {"WORK": "/tmp/work"}})
