# Notify Quartz Action Changelog

Changelog for the `notify_quartz` composite action
(`.github/actions/notify_quartz/action.yml`) and its backing script
(`scripts/notify_quartz/notify_quartz.py`).

Versions follow [Semantic Versioning](https://semver.org/). Releases are tagged
on `main` as `notify_quartz/vMAJOR.MINOR.PATCH` (plus a moving
`notify_quartz/vMAJOR` tag). Entries reference the pull request that introduced
the change.

## v1.1.3 - 2026-10-13

### Security

- Set `persist-credentials: false` on the Quartz script checkout so the job
  token is not written to `.git/config` and left in the workspace after
  checkout, closing the credential persistence flagged by the zizmor
  `artipacked` audit (#122).
- Select the Python interpreter through an intermediate `PYTHON` environment
  variable instead of inlining the `runner.os` expression in `run:`, clearing
  the CodeQL code-injection warning. Behavior is unchanged (#122).

## v1.1.2 - 2026-09-10

### Security

- Scope the GitHub App token minted for the Quartz dispatch to
  `permission-actions: write` instead of inheriting the app's full installation
  permissions (#90).

## v1.1.1 - 2026-08-12

### Fixed

- Invoke the script with `python3` on Linux and `python` on Windows so the
  action runs on both runner families (#74).

## v1.1.0 - 2026-08-10

### Added

- Soft-failure mode: when no GitHub App credentials are supplied the action
  emits a warning and skips the notification instead of failing the job. This
  keeps TheRock standalone CI green where the Quartz secrets are not defined
  (#61, #62).

## v1.0.0 - 2026-08-07

### Added

- Initial `notify_quartz` composite action: checks out the Quartz
  `notify_quartz.py` script into a private path, mints a scoped GitHub App
  token, and dispatches the Quartz ingest workflow with the caller's workflow
  inputs and captured job outputs (#53).
