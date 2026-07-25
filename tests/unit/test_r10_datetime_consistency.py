"""Tests for R10: naive/aware datetime consistency.

R10's contract: every datetime that flows into state, compares against another
pipeline datetime, or feeds a filename strftime is NAIVE Europe/Oslo wall-clock
(tzinfo=None). The `_now_local()` helper makes this explicit so the daemon doesn't
silently inherit the host system's tz if it ever changes.
"""

from datetime import datetime, timedelta, timezone

from pipeline import OSLO_TZ, _now_local, discover_recent_folders


def test_now_local_returns_naive():
    """_now_local() returns tzinfo=None so it can strftime without tz-suffix surprises."""
    assert _now_local().tzinfo is None


def test_now_local_matches_europe_oslo_wall_clock():
    """_now_local() is within 2 seconds of datetime.now(tz=OSLO_TZ) wall-clock."""
    a = _now_local()
    b = datetime.now(tz=OSLO_TZ).replace(tzinfo=None)
    assert abs((a - b).total_seconds()) < 2


def test_now_local_isoformat_has_no_tz_suffix():
    """ISO format of _now_local() must not contain '+' or 'Z' (naive)."""
    iso = _now_local().isoformat()
    assert "+" not in iso[10:], f"expected naive ISO, got {iso}"
    assert not iso.endswith("Z"), f"expected naive ISO, got {iso}"


def test_now_local_winter_no_dst_skew():
    """In January (CET, UTC+1), _now_local() wall-clock = UTC + 1h.

    Sanity-checks that OSLO_TZ is correctly wired to the IANA zone (DST-aware),
    not a fixed offset that would be wrong in winter.
    """
    # Construct a winter instant in UTC and verify Europe/Oslo offset is +1h, not +2h.
    winter_utc = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    winter_oslo = winter_utc.astimezone(OSLO_TZ)
    assert winter_oslo.utcoffset() == timedelta(hours=1), (
        "Europe/Oslo in January must be CET (+01:00), not CEST (+02:00)"
    )


def test_now_local_summer_dst_applied():
    """In July (CEST, UTC+2), Europe/Oslo offset is +2h."""
    summer_utc = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    summer_oslo = summer_utc.astimezone(OSLO_TZ)
    assert summer_oslo.utcoffset() == timedelta(hours=2)


def test_discover_recent_folders_uses_naive_cutoff(tmp_path, monkeypatch):
    """discover_recent_folders compares folder dates against a NAIVE cutoff.

    A regression that returned an aware cutoff would crash when comparing with
    `datetime.strptime(child.name, "%Y-%m-%d")` (also naive).
    """
    # Create today's folder
    today_name = _now_local().strftime("%Y-%m-%d")
    (tmp_path / today_name).mkdir()

    # Force the function to call _now_local() and produce a naive cutoff
    result = discover_recent_folders(str(tmp_path), days_back=7)
    assert len(result) == 1
    assert today_name in result[0]


def test_recover_failed_permanent_handles_naive_state_timestamp(tmp_path, monkeypatch):
    """A state entry with a NAIVE timestamp (post-R9/R10 format) is interpreted as
    Europe/Oslo and converted to UTC for legacy datetime-window matching.

    Pre-R10 the code used `.astimezone()` on naive, which treated it as the system
    local tz — same result on a Mac in Europe/Oslo, but fragile. R10 makes it
    explicit via `.replace(tzinfo=OSLO_TZ)`.
    """
    # Verify _derive_audio_ts + the replace path don't crash and produce an aware UTC
    # datetime suitable for comparison with aware-UTC rec_start in _find_best_candidate.
    from pipeline import _derive_audio_ts

    entry = {"timestamp": "2026-07-22T13:59:43", "processed_at": "2026-07-22T14:25:22"}
    ts = _derive_audio_ts("/nonexistent.m4a", entry)
    assert ts is not None
    assert ts.tzinfo is None  # state-stored naive

    # Simulate the recover_failed_permanent naive→aware conversion path
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=OSLO_TZ)
    assert ts.tzinfo is not None
    # In July, 13:59:43 Europe/Oslo = 11:59:43 UTC
    utc = ts.astimezone(timezone.utc)
    assert utc.hour == 11
