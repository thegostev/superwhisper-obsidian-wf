"""Pending-horizon telemetry and the 5-day alert (LAG-915, spec HB-16/WD-19).

``scan_days_back=7`` is a silent data-loss boundary: a recording that is not
drained before it ages out of discovery is never transcribed, and nothing said
so. These tests pin the heartbeat fields and the notification-only watchdog
ladder that warns at 5 days and pages critical at 6 — before the cutoff.
"""

import json
from datetime import datetime, timedelta

import pytest

import auto_transcribe
import health_check
import pipeline

LAUNCHCTL_OK = {"label": "com.alex.transcriber", "pid": 8078, "last_exit_status": 0}
GIB = 1024**3


def make_heartbeat(**overrides):
    payload = {
        "schema": 2,
        "pid": 8078,
        "phase": "scanning",
        "writer": "daemon",
        "cycle": 4321,
        "updated_at": "2026-11-03T12:00:00Z",
        "started_at": "2026-11-03T06:00:00Z",
        "state_complete": 300,
        "failed_permanent": 0,
        "disk_free_bytes": 6 * GIB,
        "disk_free_gb": 6.0,
        "recordings_bytes": 3 * GIB,
        "pending_count": 0,
        "oldest_pending_age_h": None,
    }
    payload.update(overrides)
    return payload


def healthy_wd_state(now=900.0):
    return {
        "schema": 1,
        "last_run_at": now,
        "consecutive_failures": 0,
        "escalated": False,
        "throughput": {"state_complete": 300, "failed_permanent": 0, "phase": "scanning", "since_at": now},
    }


class TestPendingHorizonFromPayload:
    def test_reads_both_fields(self):
        assert health_check.pending_horizon_from_payload(
            make_heartbeat(pending_count=3, oldest_pending_age_h=121.5)
        ) == (3, 121.5)

    def test_empty_queue_is_known(self):
        assert health_check.pending_horizon_from_payload(
            make_heartbeat(pending_count=0, oldest_pending_age_h=None)
        ) == (0, None)

    def test_absent_fields_are_unknown(self):
        payload = make_heartbeat()
        payload.pop("pending_count")
        payload.pop("oldest_pending_age_h")
        assert health_check.pending_horizon_from_payload(payload) == (None, None)
        assert health_check.pending_horizon_from_payload(None) == (None, None)

    def test_malformed_age_is_unknown(self):
        assert health_check.pending_horizon_from_payload(
            make_heartbeat(pending_count=2, oldest_pending_age_h="old")
        ) == (2, None)

    def test_negative_age_clamps_to_zero(self):
        assert health_check.pending_horizon_from_payload(
            make_heartbeat(pending_count=1, oldest_pending_age_h=-5.0)
        ) == (1, 0.0)


class TestPendingHorizonLevel:
    @pytest.mark.parametrize(
        ("age_h", "level"),
        [
            (100.0, None),
            (119.9, None),
            (120.0, "warn"),
            (121.0, "warn"),
            (143.9, "warn"),
            (144.0, "critical"),
            (145.0, "critical"),
        ],
    )
    def test_ladder_boundaries(self, age_h, level):
        assert health_check.pending_horizon_level(age_h) == level

    def test_unknown_age_keeps_the_raised_level(self):
        assert health_check.pending_horizon_level(None, current="warn") == "warn"


class TestPendingHorizonTick:
    def test_121_hours_warns(self):
        action, alert = health_check.pending_horizon_tick(
            {}, make_heartbeat(pending_count=2, oldest_pending_age_h=121.0), 1000.0
        )
        assert (action, alert) == ("notify_pending", health_check.PENDING_WARN_ALERT)

    def test_145_hours_is_critical(self):
        action, alert = health_check.pending_horizon_tick(
            {}, make_heartbeat(pending_count=2, oldest_pending_age_h=145.0), 1000.0
        )
        assert (action, alert) == ("notify_pending", health_check.PENDING_CRITICAL_ALERT)

    def test_100_hours_is_silent(self):
        assert health_check.pending_horizon_tick(
            {}, make_heartbeat(pending_count=2, oldest_pending_age_h=100.0), 1000.0
        ) == (None, None)

    def test_empty_queue_clears_a_raised_level(self):
        state = {"pending_level": "critical", "last_pending_notify_at": 900.0}
        assert health_check.pending_horizon_tick(
            state, make_heartbeat(pending_count=0, oldest_pending_age_h=None), 1000.0
        ) == (None, None)
        assert state["pending_level"] is None

    def test_zero_count_clears_even_beside_a_stale_age(self):
        state = {"pending_level": "warn", "last_pending_notify_at": 900.0}
        assert health_check.pending_horizon_tick(
            state, make_heartbeat(pending_count=0, oldest_pending_age_h=500.0), 1000.0
        ) == (None, None)
        assert state["pending_level"] is None

    def test_partial_sample_keeps_the_raised_level(self):
        state = {"pending_level": "warn", "last_pending_notify_at": 900.0}
        assert health_check.pending_horizon_tick(
            state, make_heartbeat(pending_count=3, oldest_pending_age_h=None), 1000.0
        ) == (None, health_check.PENDING_WARN_ALERT)
        assert state["pending_level"] == "warn"

    def test_completing_the_oldest_clears_while_younger_items_remain(self):
        state = {}
        health_check.pending_horizon_tick(state, make_heartbeat(pending_count=2, oldest_pending_age_h=145.0), 1000.0)
        assert health_check.pending_horizon_tick(
            state, make_heartbeat(pending_count=1, oldest_pending_age_h=10.0), 1060.0
        ) == (None, None)
        assert state["pending_level"] is None

    def test_raised_level_holds_through_the_cooldown_with_its_label(self):
        state = {}
        health_check.pending_horizon_tick(state, make_heartbeat(pending_count=2, oldest_pending_age_h=121.0), 1000.0)
        assert health_check.pending_horizon_tick(
            state, make_heartbeat(pending_count=2, oldest_pending_age_h=122.0), 1010.0
        ) == (None, health_check.PENDING_WARN_ALERT)
        assert (
            health_check.pending_horizon_tick(
                state, make_heartbeat(pending_count=2, oldest_pending_age_h=125.0), 4600.0
            )[0]
            == "notify_pending"
        )

    def test_warn_to_critical_pages_inside_the_cooldown(self):
        state = {}
        health_check.pending_horizon_tick(state, make_heartbeat(pending_count=2, oldest_pending_age_h=121.0), 1000.0)
        assert health_check.pending_horizon_tick(
            state, make_heartbeat(pending_count=2, oldest_pending_age_h=145.0), 1010.0
        ) == ("notify_pending", health_check.PENDING_CRITICAL_ALERT)

    def test_pre_hb16_heartbeat_never_raises_a_new_alert(self):
        payload = make_heartbeat()
        payload.pop("pending_count")
        payload.pop("oldest_pending_age_h")
        assert health_check.pending_horizon_tick({}, payload, 1000.0) == (None, None)


class TestPendingHorizonDecision:
    def test_healthy_tick_with_121_hours_notifies_without_restarting(self):
        report = health_check.assess_health(
            make_heartbeat(pending_count=3, oldest_pending_age_h=121.0), 1.0, LAUNCHCTL_OK, 300
        )
        decision = health_check.decide_action(report, healthy_wd_state(), 1000.0, max_age=300)
        assert decision["action"] == "notify_pending"
        assert decision["notify"] is True
        assert decision["alert"] == health_check.PENDING_WARN_ALERT
        assert decision["restart"] is False
        assert decision["state"]["escalated"] is False
        assert decision["state"]["consecutive_failures"] == 0

    def test_healthy_tick_with_145_hours_is_critical(self):
        report = health_check.assess_health(
            make_heartbeat(pending_count=3, oldest_pending_age_h=145.0), 1.0, LAUNCHCTL_OK, 300
        )
        decision = health_check.decide_action(report, healthy_wd_state(), 1000.0, max_age=300)
        assert decision["action"] == "notify_pending"
        assert decision["alert"] == health_check.PENDING_CRITICAL_ALERT

    def test_healthy_tick_with_100_hours_records_no_alert(self):
        report = health_check.assess_health(
            make_heartbeat(pending_count=3, oldest_pending_age_h=100.0), 1.0, LAUNCHCTL_OK, 300
        )
        decision = health_check.decide_action(report, healthy_wd_state(), 1000.0, max_age=300)
        assert decision["action"] == "record"
        assert decision["alert"] is None

    def test_completing_the_oldest_clears_the_alert_on_the_next_interval(self):
        report = health_check.assess_health(
            make_heartbeat(pending_count=2, oldest_pending_age_h=145.0), 1.0, LAUNCHCTL_OK, 300
        )
        first = health_check.decide_action(report, healthy_wd_state(), 1000.0, max_age=300)
        assert first["action"] == "notify_pending"

        later_state = dict(first["state"])
        later_state["last_run_at"] = 1000.0
        # The oldest completed; one younger recording remains pending.
        cleared = health_check.assess_health(
            make_heartbeat(pending_count=1, oldest_pending_age_h=10.0), 1.0, LAUNCHCTL_OK, 300
        )
        second = health_check.decide_action(cleared, later_state, 1060.0, max_age=300)
        assert second["action"] == "record"
        assert second["alert"] is None
        assert second["state"]["pending_level"] is None

    def test_thresholds_are_configuration_inputs(self):
        report = health_check.assess_health(
            make_heartbeat(pending_count=1, oldest_pending_age_h=30.0), 1.0, LAUNCHCTL_OK, 300
        )
        decision = health_check.decide_action(
            report, healthy_wd_state(), 1000.0, max_age=300, pending_warn_age_h=24.0, pending_critical_age_h=48.0
        )
        assert decision["action"] == "notify_pending"
        assert decision["alert"] == health_check.PENDING_WARN_ALERT


class TestPendingHorizonMessage:
    def test_warning_names_count_age_threshold_and_the_cutoff(self):
        report = {"heartbeat": make_heartbeat(pending_count=4, oldest_pending_age_h=121.5)}
        decision = {"alert": health_check.PENDING_WARN_ALERT, "state": {}}
        message = health_check._pending_horizon_message(report, decision)
        assert "4 recording(s)" in message
        assert "121.5h" in message
        assert "5 days (120h)" in message
        assert "7-day" in message or "scan_days_back" in message

    def test_critical_names_the_six_day_threshold(self):
        report = {"heartbeat": make_heartbeat(pending_count=1, oldest_pending_age_h=145.0)}
        decision = {"alert": health_check.PENDING_CRITICAL_ALERT, "state": {}}
        message = health_check._pending_horizon_message(report, decision)
        assert "critical" in message
        assert "6 days (144h)" in message

    def test_message_names_the_configured_threshold(self):
        report = {"heartbeat": make_heartbeat(pending_count=1, oldest_pending_age_h=30.0)}
        decision = {"alert": health_check.PENDING_WARN_ALERT, "state": {}}
        message = health_check._pending_horizon_message(report, decision, warn_age_h=24.0, critical_age_h=48.0)
        assert "1 day (24h)" in message


class TestDefaults:
    def test_pinned_thresholds_are_five_and_six_days(self):
        assert health_check.DEFAULT_PENDING_WARN_AGE_H == 120.0
        assert health_check.DEFAULT_PENDING_CRITICAL_AGE_H == 144.0


@pytest.fixture
def horizon_env(tmp_path, monkeypatch):
    """A heartbeat file plus a watch folder, with the writer caches reset."""
    heartbeat = tmp_path / "heartbeat.json"
    watch = tmp_path / "watch"
    watch.mkdir()
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(heartbeat))
    monkeypatch.setattr(auto_transcribe, "WATCH_FOLDER", str(watch))
    pipeline._heartbeat_last_write = 0.0
    pipeline._heartbeat_started_at = None
    pipeline._heartbeat_context = {"cycle": 0, "failed_permanent": 0, "state_complete": 0}
    pipeline._heartbeat_dependency_down = False
    pipeline._heartbeat_last_outcome = None
    pipeline._heartbeat_icloud_placeholders = None
    pipeline._heartbeat_icloud_downloads_requested = 0
    pipeline._heartbeat_last_gap_s = None
    pipeline._heartbeat_pending_count = None
    pipeline._heartbeat_oldest_pending_age_h = None
    yield heartbeat


class TestHeartbeatPendingFields:
    def test_payload_carries_count_and_age(self, horizon_env):
        pipeline.write_heartbeat("scanning", pending_count=3, oldest_pending_age_h=121.5, force=True)
        payload = json.loads(horizon_env.read_text(encoding="utf-8"))
        assert payload["pending_count"] == 3
        assert payload["oldest_pending_age_h"] == pytest.approx(121.5)

    def test_empty_queue_writes_zero_and_null(self, horizon_env):
        pipeline.write_heartbeat("scanning", pending_count=0, oldest_pending_age_h=None, force=True)
        payload = json.loads(horizon_env.read_text(encoding="utf-8"))
        assert payload["pending_count"] == 0
        assert payload["oldest_pending_age_h"] is None

    def test_throttled_write_keeps_the_fresh_sample_for_the_next_write(self, horizon_env):
        pipeline.write_heartbeat("scanning", pending_count=3, oldest_pending_age_h=121.5, force=True)
        throttled = pipeline.write_heartbeat("scanning", pending_count=5, oldest_pending_age_h=130.0)
        assert throttled is False
        pipeline.write_heartbeat("scanning", force=True)
        payload = json.loads(horizon_env.read_text(encoding="utf-8"))
        assert payload["pending_count"] == 5
        assert payload["oldest_pending_age_h"] == pytest.approx(130.0)


class TestPendingHorizonDerivation:
    def test_empty_queue_is_zero_and_null(self, horizon_env):
        assert auto_transcribe._pending_horizon({}) == {"pending_count": 0, "oldest_pending_age_h": None}

    def test_age_comes_from_state_timestamps_not_discovery_order(self, horizon_env, monkeypatch):
        # The oldest date dir (first in discovery order) holds a *fresh* entry;
        # the newest dir holds the old one. Folder order says the first file is
        # oldest; the timestamps say the opposite, and the timestamps win.
        old_dir = horizon_env.parent / "watch" / "2026-11-08"
        new_dir = horizon_env.parent / "watch" / "2026-11-14"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        (old_dir / "12-00-00.m4a").write_bytes(b"x")
        (new_dir / "00-01-00.m4a").write_bytes(b"x")
        monkeypatch.setattr(auto_transcribe, "discover_recent_folders", lambda *_a, **_k: [str(old_dir), str(new_dir)])
        now = datetime(2026, 11, 14, 12, 0, 0)
        state = {
            "processed": {
                str(old_dir / "12-00-00.m4a"): {
                    "status": "failed_retry",
                    "timestamp": (now - timedelta(hours=2)).isoformat(),
                },
                str(new_dir / "00-01-00.m4a"): {
                    "status": "in_progress",
                    "timestamp": (now - timedelta(hours=130)).isoformat(),
                },
            }
        }

        assert auto_transcribe._pending_horizon(state, now=now) == {
            "pending_count": 2,
            "oldest_pending_age_h": pytest.approx(130.0),
        }

    def test_falls_back_to_the_jpr_path_naming_without_a_state_record(self, horizon_env, monkeypatch):
        date_dir = horizon_env.parent / "watch" / "2026-11-11"
        date_dir.mkdir()
        (date_dir / "09-00-00.m4a").write_bytes(b"x")
        monkeypatch.setattr(auto_transcribe, "discover_recent_folders", lambda *_a, **_k: [str(date_dir)])

        # November is CET (UTC+1), JPR's fixed offset, so 09:00 parses to 09:00
        # Oslo wall time; three days and three hours before 12:00 on Nov 14.
        result = auto_transcribe._pending_horizon({}, now=datetime(2026, 11, 14, 12, 0, 0))

        assert result["pending_count"] == 1
        assert result["oldest_pending_age_h"] == pytest.approx(75.0)

    def test_scan_cycle_passes_the_sample_to_the_writer(self, horizon_env, monkeypatch):
        captured = []
        monkeypatch.setattr(auto_transcribe, "write_heartbeat", lambda *_a, **kw: captured.append(kw))
        monkeypatch.setattr(auto_transcribe, "discover_audio_files", lambda *_a, **_k: [])
        monkeypatch.setattr(auto_transcribe, "_icloud_placeholder_count", lambda: None)
        monkeypatch.setattr(auto_transcribe, "run_icloud_preflight", lambda: 0)
        state = {
            "processed": {
                "/watch/2026-11-09/09-00-00.m4a": {
                    "status": "failed_retry",
                    "timestamp": (pipeline._now_local() - timedelta(hours=125)).isoformat(),
                }
            }
        }

        auto_transcribe.run_scan_cycle(state, {}, 1)

        assert captured[0]["pending_count"] == 1
        assert captured[0]["oldest_pending_age_h"] == pytest.approx(125.0, abs=0.01)
