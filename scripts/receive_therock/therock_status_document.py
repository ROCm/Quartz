#!/usr/bin/env python
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Typed in-memory model for status.json v2 (pipeline-first layout).

Tree layout (a leaf's depth tracks the workflow's real granularity;
see docs/status_json/status_json_reference.jsonc for a full example):

  StatusDocument
  ├── <metadata>                      schema_version, rocm_version, build_date, ...
  ├── summary                         typed rollup (rebuilt by therock_summary)
  │   ├── overall_status
  │   └── <linux|windows>             -> PlatformSummary (always both)
  │       ├── status
  │       ├── architectures
  │       ├── urls
  │       ├── <rocm|pytorch|jax>      -> build {status} + test {status counts}
  │       └── native_packages
  │           └── <rpm|deb>           -> {status}
  └── pipelines                       detail tree (typed; absent slots -> null)
      ├── <rocm|pytorch|jax>          one Pipeline each
      │   ├── build
      │   │   └── <platform>          -> RunLeaf   (one per platform)
      │   └── test
      │       └── <platform>
      │           └── <arch>          -> RunLeaf   (one per platform,arch)
      └── native_packages
          └── <rpm|deb>               -> RunLeaf   (one per package type)

Matrixed pipelines (pytorch py x torch) attach a `variants` list to the
leaf, one entry per matrix cell. Build leaves may aggregate variants from
multiple workflow runs.

The models are Pydantic v2 `BaseModel`s: incoming status.json is validated on
load (`StatusDocument.from_dict`) and `Status` constrains the status vocabulary.
`Variant` and `RunLeaf` keep their on-disk JSON shape via custom serializers --
a variant's matrix axes are merged flat at the top level. The on-disk
projection (`to_dict`/`to_json`) then drops every `null`-valued key, so unset
state fields and absent pipelines simply do not appear.

Classes:
  Status          -- the closed status/conclusion vocabulary
  ReleaseType     -- the closed release-tier vocabulary (nightly/rc/dev)
  Variant         -- one matrix cell of a fan-out pipeline
  RunLeaf         -- one workflow run, or one aggregate matrix leaf
  Pipeline        -- one pipeline's {build, test} subtree
  NativePackages  -- the native-packages pipeline ({rpm, deb})
  Pipelines       -- the full typed detail tree
  Summary         -- read-optimized per-platform rollup (rebuilt from the tree)
  StatusDocument  -- root document; metadata + per-platform rollup inputs + pipeline tree
"""

import json
from collections.abc import Iterable
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, Field, model_serializer, model_validator

SCHEMA_VERSION: Final = "2.0"

JSONValue = str | int | float | bool | None | dict[str, "JSONValue"] | list["JSONValue"]
JSONDict = dict[str, JSONValue]


def _strip_none(value: JSONValue) -> JSONValue:
    """Recursively drop dict keys whose value is `None`; list items are kept."""
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


_VARIANT_STATE_KEYS: frozenset[str] = frozenset(
    {"run_id", "run_attempt", "status", "started_at", "completed_at"}
)


class Status(StrEnum):
    """Merged GitHub status+conclusion, normalized by the producer.

    Narrower than the raw run-status vocabulary in therock_types.py: GitHub
    conclusions collapse onto these buckets (e.g. timed_out -> failure), and
    `in_progress` is the sole non-terminal state.
    """

    in_progress = "in_progress"
    success = "success"
    failure = "failure"
    cancelled = "cancelled"
    skipped = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self is not Status.in_progress


class ReleaseType(StrEnum):
    """Release tier as rendered in status.json (v2 schema)."""

    nightly = "nightly"
    rc = "rc"
    dev = "dev"


def rollup_statuses(statuses: Iterable[Status], fallback: Status) -> Status:
    """Collapse many cell statuses into one parent status.

    Precedence failure > in_progress > cancelled > success > skipped: a failed
    cell is a terminal, irreversible outcome that a still-running sibling cannot
    undo, so it must not be masked by an `in_progress` cell. Otherwise
    `in_progress` wins (work is still ongoing and could succeed), and only once
    every cell is terminal do cancelled/success/skipped apply. Returns
    `fallback` when there is nothing to roll up.
    """
    seen = set(statuses)
    if not seen:
        return fallback
    for status in (
        Status.failure,
        Status.in_progress,
        Status.cancelled,
        Status.success,
    ):
        if status in seen:
            return status
    return Status.skipped


def rollup_sibling_statuses(statuses: Iterable[Status], fallback: Status) -> Status:
    """Collapse the statuses of independent sibling pipelines (rocm / pytorch /
    jax / native_packages) on the same platform into one platform status.

    This is deliberately a different precedence than `rollup_statuses`, which
    combines matrix cells / build+test *within* one pipeline: there, a
    terminal failure in one cell is irreversible and must not be masked by a
    still-running sibling *cell* of the same phase. Sibling *pipelines* are
    separate, independently-dispatched units of work, so one of them failing
    says nothing about whether another, still-running one is done -- it may
    yet also fail, or may still succeed. Crystallizing the platform to
    `failure` while a sibling pipeline has not reported a terminal status
    would report a verdict the platform has not actually reached yet.

    Precedence: in_progress > failure > cancelled > success > skipped. This
    also makes `failure` behave consistently with `cancelled` here (which
    already lost to `in_progress` even under the old precedence): both are
    terminal-negative, but neither can override still-pending sibling work.
    Once every sibling has reported a terminal status, the worst one applies.
    Returns `fallback` when there is nothing to roll up.
    """
    seen = set(statuses)
    if not seen:
        return fallback
    for status in (
        Status.in_progress,
        Status.failure,
        Status.cancelled,
        Status.success,
    ):
        if status in seen:
            return status
    return Status.skipped


class Variant(BaseModel):
    """One matrix cell of a fan-out pipeline (e.g. pytorch py x torch).

    `matrix` holds the GitHub Actions matrix values that identify the cell
    (py, torch, ...); the other fields are the cell's own run state. Serializes
    to/from a flat JSON dict with the matrix values and state merged at the top
    level.
    """

    status: Status = Status.in_progress
    run_id: int | None = None
    run_attempt: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    matrix: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _unflatten(cls, data: object) -> object:
        """Pull the flat matrix axes (any non-state key) into `matrix`."""
        if not isinstance(data, dict):
            return data
        state = {k: v for k, v in data.items() if k in _VARIANT_STATE_KEYS}
        matrix_raw = data.get("matrix")
        matrix = (
            {str(k): str(v) for k, v in matrix_raw.items()}
            if isinstance(matrix_raw, dict)
            else {}
        )
        for k, v in data.items():
            if k not in _VARIANT_STATE_KEYS and k != "matrix" and v is not None:
                matrix[k] = str(v)
        if not state.get("status"):
            state["status"] = Status.in_progress.value
        state["matrix"] = matrix
        return state

    @model_serializer
    def _flatten(self) -> dict[str, object]:
        out: dict[str, object] = {**self.matrix, "status": self.status.value}
        if self.run_id is not None:
            out["run_id"] = self.run_id
        if self.run_attempt is not None:
            out["run_attempt"] = self.run_attempt
        if self.started_at is not None:
            out["started_at"] = self.started_at
            out["completed_at"] = self.completed_at
        return out

    def key(self) -> tuple[tuple[str, str], ...]:
        """Stable identity: sorted matrix values, or run_id when it has none."""
        if self.matrix:
            return tuple(sorted(self.matrix.items()))
        return (("run_id", str(self.run_id or "")),)

    def is_terminal(self) -> bool:
        return self.completed_at is not None or self.status.is_terminal

    def should_replace(self, new: "Variant") -> bool:
        """Guard for this matrix cell (same rules and monotonic-run_id assumption
        as RunLeaf.should_replace)."""
        if (
            self.run_id is not None
            and new.run_id is not None
            and new.run_id != self.run_id
        ):
            return new.run_id > self.run_id
        existing_attempt = self.run_attempt or 0
        new_attempt = new.run_attempt or 0
        if new_attempt != existing_attempt:
            return new_attempt > existing_attempt
        if self.is_terminal() and not new.is_terminal():
            return False
        if self.completed_at is not None and new.completed_at is None:
            return False
        return True

    @staticmethod
    def rollup_status(variants: list["Variant"], fallback: Status) -> Status:
        """Aggregate cell statuses into a single status for the parent leaf."""
        return rollup_statuses((v.status for v in variants), fallback)


class RunLeaf(BaseModel):
    """One workflow run, or one aggregate matrix leaf, in the pipeline tree."""

    status: Status
    run_id: int | None = None
    run_attempt: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    variants: list[Variant] | None = None

    @model_serializer
    def _serialize(self) -> dict[str, object]:
        out: dict[str, object] = {
            "status": self.status.value,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }
        if self.variants is not None:
            out["variants"] = [v.model_dump() for v in self.variants]
        return out

    def is_terminal(self) -> bool:
        return self.completed_at is not None or self.status.is_terminal

    def should_replace(self, new: "RunLeaf") -> bool:
        """Guard for a leaf slot: newest run wins, do-not-downgrade within a run.

        Consequence worth knowing: because run_id dominates run_attempt, a
        deliberate re-run of an *older* run (lower id) loses to an already-
        dispatched newer run (higher id) even at a higher attempt, and is
        silently dropped.
        """
        if (
            self.run_id is not None
            and new.run_id is not None
            and new.run_id != self.run_id
        ):
            return new.run_id > self.run_id
        existing_attempt = self.run_attempt or 0
        new_attempt = new.run_attempt or 0
        if new_attempt != existing_attempt:
            return new_attempt > existing_attempt
        if self.is_terminal() and not new.is_terminal():
            return False
        if self.completed_at is not None and new.completed_at is None:
            return False
        return True


# Build runs once per platform; test fans out per arch under each platform.
BuildPhase = dict[str, RunLeaf]  # platform -> leaf
TestPhase = dict[str, dict[str, RunLeaf]]  # platform -> arch -> leaf


class Pipeline(BaseModel):
    """One pipeline's detail tree: a `build` phase and a `test` phase."""

    build: BuildPhase = Field(default_factory=dict)
    test: TestPhase = Field(default_factory=dict)
    test_full: TestPhase = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.build and not self.test and not self.test_full

    @model_serializer
    def _serialize(self) -> dict[str, object]:
        # `build` and `test` are always emitted (their empty `{}` is part of the
        # established shape); `test_full` is emitted only when populated so it
        # does not add a key to every existing pipeline.
        out: dict[str, object] = {
            "build": {p: leaf.model_dump() for p, leaf in self.build.items()},
            "test": {
                p: {a: leaf.model_dump() for a, leaf in arches.items()}
                for p, arches in self.test.items()
            },
        }
        if self.test_full:
            out["test_full"] = {
                p: {a: leaf.model_dump() for a, leaf in arches.items()}
                for p, arches in self.test_full.items()
            }
        return out


class NativePackages(BaseModel):
    """The native-packages pipeline: one leaf per package type."""

    rpm: RunLeaf | None = None
    deb: RunLeaf | None = None

    def is_empty(self) -> bool:
        return self.rpm is None and self.deb is None


class Pipelines(BaseModel):
    """Detail tree for every pipeline. An empty pipeline serializes as `null`,
    which the `to_dict`/`to_json` projection then drops from the on-disk shape."""

    rocm: Pipeline = Field(default_factory=Pipeline)
    pytorch: Pipeline = Field(default_factory=Pipeline)
    jax: Pipeline = Field(default_factory=Pipeline)
    native_packages: NativePackages = Field(default_factory=NativePackages)

    @model_validator(mode="before")
    @classmethod
    def _empty_for_null(cls, data: object) -> object:
        """A serialized doc emits `null` for a pipeline with no leaves; drop
        those (and an absent/null tree) so the empty-pipeline default is used.
        Everything else is left for Pydantic to validate."""
        if data is None:
            return {}
        if not isinstance(data, dict):
            return data
        return {k: v for k, v in data.items() if v is not None}

    @model_serializer
    def _serialize(self) -> dict[str, object]:
        # Empty pipelines emit `null`; the to_dict/to_json projection strips
        # those keys. List all four here for a stable intermediate shape.
        return {
            "rocm": None if self.rocm.is_empty() else self.rocm.model_dump(),
            "pytorch": None if self.pytorch.is_empty() else self.pytorch.model_dump(),
            "jax": None if self.jax.is_empty() else self.jax.model_dump(),
            "native_packages": (
                None
                if self.native_packages.is_empty()
                else self.native_packages.model_dump()
            ),
        }


def _merge_variant_leaf(existing: "RunLeaf | None", new: "RunLeaf") -> "RunLeaf":
    """Merge one leaf update carrying `variants` (a matrix cell) into another.

    Used for the `build` phase's pytorch/jax py x ref cells, all derived from
    one workflow run's own job list."""
    variants: list[Variant] = []
    positions: dict[tuple[tuple[str, str], ...], int] = {}

    for variant in existing.variants if existing and existing.variants else []:
        positions[variant.key()] = len(variants)
        variants.append(variant)

    for variant in new.variants or []:
        key = variant.key()
        pos = positions.get(key)
        if pos is None:
            positions[key] = len(variants)
            variants.append(variant)
        elif variants[pos].should_replace(variant):
            variants[pos] = variant

    return RunLeaf(
        status=Variant.rollup_status(variants, new.status),
        variants=variants,
    )


def merge_matrix_test_leaf(existing: "RunLeaf | None", new: "RunLeaf") -> "RunLeaf":
    """Merge fan-out test variants by matrix cell instead of replacing the
    whole leaf (mirrors `_merge_variant_leaf`, but also carries forward
    `run_id`/`run_attempt`/timestamps so later comparisons -- e.g.
    `_refresh_same_run_tests_from_build` in therock_update_status_json.py, its
    other caller -- can still identify the owning run).

    Every completion notification for a shared-entry-run matrix (pytorch/jax
    py x ref fan-out sharing one `GITHUB_RUN_ID`) re-derives *all* cells from
    a freshly re-fetched job-list snapshot, and those snapshots race each
    other under heavy concurrency with no correlation between "wins the push"
    and "is the freshest/most complete". Wholesale replacement
    (`arch_map[arch] = new`) lets an earlier, less-complete snapshot silently
    regress a later, more-complete one whenever it happens to win the race.
    Merging cell-by-cell through `Variant.should_replace` makes every cell
    advance monotonically regardless of push-race ordering.
    """
    variants: list[Variant] = []
    positions: dict[tuple[tuple[str, str], ...], int] = {}

    for variant in existing.variants if existing and existing.variants else []:
        positions[variant.key()] = len(variants)
        variants.append(variant)

    for variant in new.variants or []:
        key = variant.key()
        pos = positions.get(key)
        if pos is None:
            positions[key] = len(variants)
            variants.append(variant)
        elif variants[pos].should_replace(variant):
            variants[pos] = variant

    status = Variant.rollup_status(variants, new.status)
    completed_at: str | None = None
    if status.is_terminal:
        ends = [v.completed_at for v in variants if v.completed_at]
        completed_at = max(ends) if ends else new.completed_at

    starts = [v.started_at for v in variants if v.started_at]
    started_at = min(starts) if starts else None
    if not started_at:
        started_at = (existing.started_at if existing else None) or new.started_at

    if existing is None or existing.should_replace(new):
        run_id, run_attempt = new.run_id, new.run_attempt
    else:
        run_id, run_attempt = existing.run_id, existing.run_attempt

    return RunLeaf(
        status=status,
        run_id=run_id,
        run_attempt=run_attempt,
        started_at=started_at or None,
        completed_at=completed_at,
        variants=variants,
    )


# --- Summary rollup ----------------------------------------------------------
# `therock_summary.rebuild_summary` derives these from the pipeline detail tree
# on every update; they are the read-optimized projection consumers render.


class BuildRollup(BaseModel):
    """A build phase rolled up to a single status (variants collapse into it)."""

    status: Status = Status.in_progress


class TestRollup(BaseModel):
    """Per-status counts across every test leaf (and matrix cell) of a phase."""

    success: int = 0
    failure: int = 0
    in_progress: int = 0
    cancelled: int = 0
    skipped: int = 0


class PipelineRollup(BaseModel):
    """One pipeline's rolled-up `build` status and `test` counts.

    `test_full` mirrors `test` for the sibling full-suite phase; it is `None`
    (and dropped from the on-disk projection) unless that phase has reported."""

    build: BuildRollup = Field(default_factory=BuildRollup)
    test: TestRollup = Field(default_factory=TestRollup)
    test_full: TestRollup | None = None


class NativePackagesRollup(BaseModel):
    """Native-packages rollup. The rollup as a whole is optional on the parent
    (only linux builds native packages), but when it exists both `rpm` and `deb`
    are always present -- an unreported type shows its `in_progress` default
    rather than vanishing."""

    rpm: BuildRollup = Field(default_factory=BuildRollup)
    deb: BuildRollup = Field(default_factory=BuildRollup)


class PlatformSummary(BaseModel):
    """Per-platform rollup: overall status plus per-pipeline projections.

    `rocm` and `pytorch` run on both platforms and are always present (an
    unstarted pipeline shows its `in_progress` default). `jax` and
    `native_packages` are linux-only and stay `None` on windows, so the on-disk
    projection drops them there instead of emitting a misleading placeholder.
    Use `for_platform` to construct with the right per-platform defaults."""

    status: Status = Status.in_progress
    architectures: list[str] = Field(default_factory=list)
    urls: dict[str, str] = Field(default_factory=dict)
    rocm: PipelineRollup = Field(default_factory=PipelineRollup)
    pytorch: PipelineRollup = Field(default_factory=PipelineRollup)
    jax: PipelineRollup | None = None
    native_packages: NativePackagesRollup | None = None

    @classmethod
    def for_platform(cls, platform: str, **fields: object) -> "PlatformSummary":
        """Construct with platform-appropriate defaults: linux gets the
        linux-only `jax` / `native_packages` rollups; windows leaves them
        `None`. Any field passed explicitly overrides the default."""
        if platform == "linux":
            fields.setdefault("jax", PipelineRollup())
            fields.setdefault("native_packages", NativePackagesRollup())
        # Dynamic **kwargs passthrough: pydantic validates each field at
        # runtime, but mypy cannot match the object-typed splat to the strict
        # constructor signature.
        return cls(**fields)  # type: ignore[arg-type]


class Summary(BaseModel):
    """Read-optimized rollup of the whole document, one entry per platform."""

    overall_status: Status = Status.in_progress
    # Route the defaults through `for_platform` so a default-constructed Summary
    # honors the per-platform contract (linux carries the jax / native_packages
    # placeholders, windows leaves them None) before the first rebuild_summary.
    linux: PlatformSummary = Field(
        default_factory=lambda: PlatformSummary.for_platform("linux")
    )
    windows: PlatformSummary = Field(
        default_factory=lambda: PlatformSummary.for_platform("windows")
    )


class StatusDocument(BaseModel):
    """Root status.json v2 document.

    `summary` is fully rebuilt by `therock_summary.rebuild_summary`; never
    edit it directly here. `*_architectures` and `*_urls` are first-class so
    they survive recomputes (the summary builder reads and embeds them); they
    are loaded from `summary` and re-embedded there, not serialized at the top
    level.

    `release_type` is stored as emitted (`"rc"` for the prerelease tier).
    `rocm_version` is the scalar wheel-style identifier and the routing key;
    set once at create time and never overwritten.

    `pipelines` is the typed detail tree (`Pipelines`): one `Pipeline` per
    pipeline (`rocm`/`pytorch`/`jax`) plus `native_packages`, with leaves
    routed by `upsert_leaf`.
    """

    # Field order is the on-disk key order -- model_dump() serializes in
    # declaration order, so this block is the source of truth for it.
    #
    # Timestamps stay `str`: they are emitted ISO-8601 with a `Z` suffix, which
    # Pydantic's native `datetime` does not round-trip (it renders `+00:00`).
    # `build_date` is a compact `YYYYMMDD` string, not a date.
    schema_version: Literal["2.0"] = SCHEMA_VERSION
    release_type: ReleaseType | None = None
    rocm_version: str = ""
    build_date: str = ""
    trigger_workflow_run_id: int | None = None
    # Attempt number of the owning orchestrator run. Ownership is the pair
    # (trigger_workflow_run_id, trigger_run_attempt): a GitHub re-run keeps the
    # same run id but bumps the attempt, so the attempt breaks ties between a
    # re-run and the run it supersedes.
    trigger_run_attempt: int | None = None
    created_at: str | None = None
    completed_at: str | None = None
    # The top-level orchestrator's own terminal conclusion (success / failure /
    # cancelled), stamped by `_finalize_orchestrator`. Folded into
    # `summary.overall_status` so an aborted or failed release is never reported
    # as `success` just because the leaves that happened to report all passed.
    orchestrator_conclusion: Status | None = None
    status_json_created: str = ""
    status_json_last_updated: str = ""

    # First-class so they survive recomputes, but on disk they live only
    # inside `summary` -- excluded from the top-level serialization.
    linux_architectures: list[str] = Field(default_factory=list, exclude=True)
    windows_architectures: list[str] = Field(default_factory=list, exclude=True)
    linux_urls: dict[str, str] = Field(default_factory=dict, exclude=True)
    windows_urls: dict[str, str] = Field(default_factory=dict, exclude=True)

    summary: Summary = Field(default_factory=Summary)
    pipelines: Pipelines = Field(default_factory=Pipelines)

    @model_validator(mode="before")
    @classmethod
    def _from_wire(cls, data: object) -> object:
        """Lift the arch/url fields out of the nested `summary` block."""
        if not isinstance(data, dict):
            return data

        summary_raw = data.get("summary") or {}
        if not isinstance(summary_raw, dict):
            summary_raw = {}
        linux_summary = summary_raw.get("linux")
        if not isinstance(linux_summary, dict):
            linux_summary = {}
        windows_summary = summary_raw.get("windows")
        if not isinstance(windows_summary, dict):
            windows_summary = {}

        out = dict(data)
        out.setdefault("linux_architectures", linux_summary.get("architectures") or [])
        out.setdefault(
            "windows_architectures", windows_summary.get("architectures") or []
        )
        out.setdefault("linux_urls", linux_summary.get("urls") or {})
        out.setdefault("windows_urls", windows_summary.get("urls") or {})
        # `summary` is validated into the typed `Summary` model; it is fully
        # recomputed by `rebuild_summary`, so a stale on-disk shape is harmless.
        out["summary"] = summary_raw
        return out

    def upsert_leaf(
        self,
        platform: str,
        arch: str,
        pipeline_type: str,
        pipeline_phase: str,
        leaf: RunLeaf,
    ) -> bool:
        """Place `leaf` in the typed pipeline tree, applying the do-not-downgrade
        guard.

        Routes by pipeline / phase:
          native_packages -> `native_packages.<rpm|deb>` (phase is the pkg type)
          build           -> `<pipeline>.build[platform]`
          test            -> `<pipeline>.test[platform][arch]`
          test-full       -> `<pipeline>.test_full[platform][arch]`

        Returns True when written, False when the guard rejected it.
        """
        if pipeline_type == "native_packages":
            existing = getattr(self.native_packages, pipeline_phase, None)
            if isinstance(existing, RunLeaf) and not existing.should_replace(leaf):
                return False
            setattr(self.native_packages, pipeline_phase, leaf)
            return True

        pipeline: Pipeline = getattr(self.pipelines, pipeline_type)
        if pipeline_phase == "build":
            existing = pipeline.build.get(platform)
            if leaf.variants or (existing is not None and existing.variants):
                pipeline.build[platform] = _merge_variant_leaf(existing, leaf)
                return True
            if existing is not None and not existing.should_replace(leaf):
                return False
            pipeline.build[platform] = leaf
            return True

        phase_map = (
            pipeline.test_full if pipeline_phase == "test-full" else pipeline.test
        )
        arch_map = phase_map.setdefault(platform, {})
        existing = arch_map.get(arch)
        if leaf.variants:
            arch_map[arch] = merge_matrix_test_leaf(existing, leaf)
            return True
        if existing is not None and not existing.should_replace(leaf):
            return False
        arch_map[arch] = leaf
        return True

    @property
    def native_packages(self) -> NativePackages:
        return self.pipelines.native_packages

    @classmethod
    def from_dict(cls, data: JSONDict) -> "StatusDocument":
        """Load and validate a parsed JSON dict.

        Raises:
            ValueError: If the dict fails schema validation.
        """
        return cls.model_validate(data)

    def to_dict(self) -> JSONDict:
        """On-disk JSON shape: field order from the model, with every
        `null`-valued key dropped recursively (consumers treat an absent key
        the same as null). Empty containers (`""`/`[]`/`{}`) and `0` counts are
        kept. The per-model serializers build the full shape; this projection
        strips the nulls, including the excluded arch/url fields that live only
        inside `summary`."""
        return {
            k: _strip_none(v) for k, v in self.model_dump().items() if v is not None
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# Resolve forward references so every model has a fully built schema.
Variant.model_rebuild()
RunLeaf.model_rebuild()
Pipeline.model_rebuild()
NativePackages.model_rebuild()
Pipelines.model_rebuild()
StatusDocument.model_rebuild()
