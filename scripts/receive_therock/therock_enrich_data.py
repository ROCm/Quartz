# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Enrich dispatch payloads with additional data from the GitHub API.

For completed workflow_run dispatches, re-fetches the full job list for the
dispatch's specific run attempt (via
`GET .../actions/runs/{run_id}/attempts/{run_attempt}/jobs`) onto
`WorkflowRunRecord.api_jobs`, restoring detail the dispatcher may have
stripped to fit the workflow_dispatch inputs size cap. Fetching parent
workflow info or other metadata is out of scope for this module.

Authentication is resolved in priority order: a GITHUB_TOKEN env var
(set in CI) is sent as a Bearer token; otherwise an authenticated gh
CLI is used when available (local dev); otherwise requests fall back to
unauthenticated access, which GitHub rate-limits to 60 requests/hour.
"""

import argparse
import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from therock_types import (
    WORKFLOW_RUN_EVENT_TYPES,
    TheRockDispatchEvent,
    WorkflowJobRecord,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60


class GitHubAPIError(Exception):
    """Raised when a GitHub API request fails."""


class GitHubAPI:
    """Client for GitHub REST API requests.

    Authentication priority:
    1. GITHUB_TOKEN env var (CI environment)
    2. gh CLI if installed and authenticated (local dev)
    3. Unauthenticated (rate limited to 60 req/hour)
    """

    MAX_PAGES = 100

    # Pinned rather than following GH_HOST: this class only ever talks to
    # the public GitHub API, so the auth probe and every `gh api` call must
    # agree on the same host regardless of the environment's default.
    GITHUB_HOSTNAME = "github.com"

    def __init__(self, token: str | None = None):
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._gh_cli_path: str | None = None

        if not self._token:
            gh_path = shutil.which("gh")
            if gh_path:
                try:
                    result = subprocess.run(
                        [
                            gh_path,
                            "auth",
                            "status",
                            "--active",
                            "--hostname",
                            self.GITHUB_HOSTNAME,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=DEFAULT_TIMEOUT_SECONDS,
                    )
                    if result.returncode == 0:
                        self._gh_cli_path = gh_path
                except (subprocess.TimeoutExpired, OSError):
                    pass

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def get(
        self, url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """Send a GET request to the GitHub API and return the parsed JSON object.

        Uses the gh CLI when available and no token is set, otherwise urllib.
        """
        if self._gh_cli_path and not self._token:
            return self._get_via_gh_cli(url, timeout_seconds)
        return self._get_via_urllib(url, timeout_seconds)

    def _get_via_gh_cli(
        self, url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        api_path = url.removeprefix("https://api.github.com")
        try:
            result = subprocess.run(
                [
                    self._gh_cli_path,
                    "api",
                    api_path,
                    "--hostname",
                    self.GITHUB_HOSTNAME,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitHubAPIError(f"gh api timed out for {api_path}") from exc
        except OSError as exc:
            raise GitHubAPIError(f"Failed to run gh CLI: {exc}") from exc

        if result.returncode != 0:
            raise GitHubAPIError(f"gh api failed: {result.stderr or '(no message)'}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(f"gh api returned invalid JSON: {exc}") from exc

    def _get_via_urllib(
        self, url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        request = Request(url, headers=self._get_headers())
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise GitHubAPIError(f"HTTP {exc.code} for {url}: {exc.reason}") from exc
        except URLError as exc:
            raise GitHubAPIError(f"Network error for {url}: {exc.reason}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(f"Invalid JSON from {url}: {exc}") from exc

    def get_workflow_run_jobs(
        self, repo: str, run_id: int, run_attempt: int
    ) -> list[dict[str, Any]]:
        """Return all jobs for one attempt of a workflow run, following
        API pagination.

        The Actions API caps results per page, so this walks pages until a
        short page signals the last one, then returns the accumulated jobs.

        Returns raw GitHub API job dicts, not typed WorkflowJobRecord
        objects; this client stays HTTP-only and leaves typing to the
        caller (see enrich_payload, which maps these via
        WorkflowJobRecord.from_dict).

        `run_attempt` pins the request to
        `/actions/runs/{run_id}/attempts/{run_attempt}/jobs` for a specific
        attempt. The unqualified `/actions/runs/{run_id}/jobs` endpoint
        defaults to `filter=latest`, so if the run is rerun after the
        dispatch fires but before this executes, that endpoint would return
        jobs from the newer attempt -- silently mismatching the attempt the
        dispatch actually describes. Callers should pass the dispatch's own
        WorkflowRunRecord.run_attempt (which defaults to 1).
        """
        jobs: list[dict[str, Any]] = []
        page = 1
        per_page = 100
        base_url = (
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
            f"/attempts/{run_attempt}/jobs"
        )
        while page <= self.MAX_PAGES:
            url = f"{base_url}?per_page={per_page}&page={page}"
            data = self.get(url)
            if not isinstance(data, dict):
                raise GitHubAPIError(
                    f"Unexpected response for {url}: expected a JSON "
                    f"object, got {type(data).__name__}"
                )
            page_jobs = data.get("jobs")
            if not isinstance(page_jobs, list):
                got = "null/missing" if page_jobs is None else type(page_jobs).__name__
                raise GitHubAPIError(
                    f"Unexpected 'jobs' field in response for {url}: "
                    f"expected a list, got {got}"
                )
            jobs.extend(page_jobs)
            if len(page_jobs) < per_page:
                break
            page += 1
        else:
            log.warning(
                "get_workflow_run_jobs(%s, %d, attempt=%d): stopped after "
                "%d pages (MAX_PAGES reached); result may be incomplete",
                repo,
                run_id,
                run_attempt,
                self.MAX_PAGES,
            )
        return jobs


def enrich_payload(
    payload: TheRockDispatchEvent,
    *,
    fetch_jobs: bool = True,
    gh: GitHubAPI | None = None,
) -> TheRockDispatchEvent:
    """For workflow_run_completed dispatches, re-fetch full job details onto
    WorkflowRunRecord.api_jobs from the GitHub API (when fetch_jobs, i.e.
    the dispatcher had to strip the inline jobs to fit the size cap).

    Per-job summaries are permanently out of scope here, not just deferred:
    the `GET .../actions/runs/{run_id}/jobs` endpoint this enrichment calls
    does not return `$GITHUB_STEP_SUMMARY` content at all, so
    WorkflowJobRecord.summary stays "" after this step regardless of any
    future change to it. Producing workflows expose summaries as
    captured_outputs[<job key>].outputs.summary instead, which consumers
    should read directly off the forwarded toJSON(needs) blob -- that is the
    summary's source of truth, not WorkflowJobRecord.summary.
    """
    if payload.event_type not in WORKFLOW_RUN_EVENT_TYPES:
        log.info(
            "Skipping enrichment: %r is not a workflow_run event",
            payload.event_type,
        )
        return payload

    wr = payload.workflow_run
    if wr is None or not wr.workflow_run_id or not payload.repository:
        log.info("Skipping enrichment: missing workflow_run id or repository")
        return payload

    if not payload.event_type.endswith("_completed"):
        log.info(
            "Skipping enrichment: run %d not completed (%s)",
            wr.workflow_run_id,
            payload.event_type,
        )
        return payload

    if fetch_jobs:
        if gh is None:
            gh = GitHubAPI()
        try:
            raw_api_jobs = gh.get_workflow_run_jobs(
                payload.repository, wr.workflow_run_id, wr.run_attempt
            )
            if raw_api_jobs:
                wr.api_jobs = [WorkflowJobRecord.from_dict(j) for j in raw_api_jobs]
                log.info(
                    "Fetched %d jobs for run %d from GitHub API",
                    len(raw_api_jobs),
                    wr.workflow_run_id,
                )
        except GitHubAPIError as exc:
            log.warning(
                "Failed to fetch jobs for run %d: %s",
                wr.workflow_run_id,
                exc,
            )
            wr.enrichment_errors.append(
                f"Failed to fetch jobs for run {wr.workflow_run_id}: {exc}"
            )

    log.info(
        "Enriched run %d: %d job(s) fetched",
        wr.workflow_run_id,
        len(wr.api_jobs) if wr.api_jobs else 0,
    )
    return payload


def main(argv: list[str]) -> int:
    """Standalone entry point for data enrichment."""
    parser = argparse.ArgumentParser(
        description="Enrich a TheRock dispatch payload with GitHub API data.",
    )
    parser.add_argument(
        "--payload_file", required=True, help="Path to the JSON payload file."
    )
    parser.add_argument(
        "--fetch-jobs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Fetch job details from the GitHub API.",
    )
    parser.add_argument(
        "--output", help="Write enriched payload to this file (default: stdout)."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    raw = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
    payload = TheRockDispatchEvent.from_dict(raw)
    enrich_payload(payload, fetch_jobs=args.fetch_jobs)

    # Only dump the enriched payload, not the raw input
    dump = dataclasses.asdict(payload)
    dump.pop("raw", None)
    output = json.dumps(dump, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        log.info("Wrote enriched payload to %s", args.output)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
