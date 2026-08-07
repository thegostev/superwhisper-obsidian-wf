"""Unit tests for save_output() atomic-write durability.

save_output must write the .md to a sibling temp file then atomically rename,
never write the canonical note in place. A non-atomic write leaves a truncated
.md in the vault mid-save, which iCloud then syncs to every device and Obsidian
indexes as a half-file. The temp must not end in `.md` so the `*.md` globs used
by build_transcript_index / verify_integrity never observe it mid-flight.
"""

import os
from pathlib import Path

from pipeline import save_output


def _set_folders(monkeypatch, tmp_path):
    out_dir = tmp_path / "vault"
    out_dir.mkdir(parents=True)
    monkeypatch.setattr("pipeline.FOLDERS", {"DEFAULT": str(out_dir)})
    return out_dir


def test_save_output_writes_content_and_leaves_no_temp(tmp_path, monkeypatch):
    out_dir = _set_folders(monkeypatch, tmp_path)
    filename = "26-08-07 14.30 - Title.md"

    returned = save_output("DEFAULT", filename, "body content")

    assert returned == str(out_dir / filename)
    written = (out_dir / filename).read_text(encoding="utf-8")
    assert written == "body content"
    # No temp file left behind; the temp name does not match *.md.
    assert list(out_dir.glob("*.md")) == [out_dir / filename]
    assert not (out_dir / (filename + ".tmp")).exists()


def test_save_output_preserves_existing_on_write_failure(tmp_path, monkeypatch):
    out_dir = _set_folders(monkeypatch, tmp_path)
    filename = "26-08-07 14.30 - Title.md"
    existing = "previous content"
    (out_dir / filename).write_text(existing, encoding="utf-8")

    tmp_path_str = str(out_dir / (filename + ".tmp"))
    real_write_text = Path.write_text

    def boom(self, data, encoding=None, **kwargs):
        if str(self) == tmp_path_str:
            raise OSError("disk full")
        return real_write_text(self, data, encoding=encoding, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)

    assert save_output("DEFAULT", filename, "new content") is None

    # Existing note untouched (not overwritten with the new content).
    assert (out_dir / filename).read_text(encoding="utf-8") == existing
    # Failed temp write cleaned up.
    assert not Path(tmp_path_str).exists()


def test_save_output_preserves_existing_when_replace_fails(tmp_path, monkeypatch):
    out_dir = _set_folders(monkeypatch, tmp_path)
    filename = "26-08-07 14.30 - Title.md"
    existing = "previous content"
    (out_dir / filename).write_text(existing, encoding="utf-8")

    def boom(src, dst):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", boom)

    assert save_output("DEFAULT", filename, "new content") is None

    assert (out_dir / filename).read_text(encoding="utf-8") == existing
    assert not (out_dir / (filename + ".tmp")).exists()
