#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Regenerate `rock_workflow_inventory.json` from the live rock repos.

Snapshots two things from ROCm/TheRock and ROCm/rockrel:

1. `repos`: the list of `.github/workflows/*.yml` filenames. This is the
   source of truth `therock_workflow_registry_test.py` checks the
   WORKFLOW_SPECS / ORCHESTRATOR_SPECS tables against, so a spec can't
   reference a workflow that no longer exists in either repo.

2. `notify_quartz_calls`: for every workflow that calls the reusable
   `notify_quartz.yml`, the `with:` parameters of each call (run_phase,
   reporting_workflow, ...). This script only *records* the wiring; the
   consistency check (each `reporting_workflow` equals its own filename)
   lives in `therock_workflow_registry_test.py`, which reads this snapshot
   so the suite stays hermetic (no network / no `gh`).

Re-run this (with `gh` authenticated) whenever the upstream workflow set or
its notify_quartz wiring changes.

Usage:
    python refresh_rock_workflow_inventory.py
"""

import base64
import datetime
import json
import subprocess
from pathlib import Path

import yaml

_REPOS = ("ROCm/TheRock", "ROCm/rockrel")
_NOTIFY_QUARTZ = "notify_quartz.yml"


def _workflow_names(repo: str) -> list[str]:
    out = subprocess.check_output(
        ["gh", "api", f"repos/{repo}/contents/.github/workflows", "--jq", ".[].name"],
        text=True,
    )
    return sorted(n for n in out.splitlines() if n.endswith((".yml", ".yaml")))


def _workflow_text(repo: str, name: str) -> str:
    encoded = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/{repo}/contents/.github/workflows/{name}",
            "--jq",
            ".content",
        ],
        text=True,
    )
    return base64.b64decode(encoded).decode("utf-8")


def _notify_quartz_calls(workflow_text: str) -> list[dict[str, str]]:
    """`with:` params of every job that calls the reusable notify_quartz.yml."""
    doc = yaml.safe_load(workflow_text)
    if not isinstance(doc, dict):
        return []
    calls: list[dict[str, str]] = []
    for job in doc.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses", "")
        if isinstance(uses, str) and uses.endswith(_NOTIFY_QUARTZ):
            with_params = job.get("with", {})
            if isinstance(with_params, dict):
                calls.append({str(k): str(v) for k, v in with_params.items()})
    return calls


def _collect(repo: str) -> tuple[list[str], dict[str, list[dict[str, str]]]]:
    names = _workflow_names(repo)
    calls_by_workflow: dict[str, list[dict[str, str]]] = {}
    for name in names:
        calls = _notify_quartz_calls(_workflow_text(repo, name))
        if calls:
            calls_by_workflow[name] = calls
    return names, calls_by_workflow


def main() -> None:
    out_path = Path(__file__).with_name("rock_workflow_inventory.json")

    repos: dict[str, list[str]] = {}
    notify_quartz_calls: dict[str, dict[str, list[dict[str, str]]]] = {}
    for repo in _REPOS:
        names, calls_by_workflow = _collect(repo)
        # Fail fast rather than silently writing an empty list, which would
        # quietly weaken the registry test (specs would validate against
        # nothing).
        if not names:
            raise SystemExit(f"no workflows found for {repo}; refusing to write")
        repos[repo] = names
        notify_quartz_calls[repo] = calls_by_workflow

    now = (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    doc = {
        "_comment": (
            "Source-of-truth snapshot of workflow filenames and their "
            "notify_quartz calls in the rock repos. Refresh with: python "
            "scripts/receive_therock/tests/fixtures/"
            "refresh_rock_workflow_inventory.py"
        ),
        "_generated_at": now,
        "repos": repos,
        "notify_quartz_calls": notify_quartz_calls,
    }
    out_path.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {out_path}")
    for repo in _REPOS:
        print(
            f"  {repo}: {len(repos[repo])} workflows, "
            f"{len(notify_quartz_calls[repo])} self-report to Quartz"
        )


if __name__ == "__main__":
    main()
