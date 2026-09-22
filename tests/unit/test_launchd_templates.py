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


class TestAppNapExemption:
    """App Nap exemption (LAG-694 fix, WD-15, LAG-733).

    Without it macOS SIGSTOPs the daemon whenever the screen locks: the
    heartbeat freezes, meetings queue until unlock, and each resume leaves
    `[Errno 4] Interrupted system call` in transcriber.log. The watchdog
    cannot heal it — kickstart without -k has no effect on a frozen-but-alive
    PID. Both halves of the exemption are load-bearing and MUST hold together.
    """

    def test_daemon_is_wrapped_in_caffeinate_preventing_idle_sleep(self, daemon_template):
        """WD-15: ProgramArguments[0] is /usr/bin/caffeinate with -i — the
        idle-assertion wrapper keeps the process from being App-Nap-suspended
        while the screen is locked."""
        args = [str(a) for a in daemon_template["ProgramArguments"]]
        assert args[0] == "/usr/bin/caffeinate"
        assert "-i" in args

    def test_caffeinate_wrapper_still_execs_preflight(self, daemon_template):
        """WD-15 / PF-1: the wrapper must still exec the preflight. caffeinate
        sits in front of the chain, so a future edit to ProgramArguments that
        drops the exec would break launchd PID tracking while keeping the
        caffeinate assertion green — the two are pinned together here."""
        args = [str(a) for a in daemon_template["ProgramArguments"]]
        joined = " ".join(args)
        assert "exec" in joined
        assert "preflight.sh" in joined
        # caffeinate -i /bin/zsh -c '<...exec preflight.sh>' — zsh is the shell
        # the exec runs inside, not a daemon entry point of its own.
        assert args[2] == "/bin/zsh"
        assert args[3] == "-c"

    def test_process_type_is_interactive(self, daemon_template):
        """WD-15: ProcessType=Interactive is the launchd-native side of the
        exemption; caffeinate alone must not be the only guard."""
        assert daemon_template["ProcessType"] == "Interactive"


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

    def test_watchdog_passes_daemon_plist_for_bootstrap_repair(self, watchdog_template):
        """WD-9 revision (LAG-673): the plist path is passed at install time so
        the heal ladder can re-bootstrap a booted-out service. Without it the
        watchdog can only escalate — the gap that made the 26-09-16 outage 63h."""
        args = [str(a) for a in watchdog_template["ProgramArguments"]]
        joined = " ".join(args)
        assert "--plist" in joined
        assert "__HOME__/Library/LaunchAgents/com.alex.transcriber.plist" in joined

    def test_watchdog_has_own_durable_log(self, watchdog_template):
        """ES-5: a watchdog crash must also be durable."""
        log = watchdog_template["StandardOutPath"]
        assert "watchdog.log" in log
        assert watchdog_template["StandardErrorPath"] == log

    def test_uses_placeholders_not_home_paths(self, watchdog_template):
        blob = WATCHDOG.read_text(encoding="utf-8")
        assert "__REPO__" in blob
        assert "/Users/" not in blob
