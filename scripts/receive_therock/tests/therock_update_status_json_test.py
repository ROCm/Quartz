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


def test_leaf_event_leaves_release_in_progress(tmp_path: Path) -> None:
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
    leaf = _leaf_run()
    leaf.trigger_workflow_run_id = 123456
    out = tusj.update_status_json(
        _event(leaf), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(out)
    assert doc.trigger_workflow_run_id is None


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


def test_leaf_without_owner_but_matching_release_version_is_accepted(
    tmp_path: Path,
) -> None:
    # test_pytorch_wheels_full.yml (and similar async benc-uk dispatches) carry
    # no run id or artifact URL to derive a parent from, so trigger_workflow_run_id
    # is None even once an orchestrator has claimed ownership of the document.
    # Its classification.release_version -- resolved from the wheel's own
    # torch_version -- still pins it to this exact release, so it must be
    # accepted rather than rejected as ownerless.
    orch = _orchestrator_run()
    orch.workflow_run_id = 29079513704
    orch.conclusion = None
    orch.status = "in_progress"
    tusj.update_status_json(
        _event(orch, event_type="workflow_run_in_progress"),
        repo_dir=tmp_path,
        commit_and_push=False,
    )

    ownerless_test = _run(
        path=".github/workflows/test_pytorch_wheels_full.yml",
        platform="linux",
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    ownerless_test.workflow_run_id = 99999999999
    out = tusj.update_status_json(
        _event(ownerless_test), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(out)
    assert doc.summary.linux.rocm.test.success == 1
    # Acceptance must not disturb the document's recorded owner.
    assert doc.trigger_workflow_run_id == 29079513704


def test_leaf_without_owner_and_mismatched_release_version_is_skipped(
    tmp_path: Path,
) -> None:
    # Same shape as above, but the leaf's release_version does not match the
    # document's: even though it happens to route to the same dated file (the
    # nightly path suffix is just the embedded date), it must still be
    # rejected since it isn't provably this release.
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
        pipeline_type="rocm",
        pipeline_phase="test",
        architectures=["gfx942"],
    )
    mismatched_test.workflow_run_id = 99999999999
    mismatched_test.classification.release_version = "7.15.0a20260619"
    out = tusj.update_status_json(
        _event(mismatched_test), repo_dir=tmp_path, commit_and_push=False
    )
    doc = _load(out)
    assert doc.summary.linux.rocm.test.success == 0
    assert doc.trigger_workflow_run_id == 29079513704


def test_per_platform_orchestrator_does_not_finalize(tmp_path: Path) -> None:
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
    tusj.update_status_json(
        _event(_prerelease_leaf_run()), repo_dir=tmp_path, commit_and_push=False
    )
    latest = tmp_path / "prereleases" / "latest.json"
    assert latest.is_symlink()
    assert latest.readlink() == Path("7.14.0/7.14.0rc1/status.json")
    # prerelease has no notion of latest_good.
    assert not (tmp_path / "prereleases" / "latest_good.json").exists()


def test_prerelease_latest_advances_to_newer_candidate(tmp_path: Path) -> None:
    tusj.update_status_json(
        _event(_prerelease_leaf_run_version("7.14.0rc1")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
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
    tusj.update_status_json(
        _event(_prerelease_leaf_run_version("7.14.0rc10")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_prerelease_leaf_run_version("7.14.0rc2")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    latest = tmp_path / "prereleases" / "latest.json"
    assert latest.readlink() == Path("7.14.0/7.14.0rc10/status.json")


def test_successive_leaves_merge_into_one_document(tmp_path: Path) -> None:
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
    run_id: int, *, attempt: int = 1, conclusion: str = "success"
) -> WorkflowRunRecord:
    run = _leaf_run()
    run.workflow_run_id = run_id
    run.run_attempt = attempt
    run.conclusion = conclusion
    base = _s3_base(run_id)
    run.tarball_url = f"{base}/tarballs/"
    run.wheels_url = f"{base}/python/"
    run.artifacts_url = f"{base}/index.html"
    return run


def _linux_native_rpm_with_urls(run_id: int, *, attempt: int = 1) -> WorkflowRunRecord:
    run = _run(
        path=".github/workflows/multi_arch_build_native_linux_packages.yml",
        platform="linux",
        pipeline_type="native_packages",
        pipeline_phase="rpm",
        architectures=["gfx942"],
    )
    run.workflow_run_id = run_id
    run.run_attempt = attempt
    run.rpm_urls = {"rpm": f"{_s3_base(run_id)}/rpm/"}
    return run


def test_urls_pin_to_build_run_and_clear_stale_on_supersede(tmp_path: Path) -> None:
    # Run A: rocm build + native rpm -> the whole URL block belongs to run A.
    tusj.update_status_json(
        _event(_linux_build_with_urls(100)), repo_dir=tmp_path, commit_and_push=False
    )
    tusj.update_status_json(
        _event(_linux_native_rpm_with_urls(100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    urls = _load(_nightly_status_path(tmp_path)).summary.linux.urls
    assert "100-linux" in urls["artifacts"]
    assert "100-linux" in urls["rpm"]

    # Run B rebuilds the ROCm build and supersedes A's failed/older build leaf.
    # The block is rebuilt from B; A's rpm URL (a run B never produced) is
    # dropped rather than left dangling next to B's artifacts.
    tusj.update_status_json(
        _event(_linux_build_with_urls(200)), repo_dir=tmp_path, commit_and_push=False
    )
    urls = _load(_nightly_status_path(tmp_path)).summary.linux.urls
    assert "200-linux" in urls["artifacts"]
    assert "200-linux" in urls["wheels"]
    assert "200-linux" in urls["tarballs"]
    assert "rpm" not in urls


def test_stale_build_event_does_not_clobber_urls(tmp_path: Path) -> None:
    # Run B (attempt 2) owns the block.
    tusj.update_status_json(
        _event(_linux_build_with_urls(200, attempt=2)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    # A late, lower-attempt event from run A loses the don't-downgrade guard, so
    # its leaf is rejected -- and it must not move the URLs either.
    tusj.update_status_json(
        _event(_linux_build_with_urls(100, attempt=1)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    urls = _load(_nightly_status_path(tmp_path)).summary.linux.urls
    assert "200-linux" in urls["artifacts"]


def test_native_urls_only_fill_for_the_owning_run(tmp_path: Path) -> None:
    # Build owned by run B; a native event from a different run A must not inject
    # its rpm URL (that would mix run ids across the block).
    tusj.update_status_json(
        _event(_linux_build_with_urls(200)), repo_dir=tmp_path, commit_and_push=False
    )
    tusj.update_status_json(
        _event(_linux_native_rpm_with_urls(100)),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert "rpm" not in _load(_nightly_status_path(tmp_path)).summary.linux.urls

    # A native event from the owning run B does populate rpm.
    tusj.update_status_json(
        _event(_linux_native_rpm_with_urls(200)),
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


def test_newer_run_in_progress_supersedes_older_terminal(tmp_path: Path) -> None:
    # Older run (100) finishes the build; newer run (200) then reports the same
    # slot as in_progress. The newer run wins even though it is not terminal, so
    # the document tracks the run that is actually current -- a stale terminal
    # from the superseded run does not stick.
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="success")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(200, conclusion="")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    leaf = _linux_build_leaf(tmp_path)
    assert leaf.run_id == 200
    assert leaf.status is Status.in_progress


def test_stale_older_run_never_overwrites_newer(tmp_path: Path) -> None:
    # Newer run (200) finishes first; the older run (100) finishes later and its
    # event arrives last. Arrival order no longer decides: the older run's
    # terminal event is rejected, so the newer run keeps the slot.
    tusj.update_status_json(
        _event(_linux_build(200, conclusion="success")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="success")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    assert _linux_build_leaf(tmp_path).run_id == 200


def test_out_of_order_completed_then_started_keeps_terminal(tmp_path: Path) -> None:
    # Same run, reordered delivery: the completed event lands before the started
    # one. The stray in_progress must not downgrade the finished leaf.
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="success")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(100, conclusion="")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    leaf = _linux_build_leaf(tmp_path)
    assert leaf.status is Status.success
    assert leaf.completed_at is not None


def test_higher_attempt_supersedes_even_when_in_progress(tmp_path: Path) -> None:
    # A re-run (attempt 2) of the same run supersedes the finished attempt 1 even
    # though the re-run is only in_progress: a higher attempt always wins.
    tusj.update_status_json(
        _event(_linux_build(100, attempt=1, conclusion="success")),
        repo_dir=tmp_path,
        commit_and_push=False,
    )
    tusj.update_status_json(
        _event(_linux_build(100, attempt=2, conclusion="")),
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


def test_newer_parent_leaf_promotes_owner_and_clears_previous_run(
    tmp_path: Path,
) -> None:
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
    assert doc.trigger_workflow_run_id == 200
    assert "linux" not in doc.pipelines.rocm.build
    assert doc.pipelines.rocm.build["windows"].run_id == 202
    assert doc.pipelines.rocm.build["windows"].status is Status.failure


# --- variant derivation (pytorch/jax py x torch/jax) -------------------------
#
# Builds fan the matrix axis out across jobs named `Build | py X | torch Y`, so
# each cell becomes one variant. Tests run a single (py, ref) per dispatch and
# name their jobs by arch, so the cell is read from the dispatch inputs instead.
# jax uses the `jax_ref` axis key. Non-matrixed pipelines (rocm) get no variants.


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
) -> WorkflowRunRecord:
    run = _run(
        path=".github/workflows/x.yml",
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


def test_reusable_matrix_nested_jobs_collapse_to_one_variant_per_cell() -> None:
    # A reusable-workflow matrix expands each cell into several nested jobs that
    # all carry the cell's prefix; they roll up into one variant (worst-of
    # status, not-terminal until every nested job finishes).
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
    assert variants[0].status is Status.in_progress
    assert variants[0].completed_at is None


def test_jax_build_variants_use_jax_ref_axis() -> None:
    run = _variant_run(
        pipeline_type="jax",
        pipeline_phase="build",
        jobs=[_job("Build | py 3.12 | jax rocm-jaxlib-v0.9.1")],
    )
    variants = tusj._derive_variants(run)
    assert variants[0].matrix == {"py": "3.12", "jax_ref": "rocm-jaxlib-v0.9.1"}


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


def test_jax_test_variant_prefers_jax_ref_input() -> None:
    run = _variant_run(
        pipeline_type="jax",
        pipeline_phase="test",
        inputs={"python_version": "3.11", "jax_ref": "rocm-jaxlib-v0.9.1"},
        jobs=[_job("Test JAX | gfx942")],
    )
    variants = tusj._derive_variants(run)
    assert variants[0].matrix == {"py": "3.11", "jax_ref": "rocm-jaxlib-v0.9.1"}


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
    completed_build.classification.platform = "windows"
    tusj._merge_run_into_document(
        doc, completed_build, tusj._create_leaf(completed_build)
    )

    leaf = doc.pipelines.pytorch.test["linux"]["gfx110X-all"]
    assert leaf.status is Status.success
    assert leaf.completed_at == "2026-06-19T15:18:00Z"
    assert leaf.variants is not None
    assert leaf.variants[0].status is Status.success
