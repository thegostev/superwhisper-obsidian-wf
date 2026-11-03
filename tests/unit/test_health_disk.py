"""Disk telemetry and watchdog thresholds (LAG-910, spec DS-6..DS-9).

The 2026-10-30 incident is encoded here: the watchdog had no view of disk
pressure, and when the volume filled it could not even persist its own state
(``Errno 28``), so the alerts it computed never reached anyone. These tests pin
the schema-v2 fields, the warn/critical ladder, the 1 GiB hysteresis and the
hourly notification cooldown — all on synthetic heartbeats, including a v1
fixture that must keep working without its disk fields.
"""

import pytest

import health_check

GIB = 1024**3
LAUNCHCTL_OK = {"label": "com.alex.transcriber", "pid": 8078, "last_exit_status": 0}


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
    }
    payload.update(overrides)
    return payload


class TestSchemaCompatibility:
    def test_v1_heartbeat_still_yields_a_verdict(self):
        v1 = {
            "schema": 1,
            "pid": 8078,
            "phase": "scanning",
            "writer": "daemon",
            "state_complete": 300,
            "failed_permanent": 0,
        }
        report = health_check.assess_health(v1, 1.0, LAUNCHCTL_OK, 300)
        assert report["verdict"] == "healthy"
        assert report["reason"] is None
        assert health_check.disk_free_gb_from_payload(v1) is None

    def test_v2_heartbeat_is_accepted(self):
        report = health_check.assess_health(make_heartbeat(), 1.0, LAUNCHCTL_OK, 300)
        assert report["verdict"] == "healthy"

    def test_unknown_schema_still_unhealthy(self):
        report = health_check.assess_health(make_heartbeat(schema=3), 1.0, LAUNCHCTL_OK, 300)
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "heartbeat_schema_unknown"

    def test_disk_gb_is_derived_from_bytes_when_absent(self):
        payload = make_heartbeat(disk_free_gb=None, disk_free_bytes=4 * GIB)
        assert health_check.disk_free_gb_from_payload(payload) == pytest.approx(4.0)


class TestDiskLevel:
    def test_four_gib_is_warning(self):
        assert health_check.disk_level(4.0) == "warn"

    def test_six_gib_is_clear(self):
        assert health_check.disk_level(6.0) is None

    def test_just_under_two_gib_is_critical(self):
        assert health_check.disk_level(1.9) == "critical"

    def test_hysteresis_holds_a_warning_between_five_and_six(self):
        assert health_check.disk_level(4.9) == "warn"
        assert health_check.disk_level(5.1, current="warn") == "warn"
        assert health_check.disk_level(5.99, current="warn") == "warn"
        assert health_check.disk_level(6.0, current="warn") is None

    def test_critical_steps_down_through_hysteresis(self):
        assert health_check.disk_level(2.5, current="critical") == "critical"
        assert health_check.disk_level(3.5, current="critical") == "warn"

    def test_missing_disk_fields_are_unknown_not_an_error(self):
        assert health_check.disk_level(None) is None
        assert health_check.disk_level(None, current="warn") == "warn"


class TestDiskTick:
    def test_four_gib_notifies_once_then_holds(self):
        state = {}
        assert health_check.disk_tick(state, make_heartbeat(disk_free_gb=4.0), 1000.0) == (
            "notify_disk",
            health_check.DISK_WARN_ALERT,
        )
        assert health_check.disk_tick(state, make_heartbeat(disk_free_gb=4.0), 1010.0) == (
            None,
            health_check.DISK_WARN_ALERT,
        )
        assert state["disk_level"] == "warn"

    def test_six_gib_produces_no_alert(self):
        assert health_check.disk_tick({}, make_heartbeat(disk_free_gb=6.0), 1000.0) == (None, None)

    def test_just_under_two_gib_is_critical(self):
        action, alert = health_check.disk_tick({}, make_heartbeat(disk_free_gb=1.9), 1000.0)
        assert (action, alert) == ("notify_disk", health_check.DISK_CRITICAL_ALERT)

    def test_escalation_to_critical_notifies_inside_the_cooldown(self):
        state = {}
        health_check.disk_tick(state, make_heartbeat(disk_free_gb=4.0), 1000.0)
        action, alert = health_check.disk_tick(state, make_heartbeat(disk_free_gb=1.5), 1010.0)
        assert (action, alert) == ("notify_disk", health_check.DISK_CRITICAL_ALERT)

    def test_reminder_waits_for_the_cooldown(self):
        state = {}
        health_check.disk_tick(state, make_heartbeat(disk_free_gb=4.0), 1000.0)
        assert health_check.disk_tick(state, make_heartbeat(disk_free_gb=4.0), 2000.0)[0] is None
        assert health_check.disk_tick(state, make_heartbeat(disk_free_gb=4.0), 4600.0)[0] == "notify_disk"

    def test_v1_payload_never_alerts(self):
        v1 = make_heartbeat(schema=1)
        v1.pop("disk_free_bytes")
        v1.pop("disk_free_gb")
        v1.pop("recordings_bytes")
        assert health_check.disk_tick({}, v1, 1000.0) == (None, None)


class TestDiskNotification:
    def test_message_names_bytes_threshold_and_recordings_store(self):
        report = {
            "heartbeat": make_heartbeat(
                disk_free_bytes=int(1.5 * GIB),
                disk_free_gb=1.5,
                recordings_bytes=int(7 * GIB),
            )
        }
        decision = {"alert": health_check.DISK_CRITICAL_ALERT, "state": {}}
        message = health_check._disk_message(report, decision)
        assert "1.5 GiB" in message
        assert "2.0 GiB" in message
        assert "recordings store 7.0 GiB" in message

    def test_warning_message_names_the_warn_threshold(self):
        report = {"heartbeat": make_heartbeat(disk_free_gb=4.0, disk_free_bytes=4 * GIB)}
        decision = {"alert": health_check.DISK_WARN_ALERT, "state": {}}
        message = health_check._disk_message(report, decision)
        assert "4.0 GiB" in message and "5.0 GiB" in message


class TestHealthyTickWiring:
    def test_decision_on_healthy_low_disk_notifies(self):
        wd_state = {
            "schema": 1,
            "last_run_at": 900.0,
            "consecutive_failures": 0,
            "escalated": False,
            "throughput": {"state_complete": 300, "failed_permanent": 0, "phase": "scanning", "since_at": 900.0},
        }
        report = health_check.assess_health(
            make_heartbeat(disk_free_gb=4.0, disk_free_bytes=4 * GIB), 1.0, LAUNCHCTL_OK, 300
        )
        decision = health_check.decide_action(report, wd_state, 1000.0, max_age=300)
        assert decision["action"] == "notify_disk"
        assert decision["notify"] is True
        assert decision["alert"] == health_check.DISK_WARN_ALERT
