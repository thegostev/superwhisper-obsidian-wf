"""Tests for R9: JPR filename timezone correction.

Reproduces the 2026-07-24 finding (memory: reference-jpr-timezone.md): Just Press Record
iCloud filenames use CET (UTC+1) year-round, but Norway uses CEST (UTC+2) in summer.
The pre-fix code read the filename as naive local time, producing .md filenames 1 hour
behind the actual meeting time (e.g., a 14:31 CEST meeting got named "13.31").

R9 fix: parse the JPR filename as UTC+1 fixed (no DST), convert to Europe/Oslo, return
naive Europe/Oslo wall-clock so strftime produces the user's local meeting time.
"""

from datetime import timedelta, timezone
from unittest.mock import patch

from pipeline import get_audio_timestamp

JPR_FIXED_CET = timezone(timedelta(hours=1))


def _fake_mdls_empty(*args, **kwargs):
    """Pretend mdls returned nothing (file not yet materialized in iCloud)."""
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _fake_mdls_utc(raw_utc: str):
    def _impl(*args, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": raw_utc, "stderr": ""})()
    return _impl


# --- filename path (the R9 bug) ------------------------------------------------


def test_filename_path_summer_shifts_to_cest(tmp_path):
    """JPR file 2026-07-23/13-31-25.m4a: filename says 13:31 CET, actual local is 14:31 CEST.

    Pre-fix: returned naive 13:31:25 → .md named "13.31" (1h behind actual meeting).
    Post-fix: returns naive 14:31:25 (Europe/Oslo wall-clock) → .md named "14.31".
    """
    audio = tmp_path / "2026-07-23" / "13-31-25.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake")

    with patch("subprocess.run", _fake_mdls_empty):
        ts = get_audio_timestamp(str(audio))

    assert ts.hour == 14, f"summer: expected 14 (CEST), got {ts.hour}"
    assert ts.minute == 31
    assert ts.tzinfo is None, "should return naive Europe/Oslo wall-clock for strftime compatibility"


def test_filename_path_winter_no_shift(tmp_path):
    """JPR file 2026-01-15/13-31-25.m4a: filename says 13:31 CET, local is also 13:31 CET (winter).

    In winter Europe/Oslo = CET = UTC+1, same as JPR's fixed CET. No shift.
    """
    audio = tmp_path / "2026-01-15" / "13-31-25.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake")

    with patch("subprocess.run", _fake_mdls_empty):
        ts = get_audio_timestamp(str(audio))

    assert ts.hour == 13, f"winter: expected 13 (CET), got {ts.hour}"
    assert ts.minute == 31


def test_filename_path_spring_forward(tmp_path):
    """DST transition: a JPR file dated March 30 2026 (DST switch day in Europe/Oslo).

    At 02:00 CET on 2026-03-30, clocks spring forward to 03:00 CEST. A JPR file
    "02-30-00" would have been recorded at 02:30 CET (= 03:30 CEST local).
    """
    audio = tmp_path / "2026-03-30" / "02-30-00.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake")

    with patch("subprocess.run", _fake_mdls_empty):
        ts = get_audio_timestamp(str(audio))

    assert ts.hour == 3, f"spring-forward: expected 03 CEST, got {ts.hour}"
    assert ts.minute == 30


# --- mdls path (was already correct, should remain so) --------------------------


def test_mdls_path_returns_local_wall_clock(tmp_path):
    """mdls returns UTC+0000 — astimezone to Europe/Oslo, strip tz, return naive local."""
    audio = tmp_path / "2026-07-23" / "13-31-25.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake")

    # 12:31:25 UTC = 14:31:25 CEST
    with patch("subprocess.run", _fake_mdls_utc("2026-07-23 12:31:25 +0000")):
        ts = get_audio_timestamp(str(audio))

    assert ts.hour == 14
    assert ts.minute == 31
    assert ts.tzinfo is None


def test_mdls_naive_fallback_treats_as_utc_not_local(tmp_path):
    """If mdls returns a naive datetime (iCloud sync incomplete), assume UTC — not local.

    Pre-fix the code already did this via `replace(tzinfo=timezone.utc)`; R9 keeps the
    behavior but strips tz at the end for consistency with the filename path.
    """
    audio = tmp_path / "2026-07-23" / "13-31-25.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake")

    # Naive "12:31:25" — must be treated as UTC, giving 14:31:25 CEST, NOT 12:31 local.
    with patch("subprocess.run", _fake_mdls_utc("2026-07-23 12:31:25")):
        ts = get_audio_timestamp(str(audio))

    assert ts.hour == 14, f"naive mdls should be treated as UTC → 14 CEST, got {ts.hour}"


# --- contract: all paths return naive for strftime compatibility ----------------


def test_returns_naive_datetime_in_all_paths(tmp_path):
    """All three fallback paths (mdls, filename, ctime) must return tzinfo=None so
    strftime('%H.%M') gives wall-clock without tz suffix surprises and so callers
    that compare against naive datetimes (e.g. discover_recent_folders) don't crash.
    """
    audio = tmp_path / "2026-07-23" / "13-31-25.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"fake")

    # filename path
    with patch("subprocess.run", _fake_mdls_empty):
        assert get_audio_timestamp(str(audio)).tzinfo is None

    # mdls aware path
    with patch("subprocess.run", _fake_mdls_utc("2026-07-23 12:31:25 +0000")):
        assert get_audio_timestamp(str(audio)).tzinfo is None

    # mdls naive path
    with patch("subprocess.run", _fake_mdls_utc("2026-07-23 12:31:25")):
        assert get_audio_timestamp(str(audio)).tzinfo is None
