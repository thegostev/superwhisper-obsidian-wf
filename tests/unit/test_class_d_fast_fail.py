"""Class-D fast-fail tests (LAG-913, spec CB-25).

The 2026-10-30 disk-full handoff left a session dir holding a truncated
``output.wav`` and no ``meta.json``. The dir satisfied the CB-10 accept window
while no ``meta.json`` existed, and the empty-stub tracker had nothing to watch,
so the attempt burned its full 3600 s budget — the 1.02 s ``18-44-58.m4a`` stub
was still ``in_flight`` at 635 s with three real meetings queued behind it.

These tests pin the class-D contract: a meta-less dir counts as this attempt's
session only if it was created after handoff delivery; if ``meta.json`` never
arrives, the attempt fails inside the scaled 10..120 poll budget (≤120 polls,
under 6 min); a readable ``meta.json`` — including one whose only field is
``languageModelProcessingTime`` — is never class D.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import pipeline
from pipeline import CATEGORY_HEADER, PermanentFileError, wait_for_superwhisper_result

_POLL = 3.0


class FakeClock:
    """Deterministic clock: ``sleep`` advances ``time`` and fires tick callbacks."""

    def __init__(self) -> None:
        self.start = time.time()
        self.now = self.start
        self.on_tick: list = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for callback in list(self.on_tick):
            callback(self)

    @property
    def elapsed(self) -> float:
        return self.now - self.start


@pytest.fixture
def clock():
    fake = FakeClock()
    with patch("time.time", fake.time), patch("time.sleep", fake.sleep):
        yield fake


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    rd = tmp_path / "recordings"
    rd.mkdir()
    monkeypatch.setattr(pipeline, "SUPERWHISPER_RECORDINGS_DIR", str(rd))
    monkeypatch.setattr(pipeline, "SUPERWHISPER_POLL_INTERVAL", _POLL)
    monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 3600)
    monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", 600)
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    return rd


def _session_dir(recordings_dir: Path, name: str, mtime: float, meta: dict | None) -> Path:
    """Create a Superwhisper session dir with an explicit mtime; ``meta=None`` omits meta.json."""
    rec = recordings_dir / name
    rec.mkdir()
    if meta is not None:
        (rec / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        os.utime(rec / "meta.json", (mtime, mtime))
    os.utime(rec, (mtime, mtime))
    return rec


# --- CB-25: persistent meta-less dir fails inside its budget ------------------


def test_class_d_dir_fails_within_its_poll_budget(recordings_dir, clock):
    """A dir created after handoff that never gets meta.json fails as class D."""
    start = clock.now
    _session_dir(recordings_dir, "1793376298", start + 1, meta=None)

    with pytest.raises(PermanentFileError, match="meta.json"):
        wait_for_superwhisper_result("meeting.m4a", since=start - 1, handed_off_at=start)

    assert clock.elapsed <= 120 * _POLL  # acceptance: ≤120 polls
    assert clock.elapsed < 6 * 60  # acceptance: under 6 minutes
    assert clock.elapsed < pipeline.SUPERWHISPER_TIMEOUT


# --- CB-25: stale dirs are not this attempt's session -------------------------


def test_dir_created_before_handoff_is_not_this_session(recordings_dir, clock, monkeypatch):
    """A stale meta-less stub from before the handoff must not be counted."""
    monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", 0)  # isolate from CB-10
    monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 120)
    start = clock.now
    _session_dir(recordings_dir, "stale", start - 30, meta=None)  # before handoff, after `since`

    with pytest.raises(TimeoutError, match="did not return") as excinfo:
        wait_for_superwhisper_result("meeting.m4a", since=start - 100, handed_off_at=start)

    assert not isinstance(excinfo.value, PermanentFileError)
    assert clock.elapsed >= 120


# --- CB-25: a real result still wins -----------------------------------------


def test_valid_category_result_still_returns(recordings_dir, clock):
    """The class-D tracker must not disturb the normal success path (R5)."""
    start = clock.now
    text = f"{CATEGORY_HEADER} WORK\nFILENAME: Standup\n\nbody"
    _session_dir(recordings_dir, "good", start + 1, meta={"llmResult": text})

    got = wait_for_superwhisper_result("meeting.m4a", since=start - 1, handed_off_at=start)

    assert got == text
    assert got.recording_dir == "good"


# --- CB-25: an in-flight LLM pass is proof of life ----------------------------


def test_llm_pass_in_flight_is_proof_of_life_not_class_d(recordings_dir, clock, monkeypatch):
    """A readable meta.json with languageModelProcessingTime is never class D."""
    monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 120)
    start = clock.now
    _session_dir(recordings_dir, "inflight", start + 1, meta={"languageModelProcessingTime": 1})

    with pytest.raises(TimeoutError, match="did not return") as excinfo:
        wait_for_superwhisper_result("meeting.m4a", since=start - 1, handed_off_at=start)

    assert not isinstance(excinfo.value, PermanentFileError)
    assert clock.elapsed >= 120


def test_unreadable_meta_json_is_not_class_d(recordings_dir, clock, monkeypatch):
    """A partial/corrupt meta.json is a write in progress, not a class-D dir."""
    monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 60)
    monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", 0)  # isolate from CB-10
    start = clock.now
    rec = _session_dir(recordings_dir, "partial", start + 1, meta=None)
    (rec / "meta.json").write_text('{"llmResult": "CATEG', encoding="utf-8")
    os.utime(rec / "meta.json", (start + 1, start + 1))
    os.utime(rec, (start + 1, start + 1))

    with pytest.raises(TimeoutError, match="did not return") as excinfo:
        wait_for_superwhisper_result("meeting.m4a", since=start - 1, handed_off_at=start)

    assert not isinstance(excinfo.value, PermanentFileError)


# --- CB-25: budget scaling ----------------------------------------------------


def test_class_d_budget_scales_between_10_and_120_polls():
    """R6's one-poll-per-30s rule, bounded to 10..120 polls."""
    assert pipeline._scaled_poll_budget(None, 10, 120) == 10
    assert pipeline._scaled_poll_budget(1_020, 10, 120) == 10
    assert pipeline._scaled_poll_budget(30_000, 10, 120) == 11
    assert pipeline._scaled_poll_budget(3_600_000, 10, 120) == 120
    assert pipeline._scaled_poll_budget(100_000_000, 10, 120) == 120


# --- CB-25: post-mortem regression --------------------------------------------


def test_class_d_regression_2026_10_30_18_44_58(recordings_dir, clock):
    """The 1.02 s ``18-44-58.m4a`` stub was still in_flight at 635 s.

    Regression for the 2026-11-03 post-mortem (P1-4): Superwhisper wrote a
    truncated output.wav with no meta.json; the attempt must fail inside the
    scaled budget instead of waiting out the 3600 s deadline.
    """
    start = clock.now
    rec = _session_dir(recordings_dir, "1793376298", start + 1, meta=None)
    (rec / "output.wav").write_bytes(b"RIFF" + b"\0" * 60)  # truncated, no meta.json
    os.utime(rec, (start + 1, start + 1))

    with pytest.raises(PermanentFileError, match="meta.json"):
        wait_for_superwhisper_result(
            "/recordings/2026-10-30/18-44-58.m4a",
            since=start - 1,
            handed_off_at=start,
            expected_duration_ms=1020,
        )

    assert clock.elapsed <= 120 * _POLL
    assert clock.elapsed < 6 * 60
    assert clock.elapsed < 635  # the post-mortem's in_flight observation
