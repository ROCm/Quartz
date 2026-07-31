#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Example: consume a Quartz status.json from a downstream project.

Runnable companion to tutorial.md. It loads the latest nightly status and asks
the one question a consumer cares about: is the build I depend on ready to act
on - meaning it passed the gate and I have not already processed it? Only if so
does it go on to resolve the wheels index and check a distribution tarball
exists - all without downloading anything heavy.

Deduplication (the "skip what you have seen" step) is stubbed in
should_process(): it returns True by default so the example runs end to end, with
a comment showing where each project plugs in its own state store keyed on
build_id (rocm_version + build_date).

The gate is the important part. This example depends on the Linux ROCm build,
so it checks summary.linux.rocm.build. It deliberately does not gate on
overall_status: that folds in every pipeline and phase across both platforms,
and because some test suites are routinely red the overall rollup tends to
converge to failure even when the part you need is fine. Change PLATFORM /
PIPELINE below to gate on whatever your project actually consumes.

Nothing here mutates your system: the pip step runs with --dry-run, and the
tarball is probed with an HTTP HEAD request rather than downloaded.

Run it from a checkout of the Quartz repository:

    python3 docs/status-json/example_consume_status.py

Exit code: 0 when the build is ready and downstream work ran (or would run), 0
when there is simply nothing new to do yet, and non-zero only on an actual error
(the fetch or the resolve failed).

When run inside GitHub Actions it also writes step outputs (ready, rocm_version,
build_date) to $GITHUB_OUTPUT, so a later step can gate on the build without
re-parsing anything. See example_poll_status.yml for the wiring.

To reuse it in your own project, copy read_status_json.py into scripts/consumer/
and this file next to it (scripts/consumer/example_consume_status.py). The
sys.path shim below then resolves the helper in either layout.
"""

import os
import subprocess
import sys
import urllib.request
from pathlib import Path

# Put scripts/consumer/ (where read_status_json.py lives) on the import path,
# resolved relative to the repository root. Works both from a Quartz checkout
# and from a downstream project that mirrors the scripts/consumer/ layout.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "consumer"))

from read_status_json import PlatformStatus, StatusDocument, load_status

# The build this consumer depends on. Gate on the specific platform + pipeline
# you actually use, not overall_status.
PLATFORM = "linux"
PIPELINE = "rocm"


def set_github_outputs(**outputs: str) -> None:
    """Write step outputs to $GITHUB_OUTPUT, if running inside GitHub Actions.

    A no-op outside Actions (the variable is unset), so the script stays runnable
    locally. A later workflow step reads these with steps.<id>.outputs.<name>.
    """
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a") as handle:
        for name, value in outputs.items():
            handle.write(f"{name}={value}\n")


def ready_platform(status: StatusDocument) -> PlatformStatus | None:
    """Return the platform to act on if the build we depend on succeeded.

    Returns the PlatformStatus when PLATFORM's PIPELINE build is "success", or
    None if it is missing, still in progress, or failed - in which case there is
    nothing for this consumer to do.
    """
    platform = status.platform(PLATFORM)
    if platform is None:
        print(f"{PLATFORM}: not present in this build yet.")
        return None
    build_status = platform.pipeline_build_status(PIPELINE)
    if build_status != "success":
        print(f"{PLATFORM} {PIPELINE} build: {build_status or 'not reported'}.")
        return None
    return platform


def should_process(status: StatusDocument) -> bool:
    """Return True if this build is new and should be processed (dedup).

    Placeholder: each downstream project owns where it records the last build it
    processed - a file in the repo, a cache key, a workflow artifact, a row in a
    database, a git tag, etc. - so the persistence lives in your project, not
    here. Compare status.build_id, the (rocm_version, build_date) pair, against
    that stored identity and return False when they match (already seen).

    For this example it always returns True so it runs the full pipeline end to
    end. Without this check the workflow re-triggers on every poll once
    latest.json turns successful, for the same nightly.

    For example, you can wire it like this so you trigger only once per build:

        last = Path("state/last_build_id.txt")          # your state store
        if last.exists() and last.read_text() == "\\n".join(status.build_id):
            return False                                 # already processed
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text("\\n".join(status.build_id))     # record before acting
        return True
    """
    return True


def process(status: StatusDocument, platform: PlatformStatus) -> None:
    """Do the downstream work for a build that passed the gate.

    Stands in for whatever your project does with a good build. Here it resolves
    the wheels index and confirms a tarball exists, without downloading or
    installing either.
    """
    architectures = platform.architectures
    print(f"architectures: {', '.join(architectures) or 'none'}")

    # Dry-run install of the ROCm Python packages: point pip at the wheels index
    # and pick the device extra for the architecture you target (one device
    # extra per gfx target, e.g. device-gfx942). --dry-run only resolves and
    # reports; drop it to actually install, and do that inside a virtual
    # environment so it never touches your system Python.
    wheels_url = platform.url("wheels")
    result = subprocess.run(
        [
            "pip",
            "install",
            "--dry-run",
            "rocm[devel,device-gfx942]",
            f"--index-url={wheels_url}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    print(result.stdout)

    # Check one tarball exists without downloading it (they are many GB). The
    # target is "multiarch" or a gfx target like "gfx94X-dcgpu" (note the difference to
    # device-gfx942); pass with_tests=True for the variant that bundles the test assets.
    tarball_url = platform.tarball_url(status.rocm_version, "gfx94X-dcgpu")
    request = urllib.request.Request(tarball_url, method="HEAD")
    with urllib.request.urlopen(request) as response:
        print(f"{tarball_url} -> HTTP {response.status}")


def main() -> None:
    status = load_status()  # latest nightly (or pass a URL / path)
    print(f"{status.rocm_version} (overall: {status.overall_status})")

    platform = ready_platform(status)
    # ready means "this build should be processed": it passed the gate AND we
    # have not acted on this exact (rocm_version, build_date) before. The later
    # workflow step gates on this output, so dedup here keeps it from re-firing
    # on every poll for the same nightly.
    ready = platform is not None and should_process(status)
    set_github_outputs(
        ready=str(ready).lower(),
        rocm_version=status.rocm_version,
        build_date=status.build_date,
    )
    if platform is None:
        print("Build not ready; nothing to do.")
        return
    if not ready:
        print(f"{status.rocm_version} ({status.build_date}) already processed; skipping.")
        return

    # >>> The gate passed and the build is new: your project's real work goes here. <<<
    # The process() call currently is a dry-run of pip install and check
    # if the tarball exists. You can also just take the github output
    # and do the follow-up steps in a later workflow step, without
    # re-parsing the status.json.
    print(f"{PLATFORM} {PIPELINE} build is ready; processing.")
    process(status, platform)


if __name__ == "__main__":
    main()
