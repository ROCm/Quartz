# status.json Consumer Changelog

Changelog for the status.json reader `scripts/consumer/read_status_json.py`.

Versions follow [Semantic Versioning](https://semver.org/). Only the current
release is tagged on `main` as `consumer/vMAJOR.MINOR.PATCH` (plus a moving
`consumer/v1`); earlier versions are recorded here for history but not tagged.
Each entry references the pull request that introduced the change, whose squash
commit on `main` pins that version if you need to check out an older one.

This version tracks the consumer script itself. It is independent of the
status.json `schema_version` the reader understands (see
[status_json.md](status_json.md)): the reader currently supports schema major
`2`.

## v1.2.1 - 2026-10-06 (tagged `consumer/v1.2.1`)

### Fixed

- `build_tarball_url` now percent-encodes the tarball filename via
  `urllib.parse.quote`, so bkc versions containing `+` (for example
  `...+rocm7.0.0`) resolve to a valid download URL instead of a 404. The
  function signature is unchanged (#118).

## v1.2.0 - 2026-09-08 (not tagged)

### Added

- Read the schema-2.1 build-metadata fields emitted by the producer.
- Guard against incompatible documents: the reader rejects a status.json whose
  `schema_version` major differs from the supported major and accepts newer
  minors within that major (#98).

### Changed

- Default source (`DEFAULT_SOURCE`) now points at the `nightly/latest.json`
  endpoint instead of `release-nightly/latest.json`, matching the new published
  tree layout (#93).
- Docstring pointer reference updated for the restructured prerelease tree
  (`prerelease/`) (#94).

## v1.1.0 - 2026-09-02 (not tagged)

### Added

- Interactive dry-run mode and poll de-duplication so repeated polls do not
  reprocess an unchanged document (#91).

### Fixed

- Follow a published `latest.json` symlink once when `raw.githubusercontent.com`
  serves it as a pointer to its target path, resolving the real document (#91).

## v1.0.0 - 2026-08-03 (not tagged)

### Added

- Initial status.json reader / polling example: loads a status document from a
  URL or local path, exposes typed accessors (`rocm_version`,
  `schema_version`, per-platform status), and builds tarball download URLs
  (#32).
