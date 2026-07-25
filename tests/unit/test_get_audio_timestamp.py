"""Unit tests for get_audio_timestamp().

The timestamp drives both the output filename (YY-MM-DD HH.MM - Title.md) and the
dedup index key. It must be in Europe/Oslo wall-clock time (naive, tzinfo=None) to
match the user's meeting-time reference. mdls returns kMDItemContentCreationDate in
UTC with a +0000 offset; the function converts to Europe/Oslo. The filename fallback
parses the JPR name as CET (UTC+1 fixed — JPR ignores Norway's DST) and converts to
Europe/Oslo, so a "16-32" Jul filename → 17:32 CEST, not 16:32.
"""

from pipeline import get_audio_timestamp


def test_mdls_utc_is_converted_to_local_time(tmp_path, monkeypatch):
    """mdls returns UTC; the formatted output must match Europe/Oslo wall-clock time.

    mdls returns kMDItemContentCreationDate as UTC: 2026-07-20 14:32:02 +0000.
    In July (CEST = UTC+2) the Europe/Oslo wall-clock is 16:32. Without conversion
    the output filename would be 26-07-20 14.32 (UTC) — a 2-hour discrepancy.
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
        f"Expected Europe/Oslo 26-07-20 16.32 (CEST), got {formatted}. "
        f"mdls returned UTC 14:32 +0000 which must be converted to 16:32 CEST."
    )


def test_filename_fallback_uses_cet_fixed_not_dst(tmp_path, monkeypatch):
    """When mdls fails, the JPR folder/filename parse uses CET (UTC+1) fixed.

    JPR uses CET year-round — in July, when local is CEST (UTC+2), a "16-32" filename
    = 16:32 CET = 17:32 CEST. The output filename must reflect the user's local
    meeting time (17.32), not the JPR-stored CET value (16.32). This is the R9 fix
    documented in memory: reference-jpr-timezone.md.
    """
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
    assert formatted == "26-07-20 17.32", (
        f"JPR filename 16-32-02 is CET (UTC+1); in July local is CEST (UTC+2), "
        f"so Europe/Oslo is 17.32. Got {formatted}."
    )


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
