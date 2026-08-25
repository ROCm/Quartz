# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_status_document: the typed status.json v2 model.

The emphasis is on the two behaviors that are easy to get wrong and that
silently corrupt the document when they do:

  * matrix-variant merging across multiple workflow runs
    (`_merge_variant_leaf`), and
  * the don't-downgrade guard that decides whether a newly arrived run/leaf
    overwrites the one already in the tree (`should_replace` / `upsert_leaf`).
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from therock_status_document import (  # noqa: E402
    PipelineRollup,
    PlatformSummary,
    RunLeaf,
    Status,
    StatusDocument,
    Variant,
    _merge_variant_leaf,
    merge_matrix_test_leaf,
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


def _variant(
    *,
    matrix: dict[str, str] | None = None,
    status: Status = Status.success,
    run_id: int | None = None,
    run_attempt: int | None = 1,
    started_at: str | None = "2026-04-08T01:00:00Z",
    completed_at: str | None = "2026-04-08T01:30:00Z",
) -> Variant:
    return Variant(
        matrix=matrix or {},
        status=status,
        run_id=run_id,
        run_attempt=run_attempt,
        started_at=started_at,
        completed_at=completed_at,
    )


# --- Status -----------------------------------------------------------------


def test_only_in_progress_is_non_terminal() -> None:
    assert not Status.in_progress.is_terminal
    for terminal in (
        Status.success,
        Status.failure,
        Status.cancelled,
        Status.skipped,
    ):
        assert terminal.is_terminal


# --- Variant flat round-trip ------------------------------------------------


def test_variant_unflatten_lifts_matrix_axes_into_matrix() -> None:
    v = Variant.model_validate(
        {
            "py": "3.12",
            "torch": "2.5",
            "status": "success",
            "run_id": 7,
            "run_attempt": 2,
            "started_at": "2026-04-08T01:00:00Z",
            "completed_at": "2026-04-08T01:30:00Z",
        }
    )
    assert v.matrix == {"py": "3.12", "torch": "2.5"}
    assert v.status is Status.success
    assert v.run_id == 7
    assert v.run_attempt == 2


def test_variant_unflatten_coerces_non_string_axis_values() -> None:
    v = Variant.model_validate({"py": 3.12, "status": "success"})
    assert v.matrix == {"py": "3.12"}


def test_variant_unflatten_defaults_status_to_in_progress() -> None:
    v = Variant.model_validate({"py": "3.12"})
    assert v.status is Status.in_progress


def test_variant_serializer_omits_none_fields() -> None:
    # The serializer drops any unset optional field. The producer normally fills
    # run_id/run_attempt/started_at on test variants (see
    # therock_update_status_json._derive_variants), but the model tolerates a
    # bare {matrix, status} cell and projects only what is set.
    flat = Variant(matrix={"py": "3.12"}, status=Status.in_progress).model_dump()
    assert "run_id" not in flat
    assert "run_attempt" not in flat
    assert "started_at" not in flat
    assert "completed_at" not in flat


def test_test_phase_variant_emits_full_run_state() -> None:
    flat = Variant(
        matrix={"py": "3.12", "torch": "release/2.10"},
        status=Status.success,
        run_id=12345900,
        run_attempt=1,
        started_at="2026-04-08T11:30:00Z",
        completed_at="2026-04-08T12:15:00Z",
    ).model_dump()
    assert flat["py"] == "3.12"
    assert flat["torch"] == "release/2.10"
    assert flat["status"] == "success"
    assert flat["run_id"] == 12345900
    assert flat["run_attempt"] == 1
    assert flat["started_at"] == "2026-04-08T11:30:00Z"
    assert flat["completed_at"] == "2026-04-08T12:15:00Z"


def test_variant_flatten_drops_completed_at_without_started_at() -> None:
    # `completed_at` is emitted only inside the `started_at` guard.
    v = Variant(matrix={"py": "3.12"}, started_at=None, completed_at="2026-04-08Z")
    flat = v.model_dump()
    assert "started_at" not in flat
    assert "completed_at" not in flat


def test_variant_round_trips_through_serialization() -> None:
    original = _variant(matrix={"py": "3.12", "torch": "2.5"}, run_id=7, run_attempt=3)
    restored = Variant.model_validate(original.model_dump())
    assert restored.matrix == original.matrix
    assert restored.status == original.status
    assert restored.run_id == original.run_id
    assert restored.run_attempt == original.run_attempt
    assert restored.started_at == original.started_at
    assert restored.completed_at == original.completed_at


# --- Variant.key ------------------------------------------------------------


def test_variant_key_uses_sorted_matrix() -> None:
    v = _variant(matrix={"torch": "2.5", "py": "3.12"})
    assert v.key() == (("py", "3.12"), ("torch", "2.5"))


def test_variant_key_falls_back_to_run_id_without_matrix() -> None:
    assert _variant(matrix={}, run_id=42).key() == (("run_id", "42"),)


def test_variant_key_run_id_fallback_handles_none() -> None:
    assert _variant(matrix={}, run_id=None).key() == (("run_id", ""),)


# --- Variant.rollup_status (priority ordering) -------------------------------


def test_rollup_empty_returns_fallback() -> None:
    assert Variant.rollup_status([], Status.skipped) is Status.skipped


def test_rollup_failure_beats_in_progress() -> None:
    # A terminal failure must not be masked by a still-running sibling cell.
    variants = [
        _variant(status=Status.failure),
        _variant(status=Status.in_progress),
        _variant(status=Status.success),
    ]
    assert Variant.rollup_status(variants, Status.success) is Status.failure


def test_rollup_in_progress_beats_cancelled_and_success() -> None:
    # No failure present: ongoing work outranks terminal cancelled/success.
    variants = [
        _variant(status=Status.in_progress),
        _variant(status=Status.cancelled),
        _variant(status=Status.success),
    ]
    assert Variant.rollup_status(variants, Status.success) is Status.in_progress


def test_rollup_failure_beats_cancelled_and_success() -> None:
    variants = [
        _variant(status=Status.success),
        _variant(status=Status.cancelled),
        _variant(status=Status.failure),
    ]
    assert Variant.rollup_status(variants, Status.success) is Status.failure


def test_rollup_cancelled_beats_success() -> None:
    variants = [_variant(status=Status.success), _variant(status=Status.cancelled)]
    assert Variant.rollup_status(variants, Status.success) is Status.cancelled


def test_rollup_all_success_is_success() -> None:
    variants = [_variant(status=Status.success), _variant(status=Status.success)]
    assert Variant.rollup_status(variants, Status.in_progress) is Status.success


def test_rollup_all_skipped_is_skipped() -> None:
    variants = [_variant(status=Status.skipped), _variant(status=Status.skipped)]
    assert Variant.rollup_status(variants, Status.success) is Status.skipped


# --- is_terminal ------------------------------------------------------------


def test_leaf_terminal_via_completed_at_even_when_in_progress() -> None:
    leaf = _leaf(status=Status.in_progress, completed_at="2026-04-08Z")
    assert leaf.is_terminal()


def test_leaf_non_terminal_when_in_progress_and_no_completed_at() -> None:
    leaf = _leaf(status=Status.in_progress, completed_at=None)
    assert not leaf.is_terminal()


# --- should_replace: run_id ordering (newest run wins) ----------------------


def test_newer_run_id_replaces() -> None:
    existing = _leaf(run_id=111, run_attempt=1)
    new = _leaf(run_id=222, run_attempt=1)
    assert existing.should_replace(new)


def test_older_run_id_rejected() -> None:
    existing = _leaf(run_id=222, run_attempt=1)
    new = _leaf(run_id=111, run_attempt=1)
    assert not existing.should_replace(new)


def test_older_run_id_rejected_even_at_higher_attempt() -> None:
    # run_id dominates run_attempt: a deliberate re-run (attempt 2) of an older
    # run loses to an already-dispatched newer run. This is the semantic the
    # should_replace docstring calls out.
    existing = _leaf(run_id=222, run_attempt=1)
    new = _leaf(run_id=111, run_attempt=2)
    assert not existing.should_replace(new)


def test_equal_run_id_falls_through_to_attempt_then_terminal_guard() -> None:
    existing = _leaf(run_id=111, run_attempt=1)
    # Equal run_id -> arbitration falls through to run_attempt.
    assert existing.should_replace(_leaf(run_id=111, run_attempt=2))
    # Equal run_id and attempt -> the terminal don't-downgrade guard applies: a
    # non-terminal event cannot displace an already-terminal leaf of the same run.
    assert not existing.should_replace(
        _leaf(run_id=111, run_attempt=1, status=Status.in_progress, completed_at=None)
    )


def test_zero_sentinel_run_id_loses_to_real_run() -> None:
    # A 0 run_id (missing-id sentinel) sorts below any real id, so a real run
    # supersedes it and it never supersedes a real run.
    assert _leaf(run_id=0).should_replace(_leaf(run_id=500))
    assert not _leaf(run_id=500).should_replace(_leaf(run_id=0))


# Variant.should_replace shares the same run_id-first arbitration.
def test_variant_newer_run_id_replaces_even_at_lower_attempt() -> None:
    existing = _variant(run_id=111, run_attempt=2)
    new = _variant(run_id=222, run_attempt=1)
    assert existing.should_replace(new)


# --- should_replace: run_attempt ordering -----------------------------------


def test_higher_attempt_replaces() -> None:
    existing = _leaf(run_attempt=1)
    new = _leaf(run_attempt=2)
    assert existing.should_replace(new)


def test_lower_attempt_rejected() -> None:
    existing = _leaf(run_attempt=2)
    new = _leaf(run_attempt=1)
    assert not existing.should_replace(new)


def test_missing_attempt_treated_as_zero() -> None:
    existing = _leaf(run_attempt=None)  # -> 0
    new = _leaf(run_attempt=1)
    assert existing.should_replace(new)
    # And the reverse: a None-attempt new cannot displace an attempt-1 leaf.
    assert not _leaf(run_attempt=1).should_replace(_leaf(run_attempt=None))


# --- should_replace: same attempt, terminal guard ---------------------------


def test_same_attempt_terminal_not_downgraded_to_non_terminal() -> None:
    existing = _leaf(run_attempt=1, status=Status.success, completed_at="2026Z")
    new = _leaf(run_attempt=1, status=Status.in_progress, completed_at=None)
    assert not existing.should_replace(new)


def test_same_attempt_non_terminal_replaced_by_terminal() -> None:
    existing = _leaf(run_attempt=1, status=Status.in_progress, completed_at=None)
    new = _leaf(run_attempt=1, status=Status.success, completed_at="2026Z")
    assert existing.should_replace(new)


def test_same_attempt_terminal_replaced_by_terminal() -> None:
    # success -> failure correction within the same attempt is allowed.
    existing = _leaf(run_attempt=1, status=Status.success, completed_at="2026Z")
    new = _leaf(run_attempt=1, status=Status.failure, completed_at="2026Z")
    assert existing.should_replace(new)


# --- should_replace: terminal-by-status but missing completed_at -----------
#
# `is_terminal()` treats a terminal `status` (failure/success/...) as
# sufficient on its own, independent of `completed_at` -- a job's own
# conclusion is known the instant it finishes, before every sibling job in
# its cell/run has necessarily finished too. Two same-run/attempt snapshots
# can therefore both be "terminal" while differing in how much they actually
# know: a same-run re-derivation missing `completed_at` must not overwrite
# one that already had it.


def test_same_attempt_terminal_with_completed_at_beats_terminal_without() -> None:
    existing = _leaf(run_attempt=1, status=Status.failure, completed_at="2026Z")
    new = _leaf(run_attempt=1, status=Status.failure, completed_at=None)
    assert not existing.should_replace(new)


def test_same_attempt_terminal_without_completed_at_replaced_by_one_with() -> None:
    existing = _leaf(run_attempt=1, status=Status.failure, completed_at=None)
    new = _leaf(run_attempt=1, status=Status.failure, completed_at="2026Z")
    assert existing.should_replace(new)


def test_variant_same_attempt_terminal_with_completed_at_not_downgraded() -> None:
    existing = _variant(run_attempt=1, status=Status.failure, completed_at="2026Z")
    new = _variant(run_attempt=1, status=Status.failure, completed_at=None)
    assert not existing.should_replace(new)


# --- _merge_variant_leaf -----------------------------------------------


def test_merge_into_empty_keeps_all_new_variants() -> None:
    new = _leaf(
        variants=[
            _variant(matrix={"py": "3.11"}),
            _variant(matrix={"py": "3.12"}),
        ]
    )
    merged = _merge_variant_leaf(None, new)
    keys = {v.key() for v in merged.variants or []}
    assert keys == {(("py", "3.11"),), (("py", "3.12"),)}


def test_merge_appends_new_matrix_cells() -> None:
    existing = _leaf(variants=[_variant(matrix={"py": "3.11"})])
    new = _leaf(variants=[_variant(matrix={"py": "3.12"})])
    merged = _merge_variant_leaf(existing, new)
    keys = {v.key() for v in merged.variants or []}
    assert keys == {(("py", "3.11"),), (("py", "3.12"),)}


def test_merge_overwrites_same_cell_on_higher_attempt() -> None:
    existing = _leaf(
        variants=[_variant(matrix={"py": "3.11"}, run_attempt=1, status=Status.failure)]
    )
    new = _leaf(
        variants=[_variant(matrix={"py": "3.11"}, run_attempt=2, status=Status.success)]
    )
    merged = _merge_variant_leaf(existing, new)
    assert merged.variants is not None
    assert len(merged.variants) == 1
    assert merged.variants[0].run_attempt == 2
    assert merged.variants[0].status is Status.success


def test_merge_keeps_existing_cell_on_lower_attempt() -> None:
    existing = _leaf(
        variants=[_variant(matrix={"py": "3.11"}, run_attempt=2, status=Status.success)]
    )
    new = _leaf(
        variants=[_variant(matrix={"py": "3.11"}, run_attempt=1, status=Status.failure)]
    )
    merged = _merge_variant_leaf(existing, new)
    assert merged.variants is not None
    assert merged.variants[0].run_attempt == 2
    assert merged.variants[0].status is Status.success


def test_merge_does_not_downgrade_terminal_cell_same_attempt() -> None:
    existing = _leaf(
        variants=[
            _variant(
                matrix={"py": "3.11"},
                run_attempt=1,
                status=Status.success,
                completed_at="2026Z",
            )
        ]
    )
    new = _leaf(
        variants=[
            _variant(
                matrix={"py": "3.11"},
                run_attempt=1,
                status=Status.in_progress,
                completed_at=None,
            )
        ]
    )
    merged = _merge_variant_leaf(existing, new)
    assert merged.variants is not None
    assert merged.variants[0].status is Status.success


def test_merge_preserves_existing_cell_order_with_mixed_update() -> None:
    existing = _leaf(
        variants=[
            _variant(matrix={"py": "3.11"}, run_attempt=1),
            _variant(matrix={"py": "3.12"}, run_attempt=1),
        ]
    )
    # update 3.11, add 3.13, leave 3.12 untouched.
    new = _leaf(
        variants=[
            _variant(matrix={"py": "3.11"}, run_attempt=2),
            _variant(matrix={"py": "3.13"}, run_attempt=1),
        ]
    )
    merged = _merge_variant_leaf(existing, new)
    assert merged.variants is not None
    keys = [v.key() for v in merged.variants]
    assert keys == [(("py", "3.11"),), (("py", "3.12"),), (("py", "3.13"),)]
    assert merged.variants[0].run_attempt == 2
    assert merged.variants[1].run_attempt == 1
    assert merged.variants[2].run_attempt == 1


def test_merge_does_not_lose_completed_at_from_later_incomplete_snapshot() -> None:
    # Reproduces a real production race: multiple notify events for the SAME
    # shared-entry run each re-fetch and re-derive the *entire* job list. A
    # cell's own notify step necessarily observes its own job as still
    # running, so its own snapshot lacks a `completed_at` for itself; only a
    # *later* sibling cell's snapshot (fetched after this cell's job has
    # actually finished) captures it. If that later, complete snapshot's
    # merge is followed by processing an earlier, incomplete one for the SAME
    # cell (same run_id/attempt), the incomplete one must not erase the
    # `completed_at` already recorded.
    existing = _leaf(
        variants=[
            _variant(
                matrix={"py": "3.11"},
                run_id=901,
                run_attempt=1,
                status=Status.failure,
                completed_at="2026-04-08T01:10:00Z",
            )
        ]
    )
    stale_incomplete = _leaf(
        variants=[
            _variant(
                matrix={"py": "3.11"},
                run_id=901,
                run_attempt=1,
                status=Status.failure,
                completed_at=None,
            )
        ]
    )
    merged = _merge_variant_leaf(existing, stale_incomplete)
    assert merged.variants is not None
    assert merged.variants[0].completed_at == "2026-04-08T01:10:00Z"


def test_merge_status_rolls_up_failure() -> None:
    merged = _merge_variant_leaf(
        None,
        _leaf(
            variants=[
                _variant(matrix={"py": "3.11"}, status=Status.success),
                _variant(matrix={"py": "3.12"}, status=Status.failure),
            ]
        ),
    )
    assert merged.status is Status.failure


# --- merge_matrix_test_leaf: leaf-header identity ---------------------------


def test_matrix_merge_leaf_header_keeps_newer_run_over_stale_loser() -> None:
    # A stale older-run snapshot loses the push race but still reaches the
    # merge (the variant path bypasses RunLeaf.should_replace). Cells are held
    # by Variant.should_replace; the leaf header must not regress with them.
    existing = _leaf(
        run_id=200,
        run_attempt=1,
        variants=[_variant(matrix={"py": "3.11"}, run_id=200, run_attempt=1)],
    )
    new = _leaf(
        run_id=100,
        run_attempt=5,
        variants=[_variant(matrix={"py": "3.11"}, run_id=100, run_attempt=5)],
    )
    merged = merge_matrix_test_leaf(existing, new)
    assert merged.run_id == 200
    assert merged.run_attempt == 1
    assert merged.variants is not None
    assert merged.variants[0].run_id == 200


def test_matrix_merge_leaf_header_advances_to_newer_run() -> None:
    existing = _leaf(
        run_id=100,
        run_attempt=2,
        variants=[_variant(matrix={"py": "3.11"}, run_id=100, run_attempt=2)],
    )
    new = _leaf(
        run_id=200,
        run_attempt=1,
        variants=[_variant(matrix={"py": "3.11"}, run_id=200, run_attempt=1)],
    )
    merged = merge_matrix_test_leaf(existing, new)
    # newer run_id wins outright -- its (lower) attempt comes with it.
    assert merged.run_id == 200
    assert merged.run_attempt == 1


def test_matrix_merge_leaf_header_takes_higher_attempt_within_run() -> None:
    existing = _leaf(
        run_id=100,
        run_attempt=2,
        variants=[_variant(matrix={"py": "3.11"}, run_id=100, run_attempt=2)],
    )
    new = _leaf(
        run_id=100,
        run_attempt=1,
        variants=[_variant(matrix={"py": "3.11"}, run_id=100, run_attempt=1)],
    )
    merged = merge_matrix_test_leaf(existing, new)
    assert merged.run_id == 100
    assert merged.run_attempt == 2


# --- upsert_leaf: build phase -----------------------------------------------


def test_upsert_build_writes_then_overwrites_on_higher_attempt() -> None:
    doc = StatusDocument()
    assert doc.upsert_leaf("linux", "", "rocm", "build", _leaf(run_attempt=1))
    assert doc.upsert_leaf(
        "linux", "", "rocm", "build", _leaf(run_attempt=2, status=Status.failure)
    )
    assert doc.pipelines.rocm.build["linux"].run_attempt == 2
    assert doc.pipelines.rocm.build["linux"].status is Status.failure


def test_upsert_build_rejects_lower_attempt() -> None:
    doc = StatusDocument()
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf(run_attempt=2))
    assert not doc.upsert_leaf(
        "linux", "", "rocm", "build", _leaf(run_attempt=1, status=Status.failure)
    )
    assert doc.pipelines.rocm.build["linux"].run_attempt == 2


def test_upsert_build_does_not_downgrade_terminal_same_attempt() -> None:
    doc = StatusDocument()
    doc.upsert_leaf(
        "linux",
        "",
        "rocm",
        "build",
        _leaf(run_attempt=1, status=Status.success, completed_at="2026Z"),
    )
    assert not doc.upsert_leaf(
        "linux",
        "",
        "rocm",
        "build",
        _leaf(run_attempt=1, status=Status.in_progress, completed_at=None),
    )
    assert doc.pipelines.rocm.build["linux"].status is Status.success


def test_upsert_build_with_variants_merges_across_runs() -> None:
    doc = StatusDocument()
    doc.upsert_leaf(
        "linux",
        "",
        "pytorch",
        "build",
        _leaf(variants=[_variant(matrix={"py": "3.11"})], run_id=1),
    )
    doc.upsert_leaf(
        "linux",
        "",
        "pytorch",
        "build",
        _leaf(variants=[_variant(matrix={"py": "3.12"})], run_id=2),
    )
    leaf = doc.pipelines.pytorch.build["linux"]
    keys = {v.key() for v in leaf.variants or []}
    assert keys == {(("py", "3.11"),), (("py", "3.12"),)}


def test_upsert_build_with_variants_always_returns_true() -> None:
    # The variant path merges per-cell and bypasses the leaf-level guard.
    doc = StatusDocument()
    assert doc.upsert_leaf(
        "linux", "", "pytorch", "build", _leaf(variants=[_variant(matrix={"py": "3"})])
    )


def test_upsert_build_variant_less_completion_preserves_variants() -> None:
    # A later run-level-only report (no variants of its own) must not
    # discard matrix cells already accumulated from an earlier one.
    doc = StatusDocument()
    doc.upsert_leaf(
        "linux",
        "",
        "pytorch",
        "build",
        _leaf(variants=[_variant(matrix={"py": "3.11"})], run_id=1),
    )
    doc.upsert_leaf(
        "linux",
        "",
        "pytorch",
        "build",
        _leaf(run_id=1, status=Status.success),
    )
    leaf = doc.pipelines.pytorch.build["linux"]
    assert leaf.variants is not None
    keys = {v.key() for v in leaf.variants}
    assert keys == {(("py", "3.11"),)}


# --- upsert_leaf: test phase ------------------------------------------------


def test_upsert_test_routes_by_platform_and_arch() -> None:
    doc = StatusDocument()
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf())
    doc.upsert_leaf("linux", "gfx1100", "rocm", "test", _leaf())
    assert set(doc.pipelines.rocm.test["linux"]) == {"gfx942", "gfx1100"}


def test_upsert_test_full_does_not_collide_with_test() -> None:
    # The smoke `test` and full `test-full` phases share (platform, arch) but
    # land in separate subtrees, so neither clobbers the other.
    doc = StatusDocument()
    assert doc.upsert_leaf(
        "linux", "gfx942", "pytorch", "test", _leaf(run_id=10, status=Status.success)
    )
    assert doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test-full",
        _leaf(run_id=20, status=Status.failure),
    )
    assert doc.pipelines.pytorch.test["linux"]["gfx942"].run_id == 10
    assert doc.pipelines.pytorch.test_full["linux"]["gfx942"].run_id == 20
    assert doc.pipelines.pytorch.test["linux"]["gfx942"].status is Status.success
    assert doc.pipelines.pytorch.test_full["linux"]["gfx942"].status is Status.failure


def test_test_full_absent_from_projection_when_empty() -> None:
    # A pipeline that never ran a full suite must look exactly as before: no
    # `test_full` key added to the on-disk shape.
    doc = StatusDocument()
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf())
    projected = doc.to_dict()["pipelines"]["rocm"]
    assert "test_full" not in projected


def test_upsert_test_guard_is_per_arch() -> None:
    doc = StatusDocument()
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(run_attempt=2))
    # lower attempt for the same arch is rejected ...
    assert not doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(run_attempt=1))
    # ... but a different arch is independent.
    assert doc.upsert_leaf("linux", "gfx1100", "rocm", "test", _leaf(run_attempt=1))


def test_upsert_test_replaces_whole_leaf_with_variants_atomically() -> None:
    # A pytorch.test arch leaf is ONE workflow run; its matrix cells are jobs in
    # that run, carried on the leaf's `variants`. Like the build phase, test
    # leaves with variants are merged cell-by-cell (see `merge_matrix_test_leaf`)
    # rather than replaced wholesale. This is the rerun-of-failed-jobs case --
    # GitHub re-stamps every job with the new attempt, so every cell's higher
    # run_attempt wins the per-cell guard and the net effect looks atomic: all
    # attempt-1 values end up superseded. Re-running failed jobs keeps the
    # SAME run_id and only bumps run_attempt; the variants repeat the leaf's
    # run_id.
    run_id = 12345900
    doc = StatusDocument()
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            run_id=run_id,
            run_attempt=1,
            status=Status.failure,
            variants=[
                _variant(
                    matrix={"py": "3.11"},
                    run_id=run_id,
                    run_attempt=1,
                    status=Status.success,
                ),
                _variant(
                    matrix={"py": "3.12"},
                    run_id=run_id,
                    run_attempt=1,
                    status=Status.failure,
                ),
            ],
        ),
    )
    assert doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            run_id=run_id,
            run_attempt=2,
            status=Status.success,
            variants=[
                _variant(
                    matrix={"py": "3.11"},
                    run_id=run_id,
                    run_attempt=2,
                    status=Status.success,
                ),
                _variant(
                    matrix={"py": "3.12"},
                    run_id=run_id,
                    run_attempt=2,
                    status=Status.success,
                ),
            ],
        ),
    )
    leaf = doc.pipelines.pytorch.test["linux"]["gfx942"]
    assert leaf.run_id == run_id
    assert leaf.run_attempt == 2
    assert leaf.variants is not None
    # every cell now carries attempt 2 (same run_id) -- no attempt-1 remnants survive.
    assert {v.run_id for v in leaf.variants} == {run_id}
    assert {v.run_attempt for v in leaf.variants} == {2}
    assert all(v.status is Status.success for v in leaf.variants)


def test_upsert_test_with_variants_rejects_lower_attempt_per_cell() -> None:
    # The variant path merges per-cell (like build) and always reports the
    # write as accepted, but the per-cell guard still protects the actual
    # data: a stale lower-attempt snapshot cannot clobber a newer cell.
    doc = StatusDocument()
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            run_attempt=2,
            status=Status.success,
            variants=[_variant(matrix={"py": "3.11"}, run_attempt=2)],
        ),
    )
    assert doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(
            run_attempt=1,
            status=Status.failure,
            variants=[_variant(matrix={"py": "3.11"}, run_attempt=1)],
        ),
    )
    leaf = doc.pipelines.pytorch.test["linux"]["gfx942"]
    assert leaf.variants is not None
    assert leaf.variants[0].run_attempt == 2
    assert leaf.variants[0].status is Status.success


def test_upsert_test_with_variants_always_returns_true() -> None:
    # Mirrors test_upsert_build_with_variants_always_returns_true: the variant
    # path merges per-cell and bypasses the leaf-level guard.
    doc = StatusDocument()
    assert doc.upsert_leaf(
        "linux",
        "gfx942",
        "pytorch",
        "test",
        _leaf(variants=[_variant(matrix={"py": "3"})]),
    )


def test_upsert_test_merge_does_not_regress_completed_cell() -> None:
    # This is the push-race scenario the fix targets: two concurrent
    # `receive_therock_data.yml` runs fetch fresh job-list snapshots of the
    # SAME shared entry run at different wall-clock times. Snapshot A sees
    # py3.11 done but py3.12 still running; snapshot B (fetched slightly
    # earlier, but whose git push lands second) sees py3.11 still running but
    # py3.12 done. Regardless of push order, merging cell-by-cell must end up
    # with BOTH cells advanced -- never one snapshot regressing the other's
    # progress.
    doc = StatusDocument()
    run_id = 555
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "jax",
        "test",
        _leaf(
            run_id=run_id,
            run_attempt=1,
            status=Status.in_progress,
            completed_at=None,
            variants=[
                _variant(
                    matrix={"py": "3.11"},
                    run_id=run_id,
                    status=Status.success,
                    completed_at="2026-04-08T01:10:00Z",
                ),
                _variant(
                    matrix={"py": "3.12"},
                    run_id=run_id,
                    status=Status.in_progress,
                    completed_at=None,
                ),
            ],
        ),
    )
    # A stale snapshot lands next: py3.11 looks in_progress again (fetched
    # before it finished) but py3.12 has since completed.
    doc.upsert_leaf(
        "linux",
        "gfx942",
        "jax",
        "test",
        _leaf(
            run_id=run_id,
            run_attempt=1,
            status=Status.in_progress,
            completed_at=None,
            variants=[
                _variant(
                    matrix={"py": "3.11"},
                    run_id=run_id,
                    status=Status.in_progress,
                    completed_at=None,
                ),
                _variant(
                    matrix={"py": "3.12"},
                    run_id=run_id,
                    status=Status.success,
                    completed_at="2026-04-08T01:20:00Z",
                ),
            ],
        ),
    )
    leaf = doc.pipelines.jax.test["linux"]["gfx942"]
    assert leaf.variants is not None
    by_key = {v.key(): v for v in leaf.variants}
    assert by_key[(("py", "3.11"),)].status is Status.success
    assert by_key[(("py", "3.12"),)].status is Status.success
    # Both cells terminal -> the leaf-level rollup is terminal too.
    assert leaf.status is Status.success
    assert leaf.completed_at == "2026-04-08T01:20:00Z"


def test_upsert_test_newer_run_id_supersedes_via_upsert() -> None:
    # A fresh re-dispatch mints a NEW workflow run (larger run_id). The guard
    # compares run_id FIRST, so the newer run wins regardless of attempt -- here
    # the larger run_id short-circuits before run_attempt is even examined. (See
    # should_replace's run_id-first arbitration and its monotonic-id assumption.)
    doc = StatusDocument()
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(run_id=111, run_attempt=1))
    assert doc.upsert_leaf(
        "linux", "gfx942", "rocm", "test", _leaf(run_id=222, run_attempt=2)
    )
    leaf = doc.pipelines.rocm.test["linux"]["gfx942"]
    assert leaf.run_id == 222
    assert leaf.run_attempt == 2
    # And the newer run wins even at a LOWER attempt (run_id dominates attempt).
    assert doc.upsert_leaf(
        "linux", "gfx942", "rocm", "test", _leaf(run_id=333, run_attempt=1)
    )
    assert doc.pipelines.rocm.test["linux"]["gfx942"].run_id == 333


# --- upsert_leaf: native packages -------------------------------------------


def test_upsert_native_packages_routes_by_phase() -> None:
    doc = StatusDocument()
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf())
    doc.upsert_leaf("linux", "", "native_packages", "deb", _leaf())
    assert doc.native_packages.rpm is not None
    assert doc.native_packages.deb is not None


def test_upsert_native_packages_applies_guard() -> None:
    doc = StatusDocument()
    doc.upsert_leaf("linux", "", "native_packages", "rpm", _leaf(run_attempt=2))
    assert not doc.upsert_leaf(
        "linux", "", "native_packages", "rpm", _leaf(run_attempt=1)
    )
    assert doc.native_packages.rpm is not None
    assert doc.native_packages.rpm.run_attempt == 2


# --- PlatformSummary.for_platform -------------------------------------------


def test_for_platform_linux_initializes_linux_only_pipelines() -> None:
    summary = PlatformSummary.for_platform("linux")
    assert summary.jax is not None
    assert summary.native_packages is not None
    # native_packages always carries both rpm and deb once present.
    assert summary.native_packages.rpm is not None
    assert summary.native_packages.deb is not None


def test_for_platform_windows_leaves_linux_only_pipelines_none() -> None:
    summary = PlatformSummary.for_platform("windows")
    assert summary.jax is None
    assert summary.native_packages is None
    # rocm/pytorch are always present on both platforms.
    assert summary.rocm is not None
    assert summary.pytorch is not None


def test_for_platform_explicit_field_overrides_default() -> None:
    custom = PipelineRollup()
    summary = PlatformSummary.for_platform("linux", jax=custom)
    assert summary.jax is custom


def test_default_summary_honors_per_platform_contract() -> None:
    # A default-constructed document (before any rebuild_summary) must already
    # carry the per-platform shape, not a uniform windows-style default.
    summary = StatusDocument().summary
    assert summary.linux.jax is not None
    assert summary.linux.native_packages is not None
    assert summary.windows.jax is None
    assert summary.windows.native_packages is None


# --- to_dict / serialization projection -------------------------------------


def test_to_dict_strips_none_and_empty_pipelines() -> None:
    doc = StatusDocument(rocm_version="7.0.0a20260408")
    doc.upsert_leaf("linux", "", "rocm", "build", _leaf())
    out = doc.to_dict()

    pipelines = out["pipelines"]
    assert isinstance(pipelines, dict)
    # Only the populated pipeline survives; empty ones are dropped.
    assert set(pipelines) == {"rocm"}

    leaf = pipelines["rocm"]["build"]["linux"]
    assert "variants" not in leaf  # None -> stripped
    # Positive control: a set field must survive the strip. `success` is the
    # _leaf() fixture default, not a model default -- RunLeaf.status is required.
    assert leaf["status"] == "success"


def test_to_dict_keeps_zero_counts_in_summary() -> None:
    # `_strip_none` drops None but must keep 0 counts. Round-trip a summary that
    # carries an all-zero test rollup and confirm the zeros survive.
    doc = StatusDocument.from_dict(
        {
            "schema_version": "2.0",
            "rocm_version": "7.0.0",
            "summary": {
                "linux": {
                    "rocm": {
                        "build": {"status": "success"},
                        "test": {
                            "success": 0,
                            "failure": 0,
                            "in_progress": 0,
                            "cancelled": 0,
                            "skipped": 0,
                        },
                    }
                }
            },
        }
    )
    test_counts = doc.to_dict()["summary"]["linux"]["rocm"]["test"]
    assert test_counts["success"] == 0
    assert test_counts["skipped"] == 0


# --- from_dict / _from_wire round-trip --------------------------------------


def test_from_wire_lifts_arch_and_urls_out_of_summary() -> None:
    doc = StatusDocument.from_dict(
        {
            "schema_version": "2.0",
            "rocm_version": "7.0.0",
            "summary": {
                "linux": {
                    "architectures": ["gfx942", "gfx1100"],
                    "urls": {
                        "tarballs": "https://nightly.repo.amd.com/rocm/core/tarball/",
                        "wheels": "https://nightly.repo.amd.com/rocm/whl-next/",
                    },
                },
                "windows": {"architectures": ["gfx1201"]},
            },
        }
    )
    assert doc.linux_architectures == ["gfx942", "gfx1100"]
    assert doc.linux_urls == {
        "tarballs": "https://nightly.repo.amd.com/rocm/core/tarball/",
        "wheels": "https://nightly.repo.amd.com/rocm/whl-next/",
    }
    assert doc.windows_architectures == ["gfx1201"]


def test_document_round_trips_through_to_dict_and_from_dict() -> None:
    doc = StatusDocument(rocm_version="7.0.0a20260408")
    doc.upsert_leaf(
        "linux",
        "",
        "pytorch",
        "build",
        _leaf(variants=[_variant(matrix={"py": "3.12"}, status=Status.success)]),
    )
    doc.upsert_leaf("linux", "gfx942", "rocm", "test", _leaf(status=Status.failure))

    restored = StatusDocument.from_dict(doc.to_dict())
    assert restored.rocm_version == "7.0.0a20260408"
    assert restored.pipelines.rocm.test["linux"]["gfx942"].status is Status.failure
    pt_leaf = restored.pipelines.pytorch.build["linux"]
    assert pt_leaf.variants is not None
    assert pt_leaf.variants[0].matrix == {"py": "3.12"}
