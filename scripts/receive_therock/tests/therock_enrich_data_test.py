# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_enrich_data: the GitHub-API enrichment step.

Surfaces exercised:
  - `enrich_payload`: the gate logic (which events get enriched) and the
    fetch / empty / error branches, with a mocked `GitHubAPI`.
  - `GitHubAPI.get_workflow_run_jobs`: pagination and response-shape
    validation, with `GitHubAPI.get` stubbed so no real HTTP/`gh` calls
    are made.
  - `GitHubAPI.__init__`/`.get`: the three-tier authentication and
    transport selection (GITHUB_TOKEN / gh CLI / unauthenticated urllib),
    with `shutil.which` and `subprocess.run` stubbed.
  - `main`: the CLI entry point's end-to-end JSON output contract.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import therock_enrich_data as ted  # noqa: E402
from therock_enrich_data import (
    GitHubAPI,
    GitHubAPIError,
    enrich_payload,
    main,
)  # noqa: E402
from therock_types import TheRockDispatchEvent  # noqa: E402


def _event(
    *,
    event_type: str = "workflow_run_completed",
    repository: str = "ROCm/TheRock",
    run_id: int | None = 12000000001,
    run_attempt: int | None = None,
) -> TheRockDispatchEvent:
    """Build a dispatch event via the wire `from_dict` path.

    `run_id=None` omits the `workflow_run` block entirely (no `id` key would
    otherwise default to 0 anyway); pass an int to attach a run.
    `run_attempt=None` omits the `run_attempt` key, so `WorkflowRunRecord`
    defaults it to 1; pass an int (e.g. 2, for a rerun) to override it.
    """
    raw: dict = {"event_type": event_type, "repository": repository}
    if run_id is not None:
        raw["workflow_run"] = {
            "id": run_id,
            "path": ".github/workflows/multi_arch_build_portable_linux.yml",
            "status": "completed",
            "conclusion": "success",
        }
        if run_attempt is not None:
            raw["workflow_run"]["run_attempt"] = run_attempt
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

    gh.get_workflow_run_jobs.assert_called_once_with("ROCm/TheRock", 12000000001, 1)
    assert event.workflow_run is not None
    api_jobs = event.workflow_run.api_jobs
    assert api_jobs is not None and len(api_jobs) == 2
    assert [j.name for j in api_jobs] == ["build", "test"]
    assert event.workflow_run.enrichment_errors == []


def test_fetch_jobs_passes_dispatch_run_attempt_not_latest() -> None:
    # A rerun (run_attempt=2) must be fetched via that specific attempt, not
    # via the API's `filter=latest` default, which could by now point at a
    # newer attempt than the one this dispatch describes.
    event = _event(run_attempt=2)
    gh = MagicMock(spec=GitHubAPI)
    gh.get_workflow_run_jobs.return_value = [_job(1, "build")]

    enrich_payload(event, fetch_jobs=True, gh=gh)

    gh.get_workflow_run_jobs.assert_called_once_with("ROCm/TheRock", 12000000001, 2)


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

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99, 1)

    assert [j["id"] for j in jobs] == [1, 2]
    assert gh.get.call_count == 1


def test_pagination_targets_specific_run_attempt() -> None:
    # Not the unqualified (filter=latest) .../runs/{id}/jobs endpoint: a
    # rerun after dispatch but before enrichment must not silently pull
    # jobs from a newer attempt than the one this dispatch describes.
    gh = _api_with_pages([[_job(1, "a")]])

    gh.get_workflow_run_jobs("ROCm/TheRock", 99, 3)

    (url,) = (c.args[0] for c in gh.get.call_args_list)
    assert "/actions/runs/99/attempts/3/jobs" in url


def test_pagination_walks_until_short_page() -> None:
    full_page = [_job(i, f"job{i}") for i in range(100)]
    second_page = [_job(100, "job100")]
    gh = _api_with_pages([full_page, second_page])

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99, 1)

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

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99, 1)

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

    jobs = gh.get_workflow_run_jobs("ROCm/TheRock", 99, 1)

    assert gh.get.call_count == 3
    assert len(jobs) == 300
    assert "MAX_PAGES reached" in caplog.text


def test_get_workflow_run_jobs_rejects_non_object_response() -> None:
    # A top-level JSON array (or any non-object) must raise GitHubAPIError,
    # the same exception enrich_payload already catches and records, rather
    # than an AttributeError from calling .get() on it.
    gh = GitHubAPI(token="x")
    gh.get = MagicMock(return_value=["not", "an", "object"])

    with pytest.raises(GitHubAPIError, match="expected a JSON object"):
        gh.get_workflow_run_jobs("ROCm/TheRock", 99, 1)


def test_get_workflow_run_jobs_rejects_non_list_jobs_field() -> None:
    # A `{"jobs": null}` (or non-list `jobs`) response must raise
    # GitHubAPIError rather than a TypeError from iterating/len()-ing it.
    gh = GitHubAPI(token="x")
    gh.get = MagicMock(return_value={"jobs": None})

    with pytest.raises(GitHubAPIError, match="'jobs' field"):
        gh.get_workflow_run_jobs("ROCm/TheRock", 99, 1)


def test_get_workflow_run_jobs_response_shape_error_is_caught_by_enrich_payload() -> (
    None
):
    # The response-shape errors above must flow through the same
    # GitHubAPIError handling enrich_payload already has for API failures.
    event = _event()
    gh = MagicMock(spec=GitHubAPI)
    gh.get_workflow_run_jobs.side_effect = GitHubAPIError("expected a JSON object")

    enrich_payload(event, fetch_jobs=True, gh=gh)

    assert event.workflow_run is not None
    assert event.workflow_run.api_jobs is None
    assert len(event.workflow_run.enrichment_errors) == 1


# --- GitHubAPI authentication / transport selection -----------------------


def test_github_token_used_as_bearer_and_skips_gh_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    which_mock = MagicMock()
    monkeypatch.setattr(ted.shutil, "which", which_mock)

    gh = GitHubAPI(token="tok123")

    which_mock.assert_not_called()
    assert gh._get_headers()["Authorization"] == "Bearer tok123"
    assert gh._gh_cli_path is None


def test_github_token_env_var_is_used_when_no_token_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "envtok")
    monkeypatch.setattr(ted.shutil, "which", MagicMock())

    gh = GitHubAPI()

    assert gh._get_headers()["Authorization"] == "Bearer envtok"


def test_authenticated_gh_selects_cli_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(ted.shutil, "which", MagicMock(return_value="/usr/bin/gh"))
    run_mock = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(ted.subprocess, "run", run_mock)

    gh = GitHubAPI()

    assert gh._gh_cli_path == "/usr/bin/gh"
    # Scoped to the active github.com account only: an unrelated broken
    # login on another host (e.g. a stale enterprise account) must not
    # cause this probe to fail and fall back to unauthenticated requests.
    run_mock.assert_called_once_with(
        ["/usr/bin/gh", "auth", "status", "--active", "--hostname", "github.com"],
        capture_output=True,
        text=True,
        timeout=ted.DEFAULT_TIMEOUT_SECONDS,
    )


def test_no_gh_installed_falls_back_to_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(ted.shutil, "which", MagicMock(return_value=None))

    gh = GitHubAPI()

    assert gh._gh_cli_path is None


def test_failed_gh_auth_status_falls_back_to_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failing probe (e.g. the active github.com account itself has no
    # valid session) must not select the CLI transport.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(ted.shutil, "which", MagicMock(return_value="/usr/bin/gh"))
    run_mock = MagicMock(return_value=MagicMock(returncode=1))
    monkeypatch.setattr(ted.subprocess, "run", run_mock)

    gh = GitHubAPI()

    assert gh._gh_cli_path is None


def test_get_via_gh_cli_targets_github_com_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = GitHubAPI(token="x")
    gh._gh_cli_path = "/usr/bin/gh"
    run_mock = MagicMock(return_value=MagicMock(returncode=0, stdout="{}"))
    monkeypatch.setattr(ted.subprocess, "run", run_mock)

    gh._get_via_gh_cli("https://api.github.com/repos/ROCm/TheRock")

    args = run_mock.call_args.args[0]
    assert args[:3] == ["/usr/bin/gh", "api", "/repos/ROCm/TheRock"]
    assert args[3:] == ["--hostname", "github.com"]


def test_get_uses_gh_cli_transport_when_selected_and_unauthenticated() -> None:
    gh = GitHubAPI(token="x")
    gh._token = ""
    gh._gh_cli_path = "/usr/bin/gh"
    gh._get_via_gh_cli = MagicMock(return_value={"ok": True})
    gh._get_via_urllib = MagicMock()

    result = gh.get("https://api.github.com/x")

    gh._get_via_gh_cli.assert_called_once()
    gh._get_via_urllib.assert_not_called()
    assert result == {"ok": True}


def test_get_uses_urllib_transport_when_token_present_even_with_gh_selected() -> None:
    # A token always wins the transport choice, even if a gh CLI path was
    # somehow already set (e.g. reused client, token set after construction).
    gh = GitHubAPI(token="x")
    gh._gh_cli_path = "/usr/bin/gh"
    gh._get_via_gh_cli = MagicMock()
    gh._get_via_urllib = MagicMock(return_value={"ok": True})

    result = gh.get("https://api.github.com/x")

    gh._get_via_urllib.assert_called_once()
    gh._get_via_gh_cli.assert_not_called()
    assert result == {"ok": True}


# --- main(): end-to-end CLI JSON output -----------------------------------


def test_main_end_to_end_json_matches_dispatch_event_contract(
    tmp_path: Path,
) -> None:
    # Verifies the JSON main() actually emits -- after dataclasses.asdict()
    # and dropping `raw` -- still has the shape/values the next pipeline
    # stage expects, not just that enrich_payload's return value looks right
    # in-memory.
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "event_type": "workflow_run_completed",
                "repository": "ROCm/TheRock",
                "workflow_run": {
                    "id": 12000000001,
                    "run_attempt": 1,
                    "path": ".github/workflows/multi_arch_build_portable_linux.yml",
                    "status": "completed",
                    "conclusion": "success",
                },
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "out.json"

    rc = main(
        [
            "--payload_file",
            str(payload_path),
            "--output",
            str(output_path),
        ]
    )

    assert rc == 0
    dumped = json.loads(output_path.read_text(encoding="utf-8"))

    assert "raw" not in dumped
    assert dumped["event_type"] == "workflow_run_completed"
    assert dumped["repository"] == "ROCm/TheRock"
    assert dumped["pull_request"] is None
    assert dumped["push_event"] is None
    wr = dumped["workflow_run"]
    assert wr["workflow_run_id"] == 12000000001
    assert wr["run_attempt"] == 1
    assert wr["api_jobs"] is None
    assert wr["enrichment_errors"] == []


def test_main_end_to_end_with_fetch_jobs_includes_typed_api_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "event_type": "workflow_run_completed",
                "repository": "ROCm/TheRock",
                "workflow_run": {"id": 12000000001, "run_attempt": 1},
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "out.json"
    client = MagicMock(spec=GitHubAPI)
    client.get_workflow_run_jobs.return_value = [_job(1, "build")]
    monkeypatch.setattr(ted, "GitHubAPI", MagicMock(return_value=client))

    rc = main(
        [
            "--payload_file",
            str(payload_path),
            "--fetch-jobs",
            "--output",
            str(output_path),
        ]
    )

    assert rc == 0
    dumped = json.loads(output_path.read_text(encoding="utf-8"))
    api_jobs = dumped["workflow_run"]["api_jobs"]
    assert api_jobs is not None and len(api_jobs) == 1
    assert api_jobs[0]["name"] == "build"
