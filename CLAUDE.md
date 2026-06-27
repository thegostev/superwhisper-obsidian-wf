See [AGENTS.md](AGENTS.md) for the contract every agent (including Claude) must follow
before committing. Short version: run `ruff check --fix . && ruff format .` before every
commit; `ruff format` is the source of truth for layout; don't try to collapse multi-arg
calls onto one line (format re-expands them).
