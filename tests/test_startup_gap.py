"""Startup-gap reporting tests for LAG-908.

The previous heartbeat must be measured before the daemon's first write replaces
it. Notifications carry operator context and are deduplicated through the
persistent pipeline state so a launchd restart loop cannot page repeatedly.
"""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import auto_transcribe
import pipeline

NOW = 1_800_000_000.0
TIMESTAMP = datetime.fromtimestamp(NOW, tz=timezone.utc).strftime(pipeline.HEARTBEAT_TIME_FORMAT)
GIB = 1024**3


@pytest.fixture
def startup_environment(tmp_path, monkeypatch):
    heartbeat_path = tmp_path / "heartbeat.json"
    state_path = tmp_path / "state.json"
    watch_path = tmp_path / "watch"
    watch_path.mkdir()
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(heartbeat_path))
    monkeypatch.setattr(pipeline, "STATE_FILE", str(state_path))
    monkeypatch.setattr(auto_transcribe, "WATCH_FOLDER", str(watch_path))

    disk = {
        "min_free_bytes": int(12.5 * GIB),
        "min_free_gb": 12.5,
        "recordings_bytes": 100,
        "volumes": {},
    }
    disk_probe = Mock(return_value=disk)
    notifier = Mock(return_value=True)
    monkeypatch.setattr(pipeline, "disk_snapshot", disk_probe)
    monkeypatch.setattr(pipeline, "_notify", notifier)

    pipeline._heartbeat_last_gap_s = None
    pipeline._heartbeat_last_write = 0.0
    pipeline._heartbeat_started_at = None
    yield SimpleNamespace(
        heartbeat=heartbeat_path,
        state=state_path,
        watch=watch_path,
        notifier=notifier,
        disk_probe=disk_probe,
    )
    pipeline._heartbeat_last_gap_s = None
    pipeline._heartbeat_last_write = 0.0
    pipeline._heartbeat_started_at = None


def write_previous_heartbeat(path, *, age_s, contents=None):
    if contents is None:
        contents = json.dumps(
            {
                "schema": 2,
                "updated_at": datetime.fromtimestamp(NOW - age_s, tz=timezone.utc).strftime(
                    pipeline.HEARTBEAT_TIME_FORMAT
                ),
            }
        )
    path.write_text(contents, encoding="utf-8")
    os.utime(path, (NOW - age_s, NOW - age_s))


def state_with_pending_files():
    return {
        "processed": {
            "/complete.m4a": {"status": "complete"},
            "/permanent.m4a": {"status": "failed_permanent"},
            "/retry.m4a": {"status": "failed_retry"},
            "/interrupted.m4a": {"status": "in_progress"},
        }
    }


def run_startup(state, *, now=NOW):
    """Exercise the same measure → first write → report sequence as main()."""
    gap_s = pipeline.measure_startup_gap(now=now)
    pipeline.write_heartbeat("starting", force=True)
    pending_count, pending_error = auto_transcribe._startup_pending_context(state, gap_s)
    notified = pipeline.report_startup_gap(
        state,
        gap_s,
        pending_count=pending_count,
        pending_error=pending_error,
        now=now,
    )
    return gap_s, notified


def startup_log_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


def test_four_hour_gap_logs_and_notifies_once_with_operator_context(startup_environment, capsys):
    write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600)
    state = state_with_pending_files()

    gap_s, notified = run_startup(state)

    assert gap_s == pytest.approx(4 * 3600)
    assert notified is True
    startup_environment.notifier.assert_called_once()
    message, title = startup_environment.notifier.call_args.args
    assert title == "Transcriber startup gap"
    assert "4:00:00" in message
    assert "2 pending" in message
    assert "12.5 GiB" in message

    logs = startup_log_lines(capsys)
    assert len(logs) == 1
    assert logs[0] == {
        "event": "startup_gap",
        "timestamp": TIMESTAMP,
        "gap_s": pytest.approx(4 * 3600),
        "pending_count": 2,
        "disk_free_bytes": int(12.5 * GIB),
        "disk_free_gb": 12.5,
        "pending_error": None,
        "disk_error": None,
        "notification_error": None,
    }
    payload = json.loads(startup_environment.heartbeat.read_text(encoding="utf-8"))
    assert payload["last_gap_s"] == pytest.approx(4 * 3600)


def test_fresh_heartbeat_is_silent_and_persists_small_gap(startup_environment, capsys):
    write_previous_heartbeat(startup_environment.heartbeat, age_s=300)

    gap_s, notified = run_startup(state_with_pending_files())

    assert gap_s == pytest.approx(300)
    assert notified is False
    startup_environment.notifier.assert_not_called()
    assert startup_log_lines(capsys) == []
    payload = json.loads(startup_environment.heartbeat.read_text(encoding="utf-8"))
    assert payload["last_gap_s"] == pytest.approx(300)


@pytest.mark.parametrize(
    ("contents", "reason"),
    [(None, "heartbeat_missing"), ("{not-json", "heartbeat_unreadable"), ("[]", "heartbeat_unreadable")],
)
def test_missing_or_malformed_heartbeat_logs_unknown_without_crashing(startup_environment, capsys, contents, reason):
    if contents is not None:
        write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600, contents=contents)

    gap_s, notified = run_startup(state_with_pending_files())

    assert gap_s is None
    assert notified is False
    startup_environment.notifier.assert_not_called()
    logs = startup_log_lines(capsys)
    assert logs == [{"event": "startup_gap", "timestamp": TIMESTAMP, "gap_s": None, "reason": reason}]
    payload = json.loads(startup_environment.heartbeat.read_text(encoding="utf-8"))
    assert payload["last_gap_s"] is None


def test_notification_cooldown_survives_restart_loop(startup_environment):
    pipeline.save_state(state_with_pending_files())

    for restart in range(2):
        # Recreate the stale previous heartbeat so this test exercises the
        # persisted cooldown rather than relying on the fresh first heartbeat.
        write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600)
        restarted_state = pipeline.load_state()
        run_startup(restarted_state, now=NOW + restart * 10)

    assert startup_environment.notifier.call_count == 1
    saved = json.loads(startup_environment.state.read_text(encoding="utf-8"))
    assert saved["startup_gap"]["last_notified_epoch"] == NOW


def test_notification_failure_does_not_abort_startup(startup_environment, capsys):
    write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600)
    startup_environment.notifier.side_effect = OSError("no Notification Center")

    gap_s, notified = run_startup(state_with_pending_files())

    assert gap_s == pytest.approx(4 * 3600)
    assert notified is False
    logs = startup_log_lines(capsys)
    assert len(logs) == 1
    assert logs[0]["event"] == "startup_gap"
    assert logs[0]["timestamp"] == TIMESTAMP
    assert "no Notification Center" in logs[0]["notification_error"]


def test_failed_notification_result_is_logged(startup_environment, capsys):
    write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600)
    startup_environment.notifier.return_value = False

    _, notified = run_startup(state_with_pending_files())

    assert notified is False
    logs = startup_log_lines(capsys)
    assert len(logs) == 1
    assert logs[0]["event"] == "startup_gap"
    assert logs[0]["notification_error"] == "notifier returned false"


def test_disk_probe_failure_degrades_to_unknown_without_aborting(startup_environment, capsys):
    write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600)
    startup_environment.disk_probe.side_effect = OSError("disk unavailable")

    _, notified = run_startup(state_with_pending_files())

    assert notified is True
    message, _ = startup_environment.notifier.call_args.args
    assert "disk free unknown" in message
    logs = startup_log_lines(capsys)
    assert logs[0]["event"] == "heartbeat_disk_failed"
    assert logs[1]["event"] == "startup_gap"
    assert logs[1]["disk_error"] == "disk unavailable"
    assert logs[1]["disk_free_bytes"] is None
    assert logs[1]["disk_free_gb"] is None
    assert sum(log["event"] == "startup_gap" for log in logs) == 1


def test_pending_count_failure_degrades_to_unknown_in_single_gap_event(startup_environment, capsys, monkeypatch):
    write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600)

    def fail_pending_count(_state):
        raise OSError("watch folder unavailable")

    monkeypatch.setattr(auto_transcribe, "_pending_file_count", fail_pending_count)

    _, notified = run_startup(state_with_pending_files())

    assert notified is True
    message, _ = startup_environment.notifier.call_args.args
    assert "unknown; disk free" in message
    logs = startup_log_lines(capsys)
    assert len(logs) == 1
    assert logs[0]["event"] == "startup_gap"
    assert logs[0]["pending_count"] is None
    assert logs[0]["pending_error"] == "watch folder unavailable"


def test_pending_count_includes_unseen_audio_on_disk(startup_environment, capsys):
    date_dir = startup_environment.watch / auto_transcribe._now_local().strftime("%Y-%m-%d")
    date_dir.mkdir()
    (date_dir / "unseen.m4a").write_bytes(b"new recording")
    complete = date_dir / "complete.m4a"
    complete.write_bytes(b"already processed")
    (date_dir / ".hidden.m4a").write_bytes(b"ignored")
    (date_dir / "unfinished.tmp.m4a").write_bytes(b"ignored")
    state = state_with_pending_files()
    state["processed"][str(complete)] = {"status": "complete"}
    write_previous_heartbeat(startup_environment.heartbeat, age_s=4 * 3600)

    run_startup(state)

    message, _ = startup_environment.notifier.call_args.args
    assert "3 pending" in message
    logs = startup_log_lines(capsys)
    assert logs[0]["pending_count"] == 3


def test_gap_uses_heartbeat_mtime_not_embedded_timestamp(startup_environment):
    write_previous_heartbeat(
        startup_environment.heartbeat,
        age_s=60,
        contents=json.dumps({"schema": 2, "updated_at": "1999-01-01T00:00:00Z"}),
    )

    assert pipeline.measure_startup_gap(now=NOW) == pytest.approx(60)


def test_daemon_measures_before_first_write_and_reports_after_state_load(monkeypatch):
    events = []

    class StopAfterReportError(Exception):
        pass

    monkeypatch.setattr(auto_transcribe, "set_heartbeat_writer", lambda _writer: None)
    monkeypatch.setattr(
        auto_transcribe,
        "measure_startup_gap",
        lambda: events.append("measure") or 4 * 3600.0,
    )
    monkeypatch.setattr(
        auto_transcribe,
        "write_heartbeat",
        lambda *_args, **_kwargs: events.append("write"),
    )
    monkeypatch.setattr(
        auto_transcribe,
        "load_state",
        lambda: events.append("load") or state_with_pending_files(),
    )

    monkeypatch.setattr(auto_transcribe, "_pending_file_count", lambda _state: 2)

    def report(state, gap_s, **context):
        assert state["processed"]
        assert gap_s == 4 * 3600.0
        assert context["pending_count"] == 2
        events.append("report")
        raise StopAfterReportError

    monkeypatch.setattr(auto_transcribe, "report_startup_gap", report)

    with pytest.raises(StopAfterReportError):
        auto_transcribe.main()

    assert events == ["measure", "write", "load", "report"]
