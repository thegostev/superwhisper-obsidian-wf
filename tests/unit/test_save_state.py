"""Unit tests for save_state() atomic-write durability.

save_state must write to a temp file in the same directory then atomically rename,
so a crash or write failure mid-save cannot truncate the canonical state file. A
truncated state.json makes load_state fall back to fresh and re-queue every audio
in the scan window — a mass reprocess. These tests pin the atomicity contract.
"""

import json
from pathlib import Path

from pipeline import save_state


def test_save_state_writes_content_and_leaves_no_temp(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr("pipeline.STATE_FILE", str(state_file))

    save_state({"processed": {"x": {"status": "complete"}}})

    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk == {"processed": {"x": {"status": "complete"}}}
    # The temp file must be renamed away, not left behind.
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_state_preserves_existing_on_write_failure(tmp_path, monkeypatch):
    """If the temp write fails, the existing state file is left intact (not truncated),
    because we write to a temp and rename — never write the canonical file in place.
    """
    state_file = tmp_path / "state.json"
    existing = {"processed": {"keep": {"status": "complete"}}}
    state_file.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr("pipeline.STATE_FILE", str(state_file))

    tmp_path_str = str(state_file.with_suffix(state_file.suffix + ".tmp"))
    real_write_text = Path.write_text

    def boom(self, data, encoding=None, **kwargs):
        if str(self) == tmp_path_str:
            raise OSError("disk full")
        return real_write_text(self, data, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)

    # save_state swallows the OSError (warns) — it must not raise.
    save_state({"processed": {"new": {"status": "complete"}}})

    # Existing state untouched (not overwritten with the new content).
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk == existing
    # Failed temp write cleaned up.
    assert not Path(tmp_path_str).exists()


def test_save_state_preserves_existing_when_replace_fails(tmp_path, monkeypatch):
    """If os.replace fails, the existing state file is still intact and the temp removed."""
    state_file = tmp_path / "state.json"
    existing = {"processed": {"keep": {"status": "complete"}}}
    state_file.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr("pipeline.STATE_FILE", str(state_file))

    import os

    def boom(src, dst):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", boom)

    save_state({"processed": {"new": {"status": "complete"}}})

    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk == existing
    assert not (tmp_path / "state.json.tmp").exists()
