# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Validate incoming dispatch payloads from TheRock.

Checks that the JSON payload has the required structure and contains a
known event_type before passing it to downstream scripts.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from therock_types import (
    KNOWN_EVENT_TYPES,
    WORKFLOW_RUN_EVENT_TYPES,
    TheRockDispatchEvent,
)

log = logging.getLogger(__name__)


class PayloadValidationError(Exception):
    """Raised when the dispatch payload fails validation."""

    pass


def validate_payload(raw: dict[str, object]) -> TheRockDispatchEvent:
    """Validate a dispatch payload dict and return a typed TheRockDispatchEvent.

    Common checks:
      - `event_type` exists and is one of the known kinds
      - `repository` exists and is a non-empty string

    Per-event-type checks:
      - `workflow_run_*`     -> `workflow_run` dict with a non-empty `id`
      - `pull_request_event` -> `pull_request` dict with non-empty `number` and `id`
      - `push_event`         -> top-level `ref` is non-empty (fields are
                                  flattened onto the envelope root)

    Raises PayloadValidationError when a required field is missing or invalid.
    """
    event_type = raw.get("event_type")
    if not event_type:
        raise PayloadValidationError("Missing required key: 'event_type'")
    if not isinstance(event_type, str):
        raise PayloadValidationError(
            f"'event_type' must be a string, got {type(event_type).__name__}"
        )

    if event_type not in KNOWN_EVENT_TYPES:
        raise PayloadValidationError(
            f"Unknown event_type: '{event_type}'. "
            f"Expected one of: {', '.join(sorted(KNOWN_EVENT_TYPES))}"
        )

    repo = raw.get("repository")
    if not repo or not isinstance(repo, str):
        raise PayloadValidationError("Missing or empty required key: 'repository'")

    if event_type in WORKFLOW_RUN_EVENT_TYPES:
        wr = raw.get("workflow_run")
        if not isinstance(wr, dict):
            raise PayloadValidationError(
                f"event_type '{event_type}' requires a 'workflow_run' object"
            )
        if not wr.get("id"):
            raise PayloadValidationError("workflow_run.id is missing or empty")
    elif event_type == "pull_request_event":
        pr = raw.get("pull_request")
        if not isinstance(pr, dict):
            raise PayloadValidationError(
                "event_type 'pull_request_event' requires a 'pull_request' object"
            )
        if not pr.get("number"):
            raise PayloadValidationError("pull_request.number is missing or empty")
        if not pr.get("id"):
            raise PayloadValidationError("pull_request.id is missing or empty")
    elif event_type == "push_event":
        # Push payloads are flattened onto the envelope root (no nested
        # 'push_event' key); see PushEventInput.from_dict in therock_types.py.
        # `ref` is the minimum invariant we can reliably check.
        if not raw.get("ref"):
            raise PayloadValidationError(
                "event_type 'push_event' requires a top-level 'ref' field"
            )
    else:
        raise PayloadValidationError(
            f"event_type '{event_type}' is in KNOWN_EVENT_TYPES but has no "
            "structural validation branch in validate_payload()"
        )

    log.info("Payload validated: event_type=%s repository=%s", event_type, repo)
    return TheRockDispatchEvent.from_dict(raw)


def load_and_validate(payload_path: Path) -> TheRockDispatchEvent:
    """Load a JSON file and validate its contents as a dispatch payload.

    Raises PayloadValidationError if the file is missing, unreadable (e.g. a
    directory, permission error, or invalid encoding), not valid JSON, or
    fails structural validation.
    """
    if not payload_path.exists():
        raise PayloadValidationError(f"Payload file not found: {payload_path}")

    try:
        raw_text = payload_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PayloadValidationError(
            f"Could not read payload file {payload_path}: {exc}"
        ) from exc

    return load_and_validate_string(raw_text, source=str(payload_path))


def load_and_validate_string(
    raw_json: str, *, source: str = "<inline>"
) -> TheRockDispatchEvent:
    """Parse a JSON string and validate it as a dispatch payload.

    Lets callers feed the payload directly from in-memory sources (an env var
    written by GitHub Actions, a unit-test fixture, etc.) without a temp file --
    relevant for the receiver workflow, which passes `DISPATCH_PAYLOAD`
    straight to this process instead of writing it to disk via a fragile echo.

    `source` is a human-readable origin label used only in error messages.
    """
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise PayloadValidationError(f"Invalid JSON in {source}: {exc}") from exc

    if not isinstance(payload, dict):
        raise PayloadValidationError(
            f"Expected a JSON object at top level in {source}, "
            f"got {type(payload).__name__}"
        )

    return validate_payload(payload)


def main(argv: list[str]) -> int:
    """Standalone entry point for payload validation.

    Prints validated JSON to stdout. Returns 0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(
        description="Validate a TheRock dispatch payload JSON file.",
    )
    parser.add_argument(
        "payload_file",
        help=(
            "Path to the JSON payload file, or '-' to read raw JSON from "
            "stdin (e.g. `echo \"$DISPATCH_PAYLOAD\" | therock_parse_input.py -`)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    try:
        if args.payload_file == "-":
            payload = load_and_validate_string(sys.stdin.read(), source="<stdin>")
        else:
            payload = load_and_validate(Path(args.payload_file))
    except PayloadValidationError as exc:
        log.error("Validation failed: %s", exc)
        return 1

    print(json.dumps(payload.raw, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
