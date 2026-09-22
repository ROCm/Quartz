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

## Security scanners

Separately from the correctness checks covered by TheRock's `CONTRIBUTING.md`,
this repository scans for secrets, unsafe Python, and workflow
vulnerabilities. These run in CI via
[`security_scan_pr.yml`](.github/workflows/security_scan_pr.yml), which calls
the shared [`ROCm/rocm-security-gh`](https://github.com/ROCm/rocm-security-gh)
reusable workflow. See
[the automated security scanning section in `SECURITY.md`](SECURITY.md#automated-security-scanning)
for how the PR-time and weekly workflows fit together.

Each scanner is runnable locally against the same configuration CI uses,
which is faster than pushing a commit to see what CI says. The
configurations live at the repo root:

```bash
# Secrets, working tree only. Recommended much faster, and usually what you want locally.
gitleaks detect --source . --config gitleaks.toml --redact --no-banner --no-git

# Secrets, over the full git history. Takes longer than the working tree one above.
gitleaks detect --source . --config gitleaks.toml --redact --verbose --no-banner

# Unsafe patterns in Python (pip install bandit).
bandit --configfile bandit.yml --severity-level low --recursive .

# GitHub Actions workflow vulnerabilities (pip install zizmor).
zizmor --persona regular --config zizmor.yml .

# Dependency vulnerabilities and misconfigurations (see trivy docs).
trivy fs --config trivy.yml --severity LOW,MEDIUM,HIGH,CRITICAL --scanners misconfig,vuln .
```

> [!NOTE]
> These commands report every severity, while CI only fails on `HIGH` (and
> `CRITICAL` for trivy). Expect more output locally than a red CI check implies.
>
> The commands also scan the whole repository, while pull request runs default
> to scanning only what the pull request changed. A full-history `gitleaks` run
> in particular reports pre-existing findings that the pull request check does
> not.

CodeQL is not in the list above: it runs in CI only, against the org-wide
default configuration (Quartz does not override it locally).
