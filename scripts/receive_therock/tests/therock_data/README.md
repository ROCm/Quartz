# TheRock test data

`DISPATCH_PAYLOAD` envelopes used by the receive_therock tests. There are two
distinct families, kept separate because they serve different purposes:

- **Captured** (`multi_arch_*`) — real envelopes captured from successful
  `receive_therock_data.yml` runs, pretty-printed, one per TheRock workflow /
  event_type. These are full-fidelity payloads used to test parsing and
  status.json creation, and to benchmark wire size against the dispatch input
  cap (see [Captured payloads](#captured-payloads)).
- **Synthetic** (`nightly_*`) — hand-written minimal envelopes on a tracked
  release tier, used to drive the end-to-end smoke test
  (`therock_e2e_smoke_test.py`) since the captured payloads are all `dev` builds
  the candidacy gate rejects (see [Synthetic payloads](#synthetic-payloads)).

## Captured payloads

Real `DISPATCH_PAYLOAD` envelopes captured from successful
`receive_therock_data.yml` runs on branch `users/cgoea/quartz`,
pretty-printed. One file per TheRock workflow / event_type seen in the
100 most recent successful runs. Used to test parsing + status.json creation.

### Size report

The wire form is the compact (`DISPATCH_PAYLOAD`) JSON the workflow
receives as a string input. % is against a 65000-char reference budget.

| file                                                      | event_type               | pretty chars | compact chars | % of 65k (compact) |
| --------------------------------------------------------- | ------------------------ | -----------: | ------------: | -----------------: |
| `multi_arch_build_native_linux_packages_completed.json`   | workflow_run_completed   |        38101 |         27952 |              43.0% |
| `multi_arch_build_native_linux_packages_in_progress.json` | workflow_run_in_progress |         4974 |          4082 |               6.3% |
| `multi_arch_build_portable_linux_completed.json`          | workflow_run_completed   |        31933 |         23572 |              36.3% |
| `multi_arch_build_windows_completed.json`                 | workflow_run_completed   |        31375 |         23189 |              35.7% |
| `multi_arch_release_completed.json`                       | workflow_run_completed   |        28773 |         21264 |              32.7% |

### Source workflows captured

From the 100 most recent successful runs, only these workflow files
appeared as `workflow_run` payloads:

- `multi_arch_build_native_linux_packages.yml` (completed + in_progress)
- `multi_arch_build_portable_linux.yml` (completed)
- `multi_arch_build_windows.yml` (completed)
- `multi_arch_release.yml` (orchestrator, completed)

Other registered workflows in `WORKFLOW_SPECS` (tarballs, release wheels,
windows artifacts, wsl rocdxg, setup_multi_arch) were not dispatched in
this window, so no captured payload exists for them yet.

## Synthetic payloads

Hand-written minimal envelopes (`nightly_*`) for the end-to-end smoke test.
The captured payloads above are all `dev` builds on `ROCm/Quartz-Tester`, so the
status.json candidacy gate (release-tracking repo `rocm/rockrel`, tracked
tiers `nightly`/`prerelease`) rejects them. To drive the full receive pipeline
end-to-end (`therock_e2e_smoke_test.py`) we need a tracked-tier sequence, so
these are hand-written on `ROCm/rockrel`, release_type `nightly`, version
`7.14.0a20260619`:

- `nightly_build_portable_linux_completed.json` (linux `rocm/build`)
- `nightly_build_windows_completed.json` (windows `rocm/build`)
- `nightly_build_native_linux_packages_deb_completed.json` (linux `native_packages/deb`)
- `nightly_release_completed.json` (top-level `multi_arch_release` orchestrator)

They carry only the fields the parser/classifier/updater read; they are not
full GitHub API captures (and so are not in the size report above). Replayed in
order, the three leaves leave the release `in_progress` (capped) and the
orchestrator event finalizes it to `success`.

## Published documents

Distinct from the `DISPATCH_PAYLOAD` envelopes above: these are full
`status.json` **documents** as published by Quartz, not input events.

- `published_status_v2_0_nightly.json` — a real schema-`2.0` nightly document
  captured verbatim from `ROCm/Quartz` `main`
  (`release-nightly/20260905/status.json`). Frozen snapshot; do not edit. Used
  by `test_reads_real_published_older_minor_document` as a backward-compatibility
  guard: the current model must still parse the whole document, restamp its
  `schema_version` to the current value, and round-trip it. It is a full-shape
  document (~220 KB), so it is intentionally larger than the envelope test data.

## Note on log masking

Payloads were recovered from Actions logs, where GitHub redacts any byte
sequence matching a registered secret as `***`. 56/100 captured payloads
contained such masking and 3 were unparseable because masking landed
inside a numeric job id. The test data here was chosen from unmasked,
valid-JSON runs and all pass `validate_payload()`.
