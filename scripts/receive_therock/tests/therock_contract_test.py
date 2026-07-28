# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Producer<->receiver envelope contract test.

This is the seam the unit tests previously missed: the producer
(`notify_quartz.py`) and the receiver
(`scripts/receive_therock/therock_parse_input.py`) live in different repos and
were each tested in isolation against their own assumption of the envelope
shape. That let the lifecycle key drift (`dispatch_kind` vs `event_type`)
without any test failing.

This test crosses the boundary: it builds the real producer payload (GitHub API
calls mocked) and asserts it passes the receiver's `validate_payload` with the
expected `event_type`. If either side renames or restructures the top-level
envelope again, this fails loudly.

The producer lives in ROCm/TheRock, not Quartz, so its module is downloaded
from TheRock `main` at test time (see the `notify_quartz` fixture). The test
tracks the producer's tip so real envelope drift is caught; download/network
failures `skip` rather than fail so infra flakiness never blocks a merge.
"""

import importlib
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

RECEIVE_DIR = Path(__file__).resolve().parents[1]
if str(RECEIVE_DIR) not in sys.path:
    sys.path.insert(0, str(RECEIVE_DIR))

from therock_parse_input import validate_payload  # noqa: E402

_REPO = "ROCm/rockrel"

_NOTIFY_QUARTZ_URL = (
    "https://raw.githubusercontent.com/ROCm/TheRock/main/"
    "build_tools/github_actions/notify_quartz.py"
)

@pytest.fixture(scope="module")
def notify_quartz(tmp_path_factory):
    """Download the producer module from TheRock `main` and import it.

    Skips (never fails) when the download cannot complete, so offline dev runs
    and transient network errors do not block the suite.
    """
    dest_dir = tmp_path_factory.mktemp("producer")
    dest = dest_dir / "notify_quartz.py"
    try:
        with urllib.request.urlopen(
            _NOTIFY_QUARTZ_URL, timeout=30
        ) as response:
            dest.write_bytes(response.read())
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"could not fetch notify_quartz from TheRock: {e}")

    sys.path.insert(0, str(dest_dir))
    try:
        module = importlib.import_module("notify_quartz")
        yield module
    finally:
        sys.path.remove(str(dest_dir))
        sys.modules.pop("notify_quartz", None)


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


def _build_producer_payload(notify_quartz, run_phase: str) -> dict:
    """Run the real `_build_payload` with its GitHub API surface mocked."""
    with (
        mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "999"}),
        mock.patch.object(
            notify_quartz,
            "_github_api_request",
            return_value=notify_quartz._GithubApiResponse(
                body=_run_obj(run_phase), headers={}
            ),
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
    notify_quartz, run_phase: str, expected_event: str
) -> None:
    payload = _build_producer_payload(notify_quartz, run_phase)

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
