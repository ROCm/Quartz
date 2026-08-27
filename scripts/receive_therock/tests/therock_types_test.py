# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_types: version regexes, helpers, and `from_dict` parsers."""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from therock_types import (  # noqa: E402
    RELEASE_VERSION_DEV_RE,
    RELEASE_VERSION_NIGHTLY_RE,
    RELEASE_VERSION_PRERELEASE_RE,
    PullRequestInput,
    PushEventInput,
    TheRockDispatchEvent,
    WorkflowJobRecord,
    WorkflowRunRecord,
    parse_gh_datetime,
    parse_quartz_tracking_id,
    _normalize_labels,
)

# --- version regexes ------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [
        ("7.13.0a20260415", "20260415"),
        ("10.0.5a19991231", "19991231"),
    ],
)
def test_nightly_regex_captures_date(version: str, expected: str) -> None:
    m = RELEASE_VERSION_NIGHTLY_RE.match(version)
    assert m is not None
    assert m.group(1) == expected


@pytest.mark.parametrize(
    "version",
    [
        "7.13.0rc1",  # prerelease, not nightly
        "7.13.0a2026041",  # 7 digits
        "7.13.0a202604155",  # 9 digits
        "7.13.0a20260415-extra",  # trailing junk
        "7.13a20260415",  # missing patch
        "7.13.0.dev0",
    ],
)
def test_nightly_regex_rejects(version: str) -> None:
    assert RELEASE_VERSION_NIGHTLY_RE.match(version) is None


@pytest.mark.parametrize(
    "version,expected",
    [
        ("7.13.0rc1", "rc1"),
        ("7.13.0rc12", "rc12"),
    ],
)
def test_prerelease_regex_captures_rc(version: str, expected: str) -> None:
    m = RELEASE_VERSION_PRERELEASE_RE.match(version)
    assert m is not None
    assert m.group(1) == expected


@pytest.mark.parametrize(
    "version",
    [
        "7.13.0a20260415",  # nightly, not prerelease
        "7.13.0rc",  # no number
        "7.13.0pre1",  # alias for the "rc" Pre-release segment (PEP 440); not canonical
        "7.13.0rc1-extra",
    ],
)
def test_prerelease_regex_rejects(version: str) -> None:
    assert RELEASE_VERSION_PRERELEASE_RE.match(version) is None


@pytest.mark.parametrize(
    "version",
    [
        "7.13.0.dev0",  # PEP 440 .dev segment
        "7.13.0.dev",  # bare .dev (no number)
        "7.13.0dev0",  # no dot before dev
        "7.13.0.dev0+g1234abc",  # .dev with a local build segment
    ],
)
def test_dev_regex_matches(version: str) -> None:
    # `.search` (not match): a `.dev`/`dev` segment anywhere marks a dev build,
    # which must never reach a release status.json.
    assert RELEASE_VERSION_DEV_RE.search(version) is not None


@pytest.mark.parametrize(
    "version",
    [
        "7.13.0a20260415",  # nightly
        "7.13.0rc1",  # prerelease
        "7.13.0",  # plain release
    ],
)
def test_dev_regex_rejects_release_versions(version: str) -> None:
    assert RELEASE_VERSION_DEV_RE.search(version) is None


# --- parse_gh_datetime ----------------------------------------------------


def test_parse_gh_datetime_none_and_empty() -> None:
    assert parse_gh_datetime(None) is None
    assert parse_gh_datetime("") is None


def test_parse_gh_datetime_z_suffix_is_tz_aware() -> None:
    dt = parse_gh_datetime("2026-04-08T01:30:00Z")
    assert dt == datetime(2026, 4, 8, 1, 30, tzinfo=timezone.utc)
    assert dt.tzinfo is not None


def test_parse_gh_datetime_invalid_raises_valueerror() -> None:
    with pytest.raises(ValueError, match="failed to parse"):
        parse_gh_datetime("not-a-date")


# --- _normalize_labels ----------------------------------------------------


def test_normalize_labels_empty_inputs() -> None:
    assert _normalize_labels(None) == []
    assert _normalize_labels([]) == []


def test_normalize_labels_strings_and_dicts() -> None:
    labels = ["ubuntu-24.04", {"name": "self-hosted"}, {"id": 5}, {"name": None}, 42]
    # plain strings pass through; dicts contribute their `name`; dicts without a
    # usable `name` (and non-str/dict entries) are dropped.
    assert _normalize_labels(labels) == ["ubuntu-24.04", "self-hosted"]


# --- WorkflowJobRecord.from_dict ------------------------------------------


def test_job_from_dict_defaults_for_missing_fields() -> None:
    job = WorkflowJobRecord.from_dict({})
    assert job.job_id == 0
    assert job.name == ""
    assert job.status == ""
    assert job.conclusion is None
    assert job.created_at is None
    assert job.runner_name == ""
    assert job.labels == []
    assert job.steps == []
    assert job.summary == ""
    assert job.metrics == {}


def test_job_from_dict_parses_full_record() -> None:
    job = WorkflowJobRecord.from_dict(
        {
            "id": 7,
            "name": "build (gfx942)",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-04-08T01:05:00Z",
            "completed_at": "2026-04-08T01:30:00Z",
            "runner_name": "gpu-runner",
            "labels": ["self-hosted", {"name": "gpu"}],
        }
    )
    assert job.job_id == 7
    assert job.conclusion == "success"
    assert job.started_at == datetime(2026, 4, 8, 1, 5, tzinfo=timezone.utc)
    assert job.labels == ["self-hosted", "gpu"]


# --- WorkflowRunRecord.from_dict ------------------------------------------


def test_run_from_dict_defaults() -> None:
    wr = WorkflowRunRecord.from_dict({})
    assert wr.workflow_run_id == 0
    assert wr.run_attempt == 1
    assert wr.path == ""
    assert wr.conclusion is None
    assert wr.release_type is None
    assert wr.jobs == []
    assert wr.parent_workflow is None
    assert wr.trigger_workflow_run_id is None


def test_run_from_dict_actor_falls_back_to_triggering_actor() -> None:
    wr = WorkflowRunRecord.from_dict({"triggering_actor": {"login": "octocat"}})
    assert wr.actor_login == "octocat"


def test_run_from_dict_first_pr_wins_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = {
        "id": 5,
        "pull_requests": [
            {"number": 1, "title": "first"},
            {"number": 2, "title": "second"},
        ],
    }
    with caplog.at_level(logging.WARNING):
        wr = WorkflowRunRecord.from_dict(raw)
    assert wr.pr_number == 1
    assert wr.pr_title == "first"
    assert any("carries 2 pull_requests" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Preferred source: the channel embedded in the propagated tracking id.
        ({"inputs": {"quartz_tracking_id": "123;nightly"}}, "nightly"),
        # quartz_tracking_id wins over a per-run release_type input.
        (
            {"inputs": {"quartz_tracking_id": "123;prerelease", "release_type": "dev"}},
            "prerelease",
        ),
        # Fallback: the direct release_type input (orchestrator / manual dispatch).
        ({"inputs": {"release_type": "prerelease"}}, "prerelease"),
        # bkc channels are recognized and pass through unchanged.
        ({"inputs": {"quartz_tracking_id": "123;nightly-bkc"}}, "nightly-bkc"),
        ({"inputs": {"release_type": "dev-bkc"}}, "dev-bkc"),
        # Top-level release_type and env RELEASE_TYPE are no longer derived.
        ({"release_type": "nightly"}, None),
        ({"env": {"RELEASE_TYPE": "dev"}}, None),
        ({}, None),
        # Unrecognized value -> coerced to None.
        ({"inputs": {"release_type": "bogus"}}, None),
    ],
)
def test_run_from_dict_release_type_resolution(raw: dict, expected: str | None) -> None:
    assert WorkflowRunRecord.from_dict(raw).release_type == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ({}, (None, None)),
        ({"quartz_tracking_id": ""}, (None, None)),
        ({"quartz_tracking_id": "123;nightly"}, (123, "nightly")),
        ({"quartz_tracking_id": "123"}, (123, None)),
        ({"quartz_tracking_id": "123;"}, (123, None)),
    ],
)
def test_parse_quartz_tracking_id(value: dict, expected: tuple) -> None:
    assert parse_quartz_tracking_id(value) == expected


@pytest.mark.parametrize("value", ["123abc;nightly", ";nightly"])
def test_parse_quartz_tracking_id_malformed_run_id_raises(value: str) -> None:
    with pytest.raises(ValueError):
        parse_quartz_tracking_id({"quartz_tracking_id": value})


def test_run_from_dict_rocm_version_precedence() -> None:
    # inputs.rocm_version wins over rocm_package_version.
    wr = WorkflowRunRecord.from_dict(
        {"inputs": {"rocm_version": "7.0.0", "rocm_package_version": "9.9.9"}}
    )
    assert wr.rocm_version == "7.0.0"


def test_run_from_dict_rocm_version_from_captured_outputs() -> None:
    wr = WorkflowRunRecord.from_dict(
        {
            "captured_outputs": {
                "setup": {"outputs": {"rocm_package_version": "7.5.0a20260601"}}
            }
        }
    )
    assert wr.rocm_version == "7.5.0a20260601"


def test_run_from_dict_rocm_version_from_package_version() -> None:
    # The tarballs / python-packages producers carry the wheel-style version
    # under `package_version`; the receiver must recognize it.
    wr = WorkflowRunRecord.from_dict({"inputs": {"package_version": "7.15.0a20260706"}})
    assert wr.rocm_version == "7.15.0a20260706"


def test_run_from_dict_rocm_version_prefers_rocm_keys_over_package_version() -> None:
    wr = WorkflowRunRecord.from_dict(
        {"inputs": {"rocm_package_version": "7.0.0", "package_version": "9.9.9"}}
    )
    assert wr.rocm_version == "7.0.0"


def test_run_from_dict_rocm_version_full_precedence_chain() -> None:
    # All five sources present at once, in one assertion, so a reorder of the
    # fallback chain is caught: rocm_version > rocm_package_version >
    # package_version > captured setup output > torch_version.
    all_present = {
        "inputs": {
            "rocm_version": "1.0.0",
            "rocm_package_version": "2.0.0",
            "package_version": "3.0.0",
            "torch_version": "2.12.0+rocm5.0.0",
        },
        "captured_outputs": {"setup": {"outputs": {"rocm_package_version": "4.0.0"}}},
    }
    assert WorkflowRunRecord.from_dict(all_present).rocm_version == "1.0.0"
    # Drop the winner, the next in the chain takes over, and so on down to the
    # captured output, then finally the framework composite version.
    del all_present["inputs"]["rocm_version"]
    assert WorkflowRunRecord.from_dict(all_present).rocm_version == "2.0.0"
    del all_present["inputs"]["rocm_package_version"]
    assert WorkflowRunRecord.from_dict(all_present).rocm_version == "3.0.0"
    del all_present["inputs"]["package_version"]
    assert WorkflowRunRecord.from_dict(all_present).rocm_version == "4.0.0"
    del all_present["captured_outputs"]
    assert WorkflowRunRecord.from_dict(all_present).rocm_version == "2.12.0+rocm5.0.0"


def test_run_from_dict_rocm_version_from_torch_version() -> None:
    # test_pytorch_wheels_full.yml is an upstream benc-uk dispatch we can't add
    # a `rocm_version` input to; it only carries `torch_version`, which still
    # embeds the ROCm version as a local-version segment.
    wr = WorkflowRunRecord.from_dict(
        {"inputs": {"torch_version": "2.12.0+rocm7.15.0a20260702"}}
    )
    assert wr.rocm_version == "2.12.0+rocm7.15.0a20260702"


def test_run_from_dict_captured_outputs_inner_precedence() -> None:
    # Within a captured output block, `rocm_package_version` outranks `version`.
    wr = WorkflowRunRecord.from_dict(
        {
            "captured_outputs": {
                "setup": {
                    "outputs": {"rocm_package_version": "7.0.0", "version": "9.9.9"}
                }
            }
        }
    )
    assert wr.rocm_version == "7.0.0"


def test_run_from_dict_captured_outputs_version_fallback() -> None:
    # `version` is used only when `rocm_package_version` is absent.
    wr = WorkflowRunRecord.from_dict(
        {"captured_outputs": {"setup": {"outputs": {"version": "7.5.0a20260601"}}}}
    )
    assert wr.rocm_version == "7.5.0a20260601"


def test_run_from_dict_parent_workflow_synthesized_from_inputs() -> None:
    wr = WorkflowRunRecord.from_dict(
        {
            "inputs": {
                "parent_run_id": "999",
                "parent_workflow": "multi_arch_release.yml",
            }
        }
    )
    assert wr.parent_workflow == {"id": 999, "name": "multi_arch_release.yml"}
    assert wr.trigger_workflow_run_id == 999


def test_run_from_dict_parent_run_id_does_not_override_parent_workflow_dict() -> None:
    wr = WorkflowRunRecord.from_dict(
        {"parent_workflow": {"id": 42, "name": "p"}, "inputs": {"parent_run_id": "7"}}
    )
    assert wr.parent_workflow == {"id": 42, "name": "p"}
    assert wr.trigger_workflow_run_id == 42


def test_run_from_dict_parses_inline_jobs() -> None:
    wr = WorkflowRunRecord.from_dict(
        {"jobs": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
    )
    assert [j.job_id for j in wr.jobs] == [1, 2]


# --- TheRockDispatchEvent.from_dict ---------------------------------------


def test_event_from_dict_populates_workflow_run() -> None:
    event = TheRockDispatchEvent.from_dict(
        {
            "event_type": "workflow_run_completed",
            "repository": "ROCm/TheRock",
            "workflow_run": {"id": 3},
        }
    )
    assert event.workflow_run is not None
    assert event.workflow_run.workflow_run_id == 3
    assert event.pull_request is None
    assert event.push_event is None
    assert event.raw["repository"] == "ROCm/TheRock"


def test_event_from_dict_empty_pr_dict_is_ignored() -> None:
    event = TheRockDispatchEvent.from_dict(
        {"event_type": "pull_request_event", "repository": "r", "pull_request": {}}
    )
    assert event.pull_request is None


def test_event_from_dict_push_only_for_push_event_type() -> None:
    raw = {
        "event_type": "push_event",
        "repository": "ROCm/TheRock",
        "ref": "refs/heads/main",
    }
    event = TheRockDispatchEvent.from_dict(raw)
    assert event.push_event is not None
    assert event.push_event.ref == "refs/heads/main"
    assert event.push_event.repository == "ROCm/TheRock"


def test_event_from_dict_defaults_for_missing_envelope_fields() -> None:
    event = TheRockDispatchEvent.from_dict({})
    assert event.event_type == ""
    assert event.repository == ""
    assert event.workflow_run is None


# --- PullRequestInput.from_dict -------------------------------------------


def test_pr_from_dict_parses_nested_and_coerces_bools() -> None:
    pr = PullRequestInput.from_dict(
        {
            "number": 12,
            "id": 9001,
            "state": "open",
            "title": "Add feature",
            "draft": 1,
            "merged": 0,
            "user": {"login": "octocat"},
            "head": {"ref": "feature", "sha": "abc"},
            "base": {"ref": "main", "sha": "def"},
            "created_at": "2026-04-08T01:00:00Z",
        }
    )
    assert pr.number == 12
    assert pr.draft is True
    assert pr.merged is False
    assert pr.user_login == "octocat"
    assert pr.head_ref == "feature"
    assert pr.base_ref == "main"
    assert pr.created_at == datetime(2026, 4, 8, 1, 0, tzinfo=timezone.utc)


def test_pr_from_dict_requires_core_keys() -> None:
    with pytest.raises(KeyError):
        PullRequestInput.from_dict({"id": 1, "state": "open"})  # missing number


# --- PushEventInput.from_dict ---------------------------------------------


def test_push_pushed_at_prefers_head_commit_timestamp() -> None:
    push = PushEventInput.from_dict(
        {
            "head_commit": {"timestamp": "2026-04-08T01:00:00Z"},
            "pushed_at": "2026-01-01T00:00:00Z",
            "commits": [{"timestamp": "2025-01-01T00:00:00Z"}],
        },
        "ROCm/TheRock",
    )
    assert push.pushed_at == datetime(2026, 4, 8, 1, 0, tzinfo=timezone.utc)
    assert push.commits_count == 1


def test_push_pushed_at_falls_back_to_commits_when_head_missing() -> None:
    push = PushEventInput.from_dict(
        {
            "head_commit": None,
            "commits": [
                {"timestamp": "2026-02-01T00:00:00Z"},
                {"timestamp": "2026-03-01T00:00:00Z"},
            ],
        },
        "ROCm/TheRock",
    )
    # walks commits in reverse -> most recent entry's timestamp.
    assert push.pushed_at == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert push.commits_count == 2


def test_push_pusher_name_then_login_fallback() -> None:
    by_name = PushEventInput.from_dict({"pusher": {"name": "alice"}}, "r")
    assert by_name.pusher == "alice"
    by_login = PushEventInput.from_dict({"pusher": {"login": "bob"}}, "r")
    assert by_login.pusher == "bob"


def test_push_defaults_when_commits_absent() -> None:
    push = PushEventInput.from_dict({}, "r")
    assert push.commits_count == 0
    assert push.pushed_at is None
    assert push.forced is False
