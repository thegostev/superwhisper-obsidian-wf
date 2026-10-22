"""iCloud placeholder preflight tests (LAG-813, spec IC-1..IC-6).

The daemon used to skip `.<name>.m4a.icloud` stubs and never ask iCloud to
download them (LAG-756), so recordings sat invisible for up to two days. These
tests pin stub detection, the `brctl download` request discipline (timeout,
cooldown, per-cycle cap, never-raise), the log/heartbeat surface, the daemon
wiring, and the CLI — including a real-subprocess smoke run against a stub
`brctl` script.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import auto_transcribe
import icloud_preflight
import pipeline
from icloud_preflight import (
    PreflightResult,
    find_placeholders,
    placeholder_target,
    request_downloads,
    run_preflight,
)

PROJECT_DIR = Path(__file__).parents[2]


def _stub(folder: Path, name: str) -> Path:
    """Create an iCloud placeholder stub for ``name`` inside ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    stub = folder / f".{name}.icloud"
    stub.write_bytes(b"bplist00")  # real stubs are small binary plists
    return stub


class FakeRunner:
    """``subprocess.run`` stand-in recording argv and replaying scripted outcomes."""

    def __init__(self, outcomes=None):
        self.calls: list[tuple[list[str], dict]] = []
        self._outcomes = list(outcomes or [])

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        outcome = self._outcomes.pop(0) if self._outcomes else 0
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(argv, outcome, stdout="", stderr="boom" if outcome else "")


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# IC-1 / IC-2: detection
# ---------------------------------------------------------------------------


class TestPlaceholderDetection:
    def test_audio_stub_maps_to_real_file(self, tmp_path):
        assert placeholder_target(tmp_path / ".08-32-00.m4a.icloud") == tmp_path / "08-32-00.m4a"

    def test_suffix_match_is_case_insensitive(self, tmp_path):
        assert placeholder_target(tmp_path / ".rec.M4A.icloud") == tmp_path / "rec.M4A"

    @pytest.mark.parametrize(
        "name",
        [
            "08-32-00.m4a",  # materialized file
            ".08-32-00.m4a",  # hidden file, not a stub
            "08-32-00.m4a.icloud",  # missing the leading dot
            ".notes.txt.icloud",  # non-audio stub
            ".icloud",  # degenerate
            "..icloud",  # degenerate
            "..hidden.m4a.icloud",  # stub of a hidden file
        ],
    )
    def test_non_audio_stubs_and_regular_files_are_not_placeholders(self, tmp_path, name):
        assert placeholder_target(tmp_path / name) is None

    def test_finds_stubs_in_date_dirs_and_nested_dirs(self, tmp_path):
        day = tmp_path / "2026-10-07"
        a = _stub(day, "10-55-00.m4a")
        b = _stub(day / "nested" / "deeper", "14-00-00.m4a")
        (day / "15-02-00.m4a").write_bytes(b"audio")  # already materialized
        _stub(day, "notes.txt")  # non-audio stub

        assert find_placeholders([day]) == sorted([a, b])

    def test_only_given_folders_are_searched(self, tmp_path):
        recent = tmp_path / "2026-10-07"
        old = tmp_path / "2025-01-01"
        wanted = _stub(recent, "10-55-00.m4a")
        _stub(old, "09-00-00.m4a")  # outside the scan window (IC-2)

        assert find_placeholders([recent]) == [wanted]

    def test_missing_folder_yields_nothing(self, tmp_path):
        assert find_placeholders([tmp_path / "does-not-exist"]) == []

    def test_no_stubs_is_empty(self, tmp_path):
        day = tmp_path / "2026-10-07"
        day.mkdir()
        (day / "10-55-00.m4a").write_bytes(b"audio")
        assert find_placeholders([day]) == []

    def test_stub_seen_via_two_folders_is_listed_once(self, tmp_path):
        day = tmp_path / "2026-10-07"
        stub = _stub(day / "sub", "a.m4a")
        assert find_placeholders([day, day / "sub"]) == [stub]

    def test_unreadable_folder_is_logged_not_raised(self, tmp_path, monkeypatch, capsys):
        def broken_rglob(self, pattern):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(Path, "rglob", broken_rglob)
        assert find_placeholders([tmp_path]) == []
        assert "cannot scan" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# IC-3 / IC-4: brctl request discipline
# ---------------------------------------------------------------------------


class TestRequestDownloads:
    def test_runs_brctl_download_per_stub_with_timeout(self, tmp_path):
        stubs = [tmp_path / ".a.m4a.icloud", tmp_path / ".b.m4a.icloud"]
        runner = FakeRunner()

        result = request_downloads(stubs, timeout=7.5, runner=runner, brctl="/usr/bin/brctl")

        assert [argv for argv, _ in runner.calls] == [
            ["/usr/bin/brctl", "download", str(stubs[0])],
            ["/usr/bin/brctl", "download", str(stubs[1])],
        ]
        assert all(kw["timeout"] == 7.5 for _, kw in runner.calls)
        assert all(kw["capture_output"] for _, kw in runner.calls)
        assert (result.found, result.requested) == (2, 2)

    def test_empty_input_is_a_no_op(self):
        runner = FakeRunner()
        result = request_downloads([], runner=runner)
        assert runner.calls == []
        assert result == PreflightResult()

    def test_timeout_is_logged_and_the_pass_continues(self, tmp_path, capsys):
        stubs = [tmp_path / ".a.m4a.icloud", tmp_path / ".b.m4a.icloud"]
        runner = FakeRunner([subprocess.TimeoutExpired(cmd="brctl", timeout=10), 0])

        result = request_downloads(stubs, runner=runner)

        assert len(runner.calls) == 2
        assert (result.timed_out, result.requested) == (1, 1)
        assert "timed out" in capsys.readouterr().out

    def test_non_zero_exit_is_counted_and_logged(self, tmp_path, capsys):
        runner = FakeRunner([1])
        result = request_downloads([tmp_path / ".a.m4a.icloud"], runner=runner)
        assert (result.failed, result.requested) == (1, 0)
        assert "exited 1" in capsys.readouterr().out

    def test_missing_brctl_stops_further_calls(self, tmp_path, capsys):
        stubs = [tmp_path / f".{i}.m4a.icloud" for i in range(3)]
        runner = FakeRunner([FileNotFoundError("no brctl")])

        result = request_downloads(stubs, runner=runner)

        assert len(runner.calls) == 1
        assert result.brctl_missing
        assert result.deferred == 2
        assert "cannot run" in capsys.readouterr().out

    def test_unexpected_error_never_escapes_through_run_preflight(self, tmp_path):
        _stub(tmp_path, "a.m4a")
        result = run_preflight([tmp_path], runner=FakeRunner([PermissionError("denied")]))
        assert result.brctl_missing

    def test_per_cycle_cap_defers_the_rest(self, tmp_path):
        stubs = [tmp_path / f".{i}.m4a.icloud" for i in range(5)]
        runner = FakeRunner()

        result = request_downloads(stubs, max_requests=2, runner=runner)

        assert len(runner.calls) == 2
        assert (result.requested, result.deferred) == (2, 3)

    def test_cooldown_suppresses_re_requests_until_it_expires(self, tmp_path):
        stub = tmp_path / ".a.m4a.icloud"
        stamps: dict[str, float] = {}
        clock = FakeClock(1000.0)
        runner = FakeRunner()

        first = request_downloads([stub], cooldown=300, last_requested=stamps, runner=runner, clock=clock)
        clock.now += 299
        second = request_downloads([stub], cooldown=300, last_requested=stamps, runner=runner, clock=clock)
        clock.now += 1
        third = request_downloads([stub], cooldown=300, last_requested=stamps, runner=runner, clock=clock)

        assert (first.requested, second.requested, third.requested) == (1, 0, 1)
        assert second.skipped_cooldown == 1
        assert len(runner.calls) == 2

    def test_failed_attempt_still_starts_the_cooldown(self, tmp_path):
        """A hanging brctl must not be hammered every 30 s scan cycle."""
        stub = tmp_path / ".a.m4a.icloud"
        stamps: dict[str, float] = {}
        runner = FakeRunner([subprocess.TimeoutExpired(cmd="brctl", timeout=10)])
        clock = FakeClock()

        request_downloads([stub], last_requested=stamps, runner=runner, clock=clock)
        again = request_downloads([stub], last_requested=stamps, runner=runner, clock=clock)

        assert again.skipped_cooldown == 1
        assert len(runner.calls) == 1

    def test_dry_run_runs_nothing_and_stamps_nothing(self, tmp_path, capsys):
        stubs = [tmp_path / ".a.m4a.icloud", tmp_path / ".b.m4a.icloud"]
        stamps: dict[str, float] = {}
        runner = FakeRunner()

        result = request_downloads(stubs, dry_run=True, last_requested=stamps, runner=runner)

        assert runner.calls == []
        assert stamps == {}
        assert (result.would_request, result.requested, result.dry_run) == (2, 0, True)
        assert capsys.readouterr().out.count("[dry-run] would run") == 2


# ---------------------------------------------------------------------------
# IC-5: log + heartbeat surface
# ---------------------------------------------------------------------------


@pytest.fixture
def heartbeat_path(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(pipeline, "HEARTBEAT_FILE", str(path))
    monkeypatch.setattr(pipeline, "_heartbeat_last_write", 0.0)
    monkeypatch.setattr(pipeline, "_heartbeat_icloud_downloads_requested", 0)
    return path


class TestSummaryAndHeartbeat:
    def test_no_placeholders_is_silent(self, tmp_path, capsys):
        (tmp_path / "2026-10-07").mkdir()
        result = run_preflight([tmp_path / "2026-10-07"], runner=FakeRunner())
        assert result.found == 0
        assert capsys.readouterr().out == ""

    def test_summary_line_carries_the_counts(self, tmp_path, capsys):
        for i in range(3):
            _stub(tmp_path, f"{i}.m4a")
        run_preflight([tmp_path], max_requests=2, runner=FakeRunner([0, 1]))
        line = [ln for ln in capsys.readouterr().out.splitlines() if "iCloud preflight:" in ln][-1]
        assert "3 placeholder(s)" in line
        assert "requested 1 download(s)" in line
        assert "1 failed" in line
        assert "1 deferred to next cycle" in line

    def test_dry_run_summary_says_so(self, tmp_path, capsys):
        _stub(tmp_path, "a.m4a")
        run_preflight([tmp_path], dry_run=True, runner=FakeRunner())
        assert "would request 1 download(s) [dry-run]" in capsys.readouterr().out

    def test_heartbeat_defaults_to_zero(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", force=True)
        assert json.loads(heartbeat_path.read_text())["icloud_downloads_requested"] == 0

    def test_throttled_write_still_caches_the_count(self, heartbeat_path):
        """run_scan_cycle reports the count right after the cycle heartbeat, inside the throttle window."""
        assert pipeline.write_heartbeat("scanning", force=True)
        assert not pipeline.write_heartbeat("scanning", icloud_downloads_requested=5)  # throttled
        assert json.loads(heartbeat_path.read_text())["icloud_downloads_requested"] == 0
        pipeline.write_heartbeat("processing", force=True)
        assert json.loads(heartbeat_path.read_text())["icloud_downloads_requested"] == 5

    def test_heartbeat_carries_the_count_and_caches_it(self, heartbeat_path):
        pipeline.write_heartbeat("scanning", icloud_downloads_requested=4, force=True)
        pipeline.write_heartbeat("processing", force=True)  # caller that does not know the count
        payload = json.loads(heartbeat_path.read_text())
        assert payload["icloud_downloads_requested"] == 4
        assert payload["schema"] == pipeline.HEARTBEAT_SCHEMA_VERSION  # additive, no bump


# ---------------------------------------------------------------------------
# Daemon wiring
# ---------------------------------------------------------------------------


class TestDaemonWiring:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.setattr(auto_transcribe, "_icloud_last_requested", {})
        monkeypatch.setattr(auto_transcribe, "ICLOUD_PREFLIGHT_ENABLED", True)
        monkeypatch.setattr(auto_transcribe, "ICLOUD_PREFLIGHT_DRY_RUN", False)

    def test_preflight_searches_the_scan_window(self, tmp_path, monkeypatch):
        seen = {}

        def fake_discover(watch_folder, days_back):
            seen["args"] = (watch_folder, days_back)
            return [str(tmp_path / "2026-10-07")]

        def fake_run_preflight(folders, **kwargs):
            seen["folders"] = list(folders)
            seen["kwargs"] = kwargs
            return PreflightResult(found=2, requested=2)

        monkeypatch.setattr(auto_transcribe, "discover_recent_folders", fake_discover)
        monkeypatch.setattr(auto_transcribe, "run_preflight", fake_run_preflight)

        assert auto_transcribe.run_icloud_preflight() == 2
        assert seen["args"] == (auto_transcribe.WATCH_FOLDER, auto_transcribe.SCAN_DAYS_BACK)
        assert seen["folders"] == [str(tmp_path / "2026-10-07")]
        assert seen["kwargs"]["last_requested"] is auto_transcribe._icloud_last_requested
        assert seen["kwargs"]["max_requests"] == auto_transcribe.ICLOUD_MAX_DOWNLOADS_PER_CYCLE
        assert seen["kwargs"]["dry_run"] is False

    def test_disabled_preflight_does_nothing(self, monkeypatch):
        monkeypatch.setattr(auto_transcribe, "ICLOUD_PREFLIGHT_ENABLED", False)
        monkeypatch.setattr(auto_transcribe, "run_preflight", lambda *a, **k: pytest.fail("must not run"))
        assert auto_transcribe.run_icloud_preflight() == 0

    def test_preflight_crash_never_costs_the_scan(self, monkeypatch, capsys):
        def explode(*a, **k):
            raise RuntimeError("iCloud daemon gone")

        monkeypatch.setattr(auto_transcribe, "discover_recent_folders", explode)
        assert auto_transcribe.run_icloud_preflight() == 0
        assert "continuing scan" in capsys.readouterr().out

    def test_scan_cycle_runs_preflight_before_discovery_and_reports_count(self, monkeypatch):
        order: list[str] = []
        heartbeats: list[dict] = []

        def fake_preflight():
            order.append("preflight")
            return 3

        def fake_discover(*a, **k):
            order.append("discover")
            return []

        monkeypatch.setattr(auto_transcribe, "run_icloud_preflight", fake_preflight)
        monkeypatch.setattr(auto_transcribe, "discover_audio_files", fake_discover)
        monkeypatch.setattr(auto_transcribe, "write_heartbeat", lambda phase, **kw: heartbeats.append(kw))

        auto_transcribe.run_scan_cycle({"processed": {}}, {}, 1)

        assert order == ["preflight", "discover"]
        assert {"icloud_downloads_requested": 3} in heartbeats

    def test_end_to_end_stub_in_scan_window_triggers_brctl(self, tmp_path, monkeypatch):
        """Real discover_recent_folders + real detection; only brctl is faked."""
        today = pipeline._now_local().strftime("%Y-%m-%d")
        stub = _stub(tmp_path / today, "10-55-00.m4a")
        _stub(tmp_path / "2020-01-01", "old.m4a")  # outside scan window
        runner = FakeRunner()
        real_run_preflight = icloud_preflight.run_preflight
        monkeypatch.setattr(auto_transcribe, "WATCH_FOLDER", str(tmp_path))
        monkeypatch.setattr(
            auto_transcribe, "run_preflight", lambda folders, **kw: real_run_preflight(folders, runner=runner, **kw)
        )

        assert auto_transcribe.run_icloud_preflight() == 1
        assert [argv[2] for argv, _ in runner.calls] == [str(stub)]


# ---------------------------------------------------------------------------
# IC-6: CLI (shell-level smoke with a stub brctl)
# ---------------------------------------------------------------------------


def _fake_brctl(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "bin" / "brctl"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(PROJECT_DIR)}
    return subprocess.run(
        [sys.executable, str(PROJECT_DIR / "icloud_preflight.py"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


class TestCli:
    def test_real_subprocess_invokes_brctl_download(self, tmp_path):
        log = tmp_path / "brctl.log"
        brctl = _fake_brctl(tmp_path, f'echo "$@" >> "{log}"')
        day = tmp_path / "jpr" / "2026-10-07"
        stub = _stub(day, "10-55-00.m4a")

        proc = _run_cli("--folder", str(day), "--brctl", str(brctl))

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert log.read_text().strip() == f"download {stub}"
        assert "requested 1 download(s)" in proc.stdout

    def test_dry_run_never_executes_brctl(self, tmp_path):
        marker = tmp_path / "ran"
        brctl = _fake_brctl(tmp_path, f'touch "{marker}"')
        day = tmp_path / "2026-10-07"
        _stub(day, "10-55-00.m4a")

        proc = _run_cli("--folder", str(day), "--brctl", str(brctl), "--dry-run")

        assert proc.returncode == 0
        assert not marker.exists()
        assert "[dry-run]" in proc.stdout

    def test_hanging_brctl_times_out_and_exits_cleanly(self, tmp_path):
        brctl = _fake_brctl(tmp_path, "sleep 30")
        day = tmp_path / "2026-10-07"
        _stub(day, "10-55-00.m4a")

        proc = _run_cli("--folder", str(day), "--brctl", str(brctl), "--timeout", "0.5")

        assert proc.returncode == 0
        assert "timed out" in proc.stdout

    def test_missing_brctl_exits_2(self, tmp_path):
        day = tmp_path / "2026-10-07"
        _stub(day, "10-55-00.m4a")
        proc = _run_cli("--folder", str(day), "--brctl", str(tmp_path / "nope" / "brctl"))
        assert proc.returncode == 2
        assert "brctl unavailable" in proc.stdout

    def test_no_placeholders_reports_and_exits_0(self, tmp_path):
        (tmp_path / "2026-10-07").mkdir()
        proc = _run_cli("--folder", str(tmp_path / "2026-10-07"))
        assert proc.returncode == 0
        assert "no placeholders" in proc.stdout

    def test_module_imports_without_config(self):
        """IC-6: the CLI must run where no config.yaml exists (launchd smoke check)."""
        env = {k: v for k, v in os.environ.items() if k != "SWOWF_CONFIG_PATH"}
        env["PYTHONPATH"] = str(PROJECT_DIR)
        proc = subprocess.run(
            [sys.executable, "-c", "import icloud_preflight, sys; sys.exit('config' in sys.modules)"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd="/",
        )
        assert proc.returncode == 0, proc.stderr
