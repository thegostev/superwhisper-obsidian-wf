"""Guarded auto-relaunch tests (LAG-804, spec CB-14..CB-20).

The 26-10-08 post-mortem: the known cure for a zombie Superwhisper (quit the
app, open it again) worked on Sep 13 and Oct 8, but a human ran it both times.
During the 49 h incident nobody was watching, so nobody acted.

These tests pin the guarded relaunch contract: only a ``superwhisper-not-accepting``
streak at K is eligible (CB-14); a busy idle gate blocks instead of proceeding
(CB-15); a cooldown spans episodes (CB-16); at most one relaunch runs per
episode, persisted before the remedy and reset only by a completion (CB-17);
the remedy is quit + open and nothing stronger (CB-18); the human is told
before and after (CB-19); and a config kill switch turns it all off (CB-20).
"""

import importlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import config
import pipeline
from pipeline import (
    OUTCOME_NOT_ACCEPTING,
    OUTCOME_PERMANENT,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    OUTCOME_TRANSIENT,
    SuperwhisperNotAcceptingError,
    maybe_relaunch_superwhisper,
    process_audio,
    record_attempt_result,
)

_FAKE_PATH = "/fake/path/2026-10-08/09-58-36.m4a"
_FAKE_TS = datetime(2026, 10, 8, 9, 58, 36)
_NOW = 1_000_000.0
_K = 3


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep every test away from real state, heartbeat, Superwhisper and Notification Center."""
    monkeypatch.setattr(pipeline, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.setattr(pipeline, "SUPERWHISPER_AUTO_RELAUNCH", True)
    monkeypatch.setattr(pipeline, "SUPERWHISPER_RELAUNCH_COOLDOWN", 3600.0)
    monkeypatch.setattr(pipeline, "_notify_dependency_down", lambda state: True)
    pipeline._heartbeat_dependency_down = False
    pipeline._heartbeat_last_outcome = None
    yield
    pipeline._heartbeat_dependency_down = False
    pipeline._heartbeat_last_outcome = None


class Calls:
    """Records the guarded side effects: remedy runs, notifications, idle reads."""

    def __init__(self, idle: bool = True, remedy_result: str = "relaunched") -> None:
        self.idle = idle
        self.remedy_result = remedy_result
        self.remedy = 0
        self.notes: list[str] = []
        self.idle_reads = 0
        self.state_on_disk_at_remedy: dict | None = None

    def is_idle(self) -> bool:
        self.idle_reads += 1
        return self.idle

    def run_remedy(self) -> str:
        self.remedy += 1
        state_file = Path(pipeline.STATE_FILE)
        if state_file.exists():
            self.state_on_disk_at_remedy = json.loads(state_file.read_text(encoding="utf-8"))
        return self.remedy_result

    def notify(self, message: str, title: str) -> bool:
        self.notes.append(message)
        return True


@pytest.fixture
def calls(monkeypatch):
    c = Calls()
    monkeypatch.setattr(pipeline, "_is_superwhisper_idle", c.is_idle)
    monkeypatch.setattr(pipeline, "_relaunch_superwhisper", c.run_remedy)
    monkeypatch.setattr(pipeline, "_notify", c.notify)
    return c


def _streak(state: dict, outcome: str, times: int, start: float = _NOW) -> None:
    for i in range(times):
        record_attempt_result(state, outcome, now=start + i)


def _eligible_state(start: float = _NOW) -> dict:
    state: dict = {"processed": {}}
    _streak(state, OUTCOME_NOT_ACCEPTING, _K, start)
    return state


def _breaker(state: dict) -> dict:
    return state["circuit_breaker"]


# --- CB-14: eligibility -------------------------------------------------------


class TestEligibility:
    def test_not_accepting_streak_at_k_relaunches(self, calls):
        state = _eligible_state()
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "relaunched"
        assert calls.remedy == 1

    def test_below_k_is_not_eligible(self, calls):
        state: dict = {"processed": {}}
        _streak(state, OUTCOME_NOT_ACCEPTING, _K - 1)
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "not_eligible"
        assert calls.remedy == 0

    def test_timeout_streak_is_not_eligible(self, calls):
        state: dict = {"processed": {}}
        _streak(state, OUTCOME_TIMEOUT, _K + 2)
        assert _breaker(state)["dependency_down"] is True
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "not_eligible"
        assert calls.remedy == 0

    def test_sticky_flag_alone_does_not_make_a_later_streak_eligible(self, calls):
        """Flag raised by not-accepting, then the streak moves to another class."""
        state = _eligible_state()
        _breaker(state)["relaunch_used"] = False
        record_attempt_result(state, OUTCOME_TRANSIENT, now=_NOW + 5)
        assert _breaker(state)["dependency_down"] is True
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "not_eligible"

    def test_empty_state_is_not_eligible(self, calls):
        assert maybe_relaunch_superwhisper({"processed": {}}, now=_NOW) == "not_eligible"

    def test_only_not_accepting_is_a_relaunch_outcome(self):
        assert frozenset({OUTCOME_NOT_ACCEPTING}) == pipeline.RELAUNCH_OUTCOMES


# --- CB-15: busy idle gate blocks ---------------------------------------------


class TestIdleGate:
    def test_busy_gate_refuses(self, calls):
        """AC: guard refuses when the idle gate is busy (user dictation in progress)."""
        calls.idle = False
        state = _eligible_state()
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "busy"
        assert calls.remedy == 0

    def test_busy_gate_is_a_single_read_not_the_r7_wait(self, calls, monkeypatch):
        calls.idle = False

        def _forbidden(*args, **kwargs):
            raise AssertionError("the relaunch guard must not use the R7 wait (it proceeds anyway)")

        monkeypatch.setattr(pipeline, "_wait_for_superwhisper_idle", _forbidden)
        monkeypatch.setattr(pipeline.time, "sleep", _forbidden)
        assert maybe_relaunch_superwhisper(_eligible_state(), now=_NOW + 10) == "busy"
        assert calls.idle_reads == 1

    def test_busy_refusal_keeps_the_cap_and_is_silent(self, calls):
        calls.idle = False
        state = _eligible_state()
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        assert not _breaker(state).get("relaunch_used")
        assert calls.notes == []

    def test_relaunch_runs_once_the_gate_clears(self, calls):
        state = _eligible_state()
        calls.idle = False
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "busy"
        record_attempt_result(state, OUTCOME_NOT_ACCEPTING, now=_NOW + 700)
        calls.idle = True
        assert maybe_relaunch_superwhisper(state, now=_NOW + 701) == "relaunched"
        assert calls.remedy == 1


# --- CB-16: cooldown ----------------------------------------------------------


class TestCooldown:
    def test_inside_cooldown_refuses(self, calls):
        """AC: guard refuses during cooldown."""
        state = _eligible_state()
        _breaker(state)["last_relaunch_epoch"] = _NOW - 100
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "cooldown"
        assert calls.remedy == 0

    def test_cooldown_refusal_does_not_read_the_idle_gate_or_consume_the_cap(self, calls):
        state = _eligible_state()
        _breaker(state)["last_relaunch_epoch"] = _NOW - 100
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        assert calls.idle_reads == 0
        assert not _breaker(state).get("relaunch_used")

    def test_expired_cooldown_allows_the_relaunch(self, calls):
        state = _eligible_state()
        _breaker(state)["last_relaunch_epoch"] = _NOW - 3600
        assert maybe_relaunch_superwhisper(state, now=_NOW) == "relaunched"

    def test_cooldown_spans_episodes(self, calls):
        state = _eligible_state()
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "relaunched"
        record_attempt_result(state, OUTCOME_SUCCESS, now=_NOW + 100)
        _streak(state, OUTCOME_NOT_ACCEPTING, _K, start=_NOW + 200)
        assert maybe_relaunch_superwhisper(state, now=_NOW + 300) == "cooldown"
        assert calls.remedy == 1

    def test_cooldown_default_is_one_hour(self):
        assert config.SUPERWHISPER_RELAUNCH_COOLDOWN == 3600


# --- CB-17: one relaunch per episode -----------------------------------------


class TestOnePerEpisode:
    def test_executes_once_with_both_guards_green(self, calls):
        """AC: executes once with both guards green."""
        state = _eligible_state()
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "relaunched"
        breaker = _breaker(state)
        assert breaker["relaunch_used"] is True
        assert breaker["last_relaunch_epoch"] == _NOW + 10
        assert breaker["last_relaunch_result"] == "relaunched"
        assert calls.remedy == 1

    def test_second_eligible_streak_in_the_same_episode_is_refused(self, calls):
        """AC: hard cap of one relaunch per streak — even after the cooldown expires."""
        state = _eligible_state()
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        _streak(state, OUTCOME_NOT_ACCEPTING, _K, start=_NOW + 10_000)
        assert maybe_relaunch_superwhisper(state, now=_NOW + 20_000) == "already_used"
        assert calls.remedy == 1

    def test_a_streak_reset_does_not_reset_the_cap(self, calls):
        state = _eligible_state()
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        for outcome in (OUTCOME_TRANSIENT, OUTCOME_PERMANENT, OUTCOME_TIMEOUT):
            record_attempt_result(state, outcome, now=_NOW + 20)
            assert _breaker(state)["relaunch_used"] is True
        _streak(state, OUTCOME_NOT_ACCEPTING, _K, start=_NOW + 10_000)
        assert maybe_relaunch_superwhisper(state, now=_NOW + 20_000) == "already_used"

    def test_only_a_completion_resets_the_cap(self, calls):
        """AC: reset only on successful completion."""
        state = _eligible_state()
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        record_attempt_result(state, OUTCOME_SUCCESS, now=_NOW + 100)
        assert _breaker(state)["relaunch_used"] is False
        _streak(state, OUTCOME_NOT_ACCEPTING, _K, start=_NOW + 10_000)
        assert maybe_relaunch_superwhisper(state, now=_NOW + 20_000) == "relaunched"
        assert calls.remedy == 2

    @pytest.mark.parametrize("result", ["quit_failed", "relaunch_failed"])
    def test_a_failed_remedy_still_consumes_the_cap(self, calls, result):
        """No relaunch loops: a failed remedy is not retried in the same episode."""
        calls.remedy_result = result
        state = _eligible_state()
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == result
        assert _breaker(state)["last_relaunch_result"] == result
        _streak(state, OUTCOME_NOT_ACCEPTING, _K, start=_NOW + 10_000)
        assert maybe_relaunch_superwhisper(state, now=_NOW + 20_000) == "already_used"
        assert calls.remedy == 1

    def test_cap_is_persisted_before_the_remedy_runs(self, calls):
        state = _eligible_state()
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        on_disk = calls.state_on_disk_at_remedy
        assert on_disk is not None, "state must be saved before the remedy runs"
        assert on_disk["circuit_breaker"]["relaunch_used"] is True
        assert on_disk["circuit_breaker"]["last_relaunch_epoch"] == _NOW + 10

    def test_cap_survives_a_daemon_restart(self, calls):
        state = _eligible_state()
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        pipeline.save_state(state)
        reloaded = pipeline.load_state()
        _streak(reloaded, OUTCOME_NOT_ACCEPTING, _K, start=_NOW + 10_000)
        assert maybe_relaunch_superwhisper(reloaded, now=_NOW + 20_000) == "already_used"
        assert calls.remedy == 1


# --- CB-18: the remedy --------------------------------------------------------


class FakeMac:
    """Fakes ``subprocess.run`` for osascript/open, with a clock that sleep advances."""

    def __init__(self, quits: bool = True, open_rc: int = 0, quit_raises: Exception | None = None) -> None:
        self.quits = quits
        self.open_rc = open_rc
        self.quit_raises = quit_raises
        self.running = True
        self.calls: list[tuple[list[str], dict]] = []
        self.now = 5_000.0
        self.heartbeats: list[bool] = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        script = " ".join(argv)
        if argv[0] == "/usr/bin/osascript" and "to quit" in script:
            if self.quit_raises:
                raise self.quit_raises
            if self.quits:
                self.running = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "/usr/bin/osascript" and "is running" in script:
            return subprocess.CompletedProcess(argv, 0, "true\n" if self.running else "false\n", "")
        if argv[0] == "/usr/bin/open":
            if self.open_rc == 0:
                self.running = True
            return subprocess.CompletedProcess(argv, self.open_rc, "", "boom" if self.open_rc else "")
        raise AssertionError(f"unexpected subprocess: {argv}")

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        return self.now

    def heartbeat(self, phase, *args, force=False, **kwargs):
        self.heartbeats.append(force)
        return True

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


@pytest.fixture
def mac(monkeypatch):
    fake = FakeMac()
    monkeypatch.setattr(pipeline.subprocess, "run", fake.run)
    monkeypatch.setattr(pipeline.time, "sleep", fake.sleep)
    monkeypatch.setattr(pipeline.time, "time", fake.time)
    monkeypatch.setattr(pipeline, "write_heartbeat", fake.heartbeat)
    return fake


class TestRemedy:
    def test_quits_then_opens_the_app(self, mac):
        assert pipeline._relaunch_superwhisper() == "relaunched"
        quit_i = next(i for i, a in enumerate(mac.argvs) if "to quit" in " ".join(a))
        open_i = next(i for i, a in enumerate(mac.argvs) if a[0] == "/usr/bin/open")
        assert quit_i < open_i
        assert mac.argvs[open_i] == ["/usr/bin/open", "-a", "Superwhisper"]

    def test_waits_a_warm_up_after_opening(self, mac):
        start = mac.now
        pipeline._relaunch_superwhisper()
        assert mac.now - start >= pipeline.RELAUNCH_WARMUP_SECONDS

    def test_still_running_after_grace_is_quit_failed_and_never_opens(self, mac):
        mac.quits = False
        start = mac.now
        assert pipeline._relaunch_superwhisper() == "quit_failed"
        assert not any(a[0] == "/usr/bin/open" for a in mac.argvs)
        assert mac.now - start >= pipeline.RELAUNCH_QUIT_GRACE

    def test_never_force_kills(self, mac):
        mac.quits = False
        pipeline._relaunch_superwhisper()
        joined = [" ".join(a) for a in mac.argvs]
        assert not any(("kill" in j or "force" in j.lower()) for j in joined)

    def test_quit_timeout_is_reported_not_raised(self, mac):
        mac.quit_raises = subprocess.TimeoutExpired(["/usr/bin/osascript"], 30)
        assert pipeline._relaunch_superwhisper() == "quit_failed"

    def test_quit_timeout_that_still_stopped_the_app_relaunches(self, mac):
        """A hung quit call can still have worked; the running check decides."""
        mac.quit_raises = subprocess.TimeoutExpired(["/usr/bin/osascript"], 30)
        mac.running = False
        assert pipeline._relaunch_superwhisper() == "relaunched"

    def test_open_failure_is_relaunch_failed(self, mac):
        mac.open_rc = 1
        assert pipeline._relaunch_superwhisper() == "relaunch_failed"

    def test_open_oserror_is_relaunch_failed(self, mac, monkeypatch):
        original = mac.run

        def run(argv, **kwargs):
            if argv[0] == "/usr/bin/open":
                raise OSError("no such file")
            return original(argv, **kwargs)

        monkeypatch.setattr(pipeline.subprocess, "run", run)
        assert pipeline._relaunch_superwhisper() == "relaunch_failed"

    def test_failed_running_check_reads_as_still_running(self, mac, monkeypatch):
        """Fail closed: if we cannot tell the app stopped, we do not open it again."""
        original = mac.run

        def run(argv, **kwargs):
            if "is running" in " ".join(argv):
                raise subprocess.TimeoutExpired(argv, 10)
            return original(argv, **kwargs)

        monkeypatch.setattr(pipeline.subprocess, "run", run)
        assert pipeline._relaunch_superwhisper() == "quit_failed"

    def test_every_subprocess_uses_an_absolute_path_and_a_timeout(self, mac):
        pipeline._relaunch_superwhisper()
        assert mac.calls
        for argv, kwargs in mac.calls:
            assert argv[0].startswith("/"), argv
            assert kwargs.get("timeout"), argv

    def test_forces_a_heartbeat_before_and_after(self, mac):
        pipeline._relaunch_superwhisper()
        assert mac.heartbeats[0] is True
        assert mac.heartbeats[-1] is True


# --- CB-19: notifications -----------------------------------------------------


class TestNotifications:
    def test_notifies_before_and_after(self, calls):
        state = _eligible_state()
        state["processed"] = {"a": {"status": "failed_retry"}, "b": {"status": "failed_retry"}}
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        assert len(calls.notes) == 2
        before, after = calls.notes
        assert "relaunching" in before.lower()
        assert "2 files" in before
        assert "relaunched" in after.lower()

    @pytest.mark.parametrize("result", ["quit_failed", "relaunch_failed"])
    def test_failure_tells_the_human_what_to_do(self, calls, result):
        calls.remedy_result = result
        maybe_relaunch_superwhisper(_eligible_state(), now=_NOW + 10)
        after = calls.notes[-1].lower()
        assert "by hand" in after

    @pytest.mark.parametrize(
        "setup",
        [
            lambda s, c: setattr(c, "idle", False),
            lambda s, c: s["circuit_breaker"].__setitem__("last_relaunch_epoch", _NOW),
            lambda s, c: s["circuit_breaker"].__setitem__("relaunch_used", True),
        ],
        ids=["busy", "cooldown", "already_used"],
    )
    def test_refusals_are_silent(self, calls, setup):
        state = _eligible_state()
        setup(state, calls)
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        assert calls.notes == []

    def test_notification_failure_does_not_stop_the_remedy(self, calls, monkeypatch):
        def _broken(message, title):
            raise RuntimeError("Notification Center down")

        monkeypatch.setattr(pipeline, "_notify", _broken)
        state = _eligible_state()
        assert maybe_relaunch_superwhisper(state, now=_NOW + 10) == "relaunched"
        assert calls.remedy == 1
        assert _breaker(state)["last_relaunch_result"] == "relaunched"

    def test_sender_passes_text_as_argv(self, monkeypatch):
        captured: list[list[str]] = []

        def _run(argv, **kwargs):
            captured.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(pipeline.subprocess, "run", _run)
        message = 'quote " and \\ backslash'
        assert pipeline._notify(message, "Title") is True
        argv = captured[0]
        assert argv[0] == "/usr/bin/osascript"
        assert argv[-2:] == [message, "Title"]
        assert all(message not in part for part in argv[:-2])

    def test_sender_failure_returns_false(self, monkeypatch):
        def _run(argv, **kwargs):
            raise OSError("no osascript")

        monkeypatch.setattr(pipeline.subprocess, "run", _run)
        assert pipeline._notify("m", "t") is False


# --- CB-20: kill switch, config, wiring --------------------------------------


class TestConfig:
    def test_auto_relaunch_defaults_on(self):
        assert config.SUPERWHISPER_AUTO_RELAUNCH is True

    def test_kill_switch_refuses(self, calls, monkeypatch):
        monkeypatch.setattr(pipeline, "SUPERWHISPER_AUTO_RELAUNCH", False)
        assert maybe_relaunch_superwhisper(_eligible_state(), now=_NOW + 10) == "disabled"
        assert calls.remedy == 0
        assert calls.notes == []

    def test_config_keys_override_the_defaults(self, tmp_path, monkeypatch):
        cfg = yaml.safe_load(Path(os.environ["SWOWF_CONFIG_PATH"]).read_text(encoding="utf-8"))
        cfg["superwhisper_auto_relaunch"] = False
        cfg["superwhisper_relaunch_cooldown"] = 120
        override = tmp_path / "config.yaml"
        override.write_text(yaml.dump(cfg), encoding="utf-8")
        original = os.environ["SWOWF_CONFIG_PATH"]
        monkeypatch.setenv("SWOWF_CONFIG_PATH", str(override))
        try:
            reloaded = importlib.reload(config)
            assert reloaded.SUPERWHISPER_AUTO_RELAUNCH is False
            assert reloaded.SUPERWHISPER_RELAUNCH_COOLDOWN == 120
        finally:
            monkeypatch.setenv("SWOWF_CONFIG_PATH", original)
            importlib.reload(config)


def _not_accepting_run(state: dict, file_path: str = _FAKE_PATH):
    """Drive process_audio through a superwhisper-not-accepting failure."""
    with (
        patch("pipeline.get_audio_duration_ms", return_value=None),
        patch("pipeline.switch_superwhisper_mode"),
        patch("pipeline.handoff_to_superwhisper", return_value=_NOW),
        patch(
            "pipeline.wait_for_superwhisper_result",
            side_effect=SuperwhisperNotAcceptingError("no session dir within 600s"),
        ),
    ):
        return process_audio(file_path, _FAKE_TS, state)


class TestProcessAudioWiring:
    def test_kth_not_accepting_attempt_runs_the_remedy_once(self, calls):
        state: dict = {"processed": {}}
        for i in range(_K + 2):
            _not_accepting_run(state, f"/fake/{i}.m4a")
        assert calls.remedy == 1
        breaker = _breaker(state)
        assert breaker["relaunch_used"] is True
        assert breaker["last_relaunch_result"] == "relaunched"

    def test_remedy_fields_persist_in_the_state_file(self, calls):
        state: dict = {"processed": {}}
        for i in range(_K):
            _not_accepting_run(state, f"/fake/{i}.m4a")
        on_disk = json.loads(Path(pipeline.STATE_FILE).read_text(encoding="utf-8"))
        breaker = on_disk["circuit_breaker"]
        assert breaker["relaunch_used"] is True
        assert breaker["last_relaunch_result"] == "relaunched"
        assert isinstance(breaker["last_relaunch_epoch"], float)

    def test_attempt_bookkeeping_survives_a_crashing_guard(self, calls, monkeypatch):
        def _boom(state, *, now=None):
            raise RuntimeError("guard exploded")

        monkeypatch.setattr(pipeline, "maybe_relaunch_superwhisper", _boom)
        state: dict = {"processed": {}}
        ok, _ = _not_accepting_run(state)
        assert ok is False
        assert state["processed"][_FAKE_PATH]["outcome"] == OUTCOME_NOT_ACCEPTING
        assert state["processed"][_FAKE_PATH]["status"] == "failed_retry"

    def test_log_names_refusals_and_results(self, calls, capsys):
        calls.idle = False
        state = _eligible_state()
        maybe_relaunch_superwhisper(state, now=_NOW + 10)
        assert "busy" in capsys.readouterr().out
        calls.idle = True
        maybe_relaunch_superwhisper(state, now=_NOW + 20)
        assert "relaunched" in capsys.readouterr().out

    def test_not_eligible_is_not_logged(self, calls, capsys):
        maybe_relaunch_superwhisper({"processed": {}}, now=_NOW)
        assert "relaunch" not in capsys.readouterr().out.lower()
