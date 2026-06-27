# Agent contract

This repo is maintained partly by autonomous refactor agents whose goal is terse,
low-line-count code. That is welcome — **but it must pass the CI gates by construction,
not by fighting them.** Read this before committing.

## The one rule that prevents broken CI

Before every commit, run:

```sh
ruff check --fix .
ruff format .
```

(or `pre-commit run --all-files` — same thing, pinned to the CI ruff version).

If you do this, the `lint` job cannot fail. `ruff format` is the **single source of
truth for layout**: whatever it produces is correct by definition. Do not hand-format
against it.

## Why CI broke before (do not repeat)

Earlier the line-golfing and the gates were in permanent conflict, so every CI fix got
re-broken on the next refactor. The gates have since been reconciled with the terse
style (see comments in `pyproject.toml`). The remaining conflicts you must respect:

- **`ruff format` re-expands multi-argument calls** that you collapse onto one line,
  e.g. `print(f"…long…", flush=True)`. You cannot win this — format will always wrap
  it back. **Stop counting these as line wins.** Golf elsewhere (dead code, redundant
  temps, walrus, merged imports that still fit on one line).
- **Long string literals are exempt from line length** (`E501` is ignored) because
  `ruff format` never splits strings. A long f-string is fine; just run `ruff format`
  so the surrounding call is wrapped consistently.
- **`E701`/`E702` one-liners are allowed** (`if x: return`, `a; b`). Keep them if you like.

## Tooling (pinned — match these exactly)

| Tool   | Version  | CI command                                  |
|--------|----------|---------------------------------------------|
| ruff   | 0.15.18  | `ruff check .` then `ruff format --check .`  |
| mypy   | 2.1.0    | `mypy .`                                     |
| pytest | latest   | `pytest tests/ --cov=.`                      |

`mypy` still runs. `warn_return_any` is **off** (so `return json.loads(...).get(x)`
is fine), but mypy will still reject real type errors — fix those, don't silence them.

## Definition of done for any change

```sh
ruff check . && ruff format --check . && mypy . && pytest tests/
```

All four must pass locally before you push. They are exactly what CI runs.
