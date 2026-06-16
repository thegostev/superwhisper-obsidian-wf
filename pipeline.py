"""Shared pipeline: Superwhisper handoff, parsing, file I/O, state management.

All entry points (daemon, on-demand CLI, maintenance CLI) import from here.
"""

from __future__ import annotations

import json, re, subprocess, time
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

TIMESTAMP_FORMAT = "%y-%m-%d %H.%M"

# Output-contract markers emitted by the Superwhisper Custom Mode prompt.
# The parser reads the header lines; everything after is the analysis body.
CATEGORY_HEADER = "CATEGORY:"
FILENAME_HEADER = "FILENAME:"
CATEGORY_SECTION_MARKER = "---CATEGORY---"  # section-marker fallback format

# Sentinel defaults used when parsing fails or a category is unknown.
DEFAULT_CATEGORY = "DEFAULT"
DEFAULT_FILENAME = "Unknown Meeting"

MARKDOWN_EXT = ".md"
ANALYSIS_SUFFIX = " - Analysis.md"  # legacy two-file output, skipped during indexing
TIMESTAMP_KEY_LENGTH = 14  # leading chars of a filename forming the "YY-MM-DD HH.MM" key

MDLS_TIMEOUT_SECONDS = 5  # macOS metadata lookup
MODE_SWITCH_SETTLE_SECONDS = 1.0  # let Superwhisper apply the mode before handoff
FILE_STABILITY_WAIT_SECONDS = 2  # iCloud sync settle check


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
    if Path(STATE_FILE).exists():
        try:
            return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Warning: Could not load state file, starting fresh: {e}", flush=True)
    return {"processed": {}}


def save_state(state):
    """Save processed files state to disk."""
    try:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
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
            timeout=MDLS_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip():
            for fmt in ["%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"]:
                try:
                    return datetime.strptime(result.stdout.strip(), fmt)
                except ValueError: pass
    except Exception:
        pass

    # Strategy 2: directory + filename parse
    try:
        p = Path(audio_path)
        for part in reversed(p.parts):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                try:
                    year, month, day = part.split("-")
                    hour, minute, second = (p.stem.split()[0] if " " in p.stem else p.stem).split("-")
                    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
                except (ValueError, IndexError): pass
    except Exception:
        pass

    # Strategy 3: file creation time
    try:
        return datetime.fromtimestamp(Path(audio_path).stat().st_ctime)
    except Exception:
        return datetime.now()


def save_output(category: str, filename: str, content: str) -> str | None:
    """Save analysis output directly to the configured category folder."""
    dest = Path(FOLDERS.get(category, FOLDERS[DEFAULT_CATEGORY]))
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"⚠️  Warning: Could not create output folder: {e}", flush=True)

    p = dest / filename
    try:
        p.write_text(content, encoding="utf-8")
        return str(p)
    except OSError as e:
        print(f"⚠️  Warning: Failed to save output: {e}", flush=True)
        return None


def log_failed_analysis(transcript_path: str, category: str, filename: str) -> None:
    """Append a NEEDS_ANALYSIS entry to the persistent failure log."""
    try:
        with open(FAILED_ANALYSIS_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} | NEEDS_ANALYSIS | {category} | {filename} | {transcript_path}\n")
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
        if not (base := Path(base_path)).exists():
            continue
        try:
            for filepath in base.iterdir():
                filename = filepath.name
                if (not filename.endswith(MARKDOWN_EXT) or filename.endswith(ANALYSIS_SUFFIX)
                        or len(filename) < TIMESTAMP_KEY_LENGTH):
                    continue
                index[filename[:TIMESTAMP_KEY_LENGTH]] = {"category": category, "output_path": str(filepath)}
        except OSError as e:
            print(f"⚠️  Warning: Could not scan {base_path}: {e}", flush=True)

    return index


def is_file_stable(path: str, wait_seconds: int = FILE_STABILITY_WAIT_SECONDS) -> bool:
    """Check if file has finished syncing (not still downloading from iCloud)."""
    p = Path(path)
    try:
        size1 = p.stat().st_size
        if size1 == 0:
            return False
        time.sleep(wait_seconds)
        return size1 == p.stat().st_size
    except OSError:
        return False


def discover_recent_folders(watch_folder: str, days_back: int = 7) -> list[str]:
    """Find date-based subfolders from the last N days, sorted oldest-first."""
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    cutoff = datetime.now() - timedelta(days=days_back)
    recent_folders = []

    try:
        for child in Path(watch_folder).iterdir():
            if not date_pattern.match(child.name) or not child.is_dir():
                continue
            try:
                if (folder_date := datetime.strptime(child.name, "%Y-%m-%d")) >= cutoff:
                    recent_folders.append((folder_date, str(child)))
            except ValueError: pass
    except OSError as e:
        print(f"❌ Cannot list watch folder: {e}", flush=True)
        return []

    return [path for _, path in sorted(recent_folders, key=lambda x: x[0])]


# ============================================================================
# SUPERWHISPER INTEGRATION
# ============================================================================


def switch_superwhisper_mode() -> None:
    """Switch Superwhisper to the configured Custom Mode via deep link."""
    if not SUPERWHISPER_MODE_KEY:
        raise FatalAPIError(
            "superwhisper_mode_key is not set in config.yaml. Default value is 'meeting' — verify in ~/Documents/superwhisper/modes."
        )
    subprocess.run(["open", f"superwhisper://mode?key={SUPERWHISPER_MODE_KEY}"], check=True)
    time.sleep(MODE_SWITCH_SETTLE_SECONDS)  # allow mode switch to settle before file handoff


def handoff_to_superwhisper(file_path: str) -> None:
    """Open audio file in Superwhisper for transcription + analysis in one pass."""
    if not Path(file_path).exists():
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
        return json.loads((path / "meta.json").read_text(encoding="utf-8")).get("llmResult") or ""
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
            f"Superwhisper recordings folder not found: {recordings_dir}. Verify Superwhisper is installed and has been used at least once."
        )

    deadline = time.time() + SUPERWHISPER_TIMEOUT
    print(f"   ⏳ Waiting for Superwhisper (timeout: {SUPERWHISPER_TIMEOUT}s)...", flush=True)

    while time.time() < deadline:
        time.sleep(SUPERWHISPER_POLL_INTERVAL)
        try:
            entries = sorted(recordings_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue

        for entry in entries:
            try:
                if entry.stat().st_mtime <= since:
                    break  # all remaining entries are older than our handoff
            except OSError:
                continue
            text = _read_superwhisper_entry(entry)
            if text and (CATEGORY_HEADER in text or CATEGORY_SECTION_MARKER in text):
                print(f"   📄 Got result from: {entry.name}", flush=True)
                return text

    raise TimeoutError(
        f"Superwhisper did not return a result within {SUPERWHISPER_TIMEOUT}s for: {Path(file_path).name}"
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
    category = DEFAULT_CATEGORY
    filename = DEFAULT_FILENAME
    analysis_start = 0

    for i, line in enumerate(lines):
        if line.startswith(CATEGORY_HEADER):
            category = re.sub(r'[^\x00-\x7F]', '', line.split(CATEGORY_HEADER, 1)[1].strip().upper()).strip()
            if category not in FOLDERS:
                print(f"   ⚠️  Unknown category '{category}', falling back to DEFAULT", flush=True)
                category = DEFAULT_CATEGORY
            analysis_start = i + 1
        elif line.startswith(FILENAME_HEADER):
            filename = line.split(FILENAME_HEADER, 1)[1].strip().replace("/", "-").replace("\\", "-").replace(":", ".").replace("?", "").replace("*", "").replace('"', "") or filename
            analysis_start = i + 1

    # Skip blank lines between headers and body
    while analysis_start < len(lines) and not lines[analysis_start].strip():
        analysis_start += 1

    analysis = "\n".join(lines[analysis_start:]).strip()

    if not analysis:
        raise PermanentFileError(
            "Superwhisper output has no analysis body. Check the Custom Mode prompt outputs CATEGORY: / FILENAME: followed by content."
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
    basename = Path(file_path).name
    attempts = state.get("processed", {}).get(file_path, {}).get("attempts", 0)

    try:
        since = time.time()
        switch_superwhisper_mode()
        handoff_to_superwhisper(file_path)
        raw_output = wait_for_superwhisper_result(file_path, since=since)
        category, ai_filename, analysis = parse_superwhisper_output(raw_output)

        filename = f"{timestamp.strftime(TIMESTAMP_FORMAT)} - {ai_filename}"
        if not filename.endswith(MARKDOWN_EXT):
            filename += MARKDOWN_EXT

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
        state.setdefault("processed", {})[file_path] = {
            "status": "failed_permanent" if attempts >= MAX_RETRIES else "failed_retry",
            "error": str(e),
            "processed_at": datetime.now().isoformat(),
            "attempts": attempts,
        }
        if attempts >= MAX_RETRIES:
            print(f"   🛑 Permanently failed after {attempts} attempts", flush=True)
        else:
            print(f"   🔄 Will retry on next cycle (attempt {attempts}/{MAX_RETRIES})", flush=True)
        save_state(state)
        return False, None
