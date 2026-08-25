# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_update_status_json.

Two groups:
  - The document-level finalize path: a leaf event leaves the release
    `in_progress` (overall_status capped) and writes no `completed_at`, while
    the top-level release orchestrator's completed event stamps `completed_at`
    and uncaps `overall_status`. Per-platform orchestrators and an in-progress
    top-level orchestrator carry no document-level signal and are skipped.
  - The candidacy gate + write side effects: each non-qualifying payload
    returns None without touching disk; a missing-architectures leaf raises;
    nightly writes create the `latest.json` symlink (and `latest_good.json`
    only once the release finalizes successfully); prerelease routes to its own
    tree; and successive leaves merge into one document.

All cases run with `commit_and_push=False` (apply-on-disk dry run) so no git
operations happen, and `_utc_now` is stubbed to keep the clock check
deterministic and offline (no NTP).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import therock_update_status_json as tusj  # noqa: E402
from therock_status_document import Status, StatusDocument  # noqa: E402
from therock_types import (  # noqa: E402
    ORCHESTRATOR_SPECS,
    Classification,
    TheRockDispatchEvent,
    WorkflowJobRecord,
    WorkflowRunRecord,
)

_RELEASE_VERSION = "7.14.0a20260619"
_NIGHTLY_DATE = "20260619"


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # _utc_now() would otherwise hit NTP over the network and validate against
    # the run timestamps. Freeze it well after the fixture timestamps so the
    # drift guard always passes and the test stays offline.
    monkeypatch.setattr(
        tusj,
        "_utc_now",
        lambda: datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc),
    )


def _run(
    *,
    path: str,
    platform: str,
    pipeline_type: str,
    pipeline_phase: str,
    architectures: list[str],
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=27797822902,
        run_number=1,
        run_attempt=1,
        name="run",
        display_title="run",
        trigger_event="workflow_dispatch",
        path=path,
        status="completed",
        conclusion="success",
        head_branch="main",
        head_sha="deadbeef",
        workflow_id=1,
        html_url="https://example/runs/1",
        created_at=datetime(2026, 6, 19, 15, 8, tzinfo=timezone.utc),
        run_started_at=datetime(2026, 6, 19, 15, 8, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 19, 15, 18, tzinfo=timezone.utc),
        actor_login="octocat",
        pr_number=None,
        pr_title=None,
        release_type="nightly",
        rocm_version=_RELEASE_VERSION,
        inputs={},
        env={},
        parent_workflow=None,
        referenced_workflows=[],
        trigger_workflow_run_id=None,
        jobs=[],
        classification=Classification(
            platform=platform,
            pipeline_type=pipeline_type,
            pipeline_phase=pipeline_phase,
            architectures=architectures,
            release_version=_RELEASE_VERSION,
        ),
    )


def _leaf_run() -> WorkflowRunRecord:
    return _run(
        path=".github/workflows/multi_arch_build_portable_linux.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="build",
        architectures=["gfx942"],
    )


def _orchestrator_run(
    path: str = ".github/workflows/multi_arch_release.yml",
) -> WorkflowRunRecord:
    # Orchestrators classify to pipeline_type=orchestrator with a phase/platform
    # from ORCHESTRATOR_SPECS; the router keys off that tuple to finalize.
    spec = ORCHESTRATOR_SPECS[path.rsplit("/", 1)[-1]]
    return _run(
        path=path,
        platform=spec.platform,
        pipeline_type=spec.pipeline_type,
        pipeline_phase=spec.pipeline_phase,
        architectures=[],
    )


def _windows_leaf_run() -> WorkflowRunRecord:
    return _run(
        path=".github/workflows/multi_arch_build_windows.yml",
        platform="windows",
        pipeline_type="rocm",
        pipeline_phase="build",
        architectures=["gfx942"],
    )


_PRERELEASE_VERSION = "7.14.0rc1"


def _prerelease_leaf_run() -> WorkflowRunRecord:
    run = _leaf_run()
    run.release_type = "prerelease"
    run.rocm_version = _PRERELEASE_VERSION
    run.classification.release_version = _PRERELEASE_VERSION
    return run


def _event(
    workflow_run: WorkflowRunRecord,
    *,
    event_type: str = "workflow_run_completed",
    repository: str = "ROCm/rockrel",
) -> TheRockDispatchEvent:
    return TheRockDispatchEvent(
        event_type=event_type,
        repository=repository,
        action="",
        workflow_run=workflow_run,
        pull_request=None,
        push_event=None,
        raw={},
    )


def _load(status_path: Path) -> StatusDocument:
    return StatusDocument.from_dict(json.loads(status_path.read_text(encoding="utf-8")))


def _nightly_status_path(repo_dir: Path) -> Path:
    return repo_dir / "release-nightly" / _NIGHTLY_DATE / "status.json"


def _establish_owner(
    repo_dir: Path,
    run_id: int = 27797822902,
    *,
    release_type: str = "nightly",
    version: str = _RELEASE_VERSION,
) -> None:
    """Anchor document ownership the way the real pipeline does.

    The strict leaf gate drops any leaf that reaches an ownerless document, so
    ownership must be established first. The top-level orchestrator's
    in-progress event (or the setup run) is the only thing that records the
    owning run. A higher run id here takes over, resetting the previous run's
    detail -- the same reset a newer release run triggers in production.
    """
    orch = _orchestrator_run()
    orch.workflow_run_id = run_id
    orch.conclusion = None
    orch.status = "in_progress"
    orch.release_type = release_type
    orch.rocm_version = version
    orch.classification.release_version = version
    tusj.update_status_json(
        _event(orch, event_type="workflow_run_in_progress"),
        repo_dir=repo_dir,
        commit_and_push=False,
    )


def _setup_run(
    run_id: int,
    *,
    build_variant: str = "release",
    release_type: str = "nightly",
    version: str = _RELEASE_VERSION,
) -> WorkflowRunRecord:
    """A completed setup_multi_arch.yml run. It executes via `workflow_call`, so
    its run id equals the top-level orchestrator's -- letting it anchor
    ownership before any leaf lands. `build_variant='asan'` must never own the
    normal release document."""
    run = _run(
        path=".github/workflows/setup_multi_arch.yml",
        platform="",
        pipeline_type="setup",
        pipeline_phase="setup",
        architectures=[],
    )
    run.workflow_run_id = run_id
    run.release_type = release_type
    run.rocm_version = version
    run.classification.release_version = version
    run.classification.build_variant = build_variant
    return run


def test_leaf_event_leaves_release_in_progress(tmp_path: Path) -> None:
    _establish_owner(tmp_path)
    out = tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    assert out == _nightly_status_path(tmp_path)
    doc = _load(out)
    # A terminal build leaf alone does not end the release: capped + no stamp.
    assert doc.completed_at is None
    assert doc.summary.overall_status is Status.in_progress
    # ... but the platform rollup reflects the finished build.
    assert doc.summary.linux.rocm.build.status is Status.success


def test_rocm_test_events_populate_test_rollup(tmp_path: Path) -> None:
    # Seed the platform with a rocm build leaf, then deliver two per-arch
    # `test_artifacts.yml` runs (rocm/test). Each arch contributes one entry to
    # the rocm.test counters (no variants).
    _establish_owner(tmp_path)
    tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    passed = _run(
        path=".github/workflows/test_artifacts.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    failed = _run(
        path=".github/workflows/test_artifacts.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx1201"],
    )
    failed.conclusion = "failure"
    failed.classification.pipeline_phase = "test"

    tusj.update_status_json(_event(passed), repo_dir=tmp_path, commit_and_push=False)
    out = tusj.update_status_json(
        _event(failed), repo_dir=tmp_path, commit_and_push=False
    )

    doc = _load(out)
    test_rollup = doc.summary.linux.rocm.test
    assert test_rollup.success == 1
    assert test_rollup.failure == 1
    assert test_rollup.in_progress == 0
    # Per-arch leaves land under pipelines.rocm.test[linux][arch].
    assert set(doc.pipelines.rocm.test["linux"].keys()) == {"gfx942", "gfx1201"}


def test_version_less_test_event_routes_by_run_date(tmp_path: Path) -> None:
    # Test workflows are dispatched without a version input, so their events
    # carry no release_version. Routing falls back to the run date (created_at),
    # landing in the same dated document a build leaf seeded.
    _establish_owner(tmp_path)
    tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    test_run = _run(
        path=".github/workflows/test_artifacts.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    test_run.rocm_version = ""
    test_run.classification.release_version = ""

    out = tusj.update_status_json(
        _event(test_run), repo_dir=tmp_path, commit_and_push=False
    )
    assert out == _nightly_status_path(tmp_path)
    doc = _load(out)
    # The build leaf's version is preserved; the test rollup is populated.
    assert doc.rocm_version == _RELEASE_VERSION
    assert doc.summary.linux.rocm.test.success == 1


def test_version_less_in_progress_test_event_does_not_crash(tmp_path: Path) -> None:
    # The started (in_progress) event also carries no version; it must route by
    # run date instead of raising on an empty release_version.
    test_run = _run(
        path=".github/workflows/test_artifacts.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    test_run.status = "in_progress"
    test_run.conclusion = None
    test_run.rocm_version = ""
    test_run.classification.release_version = ""

    out = tusj.update_status_json(
        _event(test_run, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert out == _nightly_status_path(tmp_path)


def test_native_install_test_event_is_skipped(tmp_path: Path) -> None:
    # Native install tests are async sanity checks with no per-arch payload and
    # no native_packages test slot; they must be skipped, not raise.
    native_test = _run(
        path=".github/workflows/test_native_linux_packages_install.yml",
        platform="linux",
        pipeline_type="native_packages",
        pipeline_phase="test",
        architectures=[],
    )
    out = tusj.update_status_json(
        _event(native_test), repo_dir=tmp_path, commit_and_push=False
    )
    assert out is None


def test_orchestrator_completed_finalizes_release(tmp_path: Path) -> None:
    # Seed the document with the build leaf, then deliver the orchestrator's
    # completed event for the same release.
    tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    out = tusj.update_status_json(
        _event(_orchestrator_run()), repo_dir=tmp_path, commit_and_push=False
    )
    assert out == _nightly_status_path(tmp_path)
    doc = _load(out)
    # The orchestrator's completed event stamps completed_at (run end time) and
    # uncaps overall_status.
    assert doc.completed_at == "2026-06-19T15:18:00Z"
    assert doc.summary.overall_status is Status.success


def test_orchestrator_start_then_finalize_records_owner_then_completes(
    tmp_path: Path,
) -> None:
    # Orchestrator lifecycle: a leaf reports, the top-level orchestrator's in-progress
    # event stamps the owning run id (orchestrator still running), then that same
    # orchestrator's completed event finalizes the document.
    tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )

    orch = _orchestrator_run()
    orch.workflow_run_id = 29079513704
    orch.conclusion = None
    orch.status = "in_progress"
    orch.updated_at = None
    out = tusj.update_status_json(
        _event(orch, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert out == _nightly_status_path(tmp_path)
    doc = _load(out)
    assert doc.trigger_workflow_run_id == 29079513704
    assert doc.completed_at is None
    assert doc.summary.overall_status is Status.in_progress

    done = _orchestrator_run()
    done.workflow_run_id = 29079513704
    out = tusj.update_status_json(
        _event(done, event_type="workflow_run_completed"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    doc = _load(out)
    assert doc.trigger_workflow_run_id == 29079513704
    assert doc.completed_at == "2026-06-19T15:18:00Z"
    assert doc.summary.overall_status is Status.success


def test_leaf_does_not_stamp_orchestrator_owner(tmp_path: Path) -> None:
    # The owner id is the top-level orchestrator run only; a leaf's immediate
    # parent (e.g. a per-platform orchestrator) must never be recorded as it.
    # Under the strict gate a leaf whose parent differs from the owner is dropped
    # outright -- it neither takes over ownership nor lands in the document.
    _establish_owner(tmp_path)
    leaf = _leaf_run()
    leaf.trigger_workflow_run_id = 123456
    tusj.update_status_json(_event(leaf), repo_dir=tmp_path, commit_and_push=False)
    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_workflow_run_id == 27797822902
    assert "linux" not in doc.pipelines.rocm.build


def test_older_orchestrator_start_does_not_override_newer_owner(
    tmp_path: Path,
) -> None:
    newer = _orchestrator_run()
    newer.workflow_run_id = 29079513704
    tusj.update_status_json(
        _event(newer, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    older = _orchestrator_run()
    older.workflow_run_id = 29079225784
    tusj.update_status_json(
        _event(older, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_workflow_run_id == 29079513704


def test_ownerless_non_pytorch_leaf_is_rejected(
    tmp_path: Path,
) -> None:
    # An ownerless leaf that is NOT a pytorch run cannot be admitted by version
    # match: only `pipeline_type == "pytorch"` may use the version-match rescue,
    # since pytorch is the sole pipeline that carries no derivable owner. A rocm
    # leaf without a derivable owner whose run id is not the owner is dropped.
    _establish_owner(tmp_path, 29079513704)

    ownerless_test = _run(
        path=".github/workflows/test_artifacts.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    ownerless_test.workflow_run_id = 99999999999
    tusj.update_status_json(
        _event(ownerless_test), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(_nightly_status_path(tmp_path))
    assert doc.summary.linux.rocm.test.success == 0
    # The rejected leaf must not disturb the document's recorded owner.
    assert doc.trigger_workflow_run_id == 29079513704


def test_ownerless_pytorch_leaf_lands_on_version_match(
    tmp_path: Path,
) -> None:
    # test_pytorch_wheels_full.yml carries only the rocm version (inside the torch
    # version), no run id or artifact URL to derive a parent from, so
    # trigger_workflow_run_id is None. asan does not run pytorch, so a pytorch leaf
    # whose release version matches the document is a legit normal/prerelease run
    # and is admitted via the version-match rescue.
    _establish_owner(tmp_path, 29079513704)

    pytorch_test = _run(
        path=".github/workflows/test_pytorch_wheels_full.yml",
        platform="linux",
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    pytorch_test.workflow_run_id = 99999999999
    tusj.update_status_json(
        _event(pytorch_test), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(_nightly_status_path(tmp_path))
    assert doc.summary.linux.pytorch.test.success == 1
    # The admitted leaf must not take over the document's recorded owner.
    assert doc.trigger_workflow_run_id == 29079513704


def test_leaf_without_owner_and_mismatched_release_version_is_skipped(
    tmp_path: Path,
) -> None:
    # A pytorch leaf is eligible for the version-match rescue, but here its
    # release_version does not match the document's: even though it routes to the
    # same dated file (the nightly path suffix is just the embedded date), it must
    # still be rejected since it isn't provably this release.
    orch = _orchestrator_run()
    orch.workflow_run_id = 29079513704
    orch.conclusion = None
    orch.status = "in_progress"
    tusj.update_status_json(
        _event(orch, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )

    mismatched_test = _run(
        path=".github/workflows/test_pytorch_wheels_full.yml",
        platform="linux",
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    mismatched_test.workflow_run_id = 99999999999
    mismatched_test.classification.release_version = "7.15.0a20260619"
    out = tusj.update_status_json(
        _event(mismatched_test), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(out)
    assert doc.summary.linux.pytorch.test.success == 0
    assert doc.trigger_workflow_run_id == 29079513704


def test_per_platform_orchestrator_does_not_finalize(tmp_path: Path) -> None:
    _establish_owner(tmp_path)
    tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    out = tusj.update_status_json(
        _event(_orchestrator_run(".github/workflows/multi_arch_release_linux.yml")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    # Per-platform orchestrators fan out under the top-level one; they own no
    # document-level completion signal.
    assert out is None
    doc = _load(_nightly_status_path(tmp_path))
    assert doc.completed_at is None


def test_prerelease_platform_orchestrator_replaces_s3_urls_with_cdn(
    tmp_path: Path,
) -> None:
    _establish_owner(tmp_path, release_type="prerelease", version=_PRERELEASE_VERSION)
    raw_s3 = "https://therock-prerelease-artifacts.s3.amazonaws.com/27797822902-linux"
    build = _prerelease_leaf_run()
    build.tarball_url = f"{raw_s3}/tarballs/"
    build.wheels_url = f"{raw_s3}/python/"
    tusj.update_status_json(_event(build), repo_dir=tmp_path, commit_and_push=False)

    release = _orchestrator_run(".github/workflows/multi_arch_release_linux.yml")
    release.release_type = "prerelease"
    release.rocm_version = _PRERELEASE_VERSION
    release.classification.release_version = _PRERELEASE_VERSION
    release.tarball_url = "https://rocm.prereleases.amd.com/tarball-multi-arch/"
    release.wheels_url = "https://rocm.prereleases.amd.com/whl-multi-arch/"
    release.rpm_urls = {"rpm": "https://rocm.prereleases.amd.com/packages-multi-arch/"}
    release.deb_urls = {"deb": "https://rocm.prereleases.amd.com/packages-multi-arch/"}

    out = tusj.update_status_json(
        _event(release), repo_dir=tmp_path, commit_and_push=False
    )

    assert out == tmp_path / "prereleases" / "7.14.0" / "7.14.0rc1" / "status.json"
    doc = _load(out)
    assert doc.completed_at is None
    assert doc.summary.overall_status is Status.in_progress
    assert doc.summary.linux.urls == {
        "tarballs": "https://rocm.prereleases.amd.com/tarball-multi-arch/",
        "wheels": "https://rocm.prereleases.amd.com/whl-multi-arch/",
        "rpm": "https://rocm.prereleases.amd.com/packages-multi-arch/",
        "deb": "https://rocm.prereleases.amd.com/packages-multi-arch/",
    }


def test_orchestrator_without_release_version_is_skipped(tmp_path: Path) -> None:
    run = _orchestrator_run()
    run.classification.release_version = ""
    out = tusj.update_status_json(_event(run), repo_dir=tmp_path, commit_and_push=False)
    assert out is None


def test_from_dict_resolves_version_from_captured_setup_output() -> None:
    # The top-level orchestrator has no version input of its own; it recovers
    # the release version from its captured `setup` job output so the completed
    # event can route to a status.json.
    wr = WorkflowRunRecord.from_dict(
        {
            "id": 1,
            "path": ".github/workflows/multi_arch_release.yml",
            "release_type": "nightly",
            "captured_outputs": {
                "setup": {
                    "result": "success",
                    "outputs": {"rocm_package_version": _RELEASE_VERSION},
                },
                "linux_release": {"result": "success", "outputs": {}},
            },
        }
    )
    assert wr.rocm_version == _RELEASE_VERSION


# --- candidacy gate: non-qualifying payloads return None, write nothing ---


def test_untracked_event_type_is_skipped(tmp_path: Path) -> None:
    # `workflow_run_requested` is a known lifecycle event but not one the
    # updater tracks (only in_progress / completed are).
    out = tusj.update_status_json(
        _event(_leaf_run(), event_type="workflow_run_requested"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert out is None
    assert list(tmp_path.iterdir()) == []


def test_wrong_repository_is_skipped(tmp_path: Path) -> None:
    out = tusj.update_status_json(
        _event(_leaf_run(), repository="ROCm/TheRock"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert out is None
    assert list(tmp_path.iterdir()) == []


def test_repository_match_is_case_insensitive(tmp_path: Path) -> None:
    out = tusj.update_status_json(
        _event(_leaf_run(), repository="ROCm/RockRel"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert out == _nightly_status_path(tmp_path)


def test_dev_release_type_is_skipped(tmp_path: Path) -> None:
    run = _leaf_run()
    run.release_type = "dev"
    out = tusj.update_status_json(_event(run), repo_dir=tmp_path, commit_and_push=False)
    assert out is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "release_version",
    ["7.13.0.dev0", "7.13.0.dev0+g1234abc", "7.13.0dev0"],
)
def test_release_version_suffix_rejects_dev_builds(release_version: str) -> None:
    # The safety net: a dev-formatted release_version that reaches
    # _release_version_suffix (release_type unset, so the earlier "dev" gate did
    # not catch it) must raise rather than route to a release status.json.
    with pytest.raises(ValueError, match="dev build"):
        tusj._release_version_suffix(release_version)


@pytest.mark.parametrize(
    "release_version,expected",
    [("7.13.0a20260415", "20260415"), ("7.13.0rc1", "rc1")],
)
def test_release_version_suffix_extracts_release_suffixes(
    release_version: str, expected: str
) -> None:
    assert tusj._release_version_suffix(release_version) == expected


def test_unclassified_leaf_is_skipped(tmp_path: Path) -> None:
    # A non-orchestrator path that classified to an empty platform/pipeline
    # (e.g. an unmatched fan-out) is not a release pipeline -> skip.
    run = _leaf_run()
    run.classification.platform = ""
    run.classification.pipeline_type = ""
    out = tusj.update_status_json(_event(run), repo_dir=tmp_path, commit_and_push=False)
    assert out is None
    assert list(tmp_path.iterdir()) == []


def test_leaf_without_architectures_raises(tmp_path: Path) -> None:
    run = _leaf_run()
    run.classification.architectures = []
    with pytest.raises(ValueError, match="no\\s+architectures"):
        tusj.update_status_json(_event(run), repo_dir=tmp_path, commit_and_push=False)


# --- write side effects: symlinks, prerelease routing, merge -------------


def test_nightly_leaf_creates_latest_symlink_but_not_latest_good(
    tmp_path: Path,
) -> None:
    _establish_owner(tmp_path)
    out = tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    assert out == _nightly_status_path(tmp_path)

    latest = tmp_path / "release-nightly" / "latest.json"
    assert latest.is_symlink()
    assert latest.readlink().parts[0] == _NIGHTLY_DATE

    # latest_good only tracks a successful release; the leaf alone keeps the
    # release capped at in_progress, so no snapshot yet.
    assert not (tmp_path / "release-nightly" / "latest_good.json").exists()


def test_finalized_release_writes_latest_good_snapshot(tmp_path: Path) -> None:
    tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    tusj.update_status_json(
        _event(_orchestrator_run()), repo_dir=tmp_path, commit_and_push=False
    )

    latest_good = tmp_path / "release-nightly" / "latest_good.json"
    assert latest_good.exists()
    assert not latest_good.is_symlink()  # snapshot file, not a symlink
    snapshot = StatusDocument.from_dict(
        json.loads(latest_good.read_text(encoding="utf-8"))
    )
    assert snapshot.summary.overall_status is Status.success
    assert snapshot.build_date == _NIGHTLY_DATE


def _prerelease_leaf_run_version(version: str) -> WorkflowRunRecord:
    run = _prerelease_leaf_run()
    run.rocm_version = version
    run.classification.release_version = version
    return run


def test_prerelease_routes_to_nested_version_tree(tmp_path: Path) -> None:
    out = tusj.update_status_json(
        _event(_prerelease_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    # prereleases/<base>/<full>/status.json
    assert out == tmp_path / "prereleases" / "7.14.0" / "7.14.0rc1" / "status.json"
    assert not (tmp_path / "release-nightly").exists()


def test_prerelease_creates_latest_symlink(tmp_path: Path) -> None:
    _establish_owner(tmp_path, release_type="prerelease", version=_PRERELEASE_VERSION)
    tusj.update_status_json(
        _event(_prerelease_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    latest = tmp_path / "prereleases" / "latest.json"
    assert latest.is_symlink()
    assert latest.readlink() == Path("7.14.0/7.14.0rc1/status.json")
    # prerelease has no notion of latest_good.
    assert not (tmp_path / "prereleases" / "latest_good.json").exists()


def test_prerelease_latest_advances_to_newer_candidate(tmp_path: Path) -> None:
    _establish_owner(tmp_path, release_type="prerelease", version="7.14.0rc1")
    tusj.update_status_json(
        _event(_prerelease_leaf_run_version("7.14.0rc1")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    _establish_owner(tmp_path, release_type="prerelease", version="7.14.0rc2")
    tusj.update_status_json(
        _event(_prerelease_leaf_run_version("7.14.0rc2")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    latest = tmp_path / "prereleases" / "latest.json"
    assert latest.readlink() == Path("7.14.0/7.14.0rc2/status.json")


def test_prerelease_latest_does_not_regress_to_older_candidate(
    tmp_path: Path,
) -> None:
    # rc10 is numerically newer than rc2 even though it sorts lower lexically.
    _establish_owner(tmp_path, release_type="prerelease", version="7.14.0rc10")
    tusj.update_status_json(
        _event(_prerelease_leaf_run_version("7.14.0rc10")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    _establish_owner(tmp_path, release_type="prerelease", version="7.14.0rc2")
    tusj.update_status_json(
        _event(_prerelease_leaf_run_version("7.14.0rc2")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    latest = tmp_path / "prereleases" / "latest.json"
    assert latest.readlink() == Path("7.14.0/7.14.0rc10/status.json")


def test_successive_leaves_merge_into_one_document(tmp_path: Path) -> None:
    _establish_owner(tmp_path)
    out = tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    tusj.update_status_json(
        _event(_windows_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(out)
    # Both platform builds landed in the same release document.
    assert doc.summary.linux.rocm.build.status is Status.success
    assert doc.summary.windows.rocm.build.status is Status.success


# --- platform artifact URLs: gated by leaf acceptance, pinned to one run -----


def _s3_base(run_id: int) -> str:
    return f"https://therock-nightly-artifacts.s3.amazonaws.com/{run_id}-linux"


def _linux_build_with_urls(
    run_id: int,
    *,
    attempt: int = 1,
    conclusion: str = "success",
    parent_run_id: int | None = None,
) -> WorkflowRunRecord:
    run = _leaf_run()
    run.workflow_run_id = run_id
    run.run_attempt = attempt
    run.conclusion = conclusion
    run.trigger_workflow_run_id = parent_run_id
    base = _s3_base(run_id)
    run.tarball_url = f"{base}/tarballs/"
    run.wheels_url = f"{base}/python/"
    run.artifacts_url = f"{base}/index.html"
    return run


def _linux_native_rpm_with_urls(
    run_id: int, *, attempt: int = 1, parent_run_id: int | None = None
) -> WorkflowRunRecord:
    run = _run(
        path=".github/workflows/multi_arch_build_native_linux_packages.yml",
        platform="linux",
        pipeline_type="native_packages",
        pipeline_phase="rpm",
        architectures=["gfx942"],
    )
    run.workflow_run_id = run_id
    run.run_attempt = attempt
    run.trigger_workflow_run_id = parent_run_id
    run.rpm_urls = {"rpm": f"{_s3_base(run_id)}/rpm/"}
    return run


def test_urls_pin_to_build_run_and_clear_stale_on_supersede(tmp_path: Path) -> None:
    # Run A owns the document: its rocm build + native rpm form one URL block.
    _establish_owner(tmp_path, 100)
    tusj.update_status_json(
        _event(_linux_build_with_urls(100, parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_native_rpm_with_urls(100, parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    urls = _load(_nightly_status_path(tmp_path)).summary.linux.urls
    assert "100-linux" in urls["artifacts"]
    assert "100-linux" in urls["rpm"]

    # Run B takes over ownership (a newer release run) and rebuilds. The takeover
    # resets A's detail, so the block is rebuilt from B alone; A's rpm URL (a run
    # B never produced) is gone rather than left dangling next to B's artifacts.
    _establish_owner(tmp_path, 200)
    tusj.update_status_json(
        _event(_linux_build_with_urls(200, parent_run_id=200)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    urls = _load(_nightly_status_path(tmp_path)).summary.linux.urls
    assert "200-linux" in urls["artifacts"]
    assert "200-linux" in urls["wheels"]
    assert "200-linux" in urls["tarballs"]
    assert "rpm" not in urls


def test_stale_build_event_does_not_clobber_urls(tmp_path: Path) -> None:
    # Run B owns the document and the block.
    _establish_owner(tmp_path, 200)
    tusj.update_status_json(
        _event(_linux_build_with_urls(200, attempt=2, parent_run_id=200)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    # A late event from the superseded run A belongs to a different owner, so the
    # strict gate rejects its leaf -- and it must not move the URLs either.
    tusj.update_status_json(
        _event(_linux_build_with_urls(100, attempt=1, parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    urls = _load(_nightly_status_path(tmp_path)).summary.linux.urls
    assert "200-linux" in urls["artifacts"]


def test_native_urls_only_fill_for_the_owning_run(tmp_path: Path) -> None:
    # Build owned by run B; a native event from a different run A must not inject
    # its rpm URL (that would mix run ids across the block).
    _establish_owner(tmp_path, 200)
    tusj.update_status_json(
        _event(_linux_build_with_urls(200, parent_run_id=200)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_native_rpm_with_urls(100, parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert "rpm" not in _load(_nightly_status_path(tmp_path)).summary.linux.urls

    # A native event from the owning run B does populate rpm.
    tusj.update_status_json(
        _event(_linux_native_rpm_with_urls(200, parent_run_id=200)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    urls = _load(_nightly_status_path(tmp_path)).summary.linux.urls
    assert "200-linux" in urls["rpm"]


def test_dry_run_performs_no_git_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # commit_and_push=False must never shell out to git; make any call explode.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("git must not be called in dry-run mode")

    monkeypatch.setattr(tusj, "_git", _boom)
    monkeypatch.setattr(tusj, "_commit_and_push", _boom)

    out = tusj.update_status_json(
        _event(_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    assert out == _nightly_status_path(tmp_path)
    assert not (tmp_path / ".git").exists()


# --- push-race retry hardening ----------------------------------------------


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout="", stderr=stderr
    )


def _fake_git(push_result: subprocess.CompletedProcess):
    """A `_git` stand-in: `diff --cached` reports staged changes, `push` returns
    the supplied result, everything else succeeds."""

    def _run(args, cwd, check=True):
        if args[:2] == ["diff", "--cached"]:
            return _completed(1)  # 1 => there are staged changes
        if args[0] == "push":
            return push_result
        return _completed(0)

    return _run


def test_commit_and_push_done_on_clean_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tusj, "_git", _fake_git(_completed(0)))
    assert (
        tusj._commit_and_push(Path("/x"), [Path("/x/status.json")], "m")
        is tusj._PushOutcome.DONE
    )


def test_commit_and_push_retry_on_non_fast_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        "! [rejected]        main -> main (fetch first)\n"
        "error: failed to push some refs"
    )
    monkeypatch.setattr(tusj, "_git", _fake_git(_completed(1, stderr)))
    assert (
        tusj._commit_and_push(Path("/x"), [Path("/x/status.json")], "m")
        is tusj._PushOutcome.RETRY
    )


def test_commit_and_push_fatal_on_protected_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        "! [remote rejected] main -> main (protected branch hook declined)\n"
        "error: failed to push some refs"
    )
    monkeypatch.setattr(tusj, "_git", _fake_git(_completed(1, stderr)))
    assert (
        tusj._commit_and_push(Path("/x"), [Path("/x/status.json")], "m")
        is tusj._PushOutcome.FATAL
    )


def test_commit_and_push_noop_when_nothing_staged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(args, cwd, check=True):
        if args[:2] == ["diff", "--cached"]:
            return _completed(0)  # 0 => nothing staged
        if args[0] in ("commit", "push"):
            raise AssertionError(f"must not run git {args[0]} for a no-op")
        return _completed(0)

    monkeypatch.setattr(tusj, "_git", _run)
    assert (
        tusj._commit_and_push(Path("/x"), [Path("/x/status.json")], "m")
        is tusj._PushOutcome.DONE
    )


# Positive coverage is generated from the source list so a new marker added to
# `_RETRYABLE_GIT_STDERR` is tested for free. Each marker is embedded in a
# realistic line and upper-cased to also exercise the case-insensitive match.
@pytest.mark.parametrize("marker", tusj._RETRYABLE_GIT_STDERR)
def test_every_retryable_marker_is_classified_retryable(marker: str) -> None:
    assert tusj._git_stderr_is_retryable(f"error: {marker.upper()}")
    assert tusj._git_stderr_is_retryable(f"fatal: {marker.upper()}")


# Fatal strings are the complement of the retry list -- they cannot be derived
# from it, so they stay explicit. The bare "[remote rejected]" case guards that
# no ambiguous server-refusal marker leaked into `_RETRYABLE_GIT_STDERR`.
@pytest.mark.parametrize(
    "stderr",
    [
        "remote: Permission to repo denied",
        "fatal: Authentication failed",
        "protected branch hook declined",
        "! [remote rejected] main -> main (pre-receive hook declined)",
    ],
)
def test_git_stderr_fatal_classification(stderr: str) -> None:
    assert tusj._git_stderr_is_retryable(stderr) is False


def test_sync_to_upstream_false_on_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(args, cwd, check=True):
        if args[0] == "fetch":
            return _completed(1, "fatal: unable to access")
        raise AssertionError("reset must not run after a failed fetch")

    monkeypatch.setattr(tusj, "_git", _run)
    assert tusj._sync_to_upstream(Path("/x")) is False


def test_sync_to_upstream_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tusj, "_git", lambda args, cwd, check=True: _completed(0))
    assert tusj._sync_to_upstream(Path("/x")) is True


def test_max_retries_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARTZ_STATUS_PUSH_MAX_RETRIES", "25")
    assert tusj._max_retries() == 25


def test_max_retries_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARTZ_STATUS_PUSH_MAX_RETRIES", "nonsense")
    assert tusj._max_retries() == tusj._DEFAULT_MAX_RETRIES
    monkeypatch.setenv("QUARTZ_STATUS_PUSH_MAX_RETRIES", "-3")
    assert tusj._max_retries() == tusj._DEFAULT_MAX_RETRIES


# --- stale .git lock recovery -----------------------------------------------


def test_commit_and_push_retry_on_index_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stale-lock wedge surfaces on `git add`; it must classify as RETRY (the
    # next attempt clears the lock) rather than raising out of the loop.
    stderr = (
        "fatal: Unable to create '/w/.git/index.lock': File exists.\n"
        "Another git process seems to be running in this repository"
    )

    def _run(args, cwd, check=True):
        if args[0] == "add":
            return _completed(128, stderr)
        raise AssertionError(f"must not run git {args[0]} after a failed add")

    monkeypatch.setattr(tusj, "_git", _run)
    assert (
        tusj._commit_and_push(Path("/w"), [Path("/w/status.json")], "m")
        is tusj._PushOutcome.RETRY
    )


def test_clear_stale_git_locks_removes_old_but_keeps_fresh(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)

    stale_index = git_dir / "index.lock"
    stale_index.write_text("")
    stale_ref = git_dir / "refs" / "heads" / "main.lock"
    stale_ref.write_text("")
    old = time.time() - 10 * 60  # 10 minutes ago
    os.utime(stale_index, (old, old))
    os.utime(stale_ref, (old, old))

    fresh = git_dir / "shallow.lock"
    fresh.write_text("")  # just created -> recent

    tusj._clear_stale_git_locks(tmp_path)

    assert not stale_index.exists(), "stale index.lock should be removed"
    assert not stale_ref.exists(), "stale ref lock should be removed"
    assert fresh.exists(), "a recent lock must be left in place"


def test_clear_stale_git_locks_noop_without_git_dir(tmp_path: Path) -> None:
    # No `.git` directory: must be a safe no-op, not an error.
    tusj._clear_stale_git_locks(tmp_path)


# --- two overlapping same-date runs (run-recency arbitration) ----------------
#
# Two release runs on the same nightly date share one status.json (the path is
# keyed by date, not run id) and share each per-arch leaf slot. Arbitration
# keeps the document consistent to the newest run: RunLeaf.should_replace
# prefers the larger `run_id` (GitHub ids increase monotonically, so the larger
# id is the later run), falling back to `run_attempt` + the don't-downgrade
# terminal guard only within a single run. The orchestrator finalize applies the
# same rule at the document level via `trigger_workflow_run_id`.
#
# Deriving the effective owner run id itself is classify()'s job now (see
# DeriveEffectiveOwnerRunIdTest in therock_classify_test.py); by the time a
# WorkflowRunRecord reaches update_status_json, trigger_workflow_run_id is
# already the resolved owner, so these fixtures set it directly.


def _linux_build(
    run_id: int,
    *,
    attempt: int = 1,
    conclusion: str = "success",
    parent_run_id: int | None = None,
) -> WorkflowRunRecord:
    """A rocm/build linux leaf for one run. `conclusion=""` => in_progress."""
    run = _leaf_run()
    run.workflow_run_id = run_id
    run.run_attempt = attempt
    run.conclusion = conclusion
    run.trigger_workflow_run_id = parent_run_id
    return run


def _linux_build_leaf(repo_dir: Path):
    return _load(_nightly_status_path(repo_dir)).pipelines.rocm.build["linux"]


def test_newer_run_takes_over_slot_after_ownership_change(tmp_path: Path) -> None:
    # Older run (100) owns the document and finishes the build. A newer run (200)
    # then takes over ownership (a re-dispatched release), which resets the
    # previous run's detail; run 200's own in_progress build is all that remains,
    # so the document tracks the run that is actually current.
    _establish_owner(tmp_path, 100)
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="success", parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    _establish_owner(tmp_path, 200)
    tusj.update_status_json(
        _event(_linux_build(200, conclusion="", parent_run_id=200)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    leaf = _linux_build_leaf(tmp_path)
    assert leaf.run_id == 200
    assert leaf.status is Status.in_progress


def test_stale_older_run_never_overwrites_newer(tmp_path: Path) -> None:
    # Newer run (200) owns the document and finishes first. A late build event
    # from the superseded run (100) belongs to a different owner, so the strict
    # gate rejects it -- the newer run keeps the slot regardless of arrival order.
    _establish_owner(tmp_path, 200)
    tusj.update_status_json(
        _event(_linux_build(200, conclusion="success", parent_run_id=200)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="success", parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert _linux_build_leaf(tmp_path).run_id == 200


def test_out_of_order_completed_then_started_keeps_terminal(tmp_path: Path) -> None:
    # Same run, reordered delivery: the completed event lands before the started
    # one. The stray in_progress must not downgrade the finished leaf.
    _establish_owner(tmp_path, 100)
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="success", parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="", parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    leaf = _linux_build_leaf(tmp_path)
    assert leaf.status is Status.success
    assert leaf.completed_at is not None


def test_higher_attempt_supersedes_even_when_in_progress(tmp_path: Path) -> None:
    # A re-run (attempt 2) of the same run supersedes the finished attempt 1 even
    # though the re-run is only in_progress: a higher attempt always wins.
    _establish_owner(tmp_path, 100)
    tusj.update_status_json(
        _event(_linux_build(100, attempt=1, conclusion="success", parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(100, attempt=2, conclusion="", parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    leaf = _linux_build_leaf(tmp_path)
    assert leaf.run_attempt == 2
    assert leaf.status is Status.in_progress


def test_newer_orchestrator_finalize_records_owning_run(tmp_path: Path) -> None:
    # The top-level orchestrator's completed event stamps the document and
    # records its run id as the owner in trigger_workflow_run_id.
    run = _orchestrator_run()
    run.workflow_run_id = 29079513704
    out = tusj.update_status_json(_event(run), repo_dir=tmp_path, commit_and_push=False)
    doc = _load(out)
    assert doc.trigger_workflow_run_id == 29079513704
    assert doc.completed_at is not None


def test_superseded_orchestrator_finalize_is_ignored(tmp_path: Path) -> None:
    # The newer orchestrator (larger id) finalizes as success and owns the doc.
    newer = _orchestrator_run()
    newer.workflow_run_id = 29079513704
    tusj.update_status_json(_event(newer), repo_dir=tmp_path, commit_and_push=False)

    # The older orchestrator (smaller id) completes later as a failure. It is a
    # superseded run, so its conclusion must not stomp the newer success.
    older = _orchestrator_run()
    older.workflow_run_id = 29079225784
    older.conclusion = "failure"
    tusj.update_status_json(_event(older), repo_dir=tmp_path, commit_and_push=False)

    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_workflow_run_id == 29079513704
    assert doc.orchestrator_conclusion is Status.success
    assert doc.summary.overall_status is Status.success


def test_newer_orchestrator_owner_resets_previous_run_leaves(tmp_path: Path) -> None:
    older = _orchestrator_run()
    older.workflow_run_id = 100
    tusj.update_status_json(
        _event(older, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(101, parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert _linux_build_leaf(tmp_path).run_id == 101

    newer = _orchestrator_run()
    newer.workflow_run_id = 200
    tusj.update_status_json(
        _event(newer, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )

    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_workflow_run_id == 200
    assert "linux" not in doc.pipelines.rocm.build
    assert doc.summary.overall_status is Status.in_progress


def test_newer_orchestrator_owner_refreshes_created_at(tmp_path: Path) -> None:
    # `created_at` is sticky (set once), but a supersede must re-point it at the
    # new owner run so it agrees with `build_date`; otherwise the document shows
    # two different runs across its two date fields.
    older = _orchestrator_run()
    older.workflow_run_id = 100
    older.created_at = datetime(2026, 6, 19, 15, 8, tzinfo=timezone.utc)
    tusj.update_status_json(
        _event(older, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert _load(_nightly_status_path(tmp_path)).created_at == "2026-06-19T15:08:00Z"

    newer = _orchestrator_run()
    newer.workflow_run_id = 200
    newer.created_at = datetime(2026, 6, 20, 15, 8, tzinfo=timezone.utc)
    tusj.update_status_json(
        _event(newer, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )

    doc = _load(_nightly_status_path(tmp_path))
    assert doc.created_at == "2026-06-20T15:08:00Z"
    assert doc.build_date == "20260620"


def test_superseded_parent_leaf_is_ignored_even_with_newer_child_id(
    tmp_path: Path,
) -> None:
    owner = _orchestrator_run()
    owner.workflow_run_id = 200
    tusj.update_status_json(
        _event(owner, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(201, parent_run_id=200)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )

    stale = _linux_build(300, conclusion="failure", parent_run_id=100)
    tusj.update_status_json(_event(stale), repo_dir=tmp_path, commit_and_push=False)

    leaf = _linux_build_leaf(tmp_path)
    assert leaf.run_id == 201
    assert leaf.status is Status.success


def test_newer_parent_leaf_never_takes_over_owner(
    tmp_path: Path,
) -> None:
    # A leaf from a different (even newer) run must never promote itself to owner
    # -- only the owner-writer path (orchestrator start / setup) may change
    # ownership. This is the core of the strict gate: without it an asan run's
    # leaves, whose run id is higher, would hijack the normal release document.
    owner = _orchestrator_run()
    owner.workflow_run_id = 100
    tusj.update_status_json(
        _event(owner, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(101, parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )

    newer_leaf = _windows_leaf_run()
    newer_leaf.workflow_run_id = 202
    newer_leaf.trigger_workflow_run_id = 200
    newer_leaf.conclusion = "failure"
    tusj.update_status_json(
        _event(newer_leaf), repo_dir=tmp_path, commit_and_push=False
    )

    doc = _load(_nightly_status_path(tmp_path))
    # Owner unchanged; the owner's linux leaf stays; the foreign leaf is dropped.
    assert doc.trigger_workflow_run_id == 100
    assert doc.pipelines.rocm.build["linux"].run_id == 101
    assert "windows" not in doc.pipelines.rocm.build


# --- issue #65: asan runs must never own or pollute the release document ------
#
# An asan release is a second, later run (higher run id) that shares the release
# version and nightly date. Without the strict gate + setup anchor its leaves
# would land on -- or take over -- the normal release document. Three guarantees:
# a normal setup run anchors ownership; an asan setup run never owns; and an asan
# leaf whose parent is the asan run is dropped from the normally-owned document.


def test_setup_release_run_anchors_owner_and_admits_leaf(tmp_path: Path) -> None:
    # A normal setup_multi_arch.yml run executes via workflow_call, so its run id
    # is the top-level orchestrator's. It records ownership before any leaf, so a
    # leaf that names it as parent is admitted.
    tusj.update_status_json(
        _event(_setup_run(500)), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_workflow_run_id == 500

    leaf = _leaf_run()
    leaf.trigger_workflow_run_id = 500
    tusj.update_status_json(_event(leaf), repo_dir=tmp_path, commit_and_push=False)
    doc = _load(_nightly_status_path(tmp_path))
    assert doc.summary.linux.rocm.build.status is Status.success


@pytest.mark.parametrize("build_variant", ["asan", "host-asan", "tsan", ""])
def test_setup_non_release_run_does_not_own_release_document(
    tmp_path: Path, build_variant: str
) -> None:
    # Only a normal `release` setup run may anchor ownership of the normal
    # release document. Sanitizer variants (asan, host-asan, tsan) get their own
    # status.json file later, and an unset variant is not provably the normal
    # release; all are skipped and no document is written. Gating positively on
    # "release" is what catches host-asan/tsan, which a `== "asan"` check missed.
    out = tusj.update_status_json(
        _event(_setup_run(600, build_variant=build_variant)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert out is None
    assert not _nightly_status_path(tmp_path).exists()


def test_asan_leaf_cannot_pollute_normally_owned_document(tmp_path: Path) -> None:
    # Core #65 regression: the normal setup run (500) owns the document. The asan
    # release runs later as run 600; its test leaves name 600 as their parent.
    # The strict gate drops them, so asan results never enter the normal document.
    tusj.update_status_json(
        _event(_setup_run(500)), repo_dir=tmp_path, commit_and_push=False
    )

    asan_leaf = _run(
        path=".github/workflows/test_artifacts.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    asan_leaf.workflow_run_id = 601
    asan_leaf.trigger_workflow_run_id = 600
    asan_leaf.classification.build_variant = "asan"
    tusj.update_status_json(_event(asan_leaf), repo_dir=tmp_path, commit_and_push=False)

    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_workflow_run_id == 500
    assert doc.summary.linux.rocm.test.success == 0


# --- orchestrator re-run: ownership is (run_id, run_attempt) -----------------
#
# A GitHub re-run keeps the run id but bumps run_attempt, so ownership is the
# pair and not the id alone. Attempt 2 re-opens attempt 1's finalized document
# while keeping the passing leaves (partial re-runs only re-report failed ones),
# and a late attempt-1 completion is rejected rather than overwriting attempt 2.


def test_rerun_higher_attempt_reopens_finalized_document(tmp_path: Path) -> None:
    # Attempt 1 finalizes the release as failure. The orchestrator is then
    # re-run: attempt 2's in_progress (same run id, higher attempt) must re-open
    # the document -- drop completed_at and recap overall_status to in_progress
    # -- yet keep the passing leaves, since a partial re-run never re-reports them.
    orch1 = _orchestrator_run()
    orch1.workflow_run_id = 100
    orch1.run_attempt = 1
    orch1.conclusion = "failure"
    tusj.update_status_json(
        _event(orch1, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(101, parent_run_id=100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(_event(orch1), repo_dir=tmp_path, commit_and_push=False)

    finalized = _load(_nightly_status_path(tmp_path))
    assert finalized.completed_at is not None
    assert finalized.trigger_run_attempt == 1
    assert finalized.orchestrator_conclusion is Status.failure
    assert finalized.summary.overall_status is Status.failure

    orch2 = _orchestrator_run()
    orch2.workflow_run_id = 100
    orch2.run_attempt = 2
    tusj.update_status_json(
        _event(orch2, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )

    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_workflow_run_id == 100
    assert doc.trigger_run_attempt == 2
    # Re-opened: no longer finalized, back to in_progress...
    assert doc.completed_at is None
    assert doc.orchestrator_conclusion is None
    assert doc.summary.overall_status is Status.in_progress
    # ... but the passing leaf from attempt 1 survives (partial re-run keeps it).
    assert _linux_build_leaf(tmp_path).run_id == 101
    assert _linux_build_leaf(tmp_path).status is Status.success


def test_stale_lower_attempt_completion_does_not_overwrite_newer(
    tmp_path: Path,
) -> None:
    # Attempt 2 is running (in_progress) after a re-run. A delayed attempt-1
    # completion then arrives. It is a superseded attempt, so its finalize must
    # be rejected -- the document must not be stamped completed by attempt 1.
    orch2 = _orchestrator_run()
    orch2.workflow_run_id = 100
    orch2.run_attempt = 2
    tusj.update_status_json(
        _event(orch2, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert _load(_nightly_status_path(tmp_path)).trigger_run_attempt == 2

    stale = _orchestrator_run()
    stale.workflow_run_id = 100
    stale.run_attempt = 1
    stale.conclusion = "failure"
    tusj.update_status_json(_event(stale), repo_dir=tmp_path, commit_and_push=False)

    doc = _load(_nightly_status_path(tmp_path))
    assert doc.trigger_run_attempt == 2
    assert doc.completed_at is None
    assert doc.orchestrator_conclusion is None
    assert doc.summary.overall_status is Status.in_progress


# --- variant derivation (pytorch/jax py x torch/jax) -------------------------
#
# Builds fan the matrix axis out across jobs named `Build | py X | torch Y`, so
# each cell becomes one variant. Tests run a single (py, ref) per dispatch and
# name their jobs by arch, so the cell is read from the dispatch inputs instead.
# jax uses the `jax_version` axis key. Non-matrixed pipelines (rocm) get no variants.


def _job(
    name: str,
    *,
    conclusion: str | None = "success",
    started: str | None = "09:00",
    completed: str | None = "11:00",
) -> WorkflowJobRecord:
    def _dt(hm: str | None) -> datetime | None:
        if hm is None:
            return None
        h, m = (int(x) for x in hm.split(":"))
        return datetime(2026, 6, 19, h, m, tzinfo=timezone.utc)

    return WorkflowJobRecord(
        job_id=0,
        name=name,
        status="completed" if conclusion else "in_progress",
        conclusion=conclusion,
        created_at=_dt(started),
        started_at=_dt(started),
        completed_at=_dt(completed) if conclusion else None,
        runner_name="",
        labels=[],
        steps=[],
        summary="",
        metrics={},
    )


def _variant_run(
    *,
    pipeline_type: str,
    pipeline_phase: str,
    jobs: list[WorkflowJobRecord] | None = None,
    inputs: dict | None = None,
    run_id: int = 100,
    run_attempt: int = 1,
    conclusion: str | None = "success",
    architectures: list[str] | None = None,
    path: str = ".github/workflows/x.yml",
) -> WorkflowRunRecord:
    run = _run(
        path=path,
        platform="linux",
        pipeline_type=pipeline_type,
        pipeline_phase=pipeline_phase,
        architectures=architectures or [],
    )
    run.workflow_run_id = run_id
    run.run_attempt = run_attempt
    run.conclusion = conclusion
    run.status = "completed" if conclusion else "in_progress"
    run.jobs = jobs or []
    run.inputs = inputs or {}
    return run


def test_pytorch_build_variants_from_matrix_job_names() -> None:
    run = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        run_id=555,
        jobs=[
            _job("Build | py 3.10 | torch release/2.10"),
            _job("Build | py 3.12 | torch release/2.10"),
        ],
    )
    variants = tusj._derive_variants(run)
    assert [v.matrix for v in variants] == [
        {"py": "3.10", "torch": "release/2.10"},
        {"py": "3.12", "torch": "release/2.10"},
    ]
    assert all(v.run_id == 555 and v.status is Status.success for v in variants)


def test_build_leaf_excludes_nested_test_subjobs() -> None:
    # A reusable-workflow build run's job list carries both the cell's build
    # sub-job and its nested per-arch test sub-jobs. The build leaf must reflect
    # the build sub-job alone: an in-progress (or failed) nested test job belongs
    # to the test leaf and must not drag the build cell's status or completion
    # (the #82 regression, where the build leaf absorbed test cells).
    run = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        jobs=[
            _job("Build | py 3.12 | torch release/2.10 / build_pytorch_wheels"),
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx942",
                conclusion=None,
                completed=None,
            ),
        ],
    )
    variants = tusj._derive_variants(run)
    assert len(variants) == 1
    assert variants[0].matrix == {"py": "3.12", "torch": "release/2.10"}
    assert variants[0].status is Status.success
    assert variants[0].completed_at is not None


def test_jax_build_variants_use_jax_version_axis() -> None:
    run = _variant_run(
        pipeline_type="jax",
        pipeline_phase="build",
        jobs=[_job("Build | py 3.12 | jax rocm-jaxlib-v0.9.1")],
    )
    variants = tusj._derive_variants(run)
    assert variants[0].matrix == {"py": "3.12", "jax_version": "0.9.1"}


def test_pytorch_test_variant_read_from_inputs_when_jobs_lack_axis() -> None:
    run = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx942"],
        run_id=900,
        jobs=[_job("Test PyTorch Full | gfx94X-dcgpu")],
        inputs={"python_version": "3.12", "pytorch_git_ref": "release/2.10"},
    )
    variants = tusj._derive_variants(run)
    assert len(variants) == 1
    assert variants[0].matrix == {"py": "3.12", "torch": "release/2.10"}
    assert variants[0].run_id == 900


def test_jax_test_variant_reads_jax_ref_input() -> None:
    # Only the full `jax_ref` input is present (no explicit `jax_version`); it is
    # read and stripped to the bare version for the axis.
    run = _variant_run(
        pipeline_type="jax",
        pipeline_phase="test",
        inputs={"python_version": "3.11", "jax_ref": "rocm-jaxlib-v0.9.1"},
        jobs=[_job("Test JAX | gfx942")],
    )
    variants = tusj._derive_variants(run)
    assert variants[0].matrix == {"py": "3.11", "jax_version": "0.9.1"}


def test_jax_test_variant_prefers_explicit_jax_version_input() -> None:
    # When TheRock supplies both, the bare `jax_version` input wins over `jax_ref`.
    run = _variant_run(
        pipeline_type="jax",
        pipeline_phase="test",
        inputs={
            "python_version": "3.11",
            "jax_ref": "rocm-jaxlib-v0.9.1",
            "jax_version": "0.9.1",
        },
        jobs=[_job("Test JAX | gfx942")],
    )
    variants = tusj._derive_variants(run)
    assert variants[0].matrix == {"py": "3.11", "jax_version": "0.9.1"}


def test_jax_ref_prefixed_and_bare_spellings_collapse_to_one_cell() -> None:
    # The two spellings of one jax cell -- the build job's git-ref tail
    # ("jax rocm-jaxlib-v0.11.0") and the bare-version form the release
    # orchestrator / test dispatch inputs use ("0.11.0") -- must land on a single
    # (py, version) key. Before normalization each spelling keyed its own variant,
    # doubling every jax build and test count.
    from_job = tusj._derive_variants(
        _variant_run(
            pipeline_type="jax",
            pipeline_phase="build",
            jobs=[_job("Build | py 3.12 | jax rocm-jaxlib-v0.11.0")],
        )
    )
    from_input = tusj._derive_variants(
        _variant_run(
            pipeline_type="jax",
            pipeline_phase="test",
            inputs={"python_version": "3.12", "jax_ref": "0.11.0"},
            jobs=[_job("Test JAX | gfx942")],
        )
    )
    assert from_job[0].matrix == {"py": "3.12", "jax_version": "0.11.0"}
    assert from_input[0].matrix == {"py": "3.12", "jax_version": "0.11.0"}
    assert from_job[0].key() == from_input[0].key()

    merged = tusj.merge_matrix_test_leaf(
        tusj.RunLeaf(status=Status.in_progress, variants=from_job),
        tusj.RunLeaf(status=Status.success, variants=from_input),
    )
    assert len(merged.variants) == 1


def test_rocm_run_has_no_variants() -> None:
    run = _variant_run(
        pipeline_type="rocm", pipeline_phase="build", jobs=[_job("Build ROCm")]
    )
    assert tusj._derive_variants(run) is None


def test_test_run_without_axis_inputs_has_no_variants() -> None:
    run = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        inputs={},
        jobs=[_job("Test PyTorch Full | gfx94X-dcgpu")],
    )
    assert tusj._derive_variants(run) is None


def test_build_variants_merge_across_runs_into_platform_leaf() -> None:
    # Each (py, torch) cell is its own run; the platform build leaf aggregates
    # them, carrying only a rolled-up status plus the per-run variants.
    doc = StatusDocument()
    for rid, py in ((801, "3.10"), (802, "3.12")):
        run = _variant_run(
            pipeline_type="pytorch",
            pipeline_phase="build",
            run_id=rid,
            jobs=[_job(f"Build | py {py} | torch release/2.10")],
        )
        assert doc.upsert_leaf("linux", "", "pytorch", "build", tusj._create_leaf(run))

    build = doc.pipelines.pytorch.build["linux"]
    assert {v.run_id for v in build.variants} == {801, 802}
    assert {v.matrix["py"] for v in build.variants} == {"3.10", "3.12"}
    assert build.status is Status.success


def test_test_leaf_carries_its_own_run_state_and_variant() -> None:
    doc = StatusDocument()
    run = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx942"],
        run_id=900,
        inputs={"python_version": "3.12", "pytorch_git_ref": "release/2.10"},
        jobs=[_job("Test PyTorch Full | gfx94X-dcgpu")],
    )
    assert doc.upsert_leaf("linux", "gfx942", "pytorch", "test", tusj._create_leaf(run))

    leaf = doc.pipelines.pytorch.test["linux"]["gfx942"]
    assert leaf.run_id == 900
    assert leaf.variants is not None and len(leaf.variants) == 1
    assert leaf.variants[0].matrix == {"py": "3.12", "torch": "release/2.10"}


def test_completed_fanout_build_refreshes_same_run_test_leaves() -> None:
    # The delegated wheel release run reports one shared run id. Earlier payloads
    # can project its job snapshot into per-arch test leaves while a matrix cell
    # is still running; the final completed build-phase payload must retire those
    # same-run test leaves even though it is classified as build.
    doc = StatusDocument()
    stale_test = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx110X-all"],
        run_id=901,
        conclusion=None,
        jobs=[
            _job("Build | py 3.12 | torch release/2.10 / Build"),
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx110X-all",
                conclusion=None,
                completed=None,
            ),
        ],
    )
    doc.upsert_leaf(
        "linux", "gfx110X-all", "pytorch", "test", tusj._create_leaf(stale_test)
    )

    completed_build = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        run_id=901,
        jobs=[
            _job("Build | py 3.12 | torch release/2.10 / Build"),
            _job("Build | py 3.12 | torch release/2.10 / Test | gfx110X-all"),
        ],
    )
    # Deliberately mismatched vs. the linux leaf above: this proves the match
    # is keyed on (run_id, run_attempt) alone, not platform. That's safe in
    # practice -- GitHub's run_id is unique per repository across every
    # workflow/platform, so a real linux and windows run can never collide on
    # one -- but it's an isolation technique, not a model of real data; don't
    # read it as "a windows run can update a linux leaf" in production.
    completed_build.classification.platform = "windows"
    tusj._merge_run_into_document(
        doc, completed_build, tusj._create_leaf(completed_build)
    )

    leaf = doc.pipelines.pytorch.test["linux"]["gfx110X-all"]
    assert leaf.status is Status.success
    assert leaf.completed_at == "2026-06-19T15:18:00Z"
    assert leaf.variants is not None
    assert leaf.variants[0].status is Status.success


def test_completed_fanout_build_does_not_leak_status_across_architectures() -> None:
    # Two GPUs are tested under the *same* (py, torch) build cell. The
    # matrix-cell key parsed from job names carries no arch, so refreshing
    # same-run test leaves must not broadcast one architecture's outcome onto
    # another's: gfx1101 failing must not drag down gfx942's passing leaf.
    doc = StatusDocument()
    for arch in ("gfx942", "gfx1101"):
        stale_test = _variant_run(
            pipeline_type="pytorch",
            pipeline_phase="test",
            architectures=[arch],
            run_id=901,
            conclusion=None,
            jobs=[
                _job("Build | py 3.12 | torch release/2.10 / Build"),
                _job(
                    f"Build | py 3.12 | torch release/2.10 / Test | {arch}",
                    conclusion=None,
                    completed=None,
                ),
            ],
        )
        doc.upsert_leaf("linux", arch, "pytorch", "test", tusj._create_leaf(stale_test))

    completed_build = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        run_id=901,
        conclusion="failure",
        jobs=[
            _job("Build | py 3.12 | torch release/2.10 / Build"),
            _job("Build | py 3.12 | torch release/2.10 / Test | gfx942"),
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx1101",
                conclusion="failure",
            ),
        ],
    )
    tusj._merge_run_into_document(
        doc, completed_build, tusj._create_leaf(completed_build)
    )

    assert doc.pipelines.pytorch.test["linux"]["gfx942"].status is Status.success
    assert doc.pipelines.pytorch.test["linux"]["gfx1101"].status is Status.failure


def test_multi_arch_test_event_does_not_leak_or_alias_across_architectures() -> None:
    # Distinct from the fanout-build case above (a *build* run whose nested
    # test jobs get refreshed into already-existing per-arch test leaves):
    # this pins _merge_run_into_document's own multi_arch branch, for a
    # single *test*-phase event that itself reports more than one
    # architecture in cls.architectures. No dispatcher today fans a single
    # test-phase run across several different GPUs (each test run reports
    # exactly one arch), but nothing prevents one from doing so in the
    # future -- if it did, the two architectures' leaves must be genuinely
    # independent objects, not aliases sharing one leaf/variants list.
    run = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx942", "gfx1101"],
        run_id=904,
        conclusion="failure",
        jobs=[
            _job("py 3.12 | torch release/2.10 / Test | gfx942"),
            _job(
                "py 3.12 | torch release/2.10 / Test | gfx1101",
                conclusion="failure",
            ),
        ],
    )
    doc = StatusDocument()
    tusj._merge_run_into_document(doc, run, tusj._create_leaf(run))

    gfx942 = doc.pipelines.pytorch.test["linux"]["gfx942"]
    gfx1101 = doc.pipelines.pytorch.test["linux"]["gfx1101"]
    assert gfx942.status is Status.success
    assert gfx1101.status is Status.failure
    assert gfx942 is not gfx1101
    assert gfx942.variants is not gfx1101.variants


def test_fanout_projection_uses_variant_rollup_not_raw_run_conclusion() -> None:
    # The build run's own top-level GitHub conclusion is not necessarily the
    # worst-of its matrix cells (e.g. a cell whose nested test job failed does
    # not always flip the run's own conclusion). A projected test leaf must
    # take the worst-of its own variants, not the raw run conclusion.
    doc = StatusDocument()
    stale_test = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx110X-all"],
        run_id=902,
        conclusion=None,
        jobs=[
            _job("Build | py 3.12 | torch release/2.10 / Build"),
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx110X-all",
                conclusion=None,
                completed=None,
            ),
        ],
    )
    doc.upsert_leaf(
        "linux", "gfx110X-all", "pytorch", "test", tusj._create_leaf(stale_test)
    )

    # The run's own conclusion reports success even though its nested test
    # job for this cell failed.
    completed_build = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        run_id=902,
        conclusion="success",
        jobs=[
            _job("Build | py 3.12 | torch release/2.10 / Build"),
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx110X-all",
                conclusion="failure",
            ),
        ],
    )
    completed_build.classification.platform = "windows"
    tusj._merge_run_into_document(
        doc, completed_build, tusj._create_leaf(completed_build)
    )

    leaf = doc.pipelines.pytorch.test["linux"]["gfx110X-all"]
    assert leaf.status is Status.failure
    assert leaf.variants is not None
    assert leaf.variants[0].status is Status.failure


def test_fanout_projection_folds_raw_run_conclusion_into_rollup() -> None:
    # The inverse of the case above: every reported cell looks clean, but the
    # run itself was cancelled (e.g. a cell whose job never even started, so
    # it never shows up in `variants` at all). The projected test leaf must
    # still surface that cancellation rather than reporting the variants'
    # all-success rollup verbatim.
    doc = StatusDocument()
    stale_test = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx110X-all"],
        run_id=903,
        conclusion=None,
        jobs=[
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx110X-all",
                conclusion=None,
                completed=None,
            ),
        ],
    )
    doc.upsert_leaf(
        "linux", "gfx110X-all", "pytorch", "test", tusj._create_leaf(stale_test)
    )

    # Same platform as the stale leaf above: this test is about the
    # cancellation-folding logic, not about the (run_id, run_attempt)-only
    # matching (already covered by
    # test_completed_fanout_build_refreshes_same_run_test_leaves), so it
    # doesn't need a platform mismatch to make its point.
    # The py 3.12 build sub-job succeeded (a test job for that cell only exists
    # because its build did): the run-level "cancelled" comes from some *other*
    # cell that never started, not from this cell's build.
    cancelled_build = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        run_id=903,
        conclusion="cancelled",
        jobs=[
            _job(
                "Build | py 3.12 | torch release/2.10 / Build",
                conclusion="success",
            ),
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx110X-all",
                conclusion="success",
            ),
        ],
    )
    tusj._merge_run_into_document(
        doc, cancelled_build, tusj._create_leaf(cancelled_build)
    )

    leaf = doc.pipelines.pytorch.test["linux"]["gfx110X-all"]
    assert leaf.status is Status.cancelled

    # The build leaf keeps its own success: the cancelled run conclusion folds
    # into the test rollup (above), but must not drag down a build cell whose
    # build sub-job succeeded (#82).
    build_leaf = doc.pipelines.pytorch.build["linux"]
    assert build_leaf.status is Status.success
    assert build_leaf.variants is not None
    assert {v.matrix.get("py") for v in build_leaf.variants} == {"3.12"}
    assert build_leaf.variants[0].status is Status.success


def test_build_leaf_own_tail_ref_wins_and_test_only_cell_excluded() -> None:
    # A calling orchestrator (e.g. rockrel) wraps TheRock's own build job in a
    # differently-cased ancestor segment carrying a looser ref:
    #   "Release | py 3.12 | JAX 0.11.0 / Build | py 3.12 | jax rocm-jaxlib-v0.11.0"
    # The build job's own tail (lowercase, full ref) must win over that ancestor.
    # A py 3.13 cell that expands into a nested test sub-job ONLY (no build job of
    # its own) is a test cell, not a build cell: it borrows the uppercase ancestor
    # ref and must be excluded from the build leaf entirely -- the #82 regression,
    # where that phantom cell doubled the build leaf and let its cancelled test
    # flip the build status.
    run = _variant_run(
        pipeline_type="jax",
        pipeline_phase="build",
        jobs=[
            _job(
                "build_jax_wheels / Release | py 3.12 | JAX 0.11.0 / "
                "Build | py 3.12 | jax rocm-jaxlib-v0.11.0"
            ),
            _job(
                "build_jax_wheels / Release | py 3.13 | JAX 0.11.0 / "
                "Test | gfx94X-dcgpu | linux-gfx942-1gpu-ccs-csp-ossci-rocm / "
                "Test JAX | gfx94X-dcgpu",
                conclusion="cancelled",
            ),
        ],
    )
    variants = tusj._derive_variants(run)
    by_py = {v.matrix["py"]: v for v in variants}
    assert set(by_py) == {"3.12"}
    assert by_py["3.12"].matrix["jax_version"] == "0.11.0"
    assert by_py["3.12"].status is Status.success


def test_jax_rockrel_build_leaf_not_flipped_by_cancelled_test() -> None:
    # The #82 shape end-to-end: a rockrel-orchestrated jax build run whose nested
    # test sub-job was cancelled. The test job borrows the uppercase ancestor ref
    # ("JAX 0.10.2" -> "0.10.2") while the build job's own tail carries the full
    # ref ("rocm-jaxlib-v0.10.2"); before the fix those landed as two cells and
    # the cancelled test flipped the build. The build leaf must now stay success
    # and carry exactly one cell.
    doc = StatusDocument()
    run = _variant_run(
        pipeline_type="jax",
        pipeline_phase="build",
        run_id=930,
        conclusion="success",
        jobs=[
            _job(
                "build_jax_wheels / Release | py 3.12 | JAX 0.10.2 / "
                "Build | py 3.12 | jax rocm-jaxlib-v0.10.2"
            ),
            _job(
                "build_jax_wheels / Release | py 3.12 | JAX 0.10.2 / "
                "Test | gfx94X-dcgpu | linux-gfx942-1gpu / Test JAX | gfx94X-dcgpu",
                conclusion="cancelled",
            ),
        ],
    )
    tusj._merge_run_into_document(doc, run, tusj._create_leaf(run))

    build_leaf = doc.pipelines.jax.build["linux"]
    assert build_leaf.status is Status.success
    assert len(build_leaf.variants) == 1
    assert build_leaf.variants[0].matrix == {
        "py": "3.12",
        "jax_version": "0.10.2",
    }


def test_test_snapshot_finalizes_same_run_build_leaf() -> None:
    # Shared-run topology: a pytorch/jax build workflow calls its test workflow
    # via workflow_call, so the reusable test's own notify carries the whole
    # parent run's job list -- the finished build sub-jobs included. The build
    # leaf can be stuck in_progress (its finalizing notify not yet fired) when a
    # test-phase notify arrives mid-run with the build sub-job already terminal.
    # That test snapshot must finalize the same-run build leaf early.
    doc = StatusDocument()

    building = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        run_id=910,
        conclusion=None,
        jobs=[
            _job(
                "Build | py 3.12 | torch release/2.10 / Build",
                conclusion=None,
                completed=None,
            ),
        ],
    )
    tusj._merge_run_into_document(doc, building, tusj._create_leaf(building))
    assert doc.pipelines.pytorch.build["linux"].status is Status.in_progress

    # The nested test workflow (same run id) reports: the build sub-job has now
    # finished, plus its own still-running test job.
    test_run = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx942"],
        run_id=910,
        conclusion=None,
        jobs=[
            _job("Build | py 3.12 | torch release/2.10 / Build"),
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx942",
                conclusion=None,
                completed=None,
            ),
        ],
    )
    tusj._merge_run_into_document(doc, test_run, tusj._create_leaf(test_run))

    build_leaf = doc.pipelines.pytorch.build["linux"]
    assert build_leaf.status is Status.success
    assert build_leaf.completed_at is not None
    assert len(build_leaf.variants) == 1
    assert build_leaf.variants[0].matrix == {"py": "3.12", "torch": "release/2.10"}
    # The test snapshot's still-running test job must not appear in the build
    # leaf; the test leaf tracks it instead.
    assert doc.pipelines.pytorch.test["linux"]["gfx942"].status is Status.in_progress

    # Finalizing the build leaf early must not mark the pipeline done: the
    # rollup still shows the build as success but pytorch -- and the run
    # overall -- stays in_progress while the test cell runs.
    assert doc.summary.linux.pytorch.build.status is Status.success
    assert doc.summary.linux.pytorch.test.in_progress == 1
    assert doc.summary.overall_status is Status.in_progress


def test_standalone_test_run_does_not_touch_build_leaf() -> None:
    # A standalone test dispatch (test_pytorch_wheels_full.yml) has its OWN run
    # id, distinct from the build run's. Its snapshot must never rewrite the real
    # build run's leaf -- the mirror is guarded to the shared-run id.
    doc = StatusDocument()
    build = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="build",
        run_id=920,
        jobs=[_job("Build | py 3.12 | torch release/2.10 / Build")],
    )
    tusj._merge_run_into_document(doc, build, tusj._create_leaf(build))
    assert doc.pipelines.pytorch.build["linux"].status is Status.success

    standalone_test = _variant_run(
        pipeline_type="pytorch",
        pipeline_phase="test",
        architectures=["gfx942"],
        run_id=921,
        conclusion="failure",
        jobs=[
            _job(
                "Build | py 3.12 | torch release/2.10 / Test | gfx942",
                conclusion="failure",
            ),
        ],
    )
    tusj._merge_run_into_document(
        doc, standalone_test, tusj._create_leaf(standalone_test)
    )

    after = doc.pipelines.pytorch.build["linux"]
    assert after.status is Status.success
    assert len(after.variants) == 1
    assert after.variants[0].run_id == 920
    assert after.variants[0].status is Status.success


def test_skip_workflow_names_are_all_disregarded(tmp_path: Path) -> None:
    # Guards the generic `_SKIP_WORKFLOW_NAMES` mechanism itself, not just the
    # one workflow it was introduced for: whatever is in the set must be
    # disregarded outright, with nothing written to status.json.
    for name in tusj._SKIP_WORKFLOW_NAMES:
        run = _run(
            path=name,
            platform="linux",
            pipeline_type="rocm",
            pipeline_phase="test",
            architectures=["gfx1151"],
        )
        out = tusj.update_status_json(
            _event(run), repo_dir=tmp_path, commit_and_push=False
        )
        assert out is None, f"{name!r} was not disregarded"
    assert not any(tmp_path.rglob("status.json"))


def _test_component_run(
    *,
    job_name: str,
    conclusion: str | None = "success",
    run_id: int = 200,
) -> WorkflowRunRecord:
    """A `test_component.yml` completion for one ROCm component."""
    run = _run(
        path="test_component.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx1151"],
    )
    run.workflow_run_id = run_id
    run.conclusion = conclusion
    run.inputs = {"component": json.dumps({"job_name": job_name, "total_shards": 3})}
    return run


def test_test_component_completion_is_skipped(tmp_path: Path) -> None:
    # test_component.yml is registered in WORKFLOW_SPECS solely so
    # classification doesn't raise; its per-component completions must be
    # disregarded entirely (component-level granularity is not yet decided),
    # not written anywhere.
    out = tusj.update_status_json(
        _event(_test_component_run(job_name="hipblaslt")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert out is None
    assert not any(tmp_path.rglob("status.json"))


def test_test_component_completion_does_not_affect_artifact_level_leaf(
    tmp_path: Path,
) -> None:
    # A failing test_component.yml completion arriving either before or after
    # test_artifacts.yml's own (artifact-level) report must not influence the
    # [platform][arch] leaf at all -- it stays exactly what test_artifacts.yml
    # itself reported, with no variants.
    _establish_owner(tmp_path)
    tusj.update_status_json(
        _event(
            _test_component_run(job_name="hipblaslt", conclusion="failure", run_id=201)
        ),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    artifact_run = _run(
        path=".github/workflows/test_artifacts.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx1151"],
    )
    out = tusj.update_status_json(
        _event(artifact_run), repo_dir=tmp_path, commit_and_push=False
    )
    assert out is not None
    leaf = _load(out).pipelines.rocm.test["linux"]["gfx1151"]
    assert leaf.status is Status.success
    assert leaf.variants is None
