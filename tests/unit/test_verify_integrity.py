"""Tests for R11: pipeline integrity verification tool.

Covers the three cross-checks documented in the 2026-07-24 P2 plan:
  - state status=complete but no .md in FOLDERS[category]            → missing_md
  - state status=complete, .md exists but in a different category    → wrong_folder_md
  - .md in a FOLDERS[*] folder with no state entry claiming it       → orphan_md
  - audio file in watch folder not in state["processed"]             → untracked_audio
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from verify_integrity import verify_integrity


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _make_state_entry(status="complete", category="MUSIKERE", timestamp_iso=None):
    ts = timestamp_iso or datetime.now().isoformat()
    return {"status": status, "category": category, "timestamp": ts, "attempts": 1}


@pytest.fixture
def folders(tmp_path):
    """Minimal FOLDERS dict pointing at temp dirs."""
    return {
        "MUSIKERE": str(tmp_path / "musikere"),
        "MINNESOTERE": str(tmp_path / "minnesotere"),
        "PERSONLIG": str(tmp_path / "personlig"),
        "DEFAULT": str(tmp_path / "personlig"),  # alias of PERSONLIG, like config.yaml
    }


@pytest.fixture
def watch_folder(tmp_path):
    wf = tmp_path / "watch"
    (wf / _today_str()).mkdir(parents=True)
    return str(wf)


# --- missing_md -----------------------------------------------------------------


def test_missing_md_reported(folders, watch_folder, tmp_path):
    """State says complete but no .md in FOLDERS[category] → reported in missing_md."""
    state = {"processed": {"audio/a.m4a": _make_state_entry(category="MUSIKERE")}}
    Path(folders["MUSIKERE"]).mkdir()

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert len(report["missing_md"]) == 1
    assert report["missing_md"][0]["expected_category"] == "MUSIKERE"
    assert report["wrong_folder_md"] == []


def test_md_in_correct_folder_not_reported(folders, watch_folder):
    """State complete, .md exists in FOLDERS[category] with matching timestamp → not reported."""
    now = datetime.now()
    state = {"processed": {"audio/a.m4a": _make_state_entry(category="MUSIKERE", timestamp_iso=now.isoformat())}}
    musikere = Path(folders["MUSIKERE"])
    musikere.mkdir()
    (musikere / f"{now.strftime('%y-%m-%d %H.%M')} - Some Meeting.md").write_text("body")

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert report["missing_md"] == []
    assert report["wrong_folder_md"] == []


def test_failed_permanent_does_not_trigger_missing_md(folders, watch_folder):
    """failed_permanent entries have no .md and should not be flagged as missing."""
    state = {"processed": {"audio/a.m4a": _make_state_entry(status="failed_permanent", category="MUSIKERE")}}
    Path(folders["MUSIKERE"]).mkdir()

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert report["missing_md"] == []


# --- wrong_folder_md -------------------------------------------------------------


def test_wrong_folder_md_reported(folders, watch_folder):
    """State says MUSIKERE but .md is in MINNESOTERE folder → wrong_folder_md."""
    now = datetime.now()
    state = {"processed": {"audio/a.m4a": _make_state_entry(category="MUSIKERE", timestamp_iso=now.isoformat())}}
    minnesotere = Path(folders["MINNESOTERE"])
    minnesotere.mkdir()
    (minnesotere / f"{now.strftime('%y-%m-%d %H.%M')} - Routed Wrong.md").write_text("body")

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert report["missing_md"] == []
    assert len(report["wrong_folder_md"]) == 1
    assert report["wrong_folder_md"][0]["expected_category"] == "MUSIKERE"
    assert report["wrong_folder_md"][0]["actual_category"] == "MINNESOTERE"


# --- orphan_md ------------------------------------------------------------------


def test_orphan_md_reported(folders, watch_folder):
    """An .md in a vault folder with no matching state entry → orphan_md."""
    minnesotere = Path(folders["MINNESOTERE"])
    minnesotere.mkdir()
    (minnesotere / "26-07-23 14.30 - Orphan Meeting.md").write_text("body")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=1)

    assert len(report["orphan_md"]) == 1
    assert report["orphan_md"][0]["category"] == "MINNESOTERE"
    assert report["orphan_md"][0]["timestamp_key"] == "26-07-23 14.30"


def test_orphan_not_reported_when_state_claims_same_key_same_category(folders, watch_folder):
    """An .md whose timestamp+category match a state entry is NOT an orphan."""
    now = datetime.now()
    key = now.strftime("%y-%m-%d %H.%M")
    musikere = Path(folders["MUSIKERE"])
    musikere.mkdir()
    (musikere / f"{key} - Claimed.md").write_text("body")
    state = {"processed": {"audio/a.m4a": _make_state_entry(category="MUSIKERE", timestamp_iso=now.isoformat())}}

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert report["orphan_md"] == []


def test_orphan_not_reported_when_folder_alias_has_claim(folders, watch_folder):
    """DEFAULT and PERSONLIG point to the same path. A PERSONLIG claim should suppress
    a DEFAULT-folder orphan report for the same .md (avoid false positive).
    """
    now = datetime.now()
    key = now.strftime("%y-%m-%d %H.%M")
    shared = Path(folders["PERSONLIG"])  # same path as DEFAULT
    shared.mkdir()
    (shared / f"{key} - Claimed.md").write_text("body")
    state = {"processed": {"audio/a.m4a": _make_state_entry(category="PERSONLIG", timestamp_iso=now.isoformat())}}

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert report["orphan_md"] == [], f"expected no orphan via alias, got {report['orphan_md']}"


def test_legacy_analysis_files_skipped(folders, watch_folder):
    """Legacy two-file output (*- Analysis.md) is skipped by build_transcript_index; verify
    should skip it too so it doesn't show as orphan noise.
    """
    musikere = Path(folders["MUSIKERE"])
    musikere.mkdir()
    (musikere / "26-07-23 14.30 - Some Meeting - Analysis.md").write_text("body")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=1)

    assert report["orphan_md"] == []


# --- state_since filter ---------------------------------------------------------


def test_orphan_suppressed_when_older_than_state_since(folders, watch_folder):
    """--state-since=2026-07-01 suppresses an orphan dated 2026-06-15; counts in summary."""
    musikere = Path(folders["MUSIKERE"])
    musikere.mkdir()
    (musikere / "26-06-15 09.00 - Legacy Meeting.md").write_text("body")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=1, state_since="26-07-01")

    assert report["orphan_md"] == []
    assert report["summary"]["orphans_suppressed"] == 1


def test_orphan_not_suppressed_when_newer_than_state_since(folders, watch_folder):
    """--state-since=2026-07-01 does NOT suppress an orphan dated 2026-07-23."""
    minnesotere = Path(folders["MINNESOTERE"])
    minnesotere.mkdir()
    (minnesotere / "26-07-23 14.30 - Recent Orphan.md").write_text("body")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=1, state_since="26-07-01")

    assert len(report["orphan_md"]) == 1
    assert report["summary"]["orphans_suppressed"] == 0


def test_state_since_does_not_affect_missing_md(folders, watch_folder):
    """state_since filters orphans only; missing_md is reported regardless of age."""
    state = {"processed": {"audio/a.m4a": _make_state_entry(category="MUSIKERE", timestamp_iso="2026-06-15T09:00:00")}}
    Path(folders["MUSIKERE"]).mkdir()

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1, state_since="26-07-01")

    assert len(report["missing_md"]) == 1
    assert report["summary"]["orphans_suppressed"] == 0


def test_state_since_none_reports_all_orphans(folders, watch_folder):
    """Default state_since=None behaves identically to no filter — every orphan reported."""
    musikere = Path(folders["MUSIKERE"])
    musikere.mkdir()
    (musikere / "26-06-15 09.00 - Legacy.md").write_text("body")
    (musikere / "26-07-23 14.30 - Recent.md").write_text("body")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=1)

    assert len(report["orphan_md"]) == 2
    assert report["summary"]["orphans_suppressed"] == 0


# --- untracked_audio ------------------------------------------------------------


def test_untracked_audio_reported(folders, watch_folder):
    """An .m4a in a recent watch-folder date dir, not in state → untracked_audio."""
    audio = Path(watch_folder) / _today_str() / "14-30-18.m4a"
    audio.write_bytes(b"fake")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=1)

    assert len(report["untracked_audio"]) == 1
    assert str(audio) in report["untracked_audio"][0]


def test_tracked_audio_not_reported(folders, watch_folder):
    """An .m4a whose path is in state["processed"] is NOT untracked."""
    audio = Path(watch_folder) / _today_str() / "14-30-18.m4a"
    audio.write_bytes(b"fake")
    state = {"processed": {str(audio): _make_state_entry()}}

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert report["untracked_audio"] == []


def test_icloud_temp_and_dotfile_audio_skipped(folders, watch_folder):
    """iCloud-not-yet-downloaded (.icloud), temp, and dotfile audio are skipped."""
    today_dir = Path(watch_folder) / _today_str()
    (today_dir / ".14-30-18.m4a").write_bytes(b"hidden")
    (today_dir / "14-30-18.m4a.icloud").write_text("icloud")
    (today_dir / "14-30-19.tmp.m4a").write_bytes(b"tmp")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=1)

    assert report["untracked_audio"] == []


def test_old_audio_outside_scan_window_skipped(folders, watch_folder, tmp_path):
    """Audio in a date dir older than scan_days_back is skipped (not untracked)."""
    old_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    old_dir = Path(watch_folder) / old_date
    old_dir.mkdir(parents=True)
    (old_dir / "14-30-18.m4a").write_bytes(b"fake")

    report = verify_integrity({"processed": {}}, folders, watch_folder, scan_days_back=7)

    assert report["untracked_audio"] == []


# --- summary + clean state ------------------------------------------------------


def test_clean_state_no_issues(folders, watch_folder):
    """State, vault, and watch folder all agree → no issues."""
    now = datetime.now()
    key = now.strftime("%y-%m-%d %H.%M")
    musikere = Path(folders["MUSIKERE"])
    musikere.mkdir()
    (musikere / f"{key} - Clean Meeting.md").write_text("body")
    audio = Path(watch_folder) / _today_str() / "14-30-18.m4a"
    audio.write_bytes(b"fake")
    state = {
        "processed": {
            str(audio): _make_state_entry(category="MUSIKERE", timestamp_iso=now.isoformat()),
        }
    }

    report = verify_integrity(state, folders, watch_folder, scan_days_back=1)

    assert report["missing_md"] == []
    assert report["wrong_folder_md"] == []
    assert report["orphan_md"] == []
    assert report["untracked_audio"] == []
    assert report["summary"]["state_complete"] == 1
