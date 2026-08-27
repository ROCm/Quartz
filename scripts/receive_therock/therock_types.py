# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Shared types and constants for TheRock data processing.

Provides dataclasses and helper functions used across the receive_therock
scripts. GitHub Actions vocabulary (event names, run/job status,
conclusion) is preserved verbatim on the wire and in storage; the DB
schema is expected to carry the same values.

`TheRockDispatchEvent.from_dict` parses the raw dispatch JSON, but that is
only the first step: the event is populated incrementally as it moves
through the pipeline (enrichment adds API-fetched data, classification
adds derived fields). A freshly parsed event is not yet complete.

For routing and dashboards, prefer the stable derived values under
`event.workflow_run.classification` over the raw wire fields, which come
verbatim from GitHub and can be unstable.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


# Releases are promoted from an existing prerelease build.
#   "7.13.0a20260415" -> nightly    "a20260415"
#   "7.13.0rc1"       -> prerelease "rc1"
RELEASE_VERSION_NIGHTLY_RE = re.compile(r"^\d+\.\d+\.\d+a(\d{8})$")
RELEASE_VERSION_PRERELEASE_RE = re.compile(r"^\d+\.\d+\.\d+(rc\d+)$")
# `dev` is not a release routing format: any version carrying a `.dev` local
# segment or a bare `<X>.<Y>.<Z>dev<N>` is rejected before it can reach a
# release status.json (see therock_update_status_json._release_version_suffix).
# The first alternative is intentionally unanchored so it also catches `.dev`
# inside longer local version segments (e.g. `+rocm...dev`); the second is
# anchored because it must match the whole bare `X.Y.Zdev<N>` form.
RELEASE_VERSION_DEV_RE = re.compile(r"\.dev\d*|^\d+\.\d+\.\d+dev\d*")


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def parse_quartz_tracking_id(inputs: dict[str, Any]) -> tuple[int | None, str | None]:
    """Split the propagated `quartz_tracking_id` into (owner_run_id, release_type).

    The top-level `multi_arch_release.yml` orchestrator stamps every workflow it
    triggers with `quartz_tracking_id: "<github.run_id>;<release_type>"` (empty
    when tracking is disabled). `github.run_id` is the orchestrator's own run, the
    top-level owner of the whole release lineage, and `release_type` is the
    channel it published to. Both are authoritative for every descendant run, so
    they are read straight from here rather than reconstructed from artifact ids,
    URLs, or the immediate GitHub parent.

    Returns `(None, None)` when the input is absent or empty (CI runs, manual
    TheRock dispatches, and the orchestrator's own record, which generates the id
    but does not carry it on its own inputs). A present but non-numeric run-id is
    a producer-side format bug and raises rather than coercing to None.
    """
    raw = inputs.get("quartz_tracking_id")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    run_id_part, _, release_type_part = raw.partition(";")
    try:
        run_id = int(run_id_part.strip())
    except ValueError as exc:
        raise ValueError(
            f"parse_quartz_tracking_id: non-numeric run-id {run_id_part!r} "
            f"in quartz_tracking_id={raw!r}"
        ) from exc
    return run_id, release_type_part.strip() or None


# Allow-list of `event_type` values the ingest pipeline accepts on a
# dispatch envelope, so producers and consumers agree on the wire
# vocabulary in one place.
# workflow_run lifecycle events, mirroring GitHub's `workflow_run` webhook
# actions (`requested` is the start marker, not `in_progress`).
WORKFLOW_RUN_EVENT_TYPES = frozenset(
    {
        "workflow_run_requested",
        "workflow_run_in_progress",
        "workflow_run_completed",
    }
)


KNOWN_EVENT_TYPES = WORKFLOW_RUN_EVENT_TYPES | frozenset(
    {
        "pull_request_event",
        "push_event",
    }
)


# The release channels this pipeline recognizes, mirroring the orchestrator's
# own `release_type` enum in multi_arch_release.yml. Anything outside this set
# (a producer typo or a channel we do not model yet) is coerced to None. This is
# broader than `_TRACKED_RELEASE_TYPES` in therock_update_status_json.py, which
# is the narrower subset that actually gets a status.json document.
KNOWN_RELEASE_TYPES = frozenset(
    {
        "dev",
        "dev-bkc",
        "nightly",
        "nightly-bkc",
        "prerelease",
    }
)


@dataclass(frozen=True)
class WorkflowSpec:
    """Static classification for one TheRock workflow file.

    Any TheRock workflow whose runs should be captured into the DB /
    status.json must have a `WorkflowSpec` registered in `WORKFLOW_SPECS`
    (keyed by workflow file basename). Unregistered workflows are not
    classified and contribute nothing.
    """

    # "linux"/"windows" ("" for the platform-agnostic setup and the top-level
    # release orchestrators, which fan out to both platforms).
    platform: str

    # "rocm" | "pytorch" | "jax" | "native_packages" | "setup" | "orchestrator"
    pipeline_type: str

    # for native packages: "rpm" or "deb"
    # for setup: "setup"
    # for orchestrators: "release" | "release-asan" | "release-linux" |
    #   "release-windows" | "repackage" | "python-packages"
    # for all other workflows: "build" or "test"
    pipeline_phase: str

    # Disambiguates one workflow file fanning out into several specs:
    # each entry must equal `inputs[key]` to match (empty = matches any).
    match_when: dict[str, str] = field(default_factory=dict)

    # When set, `platform` is ignored and resolved at classify time from the
    # `test_runs_on` input: "windows" if it names a windows runner, else
    # "linux". Used by `test_artifacts.yml` -- the one test workflow dispatched
    # for both platforms with no static platform input (the linux and windows
    # release orchestrators both call it, differing only by runner label).
    platform_from_test_runs_on: bool = False


# Specifications and categorizations of TheRock workflows. Every key is
# a `multi_arch_*` workflow whose runs land in the DB / status.json.
WORKFLOW_SPECS: dict[str, list[WorkflowSpec]] = {
    # --- Linux: portable rocm build ---
    "multi_arch_build_portable_linux.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="rocm",
            pipeline_phase="build",
        ),
    ],
    "multi_arch_build_portable_linux_artifacts.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="rocm",
            pipeline_phase="build",
        ),
    ],
    # --- Linux: framework wheel releases (build phase) ---
    "multi_arch_release_linux_pytorch_wheels.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="pytorch",
            pipeline_phase="build",
        ),
    ],
    "multi_arch_build_portable_linux_pytorch_wheels.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="pytorch",
            pipeline_phase="build",
        ),
    ],
    # JAX wheel releases (build phase). Active in the rockrel release pipeline.
    "multi_arch_release_linux_jax_wheels.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="jax",
            pipeline_phase="build",
        ),
    ],
    "multi_arch_build_linux_jax_wheels.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="jax",
            pipeline_phase="build",
        ),
    ],
    # JAX wheel tests (linux-only). JAX has no multi-arch fan-out, so a single
    # test workflow covers the pipeline (keyed per-arch by `amdgpu_family`).
    "test_linux_jax_wheels.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="jax",
            pipeline_phase="test",
        ),
    ],
    "test_linux_jax_wheels_partial.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="jax",
            pipeline_phase="test",
        ),
    ],
    # --- Linux: native packages (rpm/deb fan-out keyed by the
    # `native_package_type` input) ---
    "multi_arch_build_native_linux_packages.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="native_packages",
            pipeline_phase="rpm",
            match_when={"native_package_type": "rpm"},
        ),
        WorkflowSpec(
            platform="linux",
            pipeline_type="native_packages",
            pipeline_phase="deb",
            match_when={"native_package_type": "deb"},
        ),
    ],
    # Native Linux package install tests (rockrel release pipeline).
    "test_native_linux_packages_install.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="native_packages",
            pipeline_phase="test",
        ),
    ],
    # --- ROCm artifact tests. Dispatched once per family (single `amdgpu_families`)
    # by the linux/windows release orchestrators' `trigger_test_artifacts_per_family`
    # matrix, so each run is a per-arch ROCm test. ---
    "test_artifacts.yml": [
        WorkflowSpec(
            platform="",
            pipeline_type="rocm",
            pipeline_phase="test",
            platform_from_test_runs_on=True,
        ),
    ],
    # Per-component ROCm test shard-group: `test_artifacts.yml` fans out one
    # `test_component.yml` call per component (each internally sharded via a
    # matrix), and each call self-reports via notify_quartz with its own
    # already shard-rolled-up result. Registered here only so classification
    # doesn't raise (see `derive_platform_and_pipeline`) -- these completions
    # are otherwise disregarded (see `update_status_json`'s skip for this
    # workflow) until we decide whether component-level granularity is worth
    # tracking; `test_artifacts.yml`'s own report is still the artifact-level
    # source of truth for the `[platform][arch]` leaf.
    "test_component.yml": [
        WorkflowSpec(
            platform="",
            pipeline_type="rocm",
            pipeline_phase="test",
            platform_from_test_runs_on=True,
        ),
    ],
    # ROCm wheel tests: CI-only in TheRock (TBD whether this workflow is kept),
    # kept so a CI event still classifies as rocm/test, keyed per-arch by
    # `amdgpu_family`.
    "test_rocm_wheels.yml": [
        WorkflowSpec(
            platform="",
            pipeline_type="rocm",
            pipeline_phase="test",
            platform_from_test_runs_on=True,
        ),
    ],
    # --- Windows: portable rocm build ---
    "multi_arch_build_windows.yml": [
        WorkflowSpec(
            platform="windows",
            pipeline_type="rocm",
            pipeline_phase="build",
        ),
    ],
    "multi_arch_build_windows_artifacts.yml": [
        WorkflowSpec(
            platform="windows",
            pipeline_type="rocm",
            pipeline_phase="build",
        ),
    ],
    # --- Windows: framework wheel releases (build phase) ---
    "multi_arch_release_windows_pytorch_wheels.yml": [
        WorkflowSpec(
            platform="windows",
            pipeline_type="pytorch",
            pipeline_phase="build",
        ),
    ],
    "multi_arch_build_windows_pytorch_wheels.yml": [
        WorkflowSpec(
            platform="windows",
            pipeline_type="pytorch",
            pipeline_phase="build",
        ),
    ],
    # --- PyTorch wheel tests. The same reusable workflow is called by both
    # linux and windows PyTorch wheel release workflows; platform follows the
    # target runner label, arch comes from `amdgpu_family`.
    # Test leaves key by [platform][arch], so this never collides with a build
    # leaf. ---
    "test_pytorch_wheels.yml": [
        WorkflowSpec(
            platform="",
            pipeline_type="pytorch",
            pipeline_phase="test",
            platform_from_test_runs_on=True,
        ),
    ],
    # Full PyTorch test suite. Routed to its own `test-full` phase (a sibling of
    # `test`) so it does NOT collide with test_pytorch_wheels.yml: both can run
    # for the same build.
    "test_pytorch_wheels_full.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="pytorch",
            pipeline_phase="test-full",
        ),
    ],
    # --- Tarballs: platform carried by the `platform` input (linux/windows);
    # fan out like the native-packages case so each run classifies to its
    # platform. ---
    "multi_arch_build_tarballs.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="rocm",
            pipeline_phase="build",
            match_when={"platform": "linux"},
        ),
        WorkflowSpec(
            platform="windows",
            pipeline_type="rocm",
            pipeline_phase="build",
            match_when={"platform": "windows"},
        ),
    ],
    # --- WSL: ROCDXG driver artifacts. Mapped to linux for now since it is
    # part of the portable linux build workflow. ---
    "multi_arch_build_wsl_rocdxg_artifacts.yml": [
        WorkflowSpec(
            platform="linux",
            pipeline_type="rocm",
            pipeline_phase="build",
        ),
    ],
    # --- Setup: runs once before the platform fan-out; computes the build
    # matrix, versions, and resolved ref. Platform-agnostic (`platform=""`),
    # so it gets a single global row rather than per-platform duplicates. ---
    "setup_multi_arch.yml": [
        WorkflowSpec(
            platform="",
            pipeline_type="setup",
            pipeline_phase="setup",
        ),
    ],
}

# Parent orchestrators that fan out into the `WORKFLOW_SPECS` leaves. They own
# no pipeline leaf of their own, so they classify to `pipeline_type=orchestrator`
# with a `pipeline_phase` naming the orchestrator; `platform` is "" for the
# top-level (both-platform) orchestrators and "linux"/"windows" for the
# per-platform ones. The status.json router keys off this tuple:
#   - top-level release orchestrators (`release`/`release-asan`/`repackage`)
#     stamp the document's completion signal;
#   - per-platform orchestrators (`release-linux`/`release-windows`, and the
#     `repackage` children) backfill pipeline slots from their captured per-job
#     results but carry no document-level completion signal;
#   - other top-level orchestrators (`python-packages`) carry no document
#     signal and are skipped silently.
ORCHESTRATOR_SPECS: dict[str, WorkflowSpec] = {
    "multi_arch_release.yml": WorkflowSpec(
        platform="", pipeline_type="orchestrator", pipeline_phase="release"
    ),
    "multi_arch_release_asan.yml": WorkflowSpec(
        platform="", pipeline_type="orchestrator", pipeline_phase="release-asan"
    ),
    "multi_arch_release_linux.yml": WorkflowSpec(
        platform="linux", pipeline_type="orchestrator", pipeline_phase="release-linux"
    ),
    "multi_arch_release_windows.yml": WorkflowSpec(
        platform="windows",
        pipeline_type="orchestrator",
        pipeline_phase="release-windows",
    ),
    # Repackage existing artifacts without rebuilding; fans out to the (noop)
    # per-platform repackage children, so it owns no leaf itself.
    "multi_arch_repackage.yml": WorkflowSpec(
        platform="", pipeline_type="orchestrator", pipeline_phase="repackage"
    ),
    # Per-platform repackage children dispatched by multi_arch_repackage.yml.
    # Like the other non-release per-platform orchestrators they carry no
    # document-level signal, so the router captures them without writing a leaf.
    "multi_arch_repackage_linux.yml": WorkflowSpec(
        platform="linux", pipeline_type="orchestrator", pipeline_phase="repackage"
    ),
    "multi_arch_repackage_windows.yml": WorkflowSpec(
        platform="windows", pipeline_type="orchestrator", pipeline_phase="repackage"
    ),
    # `build_*_python_packages.yml` package the ROCm wheels produced by the rocm
    # build. They carry notify_quartz jobs and their events are captured, but
    # they deliberately do NOT map to a `rocm/build` leaf: that leaf belongs to
    # `multi_arch_build_portable_linux[_artifacts].yml` and carries per-family
    # `variants`; a non-variant python-packages leaf would clobber it. As
    # per-platform orchestrators they carry no document-level signal, so the
    # router captures them without writing a status.json leaf.
    "build_portable_linux_python_packages.yml": WorkflowSpec(
        platform="linux", pipeline_type="orchestrator", pipeline_phase="python-packages"
    ),
    "build_windows_python_packages.yml": WorkflowSpec(
        platform="windows",
        pipeline_type="orchestrator",
        pipeline_phase="python-packages",
    ),
}

# Pipelines every release is expected to run, per platform. rocm + pytorch run
# on both; jax + native_packages are linux-only. The status.json rollup counts
# an expected-but-unstarted pipeline against its platform, so a platform never
# reads `success` while a pipeline it will still run is unreported. Mirrors the
# orchestrator `needs:` fan-out (multi_arch_release_{linux,windows}.yml).
EXPECTED_PIPELINE_TYPES: dict[str, frozenset[str]] = {
    "linux": frozenset({"rocm", "pytorch", "jax", "native_packages"}),
    "windows": frozenset({"rocm", "pytorch"}),
}


def parse_gh_datetime(value: str | None) -> datetime | None:
    """Parse a GitHub API datetime string into a tz-aware datetime, or None.

    GitHub returns ISO 8601 with a trailing `Z`; an empty/missing value
    returns `None` (the normal "field absent" case). `datetime.fromisoformat`
    only accepts a trailing `Z` on Python 3.11+, so it is normalized to an
    explicit UTC offset first to also support 3.10.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"parse_gh_datetime: failed to parse {value!r} as ISO 8601"
        ) from exc


def _normalize_labels(labels: Any) -> list[str]:
    """Normalize job labels from GitHub API format to plain strings."""
    if not labels:
        return []
    out: list[str] = []
    for lb in labels:
        if isinstance(lb, str):
            out.append(lb)
        elif isinstance(lb, dict) and lb.get("name") is not None:
            out.append(str(lb["name"]))
    return out


# ---------------------------------------------------------------------------
# Input dataclasses: structured representations of GitHub dispatch payloads
# ---------------------------------------------------------------------------
#
# Single tree rooted at `TheRockDispatchEvent` (the envelope); sub-objects
# are populated selectively by `event_type` (see `KNOWN_EVENT_TYPES`). Each
# dataclass owns a `from_dict`; declared leaves-first so annotations resolve
# without forward references.
#
#   TheRockDispatchEvent              # envelope root
#     event_type: str                 # routes which sub-object is set
#     repository: str
#     action: str                     # GitHub webhook action sub-type
#     workflow_run:  WorkflowRunRecord  | None    # `workflow_run_*` events
#       jobs:        list[WorkflowJobRecord]      # inline jobs
#       api_jobs:    list[WorkflowJobRecord]      # enrichment-fetched jobs
#       classification: Classification            # classifier-derived view
#     pull_request:  PullRequestInput   | None    # `pull_request_event`
#     push_event:    PushEventInput     | None    # `push_event`
#     raw:           dict[str, Any]               # verbatim wire payload


@dataclass
class WorkflowJobRecord:
    """Parsed job from the GitHub API or an inline dispatch payload.

    Both sources share this shape: inline jobs are carried by
    `notify_quartz.py` in the dispatch payload, API jobs are fetched in
    enrichment (same fields, richer step/label detail).
    """

    # GitHub Actions job ID. `0` is a sentinel for missing input.
    job_id: int

    # Rendered job name (matrix-expanded for matrix workflows, e.g.
    # `"Build (gfx94X-dcgpu, linux, release)"`).
    name: str

    # GitHub Actions run-status vocabulary, preserved verbatim:
    # `queued` | `in_progress` | `completed` | `waiting` |
    # `requested` | `pending`.
    status: str

    # GitHub Actions conclusion vocabulary, preserved verbatim:
    # `success` | `failure` | `cancelled` | `skipped` |
    # `timed_out` | `action_required` | `neutral` | `stale` |
    # `startup_failure`. `None` while `status != "completed"`.
    conclusion: str | None

    # When GitHub registered the job (before a runner picks it up).
    created_at: datetime | None

    # When the runner began executing. `None` for jobs that never
    # started (cancelled / skipped before assignment).
    started_at: datetime | None

    # When the job finished. `None` while the job is still running
    # or queued.
    completed_at: datetime | None

    # Runner that picked the job. Usually populated (we observe jobs
    # from `in_progress` on); `""` for jobs cancelled before assignment.
    runner_name: str

    # Runner capability tags the scheduler matched against `runs-on:`
    # (e.g. `"ubuntu-24.04"`, `"self-hosted"`, `"gpu"`). A runner exposes
    # many, hence a list.
    labels: list[str]

    # Per-step entries verbatim from the API (raw dicts); kept unparsed
    # until a consumer needs them.
    steps: list[dict[str, Any]]

    # Job's `$GITHUB_STEP_SUMMARY` markdown. Not readable from the env.
    # Forward-compat slot: will be filled by the enrichment script.
    summary: str

    # Free-form job-emitted KPIs (build duration, artifact size, ...).
    # Forward-compat slot; unpopulated today.
    metrics: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkflowJobRecord":
        return cls(
            job_id=raw.get("id", 0),
            name=raw.get("name", ""),
            status=raw.get("status", ""),
            conclusion=raw.get("conclusion"),
            created_at=parse_gh_datetime(raw.get("created_at")),
            started_at=parse_gh_datetime(raw.get("started_at")),
            completed_at=parse_gh_datetime(raw.get("completed_at")),
            runner_name=raw.get("runner_name") or "",
            labels=_normalize_labels(raw.get("labels")),
            steps=raw.get("steps") or [],
            summary=raw.get("summary") or "",
            metrics=raw.get("metrics") or {},
        )


@dataclass
class Classification:
    """Classifier-derived view of a `WorkflowRunRecord`.

    Populated by `therock_classify.py` between enrichment and ingest.
    """

    # `"linux"` | `"windows"` | `""` (unmatched). Resolved together
    # with `pipeline_type` / `pipeline_phase` by matching `path` (plus
    # `inputs` for fan-out workflows) against `WORKFLOW_SPECS`.
    platform: str = ""

    # `"rocm"` | `"pytorch"` | `"jax"` | `"native_packages"` | `"setup"` | `""`.
    # Which pipeline the run belongs to; also used in the database.
    pipeline_type: str = ""

    # `"build"` | `"test"` | `"rpm"` | `"deb"` | `"setup"` | `""`. Phase within
    # `pipeline_type` (only applicable phases occur -- `native_packages`
    # yields `"rpm"` / `"deb"`, never `"build"`; `setup` yields `"setup"`).
    # Consumers slot the run into `runs[pipeline_type][pipeline_phase]`.
    pipeline_phase: str = ""

    # AMD GPU families targeted by the run, from `inputs.amdgpu_families`
    # (tier 1) or extracted from job names (tier 2). Empty = unclassified.
    architectures: list[str] = field(default_factory=list)

    # Free-form test taxonomy from `inputs.test_type` (e.g. `"full"`),
    # accepted verbatim. Empty = no value / not a test workflow.
    test_type: str = ""

    # Free-form build variant from `inputs.build_variant` /
    # `inputs.variant` (e.g. `"release"` | `"debug"` | `"asan"`),
    # accepted verbatim. Empty = no value / not a build workflow.
    build_variant: str = ""

    # Wheel-style release id (always wheel form, unlike the
    # package-flavored `rocm_version`); routes status.json output and is
    # parsed by `RELEASE_VERSION_*_RE`.
    #   None  -- absent (typical for children, which inherit the
    #            parent's value via `trigger_workflow_run_id`)
    #   ""    -- explicitly opted out
    #   else  -- e.g. `7.13.0a20260415` (nightly), `7.13.0rc1`
    #            (prerelease), `7.13.0.dev0+<sha>` (dev)
    release_version: str | None = None

    # GitHub Actions run id whose artifacts/outputs this record points to:
    # `inputs.artifact_run_id` (child test runs) -> `trigger_workflow_run_id`
    # (fan-out children) -> own `workflow_run_id`. Resolved once by the
    # classifier so artifact/tarball/wheel URL derivation never recomputes it.
    source_run_id: str | None = None


@dataclass
class WorkflowRunRecord:
    """Parsed workflow_run object from a dispatch payload."""

    # GitHub `workflow_run.id`; primary key on
    # `therock_workflow_runs`.
    workflow_run_id: int

    # Monotonic per-workflow run counter (the "#1234" shown in the
    # GitHub UI). Resets per workflow file, not global across the
    # repo.
    run_number: int

    # `1` for the first attempt; increments on retry. Pair with
    # `run_number` to identify a specific attempt.
    run_attempt: int

    # Workflow display name from the YAML's top-level `name:`
    # (e.g. `"Multi-Arch CI - Linux"`). Stable across runs.
    name: str

    # GitHub-rendered run title (Actions UI). NOT stable; never route on
    # it (it is the commit message for push/schedule, and embeds the GPU
    # family for some workflows).
    display_title: str

    # GitHub event verbatim (`workflow_dispatch` | `workflow_call` |
    # `push` | `pull_request` | `schedule` | ...). Named `trigger_event`
    # to disambiguate from the envelope's `event_type`.
    trigger_event: str

    # Workflow file path under `.github/workflows/`. The stable anchor
    # for path-based routing in `therock_classify.py` (tier 1).
    path: str

    # GitHub Actions run-status vocabulary, preserved verbatim:
    # `queued` | `in_progress` | `completed` | `waiting` |
    # `requested` | `pending`.
    status: str

    # GitHub Actions conclusion vocabulary, preserved verbatim:
    # `success` | `failure` | `cancelled` | `skipped` |
    # `timed_out` | `action_required` | `neutral` | `stale` |
    # `startup_failure`. `None` while `status != "completed"`.
    conclusion: str | None

    # Ref the run was triggered against (e.g. `"main"`). Empty only as a
    # missing-data fallback.
    head_branch: str

    # Commit SHA the run was triggered against. Empty only as a
    # missing-data fallback.
    head_sha: str

    # GitHub's stable workflow definition ID -- one per workflow
    # file, constant across runs and renames.
    workflow_id: int

    # Permalink to the run on github.com.
    html_url: str

    # When GitHub queued the run.
    created_at: datetime | None

    # When the workflow's first job started executing. `None`
    # while still queued.
    run_started_at: datetime | None

    # Last time GitHub touched the run record (job-state changes,
    # etc.).
    updated_at: datetime | None

    # `actor.login`, falling back to `triggering_actor.login`. Empty when
    # both are absent (rare; system-triggered runs).
    actor_login: str

    # First PR number from the payload's `pull_requests`, or `None` for
    # non-PR events. Multiple entries are possible.
    pr_number: int | None

    # Title of the first PR (paired with `pr_number`), or `None` for
    # non-PR events.
    pr_title: str | None

    # Release tier: one of `KNOWN_RELEASE_TYPES`, or `None`.
    release_type: str | None

    # Package-flavored ROCm version (wheel/deb/rpm differ, e.g.
    # `7.13.0~20260415` vs `7.13.0a20260415`). NOT for routing -- use
    # `classification.release_version`.
    rocm_version: str

    # Verbatim `${{ toJSON(inputs) }}` blob (forwarded by
    # `notify_quartz.py`). Primary source for classifier tier 1.
    inputs: dict[str, Any]

    # Verbatim env block from notify_quartz.py self-report. Source
    # for `rocm_version` and any other env-driven classification.
    env: dict[str, Any]

    # Parent context `{"id", "name"}` or `None`. Set by `notify_quartz.py`
    # via `check_suite_id` for `workflow_call` children.
    parent_workflow: dict[str, Any] | None

    # `uses:` workflows, GitHub-API shape. Verbatim dicts; unparsed
    # until a consumer needs them.
    referenced_workflows: list[dict[str, Any]]

    # Immediate parent run ID when GitHub exposes one. Status.json derives its
    # effective document owner after classification.
    trigger_workflow_run_id: int | None

    # Inline jobs from the dispatch payload. Empty for kickoff dispatches
    # where no job has run yet.
    jobs: list[WorkflowJobRecord]

    # Jobs fetched during enrichment (`GitHubAPI.get_workflow_run_jobs`),
    # richer than `jobs`. `None` when enrichment did not run or failed.
    api_jobs: list[WorkflowJobRecord] | None = None

    # `${{ toJSON(needs) }}` blob (per-job `result` + `outputs`),
    # forwarded by notify_quartz.py. `None` when not supplied (e.g. at
    # run kickoff). Contractually a JSON object, so not typechecked.
    captured_outputs: dict[str, Any] | None = None

    # Non-fatal enrichment failures (`therock_enrich_data.py`); the
    # pipeline continues and consumers can surface them.
    enrichment_errors: list[str] = field(default_factory=list)

    # Classifier-derived view, nested so it stays distinct from the raw
    # wire fields above (e.g. `wr.classification.platform`). These are the
    # stable values consumers should route on (platform / pipeline_type /
    # pipeline_phase / architectures place a run in the DB and status.json);
    # the raw wire fields are inputs to classification, not routing keys.
    classification: Classification = field(default_factory=Classification)

    tarball_url: str | None = None
    wheels_url: str | None = None
    rpm_urls: dict[str, str] = field(default_factory=dict)
    deb_urls: dict[str, str] = field(default_factory=dict)
    artifacts_url: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkflowRunRecord":
        actor = raw.get("actor") or raw.get("triggering_actor") or {}
        prs = raw.get("pull_requests") or []
        pr_number = prs[0].get("number") if prs else None
        pr_title = prs[0].get("title") if prs else None
        if len(prs) > 1:
            log.warning(
                "workflow_run %s carries %d pull_requests; "
                "keeping pr_number=%s, dropping %s",
                raw.get("id"),
                len(prs),
                pr_number,
                [p.get("number") for p in prs[1:]],
            )
        inputs = raw.get("inputs") or {}
        env = raw.get("env") or {}
        parent_wf = raw.get("parent_workflow")
        if not isinstance(parent_wf, dict):
            parent_wf = None
        parent_input_run_id = _parse_int(inputs.get("parent_run_id"))
        if parent_wf is None and parent_input_run_id is not None:
            parent_wf = {
                "id": parent_input_run_id,
                "name": inputs.get("parent_workflow") or "",
            }
        trigger_workflow_run_id = (
            parent_wf.get("id") if isinstance(parent_wf, dict) else None
        )
        captured_outputs = raw.get("captured_outputs")
        # release_type comes from the propagated `quartz_tracking_id` (the
        # authoritative channel the top-level orchestrator published to);
        # `inputs.release_type` is the direct declared input that feeds that id
        # and is the only source on the orchestrator's own record and manual
        # dispatches. "" (CI) / absent both normalize to None.
        _, quartz_release_type = parse_quartz_tracking_id(inputs)
        explicit_rt = quartz_release_type or inputs.get("release_type") or None
        if explicit_rt and explicit_rt not in KNOWN_RELEASE_TYPES:
            log.warning(
                "workflow_run %s has unrecognized release_type=%r; coercing to None",
                raw.get("id"),
                explicit_rt,
            )
            explicit_rt = None
        captured_setup_version: str = ""
        if isinstance(captured_outputs, dict):
            for need_data in captured_outputs.values():
                if not isinstance(need_data, dict):
                    continue
                outs = need_data.get("outputs")
                if not isinstance(outs, dict):
                    continue
                for vkey in ("rocm_package_version", "version"):
                    val = outs.get(vkey)
                    if val:
                        captured_setup_version = str(val)
                        break
                if captured_setup_version:
                    break
        inline_jobs = raw.get("jobs") or []
        return cls(
            workflow_run_id=raw.get("id", 0),
            run_number=raw.get("run_number", 0),
            run_attempt=raw.get("run_attempt", 1),
            name=raw.get("name", ""),
            display_title=raw.get("display_title") or raw.get("name") or "",
            trigger_event=raw.get("event", ""),
            path=raw.get("path") or "",
            status=raw.get("status", ""),
            conclusion=raw.get("conclusion"),
            head_branch=raw.get("head_branch") or "",
            head_sha=raw.get("head_sha") or "",
            workflow_id=raw.get("workflow_id", 0),
            html_url=raw.get("html_url") or "",
            created_at=parse_gh_datetime(raw.get("created_at")),
            run_started_at=parse_gh_datetime(raw.get("run_started_at")),
            updated_at=parse_gh_datetime(raw.get("updated_at")),
            actor_login=actor.get("login") or "",
            pr_number=pr_number,
            pr_title=pr_title,
            release_type=explicit_rt,
            rocm_version=(
                inputs.get("rocm_version")
                or inputs.get("rocm_package_version")
                # `package_version` is the wheel-style version key used by the
                # tarballs and python-packages producers (see TheRock's
                # multi_arch_build_tarballs.yml / build_portable_linux_python_packages.yml).
                or inputs.get("package_version")
                or captured_setup_version
                # Last resort: the PyTorch full-test dispatch (an upstream
                # benc-uk workflow we cannot add a `rocm_version` input to)
                # only carries `torch_version`, which still embeds the ROCm
                # version as a local-version segment, e.g.
                # `2.12.0+rocm7.15.0a20260702`; `_rocm_version_segment`
                # extracts it back out.
                or inputs.get("torch_version")
                or ""
            ),
            inputs=inputs,
            env=env,
            parent_workflow=parent_wf,
            referenced_workflows=raw.get("referenced_workflows") or [],
            trigger_workflow_run_id=trigger_workflow_run_id,
            jobs=[WorkflowJobRecord.from_dict(j) for j in inline_jobs],
            captured_outputs=captured_outputs,
        )


@dataclass
class PullRequestInput:
    """Parsed `pull_request` object from a dispatch payload."""

    number: int
    id: int
    # GitHub PR state: `"open"` or `"closed"`.
    state: str
    title: str
    draft: bool
    merged: bool
    user_login: str
    head_ref: str
    base_ref: str
    head_sha: str
    merge_commit_sha: str | None
    created_at: datetime | None
    updated_at: datetime | None
    closed_at: datetime | None
    merged_at: datetime | None
    additions: int | None
    deletions: int | None
    changed_files: int | None
    commits: int | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PullRequestInput":
        head = raw.get("head") or {}
        base = raw.get("base") or {}
        user = raw.get("user") or {}
        return cls(
            number=raw["number"],
            id=raw["id"],
            state=raw.get("state", ""),
            title=raw.get("title", ""),
            draft=bool(raw.get("draft")),
            merged=bool(raw.get("merged")),
            user_login=user.get("login") or "",
            head_ref=head.get("ref") or "",
            base_ref=base.get("ref") or "",
            head_sha=head.get("sha") or "",
            merge_commit_sha=raw.get("merge_commit_sha"),
            created_at=parse_gh_datetime(raw.get("created_at")),
            updated_at=parse_gh_datetime(raw.get("updated_at")),
            closed_at=parse_gh_datetime(raw.get("closed_at")),
            merged_at=parse_gh_datetime(raw.get("merged_at")),
            additions=raw.get("additions"),
            deletions=raw.get("deletions"),
            changed_files=raw.get("changed_files"),
            commits=raw.get("commits"),
        )


@dataclass
class PushEventInput:
    """Parsed push event (`event_type="push_event"`) from a dispatch payload.

    Unlike `PullRequestInput`, push fields are flattened onto the envelope
    root (not nested), so `from_dict` takes the FULL envelope plus a
    separately-resolved `repo` string.
    """

    delivery_id: str
    ref: str
    before_sha: str
    after_sha: str
    pusher: str
    forced: bool
    commits_count: int
    repository: str
    pushed_at: datetime | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], repo: str) -> "PushEventInput":
        pusher = raw.get("pusher") or {}
        commits = raw.get("commits")
        # `head_commit` is legitimately `null` on the wire for some pushes
        # (branch/tag deletes, the occasional "no commits" edge); coalesce
        # to `{}` so the timestamp lookup stays safe and the fallbacks below
        # can still recover `pushed_at`.
        head_commit = raw.get("head_commit") or {}
        # Tier 1: head_commit.timestamp. Tier 2: top-level pushed_at.
        # Tier 3: walk commits[] in reverse for the most recent timestamp.
        pushed_at = parse_gh_datetime(
            head_commit.get("timestamp")
        ) or parse_gh_datetime(raw.get("pushed_at"))
        if pushed_at is None and isinstance(commits, list):
            for c in reversed(commits):
                pushed_at = parse_gh_datetime(c.get("timestamp"))
                if pushed_at is not None:
                    break
        return cls(
            delivery_id=raw.get("delivery_id", ""),
            ref=raw.get("ref", ""),
            before_sha=raw.get("before", ""),
            after_sha=raw.get("after", ""),
            pusher=pusher.get("name") or pusher.get("login") or "",
            forced=bool(raw.get("forced")),
            commits_count=len(commits) if isinstance(commits, list) else 0,
            repository=repo,
            pushed_at=pushed_at,
        )


@dataclass
class TheRockDispatchEvent:
    """Top-level dispatch payload envelope.

    Built from the raw JSON by `from_dict`. One sub-object is populated
    per dispatch based on `event_type`:

      `workflow_run_in_progress` / `..._completed` -> workflow_run
      `pull_request_event`                          -> pull_request
      `push_event`                                  -> push_event
    """

    event_type: str
    repository: str
    action: str = ""
    workflow_run: WorkflowRunRecord | None = None
    pull_request: PullRequestInput | None = None
    push_event: PushEventInput | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TheRockDispatchEvent":
        event_type = raw.get("event_type", "")
        repo = raw.get("repository", "")

        workflow_run: WorkflowRunRecord | None = None
        if isinstance(raw.get("workflow_run"), dict):
            workflow_run = WorkflowRunRecord.from_dict(raw["workflow_run"])

        pull_request: PullRequestInput | None = None
        pr_raw = raw.get("pull_request")
        if isinstance(pr_raw, dict) and pr_raw:
            pull_request = PullRequestInput.from_dict(pr_raw)

        push_event: PushEventInput | None = None
        if event_type == "push_event":
            push_event = PushEventInput.from_dict(raw, repo)

        return cls(
            event_type=event_type,
            repository=repo,
            action=raw.get("action", ""),
            workflow_run=workflow_run,
            pull_request=pull_request,
            push_event=push_event,
            raw=raw,
        )
