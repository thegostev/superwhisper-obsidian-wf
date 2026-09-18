"""Heartbeat writer tests (ADR 0009, spec HB-1..HB-12).

The heartbeat file is the daemon's liveness signal: a dyld-aborting interpreter
can never produce one, and staleness of this file is what the watchdog judges.
These tests pin the schema contract (v1), the atomicity/throttle/failure
discipline of the writer, and the RFC 3339 UTC timestamp format.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

import auto_transcribe
import pipeline

RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

PROJECT_DIR = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def fresh_writer_state():
    """Reset the writer's module-level throttle/failure/context state per test.

    The writer caches last-write time and context (cycle, counts) across calls;
    without a reset, one test's writes would throttle or pollute the next.
    """
    pipeline._heartbeat_last_write = 0.0
    pipeline._heartbeat_failures = 0
    pipeline._heartbeat_context = {"cycle": 0, "failed_permanent": 0, "state_complete": 0}
    pipeline._heartbeat_started_at = None
    pipeline._heartbeat_writer = pipeline.DEFAULT_HEARTBEAT_WRITER
    yield


@pytest.fixture
def heartbeat_path(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(path))
    return path


def read_heartbeat(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestSchemaV1:
    def test_write_creates_valid_json_payload(self, heartbeat_path):
        assert pipeline.write_heartbeat("scanning", force=True) is True
        payload = read_heartbeat(heartbeat_path)
        assert payload["schema"] == 1
        assert payload["phase"] == "scanning"
        assert payload["pid"] == os.getpid()

    def test_timestamps_are_rfc3339_utc(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", force=True)
        payload = read_heartbeat(heartbeat_path)
        assert RFC3339_UTC.match(payload["updated_at"])
        assert RFC3339_UTC.match(payload["started_at"])
        # The Z-suffix format must parse back as UTC — naive local timestamps are
        # forbidden (HB-9): they are ambiguous across DST transitions.
        datetime.strptime(payload["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
        datetime.strptime(payload["started_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_write_is_atomic_no_tmp_left_behind(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", force=True)
        assert not heartbeat_path.with_suffix(heartbeat_path.suffix + ".tmp").exists()
        assert heartbeat_path.exists()

    def test_fatal_phase_carries_fatal_reason(self, heartbeat_path):
        pipeline.write_heartbeat("fatal", fatal_reason="FatalAPIError: mode key empty", force=True)
        payload = read_heartbeat(heartbeat_path)
        assert payload["phase"] == "fatal"
        assert payload["fatal_reason"] == "FatalAPIError: mode key empty"

    def test_non_fatal_phase_has_no_fatal_reason_key(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", force=True)
        assert "fatal_reason" not in read_heartbeat(heartbeat_path)


class TestContextCarrying:
    def test_cycle_and_counts_carry_across_throttled_callers(self, heartbeat_path):
        """Call sites that know cycle/counts set them; later call sites that don't
        (poll loop, handoff) must still emit the last known values — the heartbeat
        must always carry failed_permanent (HB-6)."""
        pipeline.write_heartbeat("scanning", cycle=42, failed_permanent=2, state_complete=9, force=True)
        pipeline.write_heartbeat("processing", force=True)  # knows none of the context
        payload = read_heartbeat(heartbeat_path)
        assert payload["cycle"] == 42
        assert payload["failed_permanent"] == 2
        assert payload["state_complete"] == 9

    def test_context_updatable_later(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", cycle=1, force=True)
        pipeline.write_heartbeat("scanning", cycle=2, failed_permanent=1, force=True)
        payload = read_heartbeat(heartbeat_path)
        assert payload["cycle"] == 2
        assert payload["failed_permanent"] == 1


class TestThrottle:
    def test_second_write_within_interval_is_suppressed(self, heartbeat_path):
        assert pipeline.write_heartbeat("scanning", force=True) is True
        mtime1 = heartbeat_path.stat().st_mtime
        assert pipeline.write_heartbeat("processing") is False  # within 15s throttle
        assert heartbeat_path.stat().st_mtime == mtime1

    def test_force_bypasses_throttle(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", force=True)
        assert pipeline.write_heartbeat("processing", force=True) is True

    def test_fatal_write_is_never_throttled(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", force=True)
        # PF-16: the fatal write MUST land before the process exits — the writer
        # itself forces it so a caller cannot forget.
        assert pipeline.write_heartbeat("fatal", fatal_reason="boom") is True


class TestFailureDiscipline:
    def test_write_failure_never_raises(self, heartbeat_path, monkeypatch, capsys):
        def boom(src, dst):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(pipeline.os, "replace", boom)
        assert pipeline.write_heartbeat("scanning", force=True) is False
        assert "⚠️" in capsys.readouterr().out

    def test_consecutive_failures_warn_with_likely_cause(self, heartbeat_path, monkeypatch, capsys):
        def boom(src, dst):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(pipeline.os, "replace", boom)
        for _ in range(pipeline.HEARTBEAT_WRITE_FAILURE_WARN_THRESHOLD):
            pipeline.write_heartbeat("scanning", force=True)
        out = capsys.readouterr().out
        # HB-10: past the threshold the warning names the runbook cause — a daemon
        # that cannot write its heartbeat is indistinguishable from a dead one.
        assert "disk" in out.lower() or "permission" in out.lower()

    def test_success_resets_failure_count(self, heartbeat_path, monkeypatch):
        real_replace = pipeline.os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        monkeypatch.setattr(pipeline.os, "replace", flaky)
        pipeline.write_heartbeat("scanning", force=True)
        assert pipeline.write_heartbeat("scanning", force=True) is True
        assert pipeline._heartbeat_failures == 0

    def test_missing_parent_directory_is_created(self, tmp_path, monkeypatch):
        path = tmp_path / "sub" / "dir" / "heartbeat.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(path))
        assert pipeline.write_heartbeat("scanning", force=True) is True
        assert path.exists()


class TestCallSites:
    def test_handoff_writes_forced_heartbeat(self, heartbeat_path, tmp_path, monkeypatch):
        audio = tmp_path / "audio.m4a"
        audio.write_bytes(b"x")
        monkeypatch.setattr(pipeline, "_wait_for_superwhisper_idle", lambda: None)
        monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: None)
        pipeline._heartbeat_last_write = 0.0
        pipeline.handoff_to_superwhisper(str(audio))
        payload = read_heartbeat(heartbeat_path)
        assert payload["phase"] == "processing"

    def test_idle_wait_loop_writes_heartbeat(self, heartbeat_path, monkeypatch):
        # _wait_for_superwhisper_idle busy for 2 iterations then idle.
        polls = {"n": 0}

        def busy_then_idle():
            polls["n"] += 1
            return polls["n"] > 2

        monkeypatch.setattr(pipeline, "_is_superwhisper_idle", busy_then_idle)
        monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
        pipeline._heartbeat_last_write = 0.0
        pipeline._wait_for_superwhisper_idle(timeout=10)
        payload = read_heartbeat(heartbeat_path)
        assert payload["phase"] == "processing"

    def test_transcript_index_walk_writes_heartbeat(self, heartbeat_path, tmp_path, monkeypatch):
        base = tmp_path / "vault"
        base.mkdir()
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(heartbeat_path))
        pipeline._heartbeat_last_write = 0.0
        pipeline.build_transcript_index({"WORK": str(base)})
        payload = read_heartbeat(heartbeat_path)
        assert payload["phase"] == "starting"


class _FirstHeartbeatError(Exception):
    """Sentinel: aborts the daemon main() at its first heartbeat write."""


class TestWriterField:
    """HB-12 (LAG-675): the heartbeat names the process that wrote it.

    An ops script reaches write_heartbeat through the shared pipeline
    functions, so a manual run refreshes the very file the watchdog reads.
    The writer identity is what lets the reader tell the two apart.
    """

    def test_default_writer_is_manual(self, heartbeat_path):
        """Fail-safe default: a caller that never declares itself is manual, so
        forgetting the declaration cannot let an ops run pose as the daemon."""
        assert pipeline.DEFAULT_HEARTBEAT_WRITER == "manual"
        pipeline.write_heartbeat("processing", force=True)
        assert read_heartbeat(heartbeat_path)["writer"] == "manual"

    def test_daemon_declares_itself(self, heartbeat_path):
        pipeline.set_heartbeat_writer("daemon")
        pipeline.write_heartbeat("scanning", force=True)
        assert read_heartbeat(heartbeat_path)["writer"] == "daemon"

    def test_writer_is_sticky_across_writes(self, heartbeat_path):
        pipeline.set_heartbeat_writer("daemon")
        pipeline.write_heartbeat("starting", force=True)
        pipeline.write_heartbeat("scanning", force=True)
        assert read_heartbeat(heartbeat_path)["writer"] == "daemon"

    def test_fatal_heartbeat_carries_the_writer_too(self, heartbeat_path):
        pipeline.set_heartbeat_writer("daemon")
        pipeline.write_heartbeat("fatal", fatal_reason="boom")
        payload = read_heartbeat(heartbeat_path)
        assert payload["phase"] == "fatal"
        assert payload["writer"] == "daemon"

    def test_unknown_writer_is_rejected(self):
        with pytest.raises(ValueError, match="writer"):
            pipeline.set_heartbeat_writer("cron")

    def test_schema_version_is_unchanged(self, heartbeat_path):
        """The writer field is additive: pre-HB-12 readers must keep working,
        so schema v1 stays v1 (the reader defaults an absent writer to daemon)."""
        pipeline.write_heartbeat("scanning", force=True)
        assert read_heartbeat(heartbeat_path)["schema"] == 1


class TestDaemonEntryPointDeclaresItself:
    def test_daemon_main_declares_daemon_before_first_heartbeat(self, monkeypatch):
        """HB-12: the daemon is the one caller that declares itself, and it must
        do so before the first startup heartbeat (HB-7 milestone 1/4) — a
        heartbeat written before the declaration would read as manual."""
        seen = []

        def record(writer):
            seen.append(writer)

        def stop(*_args, **_kwargs):
            raise _FirstHeartbeatError

        monkeypatch.setattr(auto_transcribe, "set_heartbeat_writer", record)
        monkeypatch.setattr(auto_transcribe, "write_heartbeat", stop)
        with pytest.raises(_FirstHeartbeatError):
            auto_transcribe.main()
        assert seen == ["daemon"]

    def test_ops_entry_points_do_not_declare_daemon(self):
        """Only the daemon may declare itself; every other entry point inherits
        the manual default. Guards against a copy-paste into an ops script."""
        for module in ("ondemand_transcribe.py", "redrive_ops.py", "salvage_ops.py", "reclassify_and_fix.py"):
            source = (PROJECT_DIR / module).read_text(encoding="utf-8")
            assert 'set_heartbeat_writer("daemon")' not in source, module
