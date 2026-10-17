"""Dependency-down attempt pricing tests (LAG-803, spec CB-14..CB-16).

The 26-10-08 post-mortem: healthy handoff latency is 55-100 s, yet every
attempt against a dead Superwhisper burned the full 3600 s deadline - three
orders of magnitude of mispricing, and three 1 h burns per file flipped good
files to ``failed_permanent``. LAG-801 made the dead dependency nameable
(``dependency_down``); this file pins how it is priced:

- CB-14: while the flag is up, the per-attempt wait budget is the fast-fail
  budget (``dependency_down_attempt_timeout``, default 600 s - the LAG-802
  accept window) instead of ``superwhisper_timeout``; no flag, no change.
- CB-15: the first success clears the flag (LAG-801 CB-3) and the very next
  attempt is priced at the full budget again.
- CB-16: a capped attempt whose own recording shows an LLM pass in flight
  is proof of life, so its deadline extends to the full budget - a healthy
  long meeting must not be failed by a stale flag.
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
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    SuperwhisperNotAcceptingError,
    attempt_timeout_budget,
    process_audio,
    record_attempt_result,
    wait_for_superwhisper_result,
)

_FAKE_PATH = "/fake/path/2026-10-08/09-58-36.m4a"
_FAKE_TS = datetime(2026, 10, 8, 9, 58, 36)
_FULL = 3600
_FAST = 600
_POLL = 3.0
_RESULT = f"{CATEGORY_HEADER} WORK\nFILENAME: Standup\n\nbody"


class FakeClock:
    """Deterministic clock for the poll loop: ``sleep`` advances ``time``."""

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


@pytest.fixture(autouse=True)
def budgets(monkeypatch):
    monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", _FULL)
    monkeypatch.setattr(pipeline, "DEPENDENCY_DOWN_ATTEMPT_TIMEOUT", _FAST)


@pytest.fixture
def recordings_dir(tmp_path, monkeypatch):
    rd = tmp_path / "recordings"
    rd.mkdir()
    monkeypatch.setattr(pipeline, "SUPERWHISPER_RECORDINGS_DIR", str(rd))
    monkeypatch.setattr(pipeline, "SUPERWHISPER_POLL_INTERVAL", _POLL)
    monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", _FAST)
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    return rd


@pytest.fixture(autouse=True)
def fresh_heartbeat_cache():
    pipeline._heartbeat_dependency_down = False
    pipeline._heartbeat_last_outcome = None
    yield
    pipeline._heartbeat_dependency_down = False
    pipeline._heartbeat_last_outcome = None


def _write_session(recordings_dir: Path, name: str, mtime: float, meta: dict) -> Path:
    rec = recordings_dir / name
    rec.mkdir(exist_ok=True)
    (rec / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    os.utime(rec / "meta.json", (mtime, mtime))
    os.utime(rec, (mtime, mtime))
    return rec


def _flagged_state() -> dict:
    return {"processed": {}, "circuit_breaker": {"dependency_down": True, "streak": 3, "last_outcome": "timeout"}}


# --- CB-14: budget selection ---------------------------------------------------------


class TestBudgetSelection:
    def test_no_breaker_state_gets_the_full_budget(self):
        assert attempt_timeout_budget({"processed": {}}) == _FULL

    def test_breaker_present_but_not_flagged_gets_the_full_budget(self):
        state = {"processed": {}, "circuit_breaker": {"dependency_down": False, "streak": 2}}
        assert attempt_timeout_budget(state) == _FULL

    def test_dependency_down_gets_the_fast_fail_budget(self):
        assert attempt_timeout_budget(_flagged_state()) == _FAST

    def test_fast_budget_never_exceeds_the_full_budget(self, monkeypatch):
        """A short superwhisper_timeout must not be lengthened by the flag."""
        monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 300)
        assert attempt_timeout_budget(_flagged_state()) == 300

    @pytest.mark.parametrize("disabled", [0, -1])
    def test_non_positive_fast_budget_disables_pricing(self, monkeypatch, disabled):
        monkeypatch.setattr(pipeline, "DEPENDENCY_DOWN_ATTEMPT_TIMEOUT", disabled)
        assert attempt_timeout_budget(_flagged_state()) == _FULL

    def test_selection_does_not_mutate_state(self):
        state = _flagged_state()
        before = json.dumps(state, sort_keys=True)
        attempt_timeout_budget(state)
        assert json.dumps(state, sort_keys=True) == before


class TestBudgetConfig:
    def test_default_fast_budget_is_the_accept_window(self):
        """Default 600 s - the LAG-802 superwhisper-not-accepting budget."""
        assert config.DEPENDENCY_DOWN_ATTEMPT_TIMEOUT == 600
        assert config.DEPENDENCY_DOWN_ATTEMPT_TIMEOUT == config.SUPERWHISPER_ACCEPT_TIMEOUT

    def test_config_key_overrides_the_default(self, tmp_path, monkeypatch):
        cfg = yaml.safe_load(Path(os.environ["SWOWF_CONFIG_PATH"]).read_text(encoding="utf-8"))
        cfg["dependency_down_attempt_timeout"] = 240
        override = tmp_path / "config.yaml"
        override.write_text(yaml.dump(cfg), encoding="utf-8")
        original = os.environ["SWOWF_CONFIG_PATH"]
        monkeypatch.setenv("SWOWF_CONFIG_PATH", str(override))
        try:
            assert importlib.reload(config).DEPENDENCY_DOWN_ATTEMPT_TIMEOUT == 240
        finally:
            monkeypatch.setenv("SWOWF_CONFIG_PATH", original)
            importlib.reload(config)

    def test_example_config_documents_the_key(self):
        example = Path(__file__).parents[2] / "config.example.yaml"
        assert yaml.safe_load(example.read_text(encoding="utf-8"))["dependency_down_attempt_timeout"] == 600


# --- CB-14: the wait honours the budget ------------------------------------------------


def _accepted_but_silent(recordings_dir: Path, clock: FakeClock) -> None:
    """A session dir appears (Superwhisper took the handoff) but never progresses:
    no LLM pass, no result. Its meta keeps changing so the abandoned-stub
    fast-fail never fires - the shape that used to burn the full deadline."""

    def tick(c: FakeClock) -> None:
        _write_session(recordings_dir, "silent", c.now, {"duration": 0, "processingTime": int(c.now)})

    clock.on_tick.append(tick)


class TestWaitHonoursBudget:
    def test_capped_wait_times_out_at_the_fast_budget(self, recordings_dir, clock):
        _accepted_but_silent(recordings_dir, clock)
        with pytest.raises(TimeoutError) as excinfo:
            wait_for_superwhisper_result("x.m4a", since=clock.now - 1, handed_off_at=clock.now, timeout=_FAST)
        assert not isinstance(excinfo.value, SuperwhisperNotAcceptingError)
        assert _FAST <= clock.elapsed < _FAST + 2 * _POLL
        assert f"within {_FAST}s" in str(excinfo.value)

    def test_default_wait_is_unchanged(self, recordings_dir, clock):
        """No timeout argument -> superwhisper_timeout, exactly as before."""
        _accepted_but_silent(recordings_dir, clock)
        with pytest.raises(TimeoutError) as excinfo:
            wait_for_superwhisper_result("x.m4a", since=clock.now - 1, handed_off_at=clock.now)
        assert _FULL <= clock.elapsed < _FULL + 2 * _POLL
        assert f"within {_FULL}s" in str(excinfo.value)

    def test_capped_wait_still_returns_a_fast_result(self, recordings_dir, clock):
        start = clock.now

        def finish(c: FakeClock) -> None:
            if c.now - start >= 90:
                _write_session(recordings_dir, "fast", start + 90, {"llmResult": _RESULT})

        clock.on_tick.append(finish)
        assert wait_for_superwhisper_result("x.m4a", since=start - 1, handed_off_at=start, timeout=_FAST) == _RESULT


# --- CB-16: proof of life extends a capped attempt -------------------------------------


class TestLivenessExtension:
    def test_llm_pass_in_flight_extends_to_the_full_budget(self, recordings_dir, clock):
        """A healthy long meeting under a stale flag: LLM pass starts at t+60,
        result lands at t+1500 - past the 600 s cap, inside the full budget."""
        start = clock.now

        def progress(c: FakeClock) -> None:
            elapsed = c.now - start
            if 60 <= elapsed < 1500 and not (recordings_dir / "long").exists():
                _write_session(recordings_dir, "long", c.now, {"languageModelProcessingTime": 1})
            if elapsed >= 1500:
                meta = {"languageModelProcessingTime": 1, "llmResult": _RESULT}
                rec = recordings_dir / "long" / "meta.json"
                if json.loads(rec.read_text(encoding="utf-8")) != meta:
                    _write_session(recordings_dir, "long", c.now, meta)

        clock.on_tick.append(progress)
        got = wait_for_superwhisper_result("x.m4a", since=start - 1, handed_off_at=start, timeout=_FAST)
        assert got == _RESULT
        assert clock.elapsed > _FAST

    def test_extended_wait_is_still_bounded_by_the_full_budget(self, recordings_dir, clock):
        start = clock.now
        _write_session(recordings_dir, "stuck", start + 1, {"languageModelProcessingTime": 1})
        with pytest.raises(TimeoutError) as excinfo:
            wait_for_superwhisper_result("x.m4a", since=start - 1, handed_off_at=start, timeout=_FAST)
        assert _FULL <= clock.elapsed < _FULL + 2 * _POLL
        assert f"within {_FULL}s" in str(excinfo.value)

    def test_extension_is_logged_once(self, recordings_dir, clock, capsys):
        start = clock.now
        _write_session(recordings_dir, "stuck", start + 1, {"languageModelProcessingTime": 1})
        with pytest.raises(TimeoutError):
            wait_for_superwhisper_result("x.m4a", since=start - 1, handed_off_at=start, timeout=_FAST)
        assert capsys.readouterr().out.count("LLM pass in flight") == 1

    def test_duration_mismatched_recording_is_not_proof_of_life(self, recordings_dir, clock):
        """Someone else's in-flight recording (R3 mismatch) must not extend ours."""
        start = clock.now
        _write_session(recordings_dir, "other", start + 1, {"languageModelProcessingTime": 1, "duration": 999_000})
        with pytest.raises(TimeoutError):
            wait_for_superwhisper_result(
                "x.m4a", since=start - 1, handed_off_at=start, timeout=_FAST, expected_duration_ms=60_000
            )
        assert _FAST <= clock.elapsed < _FAST + 2 * _POLL

    def test_full_budget_wait_is_not_extended_further(self, recordings_dir, clock, capsys):
        start = clock.now
        _write_session(recordings_dir, "stuck", start + 1, {"languageModelProcessingTime": 1})
        with pytest.raises(TimeoutError):
            wait_for_superwhisper_result("x.m4a", since=start - 1, handed_off_at=start)
        assert _FULL <= clock.elapsed < _FULL + 2 * _POLL
        assert "LLM pass in flight" not in capsys.readouterr().out


# --- CB-14/CB-15: process_audio wiring --------------------------------------------------


def _run(state: dict, *, wait_side_effect=None, wait_return=None):
    with (
        patch("pipeline.get_audio_duration_ms", return_value=None),
        patch("pipeline.switch_superwhisper_mode"),
        patch("pipeline.handoff_to_superwhisper", return_value=1.0),
        patch("pipeline.wait_for_superwhisper_result", side_effect=wait_side_effect, return_value=wait_return) as wait,
        patch("pipeline.save_output", return_value="/tmp/test-work/x.md"),
        patch("pipeline.save_state"),
        patch("pipeline._notify_dependency_down"),
    ):
        result = process_audio(_FAKE_PATH, _FAKE_TS, state)
    return result, wait


class TestProcessAudioPricing:
    def test_unflagged_attempt_is_priced_at_the_full_budget(self):
        _, wait = _run({"processed": {}}, wait_return=_RESULT)
        assert wait.call_args.kwargs["timeout"] == _FULL

    def test_flagged_attempt_is_priced_at_the_fast_budget(self):
        _, wait = _run(_flagged_state(), wait_side_effect=TimeoutError("nothing"))
        assert wait.call_args.kwargs["timeout"] == _FAST

    def test_failure_record_carries_the_budget(self):
        state = _flagged_state()
        _run(state, wait_side_effect=TimeoutError("nothing"))
        record = state["processed"][_FAKE_PATH]
        assert record["attempt_budget_s"] == _FAST
        assert record["outcome"] == OUTCOME_TIMEOUT
        assert record["status"] == "failed_retry"

    def test_permanent_failure_record_carries_the_budget(self):
        state = {"processed": {}}
        _run(state, wait_side_effect=pipeline.PermanentFileError("refused"))
        assert state["processed"][_FAKE_PATH]["attempt_budget_s"] == _FULL

    def test_priced_attempt_is_logged(self, capsys):
        _run(_flagged_state(), wait_side_effect=TimeoutError("nothing"))
        out = capsys.readouterr().out
        assert "dependency_down" in out
        assert f"{_FAST}s" in out

    def test_unflagged_attempt_logs_no_pricing_line(self, capsys):
        _run({"processed": {}}, wait_return=_RESULT)
        assert "dependency_down" not in capsys.readouterr().out


class TestFlagLifecycle:
    def test_trip_then_success_restores_the_full_budget(self):
        """End to end through the breaker: K timeouts trip the flag and price
        the next attempt at 600 s; its success clears the flag (CB-3) and the
        attempt after that is back at 3600 s."""
        state: dict = {"processed": {}}
        with patch("pipeline._notify_dependency_down"):
            for i in range(pipeline.CIRCUIT_BREAKER_THRESHOLD):
                record_attempt_result(state, OUTCOME_TIMEOUT, now=1_000.0 + i)
        assert attempt_timeout_budget(state) == _FAST

        _, wait = _run(state, wait_return=_RESULT)
        assert wait.call_args.kwargs["timeout"] == _FAST
        assert state["circuit_breaker"]["dependency_down"] is False
        assert state["processed"][_FAKE_PATH]["outcome"] == OUTCOME_SUCCESS

        _, wait = _run(state, wait_return=_RESULT)
        assert wait.call_args.kwargs["timeout"] == _FULL

    def test_failures_under_the_flag_keep_the_fast_budget(self):
        """The flag is sticky (CB-3): failed fast attempts stay fast."""
        state = _flagged_state()
        for _ in range(2):
            _, wait = _run(state, wait_side_effect=TimeoutError("nothing"))
            assert wait.call_args.kwargs["timeout"] == _FAST
        assert state["circuit_breaker"]["dependency_down"] is True


class TestEndToEnd:
    def _process(self, state: dict) -> tuple[bool, str | None]:
        with (
            patch("pipeline.get_audio_duration_ms", return_value=None),
            patch("pipeline.switch_superwhisper_mode"),
            patch("pipeline.handoff_to_superwhisper", side_effect=lambda _p: time.time()),
            patch("pipeline.save_state"),
            patch("pipeline._notify_dependency_down"),
        ):
            return process_audio(_FAKE_PATH, _FAKE_TS, state)

    def test_flagged_attempt_against_a_silent_superwhisper_burns_600s_not_3600s(self, recordings_dir, clock):
        """The incident shape, real poll loop: Superwhisper takes the handoff,
        then produces nothing. Under the flag the attempt fails at ~600 s."""
        _accepted_but_silent(recordings_dir, clock)
        state = _flagged_state()
        assert self._process(state) == (False, None)
        record = state["processed"][_FAKE_PATH]
        assert _FAST <= clock.elapsed < _FAST + 2 * _POLL
        assert record["status"] == "failed_retry"
        assert record["outcome"] == OUTCOME_TIMEOUT
        assert record["attempt_budget_s"] == _FAST
        assert f"within {_FAST}s" in record["error"]

    def test_unflagged_attempt_against_a_silent_superwhisper_keeps_the_full_deadline(self, recordings_dir, clock):
        _accepted_but_silent(recordings_dir, clock)
        state: dict = {"processed": {}}
        self._process(state)
        assert _FULL <= clock.elapsed < _FULL + 2 * _POLL
        assert state["processed"][_FAKE_PATH]["attempt_budget_s"] == _FULL
