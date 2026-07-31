# `status.json` overview

For each TheRock release, Quartz publishes a `status.json` describing the build
and test outcome. This page explains what the file contains: its layout, the
endpoints it is served at, the fields, and the status values.

Quartz commits these files right into the repository, so they are readable
straight off raw GitHub URLs. No GitHub App, token, or installation to set up.

Ready to consume it from a downstream project? See the
**[tutorial](tutorial.md)**. It walks through polling for new builds, gating on
the status you depend on, and pulling down the artifacts, with copy-paste Python
and a GitHub Actions workflow.

- [Layout and terms](#layout-and-terms)
- [Endpoints](#endpoints)
- [What is in a status.json](#what-is-in-a-statusjson)
- [Status values](#status-values)
- [Full schema reference](#full-schema-reference)

## Layout and terms

A `status.json` has the following shape:

```text
status.json
├─ release metadata        rocm_version, build_date, release_type, timestamps
├─ summary                 the at-a-glance rollup
│  └─ <platform>           linux | windows
│     ├─ status            worst-of rollup for the platform
│     ├─ architectures     e.g. gfx942, gfx1201
│     ├─ urls              tarballs, wheels, rpm/deb, artifacts index
│     └─ <pipeline>        rocm | pytorch | jax | native_packages
│        ├─ build          one status
│        └─ test           pass/fail counters
└─ pipelines               deep per-arch / per-variant detail
```

For example, to know if ROCm built successfully, check
`summary.<platform>.rocm.build.status`.

The tree uses a handful of terms that recur throughout this guide and map
directly to keys in the document:

| Term                  | Meaning                                                                                                                                                |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **TheRock**           | The build system that produces ROCm releases. Its CI is what Quartz reports on.                                                                        |
| **nightly**           | An automatic build produced once a day.                                                                                                                |
| **prerelease** (`rc`) | A release candidate build for an upcoming ROCm release.                                                                                                |
| **architecture**      | A GPU target, for example `gfx942` or `gfx1201` (the same identifiers ROCm uses).                                                                      |
| **pipeline**          | One product built from a release: `rocm` (the ROCm stack itself), `pytorch`, `jax`, and `native_packages`. A release can produce several.              |
| **phase**             | A stage of a pipeline: `build` and `test`. For `native_packages`, `rpm` or `deb` instead.                                                              |
| **variant**           | For PyTorch/JAX, one cell of the version matrix (for example Python 3.12 with a given Torch branch). Relevant only to consumers of PyTorch/JAX detail. |

Not every pipeline runs on every platform, and `native_packages` is a special
case with no `build` / `test` phases:

| Pipeline          | Phases                          | Platforms      |
| ----------------- | ------------------------------- | -------------- |
| `rocm`            | `build`, `test`                 | linux, windows |
| `pytorch`         | `build`, `test`                 | linux, windows |
| `jax`             | `build`, `test`                 | linux only     |
| `native_packages` | per package type (`rpm`, `deb`) | linux only     |

`build` is a single status; `test` carries pass/fail counters. Each block is
described in detail in [What is in a status.json](#what-is-in-a-statusjson)
below.

## Endpoints

Quartz publishes one `status.json` per release build (nightly/prerelease), plus stable pointers to the
most recent builds.

| Endpoint                             | Points to                                                              |
| ------------------------------------ | ---------------------------------------------------------------------- |
| `release-nightly/<date>/status.json` | A specific nightly, for example `release-nightly/20260707/status.json` |
| `release-nightly/latest.json`        | The most recent nightly (any result, including still in progress)      |
| `release-nightly/latest_good.json`   | The most recent fully-passing nightly                                  |
| `prerelease/<version>/status.json`   | A specific prerelease, for example `prerelease/7.14.0rc1/status.json`  |
| `prerelease/latest.json`             | The most recent prerelease                                             |

Each is served as raw content. The raw URL form is:

```text
https://raw.githubusercontent.com/ROCm/quartz/main/release-nightly/latest.json
```

> **Note on `latest_good.json`:** Is currently unavailable, as the definition of "fully passing"
> still needs to be determined.

> These endpoints go live as TheRock release workflows are instrumented to report
> to Quartz. Until a given release type is instrumented, its files may be absent.
> Consumers should handle a missing or not-yet-updated file.

## What is in a `status.json`

Each file has three parts:

1. **Top-level release metadata**: schema version, release type, ROCm version,
   build date, run id of the triggering workflow, and timestamps.
1. **`summary`**: a Quartz-computed at-a-glance rollup: overall status,
   per-platform (`linux` / `windows`) status, requested architectures, artifact
   download URLs, and per-pipeline pass/fail counts.
1. **`pipelines`**: the detailed per-pipeline, per-phase, per-architecture
   breakdown, including individual workflow `run_id`s and timestamps.

Most consumers need only the top-level metadata and `summary`. The `pipelines`
block is required only for per-architecture or per-variant detail.

For a complete, annotated example, see
[`status_json_reference.jsonc`](status_json_reference.jsonc).

### Most-used fields

| Field                              | Meaning                                                                                                          |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `release_type`                     | `nightly`, `rc` (prerelease/release candidate)                                                                   |
| `rocm_version`                     | The ROCm version string for this build. Normalized to use the representation for wheels (rpm/deb are different). |
| `build_date`                       | `YYYYMMDD` of the build.                                                                                         |
| `completed_at`                     | `null` while the build is still running; a timestamp once done.                                                  |
| `summary.overall_status`           | Roll-up status over all platforms and pipelines.                                                                 |
| `summary.<platform>.status`        | Per-platform roll-up (`linux` / `windows`).                                                                      |
| `summary.<platform>.architectures` | Requested architectures for the platform.                                                                        |
| `summary.<platform>.urls`          | Base URLs for tarballs, wheels, packages, and the artifact index.                                                |
| `summary.<platform>.<pipeline>`    | Per-pipeline (`rocm`, `pytorch`, `jax`, `native_packages`) build status and test counters.                       |

While the release is live, an expected-but-unreported pipeline is shown as
`in_progress` (a placeholder that also feeds the platform worst-of, so a platform
never reads `success` while a pipeline it will still run is pending). Once the
platform is finalized, a pipeline that never reported is removed — it did not run
this release — so it is simply absent, with no `null` or `"pending"`
placeholders. Consumers must still guard for missing keys.

## Status values

| Value         | Meaning                                                   |
| ------------- | --------------------------------------------------------- |
| `in_progress` | Running, not yet finished.                                |
| `success`     | Completed successfully.                                   |
| `failure`     | Completed with a failure.                                 |
| `cancelled`   | Cancelled before completion.                              |
| `skipped`     | Not run (for example, a platform not built this release). |

`overall_status` and per-platform `status` are **worst-of** rollups: if any
constituent is `failure`, the rollup is `failure`; if any is still
`in_progress`, the rollup is `in_progress`.

Test phases in `summary` carry **counters** (one count per matrix entry), not a
single status. Pass/fail is derived by summing or inspecting them.

## Full schema reference

The complete annotated layout, including the deep `pipelines` tree, PyTorch/JAX
`variants`, and `native_packages`, is maintained as the canonical schema v2
reference:

- [`status_json_reference.jsonc`](status_json_reference.jsonc) (schema v2)

Consult it when you need per-architecture or per-variant detail beyond the
`summary` block.
