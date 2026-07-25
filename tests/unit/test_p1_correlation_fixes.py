"""Tests for the P1 correlation fixes (R3, R4, R5, R7, R8, R13).

Covers the failure modes documented in the 2026-07-24 postmortem:
  - R3: duration-based correlation prevents off-by-one misrouting
  - R4: consumption marker prevents a retry from re-matching a used recording
  - R5: write-stability check prevents truncated reads mid-stream
  - R7: handoff sequencing waits for Superwhisper to be idle
  - R8: salvage pass matches by duration (datetime window was unreliable)
  - R13: success log includes dir name, byte count, and content hash
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import (
    CATEGORY_HEADER,
    CONSUMED_SENTINEL,
    PermanentFileError,
    _is_superwhisper_idle,
    _find_best_candidate_by_duration,
    _mark_consumed,
    get_audio_duration_ms,
    handoff_to_superwhisper,
    recover_failed_permanent,
    wait_for_superwhisper_result,
)


def _write_recording(recordings_dir: Path, name: str, mtime_offset: float, meta: dict) -> Path:
    rec_dir = recordings_dir / name
    rec_dir.mkdir(parents=True, exist_ok=True)
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


# --- R3: duration-based correlation ------------------------------------------


def test_skips_recording_with_mismatched_duration(recordings_dir):
    """A recording whose duration differs from the source audio by >5% is skipped.

    Reproduces the Jul 24 off-by-one: a retry handoff for the 13-31 audio (24 min)
    would otherwise match the 14-00 audio's recording (30 min). With R3, the 30-min
    recording is skipped because 30 min is >5% off from 24 min.
    """
    # 30-min recording in the dir (would be picked by mtime-only correlation)
    _write_recording(
        recordings_dir,
        "1784881520",
        0.0,
        {
            "duration": 1797000,  # 29:57
            "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: wrong match\n\nbody",
        },
    )
    since = time.time() - 1
    with patch("time.sleep"), pytest.raises(TimeoutError, match="did not return"):
        # Source audio is 24:02 = 1442000 ms; 1797000 is 24.6% off, outside 5% tolerance
        wait_for_superwhisper_result("foo.m4a", since=since, expected_duration_ms=1442000)


def test_matches_recording_with_matching_duration(recordings_dir):
    """A recording whose duration matches the source audio within tolerance is returned."""
    _write_recording(
        recordings_dir,
        "1784881425",
        0.0,
        {
            "duration": 1442000,  # 24:02 — exact match
            "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: right match\n\nbody",
        },
    )
    since = time.time() - 1
    with patch("time.sleep"):
        result = wait_for_superwhisper_result("foo.m4a", since=since, expected_duration_ms=1442000)
    assert "right match" in result


def test_duration_match_within_tolerance(recordings_dir):
    """A recording 4% off from expected duration is still matched (within 5% tolerance)."""
    _write_recording(
        recordings_dir,
        "1",
        0.0,
        {
            "duration": 1500000,  # 4% off from 1442000
            "llmResult": f"{CATEGORY_HEADER} X\nFILENAME: y\n\nbody",
        },
    )
    since = time.time() - 1
    with patch("time.sleep"):
        result = wait_for_superwhisper_result("foo.m4a", since=since, expected_duration_ms=1442000)
    assert CATEGORY_HEADER in result


# --- R5: write-stability check -------------------------------------------------


def test_does_not_return_on_first_category_sighting(recordings_dir):
    """R5: a recording with CATEGORY: in llmResult must be polled twice (stable mtime+size)
    before returning. Prevents truncated reads when Superwhisper is still appending.
    """
    _write_recording(
        recordings_dir,
        "1",
        0.0,
        {"duration": 600000, "llmResult": f"{CATEGORY_HEADER} X\nFILENAME: y\n\nbody"},
    )
    since = time.time() - 1

    # Track how many times the poller reads meta.json. With WRITE_STABILITY_POLLS=2,
    # the first read sets stability count to 1, the second read returns.
    call_count = {"n": 0}
    real_read = Path.read_text

    def counting_read(self, *args, **kwargs):
        call_count["n"] += 1
        return real_read(self, *args, **kwargs)

    with patch("time.sleep"), patch.object(Path, "read_text", counting_read):
        result = wait_for_superwhisper_result("foo.m4a", since=since, expected_duration_ms=600000)

    assert CATEGORY_HEADER in result
    # Should have read meta.json at least twice (once for stability count 1, once for the return)
    assert call_count["n"] >= 2, f"Expected >=2 reads for write-stability, got {call_count['n']}"


# --- R4: consumption marker ---------------------------------------------------


def test_mark_consumed_writes_sentinel(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    _mark_consumed(rec)
    assert (rec / CONSUMED_SENTINEL).exists()


def test_consumed_recording_is_skipped_by_poller(recordings_dir):
    """R4: a recording marked consumed is not re-matched by a later retry."""
    rec = _write_recording(
        recordings_dir,
        "1",
        0.0,
        {
            "duration": 600000,
            "llmResult": f"{CATEGORY_HEADER} X\nFILENAME: already-used\n\nbody",
        },
    )
    _mark_consumed(rec)

    since = time.time() - 1
    with patch("time.sleep"), pytest.raises(TimeoutError, match="did not return"):
        wait_for_superwhisper_result("foo.m4a", since=since, expected_duration_ms=600000)


def test_successful_match_marks_recording_consumed(recordings_dir):
    """R4: after a successful match, the recording dir has the consumed sentinel."""
    rec = _write_recording(
        recordings_dir,
        "1",
        0.0,
        {
            "duration": 600000,
            "llmResult": f"{CATEGORY_HEADER} X\nFILENAME: y\n\nbody",
        },
    )
    since = time.time() - 1
    with patch("time.sleep"):
        wait_for_superwhisper_result("foo.m4a", since=since, expected_duration_ms=600000)
    assert (rec / CONSUMED_SENTINEL).exists(), "consumption sentinel not written after success"


# --- R7: handoff sequencing ----------------------------------------------------


def test_is_superwhisper_idle_when_no_recordings(recordings_dir):
    assert _is_superwhisper_idle() is True


def test_is_superwhisper_idle_when_llm_pass_in_flight(recordings_dir):
    """A recording with languageModelProcessingTime set but no llmResult = in-flight."""
    _write_recording(
        recordings_dir,
        "1",
        0.0,
        {"duration": 600000, "processingTime": 0, "languageModelProcessingTime": 5000},
    )
    assert _is_superwhisper_idle() is False


def test_is_superwhisper_idle_when_llm_pass_complete(recordings_dir):
    """A recording with llmResult present = LLM pass done, idle."""
    _write_recording(
        recordings_dir,
        "1",
        0.0,
        {
            "duration": 600000,
            "languageModelProcessingTime": 5000,
            "llmResult": f"{CATEGORY_HEADER} X\nFILENAME: y\n\nbody",
        },
    )
    assert _is_superwhisper_idle() is True


def test_handoff_waits_for_idle_before_open(recordings_dir, tmp_path, monkeypatch):
    """R7: handoff_to_superwhisper calls _wait_for_superwhisper_idle before `open`."""
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")

    idle_calls = {"n": 0}

    def fake_wait_idle(timeout=300):
        idle_calls["n"] += 1

    open_calls = []

    def fake_run(cmd, **kwargs):
        open_calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("pipeline._wait_for_superwhisper_idle", fake_wait_idle)
    monkeypatch.setattr("subprocess.run", fake_run)

    handoff_to_superwhisper(str(audio))

    assert idle_calls["n"] == 1, "handoff did not wait for idle before open"
    assert any("Superwhisper" in str(c) for c in open_calls), "open -a Superwhisper not called"


# --- R8: duration-based salvage matching --------------------------------------


def test_recovery_matches_by_duration_even_when_datetime_far_off(tmp_path, monkeypatch):
    """R8: salvage matches by duration, not datetime. Reproduces the Jul 24 case where
    meta.datetime was ~20h off from the audio recording time (iCloud sync delay), so the
    datetime-window match failed but duration match succeeds.
    """
    rd = tmp_path / "recordings"
    rd.mkdir()
    monkeypatch.setattr("pipeline.SUPERWHISPER_RECORDINGS_DIR", str(rd))

    state_path = tmp_path / "state.json"
    state = {
        "processed": {
            "/audio/14-30-18.m4a": {
                "status": "failed_permanent",
                "error": "empty stub",
                "timestamp": "2026-07-23T14:30:18",
                "processed_at": "2026-07-24T10:22:33",
                "attempts": 3,
                "expected_duration_ms": 3435000,  # 57:15 — matches the stub
            }
        }
    }
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr("pipeline.STATE_FILE", str(state_path))

    # Stub with datetime ~20h off from audio timestamp — datetime-window match would fail.
    # But duration matches expected_duration_ms exactly.
    _write_recording(
        rd,
        "1784881320",
        0.0,
        {
            "datetime": "2026-07-24T08:22:00",  # ~20h after audio recording time
            "duration": 3435000,
            "llmResult": f"{CATEGORY_HEADER} MUSIKERE\nFILENAME: Sample Project Sync\n\nbody",
        },
    )

    out_dir = tmp_path / "vault" / "Musikere"
    out_dir.mkdir(parents=True)
    monkeypatch.setattr("pipeline.FOLDERS", {"MUSIKERE": str(out_dir), "DEFAULT": str(out_dir)})

    recovered = recover_failed_permanent(state)
    assert recovered == 1
    entry = state["processed"]["/audio/14-30-18.m4a"]
    assert entry["status"] == "complete"
    assert "duration match" in entry["note"]


def test_recovery_duration_helper_picks_latest_matching(tmp_path, monkeypatch):
    """When multiple candidates have matching durations, the latest mtime wins."""
    rd = tmp_path / "recordings"
    rd.mkdir()
    monkeypatch.setattr("pipeline.SUPERWHISPER_RECORDINGS_DIR", str(rd))

    candidates = [
        (1000.0, None, 3435000, "old"),
        (2000.0, None, 3435000, "new"),
        (3000.0, None, 9999999, "wrong duration"),  # different duration, skipped
    ]
    best = _find_best_candidate_by_duration(candidates, expected_duration_ms=3435000)
    assert best is not None
    assert best[3] == "new"


def test_recovery_returns_none_when_duration_missing():
    candidates = [(1000.0, None, 0, "no duration")]
    assert _find_best_candidate_by_duration(candidates, expected_duration_ms=3435000) is None


def test_recovery_returns_none_when_expected_duration_none():
    candidates = [(1000.0, None, 3435000, "x")]
    assert _find_best_candidate_by_duration(candidates, expected_duration_ms=None) is None


# --- R13: logging reliability --------------------------------------------------


def test_success_log_includes_dir_hash_and_bytes(recordings_dir, capsys):
    """R13: log line includes dir name, byte count, and content hash for post-hoc verification."""
    body = f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: test\n\nbody content here"
    _write_recording(
        recordings_dir,
        "1784889999",
        0.0,
        {"duration": 600000, "llmResult": body},
    )
    since = time.time() - 1
    with patch("time.sleep"):
        wait_for_superwhisper_result("foo.m4a", since=since, expected_duration_ms=600000)

    captured = capsys.readouterr()
    assert "1784889999" in captured.out, "dir name not in log"
    assert "bytes=" in captured.out, "byte count not in log"
    assert "hash=" in captured.out, "content hash not in log"