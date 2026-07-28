# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Main entry point for processing TheRock CI dispatch payloads.

Orchestrates the receive pipeline:
  1. Parse and validate the incoming JSON payload
  2. Enrich with additional data from the GitHub API (fetches the
     run's jobs onto `workflow_run.api_jobs`)
  3. Classify derived fields (release_type / pipeline_type /
     pipeline_phase / etc.) onto the workflow_run record
  4. Update status.json for nightly / prerelease tracking: apply the
     change into a local clone of the status-data repository and push
     it. If the push loses a race (a competing update landed first),
     retry (pull + re-apply) up to 5 times with random backoff.
     Pushing is opt-in -- the default is a dry-run (apply on disk
     only); pass `--commit-and-push` to push.


The payload is read from a JSON file, supplied either as
`--payload-file` (humans and local tests) or via the
`DISPATCH_PAYLOAD_FILE` env var (set by the receiver workflow, which
writes the GitHub Actions `inputs.payload_json` to a file and exports
its path):

      python scripts/receive_therock/therock_process_data.py \\
          --payload-file tests/fixtures/dispatch/sample.json

      DISPATCH_PAYLOAD_FILE=payload.json \\
          python scripts/receive_therock/therock_process_data.py --fetch-jobs

The env var only supplies the default, so passing `--payload-file`
explicitly overrides it.
"""

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

from therock_classify import classify
from therock_enrich_data import enrich_payload
from therock_parse_input import (
    PayloadValidationError,
    load_and_validate,
    load_and_validate_string,
)
from therock_update_status_json import update_status_json

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="therock_process_data.py",
        description="Process a TheRock CI dispatch payload end-to-end.",
    )
    parser.add_argument(
        "--payload-file",
        type=Path,
        default=os.getenv("DISPATCH_PAYLOAD_FILE"),
        help=(
            "Path to the JSON dispatch payload file. Defaults to the "
            "DISPATCH_PAYLOAD_FILE env var (set by the receiver workflow, "
            "which writes inputs.payload_json to a file and exports its "
            "path)."
        ),
    )
    parser.add_argument(
        "--fetch-jobs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Fetch job details from the GitHub API during enrichment. "
            "Pass --fetch-jobs to enable, --no-fetch-jobs to disable. "
            "Default: disabled (so local fixture replay does not hit "
            "the live API). The receiver workflow always passes one of "
            "the two flags explicitly so the caller's intent is visible."
        ),
    )
    parser.add_argument(
        "--status-repo",
        default=None,
        type=Path,
        help=(
            "Path to a local clone of the status-data repository. When "
            "provided, nightly / prerelease runs trigger a status.json "
            "update under the appropriate "
            "release-nightly/<date>/ or prerelease/<rc>/ tree. When "
            "omitted, the status.json update step is skipped entirely "
            "(useful for ingest-only runs and unit replay)."
        ),
    )
    parser.add_argument(
        "--commit-and-push",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Commit and push the status.json update to the status-data "
            "repository (pull / commit / push, retried with backoff if a "
            "competing update lands first). Pass --commit-and-push to push, "
            "--no-commit-and-push for a dry-run that applies the change on "
            "disk only. Default: dry-run. CI passes --commit-and-push;"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable DEBUG logging.",
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    """Configure root logging.

    Idempotent: `basicConfig` is a no-op once the root logger has
    handlers, so calling `main()` from a host that already set up logging
    will not fight its configuration.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    # The receiver workflow exports the payload inline as DISPATCH_PAYLOAD;
    # --payload-file / DISPATCH_PAYLOAD_FILE remain for humans and replay.
    inline_payload = os.getenv("DISPATCH_PAYLOAD")
    try:
        if args.payload_file:
            log.info("Step 1: Validating payload from %s", args.payload_file)
            payload = load_and_validate(args.payload_file)
        elif inline_payload:
            log.info("Step 1: Validating inline payload from DISPATCH_PAYLOAD")
            payload = load_and_validate_string(
                inline_payload, source="DISPATCH_PAYLOAD env"
            )
        else:
            log.error(
                "No payload provided: pass --payload-file, or set "
                "DISPATCH_PAYLOAD (inline JSON) / DISPATCH_PAYLOAD_FILE (path)."
            )
            return 1
    except PayloadValidationError as exc:
        log.error("Payload validation failed: %s", exc)
        return 1

    log.info(
        "Payload OK: event_type=%s repository=%s",
        payload.event_type,
        payload.repository,
    )

    if payload.workflow_run is None:
        log.info(
            "Payload carries no workflow_run (event_type=%s); nothing to "
            "enrich or classify.",
            payload.event_type,
        )
        return 0

    # Step 2: Enrich from GitHub API
    log.info("Step 2: Enriching payload (fetch_jobs=%s)", args.fetch_jobs)
    payload = enrich_payload(payload, fetch_jobs=args.fetch_jobs)
    wr = payload.workflow_run

    if wr.enrichment_errors:
        total = len(wr.enrichment_errors)
        for i, err in enumerate(wr.enrichment_errors, start=1):
            log.warning("Enrichment warning %d/%d: %s", i, total, err)

    # Step 3: Classify derived fields on the workflow_run record.
    log.info("Step 3: Classifying derived fields")
    classify(wr)
    log.debug(
        "Classified: platform=%s pipeline_type=%s pipeline_phase=%s release_type=%s "
        "architectures=%s test_type=%s build_variant=%s",
        wr.classification.platform,
        wr.classification.pipeline_type,
        wr.classification.pipeline_phase,
        wr.release_type,
        wr.classification.architectures,
        wr.classification.test_type,
        wr.classification.build_variant,
    )

    # Step 4: Update status.json (opt-in). `update_status_json` owns the
    # full candidacy gate
    if args.status_repo is None:
        log.info("Step 4: --status-repo not provided; skipping status.json update")
    else:
        log.info(
            "Step 4: Evaluating status.json update under %s (commit_and_push=%s)",
            args.status_repo,
            args.commit_and_push,
        )
        update_status_json(
            payload,
            repo_dir=args.status_repo,
            commit_and_push=args.commit_and_push,
        )

    log.info("Processing complete.")
    # stdout is the machine-readable channel; all logs go to stderr. Emit the
    # enriched + classified payload (not the verbatim wire `raw`) and keep
    # stdout JSON-only so downstream consumers can parse it.
    dump = dataclasses.asdict(payload)
    dump.pop("raw", None)
    print(json.dumps(dump, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
