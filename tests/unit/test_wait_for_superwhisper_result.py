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


def test_does_not_fast_fail_while_llm_pass_is_in_flight(recordings_dir):
    """languageModelProcessingTime present (LLM pass started) but llmResult not yet written.

    Reproduces the July 22 bug: a 21-minute meeting took ~23s of LLM inference. The old
    stability check (5 polls × 3s = 15s) fast-failed mid-inference, then retried by
    re-opening the file, which interrupted the in-flight pass and created a new stub.
    The daemon marked the file failed_permanent despite Superwhisper eventually writing
    a valid llmResult. With the fix, the poller must keep waiting until the LLM pass
    finishes and writes llmResult (or the deadline hits).
    """
    # meta.json stable (mtime in the past), no llmResult, but LLM pass has started.
    meta = {
        "processingTime": 0,
        "languageModelProcessingTime": 5000,  # LLM pass in flight
        "duration": 1299000,
    }
    _write_recording(recordings_dir, "444", -10.0, meta)  # mtime 10s ago, won't change
    since = time.time() - 11

    # Poll twice the stability threshold — must NOT raise. Patch deadline so we don't
    # wait the full SUPERWHISPER_TIMEOUT (5s in the fixture).
    call_count = {"n": 0}

    def fake_sleep(_):
        call_count["n"] += 1

    # Short timeout so the test fails fast if the bug regresses (would raise mid-poll).
    with patch("time.sleep", fake_sleep), patch("pipeline.RECORDING_STABILITY_POLLS", 2), patch(
        "pipeline.SUPERWHISPER_TIMEOUT", 0.2
    ):
        with pytest.raises(TimeoutError, match="did not return"):
            wait_for_superwhisper_result("foo.m4a", since=since)
    # If the bug regressed, we'd have raised "empty recording stub" TimeoutError before
    # hitting the deadline. The "did not return" match confirms we waited the full window.


# --- wait_for_superwhisper_result: timeout still works for in-progress ------

def test_timeout_when_recording_never_appears(recordings_dir, monkeypatch):
    """No recordings newer than `since` → should hit the timeout."""
    monkeypatch.setattr("pipeline.SUPERWHISPER_TIMEOUT", 0.05)
    since = time.time() + 100  # force all recordings to be "older" than since
    with patch("time.sleep"), pytest.raises(TimeoutError, match="did not return"):
        wait_for_superwhisper_result("foo.m4a", since=since)