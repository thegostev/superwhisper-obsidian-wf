"""health_check.py assessment tests (ADR 0009/0010, spec HC-*).

The 2026-09-07 incident is encoded here: the log was written by the failure
itself, launchctl columns lie in both directions, and the heartbeat file's
mtime is the only honest signal. assess_health and parse_launchctl_list are
pure functions (HC-7) — these tests also pin that purity.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import health_check

PROJECT_DIR = Path(__file__).parents[2]

LAUNCHCTL_OUTPUT = """\
PID	Status	Label
8078	-15	com.alex.transcriber
-	0	com.alex.transcriber.watchdog
-	-	com.other.service
"""


def make_heartbeat(**overrides):
    payload = {
        "schema": 1,
        "pid": 8078,
        "phase": "scanning",
        "cycle": 4321,
        "updated_at": "2026-09-07T14:31:02Z",
        "started_at": "2026-09-07T02:31:10Z",
        "state_complete": 316,
        "failed_permanent": 0,
    }
    payload.update(overrides)
    return payload


class TestParseLaunchctlList:
    def test_exact_label_match(self):
        status = health_check.parse_launchctl_list(LAUNCHCTL_OUTPUT, "com.alex.transcriber")
        assert status == {"label": "com.alex.transcriber", "pid": 8078, "last_exit_status": -15}

    def test_dash_columns_parse_as_none(self):
        status = health_check.parse_launchctl_list(LAUNCHCTL_OUTPUT, "com.alex.transcriber.watchdog")
        assert status == {"label": "com.alex.transcriber.watchdog", "pid": None, "last_exit_status": 0}

    def test_missing_label_returns_none(self):
        assert health_check.parse_launchctl_list(LAUNCHCTL_OUTPUT, "com.missing.label") is None

    def test_watchdog_label_does_not_match_daemon_label(self):
        """run_transcriber.sh's old substring grep would confuse the two (commit 8);
        the health check must match by exact field equality (HC-6)."""
        assert health_check.parse_launchctl_list(LAUNCHCTL_OUTPUT, "com.alex.transcriber.watchdog") is not None
        daemon = health_check.parse_launchctl_list(LAUNCHCTL_OUTPUT, "com.alex.transcriber")
        assert daemon["label"] == "com.alex.transcriber"

    def test_no_header_still_parses(self):
        assert health_check.parse_launchctl_list("123\t0\tcom.x\n", "com.x") == {
            "label": "com.x",
            "pid": 123,
            "last_exit_status": 0,
        }

    def test_is_pure_no_subprocess(self, monkeypatch):
        def forbidden(*_a, **_k):
            raise AssertionError("parse_launchctl_list must not spawn subprocesses (HC-7)")

        monkeypatch.setattr(subprocess, "run", forbidden)
        health_check.parse_launchctl_list(LAUNCHCTL_OUTPUT, "com.alex.transcriber")


class TestAssessHealth:
    """report = assess_health(heartbeat_payload, heartbeat_age, launchctl_status, max_age)."""

    def test_fresh_heartbeat_is_healthy(self):
        report = health_check.assess_health(make_heartbeat(), 5.0, {"pid": 8078, "last_exit_status": -15}, 300)
        assert report["verdict"] == "healthy"
        assert report["reason"] is None

    def test_fresh_heartbeat_healthy_despite_nonzero_last_exit_status(self):
        """The verified live case: launchctl reports 8078 -15 — a live PID beside a
        stale non-zero exit from the previous run (HC-4)."""
        report = health_check.assess_health(make_heartbeat(), 5.0, {"pid": 8078, "last_exit_status": -15}, 300)
        assert report["verdict"] == "healthy"

    def test_stale_heartbeat_unhealthy_even_with_live_pid(self):
        """Crash-loop sampling race: a live PID exists on every respawn for a few
        ms (HC-5) — the heartbeat still says unhealthy."""
        report = health_check.assess_health(make_heartbeat(), 900.0, {"pid": 8078, "last_exit_status": 0}, 300)
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "heartbeat_stale"

    def test_fresh_heartbeat_with_zero_processed_files_is_healthy(self):
        """Quiet-weekend false positive: state_complete=0 must not matter."""
        report = health_check.assess_health(make_heartbeat(state_complete=0), 5.0, {"pid": 8078}, 300)
        assert report["verdict"] == "healthy"

    def test_missing_heartbeat_is_unhealthy(self):
        report = health_check.assess_health(None, None, {"pid": 8078}, 300)
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "heartbeat_missing"

    def test_unknown_schema_is_unhealthy(self):
        report = health_check.assess_health(make_heartbeat(schema=99), 5.0, {"pid": 8078}, 300)
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "heartbeat_schema_unknown"

    def test_service_not_loaded_is_unhealthy(self):
        report = health_check.assess_health(make_heartbeat(), 5.0, None, 300)
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "service_not_loaded"

    def test_fresh_fatal_heartbeat_is_unhealthy(self):
        """HC-15/PF-16: a fresh phase=fatal heartbeat maps to escalate-without-restart."""
        report = health_check.assess_health(
            make_heartbeat(phase="fatal", fatal_reason="FatalAPIError: boom"), 5.0, {"pid": None}, 300
        )
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "heartbeat_fatal"
        assert report["fatal_reason"] == "FatalAPIError: boom"

    def test_pid_missing_warning_is_not_unhealthy(self):
        """HC-5: a fresh heartbeat beside a missing PID warns but stays healthy."""
        report = health_check.assess_health(make_heartbeat(), 5.0, {"pid": None, "last_exit_status": 1}, 300)
        assert report["verdict"] == "healthy"
        assert report["pid_missing_warning"] is True

    def test_heartbeat_missing_pid_warning_false_when_pid_present(self):
        report = health_check.assess_health(make_heartbeat(), 5.0, {"pid": 8078}, 300)
        assert report["pid_missing_warning"] is False


class TestReadHeartbeatFile:
    def test_reads_payload_and_age(self, tmp_path):
        path = tmp_path / "hb.json"
        path.write_text(json.dumps(make_heartbeat()), encoding="utf-8")
        payload, age, reason = health_check.read_heartbeat(path)
        assert payload["schema"] == 1
        assert 0 <= age < 5
        assert reason is None

    def test_missing_file_reason(self, tmp_path):
        payload, age, reason = health_check.read_heartbeat(tmp_path / "nope.json")
        assert payload is None
        assert reason == "heartbeat_missing"

    def test_unreadable_json_reason(self, tmp_path):
        path = tmp_path / "hb.json"
        path.write_text("{not json", encoding="utf-8")
        payload, age, reason = health_check.read_heartbeat(path)
        assert payload is None
        assert reason == "heartbeat_unreadable"

    def test_non_dict_json_reason(self, tmp_path):
        path = tmp_path / "hb.json"
        path.write_text("[1, 2]", encoding="utf-8")
        payload, age, reason = health_check.read_heartbeat(path)
        assert payload is None
        assert reason == "heartbeat_unreadable"


class TestPurity:
    def test_assess_health_performs_no_io(self, monkeypatch):
        def forbidden(*_a, **_k):
            raise AssertionError("assess_health must not perform I/O (HC-7)")

        monkeypatch.setattr(subprocess, "run", forbidden)
        monkeypatch.setattr(Path, "stat", forbidden)
        health_check.assess_health(make_heartbeat(), 5.0, {"pid": 1}, 300)


class TestSystemPythonIsolation:
    def test_health_check_imports_under_system_python(self):
        """HC-11: the watchdog runs under /usr/bin/python3 (3.9.6) — the one
        interpreter Homebrew cannot break. This subprocess test permanently
        enforces stdlib-only + `from __future__ import annotations`: importing
        config.py (PyYAML) or the repo's runtime-evaluated annotation style
        would fail the import."""
        result = subprocess.run(
            [sys.executable, "-c", "import health_check"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS system python is 3.9.6")
    def test_import_under_python39(self):
        result = subprocess.run(
            ["/usr/bin/python3", "-c", "import health_check"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_source_does_not_import_config_or_yaml(self):
        source = (PROJECT_DIR / "health_check.py").read_text(encoding="utf-8")
        import_lines = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        joined = "\n".join(import_lines)
        assert "config" not in joined
        assert "yaml" not in joined
        assert "pipeline" not in joined


class TestWriterReaderContract:
    def test_pipeline_heartbeat_is_readable_by_health_check(self, tmp_path, monkeypatch):
        """The JSON schema is the contract between a 3.13 writer and a 3.9 reader."""
        import pipeline

        path = tmp_path / "hb.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(path))
        pipeline._heartbeat_last_write = 0.0
        pipeline._heartbeat_failures = 0
        pipeline._heartbeat_context = {"cycle": 0, "failed_permanent": 0, "state_complete": 0}
        pipeline._heartbeat_started_at = None
        assert pipeline.write_heartbeat("scanning", cycle=7, failed_permanent=1, state_complete=2, force=True)

        payload, age, reason = health_check.read_heartbeat(path)
        assert reason is None
        report = health_check.assess_health(payload, age, {"pid": payload["pid"]}, 300)
        assert report["verdict"] == "healthy"
        assert payload["failed_permanent"] == 1
        assert payload["state_complete"] == 2


class TestCli:
    def test_dry_run_healthy_exit_0(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "hb.json"
        path.write_text(json.dumps(make_heartbeat()), encoding="utf-8")
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t-15\tcom.alex.transcriber\n")
        rc = health_check.main(["--heartbeat", str(path), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "healthy" in out.lower()

    def test_unhealthy_exit_1_no_actions_taken(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t-15\tcom.alex.transcriber\n")
        rc = health_check.main(["--heartbeat", str(tmp_path / "missing.json"), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "heartbeat_missing" in out

    def test_service_not_loaded_reported_distinctly(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "hb.json"
        path.write_text(json.dumps(make_heartbeat()), encoding="utf-8")
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "123\t0\tcom.unrelated\n")
        rc = health_check.main(["--heartbeat", str(path), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "service_not_loaded" in out

    def test_no_subprocess_side_effects_in_dry_run(self, tmp_path, monkeypatch):
        """HC-8/HC-9: read-only mode must not restart, write state, or notify."""
        calls = []

        def tracking_run(*args, **kwargs):
            calls.append(args[0] if args else kwargs)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", tracking_run)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t-15\tcom.alex.transcriber\n")
        rc = health_check.main(["--heartbeat", str(tmp_path / "missing.json"), "--dry-run"])
        assert rc == 1
        assert not calls  # only the launchctl read happened, nothing else

    def test_max_age_is_explicit_configuration(self, tmp_path, monkeypatch):
        """HC-14: the maximum age is an explicit input, default 300."""
        assert health_check.DEFAULT_MAX_AGE == 300
        path = tmp_path / "hb.json"
        path.write_text(json.dumps(make_heartbeat()), encoding="utf-8")
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        # age 400: unhealthy at max_age 300, healthy at 500
        monkeypatch.setattr(health_check, "_heartbeat_age", lambda _p: 400.0)
        assert health_check.main(["--heartbeat", str(path), "--dry-run"]) == 1
        assert health_check.main(["--heartbeat", str(path), "--dry-run", "--max-age", "500"]) == 0

    def test_internal_error_exit_3(self, tmp_path, monkeypatch):
        def broken():
            raise RuntimeError("launchctl exploded")

        monkeypatch.setattr(health_check, "run_launchctl_list", broken)
        rc = health_check.main(["--heartbeat", str(tmp_path / "missing.json"), "--dry-run"])
        assert rc == 3


def test_heartbeat_age_uses_mtime(monkeypatch, tmp_path):
    """HC-1: staleness is judged on the file's modification time, not the
    embedded updated_at (kernel-written, immune to the writer's clock)."""
    path = tmp_path / "hb.json"
    path.write_text(json.dumps(make_heartbeat(updated_at="1999-01-01T00:00:00Z")), encoding="utf-8")
    payload, age, reason = health_check.read_heartbeat(path)
    assert reason is None
    assert age < 60  # fresh mtime despite ancient embedded timestamp


NOW = 1_800_000_000.0
SCAN_CYCLE = 30.0
GAP = 8 * 3600.0  # an 8-hour lid-close suspension


def wd_report(reason, age=900.0):
    verdict = "healthy" if reason is None else "unhealthy"
    return {
        "verdict": verdict,
        "reason": reason,
        "fatal_reason": "FatalAPIError: boom" if reason == "heartbeat_fatal" else None,
        "age": age,
    }


def wd_state(**overrides):
    state = health_check.default_wd_state()
    state["last_run_at"] = NOW - 300.0  # a normal tick ago
    state.update(overrides)
    return state


def decide(state, reason, *, age=900.0, paused=False, rebuild_busy=False, max_age=300.0):
    return health_check.decide_action(
        wd_report(reason, age), state, NOW, max_age=max_age, paused=paused, rebuild_busy=rebuild_busy
    )


class TestDecideAction:
    # -- WD-10 pause sentinel ------------------------------------------------
    def test_pause_sentinel_takes_no_action(self):
        out = decide(wd_state(), "heartbeat_stale", paused=True)
        assert out["action"] == "pause"
        assert not out["restart"]
        assert not out["notify"]
        assert out["state"]["last_run_at"] == NOW  # the run is still recorded

    # -- WD-5 first run ------------------------------------------------------
    def test_first_run_skips_tick(self):
        out = decide(health_check.default_wd_state(), "heartbeat_stale")
        assert out["action"] == "first_run"
        assert not out["restart"]
        assert not out["notify"]

    # -- healthy ticks -------------------------------------------------------
    def test_healthy_resets_counters(self):
        out = decide(wd_state(consecutive_failures=2), None, age=5.0)
        assert out["action"] == "record"
        assert out["state"]["consecutive_failures"] == 0

    def test_healthy_after_escalation_notifies_recovery_and_resets(self):
        """ES-4: exactly one recovery notification, sent by the watchdog, and the
        component that sends it resets its counters."""
        out = decide(wd_state(escalated=True, consecutive_failures=2), None, age=5.0)
        assert out["action"] == "notify_recovery"
        assert out["notify"]
        assert out["state"]["escalated"] is False
        assert out["state"]["consecutive_failures"] == 0

    # -- WD-6 restart ladder -------------------------------------------------
    def test_first_unhealthy_tick_kickstarts(self):
        out = decide(wd_state(), "heartbeat_stale")
        assert out["action"] == "kickstart"
        assert out["restart"]
        assert out["state"]["consecutive_failures"] == 1
        assert out["state"]["last_kickstart_at"] == NOW

    def test_second_unhealthy_tick_kickstarts_again(self):
        out = decide(wd_state(consecutive_failures=1, last_kickstart_at=NOW - 300.0), "heartbeat_stale")
        assert out["restart"]
        assert out["state"]["consecutive_failures"] == 2

    def test_third_unhealthy_tick_escalates_without_restart(self):
        state = wd_state(consecutive_failures=2, last_kickstart_at=NOW - 300.0)
        out = decide(state, "heartbeat_stale")
        assert out["action"] == "escalate"
        assert not out["restart"]
        assert out["notify"]
        assert out["state"]["escalated"] is True
        assert out["state"]["escalated_at"] == NOW

    def test_slow_retry_kickstarts_again_after_an_hour(self):
        """WD-6: bounded slow retry — at most one restart per hour while
        unhealthy, so recovery resumes once a transient cause clears."""
        state = wd_state(
            consecutive_failures=3, escalated=True, last_kickstart_at=NOW - 7200.0, last_notify_at=NOW - 7200.0
        )
        out = decide(state, "heartbeat_stale")
        assert out["restart"]
        assert out["notify"]  # cooldown expired too

    def test_slow_retry_waits_within_the_hour(self):
        state = wd_state(
            consecutive_failures=3, escalated=True, last_kickstart_at=NOW - 600.0, last_notify_at=NOW - 600.0
        )
        out = decide(state, "heartbeat_stale")
        assert not out["restart"]
        assert not out["notify"]
        assert out["action"] == "wait"

    def test_es3_notification_cooldown_suppresses_repeat_escalation(self):
        state = wd_state(
            consecutive_failures=3, escalated=True, last_kickstart_at=NOW - 7200.0, last_notify_at=NOW - 60.0
        )
        out = decide(state, "heartbeat_stale")
        assert out["restart"]
        assert not out["notify"]  # notified a minute ago — ES-3 shared cooldown

    # -- PF-16 reader half: fatal heartbeat ----------------------------------
    def test_fatal_heartbeat_escalates_without_restart_on_first_tick(self):
        out = decide(wd_state(), "heartbeat_fatal")
        assert not out["restart"]
        assert out["notify"]
        assert out["state"]["escalated"] is True

    # -- WD-9: service_not_loaded is escalate-only ---------------------------
    def test_service_not_loaded_escalates_without_restart(self):
        out = decide(wd_state(), "service_not_loaded")
        assert not out["restart"]
        assert out["notify"]

    # -- PF-14: fresh rebuild marker is busy ---------------------------------
    def test_rebuild_marker_busy_observes_without_counting_failure(self):
        out = decide(wd_state(), "heartbeat_stale", rebuild_busy=True)
        assert out["action"] == "observe_busy"
        assert not out["restart"]
        assert out["state"]["consecutive_failures"] == 0  # no counter increment

    # -- WD-4: sleep-gap observational grace ---------------------------------
    def test_stale_heartbeat_explained_by_gap_is_observational(self):
        state = wd_state(escalated=True)  # escalation state must survive
        state["last_run_at"] = NOW - GAP
        out = decide(state, "heartbeat_stale", age=GAP - 60.0)  # beat 1 min before sleep
        assert out["action"] == "observational"
        assert not out["restart"]
        assert not out["notify"]
        assert out["state"]["consecutive_failures"] == 0
        assert out["state"]["escalated"] is True  # reset must not clear escalation (WD-4)

    def test_grace_boundary_is_one_scan_cycle(self):
        state = wd_state()
        state["last_run_at"] = NOW - GAP
        older = decide(state, "heartbeat_stale", age=GAP + SCAN_CYCLE + 1)
        assert older["restart"]  # proven pre-sleep failure — decide normally
        within = decide(state, "heartbeat_stale", age=GAP + SCAN_CYCLE)
        assert within["action"] == "observational"

    def test_normal_jitter_gap_does_not_grant_grace(self):
        """A stale heartbeat after a normal 300 s tick is a real failure."""
        out = decide(wd_state(), "heartbeat_stale", age=500.0)
        assert out["restart"]

    def test_missing_heartbeat_during_sleep_decides_normally(self):
        """Sleep preserves files; a missing heartbeat is never gap-explained."""
        state = wd_state()
        state["last_run_at"] = NOW - GAP
        out = decide(state, "heartbeat_missing", age=None)
        assert out["restart"]

    def test_fresh_heartbeat_after_sleep_still_recovers(self):
        """ES-4 unaffected by jitter: a fresh heartbeat decides normally even
        after an 8-hour gap."""
        state = wd_state(escalated=True)
        state["last_run_at"] = NOW - GAP
        out = decide(state, None, age=5.0)
        assert out["action"] == "notify_recovery"


class TestWdStatePersistence:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "wd.json"
        state = wd_state(escalated=True, last_notify_at=NOW)
        health_check.write_wd_state(path, state)
        assert health_check.load_wd_state(path) == state

    def test_missing_state_is_first_run(self, tmp_path):
        assert health_check.load_wd_state(tmp_path / "nope.json") == health_check.default_wd_state()

    def test_corrupt_state_is_first_run(self, tmp_path):
        """WD-12: unreadable/corrupt state → treat as first run and overwrite."""
        path = tmp_path / "wd.json"
        path.write_text("{corrupt", encoding="utf-8")
        assert health_check.load_wd_state(path) == health_check.default_wd_state()

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        path = tmp_path / "wd.json"
        health_check.write_wd_state(path, wd_state())
        assert not path.with_suffix(".json.tmp").exists()


class TestWatchdogWrappers:
    def test_kickstart_uses_absolute_path_and_timeout(self, monkeypatch):
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"], recorded["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        health_check.kickstart("com.alex.transcriber")
        assert recorded["cmd"][0] == "/bin/launchctl"  # WD-8
        assert "kickstart" in recorded["cmd"]
        assert f"gui/{os.getuid()}/com.alex.transcriber" in recorded["cmd"]
        assert recorded["kwargs"].get("timeout") is not None  # WD-7

    def test_notify_passes_text_as_argv_not_interpolation(self, monkeypatch):
        """ES-2: message/title arrive as arguments to an `on run argv` handler."""
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"], recorded["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        health_check.notify("Title", 'Message "with quotes"')
        cmd = recorded["cmd"]
        assert "on run argv" in cmd
        # the message must appear only as a trailing argv item — never inside an
        # AppleScript (-e) fragment (ES-2)
        fragments = cmd[1:-2]
        assert all('Message "with quotes"' not in fragment for fragment in fragments)
        assert cmd[-2] == 'Message "with quotes"'
        assert cmd[-1] == "Title"
        assert recorded["kwargs"].get("timeout") is not None

    def test_notify_failure_does_not_raise(self, monkeypatch):
        """ES-6: a failed notification must not abort remaining logic."""

        def boom(*_a, **_k):
            raise OSError("osascript missing")

        monkeypatch.setattr(subprocess, "run", boom)
        assert health_check.notify("Title", "Message") is False

    def test_fresh_rebuild_marker_is_busy(self, tmp_path, monkeypatch):
        marker = tmp_path / "rebuild.json"
        marker.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time() - 10}), encoding="utf-8")
        assert health_check.load_rebuild_marker(marker) is True  # our own PID is alive
        assert marker.exists()

    def test_stale_rebuild_marker_is_ignored_and_removed(self, tmp_path):
        """PF-14: dead holding PID, or age beyond the rebuild budget → ignored, removed."""
        marker = tmp_path / "rebuild.json"
        marker.write_text(json.dumps({"pid": 999999999, "started_at": time.time() - 10}), encoding="utf-8")
        assert health_check.load_rebuild_marker(marker) is False
        assert not marker.exists()

    def test_marker_beyond_rebuild_budget_is_stale(self, tmp_path):
        marker = tmp_path / "rebuild.json"
        marker.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time() - 10_000}), encoding="utf-8")
        assert health_check.load_rebuild_marker(marker) is False
        assert not marker.exists()

    def test_corrupt_marker_is_not_busy(self, tmp_path):
        marker = tmp_path / "rebuild.json"
        marker.write_text("nope", encoding="utf-8")
        assert health_check.load_rebuild_marker(marker) is False


class TestHealCli:
    def setup_paths(self, monkeypatch, tmp_path):
        state_path = tmp_path / "wd.json"
        monkeypatch.setattr(health_check, "PAUSE_SENTINEL_PATH", str(tmp_path / "pause"))
        monkeypatch.setattr(health_check, "REBUILD_MARKER_PATH", str(tmp_path / "rebuild.json"))
        monkeypatch.setattr(health_check, "REPOINT_MARKER_PATH", str(tmp_path / "repoint.json"))
        return state_path

    def test_heal_kickstarts_on_missing_heartbeat_and_persists_state(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        kicked, told = [], []
        monkeypatch.setattr(health_check, "kickstart", lambda label: kicked.append(label) or True)
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        health_check.write_wd_state(state_path, wd_state())
        rc = health_check.main(["--heal", "--heartbeat", str(tmp_path / "nope.json"), "--state", str(state_path)])
        assert rc == 1
        assert kicked == ["com.alex.transcriber"]
        assert told == []  # first two ticks kickstart, no escalation yet
        saved = health_check.load_wd_state(state_path)
        assert saved["consecutive_failures"] == 1
        assert saved["last_action"] == "kickstart"

    def test_heal_dry_run_performs_nothing(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        kicked, told = [], []
        monkeypatch.setattr(health_check, "kickstart", lambda label: kicked.append(label) or True)
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        rc = health_check.main(
            ["--heal", "--heartbeat", str(tmp_path / "nope.json"), "--state", str(state_path), "--dry-run"]
        )
        assert rc == 1
        assert kicked == [] and told == []
        assert not state_path.exists()  # dry-run writes no state either
        assert "dry-run" in capsys.readouterr().out

    def test_heal_pause_sentinel_exits_zero(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        sentinel = tmp_path / "pause"
        sentinel.write_text("", encoding="utf-8")
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        kicked = []
        monkeypatch.setattr(health_check, "kickstart", lambda label: kicked.append(label) or True)
        rc = health_check.main(["--heal", "--heartbeat", str(tmp_path / "nope.json"), "--state", str(state_path)])
        assert rc == 0
        assert kicked == []
        assert "pause" in capsys.readouterr().out.lower()

    def test_wd2_watchdog_refuses_to_target_itself(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        kicked = []
        monkeypatch.setattr(health_check, "kickstart", lambda label: kicked.append(label) or True)
        rc = health_check.main(
            [
                "--heal",
                "--label",
                "com.alex.transcriber.watchdog",
                "--self-label",
                "com.alex.transcriber.watchdog",
                "--heartbeat",
                str(tmp_path / "nope.json"),
                "--state",
                str(state_path),
            ]
        )
        assert rc == 3
        assert kicked == []

    def test_notify_test_sends_test_notification(self, tmp_path, monkeypatch, capsys):
        self.setup_paths(monkeypatch, tmp_path)
        told = []
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        rc = health_check.main(["--notify-test"])
        assert rc == 0
        assert len(told) == 1

    def test_escalation_failure_does_not_change_exit_code(self, tmp_path, monkeypatch, capsys):
        """ES-6."""
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        monkeypatch.setattr(health_check, "kickstart", lambda label: True)
        monkeypatch.setattr(
            health_check, "notify", lambda t, m: (_ for _ in ()).throw(OSError("no Notification Center"))
        )

        def boom(t, m):
            raise OSError("osascript failed")

        monkeypatch.setattr(health_check, "notify", boom)
        health_check.write_wd_state(state_path, wd_state(consecutive_failures=2, last_kickstart_at=NOW - 300.0))
        rc = health_check.main(["--heal", "--heartbeat", str(tmp_path / "nope.json"), "--state", str(state_path)])
        assert rc == 1  # unhealthy, not an internal error
        saved = health_check.load_wd_state(state_path)
        assert saved["escalated"] is True  # escalation state recorded despite failed delivery

    def test_recovery_notification_text_mentions_restored_health(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        hb = tmp_path / "hb.json"
        hb.write_text(json.dumps(make_heartbeat()), encoding="utf-8")
        told = []
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        health_check.write_wd_state(state_path, wd_state(escalated=True, consecutive_failures=2))
        rc = health_check.main(["--heal", "--heartbeat", str(hb), "--state", str(state_path)])
        assert rc == 0
        assert len(told) == 1
        assert "recover" in told[0][1].lower() or "restored" in told[0][1].lower()

    def test_escalation_message_includes_repoint_note(self, tmp_path, monkeypatch, capsys):
        """ES-8: escalation text states venv rebuilds and interpreter repoints."""
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        monkeypatch.setattr(health_check, "kickstart", lambda label: True)
        told = []
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        repoint = tmp_path / "repoint.json"
        repoint.write_text(
            json.dumps({"message": "venv rebuilt; python repointed to /opt/homebrew/bin/python3.12"}), encoding="utf-8"
        )
        health_check.write_wd_state(state_path, wd_state(consecutive_failures=2, last_kickstart_at=NOW - 300.0))
        health_check.main(["--heal", "--heartbeat", str(tmp_path / "nope.json"), "--state", str(state_path)])
        assert any("repointed" in m for _t, m in told)
