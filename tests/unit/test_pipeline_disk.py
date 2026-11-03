"""Pre-handoff free-space gate (LAG-909, spec DS-1..DS-5).

The 2026-10-30 incident is encoded here: the Data volume hit 100%, the daemon
kept handing off, and Superwhisper wrote a truncated ``output.wav`` with no
``meta.json``. The gate must refuse the handoff while free space is below the
configured floor, record a retryable outcome, and never let the file age into
``failed_permanent`` because the disk was full.

The probe is monkeypatched at ``pipeline.shutil.disk_usage`` so boundaries are
exact: 4.99/5.00/5.01 GiB against the 5 GiB default.
"""

import json
from collections import namedtuple
from datetime import datetime

import pytest

import pipeline

GIB = 1024**3
Usage = namedtuple("usage", "total used free")


def use_free(monkeypatch, free_bytes):
    """Make every filesystem report the same free space."""
    monkeypatch.setattr(
        pipeline.shutil,
        "disk_usage",
        lambda _path: Usage(500 * GIB, 500 * GIB - free_bytes, free_bytes),
    )


def use_free_by_path(monkeypatch, free_by_path):
    """Map probe anchor path → free bytes; anything else is plentiful."""
    monkeypatch.setattr(
        pipeline.shutil,
        "disk_usage",
        lambda path: Usage(500 * GIB, 0, free_by_path.get(str(path), 100 * GIB)),
    )


@pytest.fixture
def disk_roots(tmp_path, monkeypatch):
    """A recordings store and two output roots, all under tmp_path."""
    recordings = tmp_path / "recordings"
    work = tmp_path / "work"
    personal = tmp_path / "personal"
    for directory in (recordings, work, personal):
        directory.mkdir()
    monkeypatch.setattr(pipeline, "SUPERWHISPER_RECORDINGS_DIR", str(recordings))
    monkeypatch.setattr(pipeline, "FOLDERS", {"WORK": str(work), "PERSONAL": str(personal)})
    return recordings, work, personal


class TestDiskProbe:
    def test_default_floor_is_five_gib(self):
        assert pipeline.DISK_MIN_FREE_GB == 5.0

    def test_snapshot_reports_every_root(self, disk_roots, monkeypatch):
        use_free(monkeypatch, 8 * GIB)
        snapshot = pipeline.disk_snapshot()
        roles = set(snapshot["volumes"])
        assert {"recordings", "output:WORK", "output:PERSONAL"} <= roles

    def test_unprobeable_path_reads_as_unknown_not_zero(self, tmp_path, monkeypatch):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(pipeline, "SUPERWHISPER_RECORDINGS_DIR", str(missing))
        monkeypatch.setattr(pipeline, "FOLDERS", {})
        monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: (_ for _ in ()).throw(OSError("nope")))
        snapshot = pipeline.disk_snapshot()
        assert snapshot["min_free_bytes"] is None
        assert snapshot["volumes"]["recordings"]["free_bytes"] is None


class TestFreeSpaceGate:
    @pytest.mark.parametrize(
        ("free_gib", "blocked"),
        [(4.99, True), (5.00, False), (5.01, False)],
    )
    def test_boundary_against_default_floor(self, disk_roots, monkeypatch, free_gib, blocked):
        use_free(monkeypatch, int(free_gib * GIB))
        if blocked:
            with pytest.raises(pipeline.InsufficientDiskError):
                pipeline.check_disk_space()
        else:
            snapshot = pipeline.check_disk_space()
            assert snapshot["min_free_gb"] == pytest.approx(free_gib)

    def test_lowest_root_wins_when_recordings_is_tighter(self, disk_roots, monkeypatch):
        recordings, work, _ = disk_roots
        use_free_by_path(monkeypatch, {str(recordings): int(4.9 * GIB), str(work): 9 * GIB})
        with pytest.raises(pipeline.InsufficientDiskError):
            pipeline.check_disk_space()

    def test_lowest_root_wins_when_an_output_is_tighter(self, disk_roots, monkeypatch):
        recordings, work, _ = disk_roots
        use_free_by_path(monkeypatch, {str(recordings): 9 * GIB, str(work): int(4.9 * GIB)})
        with pytest.raises(pipeline.InsufficientDiskError):
            pipeline.check_disk_space()

    def test_error_names_the_free_space_and_floor(self, disk_roots, monkeypatch):
        use_free(monkeypatch, int(4.9 * GIB))
        with pytest.raises(pipeline.InsufficientDiskError) as excinfo:
            pipeline.check_disk_space()
        assert "4.90" in str(excinfo.value)
        assert "5.00" in str(excinfo.value)


class TestNoHandoffOnLowDisk:
    def test_no_handoff_and_failed_retry_outcome(self, disk_roots, tmp_path, monkeypatch):
        audio = tmp_path / "2026-11-03" / "09-00-00.m4a"
        audio.parent.mkdir()
        audio.write_bytes(b"not-really-audio")
        monkeypatch.setattr(pipeline, "STATE_FILE", str(tmp_path / "state.json"))
        monkeypatch.setattr(pipeline, "get_audio_duration_ms", lambda _p: None)
        use_free(monkeypatch, int(4.9 * GIB))
        calls = []
        monkeypatch.setattr(pipeline, "switch_superwhisper_mode", lambda: calls.append("mode"))
        monkeypatch.setattr(pipeline, "handoff_to_superwhisper", lambda path: calls.append(path) or 0.0)

        state = {"processed": {}}
        ok, category = pipeline.process_audio(str(audio), datetime(2026, 11, 3, 9, 0), state)

        assert ok is False and category is None
        assert calls == [], "nothing may be opened in Superwhisper below the floor"
        record = state["processed"][str(audio)]
        assert record["status"] == "failed_retry"
        assert record["outcome"] == pipeline.OUTCOME_INSUFFICIENT_DISK
        assert record["disk_free_gb"] == pytest.approx(4.9, abs=0.01)

    def test_repeated_low_disk_never_becomes_permanent(self, disk_roots, tmp_path, monkeypatch):
        audio = tmp_path / "2026-11-03" / "10-00-00.m4a"
        audio.parent.mkdir()
        audio.write_bytes(b"not-really-audio")
        monkeypatch.setattr(pipeline, "STATE_FILE", str(tmp_path / "state.json"))
        monkeypatch.setattr(pipeline, "get_audio_duration_ms", lambda _p: None)
        use_free(monkeypatch, int(4.9 * GIB))
        monkeypatch.setattr(pipeline, "switch_superwhisper_mode", lambda: None)
        monkeypatch.setattr(pipeline, "handoff_to_superwhisper", lambda path: 0.0)

        state = {"processed": {}}
        for _ in range(pipeline.MAX_RETRIES + 1):
            pipeline.process_audio(str(audio), datetime(2026, 11, 3, 10, 0), state)

        assert state["processed"][str(audio)]["status"] == "failed_retry"
        assert state["processed"][str(audio)]["attempts"] == pipeline.MAX_RETRIES + 1


class TestHeartbeatDiskTelemetry:
    def test_heartbeat_carries_disk_fields(self, disk_roots, tmp_path, monkeypatch):
        heartbeat = tmp_path / "heartbeat.json"
        monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(heartbeat))
        pipeline._heartbeat_last_write = 0.0
        pipeline._heartbeat_failures = 0
        recordings, _, _ = disk_roots
        (recordings / "123").mkdir()
        (recordings / "123" / "output.wav").write_bytes(b"x" * 4096)
        use_free(monkeypatch, 6 * GIB)

        assert pipeline.write_heartbeat("scanning", force=True) is True
        payload = json.loads(heartbeat.read_text(encoding="utf-8"))

        assert payload["schema"] == pipeline.HEARTBEAT_SCHEMA_VERSION == 2
        assert payload["disk_free_bytes"] == 6 * GIB
        assert payload["disk_free_gb"] == pytest.approx(6.0)
        assert payload["recordings_bytes"] >= 4096
        assert set(payload["disk_volumes"]) >= {"recordings", "output:WORK", "output:PERSONAL"}
