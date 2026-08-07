"""Unit tests for recover_failed_permanent().

Verifies that a failed_permanent state entry is salvaged when a Superwhisper recording
stub later completes with a valid llmResult, and that the .md is written to the
category folder with the correct timestamp prefix.
"""

import json
import os
import time
from pathlib import Path

import pytest

from pipeline import CATEGORY_HEADER, CONSUMED_SENTINEL, _mark_consumed, recover_failed_permanent


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
    # Stub datetime is 2026-07-22T14:24:06 Europe/Oslo (= 12:24:06 UTC) — ~24min after
    # the audio recording started (13:59:43 Europe/Oslo), within the legacy
    # [recording_start - 5min, recording_start + duration + 30min] window.
    meta = {
        "datetime": "2026-07-22T14:24:06",
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
        "datetime": "2026-07-01T14:24:06",
        "duration": 1299000,
        "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: unrelated\n\nbody",
    }
    _write_stub(recordings_dir, "999", meta)
    assert recover_failed_permanent(state_with_failed_entry) == 0
    entry = state_with_failed_entry["processed"]["/recordings/2026-07-22/13-59-43.m4a"]
    assert entry["status"] == "failed_permanent"


def test_no_recovery_when_stub_llmresult_missing(recordings_dir, state_with_failed_entry):
    """A stub with no llmResult (genuine empty stub) is not a recovery candidate."""
    meta = {"datetime": "2026-07-22T14:24:06", "duration": 0, "processingTime": 0}
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

    Stub datetimes are Europe/Oslo local (matches real Superwhisper meta.json, which
    is naive ISO written in Mac-local time). All three stubs fall in the audio's
    [recording_start - 5min, recording_start + duration + 30min] window; mtime decides.
    """
    # Abandoned stub — oldest mtime, but datetime still in window
    _write_stub(
        recordings_dir,
        "1784722997",
        {
            "datetime": "2026-07-22T14:23:17",
            "duration": 0,
            "processingTime": 0,
        },
        mtime_offset=-100.0,
    )
    # Completed stub 1
    _write_stub(
        recordings_dir,
        "1784723046",
        {
            "datetime": "2026-07-22T14:24:06",
            "duration": 1299000,
            "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: First completion\n\nbody A",
        },
        mtime_offset=-50.0,
    )
    # Completed stub 2 — newest, should win
    _write_stub(
        recordings_dir,
        "1784723106",
        {
            "datetime": "2026-07-22T14:25:06",
            "duration": 1299000,
            "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: Second completion\n\nbody B",
        },
        mtime_offset=0.0,
    )

    out_dir = tmp_path / "vault"
    out_dir.mkdir()
    monkeypatch.setattr("pipeline.FOLDERS", {"MINNESOTERE": str(out_dir), "DEFAULT": str(out_dir)})

    assert recover_failed_permanent(state_with_failed_entry) == 1
    files = list(out_dir.glob("*.md"))
    assert len(files) == 1
    assert "Second completion" in files[0].name


def test_consumed_stub_is_skipped_by_salvage(recordings_dir, state_with_failed_entry, tmp_path, monkeypatch):
    """A recording dir already marked consumed (its llmResult was returned by the main
    path) must not be re-used by salvage, even when its duration matches a later
    failed_permanent entry. Without the skip, the same stub is written again under a
    different audio's timestamp — a salvage-path duplicate/misroute (the same class of
    bug as the 2026-07-24 incident, now possible in the recovery path).
    """
    entry_path = "/recordings/2026-07-22/13-59-43.m4a"
    # Duration matching is the primary correlation; give the failed entry a stored
    # duration that matches the (already-consumed) stub below.
    state_with_failed_entry["processed"][entry_path]["expected_duration_ms"] = 1299000

    rec = _write_stub(
        recordings_dir,
        "1784723106",
        {
            "datetime": "2026-07-22T14:24:06",
            "duration": 1299000,
            "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: already consumed\n\nbody",
        },
    )
    _mark_consumed(rec)  # main path already wrote this stub's output

    out_dir = tmp_path / "vault" / "Minnesotere"
    out_dir.mkdir(parents=True)
    monkeypatch.setattr("pipeline.FOLDERS", {"MINNESOTERE": str(out_dir), "DEFAULT": str(out_dir)})

    assert recover_failed_permanent(state_with_failed_entry) == 0
    assert state_with_failed_entry["processed"][entry_path]["status"] == "failed_permanent"
    assert list(out_dir.glob("*.md")) == [], "consumed stub must not be re-written by salvage"


def test_recovered_stub_is_marked_consumed(recordings_dir, state_with_failed_entry, tmp_path, monkeypatch):
    """After salvage writes output from a stub, that stub is marked consumed so a later
    failed entry with a near-matching duration cannot re-match it on a subsequent
    salvage pass (duplicate write). Closes R4's loop on the recovery path.
    """
    entry_path = "/recordings/2026-07-22/13-59-43.m4a"
    state_with_failed_entry["processed"][entry_path]["expected_duration_ms"] = 1299000

    rec = _write_stub(
        recordings_dir,
        "1784723106",
        {
            "datetime": "2026-07-22T14:24:06",
            "duration": 1299000,
            "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: Salvaged Sync\n\nbody",
        },
    )
    out_dir = tmp_path / "vault" / "Minnesotere"
    out_dir.mkdir(parents=True)
    monkeypatch.setattr("pipeline.FOLDERS", {"MINNESOTERE": str(out_dir), "DEFAULT": str(out_dir)})

    assert recover_failed_permanent(state_with_failed_entry) == 1
    assert (rec / CONSUMED_SENTINEL).exists(), "recovered stub was not marked consumed"
