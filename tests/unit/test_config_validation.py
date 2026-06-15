"""Unit tests for config.validate_config().

validate_config() is a pure function over a parsed dict, so it can be tested
without triggering config.py's import-time load.
"""

from pathlib import Path

import pytest

from config import validate_config

_PATH = Path("/tmp/config.yaml")


def _valid_cfg():
    return {
        "watch_folder": "~/recordings",
        "folders": {"DEFAULT": "~/vault/Inbox", "WORK": "~/vault/Work"},
    }


def test_valid_config_passes_through():
    cfg = _valid_cfg()
    assert validate_config(cfg, _PATH) is cfg


@pytest.mark.parametrize("bad", [None, [], "watch_folder: x", 42])
def test_non_dict_config_exits(bad):
    with pytest.raises(SystemExit):
        validate_config(bad, _PATH)


def test_missing_watch_folder_exits():
    cfg = _valid_cfg()
    del cfg["watch_folder"]
    with pytest.raises(SystemExit):
        validate_config(cfg, _PATH)


def test_empty_folders_exits():
    cfg = _valid_cfg()
    cfg["folders"] = {}
    with pytest.raises(SystemExit):
        validate_config(cfg, _PATH)


def test_folders_without_default_exits():
    cfg = _valid_cfg()
    cfg["folders"] = {"WORK": "~/vault/Work"}
    with pytest.raises(SystemExit):
        validate_config(cfg, _PATH)
