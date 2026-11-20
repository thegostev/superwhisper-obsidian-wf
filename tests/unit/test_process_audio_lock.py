"""process_audio takes the shared Superwhisper lock (LAG-1057).

The daemon and podscribe must never interleave mode switches/handoffs. The lock
must span mode switch -> handoff -> result wait, and be released before outcome
bookkeeping / auto-relaunch and when the wait raises.
"""

import contextlib
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
