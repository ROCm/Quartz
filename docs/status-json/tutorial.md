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

1. **Fetch** `release-nightly/latest.json` on a schedule. A cron job or a
   scheduled GitHub Actions workflow both work well. Note that `latest.json` can
   point at a build still in progress, so step 3 matters.
2. **Skip what you have seen.** Read `rocm_version` / `build_date` and compare
   against the last build you acted on. If it is unchanged, there is nothing to
   do, so stop here.
3. **Check it passed.** Gate on the specific pipeline and phase you depend on,
   for example `summary.linux.rocm.build.status == "success"`. Do not gate on
   `summary.overall_status`: it folds in every pipeline and phase across both
   platforms, and because some test suites are still routinely red, the overall
   rollup tends to converge to `failure` even when the part you need is fine. If
   the build is new and the status you care about is `success`, trigger your
   downstream work.
4. **Grab the artifacts.** Build the download URLs from `summary.<platform>.urls`.

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

| Key | What it points to |
|---|---|
| `tarballs` | Directory of `.tar.*` archives of the full ROCm build. |
| `wheels` | Directory of Python wheels (the ROCm Python packages, and PyTorch/JAX wheels when built). |
| `rpm` / `deb` | Directories of native Linux packages (Linux only). |
| `artifacts` | A browsable index page listing everything published for the build. |

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
  or a local path and exposes typed accessors.

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

`load_status(source)` returns a `Status`. From there:

```text
Status
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

- **[`example-poll-status.yml`](example-poll-status.yml)** runs
  `example_consume_status.py` hourly to fetch `release-nightly/latest.json`, gate
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
