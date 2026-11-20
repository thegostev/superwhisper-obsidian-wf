"""Shared cross-process Superwhisper lock (LAG-1057).

The lock is an exclusive ``fcntl.flock`` on ``~/.superwhisper.lock`` (override
with ``lock_path=`` or ``$SWOWF_LOCK_PATH``). ``analyze_file`` holds it from the
mode switch through the result; ``pipeline.process_audio`` takes the same lock.
These tests pin mutual exclusion across threads and processes, release on
exception, and path resolution.
"""

import multiprocessing
import queue
import threading

import pytest

from superwhisper_client import resolve_lock_path, superwhisper_lock


def _acquire(lock_path: str) -> bool:
    with superwhisper_lock(lock_path):
        return True


def _process_worker(lock_path: str, result_queue) -> None:
    with superwhisper_lock(lock_path):
        result_queue.put("child-acquired")


def test_explicit_path_is_used_and_created(tmp_path):
    lock_path = tmp_path / "custom.lock"
    with superwhisper_lock(lock_path) as held:
        assert held == lock_path
        assert lock_path.exists()
    # The lock file is reusable; the lock itself was released.
    with superwhisper_lock(lock_path):
        pass


def test_env_override_is_used(tmp_path, monkeypatch):
    env_path = tmp_path / "env.lock"
    monkeypatch.setenv("SWOWF_LOCK_PATH", str(env_path))
    assert resolve_lock_path() == env_path
    with superwhisper_lock() as held:
        assert held == env_path


def test_explicit_path_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWOWF_LOCK_PATH", str(tmp_path / "env.lock"))
    explicit = tmp_path / "explicit.lock"
    assert resolve_lock_path(explicit) == explicit


def test_threads_cannot_enter_at_once(tmp_path):
    lock_path = tmp_path / "threads.lock"
    entered: list[str] = []
    waiter_done = threading.Event()

    def waiter() -> None:
        with superwhisper_lock(lock_path):
            entered.append("entered")
        waiter_done.set()

    with superwhisper_lock(lock_path):
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=0.3)
        assert entered == [], "second thread entered while the lock was held"

    thread.join(timeout=5)
    assert waiter_done.is_set()
    assert entered == ["entered"]


def test_processes_cannot_enter_at_once(tmp_path):
    ctx = multiprocessing.get_context("fork")
    lock_path = str(tmp_path / "processes.lock")
    result_queue = ctx.Queue()

    with superwhisper_lock(lock_path):
        proc = ctx.Process(target=_process_worker, args=(lock_path, result_queue))
        proc.start()
        with pytest.raises(queue.Empty):
            result_queue.get(timeout=0.3)
        assert proc.is_alive(), "child process exited instead of waiting for the lock"

    assert result_queue.get(timeout=5) == "child-acquired"
    proc.join(timeout=5)
    assert proc.exitcode == 0


def test_lock_released_when_body_raises(tmp_path):
    lock_path = tmp_path / "exception.lock"
    with pytest.raises(RuntimeError, match="boom"), superwhisper_lock(lock_path):
        raise RuntimeError("boom")

    acquired: list[bool] = []
    thread = threading.Thread(target=lambda: acquired.append(_acquire(str(lock_path))), daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert acquired == [True], "lock was not released on exception"


def test_lock_parent_directory_created(tmp_path):
    lock_path = tmp_path / "nested" / "dir" / "shared.lock"
    with superwhisper_lock(lock_path) as held:
        assert held.parent.is_dir()
