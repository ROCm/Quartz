# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_enrich_data: the GitHub-API enrichment step.

Two surfaces are exercised:
  - `enrich_payload`: the gate logic (which events get enriched) and the
    fetch / empty / error branches, with a mocked `GitHubAPI`.
  - `GitHubAPI.get_workflow_run_jobs`: pagination, with `GitHubAPI.get`
    stubbed so no real HTTP/`gh` calls are made.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import therock_enrich_data as ted  # noqa: E402
from therock_enrich_data import GitHubAPI, GitHubAPIError, enrich_payload  # noqa: E402
from therock_types import TheRockDispatchEvent  # noqa: E402


def _event(
    *,
    event_type: str = "workflow_run_completed",
    repository: str = "ROCm/TheRock",
    run_id: int | None = 12000000001,
) -> TheRockDispatchEvent:
    """Build a dispatch event via the wire `from_dict` path.

    `run_id=None` omits the `workflow_run` block entirely (no `id` key would
    otherwise default to 0 anyway); pass an int to attach a run.
    """
    raw: dict = {"event_type": event_type, "repository": repository}
    if run_id is not None:
        raw["workflow_run"] = {
            "id": run_id,
            "path": ".github/workflows/multi_arch_build_portable_linux.yml",
            "status": "completed",
            "conclusion": "success",
        }
    return TheRockDispatchEvent.from_dict(raw)


def _job(job_id: int, name: str) -> dict:
    return {"id": job_id, "name": name, "status": "completed", "conclusion": "success"}


# --- enrich_payload gate logic -------------------------------------------


def test_non_workflow_run_event_is_skipped() -> None:
    event = _event(event_type="push_event", run_id=None)
    gh = MagicMock(spec=GitHubAPI)

    result = enrich_payload(event, fetch_jobs=True, gh=gh)

    assert result is event
    gh.get_workflow_run_jobs.assert_not_called()


def test_missing_workflow_run_is_skipped() -> None:
    event = _event(run_id=None)
    gh = MagicMock(spec=GitHubAPI)

    enrich_payload(event, fetch_jobs=True, gh=gh)

    gh.get_workflow_run_jobs.assert_not_called()


def test_missing_run_id_is_skipped() -> None:
    # id=0 is the "missing" sentinel from WorkflowRunRecord.from_dict.
    event = _event(run_id=0)
    gh = MagicMock(spec=GitHubAPI)

    enrich_payload(event, fetch_jobs=True, gh=gh)

    gh.get_workflow_run_jobs.assert_not_called()


def test_missing_repository_is_skipped() -> None:
    event = _event(repository="")
    gh = MagicMock(spec=GitHubAPI)

    enrich_payload(event, fetch_jobs=True, gh=gh)

    gh.get_workflow_run_jobs.assert_not_called()


def test_non_completed_event_is_skipped() -> None:
    event = _event(event_type="workflow_run_in_progress")
    gh = MagicMock(spec=GitHubAPI)

    enrich_payload(event, fetch_jobs=True, gh=gh)

    gh.get_workflow_run_jobs.assert_not_called()
    assert event.workflow_run is not None
    assert event.workflow_run.api_jobs is None


def test_fetch_jobs_false_does_no_api_call() -> None:
    event = _event()
    gh = MagicMock(spec=GitHubAPI)

    enrich_payload(event, fetch_jobs=False, gh=gh)

    gh.get_workflow_run_jobs.assert_not_called()
    assert event.workflow_run is not None
    assert event.workflow_run.api_jobs is None


# --- enrich_payload fetch branches ---------------------------------------


def test_fetch_jobs_populates_api_jobs() -> None:
    event = _event()
    gh = MagicMock(spec=GitHubAPI)
    gh.get_workflow_run_jobs.return_value = [_job(1, "build"), _job(2, "test")]

    enrich_payload(event, fetch_jobs=True, gh=gh)

    gh.get_workflow_run_jobs.assert_called_once_with("ROCm/TheRock", 12000000001)
    assert event.workflow_run is not None
    api_jobs = event.workflow_run.api_jobs
    assert api_jobs is not None and len(api_jobs) == 2
    assert [j.name for j in api_jobs] == ["build", "test"]
    assert event.workflow_run.enrichment_errors == []


def test_fetch_jobs_empty_leaves_api_jobs_none() -> None:
    event = _event()
    gh = MagicMock(spec=GitHubAPI)
    gh.get_workflow_run_jobs.return_value = []

    enrich_payload(event, fetch_jobs=True, gh=gh)

    assert event.workflow_run is not None
    assert event.workflow_run.api_jobs is None
    assert event.workflow_run.enrichment_errors == []


def test_fetch_jobs_api_error_is_recorded_not_raised() -> None:
    event = _event()
    gh = MagicMock(spec=GitHubAPI)
    gh.get_workflow_run_jobs.side_effect = GitHubAPIError("boom")

    enrich_payload(event, fetch_jobs=True, gh=gh)

    assert event.workflow_run is not None
    assert event.workflow_run.api_jobs is None
    assert len(event.workflow_run.enrichment_errors) == 1
    assert "boom" in event.workflow_run.enrichment_errors[0]


def test_enrich_uses_default_client_when_none_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event()
    client = MagicMock(spec=GitHubAPI)
    client.get_workflow_run_jobs.return_value = [_job(1, "build")]
    monkeypatch.setattr(ted, "GitHubAPI", MagicMock(return_value=client))

    enrich_payload(event, fetch_jobs=True)

    client.get_workflow_run_jobs.assert_called_once()
    assert event.workflow_run is not None
    assert event.workflow_run.api_jobs is not None


# --- GitHubAPI.get_workflow_run_jobs pagination --------------------------


def _api_with_pages(pages: list[list[dict]]) -> GitHubAPI:
    """A GitHubAPI whose `get` returns each page's `{"jobs": [...]}` in turn.

    `token="x"` keeps `__init__` from probing the `gh` CLI; `get` is stubbed
    so neither urllib nor `gh` is ever invoked.
    """
    gh = GitHubAPI(token="x")
    gh.get = MagicMock(side_effect=[{"jobs": page} for page in pages])
    return gh


def test_pagination_single_short_page_stops() -> None:
    gh = _api_with_pages([[_job(1, "a"), _job(2, "b")]])

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99)

    assert [j["id"] for j in jobs] == [1, 2]
    assert gh.get.call_count == 1


def test_pagination_walks_until_short_page() -> None:
    full_page = [_job(i, f"job{i}") for i in range(100)]
    second_page = [_job(100, "job100")]
    gh = _api_with_pages([full_page, second_page])

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99)

    assert len(jobs) == 101
    assert gh.get.call_count == 2
    first_url, second_url = (c.args[0] for c in gh.get.call_args_list)
    assert "page=1" in first_url
    assert "page=2" in second_url


def test_pagination_exact_multiple_fetches_trailing_empty_page() -> None:
    # A full final page can't signal "last", so the walker requests one more
    # page and stops on the empty result.
    full_page = [_job(i, f"job{i}") for i in range(100)]
    gh = _api_with_pages([full_page, []])

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99)

    assert len(jobs) == 100
    assert gh.get.call_count == 2


def test_pagination_stops_at_max_pages_if_pages_never_go_short(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # If the "short page = last page" contract is ever violated (e.g. an API
    # bug that returns full pages forever), MAX_PAGES bounds the walk
    # instead of hanging indefinitely.
    full_page = [_job(i, f"job{i}") for i in range(100)]
    gh = _api_with_pages([full_page] * 5)
    gh.MAX_PAGES = 3

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99)

    assert gh.get.call_count == 3
    assert len(jobs) == 300
    assert "MAX_PAGES reached" in caplog.text
