"""launchd plist template tests (ADR 0010, spec WD-1/WD-11, PF-1).

The committed templates are the deployment source of truth. These tests pin
the properties whose absence would reintroduce the incident: the daemon is
wrapped by the preflight and execs through it (PF-1); the watchdog runs under
/usr/bin/python3 with StartInterval and NO KeepAlive (WD-1/WD-11) — KeepAlive
on a run-once script is an instant infinite loop.
"""

import plistlib
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parents[2]
TEMPLATES = PROJECT_DIR / "docs" / "launchd"
DAEMON = TEMPLATES / "com.alex.transcriber.plist.template"
WATCHDOG = TEMPLATES / "com.alex.transcriber.watchdog.plist.template"


@pytest.fixture
def daemon_template():
    return plistlib.loads(DAEMON.read_bytes())


@pytest.fixture
def watchdog_template():
    return plistlib.loads(WATCHDOG.read_bytes())


class TestDaemonTemplate:
    def test_invokes_preflight_wrapper(self, daemon_template):
        args = [str(a) for a in daemon_template["ProgramArguments"]]
        assert any("preflight.sh" in a for a in args)
        assert any("venv/bin/python3" not in a for a in args)

    def test_wrapper_sources_secret_env_file(self, daemon_template):
        """Keys come from ~/.secrets/koding-transcriber.env — never hardcoded."""
        args = " ".join(str(a) for a in daemon_template["ProgramArguments"])
        assert "koding-transcriber.env" in args

    def test_keepalive_on_successful_exit_false(self, daemon_template):
        assert daemon_template["KeepAlive"] == {"SuccessfulExit": False}

    def test_throttle_interval_present(self, daemon_template):
        assert daemon_template["ThrottleInterval"] >= 10

    def test_uses_placeholders_not_home_paths(self, daemon_template):
        """Templates are machine-neutral: __REPO__/__HOME__ placeholders, and no
        absolute user path is ever committed."""
        blob = DAEMON.read_text(encoding="utf-8")
        assert "__REPO__" in blob
        assert "__HOME__" in blob
        assert "/Users/" not in blob

    def test_logs_are_durable(self, daemon_template):
        log = daemon_template["StandardOutPath"]
        assert "superwhisper-transcriber" in log
        assert daemon_template["StandardErrorPath"] == log


class TestWatchdogTemplate:
    def test_runs_under_system_python(self, watchdog_template):
        args = [str(a) for a in watchdog_template["ProgramArguments"]]
        assert args[0] == "/usr/bin/python3"
        assert any("health_check.py" in a for a in args)
        assert "--heal" in args

    def test_start_interval_300_and_no_keepalive(self, watchdog_template):
        """WD-1/WD-11: StartInterval 300; KeepAlive on a run-once script is an
        instant infinite loop, so it MUST be absent."""
        assert watchdog_template["StartInterval"] == 300
        assert "KeepAlive" not in watchdog_template

    def test_run_at_load(self, watchdog_template):
        assert watchdog_template["RunAtLoad"] is True

    def test_watchdog_targets_the_daemon_label(self, watchdog_template):
        args = [str(a) for a in watchdog_template["ProgramArguments"]]
        joined = " ".join(args)
        assert "--label com.alex.transcriber" in joined
        assert "--self-label com.alex.transcriber.watchdog" in joined

    def test_watchdog_has_own_durable_log(self, watchdog_template):
        """ES-5: a watchdog crash must also be durable."""
        log = watchdog_template["StandardOutPath"]
        assert "watchdog.log" in log
        assert watchdog_template["StandardErrorPath"] == log

    def test_uses_placeholders_not_home_paths(self, watchdog_template):
        blob = WATCHDOG.read_text(encoding="utf-8")
        assert "__REPO__" in blob
        assert "/Users/" not in blob
