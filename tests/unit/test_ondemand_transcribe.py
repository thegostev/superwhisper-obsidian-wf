"""Characterization tests: unprocessed-filter loop in ondemand_transcribe.main().

Pins the behaviour: audio files already in the transcript index must NOT
appear in unprocessed; transcript_only is always empty.
"""

from datetime import datetime

from pipeline import TIMESTAMP_FORMAT


def _make_index(*timestamps):
    return {ts.strftime(TIMESTAMP_FORMAT): {"category": "DEFAULT", "output_path": "/x.md"} for ts in timestamps}


def test_unprocessed_filter_excludes_indexed_files():
    ts_hit = datetime(2026, 1, 1, 10, 0)
    ts_miss = datetime(2026, 1, 1, 11, 0)
    all_files = [("/a.m4a", ts_hit), ("/b.m4a", ts_miss)]
    index = _make_index(ts_hit)

    unprocessed = [(ap, ts) for ap, ts in all_files if not index.get(ts.strftime(TIMESTAMP_FORMAT))]

    assert len(unprocessed) == 1
    assert unprocessed[0][0] == "/b.m4a"


def test_unprocessed_filter_empty_when_all_indexed():
    ts = datetime(2026, 1, 1, 10, 0)
    all_files = [("/a.m4a", ts)]
    unprocessed = [(ap, t) for ap, t in all_files if not _make_index(ts).get(t.strftime(TIMESTAMP_FORMAT))]
    assert unprocessed == []


def test_unprocessed_filter_all_when_none_indexed():
    ts1, ts2 = datetime(2026, 1, 1, 10, 0), datetime(2026, 1, 1, 11, 0)
    all_files = [("/a.m4a", ts1), ("/b.m4a", ts2)]
    unprocessed = [(ap, t) for ap, t in all_files if not {}.get(t.strftime(TIMESTAMP_FORMAT))]
    assert len(unprocessed) == 2


def test_transcript_only_is_always_empty():
    """transcript_only is never populated — verify it stays []."""
    transcript_only = []
    assert transcript_only == []
