"""Unit tests for load_config() validation.

These pin the fail-fast behaviour: a misconfigured config.yaml must stop the
service at startup with a clear error, not crash deep in the pipeline later.
load_config() is parameterized by path, so it can be exercised in isolation
without the import-time load in config.py.
"""

import pytest
import yaml

from config import load_config


def _write(tmp_path, cfg):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg) if isinstance(cfg, (dict, list)) else cfg)
    return path


_VALID = {
    "watch_folder": "/tmp/watch",
    "folders": {"WORK": "/tmp/work", "DEFAULT": "/tmp/default"},
}


def test_valid_config_loads(tmp_path):
    cfg = load_config(_write(tmp_path, _VALID))
    assert cfg["folders"]["DEFAULT"].endswith("default")
    assert cfg["watch_folder"].endswith("watch")


def test_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        load_config(tmp_path / "does-not-exist.yaml")


def test_empty_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, ""))


def test_missing_watch_folder_exits(tmp_path):
    cfg = {"folders": {"DEFAULT": "/tmp/default"}}
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, cfg))


def test_missing_folders_exits(tmp_path):
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, {"watch_folder": "/tmp/watch"}))


def test_folders_without_default_exits(tmp_path):
    cfg = {"watch_folder": "/tmp/watch", "folders": {"WORK": "/tmp/work"}}
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, cfg))


def test_folders_not_a_mapping_exits(tmp_path):
    cfg = {"watch_folder": "/tmp/watch", "folders": ["WORK", "DEFAULT"]}
    with pytest.raises(SystemExit):
        load_config(_write(tmp_path, cfg))
