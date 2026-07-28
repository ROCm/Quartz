# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Producer<->receiver envelope contract test.

This is the seam the unit tests previously missed: the producer
(`build_tools/github_actions/notify_quartz.py`) and the receiver
(`scripts/receive_therock/therock_parse_input.py`) live in different packages
and were each tested in isolation against their own assumption of the envelope
shape. That let the lifecycle key drift (`dispatch_kind` vs `event_type`)
without any test failing.

This test crosses the boundary: it builds the real producer payload (GitHub API
calls mocked) and asserts it passes the receiver's `validate_payload` with the
expected `event_type`. If either side renames or restructures the top-level
envelope again, this fails loudly.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

RECEIVE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCER_DIR = REPO_ROOT / "build_tools" / "github_actions"
for p in (RECEIVE_DIR, PRODUCER_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import notify_quartz  # noqa: E402
from notify_quartz import _GithubApiResponse  # noqa: E402
from therock_parse_input import validate_payload  # noqa: E402

_REPO = "ROCm/rockrel"


def _run_obj(run_phase: str) -> dict:
    # Mirror GitHub's run object for the phase under test: a completed run reads
    # `status="completed"` with a conclusion, a started run reads `in_progress`.
    completed = run_phase == "completed"
    return {
        "id": 999,
        "name": "Multi arch build portable linux",
        "path": ".github/workflows/multi_arch_build_portable_linux.yml",
        "workflow_id": 1,
        "status": "completed" if completed else "in_progress",
        "conclusion": "success" if completed else None,
    }


def _build_producer_payload(run_phase: str) -> dict:
    """Run the real `_build_payload` with its GitHub API surface mocked."""
    with (
        mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "999"}),
        mock.patch.object(
            notify_quartz,
            "_github_api_request",
            return_value=_GithubApiResponse(body=_run_obj(run_phase), headers={}),
        ),
        mock.patch.object(notify_quartz, "_fetch_jobs", return_value=[]),
    ):
        return notify_quartz._build_payload(
            token="t",
            repo=_REPO,
            embedded_inputs={},
            captured_outputs={},
            run_conclusion="success",
            run_phase=run_phase,
            reporting_workflow="",
        )


@pytest.mark.parametrize(
    "run_phase,expected_event",
    [
        ("started", "workflow_run_in_progress"),
        ("completed", "workflow_run_completed"),
    ],
)
def test_producer_payload_passes_receiver_validation(
    run_phase: str, expected_event: str
) -> None:
    payload = _build_producer_payload(run_phase)

    # The producer's top-level envelope is exactly what the receiver expects:
    # the lifecycle marker is `event_type` (not `dispatch_kind`).
    assert set(payload) == {"event_type", "repository", "workflow_run"}
    assert payload["event_type"] == expected_event

    # And it survives the receiver's gate, parsing to the matching event.
    event = validate_payload(payload)
    assert event.event_type == expected_event
    assert event.repository == _REPO
    assert event.workflow_run is not None
    assert event.workflow_run.workflow_run_id == 999
