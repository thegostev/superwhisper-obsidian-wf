# Scheduled-run git push & CI runbook (superwhisper-obsidian-wf)

Resolves the operational half of Linear LAG-687 ("Agent sandbox cannot push to
superwhisper-obsidian-wf — every scheduled run stops before CI"). That issue's
symptoms were environmental: the 2026-09-18 scheduled run executed in a cloud
sandbox whose egress proxy did not have `thegostev/superwhisper-obsidian-wf` in
its authorized repository set — `git push` got a proxy 403 and the GitHub API
refused every call, so commit → push → CI could never complete there. That
remains a human step (add the repo to the session's sources with push access).

Scheduled runs now execute on the maintenance Mac, which holds real
credentials. This runbook is the verified path for the push + CI steps of the
"scheduled run" task; it was exercised end-to-end when this document landed
(LAG-687 verification evidence on the issue).

## Environment facts (verified 2026-09-20)

- Repo checkout: `/Users/harald/Documents/Obsidian/Utvikling/1 - Code/SuperwhisperObsidianWF/superwhisper-obsidian-wf/`
- GitHub CLI: `/opt/homebrew/bin/gh`, authenticated as `thegostev`; token scopes
  include `repo`. Push permission is verifiable per session with
  `gh api repos/thegostev/superwhisper-obsidian-wf --jq .permissions.push` → `true`.
- Git-over-HTTPS auth rides the macOS keychain credential helper
  (`credential.helper=osxkeychain`); `gh` is used for API calls
  (`run list`, `run watch`, `pr create`), not for injecting push credentials.
- Merges go through PRs (PR #16 flow). CI (`.github/workflows/ci.yml`) triggers
  on `push` to `main` and on `pull_request` targeting `main`: fast tests
  (ubuntu + macos, py3.12/3.13, `--cov-fail-under=15`), lint (ruff check +
  format), typecheck (mypy), quality (radon/vulture/pydeps/pip-audit). Slow
  tests additionally run on `main` pushes only.
- Local pre-commit hooks mirror CI exactly: commit stage runs ruff (fix+format),
  mypy, and the pytest fast subset; push stage re-runs all three as read-only
  gates. A green local commit is therefore a strong CI predictor.

## Procedure

1. Branch from a fresh `origin/main`, named like Linear's suggested
   `gitBranchName` (`trives/lag-<n>-<slug>`). Do not reuse stale local branches:
   check `git branch -vv` — an upstream marked `[gone]` means the PR was
   squash-merged and its content is already on `main`.
2. Commit. Never use `--no-verify` unless a hook failure is pre-existing and
   documented on the issue; fix or revert instead.
3. `git push -u origin HEAD`.
4. `gh pr create --fill` (or an explicit title/body referencing the issue id).
5. Check CI on the pushed commit:
   `gh run list --limit 3 --branch <branch>` then `gh run watch <run-id>`.
   The coverage bot comments on the PR when `test-fast` is green.
6. Merge squash when green; CI re-runs on the `main` push (including slow
   tests). Only then is the Linear issue eligible for Done.

## If a scheduled run is back in the sandbox

The failure signature is exactly LAG-687's: proxy `access denied by the git
proxy ... is not in this session's authorized repository set`, and the GitHub
API returning *"GitHub access to this repository is not enabled for this
session. Use add_repo to request access"*. The `add_repo` tool the error names
is not exposed to the agent, so this is not self-serviceable — file it on
LAG-687 (or its follow-up) with the evidence and fall back to attaching a
`git format-patch` to the issue. An issue MUST NOT be set Done on a
locally-green-but-unpushed change: green CI on the upstream commit is part of
the task's definition of done.