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
├─ release metadata        rocm_version, build_date, release_type, build_variant,
│                          therock_commit, pytorch_enabled, jax_enabled, timestamps
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

| Term                  | Meaning                                                                                                                                                          |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **TheRock**           | The build system that produces ROCm releases. Its CI is what Quartz reports on.                                                                                  |
| **nightly**           | An automatic build produced once a day.                                                                                                                          |
| **nightly-bkc**       | A nightly build, cut from a `release/bkc/...` branch                                                                                                             |
| **prerelease** (`rc`) | A release candidate build for an upcoming ROCm release.                                                                                                          |
| **architecture**      | A GPU target, for example `gfx942` or `gfx1201` (the same identifiers ROCm uses).                                                                                |
| **pipeline**          | One product built from a release: `rocm` (the ROCm stack itself), `pytorch`, `jax`, and `native_packages`. A release can produce several.                        |
| **phase**             | A stage of a pipeline: `build` and `test`. For `native_packages`, `rpm` or `deb` instead.                                                                        |
| **build variant**     | The release flavor, such as `release`, `asan`, or `asan-debug`. It selects the document: `status.json` for `release`, `status-asan.json` for either ASAN flavor. |
| **variant**           | For PyTorch/JAX, one cell of the version matrix (for example Python 3.12 with a given Torch branch). Relevant only to consumers of PyTorch/JAX detail.           |

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

Quartz publishes one document per build-variant family, all sharing the same
schema. The normal release build gets the unsuffixed `status.json`; ASAN builds
get `status-asan.json`. The suffix follows TheRock's own `build_variant_suffix`,
which folds the debug flavor onto its base family, so both `asan` and
`asan-debug` publish to `status-asan.json` and the document's `build_variant`
field says which flavor produced it. Each family carries its own stable pointers,
so consumers can follow one without a build of the other ever moving it.

Below, `<v>` stands for that suffix: empty for the release build, `-asan` for the
ASAN family. So `status<v>.json` is either `status.json` or `status-asan.json`.

| Endpoint                                                  | Points to                                                                       |
| --------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `nightly/<date>/status<v>.json`                           | A specific nightly build of that variant                                        |
| `nightly/latest<v>.json`                                  | The most recent build of that variant (including still in progress)             |
| `nightly/latest_good<v>.json`                             | The most recent fully-passing build of that variant                             |
| `nightly-bkc/<nightly-version>/<bkc-date>/status<v>.json` | A specific BKC nightly build of that variant                                    |
| `nightly-bkc/<nightly-version>/latest<v>.json`            | The most recent BKC build of that variant for the nightly version               |
| `nightly-bkc/<nightly-version>/latest_good<v>.json`       | The most recent fully-passing BKC build of that variant for the nightly version |
| `nightly-bkc/latest<v>.json`                              | The most recent build of that variant for the highest BKC nightly version       |
| `nightly-bkc/latest_good<v>.json`                         | The highest-versioned fully-passing BKC build of that variant                   |
| `prerelease/<major.minor>/<full>/status<v>.json`          | A specific prerelease build of that variant                                     |
| `prerelease/latest<v>.json`                               | The highest-versioned prerelease of that variant across all release lines       |
| `prerelease/<major.minor>/latest<v>.json`                 | The highest-versioned prerelease of that variant in one release line            |

> **The sanitizer endpoints are not published yet.** TheRock's
> `multi_arch_release_asan.yml` does not report to Quartz, so an `asan` or
> `asan-debug` document written today could never be finalized. Quartz routes and
> tests these files but withholds them until that orchestrator is instrumented;
> expect them to be absent until then.

Each is served as raw content. The raw URL form is:

```text
https://raw.githubusercontent.com/ROCm/quartz/main/nightly/latest.json
```

> **Note on the pointer files:** Every `latest*.json` and `latest_good*.json`
> pointer is a git symlink to the concrete `status*.json` it currently points
> at. Raw GitHub serves a symlink as its target path (a one-line body like
> `20260707/status-asan.json`), not the file it points to, so a plain fetch
> returns that path rather than JSON. The Python helper `load_status` follows
> this pointer for you transparently; if you fetch it yourself, resolve the
> returned path against the pointer URL and fetch again.

> **Note on `latest_good.json`:** "Fully passing" means the build's
> `summary.overall_status` is `success` (a worst-of rollup, so this implies the
> build finished and every reported pipeline was green). A `latest_good.json`
> pointer only advances to a build once that build reaches `success`, so it never
> regresses to an in-progress or failed build. Because it points at the live
> document rather than a copy of it, a build can stop being green after the
> pointer was written (a re-run re-opens it, or a late leaf fails it); the pointer
> then falls back to the newest build of that variant still passing, and is
> dropped entirely when none is left, so treat a missing pointer as "nothing good
> to offer right now". Prerelease has no `latest_good.json` pointer yet, for
> either build flavor.

> These endpoints go live as TheRock release workflows are instrumented to report
> to Quartz. Until a given release type is instrumented, its files may be absent.
> Consumers should handle a missing or not-yet-updated file.

## What is in a `status.json`

Each file has three parts:

1. **Top-level release metadata**: schema version, release type, ROCm version,
   build date, build variant and TheRock commit, which pipelines the release
   built (`pytorch_enabled` / `jax_enabled`), run id of the triggering workflow,
   and timestamps.
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

| Field                              | Meaning                                                                                                                              |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `release_type`                     | `nightly`, `nightly-bkc`, `rc` (prerelease/release candidate)                                                                        |
| `rocm_version`                     | The ROCm version string for this build. Normalized to use the representation for wheels (rpm/deb are different).                     |
| `build_date`                       | `YYYYMMDD` of the build.                                                                                                             |
| `build_variant`                    | Build flavor: `release`, or a sanitizer build such as `asan` (schema 2.1). `""` when no signal yet; key absent in pre-2.1 documents. |
| `therock_commit`                   | The 40-hex TheRock commit the release was built from (schema 2.1). `""` until resolved; key absent in pre-2.1 documents.             |
| `pytorch_enabled` / `jax_enabled`  | Whether this release's dispatch built the PyTorch / JAX pipeline. Disable-only: absent means enabled (`true`).                       |
| `completed_at`                     | `null` while the build is still running; a timestamp once done.                                                                      |
| `summary.overall_status`           | Roll-up status over all platforms and pipelines.                                                                                     |
| `summary.<platform>.status`        | Per-platform roll-up (`linux` / `windows`).                                                                                          |
| `summary.<platform>.architectures` | Requested architectures for the platform.                                                                                            |
| `summary.<platform>.urls`          | Base URLs for tarballs, wheels, packages, and the artifact index.                                                                    |
| `summary.<platform>.<pipeline>`    | Per-pipeline (`rocm`, `pytorch`, `jax`, `native_packages`) build status and test counters.                                           |

In `summary`, while the release is live, an expected-but-unreported pipeline is
shown as `in_progress`.
Once the platform is finalized, a pipeline that never reported is removed — it
did not run this release — so it is simply absent. The `pipelines` detail block
carries only reported entries: a phase appears there after its first event and is
absent otherwise, with no `null` or `"pending"` placeholders. Consumers must
guard for missing keys in either view.

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
