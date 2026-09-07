"""Regression tests for run_transcriber.sh's launchctl label matching.

The old `grep -q "com.alex.transcriber"` substring match confused the daemon
label with com.alex.transcriber.watchdog — once the watchdog agent (commit 10)
is loaded, `./run_transcriber.sh start` would refuse to start even with the
daemon unloaded (HC-6). Exact field-3 matching fixes it; these tests pin both
directions through a fake launchctl on PATH, and a static test bans the
substring grep from returning.
"""

import subprocess
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parents[2]
SCRIPT = PROJECT_DIR / "run_transcriber.sh"


def make_launchctl_shim(tmp_path: Path, output: str) -> Path:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "launchctl"
    shim.write_text("#!/bin/sh\ncat <<'EOF'\n" + output + "EOF\n", encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir


def run_start(tmp_path: Path, launchctl_output: str) -> subprocess.CompletedProcess:
    workdir = tmp_path / "deploy"
    workdir.mkdir()
    script_copy = workdir / "run_transcriber.sh"
    script_copy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    shim_dir = make_launchctl_shim(tmp_path, launchctl_output)
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(["/bin/zsh", str(script_copy), "start"], capture_output=True, text=True, env=env, timeout=60)


def test_no_substring_grep_in_source():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'grep -q "com.alex.transcriber"' not in source


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="zsh unavailable (ubuntu CI runners)")
class TestStartRefusesOnlyWhenDaemonLoaded:
    def test_refuses_when_daemon_label_loaded(self, tmp_path):
        result = run_start(tmp_path, "PID\tStatus\tLabel\n8078\t-15\tcom.alex.transcriber\n")
        assert result.returncode != 0
        assert "Refusing" in result.stdout

    def test_does_not_refuse_when_only_watchdog_label_loaded(self, tmp_path):
        """The regression: the watchdog agent's label contains the daemon label
        as a substring — the old grep refused to start anyway."""
        result = run_start(tmp_path, "PID\tStatus\tLabel\n-\t0\tcom.alex.transcriber.watchdog\n")
        assert "Refusing" not in result.stdout
        assert "Started" in result.stdout

    def test_does_not_refuse_when_nothing_relevant_loaded(self, tmp_path):
        result = run_start(tmp_path, "PID\tStatus\tLabel\n-\t-\tcom.other.service\n")
        assert "Refusing" not in result.stdout
