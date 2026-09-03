# Consuming `status.json` in a downstream project

This tutorial shows you how to react to TheRock releases by reading the
`status.json` files Quartz publishes: poll for a new build, check the part you
depend on, and pull down the artifacts.

Quartz commits these files right into the repository, so you can read them
straight off raw GitHub URLs. No GitHub App, token, or installation to set up.

New to `status.json`? Read the [overview](README.md) first. It covers the file
layout, the vocabulary (pipeline, phase, architecture), the available endpoints,
and the field reference this tutorial builds on.

- [How to poll it](#how-to-poll-it)
- [Getting the artifacts and Python packages](#getting-the-artifacts-and-python-packages)
- [Reading it in Python](#reading-it-in-python)
- [Downstream workflow example](#downstream-workflow-example)

## How to poll it

Once you know the shape, wiring up a consumer is short. The idea: on a schedule,
grab the latest status, check whether it is new and whether it passed, and if so
kick off your own work. Concretely:

1. **Fetch** `nightly/latest.json` on a schedule. A cron job or a
   scheduled GitHub Actions workflow both work well. Note that `latest.json` can
   point at a build still in progress, so step 3 matters.
1. **Skip what you have seen.** Read `rocm_version` / `build_date` and compare
   against the last build you acted on. If it is unchanged, there is nothing to
   do, so stop here.
1. **Check it passed.** Gate on the specific pipeline and phase you depend on,
   for example `summary.linux.rocm.build.status == "success"`. Do not gate on
   `summary.overall_status`: it folds in every pipeline and phase across both
   platforms, and because some test suites are still routinely red, the overall
   rollup tends to converge to `failure` even when the part you need is fine. If
   the build is new and the status you care about is `success`, trigger your
   downstream work.
1. **Grab the artifacts.** Build the download URLs from `summary.<platform>.urls`.

You do not have to write the fetching and parsing yourself. The
[Reading it in Python](#reading-it-in-python) section below ships a small helper
that does steps 1 to 3 for you.

A few tips to keep your consumer lean and avoid needless work:

- **Poll on a schedule, not in a tight loop.** Nightlies land once per day, so a
  cron every 30 to 60 minutes is plenty, and it stays friendly to GitHub's raw
  endpoint.
- **Deduplicate on `rocm_version` + `build_date`** so you do not re-trigger for a
  build you already processed.
- **Check the specific status you care about**, not just `overall_status`. If you
  only need Linux ROCm, gate on `summary.linux.rocm.build.status` rather than the
  global rollup, which also folds in PyTorch, JAX, and Windows.
- **Expect a missing or partial file.** A build still in progress may not yet
  contain the pipeline you are waiting for, so handle that case gracefully.

## Getting the artifacts and Python packages

Step 4 is where you actually pull the build down. `summary.<platform>.urls` holds
the base locations. Each entry is a base directory (or index page), not a direct
per-file link:

| Key           | What it points to                                                                         |
| ------------- | ----------------------------------------------------------------------------------------- |
| `tarballs`    | Directory of `.tar.*` archives of the full ROCm build.                                    |
| `wheels`      | Directory of Python wheels (the ROCm Python packages, and PyTorch/JAX wheels when built). |
| `rpm` / `deb` | Directories of native Linux packages (Linux only).                                        |
| `artifacts`   | A browsable index page listing everything published for the build.                        |

Tarball filenames follow the pattern
`therock-dist-{platform}-{target}[-tests]-{version}.tar.gz`, where `target` is
either `multiarch` or a specific GPU target such as `gfx90a` or `gfx94X-dcgpu`,
and the optional `-tests` variant bundles the test assets. You do not need to
assemble these names by hand. The [Python helper](#reading-it-in-python)
resolves both the wheels index and the exact tarball URL for you. To find a
filename manually instead, open the `artifacts` index in a browser or list the
base directory.

> Windows builds expose `tarballs` and `wheels` but not `rpm` / `deb`; native
> packages are Linux only. Read the URLs from the platform you actually target,
> and guard for a platform or URL key being absent.

## Reading it in Python

Rather than hand-navigate the nested JSON, you can let Quartz's small,
dependency-free read helper do it for you:

- **[`read_status_json.py`](../../scripts/consumer/read_status_json.py)** (in the
  Quartz repository under `scripts/consumer/`) loads a `status.json` from a URL
  or a local path and exposes typed accessors. `latest.json` is a git symlink and
  raw GitHub serves it as its target path rather than the file; `load_status`
  follows that pointer for you, so pointing it at `latest.json` just works.

> Want a quick look without writing code? The helper also runs as a script.
> `python3 read_status_json.py` prints a summary of the latest nightly, or pass a
> specific endpoint or a local file (`python3 read_status_json.py status.json`).

Import it in your own code. The helper also resolves the artifact URLs, so you
can go straight from a passing build to installing wheels or checking a tarball.
A complete, runnable example lives next to this guide:

- **[`example_consume_status.py`](example_consume_status.py)** loads the latest
  nightly, gates on the Linux ROCm build, dry-run resolves the wheels, and probes
  a tarball with an HTTP HEAD. Run it straight from a Quartz checkout:
  `python3 docs/status-json/example_consume_status.py`. Nothing is installed or
  downloaded. To reuse it, copy it and `read_status_json.py` into your own
  project's `scripts/consumer/` and adapt the device extra and tarball target
  near the top.

### API reference

`load_status(source)` returns a `StatusDocument`. From there:

```text
StatusDocument
├─ rocm_version, build_date, release_type    release metadata
├─ overall_status                            worst-of rollup across platforms
├─ is_complete                               True once the build has finished
├─ build_id                                  (rocm_version, build_date) dedup key
├─ raw, pipelines                            escape hatch to the raw dicts
└─ platform(name) -> PlatformStatus
   ├─ status                                 worst-of rollup for the platform
   ├─ architectures                          e.g. ["gfx942", "gfx1201"]
   ├─ urls / url(kind)                        artifact base URLs
   ├─ pipeline_build_status(pipeline)         "success" | "failure" | ...
   ├─ pipeline_test_counts(pipeline)          pass/fail counters
   ├─ native_package_status("rpm" | "deb")    Linux native package status
   └─ tarball_url(version, target,            full tarball download URL
                  platform=None, with_tests=False)
```

`platform` defaults to the platform you called it on; `with_tests=True` selects
the test-bundled tarball variant. Reach for `raw` / `pipelines` only when you
need per-architecture or per-variant detail beyond the summary.

For example, to get the Linux `gfx94X-dcgpu` tarball URL:

```python
linux = status.platform("linux")
url = linux.tarball_url(status.rocm_version, "gfx94X-dcgpu")
# https://.../therock-dist-linux-gfx94X-dcgpu-<version>.tar.gz
```

## Downstream workflow example

A ready-to-adapt scheduled GitHub Actions workflow lives next to this guide:

- **[`example_poll_status.yml`](example_poll_status.yml)** runs
  `example_consume_status.py` hourly during the UTC window when the ROCm release
  lands, to fetch `nightly/latest.json`, gate
  on the Linux ROCm build, and report its wheels and a tarball, leaving a clearly
  marked step for your own work.

Copy the workflow into your project's `.github/workflows/`, and copy both scripts
into `scripts/consumer/` (`read_status_json.py` and `example_consume_status.py`,
side by side), then replace the "React to the build" step with whatever your
project needs to do.

The gate is wired through step outputs, not baked into the "React" step. The
consume script writes `ready`, `rocm_version`, and `build_date` to
`$GITHUB_OUTPUT`, and the "React to the build" step guards on
`if: steps.status.outputs.ready == 'true'` so it fires only when the build you
depend on passed. That keeps the decision (is the build ready?) separate from the
action (what you do about it).

### Deduplicating across runs

The example acts on each build once, with no external database. It repurposes
`actions/cache` as a **marker**: the cache key encodes the build
(`rocm-tested-<rocm_version>-<build_date>`), and the mere presence of that key is
the signal - a cache hit means a previous run already reacted to this build. The
cached file itself is empty; only the key matters.

The `react` step is gated on a cache miss, and the key is saved only after
`react` succeeds, so a failed run leaves no marker and is retried on the next
poll. Overlapping polls are serialized by the `concurrency` group, which is what
makes that single save-after-success marker enough even when the work outlives
the poll interval.

The consume script offers the same check in Python (`should_process()`); use
whichever fits your workflow.

### Serializing overlapping polls

The example sets a top-level `concurrency` group so that only one poll runs at a
time:

```yaml
concurrency:
  group: poll-rocm-nightly
  cancel-in-progress: false
```

`cancel-in-progress: false` lets an in-flight run finish rather than cancelling
it, and a scheduled poll that arrives while one is running queues behind it. This
is what makes the save-after-success marker enough: the `react` step always
writes its marker before the next poll starts, so no build is processed twice,
even when the work outlives the poll interval. The group is a static string
because `build_date` is not yet known when `concurrency` is evaluated.

This is the conservative default. GitHub also supports cancelling an in-progress
run (`cancel-in-progress: true`), keying the group on an expression, and other
scenarios - for instance, letting a manual run pre-empt a scheduled one. See the
[`concurrency` syntax reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency).

### Dispatching a separate workflow via `workflow_dispatch`

The example `example_poll_status.yml` keeps the work in the poll run itself, so the `react` step's
outcome reflects whether the downstream run actually passed, and the marker is
written only on success.
Some projects instead have the poll dispatch a separate workflow via `workflow_dispatch`. This changes the picture and needs the marker to move.

A workflow's `concurrency` group only serializes runs of that same workflow. When
the polling workflow (e.g. `example_poll_status.yml`) dispatches `my_build.yml` via `workflow_dispatch`, the dispatch returns
immediately and `my_build.yml` becomes an independent run; the poll job does not
wait for it. As such, the poll run ends and its concurrency lane frees for the
next poll while `my_build.yml` is still running. Two things follow: the poll's
`react` step succeeding means "dispatch accepted", not "build passed", and the
poll's serialization does not extend to `my_build.yml`.

The fix is to move the marker into `my_build.yml`, the only run that knows the
true outcome, and to let that workflow deduplicate itself per build:

- Pass `rocm_version` and `build_date` to `my_build.yml` as `workflow_dispatch`
  inputs, so the run knows which build it is handling.
- Key `my_build.yml`'s own `concurrency` group on that build, so duplicate
  dispatches for the same build serialize instead of running in parallel.
- At the start of `my_build.yml`, check the marker and do the real work only on a
  miss.
- At the end, write the marker only on success. A failed run leaves no marker, so
  the next poll re-dispatches - the same retry-on-failure the inline example has.

Whether to write the marker on success or on completion depends on how green your
build is. Marking only on success (as above) leaves a failed run unmarked, so the
next poll retries it - what you want when failures are transient. If your build is
instead often red and expected to stay that way for a while, only-on-success
re-dispatches on every poll and never settles; marking on completion, regardless
of outcome, processes each build once and stops the churn.

> [!IMPORTANT]
> Repeat this for every dispatched workflow (or just the longest running one): each
> needs its own per-build `concurrency` group and marker. Concurrency groups
> share one repository-wide namespace. Dispatching more than one? Prefix both the concurrency group and the marker key with the workflow's own name (`my-build-`, `my-test-`).

The two lines that carry the section are the per-build `concurrency` group and
the save-on-success marker:

```yaml
# my_build.yml - the dispatched workflow. The build arrives as inputs, so unlike
# the poll it can key concurrency per build and own the marker.
on:
  workflow_dispatch:
  # ...

concurrency:
  group: my-build-${{ inputs.rocm_version }}-${{ inputs.build_date }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      # ... restore the marker (lookup-only), run the real work only on a miss ...
      # Marker written only on success, so a failed run retries on the next poll.
      - run: touch .my-build-done
        if: steps.work.outcome == 'success'
      - uses: actions/cache/save@5a3ec84eff668545956fd18022155c47e93e2684 # v4.2.3
        if: steps.work.outcome == 'success'
        with:
          path: .my-build-done
          key: my-build-done-${{ inputs.rocm_version }}-${{ inputs.build_date }}
```

A separate "claim" marker, written by the poll before it dispatches, is not
needed. The per-build `concurrency` group in `my_build.yml` already blocks a
duplicate build, and a claim marker would trade away retry-on-failure: an
immutable cache entry survives a failed run for the full cache lifetime, so a
build claimed and then failed would never be retried.
