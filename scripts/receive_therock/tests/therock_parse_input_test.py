# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for therock_parse_input payload validation."""

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from therock_parse_input import (
    PayloadValidationError,
    load_and_validate,
    load_and_validate_string,
    main,
    validate_payload,
)


def _valid_workflow_run_payload() -> dict[str, object]:
    return {
        "event_type": "workflow_run_completed",
        "repository": "ROCm/TheRock",
        "workflow_run": {"id": 12345},
    }


def test_validate_payload_accepts_workflow_run_completed() -> None:
    event = validate_payload(_valid_workflow_run_payload())

    assert event.event_type == "workflow_run_completed"
    assert event.repository == "ROCm/TheRock"
    assert event.workflow_run is not None
    assert event.workflow_run.workflow_run_id == 12345


def test_validate_payload_accepts_push_event() -> None:
    event = validate_payload(
        {
            "event_type": "push_event",
            "repository": "ROCm/TheRock",
            "ref": "refs/heads/main",
        }
    )

    assert event.event_type == "push_event"
    assert event.repository == "ROCm/TheRock"


def test_validate_payload_rejects_missing_event_type() -> None:
    with pytest.raises(
        PayloadValidationError, match="Missing required key: 'event_type'"
    ):
        validate_payload({"repository": "ROCm/TheRock"})


def test_validate_payload_rejects_unknown_event_type() -> None:
    with pytest.raises(PayloadValidationError, match="Unknown event_type"):
        validate_payload({"event_type": "unknown", "repository": "ROCm/TheRock"})


def test_validate_payload_rejects_missing_repository() -> None:
    with pytest.raises(
        PayloadValidationError, match="Missing or empty required key: 'repository'"
    ):
        validate_payload(
            {"event_type": "workflow_run_completed", "workflow_run": {"id": 1}}
        )


def test_validate_payload_rejects_missing_workflow_run_object() -> None:
    with pytest.raises(
        PayloadValidationError, match="requires a 'workflow_run' object"
    ):
        validate_payload(
            {"event_type": "workflow_run_completed", "repository": "ROCm/TheRock"}
        )


def test_validate_payload_rejects_missing_workflow_run_id() -> None:
    with pytest.raises(
        PayloadValidationError, match="workflow_run.id is missing or empty"
    ):
        validate_payload(
            {
                "event_type": "workflow_run_completed",
                "repository": "ROCm/TheRock",
                "workflow_run": {},
            }
        )


def test_validate_payload_rejects_missing_pull_request_number() -> None:
    with pytest.raises(
        PayloadValidationError, match="pull_request.number is missing or empty"
    ):
        validate_payload(
            {
                "event_type": "pull_request_event",
                "repository": "ROCm/TheRock",
                "pull_request": {"id": 11},
            }
        )


def test_validate_payload_rejects_non_dict_pull_request() -> None:
    with pytest.raises(
        PayloadValidationError, match="requires a 'pull_request' object"
    ):
        validate_payload(
            {
                "event_type": "pull_request_event",
                "repository": "ROCm/TheRock",
                "pull_request": "not-a-dict",
            }
        )


def test_validate_payload_rejects_missing_pull_request_id() -> None:
    with pytest.raises(
        PayloadValidationError, match="pull_request.id is missing or empty"
    ):
        validate_payload(
            {
                "event_type": "pull_request_event",
                "repository": "ROCm/TheRock",
                "pull_request": {"number": 11},
            }
        )


def test_validate_payload_rejects_missing_push_ref() -> None:
    with pytest.raises(
        PayloadValidationError, match="requires a top-level 'ref' field"
    ):
        validate_payload({"event_type": "push_event", "repository": "ROCm/TheRock"})


def test_load_and_validate_string_rejects_invalid_json() -> None:
    with pytest.raises(
        PayloadValidationError, match=r"Invalid JSON in unit-test source"
    ):
        load_and_validate_string("{", source="unit-test source")


def test_load_and_validate_string_rejects_non_object_top_level() -> None:
    with pytest.raises(
        PayloadValidationError, match="Expected a JSON object at top level"
    ):
        load_and_validate_string("[]")


def test_load_and_validate_reads_file_and_returns_event(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(_valid_workflow_run_payload()), encoding="utf-8")

    event = load_and_validate(payload_path)

    assert event.event_type == "workflow_run_completed"
    assert event.workflow_run is not None
    assert event.workflow_run.workflow_run_id == 12345


def test_load_and_validate_rejects_missing_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"

    with pytest.raises(PayloadValidationError, match="Payload file not found"):
        load_and_validate(missing_path)


def test_main_returns_zero_for_valid_payload_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(_valid_workflow_run_payload()), encoding="utf-8")

    rc = main([str(payload_path)])

    assert rc == 0
    stdout = capsys.readouterr().out
    parsed = json.loads(stdout)
    assert parsed["event_type"] == "workflow_run_completed"


def test_main_returns_one_for_invalid_payload_file(tmp_path: Path) -> None:
    payload_path = tmp_path / "bad_payload.json"
    payload_path.write_text("{}", encoding="utf-8")

    rc = main([str(payload_path)])

    assert rc == 1
