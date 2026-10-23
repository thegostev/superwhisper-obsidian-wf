"""Attempt accounting at attempt start (LAG-807, spec CB-21, HB-13).

The 26-10-08 post-mortem: attempts were recorded only at *failure* time, so an
in-flight attempt was invisible to state and heartbeat. The Oct 8 kickstart
abandoned an attempt-2 wait that had never been recorded, and the file's
attempt counter under-reported the truth.

These tests pin the fix: the attempt number is persisted before the wait
begins (CB-21), a crash inside the wait leaves a truthful count on disk, the
failure bookkeeping updates that recorded attempt without a second increment,
and the heartbeat names the in-flight file (HB-13).
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import pipeline
from pipeline import (
    CATEGORY_HEADER,
    FatalAPIError,
    PermanentFileError,
    load_state,
    process_audio,
    write_heartbeat,
)

_FAKE_PATH = "/fake/path/2026-10-08/09-58-36.m4a"
_FAKE_TS = datetime(2026, 10, 8, 9, 58, 36)
_GOOD_OUTPUT = f"{CATEGORY_HEADER} DEFAULT\nFILENAME: Sync\n\nBody text."


class _Crash(BaseException):
    """Stands in for SIGKILL / launchctl kickstart -k: nothing in process_audio catches it."""


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    pipeline._heartbeat_last_write = 0.0
    pipeline._heartbeat_in_flight = None
    pipeline._heartbeat_last_outcome = None
    pipeline._heartbeat_dependency_down = False
    yield tmp_path
    pipeline._heartbeat_in_flight = None
    pipeline._heartbeat_last_write = 0.0


def _run(state: dict, wait_side_effect) -> tuple[bool, str | None]:
    """Run process_audio with Superwhisper mocked; ``wait_side_effect`` decides the attempt."""
    with (
        patch("pipeline.get_audio_duration_ms", return_value=None),
        patch("pipeline.switch_superwhisper_mode"),
        patch("pipeline.handoff_to_superwhisper", return_value=0.0),
        patch("pipeline.wait_for_superwhisper_result", side_effect=wait_side_effect),
        patch("pipeline.save_output", return_value="/tmp/out.md"),
        patch("pipeline._notify_dependency_down"),
    ):
        return process_audio(_FAKE_PATH, _FAKE_TS, state)


def _on_disk() -> dict:
    return json.loads(Path(pipeline.STATE_FILE).read_text(encoding="utf-8"))["processed"][_FAKE_PATH]


def _heartbeat() -> dict:
    return json.loads(Path(pipeline.HEARTBEAT_FILE).read_text(encoding="utf-8"))


# --- CB-21: counter persisted before the wait ----------------------------------


class TestAttemptPersistedAtStart:
    def test_first_attempt_is_on_disk_before_the_wait_begins(self):
        seen: dict = {}

        def wait(*_a, **_k):
            seen.update(_on_disk())
            return _GOOD_OUTPUT

        _run({"processed": {}}, wait)
        assert seen["status"] == "in_progress"
        assert seen["attempts"] == 1
        assert "attempt_started_at" in seen

    def test_retry_attempt_increments_from_the_recorded_count(self):
        state = {"processed": {_FAKE_PATH: {"status": "failed_retry", "attempts": 1, "outcome": "timeout"}}}
        seen: dict = {}

        def wait(*_a, **_k):
            seen.update(_on_disk())
            return _GOOD_OUTPUT

        _run(state, wait)
        assert seen["status"] == "in_progress"
        assert seen["attempts"] == 2

    def test_in_progress_record_keeps_prior_fields_for_forensics(self):
        state = {
            "processed": {
                _FAKE_PATH: {"status": "failed_retry", "attempts": 1, "outcome": "timeout", "error": "deadline"}
            }
        }
        seen: dict = {}

        def wait(*_a, **_k):
            seen.update(_on_disk())
            raise TimeoutError("again")

        _run(state, wait)
        assert seen["error"] == "deadline"


# --- CB-21: crash inside the wait leaves a truthful count -----------------------


class TestCrashInsideWait:
    def test_crash_leaves_the_started_attempt_counted(self):
        with pytest.raises(_Crash):
            _run({"processed": {}}, _Crash())
        record = load_state()["processed"][_FAKE_PATH]
        assert record["status"] == "in_progress"
        assert record["attempts"] == 1

    def test_restart_after_crash_counts_the_abandoned_attempt(self):
        """The Oct 8 shape: attempt 1 failed, attempt 2 abandoned by kickstart, attempt 3 runs."""
        _run({"processed": {}}, TimeoutError("attempt 1"))
        with pytest.raises(_Crash):
            _run(load_state(), _Crash())
        assert load_state()["processed"][_FAKE_PATH]["attempts"] == 2

        _run(load_state(), TimeoutError("attempt 3"))
        record = load_state()["processed"][_FAKE_PATH]
        assert record["attempts"] == 3
        assert record["status"] == "failed_permanent"  # MAX_RETRIES=3 reached truthfully

    def test_in_progress_entry_is_retryable_by_discovery(self):
        """An abandoned attempt must not strand the file: only complete/failed_permanent are skipped."""
        with pytest.raises(_Crash):
            _run({"processed": {}}, _Crash())
        assert load_state()["processed"][_FAKE_PATH]["status"] not in ("complete", "failed_permanent")


# --- CB-21: no double increment at outcome time --------------------------------


class TestNoDoubleIncrement:
    def test_success_keeps_the_start_count(self):
        state: dict = {"processed": {}}
        assert _run(state, lambda *a, **k: _GOOD_OUTPUT)[0] is True
        assert state["processed"][_FAKE_PATH]["attempts"] == 1
        assert _on_disk()["status"] == "complete"

    def test_transient_failure_keeps_the_start_count(self):
        state: dict = {"processed": {}}
        _run(state, TimeoutError("deadline"))
        assert state["processed"][_FAKE_PATH]["attempts"] == 1
        assert state["processed"][_FAKE_PATH]["status"] == "failed_retry"

    def test_permanent_failure_keeps_the_start_count(self):
        state = {"processed": {_FAKE_PATH: {"status": "failed_retry", "attempts": 1}}}
        _run(state, PermanentFileError("refused"))
        assert state["processed"][_FAKE_PATH]["attempts"] == 2
        assert state["processed"][_FAKE_PATH]["status"] == "failed_permanent"

    def test_outcome_record_drops_the_in_progress_marker(self):
        state: dict = {"processed": {}}
        _run(state, TimeoutError("deadline"))
        assert "attempt_started_at" not in state["processed"][_FAKE_PATH]


# --- CB-21: a fatal error is not an attempt on this file -----------------------


class TestFatalRollsBack:
    def test_fatal_restores_the_previous_record(self):
        prior = {"status": "failed_retry", "attempts": 1, "outcome": "timeout"}
        state = {"processed": {_FAKE_PATH: dict(prior)}}
        with pytest.raises(FatalAPIError):
            _run(state, FatalAPIError("mode key empty"))
        assert state["processed"][_FAKE_PATH] == prior
        assert _on_disk() == prior

    def test_fatal_on_a_new_file_leaves_no_record(self):
        state: dict = {"processed": {}}
        with pytest.raises(FatalAPIError):
            _run(state, FatalAPIError("mode key empty"))
        assert _FAKE_PATH not in state["processed"]
        assert _FAKE_PATH not in load_state()["processed"]


# --- HB-13: in-flight file visible in the heartbeat ----------------------------


class TestHeartbeatInFlight:
    def test_null_when_nothing_is_in_flight(self):
        write_heartbeat("scanning", force=True)
        assert _heartbeat()["in_flight"] is None

    def test_names_path_attempt_and_elapsed_during_the_wait(self):
        seen: dict = {}

        def wait(*_a, **_k):
            write_heartbeat("processing", force=True)
            seen.update(_heartbeat()["in_flight"])
            return _GOOD_OUTPUT

        _run({"processed": {_FAKE_PATH: {"status": "failed_retry", "attempts": 1}}}, wait)
        assert seen["path"] == _FAKE_PATH
        assert seen["attempt"] == 2
        assert isinstance(seen["elapsed_s"], int)
        assert seen["elapsed_s"] >= 0
        assert seen["started_at"].endswith("Z")

    def test_elapsed_grows_with_wall_clock(self):
        pipeline._heartbeat_in_flight = {"path": _FAKE_PATH, "attempt": 1, "started": 1_000.0}
        with patch("pipeline.time.time", return_value=1_090.4):
            write_heartbeat("processing", force=True)
        assert _heartbeat()["in_flight"]["elapsed_s"] == 90

    @pytest.mark.parametrize(
        ("side_effect", "propagates"),
        [
            (lambda *a, **k: _GOOD_OUTPUT, None),
            (TimeoutError("x"), None),
            (PermanentFileError("x"), None),
            (FatalAPIError("x"), FatalAPIError),
            (_Crash(), _Crash),
        ],
        ids=["success", "transient", "permanent", "fatal", "crash"],
    )
    def test_cleared_when_the_attempt_ends(self, side_effect, propagates):
        if propagates is None:
            _run({"processed": {}}, side_effect)
        else:
            with pytest.raises(propagates):
                _run({"processed": {}}, side_effect)
        assert pipeline._heartbeat_in_flight is None
        write_heartbeat("scanning", force=True)
        assert _heartbeat()["in_flight"] is None
