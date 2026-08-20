"""Unit tests for salvage-path internals: _read_recording_meta and _parse_recovery_candidate.

Pins two contracts:
  - _read_recording_meta returns only the fields the pipeline actually consumes
    (the dead `has_transcript` field was computed but never read).
  - _parse_recovery_candidate reads meta.json exactly once per candidate (it
    previously parsed the file twice — once via _read_recording_meta for llmResult,
    again directly for datetime/duration). Salvage scans every recording dir on
    startup and every SALVAGE_PASS_EVERY_N_CYCLES, so the doubled I/O compounds as
    the recordings dir fills with completed stubs.
"""

import json
from pathlib import Path

from pipeline import CATEGORY_HEADER, _parse_recovery_candidate, _read_recording_meta


def _make_rec(tmp_path, name="rec", meta=None):
    rec = tmp_path / name
    rec.mkdir()
    (rec / "meta.json").write_text(json.dumps(meta or {}), encoding="utf-8")
    return rec


def test_read_recording_meta_returns_only_consumed_fields(tmp_path):
    rec = _make_rec(
        tmp_path,
        meta={
            "llmResult": "CATEGORY: X\nFILENAME: t\n\nbody",
            "result": "raw",
            "processingTime": 5,
            "languageModelProcessingTime": 12,
            "duration": 30000,
        },
    )
    info = _read_recording_meta(rec)
    # has_transcript was computed but never read by any caller — drop it.
    assert set(info) == {"llm_result", "processing_time", "llm_processing_time", "duration_ms"}
    assert info["llm_result"] == "CATEGORY: X\nFILENAME: t\n\nbody"
    assert info["processing_time"] == 5
    assert info["llm_processing_time"] == 12
    assert info["duration_ms"] == 30000


def test_read_recording_meta_missing_returns_none(tmp_path):
    rec = tmp_path / "empty"
    rec.mkdir()
    assert _read_recording_meta(rec) is None


def test_parse_recovery_candidate_reads_meta_once(tmp_path, monkeypatch):
    rec = _make_rec(
        tmp_path,
        meta={
            "datetime": "2026-07-22T14:24:06",
            "duration": 1299000,
            "llmResult": f"{CATEGORY_HEADER} MINNESOTERE\nFILENAME: Sync\n\nbody",
        },
    )
    meta_path = str(rec / "meta.json")
    real_read_text = Path.read_text
    calls = {"n": 0}

    def counting(self, *args, **kwargs):
        if str(self) == meta_path:
            calls["n"] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)

    result = _parse_recovery_candidate(rec, 1000.0)

    assert result is not None
    assert calls["n"] == 1, f"meta.json read {calls['n']} times, expected 1"
    # Tuple shape (5-tuple with entry Path) preserved.
    mtime, rec_start, duration_ms, text, entry = result
    assert mtime == 1000.0
    assert duration_ms == 1299000
    assert CATEGORY_HEADER in text
    assert entry == rec
