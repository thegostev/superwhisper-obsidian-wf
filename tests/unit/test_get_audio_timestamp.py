"""Unit tests for get_audio_timestamp().

The timestamp drives both the output filename (YY-MM-DD HH.MM - Title.md) and the
dedup index key. It must be in LOCAL time to match the Just Press Record folder/filename
convention (which is local) and the user's meeting-time reference. mdls returns
kMDItemContentCreationDate in UTC with a +0000 offset; the function must convert to
local time before returning so the output filename matches the JPR folder time.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path

from pipeline import get_audio_timestamp


def test_mdls_utc_is_converted_to_local_time(tmp_path, monkeypatch):
    """mdls returns UTC; the formatted output must match the JPR local folder time.

    JPR names files in local time: 2026-07-20/16-32-02.m4a = 16:32 CEST.
    mdls returns kMDItemContentCreationDate as UTC: 2026-07-20 14:32:02 +0000.
    Without conversion, the output filename is 26-07-20 14.32 (UTC) instead of
    26-07-20 16.32 (local) — a 2-hour discrepancy for CEST.
    """
    audio = tmp_path / "16-32-02.m4a"
    audio.write_text("x")

    # Simulate mdls returning the UTC timestamp
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "2026-07-20 14:32:02 +0000"
            stderr = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = get_audio_timestamp(str(audio))
    formatted = result.strftime("%y-%m-%d %H.%M")
    assert formatted == "26-07-20 16.32", (
        f"Expected local time 26-07-20 16.32 (matches JPR folder), got {formatted}. "
        f"mdls returned UTC 14:32 +0000 which must be converted to local 16:32 CEST."
    )


def test_filename_fallback_is_local_time(tmp_path, monkeypatch):
    """When mdls fails, the folder/filename parse should give local time directly."""
    audio = tmp_path / "2026-07-20" / "16-32-02.m4a"
    audio.parent.mkdir()
    audio.write_text("x")

    # Make mdls return empty so it falls through to the filename parser
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = get_audio_timestamp(str(audio))
    formatted = result.strftime("%y-%m-%d %H.%M")
    assert formatted == "26-07-20 16.32", f"Filename parse should give 16.32, got {formatted}"


def test_mdls_naive_datetime_is_treated_as_utc(tmp_path, monkeypatch):
    """When iCloud sync is incomplete, mdls can return a naive datetime (no +0000).

    The value is still UTC. Without assuming UTC, the raw 12:00:36 would be returned
    as local time, producing filename "26-07-21 12.00" instead of "26-07-21 14.00" CEST.
    This reproduces the July 21 recording bug where the daemon saved the analysis with
    a 2-hour-shifted timestamp prefix.
    """
    audio = tmp_path / "2026-07-21" / "14-00-36.m4a"
    audio.parent.mkdir()
    audio.write_text("x")

    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "2026-07-21 12:00:36"  # naive — no +0000 suffix
            stderr = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = get_audio_timestamp(str(audio))
    formatted = result.strftime("%y-%m-%d %H.%M")
    assert formatted == "26-07-21 14.00", (
        f"Naive mdls output must be treated as UTC and converted to local. "
        f"Expected 26-07-21 14.00 (CEST), got {formatted}."
    )