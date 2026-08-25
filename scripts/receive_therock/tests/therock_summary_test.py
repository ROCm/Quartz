# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_summary: the pipeline-tree -> Summary projection.

These are integration-style: build a real `StatusDocument` through the same
public API the producer uses (`freeze_requested_architectures` + `upsert_leaf`),
run `rebuild_summary`, then assert on the derived `doc.summary`. The emphasis is
on the rollup precedence, the per-platform contract (jax / native_packages are
linux-only), matrix-cell counting, and the empty-platform handling.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from therock_status_document import (  # noqa: E402
    RunLeaf,
    Status,
    StatusDocument,
    Variant,
)
from therock_summary import (  # noqa: E402
    freeze_requested_architectures,
    rebuild_summary,
)


def _leaf(
    *,
    status: Status = Status.success,
    run_id: int | None = 1,
    run_attempt: int | None = 1,
    started_at: str | None = "2026-04-08T01:00:00Z",
    completed_at: str | None = "2026-04-08T01:30:00Z",
    variants: list[Variant] | None = None,
) -> RunLeaf:
    return RunLeaf(
        status=status,
        run_id=run_id,
        run_attempt=run_attempt,
        started_at=started_at,
        completed_at=completed_at,
        variants=variants,
    )


def _variant(*, matrix: dict[str, str], status: Status = Status.success) -> Variant:
    return Variant(matrix=matrix, status=status, run_attempt=1)


def _freeze(doc: StatusDocument, platform: str, arches: list[str]) -> None:
    # Arches are only frozen off a rocm/build event; go through the public path
    # rather than poking the field so the test exercises the real contract.
    freeze_requested_architectures(
        doc,
        platform=platform,
        pipeline_type="rocm",
        pipeline_phase="build",
        architectures=arches,
    )


# --- overall_status: worst-of across platforms ------------------------------


def test_empty_document_overall_in_progress() -> None:
    # Nothing reported, no arches: both platforms carry no data, so neither
    # feeds the overall rollup -> the empty default (in_progress) stands.
    doc = StatusDocument()
    rebuild_summary(doc)
    assert doc.summary.overall_status is Status.in_progress


def test_overall_takes_worst_of_platforms() -> None:
    doc = StatusDocument()
    # overall_status is only computed once the release is finalized; set
    # completed_at so the worst-of rollup is exercised rather than the cap.
    doc.completed_at = "2026-04-08T02:00:00Z"
    _freeze(doc, "linux", ["gfx942"])
    _freeze(doc, "windows", ["gfx1100"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("windows", "", "rocm", "build", _leaf(status=Status.failure))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.success
    assert doc.summary.windows.status is Status.failure
    assert doc.summary.overall_status is Status.failure


def test_cancelled_orchestrator_overrides_leaf_successes() -> None:
    # A cancelled/aborted release must not read `success` just because every
    # leaf that reported before the cancel happened to pass.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.cancelled
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.success
    assert doc.summary.overall_status is Status.cancelled


def test_cancelled_orchestrator_overrides_lingering_in_progress_leaf() -> None:
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.cancelled
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "rocm",
        "test",
        _leaf(status=Status.in_progress, completed_at=None),
    )
    rebuild_summary(doc)
    # The platform keeps in_progress (ongoing work outranks cancelled there)...
    assert doc.summary.linux.status is Status.in_progress
    # ...but the decisive cancelled conclusion wins at the top level.
    assert doc.summary.overall_status is Status.cancelled


def test_failed_orchestrator_overrides_lingering_in_progress_leaf() -> None:
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.failure
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "rocm",
        "test",
        _leaf(status=Status.in_progress, completed_at=None),
    )
    rebuild_summary(doc)
    assert doc.summary.overall_status is Status.failure


def test_success_orchestrator_stays_in_progress_with_lingering_leaf() -> None:
    # `success` is NOT decisive: a still-running leaf might yet fail, so the
    # release must stay `in_progress` rather than reporting a premature success.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.success
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "rocm",
        "test",
        _leaf(status=Status.in_progress, completed_at=None),
    )
    rebuild_summary(doc)
    assert doc.summary.overall_status is Status.in_progress


def test_failed_orchestrator_overrides_leaf_successes() -> None:
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.failure
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.overall_status is Status.failure


def test_success_orchestrator_is_a_noop_over_leaf_rollup() -> None:
    # A `success` orchestrator conclusion must not mask a failing leaf: the
    # worst-of still wins.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.success
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.failure))
    rebuild_summary(doc)
    assert doc.summary.overall_status is Status.failure


# --- rollup precedence (platform status) ------------------------------------


def test_failure_is_not_masked_by_in_progress_sibling() -> None:
    # A stuck/never-completing sibling leaf must not hide a real failure on the
    # same platform: a terminal failure outranks in_progress.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.failure))
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "rocm",
        "test",
        _leaf(status=Status.in_progress, completed_at=None),
    )
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.failure


def test_in_progress_dominates_cancelled_and_success_on_platform() -> None:
    # With no failure present, ongoing work outranks a terminal cancelled leaf.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942", "gfx1100"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.cancelled))
    doc.upsert_leaf(
        "linux",
        "gfx1100",
        "rocm",
        "test",
        _leaf(status=Status.in_progress, completed_at=None),
    )
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.in_progress


def test_failure_beats_cancelled_and_success_within_one_pipeline() -> None:
    # Within one pipeline (matrix cells / build+test), a terminal failure
    # still outranks a terminal cancelled and a terminal success -- that
    # precedence is unaffected by the cross-pipeline sibling fix below. Every
    # sibling pipeline also reports terminally here, isolating the
    # within-pipeline precedence from the cross-pipeline
    # in_progress-outranks-failure rule (see
    # test_multiple_pipelines_aggregate_into_platform_status).
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942", "gfx1100"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.cancelled))
    doc.upsert_leaf("linux", "gfx1100", "rocm", "test", _leaf(status=Status.failure))
    doc.upsert_leaf("linux", "", "pytorch", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "deb", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.failure


def test_cancelled_beats_success() -> None:
    # A cancelled rocm BUILD gates every downstream pipeline, so the platform
    # reads cancelled even while a rocm test leaf reports success. The cancel
    # propagates to the unstarted pytorch/jax/native placeholders (see the
    # dedicated propagation tests below).
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.cancelled))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.cancelled


def test_cancelled_rocm_build_propagates_to_unstarted_pipelines() -> None:
    # A cancelled rocm build gates build_artifacts, so pytorch/jax/native never
    # dispatch. Their unstarted placeholders must render cancelled -- not
    # in_progress -- so the child pipelines stay consistent with the cancelled
    # platform instead of looking live forever.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.cancelled))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.cancelled
    assert doc.summary.linux.pytorch.build.status is Status.cancelled
    assert doc.summary.linux.jax.build.status is Status.cancelled
    assert doc.summary.linux.native_packages.rpm.status is Status.cancelled
    assert doc.summary.linux.native_packages.deb.status is Status.cancelled


def test_cancelled_rocm_build_propagates_after_finalize() -> None:
    # The cancel propagation does not depend on the release being live: a
    # cancelled build never dispatched downstream whether the orchestrator is
    # still running or already finished. Once `completed_at` is set the
    # unstarted-pipeline worst-of injection is skipped, but the placeholders must
    # still render cancelled so a finished, doomed release does not leave
    # pytorch/jax/native reading in_progress forever.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.cancelled
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.cancelled))
    rebuild_summary(doc)
    assert doc.summary.overall_status is Status.cancelled
    assert doc.summary.linux.status is Status.cancelled
    assert doc.summary.linux.rocm.build.status is Status.cancelled
    assert doc.summary.linux.pytorch.build.status is Status.cancelled
    assert doc.summary.linux.jax.build.status is Status.cancelled
    assert doc.summary.linux.native_packages.rpm.status is Status.cancelled
    assert doc.summary.linux.native_packages.deb.status is Status.cancelled


def test_failed_rocm_build_propagates_skipped_to_unstarted_pipelines() -> None:
    # A failed rocm build blocks build_artifacts; downstream `needs:` jobs whose
    # default `if: success()` is now false are `skipped` by GitHub (not
    # cancelled). The unstarted pytorch/jax/native placeholders mirror that:
    # `skipped`, not in_progress. The platform still reads `failure` -- the build
    # leaf contributes it and it outranks the propagated `skipped`.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.failure))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.failure
    assert doc.summary.linux.pytorch.build.status is Status.skipped
    assert doc.summary.linux.jax.build.status is Status.skipped
    assert doc.summary.linux.native_packages.rpm.status is Status.skipped
    assert doc.summary.linux.native_packages.deb.status is Status.skipped


def test_failed_rocm_build_propagates_skipped_after_finalize() -> None:
    # Same as above but finalized: the propagation does not depend on the release
    # being live, so a finished failed release still renders the unstarted
    # children `skipped` rather than leaving them in_progress forever.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    doc.orchestrator_conclusion = Status.failure
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.failure))
    rebuild_summary(doc)
    assert doc.summary.linux.rocm.build.status is Status.failure
    assert doc.summary.overall_status is Status.failure
    assert doc.summary.linux.status is Status.failure
    assert doc.summary.linux.pytorch.build.status is Status.skipped
    assert doc.summary.linux.jax.build.status is Status.skipped
    assert doc.summary.linux.native_packages.rpm.status is Status.skipped
    assert doc.summary.linux.native_packages.deb.status is Status.skipped


def test_cancelled_rocm_test_does_not_propagate_to_unstarted_pipelines() -> None:
    # A cancelled rocm *test* gates nothing downstream (build_artifacts already
    # succeeded), so pytorch/jax/native are still pending and their placeholders
    # stay in_progress. The platform is worst-of: cancelled test + still-pending
    # pipelines -> in_progress (in_progress outranks cancelled).
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.cancelled))
    rebuild_summary(doc)
    assert doc.summary.linux.rocm.test.cancelled == 1
    assert doc.summary.linux.status is Status.in_progress
    assert doc.summary.linux.pytorch.build.status is Status.in_progress
    assert doc.summary.linux.jax.build.status is Status.in_progress
    assert doc.summary.linux.native_packages.rpm.status is Status.in_progress
    assert doc.summary.linux.native_packages.deb.status is Status.in_progress


def test_all_success_is_success() -> None:
    # Every expected linux pipeline (rocm, pytorch, jax, native_packages) must
    # report success before the platform reads success; an unstarted expected
    # pipeline keeps the platform in_progress (see test below).
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "pytorch", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "deb", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.success


def test_all_skipped_rolls_up_to_skipped() -> None:
    # a platform whose only reported leaves are skipped must roll up to
    # skipped, not success (matching Variant.rollup_status).
    # This is pure hypothetical to test the rollup logic:
    # it cannot happen in reality as notify_quartz
    # is only dispatched if a workflow is not skipped.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.skipped))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.skipped))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.skipped
    assert doc.summary.overall_status is Status.skipped


# --- empty-platform handling ------------------------------------------------


def test_platform_with_arches_but_no_runs_is_in_progress() -> None:
    # Arches requested (build promised) but nothing reported yet: pending.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.in_progress
    assert doc.summary.linux.architectures == ["gfx942"]


def test_unused_platform_reads_skipped_but_does_not_drag_overall() -> None:
    # linux-only release: windows has no arches and no leaves. Its own status
    # reads `skipped` (empty_default) but it is gated out of overall_status, so
    # overall reflects only linux.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.windows.status is Status.skipped
    assert doc.summary.windows.architectures == []
    assert doc.summary.windows.rocm.build.status is Status.skipped
    assert doc.summary.windows.pytorch.build.status is Status.skipped
    assert doc.summary.overall_status is Status.success


# --- per-platform contract (jax / native_packages are linux-only) -----------


def test_both_platforms_always_emitted_with_rocm_pytorch_placeholders() -> None:
    doc = StatusDocument()
    rebuild_summary(doc)
    # rocm/pytorch are always present (placeholder) on both platforms.
    assert doc.summary.linux.rocm is not None
    assert doc.summary.linux.pytorch is not None
    assert doc.summary.windows.rocm is not None
    assert doc.summary.windows.pytorch is not None


def test_jax_and_native_present_on_linux_absent_on_windows() -> None:
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    _freeze(doc, "windows", ["gfx1100"])
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.in_progress))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.linux.jax is not None
    assert doc.summary.linux.native_packages is not None
    # windows never carries the linux-only pipelines.
    assert doc.summary.windows.jax is None
    assert doc.summary.windows.native_packages is None


def test_native_packages_counts_rpm_and_deb_into_platform_status() -> None:
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "deb", _leaf(status=Status.failure))
    rebuild_summary(doc)
    native = doc.summary.linux.native_packages
    assert native is not None
    assert native.rpm.status is Status.success
    assert native.deb.status is Status.failure
    # deb failure sets native_packages' own status to failure, but rocm/
    # pytorch/jax are still pending (never reported) on this platform, so the
    # platform itself must not crystallize to failure yet.
    assert doc.summary.linux.status is Status.in_progress


def test_native_packages_failure_drags_platform_once_siblings_are_terminal() -> None:
    # Same deb failure, but every sibling pipeline has also reported
    # terminally: nothing is left pending, so the worst-of (native's failure)
    # now applies.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "pytorch", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "deb", _leaf(status=Status.failure))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.failure


def test_native_packages_pending_until_both_rpm_and_deb_report() -> None:
    # rpm and deb are both always expected. With every other pipeline green but
    # only rpm reported, the live release must stay in_progress -- the missing
    # deb keeps native_packages pending so the platform cannot read success.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "pytorch", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    rebuild_summary(doc)
    native = doc.summary.linux.native_packages
    assert native is not None
    assert native.rpm.status is Status.success
    # deb never reported: renders its in_progress default and holds the platform.
    assert native.deb.status is Status.in_progress
    assert doc.summary.linux.status is Status.in_progress


def test_native_packages_single_side_does_not_wedge_finalized_release() -> None:
    # For the moment: hypothetical test:
    # Once finalized, a side that never reported was gated out: it renders
    # `skipped` (not a wedging `in_progress`) and the platform follows the
    # reported statuses (here, rpm success).
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "pytorch", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    rebuild_summary(doc)
    native = doc.summary.linux.native_packages
    assert native is not None
    assert native.rpm.status is Status.success
    assert native.deb.status is Status.skipped
    assert doc.summary.linux.status is Status.success


# --- freeze / urls projection -----------------------------------------------


def test_freeze_requested_architectures_is_set_once() -> None:
    # The first rocm/build event fixes the promised arch set; a later build
    # event (e.g. a re-run) must not overwrite it.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942", "gfx1100"])
    _freeze(doc, "linux", ["gfx1201"])
    rebuild_summary(doc)
    assert doc.summary.linux.architectures == ["gfx942", "gfx1100"]


def test_urls_project_into_platform_summary() -> None:
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.linux_urls = {"gfx942": "https://example.com/gfx942"}
    rebuild_summary(doc)
    assert doc.summary.linux.urls == {"gfx942": "https://example.com/gfx942"}


# --- test-rollup: per matrix-cell counting ----------------------------------


def test_test_rollup_counts_each_matrix_cell_not_each_arch() -> None:
    # A pytorch.test arch leaf carries variants (py x torch). The counters must
    # count one per cell, not one per arch. One arch with 2 cells -> 2 counts.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            status=Status.in_progress,
            completed_at=None,
            variants=[
                _variant(matrix={"py": "3.11"}, status=Status.success),
                _variant(matrix={"py": "3.12"}, status=Status.failure),
            ],
        ),
    )
    rebuild_summary(doc)
    test = doc.summary.linux.pytorch.test
    assert test.success == 1
    assert test.failure == 1
    assert test.in_progress == 0


def test_running_test_leaf_not_masked_by_terminal_cells() -> None:
    # A still-running pytorch test workflow whose only-parsed-so-far matrix cells
    # all passed: the workflow-level leaf.status is in_progress, but `variants`
    # holds only the finished cells. The leaf's own status must feed the platform
    # worst-of so the platform reads in_progress, not a premature success (the
    # yet-to-start cells aren't in `variants` yet). Cell counts stay cell-only.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            status=Status.in_progress,
            completed_at=None,
            variants=[
                _variant(matrix={"py": "3.11"}, status=Status.success),
                _variant(matrix={"py": "3.12"}, status=Status.success),
            ],
        ),
    )
    rebuild_summary(doc)
    test = doc.summary.linux.pytorch.test
    # Counts still reflect cells only (the leaf's own status is not a cell).
    assert test.success == 2
    assert test.in_progress == 0
    # ...but the platform is not fooled into success while the workflow runs.
    assert doc.summary.linux.status is Status.in_progress


def test_test_full_phase_rolls_up_separately_and_feeds_platform() -> None:
    # The full-suite `test-full` phase renders as its own counts block and its
    # status still feeds pytorch's own rollup (a failing full suite fails
    # pytorch even when the smoke `test` passed). jax/native_packages are
    # still pending on this platform, so the platform itself stays
    # in_progress until they report too.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "pytorch", "test", _leaf(status=Status.success))
    doc.upsert_leaf(
        "linux", "gfx942", "pytorch", "test-full", _leaf(status=Status.failure)
    )
    rebuild_summary(doc)
    pytorch = doc.summary.linux.pytorch
    assert pytorch.test.success == 1
    assert pytorch.test_full is not None
    assert pytorch.test_full.failure == 1
    assert doc.summary.linux.status is Status.in_progress


def test_test_full_phase_failure_drags_platform_once_siblings_are_terminal() -> None:
    # Same failing full suite, but jax/native_packages have also reported
    # terminally: nothing is left pending, so the worst-of (pytorch's
    # test-full failure) now applies.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "pytorch", "test", _leaf(status=Status.success))
    doc.upsert_leaf(
        "linux", "gfx942", "pytorch", "test-full", _leaf(status=Status.failure)
    )
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "deb", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.failure


def test_test_rollup_counts_cells_across_multiple_arches() -> None:
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942", "gfx1100"])
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "rocm",
        "test",
        _leaf(status=Status.success),
    )
    doc.upsert_leaf(
        "linux",
        "gfx1100",
        "rocm",
        "test",
        _leaf(status=Status.failure),
    )
    rebuild_summary(doc)
    test = doc.summary.linux.rocm.test
    # No variants -> each leaf contributes one cell (its own status).
    assert test.success == 1
    assert test.failure == 1


def test_test_rollup_counts_every_status_once() -> None:
    # One arch carrying five cells, one per status: the counter must map each
    # status to exactly one count, pinning the whole TestRollup projection.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            status=Status.in_progress,
            completed_at=None,
            variants=[
                _variant(matrix={"py": "3.10"}, status=Status.success),
                _variant(matrix={"py": "3.11"}, status=Status.failure),
                _variant(matrix={"py": "3.12"}, status=Status.in_progress),
                _variant(matrix={"py": "3.13"}, status=Status.cancelled),
                _variant(matrix={"py": "3.14"}, status=Status.skipped),
            ],
        ),
    )
    rebuild_summary(doc)
    test = doc.summary.linux.pytorch.test
    assert test.success == 1
    assert test.failure == 1
    assert test.in_progress == 1
    assert test.cancelled == 1
    assert test.skipped == 1


def test_test_rollup_aggregates_across_leaves_and_cells() -> None:
    # Counters sum over every test leaf AND every matrix cell within them: a
    # variant leaf (2 cells) plus a plain arch leaf (1 cell) -> 3 successes.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942", "gfx1100"])
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            variants=[
                _variant(matrix={"py": "3.11"}, status=Status.success),
                _variant(matrix={"py": "3.12"}, status=Status.success),
            ],
        ),
    )
    doc.upsert_leaf(
        "linux",
        "gfx1100",
        "pytorch",
        "test",
        _leaf(status=Status.success),
    )
    rebuild_summary(doc)
    assert doc.summary.linux.pytorch.test.success == 3


def test_build_and_test_coexist_on_one_pipeline_rollup() -> None:
    # A pipeline with both a build leaf and test leaves carries both rollups:
    # build collapses to a status, test counts cells.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    rebuild_summary(doc)
    rocm = doc.summary.linux.rocm
    assert rocm.build.status is Status.success
    assert rocm.test.success == 1


def test_test_only_pipeline_rollup_does_not_leave_build_in_progress() -> None:
    # Delegated framework releases can report platform test leaves without a
    # matching build leaf for that same platform. The mandatory build projection
    # should reflect the completed test data, not look like live work.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(status=Status.success),
    )
    rebuild_summary(doc)
    assert doc.summary.linux.pytorch.build.status is Status.success
    assert doc.summary.linux.pytorch.test.success == 1
    # Only pytorch reported; rocm/jax/native are still unstarted, so the live
    # platform stays in_progress even though pytorch itself finished.
    assert doc.summary.linux.status is Status.in_progress


def test_multiple_pipelines_aggregate_into_platform_status() -> None:
    # rocm and pytorch both run on linux, but jax/native_packages are
    # independent sibling pipelines that have not reported anything yet.
    # rocm succeeds and pytorch's build fails -- each pipeline keeps its own
    # status -- but jax/native_packages might still succeed or fail, so the
    # platform must not crystallize to failure while they are still pending:
    # it stays in_progress (see rollup_sibling_statuses).
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "pytorch", "build", _leaf(status=Status.failure))
    rebuild_summary(doc)
    assert doc.summary.linux.rocm.build.status is Status.success
    assert doc.summary.linux.rocm.test.success == 1
    assert doc.summary.linux.pytorch.build.status is Status.failure
    assert doc.summary.linux.status is Status.in_progress


def test_multiple_pipelines_failure_wins_once_every_sibling_is_terminal() -> None:
    # Same rocm success + pytorch build failure, but jax and native_packages
    # have now also reported terminally: nothing on the platform is left
    # pending, so the worst-of (pytorch's failure) applies.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "pytorch", "build", _leaf(status=Status.failure))
    doc.upsert_leaf("linux", "", "jax", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "", "native_packages", "deb", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.linux.status is Status.failure


def test_overall_capped_in_progress_until_completed_at() -> None:
    # Every leaf is terminal (success), but with no completed_at the release is
    # not done: overall_status must stay in_progress.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.completed_at is None
    assert doc.summary.overall_status is Status.in_progress
    # The platform rollup is live worst-of, not capped. Here only rocm reported;
    # the still-unstarted pytorch/jax/native pipelines keep linux in_progress.
    assert doc.summary.linux.status is Status.in_progress


def test_overall_uncapped_once_completed_at_is_set() -> None:
    # Same terminal leaves, but completed_at present -> the worst-of rollup
    # stands and overall_status reflects the real result.
    doc = StatusDocument()
    doc.completed_at = "2026-04-08T02:00:00Z"
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.summary.overall_status is Status.success


def test_rebuild_summary_never_sets_completed_at() -> None:
    # rebuild_summary only reads completed_at to decide the cap; it must never
    # stamp it. completed_at is owned by the orchestrator finalize path.
    doc = StatusDocument()
    _freeze(doc, "linux", ["gfx942"])
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(status=Status.success))
    rebuild_summary(doc)
    assert doc.completed_at is None
