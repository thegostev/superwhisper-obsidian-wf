"""Shared pipeline: Superwhisper handoff, parsing, file I/O, state management.

All entry points (daemon, on-demand CLI, maintenance CLI) import from here.
"""

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import (
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
_FILENAME_SANITIZE = str.maketrans("/\\:", "--.", '?"*')

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
    if Path(STATE_FILE).exists():
        try:
            return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Warning: Could not load state file, starting fresh: {e}", flush=True)
    return {"processed": {}}


def save_state(state):
    try:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        print(f"⚠️  Warning: Could not save state file: {e}", flush=True)


# ============================================================================
# HELPERS: TIMESTAMP, PARSING, FILE I/O
# ============================================================================


def get_audio_timestamp(audio_path):
    """Extract recording timestamp via mdls → dir/filename parse → file ctime."""
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemContentCreationDate", "-raw", audio_path],
            capture_output=True,
            text=True,
            timeout=MDLS_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and (raw := result.stdout.strip()):
            for fmt in ["%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"]:
                try:
                    return datetime.strptime(raw, fmt)
                except ValueError:
                    pass
    except Exception:
        pass

    try:
        for part in reversed(Path(audio_path).parts):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                try:
                    dp, tp = part.split("-"), Path(audio_path).stem.split()[0].split("-")
                    return datetime(int(dp[0]), int(dp[1]), int(dp[2]), int(tp[0]), int(tp[1]), int(tp[2]))
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass

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

    try:
        (out := dest / filename).write_text(content, encoding="utf-8")
        return str(out)
    except OSError as e:
        print(f"⚠️  Warning: Failed to save output: {e}", flush=True)
        return None


# ============================================================================
# DISCOVERY & INDEXING
# ============================================================================


def build_transcript_index(folders: dict[str, str]) -> dict[str, dict]:
    """Scan category folders; return lookup dict keyed by "YY-MM-DD HH.MM" for deduplication."""
    index: dict[str, dict] = {}
    for category, base_path in folders.items():
        if (base := Path(base_path)).exists():
            try:
                for filepath in base.glob(f"*{MARKDOWN_EXT}"):
                    if (name := filepath.name).endswith(ANALYSIS_SUFFIX) or len(name) < TIMESTAMP_KEY_LENGTH:
                        continue
                    index[name[:TIMESTAMP_KEY_LENGTH]] = {"category": category, "output_path": str(filepath)}
            except OSError as e:
                print(f"⚠️  Warning: Could not scan {base_path}: {e}", flush=True)
    return index


def is_file_stable(path: str, wait_seconds: int = FILE_STABILITY_WAIT_SECONDS) -> bool:
    """Check if file has finished syncing (not still downloading from iCloud)."""
    try:
        if not (size1 := Path(path).stat().st_size):
            return False
        time.sleep(wait_seconds)
        return size1 == Path(path).stat().st_size
    except OSError:
        return False


def discover_recent_folders(watch_folder: str, days_back: int = 7) -> list[str]:
    """Find date-based subfolders from the last N days, sorted oldest-first."""
    cutoff = datetime.now() - timedelta(days=days_back)
    dated = []
    try:
        for child in Path(watch_folder).iterdir():
            if child.is_dir():
                try:
                    if (d := datetime.strptime(child.name, "%Y-%m-%d")) >= cutoff:
                        dated.append((d, str(child)))
                except ValueError:
                    pass
    except OSError as e:
        print(f"❌ Cannot list watch folder: {e}", flush=True)
        return []

    return [path for _, path in sorted(dated)]


# ============================================================================
# SUPERWHISPER INTEGRATION
# ============================================================================


def switch_superwhisper_mode() -> None:
    """Switch Superwhisper to the configured Custom Mode via deep link."""
    if not SUPERWHISPER_MODE_KEY:
        raise FatalAPIError("superwhisper_mode_key is not set in config.yaml. Default value is 'meeting' — verify in ~/Documents/superwhisper/modes.")
    subprocess.run(["open", f"superwhisper://mode?key={SUPERWHISPER_MODE_KEY}"], check=True)
    time.sleep(MODE_SWITCH_SETTLE_SECONDS)  # allow mode switch to settle before file handoff


def handoff_to_superwhisper(file_path: str) -> None:
    """Open audio file in Superwhisper for transcription + analysis in one pass."""
    if not Path(file_path).exists():
        raise PermanentFileError(f"Audio file not found: {file_path}")
    subprocess.run(["open", file_path, "-a", "Superwhisper"], check=True)


def _read_superwhisper_entry(path: Path) -> str | None:
    """Return the llmResult field from a Superwhisper recording directory's meta.json."""
    try:
        result = json.loads((path / "meta.json").read_text(encoding="utf-8")).get("llmResult")
        return str(result) if result is not None else None
    except (OSError, ValueError):
        return None


def wait_for_superwhisper_result(file_path: str, since: float) -> str:
    """Poll until Superwhisper finishes; `since` = time.time() before handoff_to_superwhisper()."""
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
            if (text := _read_superwhisper_entry(entry)) and (
                CATEGORY_HEADER in text or CATEGORY_SECTION_MARKER in text
            ):
                print(f"   📄 Got result from: {entry.name}", flush=True)
                return text

    raise TimeoutError(f"Superwhisper did not return a result within {SUPERWHISPER_TIMEOUT}s for: {Path(file_path).name}")


def parse_superwhisper_output(raw_output: str) -> tuple[str, str, str]:
    """Parse Superwhisper output → (category, filename, analysis).

    Expects CATEGORY:<name> / FILENAME:<title> header lines followed by the analysis body.
    Falls back to DEFAULT/'Unknown Meeting'; raises PermanentFileError if no body.
    """
    lines = raw_output.strip().split("\n")
    category, filename, analysis_start = DEFAULT_CATEGORY, DEFAULT_FILENAME, 0

    for i, line in enumerate(lines):
        if line.startswith(CATEGORY_HEADER):
            category = re.sub(r"[^\x00-\x7F]", "", line.split(CATEGORY_HEADER, 1)[1].strip().upper()).strip()
            if category not in FOLDERS:
                print(f"   ⚠️  Unknown category '{category}', falling back to DEFAULT", flush=True)
                category = DEFAULT_CATEGORY
            analysis_start = i + 1
        elif line.startswith(FILENAME_HEADER):
            filename = line.split(FILENAME_HEADER, 1)[1].strip().translate(_FILENAME_SANITIZE) or filename
            analysis_start = i + 1

    analysis_start = next((i for i in range(analysis_start, len(lines)) if lines[i].strip()), len(lines))
    if not (analysis := "\n".join(lines[analysis_start:]).strip()):
        raise PermanentFileError("Superwhisper output has no analysis body. Check the Custom Mode prompt outputs CATEGORY: / FILENAME: followed by content.")
    return category, filename, analysis


# ============================================================================
# MAIN PIPELINE
# ============================================================================


def process_audio(file_path: str, timestamp, state: dict) -> tuple[bool, str | None]:
    """Full pipeline: mode switch → handoff → parse → save; returns (success, category|None)."""
    attempts = (processed := state.setdefault("processed", {})).get(file_path, {}).get("attempts", 0)

    try:
        since = time.time()
        switch_superwhisper_mode()
        handoff_to_superwhisper(file_path)
        raw_output = wait_for_superwhisper_result(file_path, since=since)
        category, ai_filename, analysis = parse_superwhisper_output(raw_output)

        if output_path := save_output(
            category,
            f"{timestamp.strftime(TIMESTAMP_FORMAT)} - {ai_filename.removesuffix(MARKDOWN_EXT)}{MARKDOWN_EXT}",
            analysis,
        ):
            print(f"   ✅ Analysis saved: {output_path}", flush=True)

        processed[file_path] = {
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
        print(f"   🛑 Permanent error for {Path(file_path).name}: {e}", flush=True)
        processed[file_path] = {
            "status": "failed_permanent",
            "error": str(e),
            "processed_at": datetime.now().isoformat(),
            "attempts": attempts + 1,
        }
        save_state(state)
        return False, None

    except Exception as e:
        print(f"   ❌ Failed to process {Path(file_path).name}: {e}", flush=True)
        processed[file_path] = {
            "status": "failed_permanent" if (na := attempts + 1) >= MAX_RETRIES else "failed_retry",
            "error": str(e),
            "processed_at": datetime.now().isoformat(),
            "attempts": na,
        }
        if na >= MAX_RETRIES:
            print(f"   🛑 Permanently failed after {na} attempts", flush=True)
        else:
            print(f"   🔄 Will retry on next cycle (attempt {na}/{MAX_RETRIES})", flush=True)
        save_state(state)
        return False, None
