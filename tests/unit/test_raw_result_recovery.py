"""LAG-805: persist rawResult (transcript) into state when an attempt fails.

Post-mortem 26-10-08, recommendation 4a: a recording whose *transcription*
completed but whose LLM pass never ran (abandoned stub), or whose LLM pass
refused the output contract (Class A), had its transcript discarded at fail
time. The failed attempt's state record now carries that transcript as
recovery material for triage/redrive — never as a saved note.

State schema (documented in ARCHITECTURE.md S3.M1): a failed record's
``recovery`` key is either ``None`` or::

    {"raw_result": str, "raw_result_chars": int, "truncated": bool,
     "duration_ms": int | None, "app_version": str | None,
     "recording_dir": str, "attempt": int}
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import pipeline
from pipeline import (
    PermanentFileError,
    _read_recording_meta,
    _recovery_material,
    process_audio,
    wait_for_superwhisper_result,
)

_FAKE_PATH = "/fake/path/2026-10-07/08-32-00.m4a"
_FAKE_TS = datetime(2026, 10, 7, 8, 32, 0)
_TRANSCRIPT = "Speaker 1: Let's go through the rollout plan for the gateway."


def _write_recording(recordings_dir: Path, name: str, mtime_offset: float, meta: dict) -> Path:
    rec_dir = recordings_dir / name
    rec_dir.mkdir(parents=True)
    (rec_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    ts = time.time() + mtime_offset
    os.utime(rec_dir / "meta.json", (ts, ts))
    os.utime(rec_dir, (ts, ts))
    return rec_dir


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    rd = tmp_path / "recordings"
    rd.mkdir()
    monkeypatch.setattr("pipeline.SUPERWHISPER_RECORDINGS_DIR", str(rd))
    monkeypatch.setattr("pipeline.SUPERWHISPER_POLL_INTERVAL", 0.01)
    monkeypatch.setattr("pipeline.SUPERWHISPER_TIMEOUT", 5)
    return rd


# --- _read_recording_meta ---------------------------------------------------


def test_read_recording_meta_extracts_raw_result_and_app_version(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"rawResult": _TRANSCRIPT, "appVersion": "2.4.1", "duration": 606000}))
    info = _read_recording_meta(d)
    assert info is not None
    assert info["raw_result"] == _TRANSCRIPT
    assert info["app_version"] == "2.4.1"
    assert info["duration_ms"] == 606000


def test_read_recording_meta_raw_result_absent_is_none(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"processingTime": 0}))
    info = _read_recording_meta(d)
    assert info is not None
    assert info["raw_result"] is None
    assert info["app_version"] is None


# --- _recovery_material -----------------------------------------------------


def test_recovery_material_none_when_raw_result_absent_or_blank():
    base = {"duration_ms": 1000, "app_version": "2.4.1"}
    assert _recovery_material({**base, "raw_result": None}, Path("/x/rec")) is None
    assert _recovery_material({**base, "raw_result": "   \n"}, Path("/x/rec")) is None


def test_recovery_material_carries_duration_app_version_and_dir():
    info = {"raw_result": _TRANSCRIPT, "duration_ms": 606000, "app_version": "2.4.1"}
    rec = _recovery_material(info, Path("/x/1788857138"))
    assert rec == {
        "raw_result": _TRANSCRIPT,
        "raw_result_chars": len(_TRANSCRIPT),
        "truncated": False,
        "duration_ms": 606000,
        "app_version": "2.4.1",
        "recording_dir": "1788857138",
    }


def test_recovery_material_caps_storage_size(monkeypatch):
    monkeypatch.setattr("pipeline.RAW_RESULT_MAX_CHARS", 10)
    info = {"raw_result": "x" * 25, "duration_ms": None, "app_version": None}
    rec = _recovery_material(info, Path("/x/rec"))
    assert rec is not None
    assert rec["raw_result"] == "x" * 10
    assert rec["raw_result_chars"] == 25
    assert rec["truncated"] is True


# --- wait_for_superwhisper_result attaches recovery to the raised error ------


def test_abandoned_stub_timeout_carries_raw_result(recordings_dir):
    """Transcription ran, LLM pass never did: the stub's transcript rides the error."""
    meta = {"rawResult": _TRANSCRIPT, "processingTime": 18000, "duration": 606000, "appVersion": "2.4.1"}
    _write_recording(recordings_dir, "333", -1.0, meta)
    with (
        patch("time.sleep"),
        patch("pipeline.RECORDING_STABILITY_POLLS", 2),
        pytest.raises(TimeoutError) as excinfo,
    ):
        wait_for_superwhisper_result("foo.m4a", since=time.time() - 2)
    rec = excinfo.value.recovery  # type: ignore[attr-defined]
    assert rec["raw_result"] == _TRANSCRIPT
    assert rec["duration_ms"] == 606000
    assert rec["app_version"] == "2.4.1"
    assert rec["recording_dir"] == "333"


def test_contract_refusal_carries_raw_result(recordings_dir):
    """Class A refusal: llmResult without CATEGORY — the transcript is kept."""
    meta = {"llmResult": "I cannot summarise this.", "rawResult": _TRANSCRIPT, "duration": 1000}
    _write_recording(recordings_dir, "222", 0.0, meta)
    with patch("time.sleep"), pytest.raises(PermanentFileError) as excinfo:
        wait_for_superwhisper_result("foo.m4a", since=time.time() - 1)
    rec = excinfo.value.recovery  # type: ignore[attr-defined]
    assert rec["raw_result"] == _TRANSCRIPT
    assert rec["app_version"] is None


def test_stub_without_raw_result_carries_none(recordings_dir):
    _write_recording(recordings_dir, "555", -1.0, {"processingTime": 0, "duration": 1000})
    with (
        patch("time.sleep"),
        patch("pipeline.RECORDING_STABILITY_POLLS", 2),
        pytest.raises(TimeoutError) as excinfo,
    ):
        wait_for_superwhisper_result("foo.m4a", since=time.time() - 2)
    assert excinfo.value.recovery is None  # type: ignore[attr-defined]


def test_deadline_timeout_carries_last_seen_raw_result(recordings_dir, monkeypatch):
    """Poll deadline hit while a matched dir holds a transcript (LLM pass in flight)."""
    meta = {"rawResult": _TRANSCRIPT, "languageModelProcessingTime": 5000, "duration": 1000, "appVersion": "2.4.1"}
    _write_recording(recordings_dir, "666", -1.0, meta)
    clock = {"t": time.time()}

    def fake_time():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr("pipeline.SUPERWHISPER_TIMEOUT", 3)
    with (
        patch("time.sleep"),
        patch("pipeline.time.time", side_effect=fake_time),
        pytest.raises(TimeoutError) as excinfo,
    ):
        wait_for_superwhisper_result("foo.m4a", since=clock["t"] - 5)
    rec = excinfo.value.recovery  # type: ignore[attr-defined]
    assert rec is not None
    assert rec["raw_result"] == _TRANSCRIPT
    assert rec["recording_dir"] == "666"


# --- process_audio persists recovery into state -----------------------------

_RECOVERY = {
    "raw_result": _TRANSCRIPT,
    "raw_result_chars": len(_TRANSCRIPT),
    "truncated": False,
    "duration_ms": 606000,
    "app_version": "2.4.1",
    "recording_dir": "333",
}


def _error_with(exc: BaseException, recovery: dict | None) -> BaseException:
    exc.recovery = recovery  # type: ignore[attr-defined]
    return exc


def _run(state: dict, error: BaseException):
    with (
        patch("pipeline.get_audio_duration_ms", return_value=606000),
        patch("pipeline.switch_superwhisper_mode"),
        patch("pipeline.handoff_to_superwhisper", return_value=time.time()),
        patch("pipeline.wait_for_superwhisper_result", side_effect=error),
        patch("pipeline.save_output") as save_output,
        patch("pipeline.save_state"),
        patch("pipeline._notify_dependency_down", return_value=True),
    ):
        result = process_audio(_FAKE_PATH, _FAKE_TS, state)
    return result, save_output


def test_failed_retry_persists_recovery_with_duration_and_app_version():
    state: dict = {"processed": {}}
    (ok, _), save_output = _run(state, _error_with(TimeoutError("stub"), dict(_RECOVERY)))
    assert ok is False
    record = state["processed"][_FAKE_PATH]
    assert record["status"] == "failed_retry"
    assert record["recovery"] == {**_RECOVERY, "attempt": 1}
    save_output.assert_not_called()  # recovery material is never saved as a note


def test_failed_permanent_refusal_persists_recovery():
    state: dict = {"processed": {}}
    (ok, _), save_output = _run(state, _error_with(PermanentFileError("refused"), dict(_RECOVERY)))
    record = state["processed"][_FAKE_PATH]
    assert record["status"] == "failed_permanent"
    assert record["recovery"]["raw_result"] == _TRANSCRIPT
    assert record["recovery"]["app_version"] == "2.4.1"
    save_output.assert_not_called()


def test_failed_attempt_without_raw_result_records_null():
    state: dict = {"processed": {}}
    _run(state, _error_with(TimeoutError("stub"), None))
    assert state["processed"][_FAKE_PATH]["recovery"] is None


def test_error_without_recovery_attribute_records_null():
    state: dict = {"processed": {}}
    _run(state, RuntimeError("boom"))
    assert state["processed"][_FAKE_PATH]["recovery"] is None


def test_later_attempt_without_raw_result_keeps_earlier_transcript():
    """A retry that yields nothing must not erase the transcript an earlier attempt saved."""
    earlier = {**_RECOVERY, "attempt": 1}
    state: dict = {"processed": {_FAKE_PATH: {"status": "failed_retry", "attempts": 1, "recovery": earlier}}}
    _run(state, _error_with(TimeoutError("stub again"), None))
    record = state["processed"][_FAKE_PATH]
    assert record["attempts"] == 2
    assert record["recovery"] == earlier


def test_later_attempt_with_raw_result_replaces_earlier_transcript():
    earlier = {**_RECOVERY, "raw_result": "old", "attempt": 1}
    state: dict = {"processed": {_FAKE_PATH: {"status": "failed_retry", "attempts": 1, "recovery": earlier}}}
    _run(state, _error_with(TimeoutError("stub"), dict(_RECOVERY)))
    assert state["processed"][_FAKE_PATH]["recovery"] == {**_RECOVERY, "attempt": 2}


def test_raw_result_max_chars_is_bounded():
    assert 0 < pipeline.RAW_RESULT_MAX_CHARS <= 500_000
