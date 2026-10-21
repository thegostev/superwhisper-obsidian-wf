"""iCloud placeholder telemetry in the heartbeat (LAG-814, spec HB-14).

On Oct 7 the daemon logged "No new files" for ~20 h while three meetings sat
in the JPR tree as iCloud placeholder stubs (``.<name>.m4a.icloud``). The
heartbeat could not tell "nothing recorded" from "recorded but stuck
upstream". These tests pin the count the heartbeat now carries: placeholders
vs materialized files, nested dirs, scan-window scope, the name-only match
(a full-path substring test is the known path-scope trap), and the schema.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import auto_transcribe
import pipeline
from pipeline import count_icloud_placeholders, is_icloud_audio_placeholder, write_heartbeat


@pytest.fixture(autouse=True)
def fresh_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    pipeline._heartbeat_last_write = 0.0
    pipeline._heartbeat_icloud_placeholders = None
    yield
    pipeline._heartbeat_icloud_placeholders = None
    pipeline._heartbeat_last_write = 0.0


def _heartbeat() -> dict:
    return json.loads(Path(pipeline.HEARTBEAT_FILE).read_text(encoding="utf-8"))


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


# --- the predicate: name-only, audio stubs only ------------------------------


class TestPlaceholderPredicate:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (".09-58-36.m4a.icloud", True),
            (".09-58-36.M4A.icloud", True),
            ("09-58-36.m4a", False),  # materialized
            (".09-58-36.m4a", False),  # hidden, not a stub
            (".notes.txt.icloud", False),  # non-audio stub
            (".icloud", False),
            ("..icloud", False),
            ("09-58-36.m4a.icloud", False),  # stubs always start with a dot
        ],
    )
    def test_names(self, name, expected):
        assert is_icloud_audio_placeholder(Path("/jpr/2026-10-07") / name) is expected

    def test_dot_icloud_in_a_parent_path_does_not_make_a_stub(self):
        """Path-scope trap: only the file name decides, never the full path."""
        assert is_icloud_audio_placeholder(Path("/x/.icloud/2026-10-07/09-58-36.m4a")) is False


# --- the count ---------------------------------------------------------------


class TestCount:
    def test_counts_placeholders_not_materialized_files(self, tmp_path):
        day = tmp_path / "2026-10-07"
        _touch(day / ".08-32-00.m4a.icloud")
        _touch(day / ".09-58-36.m4a.icloud")
        _touch(day / "07-15-00.m4a")
        assert count_icloud_placeholders([day]) == {"total": 2, "by_date_dir": {"2026-10-07": 2}}

    def test_per_date_dir_lists_only_pending_dirs(self, tmp_path):
        oct6, oct7 = tmp_path / "2026-10-06", tmp_path / "2026-10-07"
        _touch(oct6 / ".19-35-38.m4a.icloud")
        _touch(oct7 / "08-32-00.m4a")  # fully materialized day
        assert count_icloud_placeholders([oct6, oct7]) == {"total": 1, "by_date_dir": {"2026-10-06": 1}}

    def test_nested_dirs_roll_up_to_their_date_dir(self, tmp_path):
        day = tmp_path / "2026-10-07"
        _touch(day / "sub" / "deeper" / ".10-00-00.m4a.icloud")
        _touch(day / ".11-00-00.m4a.icloud")
        assert count_icloud_placeholders([day]) == {"total": 2, "by_date_dir": {"2026-10-07": 2}}

    def test_non_audio_stubs_are_ignored(self, tmp_path):
        day = tmp_path / "2026-10-07"
        _touch(day / ".settings.plist.icloud")
        assert count_icloud_placeholders([day]) == {"total": 0, "by_date_dir": {}}

    def test_materialized_file_under_a_dot_icloud_path_is_not_counted(self, tmp_path):
        day = tmp_path / ".icloud" / "2026-10-07"
        _touch(day / "09-58-36.m4a")
        assert count_icloud_placeholders([day]) == {"total": 0, "by_date_dir": {}}

    def test_empty_and_missing_folders_count_zero_without_raising(self, tmp_path):
        (tmp_path / "2026-10-07").mkdir()
        assert count_icloud_placeholders([tmp_path / "2026-10-07", tmp_path / "gone"]) == {
            "total": 0,
            "by_date_dir": {},
        }

    def test_unreadable_folder_is_skipped_and_logged(self, tmp_path, capsys):
        good = tmp_path / "2026-10-07"
        _touch(good / ".09-58-36.m4a.icloud")
        bad = tmp_path / "2026-10-06"
        bad.mkdir()
        real_rglob = Path.rglob

        def rglob(self, pattern):
            if self == bad:
                raise PermissionError("Operation not permitted")
            return real_rglob(self, pattern)

        with patch.object(Path, "rglob", rglob):
            result = count_icloud_placeholders([bad, good])
        assert result == {"total": 1, "by_date_dir": {"2026-10-07": 1}}
        assert "2026-10-06" in capsys.readouterr().out

    def test_scan_window_scope_excludes_old_date_dirs(self, tmp_path, monkeypatch):
        """Only the date dirs the daemon scans count: old Optimize-Storage stubs are not 'pending'."""
        fixed_now = pipeline.datetime(2026, 10, 8, 12, 0)
        monkeypatch.setattr(pipeline, "_now_local", lambda: fixed_now)
        _touch(tmp_path / "2026-10-07" / ".09-58-36.m4a.icloud")
        _touch(tmp_path / "2026-08-01" / ".10-00-00.m4a.icloud")
        folders = pipeline.discover_recent_folders(str(tmp_path), days_back=7)
        assert count_icloud_placeholders(folders) == {"total": 1, "by_date_dir": {"2026-10-07": 1}}


# --- HB-14: heartbeat schema ---------------------------------------------------


class TestHeartbeatField:
    def test_null_until_first_count(self):
        write_heartbeat("starting", force=True)
        assert _heartbeat()["icloud_placeholders"] is None

    def test_written_when_passed(self):
        count = {"total": 3, "by_date_dir": {"2026-10-07": 3}}
        write_heartbeat("scanning", icloud_placeholders=count, force=True)
        assert _heartbeat()["icloud_placeholders"] == count

    def test_carried_across_writers_that_do_not_know_it(self):
        write_heartbeat("scanning", icloud_placeholders={"total": 1, "by_date_dir": {"2026-10-07": 1}}, force=True)
        write_heartbeat("processing", force=True)
        assert _heartbeat()["icloud_placeholders"]["total"] == 1

    def test_cached_even_when_the_write_is_throttled(self):
        write_heartbeat("scanning", force=True)
        assert write_heartbeat("scanning", icloud_placeholders={"total": 2, "by_date_dir": {"x": 2}}) is False
        write_heartbeat("processing", force=True)
        assert _heartbeat()["icloud_placeholders"]["total"] == 2

    def test_zero_is_reported_not_null(self):
        """Zero means 'counted, nothing stuck' — distinct from null 'not counted yet'."""
        write_heartbeat("scanning", icloud_placeholders={"total": 0, "by_date_dir": {}}, force=True)
        assert _heartbeat()["icloud_placeholders"] == {"total": 0, "by_date_dir": {}}


# --- daemon wiring -------------------------------------------------------------


class TestScanCycleWiring:
    def test_scan_cycle_publishes_the_count(self, tmp_path, monkeypatch):
        day = tmp_path / "2026-10-07"
        _touch(day / ".09-58-36.m4a.icloud")
        monkeypatch.setattr(auto_transcribe, "WATCH_FOLDER", str(tmp_path))
        monkeypatch.setattr(auto_transcribe, "discover_recent_folders", lambda *_a, **_k: [str(day)])
        monkeypatch.setattr(auto_transcribe, "discover_audio_files", lambda *_a, **_k: [])
        auto_transcribe.run_scan_cycle({"processed": {}}, {}, 1)
        assert _heartbeat()["icloud_placeholders"] == {"total": 1, "by_date_dir": {"2026-10-07": 1}}

    def test_count_failure_never_breaks_the_scan_cycle(self, monkeypatch, capsys):
        def boom(*_a, **_k):
            raise RuntimeError("iCloud on fire")

        monkeypatch.setattr(auto_transcribe, "count_icloud_placeholders", boom)
        monkeypatch.setattr(auto_transcribe, "discover_audio_files", lambda *_a, **_k: [])
        assert auto_transcribe.run_scan_cycle({"processed": {}}, {}, 1) == 0
        assert "iCloud" in capsys.readouterr().out
        assert os.path.exists(pipeline.HEARTBEAT_FILE)
