"""Unit tests for recover_failed_permanent().

Verifies that a failed_permanent state entry is salvaged when a Superwhisper recording
stub later completes with a valid llmResult, and that the .md is written to the
category folder with the correct timestamp prefix.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import CATEGORY_HEADER, recover_failed_permanent


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    rd = tmp_path / "recordings"
    rd.mkdir()
    monkeypatch.setattr("pipeline.SUPERWHISPER_RECORDINGS_DIR", str(rd))
    return rd


@pytest.fixture
def state_with_failed_entry(tmp_path, monkeypatch):
    """State with one failed_permanent entry whose audio was recorded at 2026-07-22 13:59:43 local."""
    state_path = tmp_path / "state.json"
    state = {
        "processed": {
            "/recordings/2026-07-22/13-59-43.m4a": {
                "status": "failed_permanent",
                "error": "empty stub",
                "timestamp": "2026-07-22T13:59:43",
                "processed_at": "2026-07-22T14:25:22",
                "attempts": 3,
            }
        }
    }
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr("pipeline.STATE_FILE", str(state_path))
    return state


def _write_stub(recordings_dir: Path, name: str, meta: dict, mtime_offset: float = 0.0) -> Path:
    rec = recordings_dir / name
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    ts = time.time() + mtime_offset
    os.utime(rec / "meta.json", (ts, ts))
    os.utime(rec, (ts, ts))
    return rec


def test_recover_when_stub_has_valid_llmresult(recordings_dir, state_with_failed_entry, tmp_path, monkeypatch):
    """A failed_permanent entry is salvaged when a matching stub has a CATEGORY: llmResult."""
    # Stub datetime is 2026-07-22T12:24:06 UTC = 14:24:06 CEST — within the audio's
    # [recording_start - 5min, recording_start + duration + 30min] window.
    meta = {
        "datetime": "2026-07-22T12:24:06",
        "duration": 1299000,
        "processingTime": 0,
        "languageModelProcessingTime": 23159,
        "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: Board Sync - Pulse EFI\n\n**Board updates**\n- Item one\n- Item two",
    }
    _write_stub(recordings_dir, "1784723106", meta, mtime_offset=0.0)

    # Point FOLDERS at a tmp MINNESOTERE dir so save_output writes somewhere predictable.
    out_dir = tmp_path / "vault" / "Minnesotere"
    out_dir.mkdir(parents=True)
    monkeypatch.setattr("pipeline.FOLDERS", {"MINNESOTERE": str(out_dir), "DEFAULT": str(out_dir)})

    recovered = recover_failed_permanent(state_with_failed_entry)
    assert recovered == 1

    # State entry flipped to complete
    entry = state_with_failed_entry["processed"]["/recordings/2026-07-22/13-59-43.m4a"]
    assert entry["status"] == "complete"
    assert entry["category"] == "MINNESOTERE"
    assert entry.get("note") == "recovered from late-arriving Superwhisper llmResult"

    # .md file written with the audio's recording timestamp prefix (local time)
    files = list(out_dir.glob("*.md"))
    assert len(files) == 1, f"Expected 1 recovered .md, found: {[f.name for f in files]}"
    assert files[0].name.startswith("26-07-22 13.59 - "), files[0].name
    assert "Board Sync - Pulse EFI" in files[0].name
    body = files[0].read_text()
    assert "**Board updates**" in body


def test_no_recovery_when_no_matching_stub(recordings_dir, state_with_failed_entry, monkeypatch):
    """If no stub's datetime falls in the audio's window, nothing is recovered."""
    # Stub datetime is days outside the audio's window.
    meta = {
        "datetime": "2026-07-01T12:24:06",
        "duration": 1299000,
        "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: unrelated\n\nbody",
    }
    _write_stub(recordings_dir, "999", meta)
    assert recover_failed_permanent(state_with_failed_entry) == 0
    entry = state_with_failed_entry["processed"]["/recordings/2026-07-22/13-59-43.m4a"]
    assert entry["status"] == "failed_permanent"


def test_no_recovery_when_stub_llmresult_missing(recordings_dir, state_with_failed_entry):
    """A stub with no llmResult (genuine empty stub) is not a recovery candidate."""
    meta = {"datetime": "2026-07-22T12:24:06", "duration": 0, "processingTime": 0}
    _write_stub(recordings_dir, "1784722997", meta)
    assert recover_failed_permanent(state_with_failed_entry) == 0


def test_no_recovery_when_no_failed_entries(recordings_dir, tmp_path, monkeypatch):
    """Empty/complete-only state → no scan needed, returns 0."""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"processed": {}}))
    monkeypatch.setattr("pipeline.STATE_FILE", str(state_path))
    assert recover_failed_permanent({"processed": {}}) == 0


def test_picks_latest_stub_when_multiple_match(recordings_dir, state_with_failed_entry, tmp_path, monkeypatch):
    """When several stubs match the audio's window, the most recently-modified one wins.

    Reproduces the July 22 scenario: three stubs were created (one abandoned, two
    completed). The recovery must pick one of the completed stubs (the latest mtime),
    not the abandoned stub.
    """
    # Abandoned stub — oldest
    _write_stub(recordings_dir, "1784722997", {
        "datetime": "2026-07-22T12:23:17",
        "duration": 0,
        "processingTime": 0,
    }, mtime_offset=-100.0)
    # Completed stub 1
    _write_stub(recordings_dir, "1784723046", {
        "datetime": "2026-07-22T12:24:06",
        "duration": 1299000,
        "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: First completion\n\nbody A",
    }, mtime_offset=-50.0)
    # Completed stub 2 — newest, should win
    _write_stub(recordings_dir, "1784723106", {
        "datetime": "2026-07-22T12:25:06",
        "duration": 1299000,
        "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: Second completion\n\nbody B",
    }, mtime_offset=0.0)

    out_dir = tmp_path / "vault"
    out_dir.mkdir()
    monkeypatch.setattr("pipeline.FOLDERS", {"MINNESOTERE": str(out_dir), "DEFAULT": str(out_dir)})

    assert recover_failed_permanent(state_with_failed_entry) == 1
    files = list(out_dir.glob("*.md"))
    assert len(files) == 1
    assert "Second completion" in files[0].name