# Contributing to Quartz

## Branching model

Quartz uses two long-lived branches with distinct purposes:

- **`develop`** — the source of truth for **code changes**. Its history contains
  only human/code commits, making it easy to review what actually changed.
- **`main`** — the deployment/integration branch. In addition to the code
  changes promoted from `develop`, `main` accumulates high-frequency commits
  pushed by automation. This keeps `develop` readable
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

## How changes reach `main`

Syncing from `develop` to `main` is automatic. On every push to `develop`, the
[`Promote develop to main`](.github/workflows/promote_develop_to_main.yml)
workflow merges the pushed commit into `main` (`--no-ff`) and pushes the result.

Notes:

- Each promotion is a `--no-ff` merge commit on `main`, so the promotion points
  stay visible even though `main` is ahead due to bot commits. The merge commit
  is titled `Merge develop (<hash>) into main` and records the promoted commit's
  hash, subject, author, and date in its body, so it is easy to tell the merge
  commit apart from the original `develop` commit.
- If a bot commit on `main` and a change on `develop` touch the same lines, the
  merge conflicts and the workflow run fails. Resolve the conflict manually,
  then re-run or push a fix to `develop`.
- Pushes performed by the workflow do not trigger further workflow runs, so
  there is no promotion loop.
