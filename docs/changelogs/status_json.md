# status.json Schema and Layout Changelog

Changelog for the status.json data contract: the document `schema_version` and
the layout of the published tree that consumers poll. The producer lives in
`scripts/receive_therock/` and the reference document in
`docs/status-json/status_json_reference.jsonc`.

The contract is versioned in-band by the `schema_version` field, not by git
tags. Schema versions follow a `MAJOR.MINOR` scheme: a major bump is a breaking
layout change; a minor bump is a backward-compatible addition that readers on
the same major can ignore. Layout and path changes are listed under the date
they shipped even when they do not move `schema_version`. Entries reference the
pull request that introduced the change.

## schema_version 2.2 - 2026-10-14

### Added

- Per-matrix-cell (Python x framework version) `success` / `failure` /
  `in_progress` / `cancelled` / `skipped` counters on the PyTorch and JAX
  `summary.<platform>.<pipeline>.build`, next to its worst-of `status`.

## schema_version 2.1 - 2026-09-08

### Added

- `build_variant` and `therock_commit` build-metadata fields, carried from the
  orchestrator's setup run (#98).

### Changed

- The producer now restamps any on-disk document that shares the current major
  version to the current `schema_version`, and refuses to merge a document whose
  major version differs (breaking layout change) (#98).

## Layout changes (schema_version 2.0)

These changed the published tree or per-document fields without bumping
`schema_version`.

### 2026-09-21

- Publish a nightly-bkc status.json and compute `latest.json` for it (#102).

### 2026-09-08

- Publish the nightly status.json under `nightly/` instead of
  `release-nightly/` (#93).
- Restructure the prerelease tree: group under `prerelease/` by
  `major.minor` (#94).

## schema_version 2.0 - 2026-07-31

### Added

- Initial versioned status.json schema, defined by the Pydantic document model
  in `therock_status_document.py` (#40). First produced and published for
  TheRock releases in #42 (2026-08-06).
