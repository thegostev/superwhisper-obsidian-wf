"""Programmatic Superwhisper analysis seam (LAG-1057).

Public API::

    analyze_file(audio_path, *, mode_key, timeout=3600, contract_check=None,
                 recordings_dir=None, lock_path=None) -> AnalyzerResult
    superwhisper_lock(lock_path=None)  # cross-process flock context manager

This module is deliberately import-light: it imports only the standard library
at module level. ``pipeline`` is imported lazily inside ``analyze_file``
because importing it loads ``config.py``, which reads ``locations/config.yaml``
(or ``$SWOWF_CONFIG_PATH``) at import time and exits when no config exists.

Minimum configuration a caller must provide
--------------------------------------------

* A loadable config for ``config.py``: ``locations/config.yaml`` in the repo,
  or ``$SWOWF_CONFIG_PATH`` pointing at a config file (the test suite's
  pattern). The pipeline still imports config even when ``recordings_dir`` is
  passed, because it needs its other defaults (timeouts, poll interval, mode
  key for the daemon). At minimum the config should set
  ``superwhisper_recordings_dir`` (default
  ``~/Documents/superwhisper/recordings``).
* If the checkout is not inside an ``Obsidian`` folder, set
  ``SWOWF_OBSIDIAN_BASE`` unless every config path is absolute.
* A Superwhisper Custom Mode whose key is passed as ``mode_key``. The podcast
  mode key is ``podcast`` - a safe-key copy of a mode whose display key
  contains spaces, which the ``superwhisper://mode?key=`` deep link silently
  rejects. Never hardcode it; the caller supplies it.
* Shared lock path: ``~/.superwhisper.lock`` by default, overridable with
  ``lock_path=`` or ``$SWOWF_LOCK_PATH``. The meetings daemon takes the same
  lock (``pipeline.process_audio``) so the two never interleave handoffs.

The daemon owns its heartbeat and state files. ``analyze_file`` never writes
them: the pipeline calls it drives run with ``heartbeat=False``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCK_PATH = Path.home() / ".superwhisper.lock"
LOCK_PATH_ENV = "SWOWF_LOCK_PATH"
MODES_SUBDIR = Path("Documents") / "superwhisper" / "modes"


@dataclass(frozen=True)
class Segment:
    """One word-level transcript segment from ``meta.json``."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class AnalyzerResult:
    """Outcome of one Superwhisper analysis run.

    ``transcript`` is ``meta.json``'s ``rawResult`` (None when Superwhisper did
    not write one), ``analysis`` is its ``llmResult`` (the Custom Mode output
    that satisfied the contract check), ``recording_dir`` names the session
    directory the result came from, and ``duration_ms`` is the recording's
    ``duration`` field. ``segments`` and ``mode_name`` are best-effort.
    """

    transcript: str | None
    analysis: str | None
    recording_dir: Path
    duration_ms: int | None
    segments: tuple[Segment, ...] = ()
    mode_name: str | None = None


def resolve_lock_path(lock_path: str | Path | None = None) -> Path:
    """Return the shared lock path: explicit argument, env override, default."""
    if lock_path is not None:
        return Path(lock_path).expanduser()
    env_path = os.environ.get(LOCK_PATH_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_LOCK_PATH


@contextlib.contextmanager
def superwhisper_lock(lock_path: str | Path | None = None) -> Iterator[Path]:
    """Hold the cross-process Superwhisper handoff lock for the ``with`` body.

    An exclusive ``fcntl.flock`` on ``~/.superwhisper.lock`` (override with
    ``lock_path`` or ``$SWOWF_LOCK_PATH``). The lock is released on normal exit,
    on exception, and on process death (the kernel drops the flock with the
    file description). The lock file itself carries no state; its contents are
    never read.
    """
    path = resolve_lock_path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def analyze_file(
    audio_path: str | Path,
    *,
    mode_key: str,
    timeout: float = 3600,
    contract_check: Callable[[str], bool] | None = None,
    recordings_dir: Path | None = None,
    lock_path: Path | None = None,
) -> AnalyzerResult:
    """Analyze one audio file through a Superwhisper Custom Mode.

    Holds the shared Superwhisper lock from the mode switch through the result,
    so a concurrent daemon handoff cannot interleave. Inside the lock it reuses
    the pipeline's existing mode switch, handoff, R7 idle wait, R3 duration
    match, R5 write stability, R7 accept window and R4 consumed sentinel - this
    function adds no second copy of the wait logic.

    Args:
        audio_path: Audio file to hand to Superwhisper.
        mode_key: Superwhisper Custom Mode key (for example ``"podcast"``).
            Never read from ``SUPERWHISPER_MODE_KEY``; the caller owns it.
        timeout: Wait budget for the result, in seconds (default 3600). The
            pipeline's own fast-fail and accept-window rules still apply.
        contract_check: Success predicate over ``llmResult``. ``None`` means
            "any non-empty llmResult is success", which is right for modes that
            emit cleaned text instead of the meetings ``CATEGORY:`` header. The
            meetings daemon keeps its ``CATEGORY:`` rule because
            ``wait_for_superwhisper_result`` retains that default when the
            check is not supplied.
        recordings_dir: Superwhisper recordings directory; defaults to the
            configured ``SUPERWHISPER_RECORDINGS_DIR``.
        lock_path: Shared lock file; defaults to ``~/.superwhisper.lock`` or
            ``$SWOWF_LOCK_PATH``.

    Returns:
        AnalyzerResult carrying the transcript, analysis, recording dir,
        duration, segments and (best-effort) mode display name.

    Raises:
        FileNotFoundError: ``audio_path`` does not exist.
        pipeline.FatalAPIError: misconfiguration or missing recordings dir.
        pipeline.PermanentFileError: the LLM refused the contract.
        TimeoutError: no result within the budget or an abandoned stub.
        pipeline.SuperwhisperNotAcceptingError: Superwhisper never took the
            handoff within the accept window.
        NotImplementedError: running where ``fcntl`` is unavailable (POSIX
            only).
    """
    import pipeline  # deliberate lazy import; see module docstring

    path = Path(audio_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    check: Callable[[str], bool] = contract_check if contract_check is not None else _non_empty_result
    rec_dir = (
        Path(recordings_dir).expanduser() if recordings_dir is not None else Path(pipeline.SUPERWHISPER_RECORDINGS_DIR)
    )
    expected_duration_ms = pipeline.get_audio_duration_ms(str(path))

    with superwhisper_lock(lock_path):
        pipeline.switch_superwhisper_mode(mode_key=mode_key)
        since = time.time()
        handed_off_at = pipeline.handoff_to_superwhisper(
            str(path),
            recordings_dir=rec_dir,
            heartbeat=False,
        )
        result = pipeline.wait_for_superwhisper_result(
            str(path),
            since=since,
            expected_duration_ms=expected_duration_ms,
            handed_off_at=handed_off_at,
            timeout=timeout,
            contract_check=check,
            recordings_dir=rec_dir,
            heartbeat=False,
        )

    return AnalyzerResult(
        transcript=result.transcript,
        analysis=result.analysis,
        recording_dir=rec_dir / result.recording_dir,
        duration_ms=result.duration_ms,
        segments=result.segments,
        mode_name=_read_mode_name(mode_key),
    )


def _non_empty_result(text: str) -> bool:
    """Default ``analyze_file`` contract: any non-blank llmResult succeeds."""
    return bool(text.strip())


def _read_mode_name(mode_key: str) -> str | None:
    """Best-effort display name from ``~/Documents/superwhisper/modes/<key>.json``.

    Returns None when the mode file is absent or unreadable - the analysis
    result does not depend on this metadata.
    """
    try:
        mode_file = Path.home() / MODES_SUBDIR / f"{mode_key}.json"
        data = json.loads(mode_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return str(name) if name else None
