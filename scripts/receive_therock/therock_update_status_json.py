#!/usr/bin/env python
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Create or update status.json from an enriched TheRock dispatch payload.

Entry point in the pipeline (called by therock_process_data.py after enrichment):

    update_status_json(
        payload: TheRockDispatchEvent,
        repo_dir: Path,
        commit_and_push: bool = True,
    ) -> Path | None

Precondition: `payload` is already validated, enriched, and classified by the
upstream pipeline -- in particular, `workflow_run.trigger_workflow_run_id` is
already the resolved top-level owner run id (see
`therock_classify.derive_effective_owner_run_id`), not the raw immediate
GitHub parent. This module does no GitHub API calls and no workflow routing;
it owns the status.json candidacy gate and returns `None` when the payload
does not qualify (see `update_status_json` for the gate conditions).

When `commit_and_push` is True (production): each attempt fetches and
hard-resets onto the upstream head, then applies the run, commits, and pushes,
up to `MAX_RETRIES` times. Resetting to `@{u}` before every attempt drops a
commit that lost the push race and rebuilds against whatever already landed, so
each attempt starts from fresh upstream state (back off randomly between
retries).

When `commit_and_push` is False (tests / dry-run): apply once and write to
disk. No git operations are performed; `repo_dir` only needs to exist as a
filesystem location.
"""

import json
import logging
import os
import random
import re
import subprocess
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath

import ntplib

from therock_classify import (
    FINALIZING_PHASES,
    GPU_FAMILY_TOKEN,
    RELEASE_CDN_PHASES,
    is_top_level_orchestrator,
)
from therock_status_document import (
    Pipelines,
    RunLeaf,
    Status,
    StatusDocument,
    Variant,
    merge_matrix_test_leaf,
    rollup_statuses,
)
from therock_summary import freeze_requested_architectures, rebuild_summary
from therock_types import (
    RELEASE_VERSION_DEV_RE,
    RELEASE_VERSION_NIGHTLY_RE,
    RELEASE_VERSION_PRERELEASE_RE,
    TheRockDispatchEvent,
    WorkflowJobRecord,
    WorkflowRunRecord,
)

log = logging.getLogger(__name__)


# Push-race retry tuning. A large release fan-out has dozens of runs pushing to
# the same branch ref within a minute or two, so a losing run must be able to
# wait out that whole contention window. Each attempt rebuilds against fresh
# upstream (see the loop in `update_status_json`), so retrying is idempotent and
# safe -- the only reason to cap attempts is to bound wasted CI time. The cap is
# the only tuning lever for contention, so it is overridable via the
# `QUARTZ_STATUS_PUSH_MAX_RETRIES` env var for unusually large fan-outs.
def _max_retries() -> int:
    raw = os.environ.get("QUARTZ_STATUS_PUSH_MAX_RETRIES")
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
            log.warning(
                "QUARTZ_STATUS_PUSH_MAX_RETRIES=%r is not a positive int; "
                "falling back to default %s",
                raw,
                _DEFAULT_MAX_RETRIES,
            )
        except ValueError:
            log.warning(
                "QUARTZ_STATUS_PUSH_MAX_RETRIES=%r is not an int; "
                "falling back to default %s",
                raw,
                _DEFAULT_MAX_RETRIES,
            )
    return _DEFAULT_MAX_RETRIES


_DEFAULT_MAX_RETRIES = 12
# Exponential backoff with full jitter (AWS-style): attempt N (1-indexed) waits a
# random duration in [0, min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * 2**(N-1))]. Full
# jitter de-synchronizes runs that started together so they stop colliding.
BACKOFF_BASE_SEC = 1.0
BACKOFF_CAP_SEC = 30.0
# Randomized delay before the very first push so simultaneously-started runs
# do not all hit the ref in lockstep on attempt 0.
INITIAL_JITTER_SEC = 3.0
# A `.git/*.lock` older than this is treated as stale (left by a killed prior
# git) and removed. Each runner is the sole writer of its own checkout -- the
# only contention is at the *remote* ref -- so a lingering local lock is
# effectively never a live concurrent git. The age guard is belt-and-braces:
# a lock younger than this is left alone and the loop backs off instead.
STALE_LOCK_AGE_SEC = 60.0
NTP_TIMEOUT_SEC = 2


def _utc_now() -> datetime:
    """Get current UTC time, preferring NTP over system clock."""
    try:
        client = ntplib.NTPClient()
        response = client.request("pool.ntp.org", version=3, timeout=NTP_TIMEOUT_SEC)
        return datetime.fromtimestamp(response.tx_time, tz=timezone.utc).replace(
            microsecond=0
        )
    except Exception:
        log.warning("NTP query failed, falling back to system clock")
        return datetime.now(timezone.utc).replace(microsecond=0)


def _validate_clock(now: datetime, workflow_run: WorkflowRunRecord) -> None:
    """Raise if our clock is behind any timestamp in the workflow run (clock drift)."""
    for label, ts in (
        ("created_at", workflow_run.created_at),
        ("run_started_at", workflow_run.run_started_at),
        ("updated_at", workflow_run.updated_at),
    ):
        if ts is not None and now < ts:
            raise ValueError(
                f"Clock drift detected: current time {_datetime_to_z(now)} is older than "
                f"workflow_run.{label}={_datetime_to_z(ts)}. Check NTP synchronization."
            )


def _git(
    args: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git"] + args,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        # capture_output swallows git's diagnostics; surface them before the
        # traceback so operators see why the command failed, not a bare exit code.
        log.error("git %s failed (exit %s)", " ".join(args), e.returncode)
        if e.stdout:
            log.error("stdout: %s", e.stdout)
        if e.stderr:
            log.error("stderr: %s", e.stderr)
        raise


def _datetime_to_z(dt: datetime) -> str:
    """Convert a datetime to ISO-8601 UTC string with Z suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _release_version_suffix(release_version: str) -> str:
    """Extract the routing suffix from a release_version string.

    Accepts the two release formats that reach the updater:

      "7.13.0a20260415" -> "20260415"   (nightly:    8-digit YYYYMMDD after "a")
      "7.13.0rc1"       -> "rc1"        (prerelease: "rc" + integer)
    """
    if RELEASE_VERSION_DEV_RE.search(release_version):
        raise ValueError(
            f"Given release_version {release_version!r} is a dev build. "
            "therock_update_status_json should only be called with release "
            "(nightly/prerelease) builds."
        )

    m = RELEASE_VERSION_PRERELEASE_RE.match(release_version)
    if m:
        return m.group(1)

    m = RELEASE_VERSION_NIGHTLY_RE.match(release_version)
    if m:
        return m.group(1)

    raise ValueError(
        f"Cannot extract suffix from release_version {release_version!r}; "
        "expected '<major>.<minor>.<patch>a<YYYYMMDD>' (nightly) or "
        "'<major>.<minor>.<patch>rc<N>' (prerelease)."
    )


def _status_json_path(
    repo_dir: Path,
    release_type: str,
    workflow_run: WorkflowRunRecord,
) -> Path:
    release_version = workflow_run.classification.release_version or ""
    # Test workflows are dispatched without a version input, so their events
    # carry no release_version.
    if not release_version and release_type == "nightly" and workflow_run.created_at:
        suffix = workflow_run.created_at.strftime("%Y%m%d")
    else:
        suffix = _release_version_suffix(release_version)

    if release_type == "nightly":
        return repo_dir / "release-nightly" / suffix / "status.json"
    if release_type == "prerelease":
        base, full = _prerelease_dirs(release_version)
        return repo_dir / "prereleases" / base / full / "status.json"

    raise ValueError(f"Unexpected release_type: {release_type!r}")


_PRERELEASE_VERSION_KEY_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)rc(\d+)$")


def _prerelease_dirs(release_version: str) -> tuple[str, str]:
    """Split a prerelease version into its (base, full) directory names.

    "7.14.0rc2" -> ("7.14.0", "7.14.0rc2")
    """
    m = RELEASE_VERSION_PRERELEASE_RE.match(release_version)
    if not m:
        raise ValueError(
            f"Cannot route prerelease version {release_version!r}; "
            "expected '<major>.<minor>.<patch>rc<N>'."
        )
    base = release_version[: m.start(1)]
    return base, release_version


def _prerelease_version_key(version: str) -> tuple[int, int, int, int]:
    """Sorts release candidates numerically so `latest.json` never regresses from,
    e.g., rc10 to rc2.
    """
    m = _PRERELEASE_VERSION_KEY_RE.match(version)
    if not m:
        return (0, 0, 0, 0)
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4]))


_DOC_RELEASE_TYPE = {"nightly": "nightly", "prerelease": "rc"}


def _doc_release_type(release_type: str) -> str:
    return _DOC_RELEASE_TYPE.get(release_type, release_type)


def _load_or_create_document(
    status_path: Path,
    workflow_run: WorkflowRunRecord,
    release_type: str,
    now: str,
) -> tuple[StatusDocument, bool]:
    """Return (doc, file_created). file_created is True when no file existed on disk."""
    if status_path.exists():
        data = json.loads(status_path.read_text(encoding="utf-8"))
        return StatusDocument.from_dict(data), False

    build_date = (
        workflow_run.created_at.strftime("%Y%m%d") if workflow_run.created_at else ""
    )
    created_at_str = (
        _datetime_to_z(workflow_run.created_at) if workflow_run.created_at else None
    )

    return (
        StatusDocument(
            release_type=_doc_release_type(release_type),
            rocm_version=workflow_run.classification.release_version or "",
            build_date=build_date,
            status_json_created=now,
            status_json_last_updated=now,
            created_at=created_at_str,
        ),
        True,
    )


def _update_document_metadata(
    doc: StatusDocument,
    workflow_run: WorkflowRunRecord,
    now: str,
) -> None:
    """Merge top-level metadata fields from the incoming run into the document."""
    doc.status_json_last_updated = now

    if not doc.created_at and workflow_run.created_at is not None:
        doc.created_at = _datetime_to_z(workflow_run.created_at)

    if workflow_run.classification.release_version and not doc.rocm_version:
        doc.rocm_version = workflow_run.classification.release_version

    if workflow_run.created_at is not None:
        doc.build_date = workflow_run.created_at.strftime("%Y%m%d")


_CONCLUSION_MAP: dict[str, Status] = {
    "success": Status.success,
    "failure": Status.failure,
    "timed_out": Status.failure,
    "action_required": Status.failure,
    "stale": Status.failure,
    "cancelled": Status.cancelled,
    "skipped": Status.skipped,
    "neutral": Status.success,
}


# Matrix-cell job name for fan-out builds, e.g. TheRock's
#   "Build | py 3.12 | torch release/2.10"   (pytorch)
#   "Build | py 3.12 | jax rocm-jaxlib-v0.9" (jax)
# Case-insensitive: a calling orchestrator's own composite job name can wrap
# this in a differently-cased ancestor segment, e.g. rockrel's
# "Release | py 3.12 | JAX 0.11.0 / Build | py 3.12 | jax rocm-jaxlib-v0.11.0"
# -- the nested Test sub-job under that same cell has no (py, ref) of its own
# and relies entirely on that ancestor segment to be recognized.
_MATRIX_JOB_RE = re.compile(
    r"py\s+(?P<py>\S+)\s*\|\s*(?:torch|jax)\s+(?P<ref>\S+)", re.IGNORECASE
)

# One (py, ref) build cell can nest per-arch test jobs, e.g.
#   "Build | py 3.12 | torch release/2.10 / Test | gfx942 | linux-gfx942-1gpu..."
# Extracts the arch a job's own "Test | <arch>" segment names, if any, so
# jobs from different architectures nested under the same cell are never
# grouped together as if they were one architecture's result. Deliberately
# anchored to the "Test | " segment rather than reusing therock_classify's
# bare `_GPU_FAMILY_RE` (though it shares the same GPU_FAMILY_TOKEN shape):
# an unanchored scan would also match the runner-label segment that often
# follows in the same job name (e.g. "linux-gfx942-1gpu-..." above, which
# names a *different* family string than the job's own "Test | gfx94X-dcgpu"
# segment) and reintroduce the cross-arch conflation this exists to prevent.
# Case-insensitive (like `_MATRIX_JOB_RE`): this regex is also the build-vs-test
# partition signal in `_is_test_subjob`, so a differently-cased "test |" segment
# must not be misread as a build sub-job.
_TEST_ARCH_JOB_RE = re.compile(
    rf"Test\s*\|\s*(?P<arch>{GPU_FAMILY_TOKEN})", re.IGNORECASE
)

# pipeline_type -> the matrix axis key used in the variant (reference schema:
# pytorch cells key the ref as "torch", jax cells as "jax_version"). The jax axis
# is named for what it holds -- a bare version ("0.11.0"), not a git ref -- since
# the test side of a jax cell never exposes the full ref (see `_normalize_ref`).
_VARIANT_AXIS_KEY: dict[str, str] = {"pytorch": "torch", "jax": "jax_version"}

# TheRock's jax build matrix names each cell by its git ref
# ("rocm-jaxlib-v0.11.0"), while the release orchestrator's own name segment and
# the test dispatch inputs name the same cell by bare version ("0.11.0"). Both
# spellings identify one (py, version) cell, so left un-normalized they key two
# distinct variants that never merge -- doubling every jax build and test count.
# The full ref lives only on the build side; a test job's name carries only the
# bare-version ancestor, so bare is the sole spelling common to both sides.
# Canonicalize to the bare version (strip the prefix) at every point a jax ref
# enters a variant key, so the two spellings collapse. Stripping is idempotent
# on already-bare refs and, unlike adding the prefix, never mangles a non-version
# ref (e.g. a branch name).
_JAX_REF_PREFIX = "rocm-jaxlib-v"


def _normalize_ref(axis_key: str, ref: str) -> str:
    """Canonicalize one matrix-cell ref before it becomes a variant key.

    Called uniformly for every fan-out axis (torch and jax_version), so it takes
    the axis and dispatches internally. Today only jax needs it -- the torch axis
    is a pure passthrough -- so a torch ref is always returned verbatim; add an
    axis branch here if pytorch ever grows the same two-spellings problem.
    """
    if axis_key == "jax_version" and ref.startswith(_JAX_REF_PREFIX):
        return ref[len(_JAX_REF_PREFIX) :]
    return ref


# Workflow filenames (`wr.path`'s basename) that are registered in
# WORKFLOW_SPECS solely so classification doesn't raise, but whose
# completions `update_status_json` disregards entirely -- nothing is written
# to status.json for them. Add an entry here instead of a one-off check
# whenever a workflow's own report shouldn't move any leaf.
#
# `test_component.yml`: `test_artifacts.yml` fans out one call per ROCm test
# component (each internally sharded via its own matrix), and each self-
# reports independently via notify_quartz. `wr.path` is rewritten to the
# *reporting* workflow's filename (see notify_quartz.py's `reporting_workflow`
# override), so this distinguishes those per-component completions from
# `test_artifacts.yml`'s own run-level report, which remains the
# artifact-level source of truth for the `[platform][arch]` leaf. Disregarded
# until we decide whether component-level granularity is worth tracking.
_SKIP_WORKFLOW_NAMES: frozenset[str] = frozenset({"test_component.yml"})

# Run inputs that carry the (py, ref) cell for single-cell runs (tests), tried
# in order. Tests report one arch per run and never fan the axis out into job
# names, so the cell lives in the dispatch inputs instead.
# Values are TheRock's own input field names (external contract), tried in
# order. jax's explicit bare `jax_version` input is preferred over `jax_ref`
# (full) so the axis reads its canonical spelling directly; `_normalize_ref`
# still strips `jax_ref` when only that is present.
_VARIANT_INPUT_KEYS: dict[str, tuple[str, ...]] = {
    "torch": ("pytorch_git_ref", "torch_version"),
    "jax_version": ("jax_version", "jax_ref", "jax_git_ref"),
}


def _job_status(j: WorkflowJobRecord) -> Status:
    if j.conclusion:
        return _CONCLUSION_MAP.get(j.conclusion, Status.failure)
    return Status.in_progress


def _run_status(workflow_run: WorkflowRunRecord) -> Status:
    if workflow_run.conclusion:
        return _CONCLUSION_MAP.get(workflow_run.conclusion, Status.failure)
    return Status.in_progress


def _job_matches_arch(job_name: str, arch: str) -> bool:
    """True if `job_name` is arch-agnostic (no "Test | <arch>" segment of its
    own, e.g. the cell's shared build step) or explicitly names `arch`."""
    named = _TEST_ARCH_JOB_RE.findall(job_name)
    return not named or arch in named


def _is_test_subjob(job_name: str) -> bool:
    """True if the job is a nested test sub-job (carries a "Test | <arch>"
    segment of its own). A build sub-job never does. Used to partition a
    shared-run job list into its build half and its test half so a build leaf
    never absorbs test cells and vice versa."""
    return bool(_TEST_ARCH_JOB_RE.search(job_name))


def _job_archs(workflow_run: WorkflowRunRecord) -> frozenset[str]:
    """Distinct architectures named in any job's own "Test | <arch>" segment."""
    jobs = (
        workflow_run.api_jobs
        if workflow_run.api_jobs is not None
        else workflow_run.jobs
    )
    return frozenset(m for j in jobs for m in _TEST_ARCH_JOB_RE.findall(j.name))


def _variants_from_jobs(
    workflow_run: WorkflowRunRecord,
    axis_key: str,
    *,
    arch: str | None = None,
    phase: str | None = None,
) -> list[Variant]:
    """One variant per (py, ref) matrix cell parsed from job names.

    A single (py, ref) build cell can nest test jobs for several
    architectures (see `_TEST_ARCH_JOB_RE`). Passing `arch` scopes the cell
    to that architecture's own jobs plus any arch-agnostic job, so one
    architecture's result can never roll up into another's variant.

    In a shared-run workflow (a build workflow that calls its test workflow via
    `workflow_call`), the notify job list carries BOTH the build sub-jobs and
    the nested test sub-jobs. `phase` (a classification `pipeline_phase`)
    partitions that list: "build" keeps only the build sub-jobs (dropping nested
    "Test | <arch>" jobs), "test"/"test-full" keep only the test sub-jobs.
    Without it a build leaf would absorb the test cells, doubling cells and
    letting a failed test flip the build status.
    """
    jobs = (
        workflow_run.api_jobs
        if workflow_run.api_jobs is not None
        else workflow_run.jobs
    )
    if phase == "build":
        jobs = [j for j in jobs if not _is_test_subjob(j.name)]
    elif phase in ("test", "test-full"):
        jobs = [j for j in jobs if _is_test_subjob(j.name)]
    if arch is not None:
        jobs = [j for j in jobs if _job_matches_arch(j.name, arch)]
    cells: dict[tuple[str, str], list[WorkflowJobRecord]] = {}
    order: list[tuple[str, str]] = []
    for j in jobs:
        # Take the *last* match, not the first: a nested job's composite name
        # is "ancestor segment(s) / ... / own segment", and the own segment
        # (closest to the actual job) is the authoritative (py, ref) -- e.g.
        # a build job's own tail carries the full ref, while an orchestrator
        # ancestor segment upstream of it may carry a shorter/looser one. A
        # job with no segment of its own (a nested test sub-job) falls back
        # to whichever ancestor segment matched.
        matches = list(_MATRIX_JOB_RE.finditer(j.name))
        if not matches:
            continue
        match = matches[-1]
        key = (match.group("py"), _normalize_ref(axis_key, match.group("ref")))
        if key not in cells:
            cells[key] = []
            order.append(key)
        cells[key].append(j)

    variants: list[Variant] = []
    for py, ref in order:
        group = cells[(py, ref)]
        status = rollup_statuses((_job_status(j) for j in group), Status.in_progress)
        starts = [j.started_at for j in group if j.started_at]
        started = (
            min(starts)
            if starts
            else (workflow_run.run_started_at or workflow_run.created_at)
        )
        all_terminal = all(j.conclusion for j in group)
        ends = [j.completed_at for j in group if j.completed_at]
        completed = max(ends) if all_terminal and ends else None
        variants.append(
            Variant(
                matrix={"py": py, axis_key: ref},
                run_id=workflow_run.workflow_run_id,
                run_attempt=workflow_run.run_attempt,
                status=status,
                started_at=_datetime_to_z(started) if started else None,
                completed_at=_datetime_to_z(completed) if completed else None,
            )
        )
    return variants


def _variants_from_inputs(
    workflow_run: WorkflowRunRecord, axis_key: str
) -> list[Variant]:
    """Single-cell variant from the dispatch inputs"""
    inputs = workflow_run.inputs or {}
    py = inputs.get("python_version")
    ref = next(
        (inputs[k] for k in _VARIANT_INPUT_KEYS[axis_key] if inputs.get(k)),
        None,
    )
    matrix: dict[str, str] = {}
    if py:
        matrix["py"] = str(py)
    if ref:
        matrix[axis_key] = _normalize_ref(axis_key, str(ref))
    if not matrix:
        return []

    started = workflow_run.run_started_at or workflow_run.created_at
    completed = (
        workflow_run.updated_at
        if workflow_run.conclusion and workflow_run.updated_at is not None
        else None
    )
    return [
        Variant(
            matrix=matrix,
            run_id=workflow_run.workflow_run_id,
            run_attempt=workflow_run.run_attempt,
            status=_run_status(workflow_run),
            started_at=_datetime_to_z(started) if started else None,
            completed_at=_datetime_to_z(completed) if completed else None,
        )
    ]


def _derive_variants(
    workflow_run: WorkflowRunRecord, *, arch: str | None = None
) -> list[Variant] | None:
    """Matrix-cell variants for fan-out pipelines (pytorch/jax py x ref).

    See `_variants_from_jobs` for what `arch` and `phase` scope. Workflows in
    `_SKIP_WORKFLOW_NAMES` never reach here: `update_status_json` returns
    before deriving a leaf for them at all.
    """
    cls = workflow_run.classification
    axis_key = _VARIANT_AXIS_KEY.get(cls.pipeline_type)
    if axis_key is None:
        return None
    variants = _variants_from_jobs(
        workflow_run, axis_key, arch=arch, phase=cls.pipeline_phase
    )
    if not variants:
        # Phase-blind fallback for single-cell test runs lacking the py|ref
        # axis. Safe for the build partition: it fires only before any build
        # job exists, when no test job exists yet (tests need builds), so the
        # whole-run status it reads cannot be test-influenced.
        variants = _variants_from_inputs(workflow_run, axis_key)
    return variants or None


def _create_leaf(
    workflow_run: WorkflowRunRecord, *, arch: str | None = None
) -> RunLeaf:
    """Map the enriched WorkflowRunRecord to a v2 RunLeaf.

    `arch`, when given, scopes matrix-cell variants (and the leaf's own
    rolled-up status) to that architecture -- see `_variants_from_jobs`.
    `arch` is only ever set when this run reports *multiple* architectures
    (see `_merge_run_into_document`), so `workflow_run`'s own conclusion is a
    whole-run aggregate across all of them, not this one arch's outcome.
    Unlike `_refresh_same_run_tests_from_build`'s single-arch case, it must not be
    folded into the rollup as a vote here: doing so would broadcast one
    shared status onto every architecture -- exactly the leakage `arch`
    scoping exists to prevent. It is used only as the fallback when this
    arch has no variants of its own to roll up.
    """
    ts_start = workflow_run.run_started_at or workflow_run.created_at
    started_at = _datetime_to_z(ts_start) if ts_start is not None else None

    completed_at: str | None = None
    if workflow_run.conclusion and workflow_run.updated_at is not None:
        completed_at = _datetime_to_z(workflow_run.updated_at)

    variants = _derive_variants(workflow_run, arch=arch)
    if arch is not None and variants:
        status = Variant.rollup_status(variants, Status.in_progress)
    else:
        status = _run_status(workflow_run)

    return RunLeaf(
        run_id=workflow_run.workflow_run_id,
        run_attempt=workflow_run.run_attempt,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        variants=variants,
    )


def _refresh_same_run_tests_from_build(
    doc: StatusDocument, workflow_run: WorkflowRunRecord, leaf: RunLeaf
) -> bool:
    """Refresh same-run test leaves from a completed fan-out workflow snapshot.

    PyTorch/JAX test coverage (`test_pytorch_wheels.yml` / `test_linux_jax_wheels.yml`)
    is invoked as a reusable `workflow_call` nested inside the delegated release
    workflow -- not dispatched as its own top-level run -- so its jobs land in
    the *same* run id, job list, and webhook notifications as the entry build.
    The registry classifies the whole run as
    `pipeline_type`/`pipeline_phase="build"` (see `WORKFLOW_SPECS`), but its job
    list carries both the build sub-jobs and the nested test sub-jobs;
    `_variants_from_jobs(..., phase="test")` keeps only the latter (jobs with a
    "Test | <arch>" segment of their own), so this projects the run's test half
    into per-arch test leaves keyed by the same run id -- while some cells are
    still in progress. The final notification is still classified as the build
    phase, so without this function those same-run test leaves would go stale
    once the build itself is done.

    Each matching test leaf is refreshed under three constraints, all guarding
    against this coarse build-run snapshot corrupting finer-grained state:

    - Per-architecture scope. One build run can nest test jobs for several
      architectures, so `leaf.variants` may roll every architecture's outcome
      together. Each existing test leaf's variants are re-derived scoped to its
      *own* architecture (`_variants_from_jobs(..., arch=arch)`) rather than
      broadcasting `leaf.variants` wholesale, so one architecture's result can
      never leak into another's.

    - Per-cell merge. Those re-derived variants come from the build run's job
      names, so a blind overwrite could clobber genuinely newer/more-complete
      per-cell results that already landed from the test leaf's own dedicated
      completion events. `merge_matrix_test_leaf` merges cell-by-cell, keeping
      the winning `Variant` per cell, instead of replacing `variants` wholesale.

    - Run-level status only when unambiguous. `leaf.status` is this run's own
      top-level GitHub conclusion, which is not necessarily the worst-of its
      variants (a nested test cell that failed/cancelled does not always flip
      the run's conclusion, and a cell can be missing entirely if its job never
      started). It is folded into the rollup only when the run reports a single
      architecture -- mirroring what `_merge_variant_leaf` does for the build
      leaf itself. Once several architectures share the run that conclusion is a
      whole-run aggregate, so folding it in would broadcast a failure anywhere
      -- even in an unrelated architecture -- onto every architecture, the very
      leakage this scoping exists to prevent.
    """
    cls = workflow_run.classification
    axis_key = _VARIANT_AXIS_KEY.get(cls.pipeline_type)
    if (
        cls.pipeline_phase != "build"
        or axis_key is None
        or not workflow_run.conclusion
        or not leaf.variants
        or leaf.run_id is None
    ):
        return False

    single_arch = len(_job_archs(workflow_run)) <= 1
    pipeline = getattr(doc.pipelines, cls.pipeline_type)
    wrote = False
    for phase_map in (pipeline.test, pipeline.test_full):
        for arch_map in phase_map.values():
            for arch, existing in arch_map.items():
                if existing.run_id != leaf.run_id:
                    continue
                if (existing.run_attempt or 0) != (leaf.run_attempt or 0):
                    continue
                arch_variants = _variants_from_jobs(
                    workflow_run, axis_key, arch=arch, phase="test"
                )
                if not arch_variants:
                    continue
                statuses = [v.status for v in arch_variants]
                if single_arch:
                    statuses.append(leaf.status)
                candidate = leaf.model_copy(
                    update={
                        # `statuses` is never empty here (guarded by the
                        # `if not arch_variants: continue` above), so this
                        # fallback can never fire; `leaf.status` only ever
                        # affects the result via the `single_arch` vote above,
                        # never as an unconditional broadcast to every arch.
                        "status": rollup_statuses(statuses, Status.in_progress),
                        "variants": arch_variants,
                    }
                )
                if not existing.should_replace(candidate):
                    continue
                merged = merge_matrix_test_leaf(existing, candidate)
                merged.status = candidate.status
                merged.completed_at = candidate.completed_at
                arch_map[arch] = merged
                wrote = True
    return wrote


def _refresh_same_run_build_from_test(
    doc: StatusDocument, workflow_run: WorkflowRunRecord
) -> bool:
    """Finalize a same-run build leaf from a nested test-phase snapshot (mirror
    of `_refresh_same_run_tests_from_build`).

    In the shared-run topology a pytorch/jax build workflow calls its test
    workflow via `workflow_call`, so the reusable test's own notify_quartz
    (reclassified to `pipeline_phase="test"` via `reporting_workflow`) carries a
    job list spanning the *whole* parent run -- the finished build sub-jobs
    included. The build-phase notify that finalizes the build leaf may not fire
    until run completion, leaving that leaf stuck `in_progress` while a test
    notify arrives mid-run with the build sub-jobs already terminal. This
    projects the build half of that snapshot onto the same-run build leaf so it
    finalizes early instead of waiting.

    Guarded to the shared-run case only: the build leaf itself carries no run id
    (its cells aggregate across per-cell runs, so `_merge_variant_leaf` drops
    it), but each of its variants does. At least one existing build cell must
    carry *this* run id, proving the test notify shares the run that produced
    those build cells. A standalone test dispatch
    (`test_pytorch_wheels_full.yml`) has its own run id, so no build cell
    matches and the real build run's leaf is left untouched. Build status is
    rolled up from the build sub-jobs alone (`phase="build"`), so a failed test
    cell can never flip the build leaf.
    """
    cls = workflow_run.classification
    axis_key = _VARIANT_AXIS_KEY.get(cls.pipeline_type)
    if cls.pipeline_phase not in ("test", "test-full") or axis_key is None:
        return False

    pipeline = getattr(doc.pipelines, cls.pipeline_type)
    existing = pipeline.build.get(cls.platform)
    run_id = workflow_run.workflow_run_id
    if existing is None or not existing.variants:
        return False
    if not any(v.run_id == run_id for v in existing.variants):
        return False

    build_variants = _variants_from_jobs(workflow_run, axis_key, phase="build")
    if not build_variants:
        return False

    status = Variant.rollup_status(build_variants, Status.in_progress)
    starts = [v.started_at for v in build_variants if v.started_at]
    ends = [v.completed_at for v in build_variants if v.completed_at]
    candidate = RunLeaf(
        run_id=workflow_run.workflow_run_id,
        run_attempt=workflow_run.run_attempt,
        status=status,
        started_at=min(starts) if starts else existing.started_at,
        completed_at=(max(ends) if status.is_terminal and ends else None),
        variants=build_variants,
    )
    if not existing.should_replace(candidate):
        return False
    pipeline.build[cls.platform] = merge_matrix_test_leaf(existing, candidate)
    return True


def _rocm_build_run_id(doc: StatusDocument, platform: str) -> int | None:
    """Run id of the winning `rocm.build` leaf for `platform` (the run that owns
    the platform's artifact URL block), or None if no build has landed yet."""
    leaf = doc.pipelines.rocm.build.get(platform)
    return leaf.run_id if leaf is not None else None


def _update_platform_urls(
    doc: StatusDocument,
    workflow_run: WorkflowRunRecord,
    *,
    leaf_accepted: bool,
    prev_owner: int | None,
) -> None:
    """Merge this run's artifact URLs into the platform-level url block, gated
    and pinned so the block stays consistent to a single source run.
    """
    if not leaf_accepted:
        return
    cls = workflow_run.classification
    platform = cls.platform
    if platform not in ("linux", "windows"):
        return
    urls = doc.linux_urls if platform == "linux" else doc.windows_urls
    rid = workflow_run.workflow_run_id
    is_build = cls.pipeline_type == "rocm" and cls.pipeline_phase == "build"

    if is_build:
        if prev_owner is not None and rid is not None and rid != prev_owner:
            urls.clear()
    else:
        # Non-build (native packages): only fill rpm/deb for the run that owns
        # the block; a mismatch means this event belongs to a superseded run.
        owner = _rocm_build_run_id(doc, platform)
        if owner is not None and rid is not None and rid != owner:
            return

    if workflow_run.tarball_url:
        urls["tarballs"] = workflow_run.tarball_url
    if workflow_run.wheels_url:
        urls["wheels"] = workflow_run.wheels_url
    if workflow_run.artifacts_url:
        urls["artifacts"] = workflow_run.artifacts_url
    if workflow_run.rpm_urls:
        urls["rpm"] = next(iter(workflow_run.rpm_urls.values()))
    if workflow_run.deb_urls:
        urls["deb"] = next(iter(workflow_run.deb_urls.values()))


def _update_release_cdn_urls(
    doc: StatusDocument, workflow_run: WorkflowRunRecord
) -> None:
    """Apply CDN URLs from a completed per-platform release orchestrator.

    These orchestrators carry publish-bucket status, not a pipeline leaf. Once
    classification has derived CDN URLs, they replace the per-run S3 URL block
    without changing document completion.
    """
    platform = workflow_run.classification.platform
    if platform not in ("linux", "windows"):
        return
    urls = doc.linux_urls if platform == "linux" else doc.windows_urls
    if workflow_run.tarball_url:
        urls["tarballs"] = workflow_run.tarball_url
    if workflow_run.wheels_url:
        urls["wheels"] = workflow_run.wheels_url
    if workflow_run.rpm_urls:
        urls["rpm"] = next(iter(workflow_run.rpm_urls.values()))
    if workflow_run.deb_urls:
        urls["deb"] = next(iter(workflow_run.deb_urls.values()))


def _record_orchestrator_owner(
    doc: StatusDocument, workflow_run: WorkflowRunRecord
) -> bool:
    """Record the top-level orchestrator run as the document owner.

    Ownership is the pair (workflow_run_id, run_attempt). A GitHub re-run
    keeps the same run id but bumps the attempt, so both must be compared:
      - newer run id               -> full reset, the release was re-dispatched;
      - same run id, newer attempt -> finalization-only reset, a re-run of the
        same release (partial re-runs only re-report their failed leaves, so the
        passing leaves must survive);
      - older run id, or same run id with an older attempt -> rejected, so a
        late attempt-1 completion never overwrites attempt 2.
    """
    rid = workflow_run.workflow_run_id
    attempt = workflow_run.run_attempt
    owner_rid = doc.trigger_workflow_run_id
    owner_attempt = doc.trigger_run_attempt or 0
    if owner_rid not in (None, 0) and rid is not None:
        if rid < owner_rid or (rid == owner_rid and attempt < owner_attempt):
            log.info(
                "ignoring event from superseded orchestrator run_id=%s "
                "attempt=%s; document is owned by run_id=%s attempt=%s",
                rid,
                attempt,
                owner_rid,
                owner_attempt,
            )
            return False
    if rid is not None:
        if owner_rid not in (None, 0, rid) and rid > owner_rid:
            _reset_document_for_new_owner(doc)
        elif rid == owner_rid and attempt > owner_attempt:
            _reset_finalization_for_rerun(doc)
        doc.trigger_workflow_run_id = rid
        doc.trigger_run_attempt = attempt
    return True


def _reset_document_for_new_owner(doc: StatusDocument) -> None:
    """Clear run-owned detail when a newer top-level orchestrator takes over."""
    doc.completed_at = None
    doc.orchestrator_conclusion = None
    doc.created_at = None
    doc.linux_architectures.clear()
    doc.windows_architectures.clear()
    doc.linux_urls.clear()
    doc.windows_urls.clear()
    doc.pipelines = Pipelines()
    rebuild_summary(doc)


def _reset_finalization_for_rerun(doc: StatusDocument) -> None:
    """Re-open a finalized document when the same run re-runs at a higher attempt.

    A re-run keeps the run id but bumps the attempt. Partial re-runs only
    re-report their failed leaves, so the pipeline tree, urls and architectures
    are kept: the failed leaves get overwritten as the re-run reports them
    (RunLeaf.should_replace prefers the higher attempt), while the passing
    leaves survive. Only the document-level finalized state is cleared so the
    release drops back to `in_progress` until the re-run finalizes.
    """
    doc.completed_at = None
    doc.orchestrator_conclusion = None
    rebuild_summary(doc)


def _is_ownerless_pytorch_leaf_for_release(
    doc: StatusDocument, workflow_run: WorkflowRunRecord
) -> bool:
    """True for a pytorch leaf with no derivable owner that pins to this release.

    pytorch test runs carry only the rocm version (inside the torch version), no
    artifact_run_id or parent to derive an owner from. asan does not run pytorch,
    so a pytorch leaf whose release version matches the document can only be a
    legit normal/prerelease run. Restricting to `pipeline_type == "pytorch"`
    keeps this the sole ownerless-acceptance path.
    """
    if workflow_run.classification.pipeline_type != "pytorch":
        return False
    release_version = workflow_run.classification.release_version
    return bool(release_version) and release_version == doc.rocm_version


def _gate_to_document_owner(
    doc: StatusDocument, workflow_run: WorkflowRunRecord
) -> bool:
    """Allow only leaf updates that belong to the document's owning run.

    Strict by design: a leaf never establishes, supersedes, or takes over
    ownership. Ownership is established only on the owner-writer path
    (`_record_orchestrator_owner`, driven by the setup run and the top-level
    orchestrator). This keeps a second, later release run (notably an asan run,
    whose run id is higher) from hijacking the normal release document.
    """
    owner = doc.trigger_workflow_run_id
    parent = workflow_run.trigger_workflow_run_id

    if owner in (None, 0):
        log.info(
            "skipping workflow_run_id=%s: status.json has no owner yet; only a "
            "setup or top-level orchestrator run may establish ownership",
            workflow_run.workflow_run_id,
        )
        return False

    # `workflow_call` children share the owner's run id but carry no derived
    # parent; accept them when their own run id is the owner.
    if parent in (None, 0):
        if workflow_run.workflow_run_id == owner:
            return True
        # A pytorch leaf carries only the rocm version and cannot prove ownership
        # by run id; admit it when its version pins to this release. A mismatched
        # version, or any non-pytorch ownerless leaf, is still refused.
        if _is_ownerless_pytorch_leaf_for_release(doc, workflow_run):
            return True
        log.info(
            "skipping workflow_run_id=%s with no derived owner run; status.json "
            "is owned by trigger_workflow_run_id=%s",
            workflow_run.workflow_run_id,
            owner,
        )
        return False

    if parent != owner:
        log.info(
            "skipping workflow_run_id=%s from trigger_workflow_run_id=%s; "
            "status.json is owned by trigger_workflow_run_id=%s",
            workflow_run.workflow_run_id,
            parent,
            owner,
        )
        return False

    return True


def _finalize_orchestrator(
    doc: StatusDocument, workflow_run: WorkflowRunRecord
) -> bool:
    """Record the orchestrator's completed signal on the document."""
    if not _record_orchestrator_owner(doc, workflow_run):
        return False

    ts = workflow_run.updated_at
    doc.completed_at = (
        _datetime_to_z(ts) if ts is not None else doc.status_json_last_updated
    )
    # Remember the orchestrator's own conclusion so a cancelled/failed release
    # is not rendered `success` by rebuild_summary just because its reported
    # leaves passed (see StatusDocument.orchestrator_conclusion).
    doc.orchestrator_conclusion = _run_status(workflow_run)
    rebuild_summary(doc)
    return True


def _merge_run_into_document(
    doc: StatusDocument,
    workflow_run: WorkflowRunRecord,
    leaf: RunLeaf,
) -> None:
    """Place the run's leaf in the pipeline tree, freeze archs, recompute summary."""
    cls = workflow_run.classification
    # Capture the run that owns the URL block *before* the upsert, so
    # `_update_platform_urls` can tell whether this event's build supersedes it.
    prev_url_owner = _rocm_build_run_id(doc, cls.platform)
    targets = (
        list(cls.architectures) if cls.pipeline_phase in ("test", "test-full") else [""]
    )

    # A single event reporting more than one architecture (multi-arch test
    # dispatch) would otherwise upsert the *same* leaf object -- variants
    # derived from the run's full, arch-blind job list -- into every target
    # arch's slot. Re-derive a leaf scoped to each arch so one architecture's
    # result can never be attributed to another's.
    multi_arch = len(targets) > 1 and cls.pipeline_type in _VARIANT_AXIS_KEY

    leaf_accepted = False
    for arch in targets:
        arch_leaf = _create_leaf(workflow_run, arch=arch) if multi_arch else leaf
        wrote = doc.upsert_leaf(
            platform=cls.platform,
            arch=arch,
            pipeline_type=cls.pipeline_type,
            pipeline_phase=cls.pipeline_phase,
            leaf=arch_leaf,
        )
        leaf_accepted = leaf_accepted or wrote
        if not wrote:
            log.info(
                "skipped leaf upsert (do-not-downgrade guard): %s/%s/%s.%s "
                "run_id=%s run_attempt=%s status=%s",
                cls.platform,
                arch or "-",
                cls.pipeline_type,
                cls.pipeline_phase,
                workflow_run.workflow_run_id,
                workflow_run.run_attempt,
                arch_leaf.status,
            )

    # URLs are updated only after the leaf upsert, gated on acceptance: a stale
    # event that lost the guard must not clobber the block.
    _update_platform_urls(
        doc, workflow_run, leaf_accepted=leaf_accepted, prev_owner=prev_url_owner
    )
    refreshed_tests = _refresh_same_run_tests_from_build(doc, workflow_run, leaf)
    refreshed_build = _refresh_same_run_build_from_test(doc, workflow_run)

    freeze_requested_architectures(
        doc,
        platform=cls.platform,
        pipeline_type=cls.pipeline_type,
        pipeline_phase=cls.pipeline_phase,
        architectures=cls.architectures,
    )
    if refreshed_tests:
        log.info(
            "refreshed same-run %s test leaves from completed fan-out run_id=%s",
            cls.pipeline_type,
            workflow_run.workflow_run_id,
        )
    if refreshed_build:
        log.info(
            "finalized same-run %s build leaf from nested test snapshot run_id=%s",
            cls.pipeline_type,
            workflow_run.workflow_run_id,
        )
    rebuild_summary(doc)


def _update_symlinks(
    repo_dir: Path,
    doc: StatusDocument,
    status_path: Path,
    release_type: str,
) -> list[Path]:
    """Update `latest.json` (symlink) and `latest_good.json` (snapshot file).
    Only meaningful for the `nightly` release type."""
    if release_type == "prerelease":
        return _update_prerelease_latest(repo_dir, status_path)
    if release_type != "nightly":
        return []

    latest_dir = repo_dir / "release-nightly"
    new_target_relative = status_path.relative_to(latest_dir)
    new_date = new_target_relative.parts[0]

    files_written: list[Path] = []

    latest = latest_dir / "latest.json"
    if latest.is_symlink():
        try:
            existing_date = latest.readlink().parts[0]
            if existing_date > new_date:
                latest = None  # newer date already pointed at; do not regress
        except (IndexError, OSError):
            pass
    if latest is not None:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(new_target_relative)
        files_written.append(latest)

    if doc.summary.overall_status == Status.success:
        latest_good = latest_dir / "latest_good.json"
        if _latest_good_should_update(latest_good, new_date):
            if latest_good.is_symlink() or latest_good.exists():
                latest_good.unlink()
            latest_good.write_text(doc.to_json() + "\n", encoding="utf-8")
            files_written.append(latest_good)

    return files_written


def _update_prerelease_latest(repo_dir: Path, status_path: Path) -> list[Path]:
    """Point `prereleases/latest.json` at the newest release candidate."""
    prerelease_root = repo_dir / "prereleases"
    new_target_relative = status_path.relative_to(prerelease_root)
    new_version = new_target_relative.parent.name

    latest = prerelease_root / "latest.json"
    if latest.is_symlink():
        try:
            existing_version = latest.readlink().parent.name
            if _prerelease_version_key(existing_version) > _prerelease_version_key(
                new_version
            ):
                return []  # newer candidate already pointed at; do not regress
        except (IndexError, OSError):
            pass

    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(new_target_relative)
    return [latest]


def _latest_good_should_update(latest_good: Path, new_build_date: str) -> bool:
    """True unless an existing `latest_good.json` snapshots a newer build.

    Migrates legacy symlink installs implicitly: a stale symlink is treated
    as "no existing snapshot" and gets replaced on the next successful write.
    """
    if not latest_good.exists():
        return True
    if latest_good.is_symlink():
        return True
    try:
        existing = json.loads(latest_good.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    existing_date = str(existing.get("build_date") or "")
    return existing_date <= new_build_date


class _PushOutcome(Enum):
    """Result of a single commit+push attempt.

    DONE  -- pushed (or a legitimate no-op); the update is complete.
    RETRY -- lost the push race / transient git failure; rebuild and try again.
    FATAL -- non-retryable failure (auth, protected branch, bad ref); aborting
             now instead of burning the whole retry budget on something a retry
             cannot fix.
    """

    DONE = "done"
    RETRY = "retry"
    FATAL = "fatal"


# Substrings in `git` stderr that mean "someone else moved the ref, the network
# hiccuped, or a stale lock is in the way" -- i.e. retrying against fresh
# upstream (after clearing stale locks) can win. Anything else (auth,
# protected-branch hook, bad refspec) is fatal: retrying just wastes the backoff
# budget. Matched case-insensitively.
_RETRYABLE_GIT_STDERR: tuple[str, ...] = (
    "fetch first",
    "non-fast-forward",
    "cannot lock ref",
    "failed to lock",
    "the remote end hung up",
    "rpc failed",
    "could not read from remote",
    "could not resolve host",
    "connection timed out",
    "connection reset",
    "operation timed out",
    "unable to access",
    "early eof",
    # stale-lock wedge: cleared at the top of the next attempt (see
    # `_clear_stale_git_locks`), so it is worth retrying rather than aborting.
    "index.lock",
    "another git process seems to be running",
    # GitHub backend errors on the server side of a push: ref-update and
    # pack-unpack failures. Surface as "remote: fatal error in commit_refs" or
    # "remote unpack failed"; despite "fatal", a retry against fresh upstream wins.
    "commit_refs",
    "unpack failed",
    "unpacker error",
    # GitHub gateway/backend 5xx not already wrapped in "rpc failed".
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    # transient TLS handshake glitch.
    "gnutls_handshake",
    "ssl_read",
)


def _git_stderr_is_retryable(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _RETRYABLE_GIT_STDERR)


def _classify_git_failure(
    op: str, result: subprocess.CompletedProcess[str]
) -> _PushOutcome:
    """Map a failed git invocation to RETRY (lost race / transient / stale lock)
    or FATAL (auth, protected branch, bad ref)."""
    if _git_stderr_is_retryable(result.stderr):
        log.warning(
            "%s failed (exit %s); will retry. stderr: %s",
            op,
            result.returncode,
            result.stderr.strip(),
        )
        return _PushOutcome.RETRY
    log.error(
        "%s failed with a non-retryable error (exit %s); aborting without "
        "exhausting the retry budget. stderr: %s",
        op,
        result.returncode,
        result.stderr.strip(),
    )
    return _PushOutcome.FATAL


# Top-level `.git` lockfiles a wedged git can leave behind. Per-ref lockfiles
# under `.git/refs/` are swept separately (they nest arbitrarily).
_GIT_TOP_LEVEL_LOCKS: tuple[str, ...] = ("index.lock", "shallow.lock", "HEAD.lock")


def _clear_stale_git_locks(
    repo_dir: Path, max_age_s: float = STALE_LOCK_AGE_SEC
) -> None:
    """Remove `.git` lockfiles older than `max_age_s` (left by a killed git).

    A lock younger than the threshold is left in place -- the caller backs off
    and retries -- so a (theoretical) live concurrent git is never raced.
    """
    git_dir = repo_dir / ".git"
    if not git_dir.is_dir():
        # A `.git` file (worktree/submodule) or a bare/absent repo: nothing to
        # sweep here, and resolving the real gitdir is not worth the complexity.
        return
    now = time.time()
    candidates = [git_dir / name for name in _GIT_TOP_LEVEL_LOCKS]
    refs_dir = git_dir / "refs"
    if refs_dir.is_dir():
        candidates += list(refs_dir.rglob("*.lock"))
    for lock in candidates:
        try:
            if not lock.is_file():
                continue
            age = now - lock.stat().st_mtime
            if age < max_age_s:
                log.warning(
                    "git lock %s is recent (%.0fs old); leaving it and backing "
                    "off in case a git process is still using it",
                    lock,
                    age,
                )
                continue
            lock.unlink()
            log.warning("removed stale git lock %s (%.0fs old)", lock, age)
        except OSError as e:
            log.warning("could not inspect/remove git lock %s: %s", lock, e)


def _sync_to_upstream(repo_dir: Path) -> bool:
    """Fetch and hard-reset onto the upstream head. Returns False on a
    (typically transient/network) failure so the caller retries rather than
    aborting -- `fetch` is the most network-dependent op in the loop and must
    not crash the whole update on a blip.
    """
    fetch = _git(["fetch", "origin"], cwd=repo_dir, check=False)
    if fetch.returncode != 0:
        log.warning(
            "git fetch origin failed (exit %s); will retry. stderr: %s",
            fetch.returncode,
            fetch.stderr.strip(),
        )
        return False
    reset = _git(["reset", "--hard", "@{u}"], cwd=repo_dir, check=False)
    if reset.returncode != 0:
        log.warning(
            "git reset --hard @{u} failed (exit %s); will retry. stderr: %s",
            reset.returncode,
            reset.stderr.strip(),
        )
        return False
    return True


def _commit_and_push(repo_dir: Path, files: list[Path], message: str) -> _PushOutcome:
    if not files:
        log.info("no status.json changes to commit; skipping gated no-op update")
        return _PushOutcome.DONE

    # add / commit run with check=False so a stale-lock wedge (or any transient
    # git failure) is classified into RETRY instead of raising out of the loop.
    add = _git(["add"] + [str(f) for f in files], cwd=repo_dir, check=False)
    if add.returncode != 0:
        return _classify_git_failure("git add", add)
    # A no-op is a legitimate outcome, not a failure: a late `in_progress`
    # notification arriving after the entry already advanced leaves the rewritten
    # file byte-identical, so nothing is staged. `git commit` would exit 1
    # ("nothing to commit"); short-circuit to success instead of aborting.
    if _git(["diff", "--cached", "--quiet"], cwd=repo_dir, check=False).returncode == 0:
        log.info("no status.json changes to commit; skipping (no-op update)")
        return _PushOutcome.DONE
    commit = _git(["commit", "-m", message], cwd=repo_dir, check=False)
    if commit.returncode != 0:
        return _classify_git_failure("git commit", commit)
    push = _git(["push"], cwd=repo_dir, check=False)
    if push.returncode == 0:
        return _PushOutcome.DONE
    return _classify_git_failure("git push", push)


def _commit_message(
    doc: StatusDocument,
    workflow_run: WorkflowRunRecord,
    status_rel_path: Path,
    file_created: bool,
) -> str:
    created_or_update = "create" if file_created else "update"
    overall_status = doc.summary.overall_status

    cls = workflow_run.classification
    if cls.pipeline_type:
        archs = ",".join(cls.architectures) if cls.architectures else "unknown"
        trigger = (
            f" (trigger run: {doc.trigger_workflow_run_id})"
            if doc.trigger_workflow_run_id
            else ""
        )
        return (
            f"{created_or_update} {status_rel_path} - {cls.platform}/{archs}"
            f" run: {workflow_run.workflow_run_id}"
            f" - {cls.pipeline_type}.{cls.pipeline_phase}"
            f" -> {overall_status}{trigger}"
        )

    trigger = (
        f" - trigger run: {doc.trigger_workflow_run_id}"
        if doc.trigger_workflow_run_id
        else ""
    )
    return f"{created_or_update} {status_rel_path}{trigger} -> {overall_status}"


def _build_and_write(
    workflow_run: WorkflowRunRecord,
    status_path: Path,
    repo_dir: Path,
    release_type: str,
    finalize: bool = False,
    record_owner_only: bool = False,
    update_platform_urls_only: bool = False,
) -> tuple[StatusDocument, bool, list[Path], str]:
    """Read or create the status.json on disk, apply the run, and persist it.

    Modes (mutually exclusive):
      - finalize:                  stamp the orchestrator's completed signal.
      - record_owner_only:         only record the owning orchestrator run id
                                   (the orchestrator's start event, before it
                                   finalizes).
      - update_platform_urls_only: apply CDN URLs from a completed per-platform
                                   release orchestrator.
      - default:                   upsert the run's leaf into the pipeline tree.
    """
    status_path.parent.mkdir(parents=True, exist_ok=True)

    now_dt = _utc_now()
    _validate_clock(now_dt, workflow_run)
    now = _datetime_to_z(now_dt)

    doc, file_created = _load_or_create_document(
        status_path, workflow_run, release_type, now
    )

    applied = False
    if finalize:
        if _finalize_orchestrator(doc, workflow_run):
            _update_document_metadata(doc, workflow_run, now)
            applied = True
    elif record_owner_only:
        if _record_orchestrator_owner(doc, workflow_run):
            _update_document_metadata(doc, workflow_run, now)
            applied = True
    elif update_platform_urls_only:
        if _gate_to_document_owner(doc, workflow_run):
            _update_document_metadata(doc, workflow_run, now)
            _update_release_cdn_urls(doc, workflow_run)
            rebuild_summary(doc)
            applied = True
    else:
        if _gate_to_document_owner(doc, workflow_run):
            _update_document_metadata(doc, workflow_run, now)
            leaf = _create_leaf(workflow_run)
            _merge_run_into_document(doc, workflow_run, leaf)
            applied = True

    if not applied:
        return doc, file_created, [], ""

    commit_message = _commit_message(
        doc, workflow_run, status_path.relative_to(repo_dir), file_created
    )
    status_path.write_text(doc.to_json() + "\n", encoding="utf-8")

    files_to_commit = [status_path]
    files_to_commit += _update_symlinks(repo_dir, doc, status_path, release_type)

    return doc, file_created, files_to_commit, commit_message


_TRACKED_EVENT_TYPES: frozenset[str] = frozenset(
    {"workflow_run_in_progress", "workflow_run_completed"}
)

_TRACKED_RELEASE_TYPES: frozenset[str] = frozenset({"nightly", "prerelease"})

# status.json is only produced for the release-tracking repository; runs from
# anywhere else (TheRock itself, forks) never touch it. Matched case-insensitively.
# rocm/quartz-tester-rockrel is the e2e rockrel placeholder (release-tracking repo).
_TRACKED_REPOSITORIES: frozenset[str] = frozenset(
    {"rocm/rockrel", "rocm/quartz-tester-rockrel"}
)


def _is_release_cdn_url_update(
    payload: TheRockDispatchEvent,
    workflow_run: WorkflowRunRecord,
) -> bool:
    """A completed per-platform release orchestrator with CDN URLs to persist."""
    c = workflow_run.classification
    return (
        payload.event_type == "workflow_run_completed"
        and c.pipeline_type == "orchestrator"
        and c.platform in ("linux", "windows")
        and c.pipeline_phase in RELEASE_CDN_PHASES
        and bool(
            workflow_run.tarball_url
            or workflow_run.wheels_url
            or workflow_run.rpm_urls
            or workflow_run.deb_urls
        )
    )


def _is_release_completion(
    payload: TheRockDispatchEvent,
    workflow_run: WorkflowRunRecord,
) -> bool | None:
    """Whether this event finalizes the document.

    Routes off the classification tuple (see `ORCHESTRATOR_SPECS`):
      - non-orchestrator leaf                     -> False (upsert its own run)
      - per-platform orchestrator (linux/windows) -> None  (no document signal)
      - top-level orchestrator, non-finalizing    -> None  (e.g. python-packages)
      - top-level finalizing orchestrator          -> True on completion
    """
    c = workflow_run.classification
    if c.pipeline_type != "orchestrator":
        return False

    if c.platform != "":
        log.info(
            "per-platform release orchestrator (phase=%s) carries no "
            "document-level completion signal; skipping status.json update",
            c.pipeline_phase,
        )
        return None

    if c.pipeline_phase not in FINALIZING_PHASES:
        log.info(
            "orchestrator phase=%s carries no document-level completion "
            "signal; skipping status.json update",
            c.pipeline_phase,
        )
        return None

    if payload.event_type != "workflow_run_completed":
        log.info(
            "top-level release orchestrator (phase=%s) is not completed yet "
            "(event_type=%s); nothing to finalize",
            c.pipeline_phase,
            payload.event_type,
        )
        return None

    return True


def update_status_json(
    payload: TheRockDispatchEvent,
    repo_dir: Path,
    commit_and_push: bool = True,
) -> Path | None:
    """Create or update status.json from a dispatch payload, if it qualifies."""
    if payload.event_type not in _TRACKED_EVENT_TYPES:
        log.info(
            "event_type=%s is not a tracked lifecycle event (%s); skipping update",
            payload.event_type,
            sorted(_TRACKED_EVENT_TYPES),
        )
        return None

    if payload.repository.lower() not in _TRACKED_REPOSITORIES:
        log.info(
            "repository=%r is not a release-tracking repo (%s); "
            "skipping status.json update",
            payload.repository,
            sorted(_TRACKED_REPOSITORIES),
        )
        return None

    workflow_run = payload.workflow_run
    if workflow_run is None:
        log.info("no workflow_run on payload; skipping status.json update")
        return None

    release_type = workflow_run.release_type or ""
    if release_type not in _TRACKED_RELEASE_TYPES:
        log.info(
            "release_type=%r is not a tracked tier (%s); skipping status.json update",
            release_type,
            sorted(_TRACKED_RELEASE_TYPES),
        )
        return None

    finalize = None
    record_owner_only = False
    update_platform_urls_only = False

    c = workflow_run.classification
    if c.pipeline_type == "setup":
        # The setup run executes via `workflow_call`, so it shares the top-level
        # orchestrator's run id and can anchor document ownership before any leaf
        # arrives (owner writer: `_record_orchestrator_owner`). Only the normal
        # `release` setup may own the normal release document. The top-level
        # orchestrators pass build_variant verbatim to setup_multi_arch.yml
        # (multi_arch_release.yml -> "release", multi_arch_release_asan.yml ->
        # "asan"), and sanitizer variants (asan, host-asan, tsan) get their own
        # status.json file later. Gate positively on "release" so anything that
        # is not provably the normal release is refused rather than fail-open.
        if c.build_variant != "release":
            log.info(
                "setup run build_variant=%r is not the normal release; must not "
                "own the normal release document (workflow_run_id=%s); skipping "
                "status.json update",
                c.build_variant,
                workflow_run.workflow_run_id,
            )
            return None
        record_owner_only = True
    else:
        finalize = _is_release_completion(payload, workflow_run)
        if finalize is None:
            if _is_release_cdn_url_update(payload, workflow_run):
                update_platform_urls_only = True
            elif (
                payload.event_type == "workflow_run_in_progress"
                and is_top_level_orchestrator(workflow_run)
            ):
                record_owner_only = True
            else:
                return None

    if finalize or record_owner_only or update_platform_urls_only:
        if not workflow_run.classification.release_version:
            log.info(
                "release orchestrator event but release_version is "
                "unresolved (workflow_run_id=%s); cannot route to a status.json, "
                "skipping",
                workflow_run.workflow_run_id,
            )
            return None
    else:
        # Native package install tests are async sanity checks with no per-arch
        # payload; status.json has no native_packages test slot, so skip them.
        if c.pipeline_type == "native_packages" and c.pipeline_phase == "test":
            log.info(
                "native package install test is an async sanity check not tracked "
                "in status.json (workflow_run_id=%s); skipping",
                workflow_run.workflow_run_id,
            )
            return None

        # See `_SKIP_WORKFLOW_NAMES` for why these workflows are disregarded.
        workflow_name = PurePosixPath(workflow_run.path).name
        if workflow_name in _SKIP_WORKFLOW_NAMES:
            log.info(
                "workflow=%r completion is disregarded (in _SKIP_WORKFLOW_NAMES; "
                "workflow_run_id=%s); skipping",
                workflow_name,
                workflow_run.workflow_run_id,
            )
            return None

        if not c.platform or not c.pipeline_type:
            log.info(
                "workflow_run did not classify to a release pipeline "
                "(platform=%r pipeline_type=%r); skipping status.json update",
                c.platform,
                c.pipeline_type,
            )
            return None

        if not workflow_run.classification.architectures:
            raise ValueError(
                f"workflow_run_id={workflow_run.workflow_run_id} has no "
                "architectures after classification. Refusing to write status.json "
                "with no per-arch payload. Check the producer's `amdgpu_families` "
                "input or extend the architecture-extraction regex."
            )

    status_path = _status_json_path(repo_dir, release_type, workflow_run)

    if not commit_and_push:
        doc, _file_created, _files, _msg = _build_and_write(
            workflow_run,
            status_path,
            repo_dir,
            release_type,
            finalize=bool(finalize),
            record_owner_only=record_owner_only,
            update_platform_urls_only=update_platform_urls_only,
        )
        log.info(
            "status.json written (commit_and_push=False): %s",
            status_path.relative_to(repo_dir),
        )
        log.debug("%s", doc.to_json())
        return status_path

    doc: StatusDocument | None = None
    max_retries = _max_retries()
    # Spread simultaneously-started runs so they do not all contend the ref at
    # once on the first attempt.
    time.sleep(random.uniform(0, INITIAL_JITTER_SEC))
    for attempt in range(max_retries):
        if attempt > 0:
            # Exponential backoff with full jitter, capped at BACKOFF_CAP_SEC.
            ceiling = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
            backoff = random.uniform(0, ceiling)
            log.warning(
                "Update attempt %s/%s failed (lost race or transient git "
                "error), retrying in %.1fs...",
                attempt,
                max_retries - 1,
                backoff,
            )
            time.sleep(backoff)

        # Clear any stale `.git/*.lock` left by a killed prior git before
        # touching the index/refs, so a wedged lock does not fail every attempt
        # identically.
        _clear_stale_git_locks(repo_dir)

        # Always recompute against fresh upstream: fetch, then hard-reset onto
        # the upstream head. On a retry this drops the local commit that lost
        # the push race; on the first attempt it just lands us on the latest
        # remote state. Either way each attempt starts from a clean upstream
        # tree, so the status.json we build reflects whatever already landed. A
        # transient fetch/reset failure is retried like any other lost attempt
        # rather than aborting the whole update.
        if not _sync_to_upstream(repo_dir):
            continue

        doc, _file_created, files_to_commit, commit_message = _build_and_write(
            workflow_run,
            status_path,
            repo_dir,
            release_type,
            finalize=bool(finalize),
            record_owner_only=record_owner_only,
            update_platform_urls_only=update_platform_urls_only,
        )

        outcome = _commit_and_push(repo_dir, files_to_commit, commit_message)
        if outcome is _PushOutcome.DONE:
            log.info(
                "status.json updated: %s (attempt %s/%s)",
                status_path.relative_to(repo_dir),
                attempt + 1,
                max_retries,
            )
            log.debug("%s", doc.to_json())
            return status_path
        if outcome is _PushOutcome.FATAL:
            cls = workflow_run.classification
            if doc:
                log.error("Final status.json content:\n%s", doc.to_json())
            raise RuntimeError(
                "Failed to push status.json: git push reported a non-retryable "
                f"error (workflow_run_id={workflow_run.workflow_run_id}, "
                f"{cls.platform}/{cls.pipeline_type}.{cls.pipeline_phase}). "
                "See the git push stderr above."
            )
        # _PushOutcome.RETRY: lost the race; loop and rebuild against upstream.

    log.error(
        "Failed to push after %s attempts. Final status.json content:", max_retries
    )
    if doc:
        log.error("%s", doc.to_json())
    else:
        log.error("No status.json content generated.")
    cls = workflow_run.classification
    raise RuntimeError(
        f"Failed to push status.json after {max_retries} attempts "
        f"(workflow_run_id={workflow_run.workflow_run_id}, "
        f"{cls.platform}/{cls.pipeline_type}."
        f"{cls.pipeline_phase})."
    )
