"""Recordings-store retention tests for LAG-911.

The recordings store only ever grows: every handoff leaves a session dir with
``meta.json`` + ``output.wav``, and crashed handoffs leave dirs with no
``meta.json`` at all. Cleanup must delete exactly the dirs that are provably
safe to lose — consumed and past retention, or orphans past the (shorter)
orphan TTL — and must never touch a dir a non-terminal state entry still owns.
"""

import copy
import os
import time
from pathlib import Path

import pytest  # noqa: F401  (fixtures are resolved by name)

import cleanup
from pipeline import CONSUMED_SENTINEL

NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR


def make_session_dir(
    root: Path,
    name: str,
    *,
    age_s: float,
    consumed: bool = False,
    meta: bool = True,
    payload: int = 1024,
    now: float = NOW,
) -> Path:
    """Create a synthetic Superwhisper session dir and backdate it by ``age_s``."""
    path = root / name
    path.mkdir()
    (path / "output.wav").write_bytes(b"x" * payload)
    if meta:
        (path / "meta.json").write_text("{}", encoding="utf-8")
    if consumed:
        (path / CONSUMED_SENTINEL).write_text("consumed\n", encoding="utf-8")
    stamp = now - age_s
    for child in path.iterdir():
        os.utime(child, (stamp, stamp))
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def store(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    return recordings


def test_dry_run_lists_only_expired_consumed_and_orphans(store):
    old_consumed = make_session_dir(store, "old-consumed", age_s=20 * DAY, consumed=True)
    old_orphan = make_session_dir(store, "old-orphan", age_s=48 * HOUR, meta=False)
    current = make_session_dir(store, "current", age_s=DAY, consumed=True)
    in_progress = make_session_dir(store, "in-progress", age_s=10 * DAY, meta=False)
    state = {"processed": {"audio.m4a": {"status": "in_progress", "recording_dir": "in-progress"}}}

    plan = cleanup.plan_cleanup(store, state, now=NOW)

    assert {candidate.path.name: candidate.reason for candidate in plan.candidates} == {
        "old-consumed": cleanup.REASON_CONSUMED,
        "old-orphan": cleanup.REASON_ORPHAN,
    }
    assert plan.kept_by_retention == 1  # "current"
    assert plan.kept_protected == 1  # "in-progress"
    assert old_consumed.exists() and old_orphan.exists() and current.exists() and in_progress.exists()


def test_dry_run_does_not_mutate_state(store):
    make_session_dir(store, "old-consumed", age_s=20 * DAY, consumed=True)
    state = {"processed": {"audio.m4a": {"status": "complete", "recording_dir": "old-consumed"}}}
    before = copy.deepcopy(state)

    cleanup.plan_cleanup(store, state, now=NOW)

    assert state == before


def test_apply_deletes_only_candidates_and_reports_bytes(store):
    old_consumed = make_session_dir(store, "old-consumed", age_s=20 * DAY, consumed=True, payload=2048)
    old_orphan = make_session_dir(store, "old-orphan", age_s=48 * HOUR, meta=False, payload=512)
    current = make_session_dir(store, "current", age_s=DAY, consumed=True, payload=99)

    plan = cleanup.plan_cleanup(store, {}, now=NOW)
    expected_bytes = sum(candidate.bytes for candidate in plan.candidates)

    outcome = cleanup.apply_cleanup(plan)

    assert (outcome.deleted, outcome.freed_bytes, outcome.failures) == (2, expected_bytes, ())
    assert not old_consumed.exists()
    assert not old_orphan.exists()
    assert current.exists()


def test_second_apply_is_idempotent(store):
    make_session_dir(store, "old-consumed", age_s=20 * DAY, consumed=True)
    make_session_dir(store, "old-orphan", age_s=48 * HOUR, meta=False)
    state = {"processed": {}}

    cleanup.apply_cleanup(cleanup.plan_cleanup(store, state, now=NOW))
    again = cleanup.plan_cleanup(store, state, now=NOW)

    assert again.candidates == ()
    outcome = cleanup.apply_cleanup(again)
    assert (outcome.deleted, outcome.freed_bytes, outcome.failures) == (0, 0, ())


def test_unconsumed_dir_with_meta_is_never_deleted(store):
    kept = make_session_dir(store, "completed-but-unconsumed", age_s=60 * DAY, consumed=False, meta=True)

    plan = cleanup.plan_cleanup(store, {}, now=NOW)

    assert plan.candidates == ()
    assert kept.exists()


def test_completed_state_entry_does_not_protect_consumed_dir(store):
    expired = make_session_dir(store, "expired", age_s=20 * DAY, consumed=True)
    state = {"processed": {"audio.m4a": {"status": "complete", "recording_dir": "expired"}}}

    plan = cleanup.plan_cleanup(store, state, now=NOW)

    assert [candidate.path.name for candidate in plan.candidates] == ["expired"]
    assert expired.exists()


def test_nonterminal_recording_dir_protects_dir(store):
    protected = make_session_dir(store, "retrying", age_s=20 * DAY, consumed=True)
    state = {"processed": {"audio.m4a": {"status": "failed_retry", "recording_dir": "retrying"}}}

    plan = cleanup.plan_cleanup(store, state, now=NOW)

    assert plan.candidates == ()
    assert protected.exists()


def test_recovery_recording_dir_protects_orphan(store):
    protected = make_session_dir(store, "failed-orphan", age_s=48 * HOUR, meta=False)
    state = {
        "processed": {
            "audio.m4a": {
                "status": "failed_permanent",
                "recovery": {"recording_dir": "failed-orphan"},
            }
        }
    }

    plan = cleanup.plan_cleanup(store, state, now=NOW)

    assert plan.candidates == ()
    assert protected.exists()


@pytest.mark.parametrize(("age_days", "expected"), [(13, False), (15, True)])
def test_consumed_retention_boundary(store, age_days, expected):
    make_session_dir(store, "dir", age_s=age_days * DAY, consumed=True)

    plan = cleanup.plan_cleanup(store, {}, now=NOW)

    assert bool(plan.candidates) is expected


@pytest.mark.parametrize(("age_hours", "expected"), [(23, False), (25, True)])
def test_orphan_retention_boundary(store, age_hours, expected):
    make_session_dir(store, "dir", age_s=age_hours * HOUR, meta=False)

    plan = cleanup.plan_cleanup(store, {}, now=NOW)

    assert bool(plan.candidates) is expected


def test_consumed_age_uses_sentinel_mtime_not_dir_mtime(store):
    path = make_session_dir(store, "dir", age_s=20 * DAY, consumed=True)
    # Something touched the dir after consumption; retention must run from the
    # sentinel, not the dir mtime, or the dirs would look fresh forever.
    os.utime(path, (NOW, NOW))

    plan = cleanup.plan_cleanup(store, {}, now=NOW)

    assert [candidate.path.name for candidate in plan.candidates] == ["dir"]


def test_loose_files_in_store_are_ignored(store):
    loose = store / "stray.wav"
    loose.write_bytes(b"x" * 10)
    os.utime(loose, (NOW - 60 * DAY, NOW - 60 * DAY))

    plan = cleanup.plan_cleanup(store, {}, now=NOW)

    assert plan.candidates == ()
    assert loose.exists()


def test_cli_dry_run_is_the_default(store, monkeypatch, capsys):
    expired = make_session_dir(store, "expired", age_s=20 * DAY, consumed=True, now=time.time())
    monkeypatch.setattr(cleanup, "_load_state_strict", lambda: {"processed": {}})

    code = cleanup.main(["--recordings-dir", str(store)])

    out = capsys.readouterr().out
    assert code == 0
    assert expired.exists()
    assert "DRY RUN" in out
    assert "--apply" in out


def test_cli_apply_deletes(store, monkeypatch):
    expired = make_session_dir(store, "expired", age_s=20 * DAY, consumed=True, now=time.time())
    monkeypatch.setattr(cleanup, "_load_state_strict", lambda: {"processed": {}})

    code = cleanup.main(["--recordings-dir", str(store), "--apply"])

    assert code == 0
    assert not expired.exists()


def test_cli_refuses_to_run_with_an_unreadable_state_file(store, monkeypatch, tmp_path, capsys):
    expired = make_session_dir(store, "expired", age_s=20 * DAY, consumed=True, now=time.time())
    corrupt = tmp_path / "state.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cleanup, "STATE_FILE", str(corrupt))

    code = cleanup.main(["--recordings-dir", str(store), "--apply"])

    assert code == 1
    assert expired.exists()
    assert "refusing" in capsys.readouterr().out
