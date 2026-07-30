# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_process_data main orchestration."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import therock_process_data as tpd  # noqa: E402
from therock_types import (  # noqa: E402
    Classification,
    TheRockDispatchEvent,
    WorkflowRunRecord,
)


def _make_run() -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=12000000001,
        run_number=1,
        run_attempt=1,
        name="Workflow",
        display_title="Workflow",
        trigger_event="workflow_dispatch",
        path=".github/workflows/multi_arch_build_portable_linux.yml",
        status="completed",
        conclusion="success",
        head_branch="main",
        head_sha="deadbeef",
        workflow_id=1,
        html_url="https://example/runs/1",
        created_at=datetime(2026, 4, 8, 1, 0, tzinfo=timezone.utc),
        run_started_at=datetime(2026, 4, 8, 1, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 8, 1, 30, tzinfo=timezone.utc),
        actor_login="octocat",
        pr_number=None,
        pr_title=None,
        release_type="nightly",
        rocm_version="7.0.0a20260408",
        inputs={"amdgpu_families": "gfx942"},
        env={},
        parent_workflow=None,
        referenced_workflows=[],
        trigger_workflow_run_id=None,
        jobs=[],
        classification=Classification(
            platform="linux",
            pipeline_type="rocm",
            pipeline_phase="build",
            architectures=["gfx942"],
            release_version="7.0.0a20260408",
        ),
    )


def _make_event(*, workflow_run: WorkflowRunRecord | None) -> TheRockDispatchEvent:
    return TheRockDispatchEvent(
        event_type="workflow_run_completed",
        repository="ROCm/TheRock",
        action="",
        workflow_run=workflow_run,
        pull_request=None,
        push_event=None,
        raw={"source": "wire"},
    )


@pytest.fixture(autouse=True)
def _clear_dispatch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # main() reads payloads from DISPATCH_PAYLOAD / DISPATCH_PAYLOAD_FILE and
    # build_parser() defaults --payload-file to DISPATCH_PAYLOAD_FILE. Clear
    # both so an exported value in the ambient env can't leak into a test that
    # expects the inline path (or no payload at all).
    monkeypatch.delenv("DISPATCH_PAYLOAD", raising=False)
    monkeypatch.delenv("DISPATCH_PAYLOAD_FILE", raising=False)


def test_main_errors_when_no_payload() -> None:
    assert tpd.main([]) == 1


def test_main_errors_on_invalid_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"bad": 1}')

    assert tpd.main([]) == 1


def test_main_prefers_payload_file_over_inline_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload_file = tmp_path / "payload.json"
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"ignored": true}')

    event = _make_event(workflow_run=None)
    load_file = MagicMock(return_value=event)
    load_inline = MagicMock()
    monkeypatch.setattr(tpd, "load_and_validate", load_file)
    monkeypatch.setattr(tpd, "load_and_validate_string", load_inline)

    assert tpd.main(["--payload-file", str(payload_file)]) == 0
    load_file.assert_called_once_with(payload_file)
    load_inline.assert_not_called()


def test_main_uses_payload_file_env_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload_file = tmp_path / "payload.json"
    monkeypatch.setenv("DISPATCH_PAYLOAD_FILE", str(payload_file))

    event = _make_event(workflow_run=None)
    load_file = MagicMock(return_value=event)
    monkeypatch.setattr(tpd, "load_and_validate", load_file)

    assert tpd.main([]) == 0
    load_file.assert_called_once_with(payload_file)


def test_main_uses_inline_payload_when_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"event_type":"workflow_run_completed"}')
    event = _make_event(workflow_run=None)
    load_inline = MagicMock(return_value=event)
    monkeypatch.setattr(tpd, "load_and_validate_string", load_inline)

    assert tpd.main([]) == 0
    load_inline.assert_called_once()


def test_main_returns_zero_when_no_workflow_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _make_event(workflow_run=None)
    monkeypatch.setattr(tpd, "load_and_validate_string", MagicMock(return_value=event))
    enrich_mock = MagicMock()
    monkeypatch.setattr(tpd, "enrich_payload", enrich_mock)
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"event_type":"push_event"}')

    assert tpd.main([]) == 0
    enrich_mock.assert_not_called()


def test_main_full_flow_without_status_repo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _make_run()
    event = _make_event(workflow_run=run)
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"event_type":"workflow_run_completed"}')

    monkeypatch.setattr(tpd, "load_and_validate_string", MagicMock(return_value=event))
    enrich_mock = MagicMock(return_value=event)
    classify_mock = MagicMock()
    update_mock = MagicMock()
    monkeypatch.setattr(tpd, "enrich_payload", enrich_mock)
    monkeypatch.setattr(tpd, "classify", classify_mock)
    monkeypatch.setattr(tpd, "update_status_json", update_mock)

    assert tpd.main([]) == 0
    enrich_mock.assert_called_once_with(event, fetch_jobs=False)
    classify_mock.assert_called_once_with(run)
    update_mock.assert_not_called()

    dumped = json.loads(capsys.readouterr().out)
    assert dumped["event_type"] == "workflow_run_completed"
    assert "raw" not in dumped
    assert dumped["workflow_run"]["workflow_run_id"] == 12000000001


def test_main_passes_fetch_jobs_flag_to_enrich(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _make_event(workflow_run=_make_run())
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"event_type":"workflow_run_completed"}')
    monkeypatch.setattr(tpd, "load_and_validate_string", MagicMock(return_value=event))
    enrich_mock = MagicMock(return_value=event)
    monkeypatch.setattr(tpd, "enrich_payload", enrich_mock)
    monkeypatch.setattr(tpd, "classify", MagicMock())
    monkeypatch.setattr(tpd, "update_status_json", MagicMock())

    assert tpd.main(["--fetch-jobs"]) == 0
    enrich_mock.assert_called_once_with(event, fetch_jobs=True)


def test_main_with_status_repo_calls_update_status_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run = _make_run()
    event = _make_event(workflow_run=run)
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"event_type":"workflow_run_completed"}')

    monkeypatch.setattr(tpd, "load_and_validate_string", MagicMock(return_value=event))
    monkeypatch.setattr(tpd, "enrich_payload", MagicMock(return_value=event))
    monkeypatch.setattr(tpd, "classify", MagicMock())
    update_mock = MagicMock(return_value=tmp_path / "status.json")
    monkeypatch.setattr(tpd, "update_status_json", update_mock)

    status_repo = tmp_path / "status-repo"
    assert tpd.main(["--status-repo", str(status_repo), "--commit-and-push"]) == 0
    update_mock.assert_called_once_with(
        event, repo_dir=status_repo, commit_and_push=True
    )


def test_main_status_repo_dry_run_passes_commit_and_push_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = _make_event(workflow_run=_make_run())
    monkeypatch.setenv("DISPATCH_PAYLOAD", '{"event_type":"workflow_run_completed"}')
    monkeypatch.setattr(tpd, "load_and_validate_string", MagicMock(return_value=event))
    monkeypatch.setattr(tpd, "enrich_payload", MagicMock(return_value=event))
    monkeypatch.setattr(tpd, "classify", MagicMock())
    update_mock = MagicMock(return_value=tmp_path / "status.json")
    monkeypatch.setattr(tpd, "update_status_json", update_mock)

    status_repo = tmp_path / "status-repo"
    assert tpd.main(["--status-repo", str(status_repo)]) == 0
    update_mock.assert_called_once_with(
        event, repo_dir=status_repo, commit_and_push=False
    )


def test_build_parser_defaults() -> None:
    args = tpd.build_parser().parse_args([])

    assert args.fetch_jobs is False
    assert args.commit_and_push is False
    assert args.status_repo is None
    assert args.verbose is False
