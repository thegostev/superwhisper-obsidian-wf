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
        "writer": "daemon",
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
        pipeline.set_heartbeat_writer("daemon")  # HB-12: this write stands in for the daemon's
        assert pipeline.write_heartbeat("scanning", cycle=7, failed_permanent=1, state_complete=2, force=True)

        payload, age, reason = health_check.read_heartbeat(path)
        assert reason is None
        report = health_check.assess_health(payload, age, {"pid": payload["pid"]}, 300)
        assert report["verdict"] == "healthy"
        assert payload["failed_permanent"] == 1
        assert payload["state_complete"] == 2
        # HB-12/HC-17: the writer identity crosses the 3.13-writer / 3.9-reader boundary.
        assert health_check.heartbeat_writer(payload) == "daemon"

    def test_an_ops_run_heartbeat_reads_back_as_manual(self, tmp_path, monkeypatch):
        """HB-12 (LAG-675): an ops script reaches the same writer through the
        shared pipeline functions. Its heartbeat must not read as the daemon's."""
        import pipeline

        path = tmp_path / "hb.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(path))
        pipeline._heartbeat_last_write = 0.0
        pipeline._heartbeat_started_at = None
        pipeline._heartbeat_writer = pipeline.DEFAULT_HEARTBEAT_WRITER  # no declaration: an ops run
        assert pipeline.write_heartbeat("processing", force=True)

        payload, age, reason = health_check.read_heartbeat(path)
        assert reason is None
        assert health_check.heartbeat_writer(payload) == "manual"
        report = health_check.assess_health(payload, age, {"pid": payload["pid"]}, 300, daemon_pid=None)
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "heartbeat_not_daemon"


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


def decide(
    state,
    reason,
    *,
    age=900.0,
    paused=False,
    rebuild_busy=False,
    max_age=300.0,
    bootstrap_available=False,
    daemon_alive=False,
):
    return health_check.decide_action(
        wd_report(reason, age),
        state,
        NOW,
        max_age=max_age,
        paused=paused,
        rebuild_busy=rebuild_busy,
        bootstrap_available=bootstrap_available,
        daemon_alive=daemon_alive,
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

    # -- WD-9 revision (LAG-673): bootstrap repair when the plist is known ---
    def test_service_not_loaded_bootstraps_and_notifies_when_plist_available(self):
        """The 26-09-16 incident: bootout left the service unloaded for 63h
        because the ladder had no repair for it. With a plist passed at install
        time, the watchdog re-bootstraps — and notifies on the FIRST tick
        (a bootout is a deliberate human action, not a crash loop, so the
        WD-6 first-tick silence must not apply)."""
        out = decide(wd_state(), "service_not_loaded", bootstrap_available=True)
        assert out["action"] == "bootstrap"
        assert out["restart"]
        assert out["notify"]

    def test_service_not_loaded_without_plist_stays_escalate_only(self):
        """Without a plist path, kickstart cannot work and bootstrap cannot be
        attempted — behaviour unchanged (escalate-only)."""
        out = decide(wd_state(), "service_not_loaded", bootstrap_available=False)
        assert not out["restart"]
        assert out["notify"]

    def test_bootstrap_is_capped_like_kickstarts(self):
        """WD-6 capping applies to the bootstrap verb too: after the two
        immediate attempts, at most one retry per hour."""
        state = wd_state(
            consecutive_failures=3, escalated=True, last_kickstart_at=NOW - 600.0, last_notify_at=NOW - 600.0
        )
        out = decide(state, "service_not_loaded", bootstrap_available=True)
        assert not out["restart"]
        assert out["action"] == "wait"

    def test_bootstrap_slow_retry_resumes_after_an_hour(self):
        state = wd_state(
            consecutive_failures=3, escalated=True, last_kickstart_at=NOW - 7200.0, last_notify_at=NOW - 7200.0
        )
        out = decide(state, "service_not_loaded", bootstrap_available=True)
        assert out["action"] == "bootstrap"
        assert out["restart"]
        assert out["notify"]

    def test_paused_service_not_loaded_still_pauses(self):
        """WD-10 precedence is untouched: the sentinel wins over any repair."""
        out = decide(wd_state(), "service_not_loaded", paused=True, bootstrap_available=True)
        assert out["action"] == "pause"
        assert not out["restart"]

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

    # -- WD-17 frozen-but-alive escalation (LAG-753) -------------------------
    def test_stale_heartbeat_with_live_daemon_escalates_to_kickstart_kill(self):
        """WD-17: a stale heartbeat beside a live PID is the SIGSTOP/App-Nap
        shape — plain kickstart is a no-op, so the ladder must use `-k`."""
        out = decide(wd_state(), "heartbeat_stale", daemon_alive=True)
        assert out["action"] == "kickstart_kill"
        assert out["restart"]
        assert out["state"]["consecutive_failures"] == 1
        assert out["state"]["last_kickstart_at"] == NOW

    def test_stale_heartbeat_without_live_daemon_keeps_plain_kickstart(self):
        """WD-6 unchanged: process-absent still takes the ordinary path."""
        out = decide(wd_state(), "heartbeat_stale", daemon_alive=False)
        assert out["action"] == "kickstart"
        assert out["restart"]

    def test_frozen_alive_is_capped_like_kickstarts(self):
        """WD-6 capping applies unchanged: the third tick escalates instead."""
        state = wd_state(consecutive_failures=2, last_kickstart_at=NOW - 300.0)
        out = decide(state, "heartbeat_stale", daemon_alive=True)
        assert out["action"] == "escalate"
        assert not out["restart"]
        assert out["notify"]

    def test_frozen_alive_slow_retry_kills_again_after_an_hour(self):
        state = wd_state(
            consecutive_failures=3, escalated=True, last_kickstart_at=NOW - 7200.0, last_notify_at=NOW - 7200.0
        )
        out = decide(state, "heartbeat_stale", daemon_alive=True)
        assert out["action"] == "kickstart_kill"
        assert out["restart"]

    def test_sleep_gap_grace_still_wins_over_frozen_alive(self):
        """WD-4 is unchanged: staleness explained by the watchdog's own gap is
        observational, even though the daemon PID is alive throughout a sleep."""
        state = wd_state()
        state["last_run_at"] = NOW - GAP
        out = decide(state, "heartbeat_stale", age=GAP + SCAN_CYCLE, daemon_alive=True)
        assert out["action"] == "observational"
        assert not out["restart"]

    def test_service_not_loaded_is_unaffected_by_the_liveness_flag(self):
        """WD-9 owns the absent-job case; a stray alive flag must not divert it."""
        out = decide(wd_state(), "service_not_loaded", bootstrap_available=True, daemon_alive=True)
        assert out["action"] == "bootstrap"

    def test_fatal_heartbeat_is_unaffected_by_the_liveness_flag(self):
        """PF-16: a fatal daemon is alive by definition and must not be killed."""
        out = decide(wd_state(), "heartbeat_fatal", daemon_alive=True)
        assert out["action"] == "escalate"
        assert not out["restart"]

    def test_healthy_tick_is_unaffected_by_the_liveness_flag(self):
        out = decide(wd_state(), None, age=5.0, daemon_alive=True)
        assert out["action"] == "record"
        assert not out["restart"]


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
    def test_launchctl_bin_defaults_to_absolute_path(self, monkeypatch):
        """WD-8: subprocess paths are absolute in production. The env hook
        exists only so shell-level harnesses can shim launchctl (an absolute
        path cannot be intercepted via PATH)."""
        monkeypatch.delenv("HEALTH_CHECK_LAUNCHCTL", raising=False)
        # Constant is computed at import; deleting the env var restores it.
        import importlib

        module = importlib.reload(health_check)
        assert module.LAUNCHCTL_BIN == "/bin/launchctl"
        importlib.reload(health_check)  # restore for subsequent tests

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
        assert "-k" not in recorded["cmd"]  # WD-17: the plain verb stays plain

    def test_kickstart_kill_adds_the_k_flag(self, monkeypatch):
        """WD-17: `-k` kills the job before restarting it — the only verb that
        moves a frozen-but-alive PID."""
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"], recorded["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert health_check.kickstart("com.alex.transcriber", kill=True) is True
        assert recorded["cmd"][0] == "/bin/launchctl"  # WD-8
        assert recorded["cmd"][1] == "kickstart"
        assert "-k" in recorded["cmd"]
        assert recorded["cmd"][-1] == f"gui/{os.getuid()}/com.alex.transcriber"
        assert recorded["kwargs"].get("timeout") is not None  # WD-7

    def test_kickstart_kill_failure_returns_false_not_raise(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "no such process")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert health_check.kickstart("com.alex.transcriber", kill=True) is False

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

    def test_bootstrap_repair_uses_bootstrap_verb_plist_and_timeout(self, monkeypatch):
        """WD-7/WD-8: absolute binary, gui/<uid>/<label> domain target, the plist
        as the final argument, timeout set."""
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"], recorded["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert health_check.bootstrap_repair("com.alex.transcriber", "/tmp/daemon.plist") is True
        cmd = recorded["cmd"]
        assert cmd[0] == health_check.LAUNCHCTL_BIN  # WD-8
        assert "bootstrap" in cmd
        assert f"gui/{os.getuid()}/com.alex.transcriber" in cmd
        assert cmd[-1] == "/tmp/daemon.plist"
        assert recorded["kwargs"].get("timeout") is not None  # WD-7

    def test_bootstrap_repair_failure_returns_false_not_raise(self, monkeypatch):
        """ES-6: a failed repair is logged, never fatal."""

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 5, "", "Bootstrap failed: 5: Input/output error")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert health_check.bootstrap_repair("com.alex.transcriber", "/tmp/daemon.plist") is False

    def test_bootstrap_repair_oserror_returns_false_not_raise(self, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("launchctl missing")

        monkeypatch.setattr(subprocess, "run", boom)
        assert health_check.bootstrap_repair("com.alex.transcriber", "/tmp/daemon.plist") is False

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

    def test_heal_bootstraps_when_service_not_loaded_and_plist_given(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "")  # service absent entirely
        bootstrapped, told = [], []
        monkeypatch.setattr(
            health_check, "bootstrap_repair", lambda label, plist: bootstrapped.append((label, plist)) or True
        )
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        plist = tmp_path / "daemon.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        health_check.write_wd_state(state_path, wd_state())
        rc = health_check.main(
            [
                "--heal",
                "--heartbeat",
                str(tmp_path / "nope.json"),
                "--state",
                str(state_path),
                "--plist",
                str(plist),
            ]
        )
        assert rc == 1  # still unhealthy on this tick — the repair just ran
        assert bootstrapped == [("com.alex.transcriber", str(plist))]
        assert len(told) == 1  # LAG-673: notify on the first tick, not after two silent ones
        assert "service_not_loaded" in told[0][1]
        saved = health_check.load_wd_state(state_path)
        assert saved["consecutive_failures"] == 1
        assert saved["last_action"] == "bootstrap"

    def test_heal_without_plist_keeps_escalate_only(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "")
        bootstrapped, told = [], []
        monkeypatch.setattr(
            health_check, "bootstrap_repair", lambda label, plist: bootstrapped.append((label, plist)) or True
        )
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        health_check.write_wd_state(state_path, wd_state())
        rc = health_check.main(["--heal", "--heartbeat", str(tmp_path / "nope.json"), "--state", str(state_path)])
        assert rc == 1
        assert bootstrapped == []  # no plist → no repair attempt
        assert len(told) == 1  # escalation instead
        assert health_check.load_wd_state(state_path)["last_action"] == "escalate"

    def test_heal_plist_path_missing_treated_as_unavailable(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "")
        bootstrapped, told = [], []
        monkeypatch.setattr(
            health_check, "bootstrap_repair", lambda label, plist: bootstrapped.append((label, plist)) or True
        )
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        health_check.write_wd_state(state_path, wd_state())
        rc = health_check.main(
            [
                "--heal",
                "--heartbeat",
                str(tmp_path / "nope.json"),
                "--state",
                str(state_path),
                "--plist",
                str(tmp_path / "gone.plist"),
            ]
        )
        assert rc == 1
        assert bootstrapped == []
        assert len(told) == 1  # degrade to escalation, not to silence
        assert "plist" in capsys.readouterr().err.lower()  # the gap is visible in the log

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


class TestPauseSentinelTtl:
    """WD-10 revision (LAG-674): the 26-09-16 outage ran 59h because a pause
    sentinel left behind after maintenance disabled healing forever. A sentinel
    older than PAUSE_TTL expires: removed, one notice, healing resumes."""

    def test_missing_sentinel_is_absent(self, tmp_path):
        assert health_check.pause_sentinel_status(tmp_path / "pause", time.time()) == "absent"

    def test_fresh_sentinel_is_paused(self, tmp_path):
        sentinel = tmp_path / "pause"
        sentinel.write_text("", encoding="utf-8")
        assert health_check.pause_sentinel_status(sentinel, time.time()) == "paused"

    def test_sentinel_older_than_ttl_is_expired(self, tmp_path):
        sentinel = tmp_path / "pause"
        sentinel.write_text("", encoding="utf-8")
        old = time.time() - health_check.PAUSE_TTL - 60
        os.utime(sentinel, (old, old))
        assert health_check.pause_sentinel_status(sentinel, time.time()) == "expired"

    def test_sentinel_just_inside_ttl_is_paused(self, tmp_path):
        sentinel = tmp_path / "pause"
        sentinel.write_text("", encoding="utf-8")
        recent = time.time() - health_check.PAUSE_TTL + 60
        os.utime(sentinel, (recent, recent))
        assert health_check.pause_sentinel_status(sentinel, time.time()) == "paused"

    def test_ttl_is_four_hours(self):
        assert health_check.PAUSE_TTL == 4 * 3600

    def test_status_is_pure(self, tmp_path):
        """Classification never deletes — removal belongs to the acting tick."""
        sentinel = tmp_path / "pause"
        sentinel.write_text("", encoding="utf-8")
        old = time.time() - health_check.PAUSE_TTL - 60
        os.utime(sentinel, (old, old))
        health_check.pause_sentinel_status(sentinel, time.time())
        assert sentinel.exists()


class TestHealPauseSentinelTtl:
    def setup(self, monkeypatch, tmp_path, *, sentinel_age=None):
        state_path = tmp_path / "wd.json"
        sentinel = tmp_path / "pause"
        monkeypatch.setattr(health_check, "PAUSE_SENTINEL_PATH", str(sentinel))
        monkeypatch.setattr(health_check, "REBUILD_MARKER_PATH", str(tmp_path / "rebuild.json"))
        monkeypatch.setattr(health_check, "REPOINT_MARKER_PATH", str(tmp_path / "repoint.json"))
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        kicked, told = [], []
        monkeypatch.setattr(health_check, "kickstart", lambda label: kicked.append(label) or True)
        monkeypatch.setattr(health_check, "notify", lambda t, m: told.append((t, m)) or True)
        if sentinel_age is not None:
            sentinel.write_text("", encoding="utf-8")
            then = time.time() - sentinel_age
            os.utime(sentinel, (then, then))
        health_check.write_wd_state(state_path, wd_state())
        return state_path, sentinel, kicked, told

    def argv(self, tmp_path, state_path, *extra):
        return ["--heal", "--heartbeat", str(tmp_path / "nope.json"), "--state", str(state_path), *extra]

    def test_fresh_sentinel_still_pauses(self, tmp_path, monkeypatch, capsys):
        state_path, sentinel, kicked, told = self.setup(monkeypatch, tmp_path, sentinel_age=60)
        rc = health_check.main(self.argv(tmp_path, state_path))
        assert rc == 0
        assert kicked == [] and told == []
        assert sentinel.exists()

    def test_stale_sentinel_is_removed_notified_and_healing_resumes(self, tmp_path, monkeypatch, capsys):
        state_path, sentinel, kicked, told = self.setup(monkeypatch, tmp_path, sentinel_age=health_check.PAUSE_TTL + 60)
        rc = health_check.main(self.argv(tmp_path, state_path))
        assert not sentinel.exists()
        assert len(told) == 1
        assert "pause expired" in told[0][1].lower()
        assert "maintenance" in told[0][1].lower()
        assert kicked == ["com.alex.transcriber"]  # healing resumed on the same tick
        assert rc == 1

    def test_expiry_notice_fires_only_once(self, tmp_path, monkeypatch, capsys):
        state_path, sentinel, kicked, told = self.setup(monkeypatch, tmp_path, sentinel_age=health_check.PAUSE_TTL + 60)
        health_check.main(self.argv(tmp_path, state_path))
        health_check.main(self.argv(tmp_path, state_path))
        assert sum("pause expired" in m.lower() for _, m in told) == 1

    def test_missing_sentinel_heals_normally_without_notice(self, tmp_path, monkeypatch, capsys):
        state_path, sentinel, kicked, told = self.setup(monkeypatch, tmp_path)
        rc = health_check.main(self.argv(tmp_path, state_path))
        assert rc == 1
        assert kicked == ["com.alex.transcriber"]
        assert told == []

    def test_dry_run_reports_expiry_but_keeps_sentinel(self, tmp_path, monkeypatch, capsys):
        state_path, sentinel, kicked, told = self.setup(monkeypatch, tmp_path, sentinel_age=health_check.PAUSE_TTL + 60)
        health_check.main(self.argv(tmp_path, state_path, "--dry-run"))
        assert sentinel.exists()
        assert kicked == [] and told == []
        assert "expired" in capsys.readouterr().out.lower()

    def test_unremovable_stale_sentinel_does_not_pause_or_spam(self, tmp_path, monkeypatch, capsys):
        state_path, sentinel, kicked, told = self.setup(monkeypatch, tmp_path, sentinel_age=health_check.PAUSE_TTL + 60)

        def refuse(self, missing_ok=False):
            raise PermissionError("read-only")

        monkeypatch.setattr(Path, "unlink", refuse)
        health_check.main(self.argv(tmp_path, state_path))
        assert kicked == ["com.alex.transcriber"]  # an expired pause never blocks healing
        assert told == []  # notice only after a successful removal — else it repeats every tick
        assert "could not remove" in capsys.readouterr().err.lower()


LAUNCHCTL_PRINT_RUNNING = """\
com.alex.transcriber = {
	active count = 1
	path = /Users/harald/Library/LaunchAgents/com.alex.transcriber.plist
	state = running

	program = /bin/bash
	pid = 8078
	immediate reason = speculative
	forks = 0
}
"""

LAUNCHCTL_PRINT_LOADED_NOT_RUNNING = """\
com.alex.transcriber = {
	active count = 0
	path = /Users/harald/Library/LaunchAgents/com.alex.transcriber.plist
	state = not running

	last exit code = 0
}
"""

LAUNCHCTL_PRINT_MISSING = 'Could not find service "com.alex.transcriber" in domain for login\n'


class TestParseLaunchctlPrintPid:
    """HC-17: `launchctl print` reports a pid only while the job actually runs."""

    def test_running_job_yields_pid(self):
        assert health_check.parse_launchctl_print_pid(LAUNCHCTL_PRINT_RUNNING) == 8078

    def test_loaded_but_not_running_yields_none(self):
        assert health_check.parse_launchctl_print_pid(LAUNCHCTL_PRINT_LOADED_NOT_RUNNING) is None

    def test_unknown_service_yields_none(self):
        assert health_check.parse_launchctl_print_pid(LAUNCHCTL_PRINT_MISSING) is None

    def test_empty_output_yields_none(self):
        assert health_check.parse_launchctl_print_pid("") is None

    def test_does_not_match_other_pid_like_keys(self):
        """`active count`/`last exit code` and the watchdog's own pid line must
        not be mistaken for the daemon's pid."""
        assert health_check.parse_launchctl_print_pid("\tactive count = 3\n\tlast exit code = 1\n") is None

    def test_is_pure_no_subprocess(self, monkeypatch):
        def forbidden(*_a, **_k):
            raise AssertionError("parse_launchctl_print_pid must not spawn subprocesses (HC-7)")

        monkeypatch.setattr(subprocess, "run", forbidden)
        health_check.parse_launchctl_print_pid(LAUNCHCTL_PRINT_RUNNING)


class TestProbeDaemonPid:
    def test_uses_absolute_path_domain_target_and_timeout(self, monkeypatch):
        """WD-7/WD-8: absolute binary, gui/<uid>/<label> target, timeout set."""
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"], recorded["kwargs"] = cmd, kwargs
            return subprocess.CompletedProcess(cmd, 0, LAUNCHCTL_PRINT_RUNNING, "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert health_check.probe_daemon_pid("com.alex.transcriber") == 8078
        assert recorded["cmd"][0] == health_check.LAUNCHCTL_BIN
        assert "print" in recorded["cmd"]
        assert f"gui/{os.getuid()}/com.alex.transcriber" in recorded["cmd"]
        assert recorded["kwargs"].get("timeout") is not None

    def test_nonzero_exit_reads_as_no_live_pid(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 113, "", LAUNCHCTL_PRINT_MISSING),
        )
        assert health_check.probe_daemon_pid("com.alex.transcriber") is None

    def test_subprocess_failure_reads_as_no_live_pid(self, monkeypatch):
        """ES-6: a failed or timed-out probe never raises — it reads as no PID."""

        def boom(*_a, **_k):
            raise subprocess.TimeoutExpired("launchctl", 30)

        monkeypatch.setattr(subprocess, "run", boom)
        assert health_check.probe_daemon_pid("com.alex.transcriber") is None


class TestHeartbeatWriter:
    def test_manual_writer_is_read_back(self):
        assert health_check.heartbeat_writer(make_heartbeat(writer="manual")) == "manual"

    def test_daemon_writer_is_read_back(self):
        assert health_check.heartbeat_writer(make_heartbeat(writer="daemon")) == "daemon"

    def test_absent_writer_defaults_to_daemon(self):
        """HB-12: pre-HB-12 heartbeats carry no writer. Reading them as daemon
        keeps the upgrade backwards compatible — every post-HB-12 writer sets
        the field, and defaults to manual on the writer side."""
        payload = make_heartbeat()
        payload.pop("writer", None)
        assert health_check.heartbeat_writer(payload) == "daemon"

    def test_missing_payload_defaults_to_daemon(self):
        assert health_check.heartbeat_writer(None) == "daemon"

    def test_non_string_writer_defaults_to_daemon(self):
        assert health_check.heartbeat_writer(make_heartbeat(writer=17)) == "daemon"


class TestHeartbeatWriterCrossCheck:
    """HC-17 (LAG-675): a fresh manual heartbeat is not proof of liveness."""

    def test_manual_heartbeat_without_live_pid_is_unhealthy(self):
        report = health_check.assess_health(
            make_heartbeat(writer="manual"), 5.0, {"pid": 8078, "last_exit_status": 0}, 300, daemon_pid=None
        )
        assert report["verdict"] == "unhealthy"
        assert report["reason"] == "heartbeat_not_daemon"

    def test_manual_heartbeat_with_live_pid_is_healthy_but_warned(self):
        report = health_check.assess_health(
            make_heartbeat(writer="manual"), 5.0, {"pid": 8078, "last_exit_status": 0}, 300, daemon_pid=8078
        )
        assert report["verdict"] == "healthy"
        assert report["reason"] is None
        assert report["manual_heartbeat_warning"] is True

    def test_daemon_heartbeat_ignores_the_probe(self):
        report = health_check.assess_health(
            make_heartbeat(writer="daemon"), 5.0, {"pid": 8078, "last_exit_status": 0}, 300, daemon_pid=None
        )
        assert report["verdict"] == "healthy"
        assert report["manual_heartbeat_warning"] is False

    def test_legacy_heartbeat_without_writer_stays_healthy(self):
        payload = make_heartbeat()
        payload.pop("writer", None)
        report = health_check.assess_health(payload, 5.0, {"pid": 8078}, 300, daemon_pid=None)
        assert report["verdict"] == "healthy"

    def test_the_launchctl_list_pid_does_not_substitute_for_the_probe(self):
        """HC-5: the `launchctl list` PID column is sampled and lies in both
        directions — only the `launchctl print` probe may clear a manual
        heartbeat."""
        report = health_check.assess_health(
            make_heartbeat(writer="manual"), 5.0, {"pid": 4242, "last_exit_status": 0}, 300, daemon_pid=None
        )
        assert report["verdict"] == "unhealthy"

    def test_staleness_outranks_the_writer_check(self):
        report = health_check.assess_health(make_heartbeat(writer="manual"), 900.0, {"pid": 8078}, 300, daemon_pid=None)
        assert report["reason"] == "heartbeat_stale"

    def test_service_not_loaded_outranks_the_writer_check(self):
        report = health_check.assess_health(make_heartbeat(writer="manual"), 5.0, None, 300, daemon_pid=None)
        assert report["reason"] == "service_not_loaded"

    def test_fatal_outranks_the_writer_check(self):
        report = health_check.assess_health(
            make_heartbeat(writer="manual", phase="fatal", fatal_reason="boom"),
            5.0,
            {"pid": 8078},
            300,
            daemon_pid=None,
        )
        assert report["reason"] == "heartbeat_fatal"

    def test_assess_health_stays_pure(self, monkeypatch):
        def forbidden(*_a, **_k):
            raise AssertionError("assess_health must not perform I/O (HC-7)")

        monkeypatch.setattr(subprocess, "run", forbidden)
        health_check.assess_health(make_heartbeat(writer="manual"), 5.0, {"pid": 1}, 300, daemon_pid=None)


class TestWriterCrossCheckCli:
    def _write(self, tmp_path, payload):
        path = tmp_path / "hb.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_manual_heartbeat_with_dead_daemon_exits_1(self, tmp_path, monkeypatch, capsys):
        path = self._write(tmp_path, make_heartbeat(writer="manual"))
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: None)
        rc = health_check.main(["--heartbeat", str(path), "--dry-run"])
        assert rc == 1
        assert "heartbeat_not_daemon" in capsys.readouterr().out

    def test_manual_heartbeat_with_live_daemon_exits_0_and_warns(self, tmp_path, monkeypatch, capsys):
        path = self._write(tmp_path, make_heartbeat(writer="manual"))
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: 8078)
        rc = health_check.main(["--heartbeat", str(path), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "writer=manual" in out
        assert "not written by the daemon" in out

    def test_daemon_heartbeat_never_probes(self, tmp_path, monkeypatch):
        """HC-17: the healthy steady state costs no extra subprocess."""
        path = self._write(tmp_path, make_heartbeat(writer="daemon"))
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")

        def forbidden(_label):
            raise AssertionError("a daemon-written heartbeat must not trigger the probe (HC-17)")

        monkeypatch.setattr(health_check, "probe_daemon_pid", forbidden)
        assert health_check.main(["--heartbeat", str(path), "--dry-run"]) == 0

    def test_heal_tick_kickstarts_on_manual_heartbeat(self, tmp_path, monkeypatch, capsys):
        """The freshness is an artefact of the ops run; the daemon behind it is
        dead, so this maps to the ordinary restart ladder (WD-6)."""
        path = self._write(tmp_path, make_heartbeat(writer="manual"))
        state_path = tmp_path / "wd.json"
        state = health_check.default_wd_state()
        state["last_run_at"] = time.time() - 60
        state_path.write_text(json.dumps(state), encoding="utf-8")
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: None)
        monkeypatch.setattr(health_check, "PAUSE_SENTINEL_PATH", str(tmp_path / "absent.pause"))
        kicked = []
        monkeypatch.setattr(health_check, "kickstart", lambda label: kicked.append(label) or True)
        monkeypatch.setattr(health_check, "notify", lambda *_a: True)
        rc = health_check.main(["--heal", "--heartbeat", str(path), "--state", str(state_path)])
        assert rc == 1
        assert kicked == ["com.alex.transcriber"]
        assert "heartbeat_not_daemon" in capsys.readouterr().out


class TestFrozenAliveEscalation:
    """WD-17 (LAG-753): the heal ladder must see a frozen-but-alive daemon.

    The 2026-09-18 App Nap freeze (LAG-694) ran for hours with the watchdog
    firing kickstarts that a live-but-SIGSTOPped PID simply ignored.
    """

    def setup_paths(self, monkeypatch, tmp_path):
        state_path = tmp_path / "wd.json"
        monkeypatch.setattr(health_check, "PAUSE_SENTINEL_PATH", str(tmp_path / "pause"))
        monkeypatch.setattr(health_check, "REBUILD_MARKER_PATH", str(tmp_path / "rebuild.json"))
        monkeypatch.setattr(health_check, "REPOINT_MARKER_PATH", str(tmp_path / "repoint.json"))
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        return state_path

    def stale_heartbeat(self, tmp_path):
        """A daemon-written heartbeat whose mtime is well past the threshold."""
        path = tmp_path / "hb.json"
        path.write_text(json.dumps(make_heartbeat(writer="daemon")), encoding="utf-8")
        old = time.time() - 4000
        os.utime(path, (old, old))
        return path

    # -- the pure predicate --------------------------------------------------
    def test_liveness_is_probed_only_for_a_stale_heartbeat(self, monkeypatch):
        """WD-17 keeps HC-17's economy: no extra subprocess off the stale path."""

        def forbidden(_label):
            raise AssertionError("only a stale heartbeat may probe for liveness (WD-17)")

        monkeypatch.setattr(health_check, "probe_daemon_pid", forbidden)
        for reason in (None, "heartbeat_missing", "service_not_loaded", "heartbeat_fatal"):
            assert health_check.daemon_frozen_alive({"reason": reason}, "com.alex.transcriber") is False

    def test_stale_heartbeat_with_a_printed_pid_is_frozen_alive(self, monkeypatch):
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: 8078)
        assert health_check.daemon_frozen_alive({"reason": "heartbeat_stale"}, "com.alex.transcriber") is True

    def test_stale_heartbeat_without_a_printed_pid_is_not_frozen_alive(self, monkeypatch):
        """HC-17's probe, not the sampled `launchctl list` column (HC-4/HC-5)."""
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: None)
        assert health_check.daemon_frozen_alive({"reason": "heartbeat_stale"}, "com.alex.transcriber") is False

    # -- wired through --heal ------------------------------------------------
    def test_heal_kills_and_restarts_a_frozen_daemon(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        path = self.stale_heartbeat(tmp_path)
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: 8078)
        kicked = []
        monkeypatch.setattr(health_check, "kickstart", lambda label, kill=False: kicked.append((label, kill)) or True)
        monkeypatch.setattr(health_check, "notify", lambda *_a: True)
        health_check.write_wd_state(state_path, wd_state())
        rc = health_check.main(["--heal", "--heartbeat", str(path), "--state", str(state_path)])
        assert rc == 1
        assert kicked == [("com.alex.transcriber", True)]
        assert "kickstart_kill" in capsys.readouterr().out
        assert health_check.load_wd_state(state_path)["last_action"] == "kickstart_kill"

    def test_heal_falls_back_to_a_plain_kickstart_when_the_pid_is_gone(self, tmp_path, monkeypatch):
        """WD-9/WD-6 path unchanged: a dead daemon needs no kill."""
        state_path = self.setup_paths(monkeypatch, tmp_path)
        path = self.stale_heartbeat(tmp_path)
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: None)
        kicked = []
        monkeypatch.setattr(health_check, "kickstart", lambda label, kill=False: kicked.append((label, kill)) or True)
        monkeypatch.setattr(health_check, "notify", lambda *_a: True)
        health_check.write_wd_state(state_path, wd_state())
        assert health_check.main(["--heal", "--heartbeat", str(path), "--state", str(state_path)]) == 1
        assert kicked == [("com.alex.transcriber", False)]

    def test_a_failed_probe_degrades_to_the_plain_kickstart(self, tmp_path, monkeypatch):
        """ES-6: a launchctl print that errors reads as not-alive, never raises."""
        state_path = self.setup_paths(monkeypatch, tmp_path)
        path = self.stale_heartbeat(tmp_path)

        def boom(_cmd, **_kwargs):
            raise OSError("launchctl unavailable")

        monkeypatch.setattr(subprocess, "run", boom)
        kicked = []
        monkeypatch.setattr(health_check, "kickstart", lambda label, kill=False: kicked.append((label, kill)) or True)
        monkeypatch.setattr(health_check, "notify", lambda *_a: True)
        monkeypatch.setattr(health_check, "run_launchctl_list", lambda: "8078\t0\tcom.alex.transcriber\n")
        health_check.write_wd_state(state_path, wd_state())
        assert health_check.main(["--heal", "--heartbeat", str(path), "--state", str(state_path)]) == 1
        assert kicked == [("com.alex.transcriber", False)]

    def test_heal_dry_run_kills_nothing(self, tmp_path, monkeypatch, capsys):
        state_path = self.setup_paths(monkeypatch, tmp_path)
        path = self.stale_heartbeat(tmp_path)
        monkeypatch.setattr(health_check, "probe_daemon_pid", lambda _label: 8078)
        kicked = []
        monkeypatch.setattr(health_check, "kickstart", lambda label, kill=False: kicked.append((label, kill)) or True)
        health_check.write_wd_state(state_path, wd_state())
        rc = health_check.main(["--heal", "--heartbeat", str(path), "--state", str(state_path), "--dry-run"])
        assert rc == 1
        assert kicked == []
        assert "dry-run" in capsys.readouterr().out
