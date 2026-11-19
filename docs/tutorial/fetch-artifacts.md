# Fetch ROCm build artifacts

You can fetch the dependencies of a single TheRock stage or assemble a local
ROCm installation to test your software. This tutorial uses the nightly stream
as an example.

The scheduled nightly runs in `ROCm/rockrel`. Quartz records the GitHub Actions
run ID of the triggering workflow in `nightly/latest.json`. Read that ID once, then
pass it to TheRock's build tools so each download comes from the same run,
even after the next nightly appears.

## Before you start

The examples use Bash on Linux, Python 3.11 or newer, and a TheRock checkout
with its Python build-tool dependencies installed. Set `reader` to the Quartz
consumer script (a standalone copy also works) and `tools` to TheRock's build
tools directory.

```bash
reader=/path/to/quartz/scripts/consumer/read_status_json.py
tools=/path/to/TheRock/build_tools
```

## Resolve the Linux build run

Read the published nightly pin directly from raw GitHub. The reader follows the
relative symlink. This URL is also the default, so you can omit it.

```bash
run_id=$(python "$reader" --get-run-id linux https://raw.githubusercontent.com/ROCm/quartz/main/nightly/latest.json)
```

For a reproducible lookup, replace `main` in the URL with a Quartz commit that
contains the published data.

If you already have a Quartz checkout containing published data (branch `main`),
you can read its local pin instead.

```bash
run_id=$(python "$reader" --get-run-id linux /path/to/quartz/nightly/latest.json)
```

The lookup requires a successful Linux ROCm build. Reuse the resolved ID for
every fetch from this nightly.

## Choose what to fetch

Each fetch command sets `RELEASE_TYPE=nightly` to select the nightly artifact
bucket.

**Fetch the dependencies of a build stage.** The `--stage math-libs` option
selects the artifacts that `math-libs` needs as build inputs. The `--flatten`
option extracts them into one directory. Use one invocation per stage.

TheRock reads the stage dependencies from your local checkout. To reproduce a
historical nightly, use the revision recorded in the status document’s
`therock_commit` field.

```bash
RELEASE_TYPE=nightly python "$tools/artifact_manager.py" fetch --run-id "$run_id" \
  --stage math-libs --platform linux --amdgpu-families gfx94X-dcgpu \
  --expand-family-to-targets --flatten --output-dir "math-libs-inputs-$run_id"
```

**Assemble a base ROCm installation from build artifacts.** This command downloads
the base components for the selected GPU family. The installer replaces the
destination directory if it exists. Add `--dry-run` to preview the files.

```bash
RELEASE_TYPE=nightly python "$tools/install_rocm_from_artifacts.py" --run-id "$run_id" --run-github-repo ROCm/rockrel \
  --amdgpu-family gfx94X-dcgpu --base-only --output-dir "rocm-$run_id"
```
