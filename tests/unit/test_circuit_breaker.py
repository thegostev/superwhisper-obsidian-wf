"""Attempt-outcome circuit breaker tests (LAG-801, spec CB-*).

The 26-10-08 post-mortem (49 h zombie Superwhisper instance) showed nine
sequential attempts all ending in the identical
``TimeoutError("Superwhisper did not return a result within 3600s")`` — and
nothing escalated anywhere. MAX_RETRIES conflates "bad audio" with
"dependency down": on a dead Superwhisper, three 1 h burns per file flip
otherwise-good files to failed_permanent.

These tests pin the CB contract: attempt outcomes are classified and recorded
(CB-1), identical consecutive timeout outcomes drive a streak counter (CB-2)
that trips a ``dependency_down`` flag at K (CB-3, config default 3), the flag
is state-persisted (CB-9) and heartbeat-visible (CB-6), one notification fires
per trip with a cooldown (CB-4/CB-8), any other outcome resets the streak
(CB-5), and the daemon's heartbeat call sites carry the flag (CB-7).
"""

import copy
import inspect
import json
from datetime import datetime
from unittest.mock import patch

import pytest

import auto_transcribe
import pipeline
from pipeline import (
    PermanentFileError,
    classify_outcome,
    process_audio,
    record_attempt_result,
)

_FAKE_PATH = "/fake/path/2026-05-01/12-00-00.m4a"
_FAKE_TS = datetime(2026, 5, 1, 12, 0, 0)
_NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def fresh_circuit_state():
    """Reset the circuit breaker's cached heartbeat flag between tests."""
    pipeline._heartbeat_dependency_down = False
    yield
    pipeline._heartbeat_dependency_down = False


@pytest.fixture
def breaker_path(tmp_path, monkeypatch):
    """Point pipeline.STATE_FILE at a tmp file so save/load round-trips stay local."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(pipeline, "STATE_FILE", str(state_file))
    return state_file


def _timeout_streak(state, times: int, start: float = _NOW) -> bool:
    """Record `times` identical timeout outcomes; return the last trip result."""
    fired = False
    for i in range(times):
        fired = record_attempt_result(state, "timeout", now=start + i)
    return fired


def _successful_process_audio_run(state, file_path: str = _FAKE_PATH):
    """Drive process_audio through its success path without touching Superwhisper."""
    with (
        patch("pipeline.get_audio_duration_ms", return_value=None),
        patch("pipeline.switch_superwhisper_mode"),
        patch("pipeline.handoff_to_superwhisper"),
        patch(
            "pipeline.wait_for_superwhisper_result",
            return_value="CATEGORY: WORK\nFILENAME: Test Meeting\n\nBody",
        ),
        patch("pipeline.save_output", return_value="/tmp/test-work/x.md"),
        patch("pipeline.save_state"),
    ):
        return process_audio(file_path, _FAKE_TS, state)


def _failing_process_audio_run(
    state,
    error: Exception,
    file_path: str = _FAKE_PATH,
    error_type: str = "transient",
):
    """Drive process_audio into the given exception branch, save_state patched out."""
    if error_type == "permanent":
        exc_patch = patch("pipeline.switch_superwhisper_mode", side_effect=error)
    else:
        exc_patch = patch("pipeline.wait_for_superwhisper_result", side_effect=error)
    with (
        patch("pipeline.get_audio_duration_ms", return_value=None),
        patch("pipeline.handoff_to_superwhisper"),
        exc_patch,
        patch("pipeline.save_state"),
    ):
        return process_audio(file_path, _FAKE_TS, state)


# --- CB-1: outcome classification ----------------------------------------------


class TestClassifyOutcome:
    def test_timeout_error_maps_to_timeout(self):
        """CB-1: the 3600 s poll deadline and the abandoned-stub fast-fail both
        raise TimeoutError — the class LAG-801 keys on."""
        assert classify_outcome(TimeoutError("Superwhisper did not return a result within 3600s")) == "timeout"

    def test_permanent_file_error_maps_to_permanent(self):
        assert classify_outcome(PermanentFileError("no CATEGORY: header")) == "permanent"

    def test_generic_exception_maps_to_transient(self):
        assert classify_outcome(RuntimeError("boom")) == "transient"
        assert classify_outcome(OSError("disk?")) == "transient"


# --- CB-2/CB-3/CB-5: streak bookkeeping ----------------------------------------


class TestStreakAndFlag:
    def test_streak_counts_identical_timeouts(self):
        state: dict = {"processed": {}}
        _timeout_streak(state, times=3)
        breaker = state["circuit_breaker"]
        assert breaker["streak"] == 3
        assert breaker["last_outcome"] == "timeout"
        assert breaker["dependency_down"] is True

    def test_flag_trip_returns_true_exactly_on_kth_timeout(self):
        state: dict = {"processed": {}}
        results = [record_attempt_result(state, "timeout", now=_NOW + i) for i in range(4)]
        assert results == [False, False, True, False], "CB-3: trip exactly once, at the Kth timeout"

    def test_threshold_defaults_to_three(self):
        assert pipeline.CIRCUIT_BREAKER_THRESHOLD == 3

    def test_success_resets_streak(self):
        state: dict = {"processed": {}}
        _timeout_streak(state, times=2)
        assert record_attempt_result(state, "success", now=_NOW) is False
        assert record_attempt_result(state, "timeout", now=_NOW + 1) is False
        assert state["circuit_breaker"]["streak"] == 1

    def test_permanent_outcome_resets_streak(self):
        state: dict = {"processed": {}}
        _timeout_streak(state, times=2)
        assert record_attempt_result(state, "permanent", now=_NOW) is False
        assert record_attempt_result(state, "timeout", now=_NOW + 1) is False
        assert state["circuit_breaker"]["streak"] == 1

    def test_transient_outcome_resets_streak(self):
        state: dict = {"processed": {}}
        _timeout_streak(state, times=2)
        assert record_attempt_result(state, "transient", now=_NOW) is False
        assert record_attempt_result(state, "timeout", now=_NOW + 1) is False
        assert state["circuit_breaker"]["streak"] == 1

    def test_flag_persists_until_success_clears_it(self):
        state: dict = {"processed": {}}
        _timeout_streak(state, times=3)
        assert state["circuit_breaker"]["dependency_down"] is True
        # A non-timeout outcome resets the streak but the flag holds (CB-5):
        record_attempt_result(state, "transient", now=_NOW + 10)
        assert state["circuit_breaker"]["streak"] == 0
        assert state["circuit_breaker"]["dependency_down"] is True
        # Only an actual completion clears the flag.
        record_attempt_result(state, "success", now=_NOW + 11)
        assert state["circuit_breaker"]["dependency_down"] is False


# --- CB-4/CB-8: notification fire-once + cooldown -------------------------------


class TestNotification:
    def test_notification_fires_once_per_streak_not_per_attempt(self):
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down") as notify_mock:
            _timeout_streak(state, times=4)
        assert notify_mock.call_count == 1, "CB-4: one page per trip, not one per attempt"

    def test_message_names_retry_queue_size(self, monkeypatch):
        state: dict = {
            "processed": {
                "/a.m4a": {"status": "failed_retry"},
                "/b.m4a": {"status": "failed_retry"},
                "/c.m4a": {"status": "failed_retry"},
            }
        }
        recorded: list[str] = []
        real_run = pipeline.subprocess.run

        def _capture_run(argv, **kwargs):
            if "osascript" in argv[0]:
                recorded.append(argv[-2])  # message is the second-to-last argv item
                return real_run(["/usr/bin/true"], capture_output=True)
            return real_run(argv, **kwargs)

        monkeypatch.setattr(pipeline.subprocess, "run", _capture_run)
        _timeout_streak(state, times=3)
        assert "3 files in retry queue" in recorded[0]

    def test_cooldown_suppresses_second_streak_within_window(self):
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down") as notify_mock:
            _timeout_streak(state, times=3)  # trip 1 → page
            # A different outcome resets the streak; a fresh trip inside the
            # cooldown window must stay silent.
            record_attempt_result(state, "transient", now=_NOW + 60)
            _timeout_streak(state, times=3, start=_NOW + 120)
        assert notify_mock.call_count == 1, "CB-8: the cooldown is shared across trips"

    def test_cooldown_expiry_allows_a_new_page(self):
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down") as notify_mock:
            _timeout_streak(state, times=3, start=_NOW)
            record_attempt_result(state, "transient", now=_NOW + 60)
            _timeout_streak(state, times=3, start=_NOW + pipeline.CIRCUIT_BREAKER_NOTIFY_COOLDOWN + 1)
        assert notify_mock.call_count == 2

    def test_never_fires_below_threshold(self):
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down") as notify_mock:
            _timeout_streak(state, times=2)
        notify_mock.assert_not_called()

    def test_cooldown_defaults_to_one_hour(self):
        assert pipeline.CIRCUIT_BREAKER_NOTIFY_COOLDOWN == 3600.0

    def test_notify_failure_does_not_break_the_attempt_bookkeeping(self):
        """CB-4: a failed notification (no GUI session, osascript error) must not
        raise out of record_attempt_result — the outcome is already recorded."""
        state: dict = {"processed": {}}

        def _explode(title: str, message: str) -> bool:
            raise OSError("no notification centre")

        with patch("pipeline._notify_dependency_down", side_effect=_explode):
            fired = _timeout_streak(state, times=3)
        assert fired is True
        assert state["circuit_breaker"]["dependency_down"] is True


# --- CB-9: state + cooldown persistence across restart --------------------------


class TestStatePersistence:
    def test_breaker_state_round_trips_through_state_file(self, breaker_path):
        state: dict = {"processed": {}}
        _timeout_streak(state, times=3)
        pipeline.save_state(state)
        reloaded = pipeline.load_state()
        assert reloaded["circuit_breaker"]["dependency_down"] is True
        assert reloaded["circuit_breaker"]["streak"] == 3

    def test_cooldown_survives_daemon_restart(self, breaker_path):
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down") as notify_mock:
            _timeout_streak(state, times=3, start=_NOW)
            pipeline.save_state(state)
            # Simulate the daemon restarting: state comes back from disk…
            restarted = copy.deepcopy(pipeline.load_state())
            # …and a fresh trip lands inside the cooldown window.
            _timeout_streak(restarted, times=3, start=_NOW + 30)
        assert notify_mock.call_count == 1, "CB-8: the cooldown epoch is persisted"

    def test_flag_survives_daemon_restart(self, breaker_path):
        state: dict = {"processed": {}}
        _timeout_streak(state, times=3, start=_NOW)
        pipeline.save_state(state)
        restarted = copy.deepcopy(pipeline.load_state())
        assert restarted["circuit_breaker"]["dependency_down"] is True


# --- CB wiring in process_audio (per-file record carries the outcome) -----------


class TestProcessAudioOutcomeRecording:
    def test_timeout_attempt_records_timeout_outcome(self):
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down"):
            result = _failing_process_audio_run(
                state,
                TimeoutError("Superwhisper did not return a result within 3600s for: 12-00-00.m4a"),
            )
        assert result == (False, None)
        record = state["processed"][_FAKE_PATH]
        assert record["status"] == "failed_retry"
        assert record["outcome"] == "timeout"

    def test_perm_error_attempt_records_permanent_outcome(self):
        state: dict = {"processed": {}}
        _failing_process_audio_run(state, PermanentFileError("no CATEGORY header"), error_type="permanent")
        record = state["processed"][_FAKE_PATH]
        assert record["status"] == "failed_permanent"
        assert record["outcome"] == "permanent"

    def test_success_attempt_records_success_outcome(self):
        state: dict = {"processed": {}}
        result = _successful_process_audio_run(state)
        assert result == (True, "WORK")
        record = state["processed"][_FAKE_PATH]
        assert record["status"] == "complete"
        assert record["outcome"] == "success"

    def test_generic_error_records_transient_outcome(self):
        state: dict = {"processed": {}}
        _failing_process_audio_run(state, RuntimeError("handoff exploded"))
        record = state["processed"][_FAKE_PATH]
        assert record["status"] == "failed_retry"
        assert record["outcome"] == "transient"


# --- CB-6: heartbeat visibility --------------------------------------------------


class TestHeartbeatFlag:
    def test_trip_writes_dependency_down_true_to_heartbeat(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(hb))
        state: dict = {"processed": {}}
        record_attempt_result(state, "timeout", now=_NOW)
        record_attempt_result(state, "timeout", now=_NOW + 1)
        record_attempt_result(state, "timeout", now=_NOW + 2)
        payload = json.loads(hb.read_text(encoding="utf-8"))
        assert payload["dependency_down"] is True

    def test_heartbeat_defaults_to_false_when_breaker_clear(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(hb))
        pipeline.write_heartbeat("scanning", force=True)
        payload = json.loads(hb.read_text(encoding="utf-8"))
        assert payload["dependency_down"] is False

    def test_write_heartbeat_accepts_dependency_down_override(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(hb))
        pipeline.write_heartbeat("processing", dependency_down=True, force=True)
        payload = json.loads(hb.read_text(encoding="utf-8"))
        assert payload["dependency_down"] is True

    def test_success_clears_heartbeat_flag(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(hb))
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down"):
            _failing_process_audio_run(state, TimeoutError("t1"))
            _failing_process_audio_run(state, TimeoutError("t2"))
            _failing_process_audio_run(state, TimeoutError("t3"))
        assert json.loads(hb.read_text(encoding="utf-8"))["dependency_down"] is True
        _successful_process_audio_run(state)
        assert json.loads(hb.read_text(encoding="utf-8"))["dependency_down"] is False


# --- CB-7: daemon wiring ----------------------------------------------------------


class TestDaemonWiring:
    def test_scan_cycle_heartbeat_carries_dependency_down(self, tmp_path, monkeypatch):
        watch = tmp_path / "watch"
        watch.mkdir()
        monkeypatch.setattr(auto_transcribe, "WATCH_FOLDER", str(watch))
        captured: dict = {}

        def _capture(phase: str, **kwargs):
            captured.update({"phase": phase, **kwargs})

        monkeypatch.setattr(auto_transcribe, "write_heartbeat", _capture)
        auto_transcribe.run_scan_cycle({"processed": {}}, {}, 7)
        assert "dependency_down" in captured

    def test_counts_helper_exposes_the_flag(self):
        state = {"processed": {}, "circuit_breaker": {"dependency_down": True}}
        assert auto_transcribe._heartbeat_counts(state)["dependency_down"] is True
        assert auto_transcribe._heartbeat_counts({"processed": {}})["dependency_down"] is False

    def test_startup_banner_names_the_circuit_breaker_when_flagged(self):
        source = inspect.getsource(auto_transcribe.main)
        assert "Circuit breaker" in source or "circuit_breaker" in source
