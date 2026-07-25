"""Shared pipeline: Superwhisper handoff, parsing, file I/O, state management.

All entry points (daemon, on-demand CLI, maintenance CLI) import from here.
"""

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Just Press Record names iCloud-synced files using CET (UTC+1) year-round, ignoring
# Norway's DST (CEST in summer = UTC+2). A Jul 14:31 CEST meeting gets filename "13-31-25".
# Parsing the filename as naive local time silently shifts output .md filenames 1 hour
# behind in summer. JPR_FIXED_CET is the fixed UTC+1 offset; OSLO_TZ is the user's zone.
JPR_FIXED_CET = timezone(timedelta(hours=1))
OSLO_TZ = ZoneInfo("Europe/Oslo")
MODE_SWITCH_SETTLE_SECONDS = 1.0  # let Superwhisper apply the mode before handoff
FILE_STABILITY_WAIT_SECONDS = 2  # iCloud sync settle check
RECORDING_STABILITY_POLLS = 10  # consecutive polls with unchanged mtime → recording is done

# R5: write-stability check — require N consecutive polls with unchanged mtime AND
# unchanged llmResult byte length before returning CATEGORY text. Prevents truncated
# reads when Superwhisper is still appending to llmResult mid-stream.
WRITE_STABILITY_POLLS = 2

# R3: duration-based correlation tolerance. Recording dirs whose `duration` field
# differs from the source audio's duration by more than this fraction are skipped
# during result polling. Each recording dir's duration is set by Superwhisper from
# the source audio, so a correct match is within a few ms; a wrong match (off-by-one
# shift to another handoff's recording) is off by minutes.
DURATION_MATCH_TOLERANCE = 0.05

# R4: sentinel file written into a recording dir after its result has been consumed,
# so a later retry handoff does not re-match the same dir and write a duplicate output.
CONSUMED_SENTINEL = ".pipeline-consumed"

# R7: idle-check tuning for sequencing handoffs to Superwhisper.
SUPERWHISPER_IDLE_CHECK_INTERVAL = 1.0  # seconds between idle checks
SUPERWHISPER_IDLE_CHECK_TIMEOUT = 300  # max wait for Superwhisper to become idle

# R6: scaled poll budget for empty-stub fast-fail. Short audio needs the default 10
# polls (~15s) for stub creation + LLM warmup; long audio needs proportionally more
# for the LLM pass to even start. Bounded to keep the queue moving.
STABILITY_POLLS_FLOOR = 10
STABILITY_POLLS_CEILING = 120
STABILITY_POLLS_PER_30S = 1  # add 1 poll per 30s of audio duration

# R8: salvage-pass re-check interval. recover_failed_permanent runs at daemon startup
# and every N scan cycles, so a stub that completes after startup is still salvaged.
SALVAGE_PASS_EVERY_N_CYCLES = 10

AFINFO_TIMEOUT_SECONDS = 10  # macOS afinfo lookup for audio duration


class FatalAPIError(Exception):
    """Error that should stop the entire service (unrecoverable, e.g. misconfiguration)."""


class PermanentFileError(Exception):
    """Error specific to one file that retrying won't fix (bad format, corrupt audio, etc)."""


def _now_local() -> datetime:
    """Current Europe/Oslo wall-clock as a naive datetime.

    R10 contract: every datetime that flows into state or compares against another
    pipeline datetime is naive Europe/Oslo wall-clock. Using this helper (instead of
    `datetime.now()`) makes the timezone explicit so the daemon doesn't silently
    inherit the host system's tz if it ever changes.
    """
    return datetime.now(tz=OSLO_TZ).replace(tzinfo=None)


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


def get_audio_timestamp(audio_path: str) -> datetime:
    """Extract recording timestamp via mdls → dir/filename parse → file ctime.

    Returns a NAIVE Europe/Oslo wall-clock datetime (tzinfo=None) so callers can
    strftime it for filenames or compare against naive date-folder timestamps.

    Just Press Record names iCloud files using CET (UTC+1) year-round, ignoring
    Norway's DST. The filename fallback path parses the JPR time as JPR_FIXED_CET
    then converts to Europe/Oslo, so a "13-31-25" Jul filename → 14:31:25 CEST.
    The mdls path returns UTC (with +0000 suffix when iCloud sync is complete) and
    is converted to Europe/Oslo. All three paths strip tzinfo before returning.
    """
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
                    parsed = datetime.strptime(raw, fmt)
                    # mdls returns kMDItemContentCreationDate in UTC. When the suffix
                    # is present (+0000) astimezone converts to Europe/Oslo. When iCloud
                    # sync is incomplete mdls can return a naive datetime — assume UTC
                    # rather than returning raw (which would silently shift the output
                    # filename by the UTC offset, e.g. 12:00 UTC → "12.00" instead of
                    # "14.00" CEST).
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(OSLO_TZ).replace(tzinfo=None)
                except ValueError:
                    pass
    except Exception:
        pass

    try:
        for part in reversed(Path(audio_path).parts):
            if len(part) == 10 and part[4] == "-" and part[7] == "-":
                try:
                    dp, tp = part.split("-"), Path(audio_path).stem.split()[0].split("-")
                    naive_jpr = datetime(int(dp[0]), int(dp[1]), int(dp[2]), int(tp[0]), int(tp[1]), int(tp[2]))
                    # JPR filename uses CET (UTC+1 fixed). Convert to Europe/Oslo so
                    # summer recordings (CEST) shift +1h to match actual meeting time.
                    return naive_jpr.replace(tzinfo=JPR_FIXED_CET).astimezone(OSLO_TZ).replace(tzinfo=None)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass

    try:
        return datetime.fromtimestamp(Path(audio_path).stat().st_ctime, tz=OSLO_TZ).replace(tzinfo=None)
    except Exception:
        return datetime.now(tz=OSLO_TZ).replace(tzinfo=None)


def save_output(category: str, filename: str, content: str) -> str | None:
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
    cutoff = _now_local() - timedelta(days=days_back)
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


def switch_superwhisper_mode() -> None:
    """Switch Superwhisper to the configured Custom Mode via deep link."""
    if not SUPERWHISPER_MODE_KEY:
        raise FatalAPIError(
            "superwhisper_mode_key is not set in config.yaml. Default value is 'meeting' — verify in ~/Documents/superwhisper/modes."
        )
    subprocess.run(["open", f"superwhisper://mode?key={SUPERWHISPER_MODE_KEY}"], check=True)
    time.sleep(MODE_SWITCH_SETTLE_SECONDS)  # allow mode switch to settle before file handoff


def handoff_to_superwhisper(file_path: str) -> None:
    if not Path(file_path).exists():
        raise PermanentFileError(f"Audio file not found: {file_path}")
    # R7: wait for Superwhisper to finish any in-flight LLM pass before issuing the
    # next `open -a Superwhisper`. Rapid successive opens while Superwhisper is busy
    # create empty recording stubs that never get filled — the documented known issue.
    _wait_for_superwhisper_idle()
    subprocess.run(["open", file_path, "-a", "Superwhisper"], check=True)


def _read_superwhisper_entry(path: Path) -> str | None:
    try:
        result = json.loads((path / "meta.json").read_text(encoding="utf-8")).get("llmResult")
        return str(result) if result is not None else None
    except (OSError, ValueError):
        return None


def _read_recording_meta(path: Path) -> dict[str, str | bool | int | None] | None:
    """Read meta.json and return the fields relevant to result detection.

    Returns None if meta.json is missing or unreadable. Otherwise a dict with:
      - llm_result: str | None (the Custom Mode LLM output, may be empty/absent)
      - has_transcript: bool (True if result/rawResult present and non-empty)
      - processing_time: int | None (Superwhisper's processingTime field; 0 = stub)
      - llm_processing_time: int | None (languageModelProcessingTime; None = LLM pass not started/finished)
      - duration_ms: int | None (audio duration in milliseconds; 0/None = stub or unknown)
    """
    try:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    llm = meta.get("llmResult")
    raw = meta.get("result") or meta.get("rawResult")
    pt = meta.get("processingTime")
    lpt = meta.get("languageModelProcessingTime")
    duration = meta.get("duration")
    return {
        "llm_result": str(llm) if llm is not None else None,
        "has_transcript": bool(raw or raw == "") and bool(pt is not None or raw),
        "processing_time": int(pt) if isinstance(pt, (int, float)) else None,
        "llm_processing_time": int(lpt) if isinstance(lpt, (int, float)) else None,
        "duration_ms": int(duration) if isinstance(duration, (int, float)) else None,
    }


def get_audio_duration_ms(audio_path: str) -> int | None:
    """Get audio duration in milliseconds via macOS `afinfo`. Returns None on failure."""
    try:
        result = subprocess.run(["afinfo", audio_path], capture_output=True, text=True, timeout=AFINFO_TIMEOUT_SECONDS)
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            low = line.lower()
            if "estimated duration" in low or "duration" in low and "sec" in low:
                # afinfo prints: "estimated duration: 606.000000 sec"
                try:
                    seconds = float(line.split(":", 1)[1].strip().split()[0])
                    return int(seconds * 1000)
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass
    return None


def _is_consumed(recording_dir: Path) -> bool:
    """True if this recording dir has been marked consumed by the pipeline."""
    return (recording_dir / CONSUMED_SENTINEL).exists()


def _mark_consumed(recording_dir: Path) -> None:
    """Write a sentinel file so a later retry does not re-match this recording."""
    try:
        (recording_dir / CONSUMED_SENTINEL).write_text(f"consumed at {_now_local().isoformat()}\n", encoding="utf-8")
    except OSError as e:
        print(f"   ⚠️  Could not mark {recording_dir.name} as consumed: {e}", flush=True)


def _is_superwhisper_idle() -> bool:
    """True if no recording dir has an in-flight LLM pass (lpt present, llmResult absent).

    R7: gate for sequencing handoffs. `open -a Superwhisper` arriving while a prior
    LLM pass is in flight is the documented cause of empty-stub abandonment.
    """
    recordings_dir = Path(SUPERWHISPER_RECORDINGS_DIR)
    if not recordings_dir.exists():
        return True
    try:
        for entry in recordings_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("consumed-") or _is_consumed(entry):
                continue
            info = _read_recording_meta(entry)
            if info and info["llm_processing_time"] is not None and not info["llm_result"]:
                return False
    except OSError:
        pass
    return True


def _wait_for_superwhisper_idle(timeout: int = SUPERWHISPER_IDLE_CHECK_TIMEOUT) -> None:
    """Block until Superwhisper has no in-flight LLM pass, or timeout. R7."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_superwhisper_idle():
            return
        time.sleep(SUPERWHISPER_IDLE_CHECK_INTERVAL)
    print(
        f"   ⚠️  Superwhisper still busy after {timeout}s wait, proceeding anyway",
        flush=True,
    )


def wait_for_superwhisper_result(
    file_path: str,
    since: float,
    expected_duration_ms: int | None = None,
) -> str:
    """Poll until Superwhisper finishes; `since` = time.time() before handoff_to_superwhisper().

    Detection strategy (fast-fail, avoids blocking the queue for SUPERWHISPER_TIMEOUT):
      - llmResult with CATEGORY: header → success, return it (after write-stability check)
      - llmResult present but no CATEGORY: header → LLM refused the contract → PermanentFileError
      - No llmResult but meta.json mtime stable for stability_polls polls and
        transcription completed → analysis pass did not run → PermanentFileError
      - Otherwise keep polling until SUPERWHISPER_TIMEOUT → TimeoutError (transient retry)

    R3: if `expected_duration_ms` is provided, recording dirs whose `duration` field
        differs by more than DURATION_MATCH_TOLERANCE are skipped. Prevents the
        off-by-one misroute where a retry handoff matches another handoff's recording.

    R5: a recording whose llmResult first contains CATEGORY: is NOT returned immediately.
        We require WRITE_STABILITY_POLLS consecutive polls with unchanged mtime AND
        unchanged llmResult byte length before returning. Prevents truncated reads
        when Superwhisper is still appending to llmResult mid-stream.

    R4: matched recordings are marked consumed via a sentinel file so a later retry
        does not re-match the same dir and write a duplicate output.

    R13: the success log line includes the dir name, byte count, and a short hash of
        the first 200 chars of llmResult so post-hoc content attribution is verifiable.

    R6: empty-stub stability threshold scales with audio duration — long audio needs
        more polls for the LLM pass to even start. Floor and ceiling bound the budget.
    """
    recordings_dir = Path(SUPERWHISPER_RECORDINGS_DIR)
    if not recordings_dir.exists():
        raise FatalAPIError(
            f"Superwhisper recordings folder not found: {recordings_dir}. Verify Superwhisper is installed and has been used at least once."
        )

    deadline = time.time() + SUPERWHISPER_TIMEOUT
    print(f"   ⏳ Waiting for Superwhisper (timeout: {SUPERWHISPER_TIMEOUT}s)...", flush=True)

    # R6: scale empty-stub stability threshold to audio duration.
    if expected_duration_ms and expected_duration_ms > 0:
        scaled = STABILITY_POLLS_FLOOR + int(expected_duration_ms / 1000 / 30) * STABILITY_POLLS_PER_30S
        stability_polls = min(STABILITY_POLLS_CEILING, max(STABILITY_POLLS_FLOOR, scaled))
    else:
        stability_polls = RECORDING_STABILITY_POLLS

    # Empty-stub stability tracker: dir name → (last_mtime, count).
    stub_stability: dict[str, tuple[float, int]] = {}
    # R5: write-stability tracker for CATEGORY-bearing llmResults: dir name → (last_mtime, last_size, count).
    write_stability: dict[str, tuple[float, int, int]] = {}

    while time.time() < deadline:
        time.sleep(SUPERWHISPER_POLL_INTERVAL)
        try:
            entries = sorted(recordings_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue

        for entry in entries:
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime <= since:
                break  # all remaining entries are older than our handoff

            # R4: skip recordings we've already consumed.
            if _is_consumed(entry):
                continue

            info = _read_recording_meta(entry)
            if info is None:
                continue  # meta.json not ready yet

            # R3: duration-based correlation. Skip recordings whose duration doesn't
            # match the source audio within tolerance.
            duration_ms = info["duration_ms"]
            if (
                expected_duration_ms
                and isinstance(duration_ms, int)
                and duration_ms > 0
                and abs(duration_ms - expected_duration_ms) > expected_duration_ms * DURATION_MATCH_TOLERANCE
            ):
                continue

            raw_text = info["llm_result"]
            text = raw_text if isinstance(raw_text, str) else None
            if text and (CATEGORY_HEADER in text or CATEGORY_SECTION_MARKER in text):
                # R5: require WRITE_STABILITY_POLLS consecutive polls with unchanged
                # mtime AND unchanged llmResult byte length before returning. Catches
                # mid-stream reads where Superwhisper is still appending to llmResult.
                stable_text = _check_write_stability(write_stability, entry, mtime, text)
                if stable_text is None:
                    continue
                # Stable — safe to return. R13: log dir + hash + bytes for post-hoc verification.
                content_hash = hashlib.sha256(stable_text[:200].encode("utf-8")).hexdigest()[:8]
                print(
                    f"   📄 Got result from: {entry.name} (bytes={len(stable_text)}, hash={content_hash})",
                    flush=True,
                )
                # R4: mark this recording consumed so retries don't re-match it.
                _mark_consumed(entry)
                return stable_text

            # Fast-fail case 1: LLM produced output but no CATEGORY: header = refusal.
            if text and not (CATEGORY_HEADER in text or CATEGORY_SECTION_MARKER in text):
                raise PermanentFileError(
                    f"Superwhisper produced an llmResult without the {CATEGORY_HEADER} contract header "
                    f"for {Path(file_path).name}. The Custom Mode LLM likely refused the transcript "
                    f"(too short, wrong format, or non-meeting audio). First line: {text.splitlines()[0][:120]!r}"
                )

            # Fast-fail case 2: no llmResult but meta.json mtime is stable.
            # Superwhisper creates an empty stub when `open file -a Superwhisper` fires faster
            # than it can process; the stub is never filled in. This is transient — re-opening
            # the file later (when Superwhisper is idle) processes it correctly. Raise
            # TimeoutError so process_audio records failed_retry and re-queues on the next cycle.
            #
            # Important: do NOT fast-fail while the LLM pass is in flight. Superwhisper writes
            # languageModelProcessingTime into meta.json when the LLM pass starts and does not
            # touch mtime again until it writes llmResult — which can take 20-30s for a long
            # meeting. The previous logic (5 polls × 3s = 15s) fast-failed mid-inference,
            # declared the stub abandoned, and retried by re-opening the file, which interrupted
            # the in-flight LLM pass and created a new stub. Treat languageModelProcessingTime
            # present (even with processingTime still 0) as "still processing" and keep waiting.
            if info["llm_processing_time"] is not None:
                # LLM pass is in progress — reset stability counter, do not fast-fail.
                stub_stability.pop(entry.name, None)
                continue

            _check_stub_abandoned(stub_stability, entry, mtime, stability_polls, file_path)

    raise TimeoutError(
        f"Superwhisper did not return a result within {SUPERWHISPER_TIMEOUT}s for: {Path(file_path).name}"
    )


def _check_write_stability(
    write_stability: dict[str, tuple[float, int, int]],
    entry: Path,
    mtime: float,
    text: str,
) -> str | None:
    """R5: require WRITE_STABILITY_POLLS consecutive polls with unchanged mtime AND
    unchanged llmResult byte length before returning the text. Returns the text
    once stable, None if still stabilizing (caller should `continue` the poll loop).
    """
    cur_size = len(text)
    prev = write_stability.get(entry.name)
    if prev is None:
        write_stability[entry.name] = (mtime, cur_size, 1)
        return None
    prev_mtime, prev_size, count = prev
    if mtime != prev_mtime or cur_size != prev_size:
        write_stability[entry.name] = (mtime, cur_size, 1)
        return None
    count += 1
    if count < WRITE_STABILITY_POLLS:
        write_stability[entry.name] = (mtime, cur_size, count)
        return None
    return text


def _check_stub_abandoned(
    stub_stability: dict[str, tuple[float, int]],
    entry: Path,
    mtime: float,
    stability_polls: int,
    file_path: str,
) -> None:
    """Fast-fail case 2: empty stub whose meta.json mtime is stable across N polls.

    Raises TimeoutError if the stub has been stable for `stability_polls` consecutive
    polls (no llmResult, no LLM pass). Otherwise updates the stability tracker and
    returns None — caller should `continue` the poll loop.
    """
    stub_prev = stub_stability.get(entry.name)
    if stub_prev is None:
        stub_stability[entry.name] = (mtime, 1)
        return
    prev_mtime, count = stub_prev
    if mtime != prev_mtime:
        stub_stability[entry.name] = (mtime, 1)  # reset — file still being written
        return
    count += 1
    stub_stability[entry.name] = (mtime, count)
    if count >= stability_polls:
        # meta.json hasn't changed across N polls → Superwhisper dropped this stub.
        # Transient: re-opening the audio file on a later cycle should work.
        raise TimeoutError(
            f"Superwhisper created an empty recording stub for {Path(file_path).name} "
            f"(meta.json stable for {count} polls, no llmResult, no LLM pass started). "
            f"The file-open likely arrived while Superwhisper was busy with a prior handoff. "
            f"Will retry on the next scan cycle. Recording dir: {entry.name}"
        )


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
        elif line.startswith(FILENAME_HEADER):
            filename = line.split(FILENAME_HEADER, 1)[1].strip().translate(_FILENAME_SANITIZE) or filename
        else:
            continue
        analysis_start = i + 1

    analysis_start = next((i for i in range(analysis_start, len(lines)) if lines[i].strip()), len(lines))
    if not (analysis := "\n".join(lines[analysis_start:]).strip()):
        raise PermanentFileError(
            "Superwhisper output has no analysis body. Check the Custom Mode prompt outputs CATEGORY: / FILENAME: followed by content."
        )
    return category, filename, analysis


def process_audio(file_path: str, timestamp, state: dict) -> tuple[bool, str | None]:
    """Full pipeline: mode switch → handoff → parse → save; returns (success, category|None)."""
    attempts = (processed := state.setdefault("processed", {})).get(file_path, {}).get("attempts", 0)

    try:
        # R3+R6: compute source audio duration for correlation + scaled poll budget.
        expected_duration_ms = get_audio_duration_ms(file_path)
        if expected_duration_ms:
            print(f"   📏 Source audio duration: {expected_duration_ms} ms", flush=True)

        since = time.time()
        switch_superwhisper_mode()
        handoff_to_superwhisper(file_path)
        category, ai_filename, analysis = parse_superwhisper_output(
            wait_for_superwhisper_result(file_path, since=since, expected_duration_ms=expected_duration_ms)
        )

        fname = f"{timestamp.strftime(TIMESTAMP_FORMAT)} - {ai_filename.removesuffix(MARKDOWN_EXT)}{MARKDOWN_EXT}"
        if output_path := save_output(category, fname, analysis):
            print(f"   ✅ Analysis saved: {output_path}", flush=True)

        processed[file_path] = {
            "status": "complete",
            "category": category,
            "timestamp": timestamp.isoformat(),
            "processed_at": _now_local().isoformat(),
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
            "processed_at": _now_local().isoformat(),
            "attempts": attempts + 1,
            "expected_duration_ms": expected_duration_ms,
        }
    except Exception as e:
        print(f"   ❌ Failed to process {Path(file_path).name}: {e}", flush=True)
        processed[file_path] = {
            "status": "failed_permanent" if (na := attempts + 1) >= MAX_RETRIES else "failed_retry",
            "error": str(e),
            "processed_at": _now_local().isoformat(),
            "attempts": na,
            "expected_duration_ms": expected_duration_ms,
        }
        print(
            "   "
            + (
                f"🛑 Permanently failed after {na} attempts"
                if na >= MAX_RETRIES
                else f"🔄 Will retry on next cycle (attempt {na}/{MAX_RETRIES})"
            ),
            flush=True,
        )
    save_state(state)
    return False, None


def _parse_recovery_candidate(entry: Path, mtime: float) -> tuple[float, datetime, int, str] | None:
    """Return (mtime, rec_start, duration_ms, llmResult) for a stub with a valid contract, else None."""
    info = _read_recording_meta(entry)
    if not info or not isinstance(info["llm_result"], str):
        return None
    text = info["llm_result"]
    if CATEGORY_HEADER not in text and CATEGORY_SECTION_MARKER not in text:
        return None
    try:
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        rec_start = datetime.fromisoformat(str(meta.get("datetime")).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        rec_start = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return mtime, rec_start, int(meta.get("duration") or 0), text


def _collect_recovery_candidates(recordings_dir: Path, cutoff: float) -> list[tuple[float, datetime, int, str]] | None:
    """Scan the recordings dir for stubs newer than cutoff with a CATEGORY: contract.

    Returns None on OSError (recordings dir unreadable); empty list if no candidates match.
    """
    candidates: list[tuple[float, datetime, int, str]] = []
    try:
        for entry in recordings_dir.iterdir():
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if (cand := _parse_recovery_candidate(entry, mtime)) is not None:
                candidates.append(cand)
    except OSError as e:
        print(f"⚠️  Recovery scan: could not list recordings dir: {e}", flush=True)
        return None
    return candidates


def _derive_audio_ts(path: str, entry: dict) -> datetime | None:
    """Derive the audio recording timestamp from a failed entry's state.

    Failed entries don't carry a `timestamp` field (the success path sets it). Fall
    back to get_audio_timestamp() on the audio path, then to the state's processed_at.
    """
    if entry.get("timestamp"):
        try:
            return datetime.fromisoformat(entry["timestamp"])
        except ValueError:
            pass
    if Path(path).exists():
        return get_audio_timestamp(path)
    try:
        return datetime.fromisoformat(entry["processed_at"])
    except (ValueError, KeyError):
        return None


def _find_best_candidate_by_duration(
    candidates, expected_duration_ms: int | None
) -> tuple[float, datetime, int, str] | None:
    """R8: Pick the most-recently-modified candidate whose duration matches expected within tolerance.

    The previous datetime-window match was unreliable because meta.json's `datetime` field
    is the recording dir's creation time (set by Superwhisper at handoff), not the audio's
    recording start time. When iCloud sync delays the .m4a arrival by ~20h (as on Jul 23),
    the dir creation time is ~20h off from the audio recording time, and the window match
    fails. Duration is a stable per-audio identifier set by Superwhisper from the source
    audio file, so it survives sync delays and dir-creation-time skew.
    """
    if not expected_duration_ms:
        return None
    best = None
    for mtime, rec_start, duration_ms, text in candidates:
        if (
            duration_ms
            and abs(duration_ms - expected_duration_ms) <= expected_duration_ms * DURATION_MATCH_TOLERANCE
            and (best is None or mtime > best[0])
        ):
            best = (mtime, rec_start, duration_ms, text)
    return best


def _find_best_candidate(candidates, audio_utc: datetime) -> tuple[float, datetime, int, str] | None:
    """Legacy datetime-window match — kept as fallback when duration is unavailable.

    Window: [audio_start - 5min, audio_start + duration + 30min]. The wide upper
    bound covers JPR's recording start → stop → SW handoff → LLM pass start.
    """
    best = None
    for mtime, rec_start, duration_ms, text in candidates:
        if rec_start.tzinfo is None:
            # meta.datetime is written by Superwhisper on the user's Mac in local
            # time (Europe/Oslo). Treat naive as Oslo, not UTC — the previous UTC
            # assumption contributed to the legacy match being unreliable.
            rec_start = rec_start.replace(tzinfo=OSLO_TZ)
        delta = (rec_start - audio_utc).total_seconds()
        upper = duration_ms / 1000 + 1800
        if -300 <= delta <= upper and (best is None or mtime > best[0]):
            best = (mtime, rec_start, duration_ms, text)
    return best


def recover_failed_permanent(state: dict, max_age_days: int = 7) -> int:
    """Salvage analysis from failed_permanent entries whose Superwhisper stubs later completed.

    The stability fast-fail can mark a file failed_permanent while Superwhisper is still
    writing the LLM result. The stubs eventually get a valid llmResult; without recovery,
    that work is silently lost. This scans the recordings dir for stubs newer than
    max_age_days whose meta.json has a CATEGORY: contract, matches each against a
    failed_permanent entry by audio duration (R8 — datetime-window match was unreliable
    when iCloud sync delayed the .m4a arrival), and — if matched — parses, saves the
    .md, and flips the state entry to complete.

    Returns the number of entries recovered.
    """
    recordings_dir = Path(SUPERWHISPER_RECORDINGS_DIR)
    if not recordings_dir.exists():
        return 0

    processed = state.get("processed", {})
    failed = {path: entry for path, entry in processed.items() if entry.get("status") == "failed_permanent"}
    if not failed:
        return 0

    cutoff = time.time() - max_age_days * 86400
    candidates = _collect_recovery_candidates(recordings_dir, cutoff)
    if not candidates:
        return 0

    recovered = 0
    for path, entry in failed.items():
        # R8: prefer duration match. Try state-stored duration first, then re-derive
        # from the audio file if it still exists.
        expected_duration_ms = entry.get("expected_duration_ms")
        if not expected_duration_ms and Path(path).exists():
            expected_duration_ms = get_audio_duration_ms(path)

        match = _match_recovery_candidate(candidates, path, entry, expected_duration_ms)
        if match is None:
            continue
        best, matched_by_duration, audio_ts_fallback = match
        _, _, _, text = best
        try:
            category, ai_filename, analysis = parse_superwhisper_output(text)
        except PermanentFileError:
            continue

        fname = (
            f"{audio_ts_fallback.strftime(TIMESTAMP_FORMAT)} - {ai_filename.removesuffix(MARKDOWN_EXT)}{MARKDOWN_EXT}"
        )
        if not (output_path := save_output(category, fname, analysis)):
            continue
        note = (
            "recovered from late-arriving Superwhisper llmResult (duration match)"
            if matched_by_duration
            else "recovered from late-arriving Superwhisper llmResult"
        )
        print(f"   ♻️  {note} for {Path(path).name}: {output_path}", flush=True)
        processed[path] = {
            "status": "complete",
            "category": category,
            "timestamp": audio_ts_fallback.isoformat(),
            "processed_at": _now_local().isoformat(),
            "attempts": entry.get("attempts", 0) + 1,
            "expected_duration_ms": expected_duration_ms,
            "note": note,
        }
        recovered += 1

    if recovered:
        save_state(state)
    return recovered


def _match_recovery_candidate(
    candidates: list[tuple[float, datetime, int, str]],
    path: str,
    entry: dict,
    expected_duration_ms: int | None,
) -> tuple[tuple[float, datetime, int, str], bool, datetime] | None:
    """Find the best Superwhisper stub for a failed_permanent entry.

    Tries duration match first (R8); falls back to legacy datetime-window match if
    duration is unavailable. Returns (best_candidate, matched_by_duration, audio_ts)
    or None if no match. `audio_ts` is Europe/Oslo-aware (naive state timestamps
    are treated as Oslo wall-clock per R10).
    """
    best: tuple[float, datetime, int, str] | None = None
    matched_by_duration = False
    if expected_duration_ms:
        best = _find_best_candidate_by_duration(candidates, expected_duration_ms)
        if best is not None:
            matched_by_duration = True
    if best is None:
        # Fallback to legacy datetime-window match if duration unavailable.
        if (audio_ts := _derive_audio_ts(path, entry)) is None:
            return None
        if audio_ts.tzinfo is None:
            audio_ts = audio_ts.replace(tzinfo=OSLO_TZ)
        audio_utc = audio_ts.astimezone(timezone.utc)
        best = _find_best_candidate(candidates, audio_utc)
        if best is None:
            return None
        return best, False, audio_ts

    # For duration-matched recoveries, still need a timestamp for the output filename.
    audio_ts_fallback = _derive_audio_ts(path, entry)
    if audio_ts_fallback is None:
        return None
    if audio_ts_fallback.tzinfo is None:
        audio_ts_fallback = audio_ts_fallback.replace(tzinfo=OSLO_TZ)
    return best, matched_by_duration, audio_ts_fallback
