"""Unit tests for config.load_config() boundary validation.

These exercise load_config() against temp files directly, so they never touch the
module-level config.yaml that conftest creates for the rest of the suite.
"""

from pathlib import Path

import pytest
import yaml

import config


def _write(tmp_path: Path, cfg) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(cfg) if cfg is not None else "")
    return p


_VALID = {
    "watch_folder": "/tmp/watch",
    "folders": {"WORK": "/tmp/work", "DEFAULT": "/tmp/default"},
}


def test_valid_config_loads(tmp_path):
    cfg = config.load_config(_write(tmp_path, _VALID))
    assert cfg["watch_folder"].endswith("watch")
    assert set(cfg["folders"]) == {"WORK", "DEFAULT"}


def test_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(tmp_path / "does-not-exist.yaml")


def test_empty_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(_write(tmp_path, None))


def test_missing_watch_folder_exits(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(_write(tmp_path, {"folders": {"DEFAULT": "/tmp/d"}}))


def test_empty_watch_folder_exits(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(_write(tmp_path, {"watch_folder": "", "folders": {"DEFAULT": "/tmp/d"}}))


def test_missing_folders_exits(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(_write(tmp_path, {"watch_folder": "/tmp/watch"}))


def test_empty_folders_exits(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(_write(tmp_path, {"watch_folder": "/tmp/watch", "folders": {}}))


def test_folders_without_default_exits(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(_write(tmp_path, {"watch_folder": "/tmp/watch", "folders": {"WORK": "/tmp/w"}}))
