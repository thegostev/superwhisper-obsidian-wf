"""preflight.sh behavioral tests (ADR 0010, spec PF-*).

The preflight is the anti-storm heart of self-healing: it validates the venv
interpreter by executing it, rebuilds at most once per hour under an atomic
lock, defers during Homebrew operations, honours fresh fatal heartbeats, and
exits 0 (never respawning) whenever it cannot hand off a working daemon. These
tests exercise the real script in a throwaway deploy dir with shimmed
interpreters — no network, no real venv touched.

Test hooks (built into the script, PF-12): PREFLIGHT_CANDIDATES,
PREFLIGHT_NOTIFY=0, PREFLIGHT_PIP_CMD, PREFLIGHT_HEARTBEAT.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parents[2]
SCRIPT = PROJECT_DIR / "preflight.sh"

# Overwritten by the deploy fixture with fake candidate interpreter paths.
CAND_OK = "/nonexistent/cand_ok"
CAND_BAD = "/nonexistent/cand_bad"

# The interpreter running pytest: >=3.12 with PyYAML in every CI matrix and in
# the dev venv — passes both probes without depending on a repo venv layout.
REAL_PROBE_PYTHON = Path(sys.executable)

FAKE_CANDIDATE = """#!/bin/sh
# Passes the candidate probe; `-m venv DIR` fabricates a python that passes
# the full probe and echoes DAEMON-RAN when exec'd as an interpreter.
case "$1" in
  -c) exit 0 ;;
  -m)
    [ "$2" = "venv" ] || exit 1
    mkdir -p "$3/bin"
    printf '#!/bin/sh\\ncase "$1" in -c) exit 0 ;; *) echo "DAEMON-RAN $*"; exit 0 ;; esac\\n' > "$3/bin/python3"
    chmod +x "$3/bin/python3"
    ;;
esac
"""

FAKE_BAD_VENV_CANDIDATE = """#!/bin/sh
# Passes the candidate probe but builds a venv whose python fails the full probe.
case "$1" in
  -c) exit 0 ;;
  -m)
    [ "$2" = "venv" ] || exit 1
    mkdir -p "$3/bin"
    printf '#!/bin/sh\\nexit 3\\n' > "$3/bin/python3"
    chmod +x "$3/bin/python3"
    ;;
esac
"""

VENVPY_OK = '#!/bin/sh\ncase "$1" in -c) exit 0 ;; *) echo "DAEMON-RAN $*"; exit 0 ;; esac\n'
VENVPY_BAD = "#!/bin/sh\nexit 3\n"


@pytest.fixture
def deploy(tmp_path, monkeypatch):
    """A throwaway deploy dir containing the real preflight.sh and shims."""
    root = tmp_path / "deploy"
    root.mkdir()
    script_copy = root / "preflight.sh"
    script_copy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    script_copy.chmod(0o755)

    shim_bin = tmp_path / "bin"
    shim_bin.mkdir()
    pgrep = shim_bin / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # no brew running
    pgrep.chmod(0o755)

    # Fake candidate interpreters: cand_ok builds a venv that passes the full
    # probe; cand_bad builds one that fails it. Both pass the candidate probe.
    cand_ok = tmp_path / "cand_ok"
    cand_ok.write_text(FAKE_CANDIDATE, encoding="utf-8")
    cand_ok.chmod(0o755)
    cand_bad = tmp_path / "cand_bad"
    cand_bad.write_text(FAKE_BAD_VENV_CANDIDATE, encoding="utf-8")
    cand_bad.chmod(0o755)
    globals()["CAND_OK"] = str(cand_ok)
    globals()["CAND_BAD"] = str(cand_bad)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{shim_bin}:/usr/bin:/bin")
    monkeypatch.setenv("PREFLIGHT_NOTIFY", "0")
    monkeypatch.setenv("PREFLIGHT_HEARTBEAT", str(tmp_path / "hb.json"))
    monkeypatch.delenv("PREFLIGHT_CANDIDATES", raising=False)
    monkeypatch.delenv("PREFLIGHT_PIP_CMD", raising=False)
    return root


def run_preflight(deploy, *args, candidates=None, pip_cmd=None):
    env = dict(os.environ)
    if candidates is not None:
        env["PREFLIGHT_CANDIDATES"] = candidates
    if pip_cmd is not None:
        env["PREFLIGHT_PIP_CMD"] = pip_cmd
    return subprocess.run(
        ["/bin/bash", str(deploy / "preflight.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=str(deploy),
    )


def make_venv(deploy, body=VENVPY_OK):
    venv_bin = deploy / "venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    py = venv_bin / "python3"
    py.write_text(body, encoding="utf-8")
    py.chmod(0o755)


def write_heartbeat(deploy, payload, age=0):
    path = deploy.parent / "hb.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    if age:
        os.utime(path, (time.time() - age, time.time() - age))
    return path


class TestVerbs:
    def test_probe_passes_real_venv_python(self, deploy):
        assert run_preflight(deploy, "probe", str(REAL_PROBE_PYTHON)).returncode == 0

    def test_candidate_probe_rejects_failing_interpreter(self, deploy):
        """PF-13: an interpreter below 3.12 (e.g. macOS /usr/bin/python3, 3.9.x)
        fails the candidate probe. Simulated hermetically — CI runners' system
        python3 is 3.12+ and would legitimately pass."""
        old_python = deploy.parent / "old_python"
        old_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        old_python.chmod(0o755)
        assert run_preflight(deploy, "candidate-probe", str(old_python)).returncode == 1

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only: /usr/bin/python3 is 3.9.x")
    @pytest.mark.skipif(
        subprocess.run(
            ["/usr/bin/python3", "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"]
        ).returncode
        != 1,
        reason="system python3 is >= 3.12 — nothing to reject",
    )
    def test_candidate_probe_rejects_system_python(self, deploy):
        assert run_preflight(deploy, "candidate-probe", "/usr/bin/python3").returncode == 1

    def test_select_prefers_first_passing_candidate_in_order(self, deploy):
        result = run_preflight(
            deploy, "select", candidates=f"/nonexistent/pythonA:{REAL_PROBE_PYTHON}:/usr/bin/python3"
        )
        assert result.returncode == 0
        assert result.stdout.strip() == str(REAL_PROBE_PYTHON)

    def test_select_fails_when_no_candidate_passes(self, deploy):
        result = run_preflight(deploy, "select", candidates="/nonexistent/a:/nonexistent/b")
        assert result.returncode == 1

    def test_candidates_verb_lists_pass_fail(self, deploy):
        result = run_preflight(deploy, "candidates", candidates=f"/nonexistent/x:{REAL_PROBE_PYTHON}")
        assert result.returncode == 0
        assert "FAIL /nonexistent/x" in result.stdout
        assert f"PASS {REAL_PROBE_PYTHON}" in result.stdout


class TestWrapperExec:
    def test_healthy_venv_execs_daemon(self, deploy):
        """PF-1: the wrapper execs the daemon so launchd tracks its PID."""
        make_venv(deploy)
        result = run_preflight(deploy)
        assert result.returncode == 0
        assert "DAEMON-RAN" in result.stdout

    def test_dry_run_with_healthy_venv_does_not_exec(self, deploy):
        """PF-11."""
        make_venv(deploy)
        result = run_preflight(deploy, "--dry-run")
        assert result.returncode == 0
        assert "DAEMON-RAN" not in result.stdout
        assert "dry-run" in result.stderr


class TestRebuild:
    def test_rebuilds_and_hands_off_to_daemon(self, deploy, tmp_path):
        """PF-6: sibling build, validate, then move into place; PF-14 marker
        cleared; PF-15 repoint recorded; PF-10 lock released."""
        result = run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        assert result.returncode == 0
        assert "DAEMON-RAN" in result.stdout
        assert (deploy / "venv" / "bin" / "python3").exists()
        assert not (deploy / "venv.new").exists()
        home = Path(os.environ["HOME"])
        assert not (home / ".superwhisper_transcriber_rebuild.json").exists()  # PF-14 cleared
        assert not (home / ".superwhisper_transcriber_rebuild.lock").exists()  # PF-10 released
        repoint = json.loads((home / ".superwhisper_transcriber_repoint.json").read_text())
        assert "repointed" in repoint["message"]  # ES-8

    def test_failed_rebuild_exits_zero_and_escalates(self, deploy, tmp_path):
        """PF-9: exit 0 — the sole defence against an unbounded respawn loop."""
        result = run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_BAD}", pip_cmd="true")
        assert result.returncode == 0
        assert "ESCALATION" in result.stderr
        assert "DAEMON-RAN" not in result.stdout
        assert not (deploy / "venv").exists()
        assert not (deploy / "venv.new").exists()

    def test_no_candidate_at_all_exits_zero_and_escalates(self, deploy, tmp_path):
        result = run_preflight(deploy, candidates="/nonexistent/a:/nonexistent/b")
        assert result.returncode == 0
        assert "ESCALATION" in result.stderr

    def test_broken_venv_renamed_aside_most_recent_kept(self, deploy, tmp_path):
        """PF-7."""
        make_venv(deploy, VENVPY_BAD)
        older = deploy / "venv.broken.20200101-000000"
        older.mkdir()
        result = run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        assert result.returncode == 0
        assert "DAEMON-RAN" in result.stdout
        broken = list(deploy.glob("venv.broken.*"))
        assert older not in broken  # only the most recent copy is retained
        assert len(broken) == 1

    def test_rebuild_cooldown_blocks_rebuild(self, deploy, tmp_path):
        """PF-8: at most one rebuild attempt per hour."""
        home = Path(os.environ["HOME"])
        (home / ".superwhisper_transcriber_lastrebuild").write_text(str(int(time.time())), encoding="utf-8")
        result = run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        assert result.returncode == 0
        assert "cooldown" in result.stderr
        assert not (deploy / "venv").exists()

    def test_rebuild_lock_held_by_live_pid_blocks_rebuild(self, deploy, tmp_path):
        """PF-10: skip while the lock is actively held."""
        home = Path(os.environ["HOME"])
        (home / ".superwhisper_transcriber_rebuild.lock").write_text(f"{os.getpid()} {int(time.time())}\n")
        result = run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        assert result.returncode == 0
        assert "held" in result.stderr
        assert not (deploy / "venv").exists()

    def test_stale_rebuild_lock_is_broken(self, deploy, tmp_path):
        """PF-10: a dead holding PID does not block forever."""
        home = Path(os.environ["HOME"])
        (home / ".superwhisper_transcriber_rebuild.lock").write_text(f"999999999 {int(time.time())}\n")
        result = run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        assert result.returncode == 0
        assert "DAEMON-RAN" in result.stdout  # rebuild proceeded

    def test_fresh_rebuild_marker_cleaned_after_run(self, deploy, tmp_path):
        """PF-14: marker written during the rebuild, removed afterwards."""
        run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        home = Path(os.environ["HOME"])
        assert not (home / ".superwhisper_transcriber_rebuild.json").exists()


class TestGuards:
    def test_brew_operation_defers_without_consuming_cooldown(self, deploy, tmp_path):
        """PF-15: defer; the PF-8 cooldown must NOT be consumed."""
        shim_bin = Path(os.environ["PATH"].split(":")[0])
        pgrep = shim_bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")  # brew "running"
        pgrep.chmod(0o755)
        result = run_preflight(deploy, candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        assert result.returncode == 0
        assert "deferring" in result.stderr
        assert not (deploy / "venv").exists()
        home = Path(os.environ["HOME"])
        assert not (home / ".superwhisper_transcriber_lastrebuild").exists()

    def test_fresh_fatal_heartbeat_blocks_restart(self, deploy, tmp_path):
        """PF-16 reader half: a fresh fatal heartbeat is unrecoverable by restart."""
        make_venv(deploy)  # interpreter is fine — the daemon fault was fatal
        write_heartbeat(deploy, {"schema": 1, "phase": "fatal", "fatal_reason": "FatalAPIError: x"})
        result = run_preflight(deploy)
        assert result.returncode == 0
        assert "DAEMON-RAN" not in result.stdout
        assert "ESCALATION" in result.stderr

    def test_stale_fatal_heartbeat_does_not_block(self, deploy):
        make_venv(deploy)
        write_heartbeat(deploy, {"schema": 1, "phase": "fatal"}, age=900)
        result = run_preflight(deploy)
        assert "DAEMON-RAN" in result.stdout

    def test_dry_run_with_broken_venv_plans_but_does_not_rebuild(self, deploy, tmp_path):
        result = run_preflight(deploy, "--dry-run", candidates=f"/nonexistent/nope:{CAND_OK}", pip_cmd="true")
        assert result.returncode == 0
        assert "would rebuild" in result.stderr
        assert not (deploy / "venv").exists()
        assert not (deploy / "venv.new").exists()
