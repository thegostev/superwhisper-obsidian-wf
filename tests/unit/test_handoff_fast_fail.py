"""Handoff fast-fail tests (LAG-802, spec CB-10..CB-13).

The 26-10-08 post-mortem: ``open -a Superwhisper`` exiting 0 only means
LaunchServices queued the open event. A zombie Superwhisper never drained
those events, so no ``recordings/<epoch>`` session dir was ever created and
each attempt (55-100 s when healthy) was priced at the full 3600 s deadline,
under a generic timeout that could not name the failure.

These tests pin the fast-fail contract: the accept window opens at handoff
delivery, not at ``since`` (CB-10); a session dir appearing inside the window
lets the normal wait proceed; no session dir inside the window raises the
distinct ``superwhisper-not-accepting`` outcome (CB-11), which feeds the
circuit breaker as its own identical-outcome streak (CB-12) and is visible in
state, log and heartbeat (CB-13); the window is config-driven.
"""

import importlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import config
import pipeline
from pipeline import (
    CATEGORY_HEADER,
    OUTCOME_NOT_ACCEPTING,
    SuperwhisperNotAcceptingError,
    classify_outcome,
    process_audio,
    record_attempt_result,
    wait_for_superwhisper_result,
)

_FAKE_PATH = "/fake/path/2026-10-08/09-58-36.m4a"
_FAKE_TS = datetime(2026, 10, 8, 9, 58, 36)
_NOW = 1_000_000.0
_ACCEPT_WINDOW = 600
_POLL = 3.0


class FakeClock:
    """Deterministic clock for the poll loop: ``sleep`` advances ``time``.

    Starts at the real wall clock so recording-dir mtimes (set with os.utime)
    compare against the same timeline the loop reads. ``on_tick`` callbacks
    fire after each sleep, so a test can make a session dir appear at a
    chosen point inside (or after) the accept window.
    """

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
    monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", _ACCEPT_WINDOW)
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    return rd


@pytest.fixture(autouse=True)
def fresh_heartbeat_cache():
    pipeline._heartbeat_dependency_down = False
    pipeline._heartbeat_last_outcome = None
    yield
    pipeline._heartbeat_dependency_down = False
    pipeline._heartbeat_last_outcome = None


def _session_dir(recordings_dir: Path, name: str, mtime: float, meta: dict | None) -> Path:
    """Create a Superwhisper session dir with an explicit mtime."""
    rec = recordings_dir / name
    rec.mkdir()
    if meta is not None:
        (rec / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        os.utime(rec / "meta.json", (mtime, mtime))
    os.utime(rec, (mtime, mtime))
    return rec


# --- CB-10: the accept window --------------------------------------------------


class TestAcceptWindow:
    def test_no_session_dir_fails_fast_at_the_window_not_the_deadline(self, recordings_dir, clock):
        """CB-10: zombie Superwhisper → fail at ~600 s, not 3600 s."""
        handed_off_at = clock.now
        with pytest.raises(SuperwhisperNotAcceptingError):
            wait_for_superwhisper_result("x.m4a", since=clock.now - 1, handed_off_at=handed_off_at)
        assert _ACCEPT_WINDOW <= clock.elapsed < _ACCEPT_WINDOW + 2 * _POLL
        assert clock.elapsed < pipeline.SUPERWHISPER_TIMEOUT

    def test_fast_fail_is_a_timeout_error_so_retry_semantics_hold(self, recordings_dir, clock):
        """CB-11: failed_retry on the next cycle, exactly like any TimeoutError."""
        with pytest.raises(TimeoutError, match="superwhisper-not-accepting"):
            wait_for_superwhisper_result("x.m4a", since=clock.now - 1, handed_off_at=clock.now)

    def test_timer_starts_at_handoff_delivery_not_at_since(self, recordings_dir, clock, monkeypatch):
        """CB-10: mode switch + idle wait happen between `since` and delivery and
        must not eat into the window. `since` is far in the past here; measured
        from it, the window would already be spent on the first poll."""
        monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 300)
        since = clock.now - 10 * _ACCEPT_WINDOW
        with pytest.raises(TimeoutError) as excinfo:
            wait_for_superwhisper_result("x.m4a", since=since, handed_off_at=clock.now)
        assert not isinstance(excinfo.value, SuperwhisperNotAcceptingError)
        assert "did not return a result" in str(excinfo.value)

    def test_without_handed_off_at_the_window_opens_when_waiting_starts(self, recordings_dir, clock):
        """Callers that do not pass the delivery time get the closest proxy:
        the moment the wait begins (right after the handoff)."""
        since = clock.now - 10 * _ACCEPT_WINDOW
        with pytest.raises(SuperwhisperNotAcceptingError):
            wait_for_superwhisper_result("x.m4a", since=since)
        assert _ACCEPT_WINDOW <= clock.elapsed < _ACCEPT_WINDOW + 2 * _POLL

    def test_session_dir_appearing_in_time_lets_the_normal_wait_proceed(self, recordings_dir, clock):
        """A session dir at t+300 s (inside the window) → no fast-fail; the
        result written at t+900 s (after the window) is still returned."""
        start = clock.now
        result_text = f"{CATEGORY_HEADER} WORK\nFILENAME: Standup\n\nbody"

        def appear(c: FakeClock) -> None:
            elapsed = c.now - start
            rec = recordings_dir / "1788857138"
            if elapsed >= 300 and not rec.exists():
                _session_dir(recordings_dir, "1788857138", c.now, {"languageModelProcessingTime": 1})
            if elapsed >= 900 and rec.exists():
                meta = {"languageModelProcessingTime": 1, "llmResult": result_text}
                if json.loads((rec / "meta.json").read_text(encoding="utf-8")) != meta:
                    (rec / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
                    os.utime(rec / "meta.json", (c.now, c.now))
                    os.utime(rec, (c.now, c.now))

        clock.on_tick.append(appear)
        got = wait_for_superwhisper_result("x.m4a", since=start - 1, handed_off_at=start)
        assert got == result_text
        assert clock.elapsed > _ACCEPT_WINDOW

    def test_session_dir_appearing_after_the_window_is_too_late(self, recordings_dir, clock):
        start = clock.now

        def appear_late(c: FakeClock) -> None:
            if c.now - start >= _ACCEPT_WINDOW + 60 and not (recordings_dir / "late").exists():
                _session_dir(recordings_dir, "late", c.now, {"languageModelProcessingTime": 1})

        clock.on_tick.append(appear_late)
        with pytest.raises(SuperwhisperNotAcceptingError):
            wait_for_superwhisper_result("x.m4a", since=start - 1, handed_off_at=start)

    def test_session_dir_with_no_meta_yet_counts_as_accepted(self, recordings_dir, clock, monkeypatch):
        """The dir itself is the acceptance signal; meta.json may land later."""
        monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 900)
        _session_dir(recordings_dir, "fresh", clock.now + 1, meta=None)
        with pytest.raises(TimeoutError) as excinfo:
            wait_for_superwhisper_result("x.m4a", since=clock.now - 1, handed_off_at=clock.now)
        assert not isinstance(excinfo.value, SuperwhisperNotAcceptingError)

    def test_old_consumed_and_non_dir_entries_do_not_count_as_acceptance(self, recordings_dir, clock):
        """Only a new, unconsumed session dir proves Superwhisper took the handoff."""
        since = clock.now - 1
        _session_dir(recordings_dir, "old", since - 100, {"llmResult": "CATEGORY: WORK\nold"})
        consumed = _session_dir(recordings_dir, "consumed", clock.now, {"languageModelProcessingTime": 1})
        (consumed / pipeline.CONSUMED_SENTINEL).write_text("x", encoding="utf-8")
        os.utime(consumed, (clock.now, clock.now))
        stray = recordings_dir / ".DS_Store"
        stray.write_text("x", encoding="utf-8")
        os.utime(stray, (clock.now + 1, clock.now + 1))
        with pytest.raises(SuperwhisperNotAcceptingError):
            wait_for_superwhisper_result("x.m4a", since=since, handed_off_at=clock.now)


# --- CB-10: config override ----------------------------------------------------


class TestAcceptWindowConfig:
    def test_default_window_is_ten_minutes(self):
        assert config.SUPERWHISPER_ACCEPT_TIMEOUT == 600
        assert pipeline.SUPERWHISPER_ACCEPT_TIMEOUT == 600

    def test_config_key_overrides_the_default(self, tmp_path, monkeypatch):
        cfg = yaml.safe_load(Path(os.environ["SWOWF_CONFIG_PATH"]).read_text(encoding="utf-8"))
        cfg["superwhisper_accept_timeout"] = 120
        override = tmp_path / "config.yaml"
        override.write_text(yaml.dump(cfg), encoding="utf-8")
        original = os.environ["SWOWF_CONFIG_PATH"]
        monkeypatch.setenv("SWOWF_CONFIG_PATH", str(override))
        try:
            assert importlib.reload(config).SUPERWHISPER_ACCEPT_TIMEOUT == 120
        finally:
            monkeypatch.setenv("SWOWF_CONFIG_PATH", original)
            importlib.reload(config)

    def test_overridden_window_is_honoured_by_the_wait(self, recordings_dir, clock, monkeypatch):
        monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", 60)
        with pytest.raises(SuperwhisperNotAcceptingError):
            wait_for_superwhisper_result("x.m4a", since=clock.now - 1, handed_off_at=clock.now)
        assert 60 <= clock.elapsed < 60 + 2 * _POLL

    def test_zero_disables_the_fast_fail(self, recordings_dir, clock, monkeypatch):
        monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", 0)
        monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 900)
        with pytest.raises(TimeoutError) as excinfo:
            wait_for_superwhisper_result("x.m4a", since=clock.now - 1, handed_off_at=clock.now)
        assert not isinstance(excinfo.value, SuperwhisperNotAcceptingError)
        assert clock.elapsed >= 900


# --- CB-10: delivery time comes from the handoff ---------------------------------


class TestHandoffDeliveryTime:
    def test_handoff_returns_the_time_after_open_returns(self, tmp_path):
        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"x")
        clock = {"now": 100.0}
        order: list[str] = []

        def idle_wait() -> None:
            order.append("idle")
            clock["now"] = 250.0  # the idle wait itself takes time

        def fake_open(argv, **_kwargs) -> None:
            order.append(argv[0])
            clock["now"] = 260.0  # `open` returns: the event is delivered

        with (
            patch("pipeline.write_heartbeat"),
            patch("pipeline._wait_for_superwhisper_idle", side_effect=idle_wait),
            patch("pipeline.subprocess.run", side_effect=fake_open),
            patch("time.time", side_effect=lambda: clock["now"]),
        ):
            delivered_at = pipeline.handoff_to_superwhisper(str(audio))
        assert order == ["idle", "open"]
        assert delivered_at == 260.0

    def test_process_audio_passes_delivery_time_to_the_wait(self):
        state: dict = {"processed": {}}
        with (
            patch("pipeline.get_audio_duration_ms", return_value=None),
            patch("pipeline.switch_superwhisper_mode"),
            patch("pipeline.handoff_to_superwhisper", return_value=4242.0),
            patch(
                "pipeline.wait_for_superwhisper_result",
                return_value="CATEGORY: WORK\nFILENAME: T\n\nBody",
            ) as wait,
            patch("pipeline.save_output", return_value="/tmp/test-work/x.md"),
            patch("pipeline.save_state"),
        ):
            process_audio(_FAKE_PATH, _FAKE_TS, state)
        assert wait.call_args.kwargs["handed_off_at"] == 4242.0


# --- CB-11/CB-13: distinct outcome in state, log, heartbeat ------------------------


def _not_accepting_run(state: dict) -> tuple[bool, str | None]:
    with (
        patch("pipeline.get_audio_duration_ms", return_value=None),
        patch("pipeline.switch_superwhisper_mode"),
        patch("pipeline.handoff_to_superwhisper", return_value=_NOW),
        patch(
            "pipeline.wait_for_superwhisper_result",
            side_effect=SuperwhisperNotAcceptingError("no session dir within 600s"),
        ),
        patch("pipeline.save_state"),
        patch("pipeline._notify_dependency_down"),
    ):
        return process_audio(_FAKE_PATH, _FAKE_TS, state)


class TestDistinctOutcome:
    def test_outcome_class_name(self):
        assert OUTCOME_NOT_ACCEPTING == "superwhisper-not-accepting"

    def test_classify_outcome_names_not_accepting(self):
        """CB-11: checked before the generic TimeoutError branch."""
        assert classify_outcome(SuperwhisperNotAcceptingError("x")) == "superwhisper-not-accepting"
        assert classify_outcome(TimeoutError("x")) == "timeout"

    def test_state_record_carries_the_outcome_and_stays_retryable(self):
        state: dict = {"processed": {}}
        assert _not_accepting_run(state) == (False, None)
        record = state["processed"][_FAKE_PATH]
        assert record["outcome"] == "superwhisper-not-accepting"
        assert record["status"] == "failed_retry"
        assert record["attempts"] == 1

    def test_log_line_names_the_outcome(self, capsys):
        _not_accepting_run({"processed": {}})
        assert "superwhisper-not-accepting" in capsys.readouterr().out

    def test_heartbeat_carries_last_outcome(self, recordings_dir):
        """CB-13: visible from the next heartbeat after the attempt."""
        state: dict = {"processed": {}}
        _not_accepting_run(state)
        pipeline.write_heartbeat("processing", force=True)
        payload = json.loads(Path(pipeline.HEARTBEAT_FILE).read_text(encoding="utf-8"))
        assert payload["last_outcome"] == "superwhisper-not-accepting"

    def test_heartbeat_last_outcome_defaults_to_null(self, recordings_dir):
        pipeline.write_heartbeat("scanning", force=True)
        payload = json.loads(Path(pipeline.HEARTBEAT_FILE).read_text(encoding="utf-8"))
        assert payload["last_outcome"] is None

    def test_throttled_heartbeat_still_updates_the_cached_outcome(self, recordings_dir):
        record_attempt_result({"processed": {}}, "superwhisper-not-accepting", now=_NOW)
        assert pipeline._heartbeat_last_outcome == "superwhisper-not-accepting"


# --- CB-12: streak semantics -----------------------------------------------------


class TestStreakSemantics:
    def test_k_identical_not_accepting_outcomes_trip_the_breaker(self):
        state: dict = {"processed": {}}
        results = [record_attempt_result(state, OUTCOME_NOT_ACCEPTING, now=_NOW + i) for i in range(3)]
        breaker = state["circuit_breaker"]
        assert results == [False, False, True]
        assert breaker["dependency_down"] is True
        assert breaker["flagged_by_outcome"] == "superwhisper-not-accepting"

    def test_switching_between_timeout_classes_restarts_the_streak(self):
        """CB-12: identical means identical — a not-accepting after two
        timeouts is the first of a new streak, not the third of the old one."""
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down"):
            record_attempt_result(state, "timeout", now=_NOW)
            record_attempt_result(state, "timeout", now=_NOW + 1)
            tripped = record_attempt_result(state, OUTCOME_NOT_ACCEPTING, now=_NOW + 2)
        breaker = state["circuit_breaker"]
        assert tripped is False
        assert breaker["streak"] == 1
        assert breaker["last_outcome"] == "superwhisper-not-accepting"
        assert not breaker.get("dependency_down")

    def test_success_clears_a_not_accepting_trip(self):
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down"):
            for i in range(3):
                record_attempt_result(state, OUTCOME_NOT_ACCEPTING, now=_NOW + i)
            record_attempt_result(state, "success", now=_NOW + 10)
        assert state["circuit_breaker"]["dependency_down"] is False
        assert state["circuit_breaker"]["streak"] == 0
