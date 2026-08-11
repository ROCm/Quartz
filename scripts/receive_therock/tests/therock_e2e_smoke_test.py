# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""End-to-end smoke test for the full receive pipeline.

Drives `therock_process_data.main` (parse -> enrich -> classify ->
update_status_json) over real-shaped `DISPATCH_PAYLOAD` fixtures, exercising
the whole nightly sequence: build/native leaves land the release in a capped
`in_progress` state, then the top-level `multi_arch_release` completed event
finalizes the document to `success`.

Offline + deterministic:
  - `--no-fetch-jobs` so enrichment never touches the GitHub API,
  - `--status-repo <tmp>` with dry-run (no `--commit-and-push`), so the only
    side effects are files written under the temp tree,
  - `_utc_now` frozen past the fixture timestamps so the clock-drift guard
    passes without NTP.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import therock_process_data as tpd  # noqa: E402
import therock_update_status_json as tusj  # noqa: E402
from therock_status_document import Status, StatusDocument  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_NIGHTLY_DATE = "20260619"

_LINUX_BUILD = "nightly_build_portable_linux_completed.json"
_WINDOWS_BUILD = "nightly_build_windows_completed.json"
_NATIVE_DEB = "nightly_build_native_linux_packages_deb_completed.json"
_RELEASE = "nightly_release_completed.json"


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tusj,
        "_utc_now",
        lambda: datetime(2026, 6, 24, 0, 0, tzinfo=timezone.utc),
    )


def _process(fixture: str, status_repo: Path) -> int:
    """Run the full pipeline for one fixture as a dry-run on-disk update."""
    return tpd.main(
        [
            "--payload-file",
            str(FIXTURES / fixture),
            "--no-fetch-jobs",
            "--status-repo",
            str(status_repo),
        ]
    )


def _load(status_repo: Path) -> StatusDocument:
    path = status_repo / "release-nightly" / _NIGHTLY_DATE / "status.json"
    return StatusDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))


def test_full_nightly_sequence_finalizes_to_success(tmp_path: Path) -> None:
    for fixture in (_LINUX_BUILD, _WINDOWS_BUILD, _NATIVE_DEB):
        assert _process(fixture, tmp_path) == 0

    mid = _load(tmp_path)
    # All three leaves are terminal-success in their rollups...
    assert mid.summary.linux.rocm.build.status is Status.success
    assert mid.summary.windows.rocm.build.status is Status.success
    assert mid.summary.linux.native_packages.deb.status is Status.success
    assert mid.completed_at is None
    assert mid.summary.overall_status is Status.in_progress

    assert _process(_RELEASE, tmp_path) == 0

    final = _load(tmp_path)
    assert final.completed_at == "2026-06-19T15:25:00Z"
    assert final.summary.overall_status is Status.success


def test_full_nightly_sequence_writes_symlink_and_latest_good(
    tmp_path: Path,
) -> None:
    for fixture in (_LINUX_BUILD, _WINDOWS_BUILD, _NATIVE_DEB, _RELEASE):
        assert _process(fixture, tmp_path) == 0

    nightly_dir = tmp_path / "release-nightly"
    latest_good = nightly_dir / "latest_good.json"

    # latest_good is a success-only snapshot and must not exist until
    # the top-level release finalizes and overall status is "success".
    assert not latest_good.exists()

    assert _process(_RELEASE, tmp_path) == 0

    latest = nightly_dir / "latest.json"
    assert latest.is_symlink()
    assert latest.readlink() == Path(_NIGHTLY_DATE) / "status.json"

    # latest_good is the success snapshot, written only after finalize.
    assert latest_good.exists() and not latest_good.is_symlink()
    snapshot = StatusDocument.from_dict(
        json.loads(latest_good.read_text(encoding="utf-8"))
    )
    assert snapshot.summary.overall_status is Status.success
    assert snapshot.build_date == _NIGHTLY_DATE


def test_dev_capture_fixture_is_gated_out(tmp_path: Path) -> None:
    rc = _process("multi_arch_build_portable_linux_completed.json", tmp_path)
    assert rc == 0
    # The candidacy gate rejects dev builds before any routing, so no status
    # output should land anywhere under the repo tree.
    assert not list(tmp_path.rglob("*.json"))
