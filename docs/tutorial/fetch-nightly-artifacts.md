# Fetch ROCm nightly build artifacts

You can fetch the dependencies of a single TheRock stage from a ROCm nightly or
assemble a local ROCm installation to test your software. Both workflows start
by identifying the build that produced the artifacts.

The scheduled nightly runs in `ROCm/rockrel`. Quartz records the GitHub Actions
run ID for its Linux build in `nightly/latest.json`. Read that ID once, then
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

With a Quartz checkout containing published data (branch `main`), read the local
pin.

```bash
run_id=$(python "$reader" --run-id linux /path/to/quartz/nightly/latest.json)
```

Alternatively, read the published pin directly. The reader follows the relative
symlink returned by raw GitHub. Omitting the URL also selects the latest nightly.

```bash
run_id=$(python "$reader" --run-id linux https://raw.githubusercontent.com/ROCm/quartz/main/nightly/latest.json)
```

The lookup requires a successful Linux ROCm build. Reuse the resolved ID for
every fetch from this nightly. For a reproducible lookup, replace `main` in the
raw URL with a Quartz commit that contains the published data.

## Choose what to fetch

Each fetch command sets `RELEASE_TYPE=nightly` to select the nightly artifact
bucket.

**Fetch the dependencies of a build stage.** This example merges the inputs to
the `math-libs` stage into one prefix. Use one invocation per stage. Stage dependencies come
from your local TheRock checkout. To reproduce a historical nightly, use the
TheRock revision recorded in the status document's `therock_commit` field.

```bash
RELEASE_TYPE=nightly python "$tools/artifact_manager.py" fetch --run-id "$run_id" --run-github-repo ROCm/rockrel \
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
