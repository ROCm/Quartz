# Contributing to Quartz

Quartz stays closely in sync with [ROCm/TheRock](https://github.com/ROCm/TheRock).
For the general contribution policies — AI tool use, `pre-commit` setup, coding
style guides, branch naming, and the pull request / review flow — follow
[TheRock's `CONTRIBUTING.md`](https://github.com/ROCm/TheRock/blob/main/CONTRIBUTING.md).

This file covers only what is specific to Quartz: its two-branch layout and how
changes are synced from `develop` to `main`.

> [!IMPORTANT]
> **Open all code-change PRs against `develop`, never against
> `main`.** `main` is written only by automation; human changes land on
> `develop` and are synced to `main` automatically.

## Branching model

Quartz uses two long-lived branches with distinct purposes:

- **`develop`** — the source of truth for **code changes**. Its history contains
  only human/code commits, making it easy to review what actually changed.
- **`main`** — the production branch. In addition to the code
  changes synced from `develop`, `main` accumulates high-frequency commits
  pushed by automation (the ingestion bot). This keeps `develop` readable
  while `main` holds the full, up-to-date state.

```text
develop:  o──o──o                (code changes only)
             \   \
main:     o───o───o──b──b──b──o  (b = bot commits, o = synced merges)
```

## Where to make changes

- **All code changes go to `develop`.** Open pull requests against `develop`,
  not `main`.
- **Do not push directly to `main`.** Direct changes there are reserved for the
  ingestion automation and for the sync workflow described below.

## Syncing `develop` to `main`

Syncing is automatic. On every push to `develop`, the
[`Sync develop to main`](.github/workflows/sync_develop_to_main.yml)
workflow merges the pushed commit into `main` (`--no-ff`) and pushes the result.

Notes:

- Each sync is a `--no-ff` merge commit on `main`, so the sync points
  stay visible even though `main` is ahead due to bot commits. The merge commit
  is titled `Merge develop (<hash>) into main` and records the synced commit's
  hash, subject, author, and date in its body, so it is easy to tell the merge
  commit apart from the original `develop` commit.
