"""process_audio takes the shared Superwhisper lock (LAG-1057).

The daemon and podscribe must never interleave mode switches/handoffs. The lock
must span mode switch -> handoff -> result wait, and be released before outcome
bookkeeping / auto-relaunch and when the wait raises.
"""

import contextlib
import time
from datetime import datetime

import pytest

import pipeline
from pipeline import CATEGORY_HEADER, process_audio

_FAKE_PATH = "/fake/path/2026-11-20/10-00-00.m4a"
_FAKE_TS = datetime(2026, 11, 20, 10, 0, 0)
_GOOD_OUTPUT = f"{CATEGORY_HEADER} DEFAULT\nFILENAME: Sync\n\nBody text."


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    monkeypatch.setattr(pipeline, "get_audio_duration_ms", lambda _path: None)
    monkeypatch.setattr(pipeline, "check_disk_space", dict)
    monkeypatch.setattr(pipeline, "save_output", lambda *_a, **_k: "/tmp/out.md")
    monkeypatch.setattr(pipeline, "_notify_dependency_down", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "maybe_relaunch_superwhisper", lambda *_a, **_k: None)
    pipeline._heartbeat_last_write = 0.0
    pipeline._heartbeat_in_flight = None
    pipeline._heartbeat_last_outcome = None
    pipeline._heartbeat_dependency_down = False
    yield


def _record_lock(events: list[str]):
    @contextlib.contextmanager
    def fake_lock(*_args, **_kwargs):
        events.append("lock-enter")
        try:
            yield None
        finally:
            events.append("lock-exit")

    return fake_lock


def test_lock_spans_switch_handoff_and_wait(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(pipeline, "superwhisper_lock", _record_lock(events))
    monkeypatch.setattr(pipeline, "switch_superwhisper_mode", lambda *_a, **_k: events.append("switch"))
    monkeypatch.setattr(pipeline, "handoff_to_superwhisper", lambda *_a, **_k: events.append("handoff") or 0.0)
    monkeypatch.setattr(
        pipeline,
        "wait_for_superwhisper_result",
        lambda *_a, **_k: events.append("wait") or _GOOD_OUTPUT,
    )

    ok, category = process_audio(_FAKE_PATH, _FAKE_TS, {"processed": {}})

    assert ok is True
    assert category == "DEFAULT"
    assert events == ["lock-enter", "switch", "handoff", "wait", "lock-exit"]


def test_lock_released_when_wait_raises(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(pipeline, "superwhisper_lock", _record_lock(events))
    monkeypatch.setattr(pipeline, "switch_superwhisper_mode", lambda *_a, **_k: events.append("switch"))
    monkeypatch.setattr(pipeline, "handoff_to_superwhisper", lambda *_a, **_k: events.append("handoff") or 0.0)

    def fail(*_a, **_k):
        events.append("wait")
        raise TimeoutError("Superwhisper did not return a result")

    monkeypatch.setattr(pipeline, "wait_for_superwhisper_result", fail)

    ok, category = process_audio(_FAKE_PATH, _FAKE_TS, {"processed": {}})

    assert ok is False
    assert category is None
    assert events == ["lock-enter", "switch", "handoff", "wait", "lock-exit"]


def test_lock_released_when_mode_switch_raises(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(pipeline, "superwhisper_lock", _record_lock(events))
    monkeypatch.setattr(
        pipeline,
        "switch_superwhisper_mode",
        lambda *_a, **_k: (_ for _ in ()).throw(pipeline.FatalAPIError("no key")),
    )

    with pytest.raises(pipeline.FatalAPIError):
        process_audio(_FAKE_PATH, _FAKE_TS, {"processed": {}})

    assert events == ["lock-enter", "lock-exit"]


# --- LAG-1057 review: wait behaviour ------------------------------------------


def _stub_handoff(monkeypatch):
    monkeypatch.setattr(pipeline, "switch_superwhisper_mode", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "handoff_to_superwhisper", lambda *_a, **_k: time.time())


def test_since_is_captured_after_lock_acquisition(monkeypatch):
    """`since` must postdate the lock, or a failed podscribe run's leftovers are adopted."""
    entered_at: list[float] = []
    captured: dict = {}

    @contextlib.contextmanager
    def fake_lock(*_args, **_kwargs):
        time.sleep(0.05)  # force a measurable gap between attempt start and lock entry
        entered_at.append(time.time())
        yield None

    monkeypatch.setattr(pipeline, "superwhisper_lock", fake_lock)
    _stub_handoff(monkeypatch)

    def capture(_file_path, **kwargs):
        captured.update(kwargs)
        return _GOOD_OUTPUT

    monkeypatch.setattr(pipeline, "wait_for_superwhisper_result", capture)

    ok, _ = process_audio(_FAKE_PATH, _FAKE_TS, {"processed": {}})

    assert ok is True
    assert captured["since"] >= entered_at[0], "since was captured before the lock"
    assert captured["handed_off_at"] >= captured["since"]


def test_lock_wait_heartbeats_within_attempt_budget(monkeypatch):
    """The daemon refreshes its heartbeat while waiting and shares one attempt budget."""
    heartbeats: list[str] = []
    captured: dict = {}
    monkeypatch.setattr(pipeline, "write_heartbeat", lambda phase, **_kw: heartbeats.append(phase))

    @contextlib.contextmanager
    def fake_lock(*_args, **kwargs):
        captured["lock_timeout"] = kwargs.get("timeout")
        on_wait = kwargs.get("on_wait")
        assert on_wait is not None, "lock wait must refresh the daemon heartbeat"
        on_wait()
        yield None

    monkeypatch.setattr(pipeline, "superwhisper_lock", fake_lock)
    _stub_handoff(monkeypatch)

    def capture(_file_path, **kwargs):
        captured["wait_timeout"] = kwargs["timeout"]
        return _GOOD_OUTPUT

    monkeypatch.setattr(pipeline, "wait_for_superwhisper_result", capture)

    ok, _ = process_audio(_FAKE_PATH, _FAKE_TS, {"processed": {}})

    assert ok is True
    assert "processing" in heartbeats
    assert captured["lock_timeout"] is not None
    assert 0 < captured["lock_timeout"] <= pipeline.SUPERWHISPER_TIMEOUT
    assert 0 < captured["wait_timeout"] <= captured["lock_timeout"]


def test_lock_wait_timeout_fails_attempt_transiently(monkeypatch):
    """A held lock makes the bounded acquire time out; the daemon records a retry, not a crash."""
    monkeypatch.setattr(pipeline, "SUPERWHISPER_TIMEOUT", 0.2)
    _stub_handoff(monkeypatch)
    monkeypatch.setattr(
        pipeline,
        "wait_for_superwhisper_result",
        lambda *_a, **_k: pytest.fail("handoff must not happen while the lock is held"),
    )

    with superwhisper_lock_held():
        state: dict = {"processed": {}}
        ok, category = process_audio(_FAKE_PATH, _FAKE_TS, state)

    assert ok is False
    assert category is None
    record = state["processed"][_FAKE_PATH]
    assert record["status"] == "failed_retry"
    assert record["outcome"] == "timeout"


@contextlib.contextmanager
def superwhisper_lock_held():
    """Hold the configured shared lock in a thread for the duration of the body."""
    import threading

    from superwhisper_client import resolve_lock_path, superwhisper_lock

    lock_path = resolve_lock_path()
    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with superwhisper_lock(lock_path):
            ready.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    try:
        yield lock_path
    finally:
        release.set()
        thread.join(timeout=5)
