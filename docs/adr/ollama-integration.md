# Ollama Integration

RecordingAnalyser uses a local [Ollama](https://ollama.com) server for the analysis and classification stage of the pipeline. This replaces the previous cloud providers (Gemini, Claude Sonnet) with fully local inference — no API keys, no rate limits, no network dependency beyond the initial model pull.

---

## Overview

Ollama handles the second stage of the pipeline: taking a completed transcript and producing a category, a filename, and a written analysis. Transcription (Whisper) runs independently before this stage.

```
Audio → Whisper (local) → Transcript → Ollama (local) → Category + Filename + Analysis
```

If analysis fails after all retries, the transcript is still saved to the `DEFAULT` category folder and logged to `failed_analysis.log` for manual review. The transcript is never lost due to analysis failure.

---

## Configuration

Settings live in `config.yaml`:

```yaml
analysis_provider: ollama
ollama_model: "qwen3.5:9b"
ollama_host: "http://localhost:11434"
# ollama_thinking: false  # set true to enable extended reasoning (~2–3 min/file)
```

| Setting | Default | Purpose |
|---|---|---|
| `analysis_provider` | `"ollama"` | Selects the analysis backend |
| `ollama_model` | `"qwen3.5:9b"` | Model tag as known to local Ollama |
| `ollama_host` | `"http://localhost:11434"` | URL of the local Ollama server |
| `ollama_thinking` | `false` | Enables Qwen extended reasoning mode |

The model is swappable — any model pulled into local Ollama can be used (e.g., `mistral`, `llama3`, `neural-chat`).

---

## Implementation

### Dependency

```
ollama>=0.3.0   # pyproject.toml line 11
```

The Ollama Python SDK is used directly (not subprocess, not raw HTTP).

### Key code locations

| Concern | File | Lines |
|---|---|---|
| SDK import + availability check | `pipeline.py` | 24–28 |
| Global client singleton | `pipeline.py` | 75 |
| Configuration constants | `config.py` | 63–66 |
| Client initialisation | `pipeline.py` | 146–155 (`configure_ollama()`) |
| Single analysis attempt | `pipeline.py` | 542–558 (`analyze_with_ollama()`) |
| Retry wrapper | `pipeline.py` | 561–588 (`analyze_with_retry()`) |
| Fallback on total failure | `pipeline.py` | 609–626 (`process_audio()`) |
| Daemon startup | `auto_transcribe.py` | 159 |
| Maintenance CLI startup | `reclassify_and_fix.py` | 246 |

### Client initialisation

`configure_ollama()` is called once at process startup (daemon or CLI). It checks that the provider is set to `"ollama"`, then creates a singleton `ollama.Client` bound to `OLLAMA_HOST`. The singleton is stored in the module-level `_ollama_client` and reused for every call.

```python
_ollama_client = _ollama_lib.Client(host=OLLAMA_HOST)
```

### Analysis call

Each analysis attempt sends a two-message chat to the local server:

```python
response = _ollama_client.chat(
    model=OLLAMA_MODEL,
    messages=[
        {"role": "system", "content": ANALYSIS_PROMPT},
        {"role": "user",   "content": f"---TRANSCRIPT TO ANALYZE---\n{transcript_content}"},
    ],
    think=OLLAMA_THINKING,
)
```

`think=True` enables Qwen's extended reasoning mode. It significantly increases quality on complex transcripts but adds roughly 2–3 minutes per file. Disabled by default.

The response content is passed to `parse_analysis_response()`, which extracts the structured `CATEGORY`, `FILENAME`, and analysis body fields.

---

## Retry and error handling

All Ollama errors are treated as transient. The retry schedule is:

```
Attempt 1: immediate
Attempt 2: wait 60 s
Attempt 3: wait 180 s
Attempt 4: wait 300 s
```

After four failed attempts, `analyze_with_retry()` returns `None`. The caller (`process_audio()`) then:

1. Saves the transcript to the `DEFAULT` category folder with a generic filename.
2. Appends an entry to `failed_analysis.log` for manual review.

The transcript is written to disk before analysis starts, so analysis failure cannot cause data loss.

To retry analysis for files where it previously failed, run:

```bash
python reclassify_and_fix.py --generate-missing-analysis
```

---

## Relationship to other providers

Gemini and Claude Sonnet are disabled. Their import statements and client-setup functions are either commented out or stubbed (`configure_gemini()` and `configure_claude()` are no-ops). There is no active provider fallback — if Ollama exhausts all retries, the pipeline falls back to the `DEFAULT` category rather than switching to a cloud provider.

The ADR that records the decision to move to Ollama and disable cloud providers is at `docs/adr/0006-classification-in-analysis-stage.md`.
