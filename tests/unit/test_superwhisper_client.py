"""Tests for analyze_file() - the programmatic Superwhisper seam (LAG-1057).

The seam must reuse the daemon's handoff/wait machinery without writing daemon
heartbeat or state, must accept modes whose llmResult is cleaned text (no
``CATEGORY:`` header), and must hold the shared lock for the whole switch ->
handoff -> wait sequence. Superwhisper itself is faked through
``pipeline.subprocess.run``; recordings live in a temp directory.
"""

import fcntl
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline
import superwhisper_client
from superwhisper_client import Segment, analyze_file

AUDIO_DURATION_MS = 600_000
CLEAN_TEXT = "Cleaned podcast text. No CATEGORY header."
RAW_TRANSCRIPT = "word one word two"
SEGMENTS = [
    {"start": 0.0, "end": 1.25, "text": "word one"},
    {"start": 1.25, "end": 2.5, "text": "word two"},
]


def _meta(**overrides) -> dict:
    meta = {
        "duration": AUDIO_DURATION_MS,
        "rawResult": RAW_TRANSCRIPT,
        "llmResult": CLEAN_TEXT,
        "segments": SEGMENTS,
    }
    meta.update(overrides)
    return meta


def _fake_run(recordings_dir: Path, meta: dict, commands: list[list[str]]):
    """Fake `open`: Superwhisper session dir appears when the audio file is opened."""

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[:1] == ["open"] and cmd[-1:] == ["Superwhisper"]:
            session = recordings_dir / "session-1"
            session.mkdir()
            (session / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def _lock_is_held(lock_path: Path) -> bool:
    try:
        with open(lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    audio = tmp_path / "episode.m4a"
    audio.write_bytes(b"fake audio")

    monkeypatch.setattr(pipeline, "SUPERWHISPER_RECORDINGS_DIR", str(recordings))
    monkeypatch.setattr(pipeline, "SUPERWHISPER_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(pipeline, "SUPERWHISPER_ACCEPT_TIMEOUT", 600)
    monkeypatch.setattr(pipeline, "SUPERWHISPER_MODE_KEY", "test-mode-key")
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(pipeline, "get_audio_duration_ms", lambda _path: AUDIO_DURATION_MS)
    monkeypatch.setattr(superwhisper_client, "_read_mode_name", lambda _key: "Podcast")
    return SimpleNamespace(recordings=recordings, audio=audio, tmp=tmp_path)


# --- contract / happy path ----------------------------------------------------


def test_clean_text_without_category_header_succeeds(env, monkeypatch):
    """Podcast mode emits cleaned text; contract_check=None means non-empty wins."""
    commands: list[list[str]] = []
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, _meta(), commands))

    result = analyze_file(env.audio, mode_key="podcast", lock_path=env.tmp / "shared.lock")

    assert result.analysis == CLEAN_TEXT
    assert result.transcript == RAW_TRANSCRIPT
    assert result.duration_ms == AUDIO_DURATION_MS
    assert result.segments == (Segment(0.0, 1.25, "word one"), Segment(1.25, 2.5, "word two"))
    assert result.recording_dir == env.recordings / "session-1"
    assert result.mode_name == "Podcast"

    # The supplied mode_key is used; the daemon's configured key is never read.
    assert ["open", "superwhisper://mode?key=podcast"] in commands
    assert not any("test-mode-key" in " ".join(cmd) for cmd in commands)
    assert any(cmd[-1:] == ["Superwhisper"] for cmd in commands)


def test_custom_contract_check_can_accept_text(env, monkeypatch):
    commands: list[list[str]] = []
    meta = _meta(llmResult="CLEAN: accepted by the check")
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, meta, commands))

    result = analyze_file(
        env.audio,
        mode_key="podcast",
        contract_check=lambda text: text.startswith("CLEAN:"),
        lock_path=env.tmp / "shared.lock",
    )

    assert result.analysis == "CLEAN: accepted by the check"


def test_custom_contract_check_rejection_is_permanent(env, monkeypatch):
    commands: list[list[str]] = []
    meta = _meta(llmResult="no marker here")
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, meta, commands))

    with pytest.raises(pipeline.PermanentFileError, match="contract_check"):
        analyze_file(
            env.audio,
            mode_key="podcast",
            contract_check=lambda text: "MARKER" in text,
            lock_path=env.tmp / "shared.lock",
        )


def test_blank_llm_result_is_rejected_by_default(env, monkeypatch):
    commands: list[list[str]] = []
    meta = _meta(llmResult="   \n")
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, meta, commands))

    with pytest.raises(pipeline.PermanentFileError):
        analyze_file(env.audio, mode_key="podcast", lock_path=env.tmp / "shared.lock")


def test_timeout_budget_is_honoured(env, monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, _meta(), commands))

    with pytest.raises(TimeoutError):
        analyze_file(env.audio, mode_key="podcast", timeout=0, lock_path=env.tmp / "shared.lock")


# --- provenance / side effects ------------------------------------------------


def test_result_recording_is_marked_consumed(env, monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, _meta(), commands))

    analyze_file(env.audio, mode_key="podcast", lock_path=env.tmp / "shared.lock")

    assert (env.recordings / "session-1" / pipeline.CONSUMED_SENTINEL).exists()


def test_recordings_dir_override_is_used(env, monkeypatch):
    custom = env.tmp / "other-recordings"
    custom.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(custom, _meta(), commands))

    result = analyze_file(
        env.audio,
        mode_key="podcast",
        recordings_dir=custom,
        lock_path=env.tmp / "shared.lock",
    )

    assert result.recording_dir == custom / "session-1"


def test_never_writes_daemon_heartbeat(env, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(pipeline, "write_heartbeat", lambda *a, **k: calls.append((a, k)))
    commands: list[list[str]] = []
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, _meta(), commands))

    analyze_file(env.audio, mode_key="podcast", lock_path=env.tmp / "shared.lock")

    assert calls == [], "analyze_file must not refresh the daemon's liveness file"


def test_hold_does_not_touch_daemon_state_file(env, monkeypatch):
    state = env.tmp / "daemon-state.json"
    monkeypatch.setattr(pipeline, "STATE_FILE", str(state))
    commands: list[list[str]] = []
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, _meta(), commands))

    analyze_file(env.audio, mode_key="podcast", lock_path=env.tmp / "shared.lock")

    assert not state.exists(), "analyze_file must not write the daemon's state file"


def test_malformed_segments_degrade_to_valid_entries(env, monkeypatch):
    commands: list[list[str]] = []
    meta = _meta(
        segments=[
            {"start": 0, "end": 1, "text": "ok"},
            {"text": "no times"},
            "junk",
            {"start": "nan-ish", "end": 1, "text": "bad"},
        ]
    )
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, meta, commands))

    result = analyze_file(env.audio, mode_key="podcast", lock_path=env.tmp / "shared.lock")

    assert result.segments == (Segment(0.0, 1.0, "ok"),)


def test_missing_audio_raises_before_any_handoff(env, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: calls.append(a))

    with pytest.raises(FileNotFoundError):
        analyze_file(env.tmp / "missing.m4a", mode_key="podcast", lock_path=env.tmp / "shared.lock")

    assert calls == []


# --- shared lock --------------------------------------------------------------


def test_lock_is_held_across_switch_handoff_and_wait(env, monkeypatch):
    lock_path = env.tmp / "shared.lock"
    probes: list[tuple[str, bool]] = []

    real_switch = pipeline.switch_superwhisper_mode
    real_handoff = pipeline.handoff_to_superwhisper
    real_wait = pipeline.wait_for_superwhisper_result

    def probe(name, fn):
        def wrapper(*args, **kwargs):
            probes.append((name, _lock_is_held(lock_path)))
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(pipeline, "switch_superwhisper_mode", probe("switch", real_switch))
    monkeypatch.setattr(pipeline, "handoff_to_superwhisper", probe("handoff", real_handoff))
    monkeypatch.setattr(pipeline, "wait_for_superwhisper_result", probe("wait", real_wait))
    commands: list[list[str]] = []
    monkeypatch.setattr(pipeline.subprocess, "run", _fake_run(env.recordings, _meta(), commands))

    analyze_file(env.audio, mode_key="podcast", lock_path=lock_path)

    assert probes == [("switch", True), ("handoff", True), ("wait", True)]
    assert _lock_is_held(lock_path) is False, "lock must be released when analyze_file returns"


# --- import hygiene -----------------------------------------------------------


def test_importing_module_does_not_import_pipeline_or_config():
    """The new module stays import-light: pipeline/config load lazily."""
    repo_root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; import superwhisper_client; "
        "assert 'pipeline' not in sys.modules, 'pipeline imported eagerly'; "
        "assert 'config' not in sys.modules, 'config imported eagerly'"
    )
    subprocess.run([sys.executable, "-c", code], cwd=repo_root, check=True, timeout=30)
