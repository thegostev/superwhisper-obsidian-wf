"""System-domain absence supervisor tests (LAG-906, spec SUP-*).

The 2026-10-30 → 11-03 outage (3.7 days) happened because both the transcriber
and its watchdog are per-user LaunchAgents: after a reboot to the login window
neither job exists, and nothing in the system can observe "the job was never
loaded". The supervisor is a LaunchDaemon that observes the GUI domain and
records/announces its absence; it never tries to run Superwhisper headless.

These tests pin the four states (SUP-2), the two-consecutive-checks alert
threshold (SUP-3), alert-once-per-episode semantics (SUP-4), status-file
persistence (SUP-5), and the plist properties whose absence would make the
daemon useless or noisy (SUP-6).
"""

import json
import os
import plistlib
from pathlib import Path

import pytest

import supervisor

PROJECT_DIR = Path(__file__).parents[2]
TEMPLATE = PROJECT_DIR / "docs" / "launchd" / "com.alex.transcriber.supervisor.plist.template"

CONSOLE_OUTPUT = """<dictionary> {
  Name : alex
  UID : 501
}
"""

LOGIN_WINDOW_OUTPUT = """<dictionary> {
  Name : loginwindow
  UID : 0
}
"""

SETUP_USER_OUTPUT = """<dictionary> {
  Name : _mbsetupuser
  UID : 0
}
"""


class TestParseConsoleUser:
    def test_real_console_user_is_parsed(self):
        assert supervisor.parse_console_user(CONSOLE_OUTPUT) == {"name": "alex", "uid": 501}

    def test_login_window_is_not_a_console_user(self):
        assert supervisor.parse_console_user(LOGIN_WINDOW_OUTPUT) is None

    def test_setup_user_is_not_a_console_user(self):
        assert supervisor.parse_console_user(SETUP_USER_OUTPUT) is None

    def test_uid_zero_is_not_a_console_user(self):
        assert supervisor.parse_console_user("<dictionary> {\n  Name : root\n  UID : 0\n}\n") is None

    def test_empty_or_malformed_output_is_not_a_console_user(self):
        assert supervisor.parse_console_user("") is None
        assert supervisor.parse_console_user("<dictionary> {\n}\n") is None


class TestClassify:
    USER = {"name": "alex", "uid": 501}

    def test_no_console_user(self):
        assert supervisor.classify(None, False, None) == "NO_CONSOLE_USER"

    def test_job_absent(self):
        assert supervisor.classify(self.USER, False, 10.0) == "ABSENT"

    def test_loaded_but_heartbeat_stale(self):
        assert supervisor.classify(self.USER, True, 601.0) == "LOADED_STALE"

    def test_loaded_with_no_heartbeat_is_stale(self):
        assert supervisor.classify(self.USER, True, None) == "LOADED_STALE"

    def test_loaded_with_fresh_heartbeat_is_healthy(self):
        assert supervisor.classify(self.USER, True, 599.0) == "HEALTHY"

    def test_max_age_boundary_is_healthy(self):
        """`>` not `>=`: exactly 600 s old is still inside the threshold."""
        assert supervisor.classify(self.USER, True, 600.0) == "HEALTHY"


class TestAbsenceDecision:
    @staticmethod
    def _decide(previous, state, now=1000.0):
        return supervisor.decide_absence(previous, state, now)

    def test_first_absence_does_not_alert(self):
        decision = self._decide({}, "ABSENT")
        assert decision["notify"] is False
        assert decision["consecutive_absence_checks"] == 1
        assert decision["alerting"] is False

    def test_second_consecutive_absence_alerts(self):
        first = self._decide({}, "ABSENT")
        second = self._decide(first, "ABSENT")
        assert second["notify"] is True
        assert second["consecutive_absence_checks"] == 2
        assert second["alerting"] is True
        assert second["last_alert_at"] == 1000.0

    def test_mixed_absence_states_still_count_as_consecutive(self):
        """A boot that starts at the login window and ends ABSENT is one episode."""
        first = self._decide({}, "NO_CONSOLE_USER")
        second = self._decide(first, "ABSENT")
        assert second["notify"] is True

    def test_third_absence_does_not_re_alert(self):
        second = self._decide(self._decide({}, "ABSENT"), "ABSENT")
        third = self._decide(second, "ABSENT")
        assert third["notify"] is False
        assert third["alerting"] is True
        assert third["last_alert_at"] == second["last_alert_at"]

    def test_healthy_clears_the_episode(self):
        alerted = self._decide(self._decide({}, "ABSENT"), "ABSENT")
        healthy = self._decide(alerted, "HEALTHY")
        assert healthy["notify"] is False
        assert healthy["consecutive_absence_checks"] == 0
        assert healthy["alerting"] is False

    def test_loaded_stale_breaks_the_absence_streak(self):
        """A loaded job is not 'absent'; the per-user watchdog owns staleness."""
        first = self._decide({}, "ABSENT")
        stale = self._decide(first, "LOADED_STALE")
        assert stale["consecutive_absence_checks"] == 0
        assert stale["notify"] is False

    def test_a_new_episode_alerts_again_after_recovery(self):
        alerted = self._decide(self._decide({}, "ABSENT"), "ABSENT")
        recovered = self._decide(alerted, "HEALTHY")
        first_again = self._decide(recovered, "NO_CONSOLE_USER")
        second_again = self._decide(first_again, "NO_CONSOLE_USER")
        assert second_again["notify"] is True


class TestHeartbeatAge:
    def test_missing_heartbeat_is_none(self, tmp_path):
        assert supervisor.heartbeat_age(tmp_path / "missing.json", now=1000.0) is None

    def test_age_is_now_minus_mtime(self, tmp_path):
        heartbeat = tmp_path / "heartbeat.json"
        heartbeat.write_text("{}")
        os.utime(heartbeat, (1000.0, 940.0))
        assert supervisor.heartbeat_age(heartbeat, now=1000.0) == pytest.approx(60.0)

    def test_future_mtime_is_clamped_to_zero(self, tmp_path):
        heartbeat = tmp_path / "heartbeat.json"
        heartbeat.write_text("{}")
        os.utime(heartbeat, (1100.0, 1100.0))
        assert supervisor.heartbeat_age(heartbeat, now=1000.0) == 0.0


class TestStateFile:
    def test_load_state_tolerates_missing_and_corrupt_files(self, tmp_path):
        assert supervisor.load_state(tmp_path / "missing.json") == {}
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not json")
        assert supervisor.load_state(corrupt) == {}

    def test_write_state_is_atomic_and_round_trips(self, tmp_path):
        path = tmp_path / "state.json"
        assert supervisor.write_state(path, {"schema": 1, "state": "HEALTHY"}) is True
        assert json.loads(path.read_text()) == {"schema": 1, "state": "HEALTHY"}
        assert not (tmp_path / "state.json.tmp").exists()


class TestRunTick:
    def _run(self, monkeypatch, tmp_path, states, *, dry_run=False):
        """Drive main() through a sequence of observations."""
        monkeypatch.setattr(supervisor, "read_console_user", lambda: (states[0], None))
        monkeypatch.setattr(supervisor, "probe_job_loaded", lambda uid, label: states[1])
        monkeypatch.setattr(supervisor, "heartbeat_age", lambda path, now=None: states[2])
        sent = []
        monkeypatch.setattr(supervisor, "notify", lambda title, message: sent.append((title, message)) or True)
        args = [
            "--heartbeat",
            str(tmp_path / "heartbeat.json"),
            "--state",
            str(tmp_path / "supervisor.json"),
            "--label",
            "com.alex.transcriber",
        ]
        if dry_run:
            args.append("--dry-run")
        return supervisor.main(args), sent

    def test_two_absences_write_status_and_alert_once(self, monkeypatch, tmp_path):
        code, sent = self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, False, 10.0))
        assert code == 0
        assert sent == []
        status = json.loads((tmp_path / "supervisor.json").read_text())
        assert status["state"] == "ABSENT"
        assert status["console_user"] == "alex"
        assert status["consecutive_absence_checks"] == 1

        code, sent = self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, False, 10.0))
        assert code == 0
        assert len(sent) == 1
        status = json.loads((tmp_path / "supervisor.json").read_text())
        assert status["alerting"] is True

        code, sent = self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, False, 10.0))
        assert code == 0
        assert sent == []

    def test_healthy_tick_clears_alert(self, monkeypatch, tmp_path):
        self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, False, 10.0))
        self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, False, 10.0))
        code, sent = self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, True, 10.0))
        assert code == 0
        assert sent == []
        status = json.loads((tmp_path / "supervisor.json").read_text())
        assert status["state"] == "HEALTHY"
        assert status["alerting"] is False

    def test_dry_run_neither_notifies_nor_writes(self, monkeypatch, tmp_path):
        self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, False, 10.0), dry_run=True)
        code, sent = self._run(monkeypatch, tmp_path, ({"name": "alex", "uid": 501}, False, 10.0), dry_run=True)
        assert code == 0
        assert sent == []
        assert not (tmp_path / "supervisor.json").exists()

    def test_probe_failure_is_an_internal_error_without_alert(self, monkeypatch, tmp_path):
        monkeypatch.setattr(supervisor, "read_console_user", lambda: (None, "scutil failed"))
        sent = []
        monkeypatch.setattr(supervisor, "notify", lambda title, message: sent.append((title, message)) or True)
        code = supervisor.main(["--heartbeat", str(tmp_path / "hb.json"), "--state", str(tmp_path / "state.json")])
        assert code == 3
        assert sent == []
        assert not (tmp_path / "state.json").exists()

    def test_no_console_user_status_has_no_job_probe(self, monkeypatch, tmp_path):
        code, sent = self._run(monkeypatch, tmp_path, (None, False, None))
        assert code == 0
        assert sent == []
        status = json.loads((tmp_path / "supervisor.json").read_text())
        assert status["state"] == "NO_CONSOLE_USER"
        assert status["console_user"] is None
        assert status["job_loaded"] is None


class TestSupervisorTemplate:
    @pytest.fixture
    def template(self):
        return plistlib.loads(TEMPLATE.read_bytes())

    def test_label(self, template):
        assert template["Label"] == "com.alex.transcriber.supervisor"

    def test_runs_at_load_with_interval_and_no_keepalive(self, template):
        """RunAtLoad + StartInterval; KeepAlive on a run-once script is a loop."""
        assert template["RunAtLoad"] is True
        assert template["StartInterval"] == 60
        assert "KeepAlive" not in template

    def test_runs_under_system_python(self, template):
        args = [str(a) for a in template["ProgramArguments"]]
        assert args[0] == "/usr/bin/python3"
        assert any(a.endswith("supervisor.py") for a in args)

    def test_passes_label_heartbeat_and_state(self, template):
        args = [str(a) for a in template["ProgramArguments"]]
        assert "--label" in args
        assert "--heartbeat" in args
        assert "--state" in args
        assert "com.alex.transcriber" in args
        assert any(a.endswith(".superwhisper_transcriber_heartbeat.json") for a in args)
        assert any(a.endswith(".superwhisper_transcriber_supervisor.json") for a in args)

    def test_uses_placeholders_not_home_paths(self):
        blob = TEMPLATE.read_text(encoding="utf-8")
        assert "__REPO__" in blob
        assert "__HOME__" in blob
        assert "/Users/" not in blob

    def test_logs_are_durable(self, template):
        log = template["StandardOutPath"]
        assert "superwhisper-transcriber" in log
        assert template["StandardErrorPath"] == log
