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
import time

import pytest

from superwhisper_client import SuperwhisperBusyError, resolve_lock_path, superwhisper_lock


def _acquire(lock_path: str) -> bool:
    with superwhisper_lock(lock_path):
        return True


def _process_worker(lock_path: str, result_queue) -> None:
    with superwhisper_lock(lock_path):
        result_queue.put("child-acquired")


def _hold_worker(lock_path: str, ready, release) -> None:
    with superwhisper_lock(lock_path):
        ready.set()
        release.wait(timeout=10)


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


def test_bounded_wait_raises_naming_lock_and_wait(tmp_path):
    """A held lock from a forked child makes the parent's bounded acquire time out."""
    ctx = multiprocessing.get_context("fork")
    lock_path = tmp_path / "busy.lock"
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_worker, args=(str(lock_path), ready, release))
    proc.start()
    try:
        assert ready.wait(timeout=5), "child never acquired the lock"
        started = time.monotonic()
        with (
            pytest.raises(SuperwhisperBusyError, match=r"waiting for the Superwhisper lock .*busy\.lock"),
            superwhisper_lock(lock_path, timeout=0.2, poll_interval=0.01),
        ):
            pytest.fail("acquired a lock the child holds")
        assert time.monotonic() - started >= 0.2
    finally:
        release.set()
        proc.join(timeout=5)
    assert proc.exitcode == 0


def test_on_wait_fires_between_attempts(tmp_path):
    """The waiter can refresh the daemon heartbeat while another process holds the lock."""
    lock_path = tmp_path / "waited.lock"
    ready, release = threading.Event(), threading.Event()
    waits: list[float] = []

    def holder() -> None:
        with superwhisper_lock(lock_path):
            ready.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert ready.wait(timeout=5)
    try:
        with (
            pytest.raises(SuperwhisperBusyError),
            superwhisper_lock(
                lock_path,
                timeout=0.15,
                on_wait=lambda: waits.append(time.monotonic()),
                poll_interval=0.01,
            ),
        ):
            pytest.fail("acquired a lock the thread holds")
    finally:
        release.set()
        thread.join(timeout=5)
    assert waits, "on_wait was never called while waiting for the lock"
