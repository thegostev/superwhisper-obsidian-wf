"""Shared pipeline: Superwhisper handoff, parsing, file I/O, state management.

All entry points (daemon, on-demand CLI, maintenance CLI) import from here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    FAILED_ANALYSIS_LOG,
    FOLDERS,
    MAX_RETRIES,
    STATE_FILE,
    SUPERWHISPER_MODE_KEY,
    SUPERWHISPER_POLL_INTERVAL,
    SUPERWHISPER_RECORDINGS_DIR,
    SUPERWHISPER_TIMEOUT,
)

# Shared timestamp format for filenames: "YY-MM-DD HH.MM"
TIMESTAMP_FORMAT = "%y-%m-%d %H.%M"


# ============================================================================
# ERROR CLASSES
# ============================================================================


class FatalAPIError(Exception):
    """Error that should stop the entire service (unrecoverable, e.g. misconfiguration)."""


class PermanentFileError(Exception):
    """Error specific to one file that retrying won't fix (bad format, corrupt audio, etc)."""


# ============================================================================
# STATE MANAGEMENT
# ============================================================================


def load_state():
    """Load processed files state from disk."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Warning: Could not load state file, starting fresh: {e}", flush=True)
    return {"processed": {}}


def save_state(state):
    """Save processed files state to disk."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except OSError as e:
        print(f"⚠️  Warning: Could not save state file: {e}", flush=True)


# ============================================================================
# HELPERS: TIMESTAMP, PARSING, FILE I/O
# ============================================================================


def get_audio_timestamp(audio_path):
    """Extract recording timestamp from audio file.

    Strategy: 1) macOS mdls  2) directory/filename parse  3) file ctime
    """
    # Strategy 1: macOS metadata
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemContentCreationDate", "-raw", audio_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            timestamp_str = result.stdout.strip()
            for fmt in ["%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"]:
                try:
                    return datetime.strptime(timestamp_str, fmt)
                except ValueError:
                    continue
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    # Strategy 2: directory + filename parse
    try:
        path_parts = Path(audio_path).parts
        filename = Path(audio_path).stem
        for part in reversed(path_parts):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                try:
                    year, month, day = part.split("-")
                    time_part = filename.split()[0] if " " in filename else filename
                    hour, minute, second = time_part.split("-")
                    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass

    # Strategy 3: file creation time
    try:
        ctime = os.path.getctime(audio_path)
        return datetime.fromtimestamp(ctime)
    except Exception:
        return datetime.now()


def extract_section(content: str, start_marker: str, end_marker: str | None) -> str:
    """Extract content between start_marker and end_marker (or end of string)."""
    lines = content.split("\n")
    capturing = False
    section_lines = []

    for line in lines:
        if start_marker and start_marker in line:
            capturing = True
            continue
        if end_marker and end_marker in line:
            break
        if capturing:
            section_lines.append(line)

    return "\n".join(section_lines).strip()


def save_output(category: str, filename: str, content: str) -> str | None:
    """Save analysis output directly to the configured category folder."""
    dest_folder = FOLDERS.get(category, FOLDERS["DEFAULT"])
    try:
        os.makedirs(dest_folder, exist_ok=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not create output folder: {e}", flush=True)

    output_path = os.path.join(dest_folder, filename)
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        return output_path
    except OSError as e:
        print(f"⚠️  Warning: Failed to save output: {e}", flush=True)
        return None


# Keep alias for callers that import save_analysis (reclassify_and_fix.py)
save_analysis = save_output


def log_failed_analysis(transcript_path: str, category: str, filename: str) -> None:
    """Append a NEEDS_ANALYSIS entry to the persistent failure log."""
    try:
        entry = f"{datetime.now().isoformat()} | NEEDS_ANALYSIS | {category} | {filename} | {transcript_path}\n"
        with open(FAILED_ANALYSIS_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
        print(f"   📝 Logged to failed_analysis.log: {filename}", flush=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not write to failed_analysis.log: {e}", flush=True)


# ============================================================================
# DISCOVERY & INDEXING
# ============================================================================


def build_transcript_index(folders: dict[str, str]) -> dict[str, dict]:
    """Scan all category folders, build lookup dict keyed by "YY-MM-DD HH.MM".

    With single-file output, files live directly in FOLDERS[category] (not in
    a transcripts/ subfolder). The index is used for deduplication only.
    """
    index: dict[str, dict] = {}

    for category, base_path in folders.items():
        if not os.path.exists(base_path):
            continue
        try:
            for filename in os.listdir(base_path):
                if not filename.endswith(".md"):
                    continue
                if filename.endswith(" - Analysis.md"):
                    continue
                if len(filename) >= 14:
                    timestamp_key = filename[:14]
                    index[timestamp_key] = {
                        "category": category,
                        "output_path": os.path.join(base_path, filename),
                    }
        except (OSError, PermissionError) as e:
            print(f"⚠️  Warning: Could not scan {base_path}: {e}", flush=True)

    return index


def is_file_stable(path: str, wait_seconds: int = 2) -> bool:
    """Check if file has finished syncing (not still downloading from iCloud)."""
    try:
        size1 = os.path.getsize(path)
        if size1 == 0:
            return False
        time.sleep(wait_seconds)
        size2 = os.path.getsize(path)
        return size1 == size2
    except (OSError, FileNotFoundError):
        return False


def discover_recent_folders(watch_folder: str, days_back: int = 7) -> list[str]:
    """Find date-based subfolders from the last N days, sorted oldest-first."""
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    cutoff = datetime.now() - timedelta(days=days_back)
    recent_folders = []

    try:
        for entry in os.listdir(watch_folder):
            if not date_pattern.match(entry):
                continue
            full_path = os.path.join(watch_folder, entry)
            if not os.path.isdir(full_path):
                continue
            try:
                folder_date = datetime.strptime(entry, "%Y-%m-%d")
                if folder_date >= cutoff:
                    recent_folders.append((folder_date, full_path))
            except ValueError:
                continue
    except OSError as e:
        print(f"❌ Cannot list watch folder: {e}", flush=True)
        return []

    recent_folders.sort(key=lambda x: x[0])
    return [path for _, path in recent_folders]


# ============================================================================
# SUPERWHISPER INTEGRATION
# ============================================================================


def switch_superwhisper_mode() -> None:
    """Switch Superwhisper to the configured Custom Mode via deep link."""
    if not SUPERWHISPER_MODE_KEY:
        raise FatalAPIError(
            "superwhisper_mode_key is not set in config.yaml. "
            "Default value is 'meeting' — verify in ~/Documents/superwhisper/modes."
        )
    subprocess.run(
        ["open", f"superwhisper://mode?key={SUPERWHISPER_MODE_KEY}"],
        check=True,
    )
    time.sleep(1.0)  # allow mode switch to settle before file handoff


def handoff_to_superwhisper(file_path: str) -> None:
    """Open audio file in Superwhisper for transcription + analysis in one pass."""
    if not os.path.exists(file_path):
        raise PermanentFileError(f"Audio file not found: {file_path}")
    subprocess.run(["open", file_path, "-a", "Superwhisper"], check=True)
    # .m4a is an MPEG-4 audio container — accepted by Superwhisper.
    # If transcription fails silently, convert first:
    #   ffmpeg -i input.m4a -ar 16000 -ac 1 output.wav


def _read_superwhisper_entry(path: Path) -> str | None:
    """Read llmResult from a Superwhisper recording directory.

    Each recording is a directory containing meta.json + output.wav.
    The AI-processed Custom Mode output is in the "llmResult" field.
    """
    try:
        if not path.is_dir():
            return None
        meta = path / "meta.json"
        if not meta.exists():
            return None
        data = json.loads(meta.read_text(encoding="utf-8"))
        return data.get("llmResult") or ""
    except (OSError, ValueError):
        return None


def wait_for_superwhisper_result(file_path: str, since: float) -> str:
    """Poll until Superwhisper finishes processing this file. Returns raw output text.

    since: time.time() value captured just before handoff_to_superwhisper() was called.
    Raises TimeoutError if no result appears within SUPERWHISPER_TIMEOUT seconds.
    Raises FatalAPIError if the recordings folder does not exist.
    """
    recordings_dir = Path(SUPERWHISPER_RECORDINGS_DIR)
    if not recordings_dir.exists():
        raise FatalAPIError(
            f"Superwhisper recordings folder not found: {recordings_dir}. "
            "Verify Superwhisper is installed and has been used at least once."
        )

    deadline = time.time() + SUPERWHISPER_TIMEOUT
    print(f"   ⏳ Waiting for Superwhisper (timeout: {SUPERWHISPER_TIMEOUT}s)...", flush=True)

    while time.time() < deadline:
        time.sleep(SUPERWHISPER_POLL_INTERVAL)
        try:
            entries = sorted(
                recordings_dir.iterdir(),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue

        for entry in entries:
            try:
                if entry.stat().st_mtime <= since:
                    break  # all remaining entries are older than our handoff
            except OSError:
                continue
            text = _read_superwhisper_entry(entry)
            if text and ("CATEGORY:" in text or "---CATEGORY---" in text):
                print(f"   📄 Got result from: {entry.name}", flush=True)
                return text

    raise TimeoutError(
        f"Superwhisper did not return a result within {SUPERWHISPER_TIMEOUT}s "
        f"for: {os.path.basename(file_path)}"
    )


def parse_superwhisper_output(raw_output: str) -> tuple[str, str, str]:
    """Parse Superwhisper Custom Mode output into (category, filename, analysis).

    Supports the header format output by the current prompt:
        CATEGORY: <name>
        FILENAME: <title>

        <analysis body>

    Also supports the section-marker format as a fallback:
        ---CATEGORY--- / ---FILENAME--- / ---ANALYSIS---

    Falls back to DEFAULT / 'Unknown Meeting' on parse failures.
    Raises PermanentFileError if no analysis body is found.
    """
    lines = raw_output.strip().split("\n")
    category = "DEFAULT"
    filename = "Unknown Meeting"
    analysis_start = 0

    for i, line in enumerate(lines):
        if line.startswith("CATEGORY:"):
            cat = line.split("CATEGORY:", 1)[1].strip().upper()
            cat = re.sub(r'[^\x00-\x7F]', '', cat).strip()
            category = cat if cat in FOLDERS else "DEFAULT"
            if cat not in FOLDERS:
                print(f"   ⚠️  Unknown category '{cat}', falling back to DEFAULT", flush=True)
            analysis_start = i + 1
        elif line.startswith("FILENAME:"):
            fn = line.split("FILENAME:", 1)[1].strip()
            fn = fn.replace("/", "-").replace("\\", "-").replace(":", ".").replace("?", "").replace("*", "").replace('"', "")
            if fn:
                filename = fn
            analysis_start = i + 1

    # Skip blank lines between headers and body
    while analysis_start < len(lines) and not lines[analysis_start].strip():
        analysis_start += 1

    analysis = "\n".join(lines[analysis_start:]).strip()

    if not analysis:
        raise PermanentFileError(
            "Superwhisper output has no analysis body. "
            "Check the Custom Mode prompt outputs CATEGORY: / FILENAME: followed by content."
        )

    return category, filename, analysis


# ============================================================================
# MAIN PIPELINE
# ============================================================================


def process_audio(file_path: str, timestamp, state: dict) -> tuple[bool, str | None]:
    """Full pipeline: Superwhisper handoff → parse result → save analysis file.

    Returns (success: bool, category: str | None).
    Raises FatalAPIError to stop the service on unrecoverable errors.
    """
    basename = os.path.basename(file_path)
    attempts = state.get("processed", {}).get(file_path, {}).get("attempts", 0)

    try:
        since = time.time()
        switch_superwhisper_mode()
        handoff_to_superwhisper(file_path)
        raw_output = wait_for_superwhisper_result(file_path, since=since)
        category, ai_filename, analysis = parse_superwhisper_output(raw_output)

        formatted_timestamp = timestamp.strftime(TIMESTAMP_FORMAT)
        filename = f"{formatted_timestamp} - {ai_filename}"
        if not filename.endswith(".md"):
            filename += ".md"

        output_path = save_output(category, filename, analysis)
        if output_path:
            print(f"   ✅ Analysis saved: {output_path}", flush=True)

        state.setdefault("processed", {})[file_path] = {
            "status": "complete",
            "category": category,
            "timestamp": timestamp.isoformat(),
            "processed_at": datetime.now().isoformat(),
            "attempts": attempts + 1,
        }
        save_state(state)
        return True, category

    except FatalAPIError:
        raise

    except PermanentFileError as e:
        print(f"   🛑 Permanent error for {basename}: {e}", flush=True)
        state.setdefault("processed", {})[file_path] = {
            "status": "failed_permanent",
            "error": str(e),
            "processed_at": datetime.now().isoformat(),
            "attempts": attempts + 1,
        }
        save_state(state)
        return False, None

    except Exception as e:
        print(f"   ❌ Failed to process {basename}: {e}", flush=True)
        attempts += 1
        if attempts >= MAX_RETRIES:
            state.setdefault("processed", {})[file_path] = {
                "status": "failed_permanent",
                "error": str(e),
                "processed_at": datetime.now().isoformat(),
                "attempts": attempts,
            }
            print(f"   🛑 Permanently failed after {attempts} attempts", flush=True)
        else:
            state.setdefault("processed", {})[file_path] = {
                "status": "failed_retry",
                "error": str(e),
                "processed_at": datetime.now().isoformat(),
                "attempts": attempts,
            }
            print(f"   🔄 Will retry on next cycle (attempt {attempts}/{MAX_RETRIES})", flush=True)
        save_state(state)
        return False, None
