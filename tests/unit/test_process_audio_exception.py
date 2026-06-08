"""Characterization tests: process_audio exception-branch state transitions.

These tests pin the state-dict contents written by the generic except branch
so that structural refactors cannot silently change retry/permanent behaviour.
"""

from datetime import datetime
from unittest.mock import patch

from config import MAX_RETRIES
from pipeline import process_audio

_FAKE_PATH = "/fake/path/2026-05-01/12-00-00.m4a"
_FAKE_TS = datetime(2026, 5, 1, 12, 0, 0)


def _run(state):
    with (
        patch("pipeline.switch_superwhisper_mode", side_effect=RuntimeError("boom")),
        patch("pipeline.save_state"),
    ):
        return process_audio(_FAKE_PATH, _FAKE_TS, state)


def test_first_failure_records_failed_retry():
    state = {"processed": {}}
    success, category = _run(state)

    assert success is False
    assert category is None
    record = state["processed"][_FAKE_PATH]
    assert record["status"] == "failed_retry"
    assert record["attempts"] == 1
    assert "boom" in record["error"]


def test_max_retries_records_failed_permanent():
    state = {"processed": {_FAKE_PATH: {"attempts": MAX_RETRIES - 1}}}
    success, category = _run(state)

    assert success is False
    assert category is None
    record = state["processed"][_FAKE_PATH]
    assert record["status"] == "failed_permanent"
    assert record["attempts"] == MAX_RETRIES
    assert "boom" in record["error"]


def test_returns_false_none_on_exception():
    state = {"processed": {}}
    result = _run(state)
    assert result == (False, None)
