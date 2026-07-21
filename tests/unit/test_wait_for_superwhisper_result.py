"""Unit tests for wait_for_superwhisper_result() and _read_superwhisper_entry()."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import (
    CATEGORY_HEADER,
    PermanentFileError,
    _read_superwhisper_entry,
    wait_for_superwhisper_result,
)


def _write_recording(recordings_dir: Path, name: str, mtime_offset: float, meta: dict) -> Path:
    """Create a fake recording directory with meta.json, mtime set relative to now."""
    rec_dir = recordings_dir / name
    rec_dir.mkdir(parents=True)
    (rec_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    ts = time.time() + mtime_offset
    import os
    os.utime(rec_dir / "meta.json", (ts, ts))
    os.utime(rec_dir, (ts, ts))
    return rec_dir


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    """Temporary superwhisper recordings dir."""
    rd = tmp_path / "recordings"
    rd.mkdir()
    monkeypatch.setattr("pipeline.SUPERWHISPER_RECORDINGS_DIR", str(rd))
    monkeypatch.setattr("pipeline.SUPERWHISPER_POLL_INTERVAL", 0.01)
    monkeypatch.setattr("pipeline.SUPERWHISPER_TIMEOUT", 5)
    return rd


# --- _read_superwhisper_entry ------------------------------------------------

def test_read_entry_returns_llmresult_when_present():
    rec = Path(__file__).parent / "fixtures" / "has_llm"
    # inline fixture: build temp
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "rec"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"llmResult": "CATEGORY: PERSONAL\nok"}))
        assert _read_superwhisper_entry(d) == "CATEGORY: PERSONAL\nok"


def test_read_entry_returns_none_when_llmresult_absent():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "rec"
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"result": "transcript only"}))
        assert _read_superwhisper_entry(d) is None


def test_read_entry_returns_none_on_missing_meta():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        assert _read_superwhisper_entry(Path(td) / "nope") is None


# --- wait_for_superwhisper_result: success path -----------------------------

def test_returns_result_when_category_present(recordings_dir):
    meta = {"llmResult": f"{CATEGORY_HEADER} PERSONAL\nFILENAME: x\n\nbody"}
    _write_recording(recordings_dir, "111", 0.0, meta)
    since = time.time() - 1
    with patch("time.sleep"):
        result = wait_for_superwhisper_result("foo.m4a", since=since)
    assert CATEGORY_HEADER in result


# --- wait_for_superwhisper_result: fast-fail on LLM refusal -----------------

def test_fails_fast_on_llm_refusal_without_category(recordings_dir):
    """llmResult present but no CATEGORY: header = LLM refused the contract."""
    meta = {"llmResult": "I notice your message is only a fragment, not a meeting."}
    _write_recording(recordings_dir, "222", 0.0, meta)
    since = time.time() - 1
    with patch("time.sleep"), pytest.raises(PermanentFileError, match="refused|no CATEGORY|llmResult"):
        wait_for_superwhisper_result("foo.m4a", since=since)


# --- wait_for_superwhisper_result: transient fail on abandoned stub ------------

def test_retries_when_transcription_done_but_no_llm_pass(recordings_dir):
    """result/rawResult present (transcription ran) but llmResult absent and meta.json stable.

    Superwhisper creates an empty stub when overwhelmed by rapid file-opens; the stub is
    never filled in. This is transient — re-opening the audio file later should work, so
    the poller raises TimeoutError (→ failed_retry), NOT PermanentFileError.
    """
    meta = {"result": "transcript text", "rawResult": "transcript text", "processingTime": 18000}
    _write_recording(recordings_dir, "333", -1.0, meta)  # mtime 1s ago
    since = time.time() - 2
    with patch("time.sleep"), patch("pipeline.RECORDING_STABILITY_POLLS", 2):
        with pytest.raises(TimeoutError, match="empty recording stub|no llmResult|stable"):
            wait_for_superwhisper_result("foo.m4a", since=since)


# --- wait_for_superwhisper_result: timeout still works for in-progress ------

def test_timeout_when_recording_never_appears(recordings_dir, monkeypatch):
    """No recordings newer than `since` → should hit the timeout."""
    monkeypatch.setattr("pipeline.SUPERWHISPER_TIMEOUT", 0.05)
    since = time.time() + 100  # force all recordings to be "older" than since
    with patch("time.sleep"), pytest.raises(TimeoutError, match="did not return"):
        wait_for_superwhisper_result("foo.m4a", since=since)