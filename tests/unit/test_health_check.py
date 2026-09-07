"""health_check.py assessment tests (ADR 0009/0010, spec HC-*).

The 2026-09-07 incident is encoded here: the log was written by the failure
itself, launchctl columns lie in both directions, and the heartbeat file's
mtime is the only honest signal. assess_health and parse_launchctl_list are
pure functions (HC-7) — these tests also pin that purity.
"""

import json
import subprocess
import sys
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
