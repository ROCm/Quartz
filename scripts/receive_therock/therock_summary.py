#!/usr/bin/env python
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

from collections import Counter
from collections.abc import Iterable

from therock_status_document import (
    BuildRollup,
    NativePackagesRollup,
    Pipeline,
    PipelineRollup,
    PlatformSummary,
    RunLeaf,
    Status,
    StatusDocument,
    Summary,
    TestRollup,
    rollup_statuses,
)
from therock_types import EXPECTED_PIPELINE_TYPES


def freeze_requested_architectures(
    doc: StatusDocument,
    platform: str,
    pipeline_type: str,
    pipeline_phase: str,
    architectures: list[str],
) -> None:
    if pipeline_type != "rocm" or pipeline_phase != "build":
        return
    if platform == "linux" and not doc.linux_architectures:
        doc.linux_architectures = list(architectures)
    elif platform == "windows" and not doc.windows_architectures:
        doc.windows_architectures = list(architectures)


def rebuild_summary(doc: StatusDocument) -> None:
    """Rebuild `doc.summary` from the pipeline tree into the typed `Summary`.

    Both platforms are always emitted; only platforms that carry data (a
    requested arch or any run) contribute to `overall_status`.

    `overall_status` is capped at `in_progress` until the orchestrator's
    completed event stamps `doc.completed_at` (see
    `therock_update_status_json._finalize_orchestrator`). Terminal leaf rollups alone
    do not mean the release is done: more leaves may still report and the
    orchestrator may still be running. Once `doc.completed_at` is set, the
    worst-of rollup over the platforms stands. `rebuild_summary` never sets
    `completed_at` itself.

    Per-platform `status` fields are not capped: they always reflect the live
    worst-of rollup so a platform that has already finished reads its real
    state even while the release as a whole is still in progress.
    """
    linux, linux_has_data = _build_platform_summary(doc, "linux")
    windows, windows_has_data = _build_platform_summary(doc, "windows")

    platform_statuses: list[Status] = []
    if linux_has_data:
        platform_statuses.append(linux.status)
    if windows_has_data:
        platform_statuses.append(windows.status)

    if doc.completed_at is not None:
        # Fold the orchestrator's own conclusion into the worst-of so a
        # cancelled or failed release outranks any leaf successes. A `success`
        # orchestrator is a no-op here (worst-of keeps the worst leaf).
        statuses = list(platform_statuses)
        conclusion = doc.orchestrator_conclusion
        if conclusion is not None:
            statuses.append(conclusion)
        # A decisive-negative orchestrator conclusion (failure / cancelled) is
        # authoritative: a leaf that never reported a terminal event cannot
        # un-fail or un-cancel a finished release, so drop `in_progress` here so
        # the negative conclusion wins.
        if conclusion in (Status.failure, Status.cancelled):
            statuses = [s for s in statuses if s is not Status.in_progress]
        overall_status = _rollup(statuses)
    else:
        overall_status = Status.in_progress
    doc.summary = Summary(overall_status=overall_status, linux=linux, windows=windows)


def _build_platform_summary(
    doc: StatusDocument, platform: str
) -> tuple[PlatformSummary, bool]:
    """Build a platform's rollup; the bool flags whether it carries any data.

    rocm/pytorch run on both platforms and are always projected; an unstarted
    pipeline collapses to an `unstarted_status` placeholder. jax and
    native_packages are linux-only: pass them only when they carry data and let
    `for_platform` supply the linux placeholder (and the windows `None`).

    The same `unstarted_status` (see `_unstarted_pipeline_status`) both renders
    the placeholder and, while the release is live and some expected pipeline is
    unreported, feeds the platform worst-of -- so the platform never reads e.g.
    `success` while a pipeline it will still run renders below it. Once finalized
    it is not injected: a never-reported pipeline did not run this release.

    Everything downstream (pytorch/jax/native, and rocm test) gates on the rocm
    *build* via GitHub `needs:`, so the build outcome drives both the platform
    status and what the unstarted children render:

        rocm.build     platform status               unstarted children
        -----------    --------------------------    ------------------
        success        in_progress (live)            in_progress
                       -> success once finalized
        in_progress    in_progress                   in_progress
        cancelled      cancelled                     cancelled
        failure        failure                       skipped

    A cancelled/failed rocm *test* gates nothing downstream, so the children stay
    `in_progress` and the platform stays `in_progress` (in_progress outranks
    cancelled in the worst-of).
    """
    architectures = (
        doc.linux_architectures if platform == "linux" else doc.windows_architectures
    )
    urls = doc.linux_urls if platform == "linux" else doc.windows_urls

    seen_statuses: list[Status] = []
    rocm = _pipeline_rollup(doc.pipelines.rocm, platform, seen_statuses)
    pytorch = _pipeline_rollup(doc.pipelines.pytorch, platform, seen_statuses)
    jax = _pipeline_rollup(doc.pipelines.jax, platform, seen_statuses)
    native_packages = _native_rollup(doc, platform, seen_statuses)

    empty_platform_status = Status.in_progress if architectures else Status.skipped

    unstarted_status = _unstarted_pipeline_status(doc, platform, empty_platform_status)
    placeholder = PipelineRollup(build=BuildRollup(status=unstarted_status))
    native_placeholder = NativePackagesRollup(
        rpm=BuildRollup(status=unstarted_status),
        deb=BuildRollup(status=unstarted_status),
    )

    rollups = {
        "rocm": rocm,
        "pytorch": pytorch,
        "jax": jax,
        "native_packages": native_packages,
    }
    status_inputs = list(seen_statuses)
    if doc.completed_at is None and any(
        rollups[pipeline_type] is None
        for pipeline_type in EXPECTED_PIPELINE_TYPES[platform]
    ):
        status_inputs.append(unstarted_status)

    fields: dict[str, object] = {
        "status": _rollup(status_inputs, empty_default=empty_platform_status),
        "architectures": list(architectures),
        "urls": dict(urls),
        "rocm": rocm or placeholder,
        "pytorch": pytorch or placeholder,
    }
    if platform == "linux":
        fields["jax"] = jax if jax is not None else placeholder
        fields["native_packages"] = (
            native_packages if native_packages is not None else native_placeholder
        )

    has_data = bool(architectures) or bool(seen_statuses)
    return PlatformSummary.for_platform(platform, **fields), has_data


def _unstarted_pipeline_status(
    doc: StatusDocument, platform: str, empty_platform_status: Status
) -> Status:
    """The eventual status of an expected pipeline that has not reported a leaf.

    Every downstream pipeline (pytorch/jax/native packages, and rocm test) is
    gated on the rocm *build* via GitHub `needs:`. A terminal-negative build
    never opens that gate, so the unstarted pipelines will never run and inherit
    the outcome GitHub assigns a blocked `needs:` job: a cancelled build ->
    `cancelled`; a failed build -> `skipped`. This holds whether the release is
    live or finalized -- the build never dispatched downstream either way. A
    cancelled/failed rocm *test* gates nothing (downstream only needs the build),
    so it never reaches here. Otherwise the pipeline is still pending:
    `empty_platform_status`.
    """
    rocm_build = doc.pipelines.rocm.build.get(platform)
    status = rocm_build.status if rocm_build is not None else None
    if status is Status.cancelled:
        return Status.cancelled
    if status is Status.failure:
        return Status.skipped
    return empty_platform_status


def _pipeline_rollup(
    pipe: Pipeline, platform: str, seen_statuses: list[Status]
) -> PipelineRollup | None:
    """Roll one pipeline up: build collapses to a status, test to per-status
    counts. Returns `None` when this platform has neither a build nor a test
    leaf, so the pipeline is dropped from the projection instead of emitting a
    placeholder."""
    build_leaf = pipe.build.get(platform)
    arch_map = pipe.test.get(platform)
    full_arch_map = pipe.test_full.get(platform)
    if build_leaf is None and not arch_map and not full_arch_map:
        return None
    rollup = PipelineRollup()
    if build_leaf is not None:
        seen_statuses.append(build_leaf.status)
        rollup.build = BuildRollup(status=build_leaf.status)
    test_status_start = len(seen_statuses)
    if arch_map:
        rollup.test = _test_rollup(arch_map.values(), seen_statuses)
    if full_arch_map:
        rollup.test_full = _test_rollup(full_arch_map.values(), seen_statuses)
    if build_leaf is None:
        # Some delegated framework workflows report test leaves for a platform
        # without a separate build leaf in that platform's detail tree. Do not
        # leave the mandatory build projection looking live once those test
        # leaves have reached a terminal state.
        rollup.build = BuildRollup(
            status=rollup_statuses(
                seen_statuses[test_status_start:], Status.in_progress
            )
        )
    return rollup


def _native_rollup(
    doc: StatusDocument, platform: str, seen_statuses: list[Status]
) -> NativePackagesRollup | None:
    """Native packages are placed without a platform; the producer only builds
    them for linux, so they only count there. Returns `None` when nothing has
    reported (e.g. on windows), dropping the pipeline from the projection."""
    if platform != "linux":
        return None
    native = doc.pipelines.native_packages
    rollup = NativePackagesRollup()
    seen_any = False
    if native.rpm is not None:
        seen_any = True
        seen_statuses.append(native.rpm.status)
        rollup.rpm = BuildRollup(status=native.rpm.status)
    if native.deb is not None:
        seen_any = True
        seen_statuses.append(native.deb.status)
        rollup.deb = BuildRollup(status=native.deb.status)
    return rollup if seen_any else None


def _test_rollup(leaves: Iterable[RunLeaf], seen_statuses: list[Status]) -> TestRollup:
    """Per-status counts across every test leaf and matrix cell of a phase."""
    counter: Counter[Status] = Counter()
    for leaf in leaves:
        cells = _cell_statuses(leaf)
        for status in cells:
            counter[status] += 1
            seen_statuses.append(status)
        if leaf.variants:
            seen_statuses.append(leaf.status)
    return TestRollup(
        success=counter[Status.success],
        failure=counter[Status.failure],
        in_progress=counter[Status.in_progress],
        cancelled=counter[Status.cancelled],
        skipped=counter[Status.skipped],
    )


def _cell_statuses(leaf: RunLeaf) -> list[Status]:
    """The matrix-cell statuses a leaf contributes to the per-status counts:
    one per variant when the leaf fanned out, else the leaf's own status. The
    leaf's workflow-level `status` is folded into the platform worst-of
    separately by `_test_rollup` (it is not itself a cell)."""
    if leaf.variants:
        return [v.status for v in leaf.variants]
    return [leaf.status]


def _rollup(
    statuses: list[Status], empty_default: Status = Status.in_progress
) -> Status:
    """Worst-of reducer over a flat list of statuses.

    Precedence failure > in_progress > cancelled > success > skipped matches
    `Variant.rollup_status`: a failed leaf is terminal and irreversible, so it
    must not be masked by a sibling that is still `in_progress` (a stuck or
    never-completing leaf would otherwise hide a real failure on the same
    platform). `empty_default` is returned when nothing has been reported:
    `in_progress` for a release that is still pending, but callers pass `skipped`
    for a platform with no requested arches (not part of this release).
    """
    seen = set(statuses)
    if Status.failure in seen:
        return Status.failure
    if Status.in_progress in seen:
        return Status.in_progress
    if Status.cancelled in seen:
        return Status.cancelled
    if Status.success in seen:
        return Status.success
    if Status.skipped in seen:
        return Status.skipped
    return empty_default
