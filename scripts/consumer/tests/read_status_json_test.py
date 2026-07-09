#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for read_status_json, the consumer read helper.

Standard-library only (unittest), matching the helper it exercises. The
fixture is the canonical schema reference shipped alongside the docs
(docs/status-json/status_json_reference.jsonc), so these tests double as a
check that the helper still reads a document shaped like the spec.

Run from the repository root:

    python3 -m unittest scripts.consumer.tests.read_status_json_test

or directly:

    python3 scripts/consumer/tests/read_status_json_test.py
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

CONSUMER_DIR = Path(__file__).resolve().parent.parent
if str(CONSUMER_DIR) not in sys.path:
    sys.path.insert(0, str(CONSUMER_DIR))

from read_status_json import (  # noqa: E402
    PlatformStatus,
    Status,
    build_tarball_url,
    load_status,
)

REFERENCE_JSONC = (
    CONSUMER_DIR.parents[1] / "docs" / "status-json" / "status_json_reference.jsonc"
)


# Matches either a complete JSON string literal or a // line comment. The
# string alternative comes first so the engine consumes whole "..." literals
# before it can see a // inside them; only // outside a string is left for the
# comment alternative to match.
_JSONC_STRING_OR_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*')


def _strip_jsonc_comments(text: str) -> str:
    """Strip // line comments, leaving comments inside string literals intact.

    The reference has // both as comment markers and inside URLs (https://),
    so a naive replace would corrupt the data. Matching string literals as well
    means a match starting with a quote is a string (kept verbatim); anything
    else is a comment (dropped).
    """
    return _JSONC_STRING_OR_COMMENT.sub(
        lambda m: m.group(0) if m.group(0).startswith('"') else "", text
    )


def _load_reference() -> dict:
    text = REFERENCE_JSONC.read_text()
    return json.loads(_strip_jsonc_comments(text))


class StripJsoncCommentsTest(unittest.TestCase):
    def test_keeps_double_slash_inside_strings(self):
        stripped = _strip_jsonc_comments('{"u": "https://x/y"}  // trailing')
        self.assertEqual(json.loads(stripped), {"u": "https://x/y"})

    def test_reference_parses(self):
        data = _load_reference()
        self.assertEqual(data["schema_version"], "2.0")


class StatusEnumTest(unittest.TestCase):
    def test_members_equal_wire_strings(self):
        self.assertEqual(Status.success, "success")
        self.assertEqual(Status.in_progress, "in_progress")

    def test_is_terminal(self):
        self.assertFalse(Status.in_progress.is_terminal)
        self.assertTrue(Status.success.is_terminal)
        self.assertTrue(Status.failure.is_terminal)


class BuildTarballUrlTest(unittest.TestCase):
    def test_basic(self):
        url = build_tarball_url(
            "https://host/base/", "linux", "7.13.0a20260408", "gfx942"
        )
        self.assertEqual(
            url,
            "https://host/base/therock-dist-linux-gfx942-7.13.0a20260408.tar.gz",
        )

    def test_appends_missing_trailing_slash(self):
        url = build_tarball_url("https://host/base", "linux", "1.0", "multiarch")
        self.assertEqual(
            url, "https://host/base/therock-dist-linux-multiarch-1.0.tar.gz"
        )

    def test_with_tests(self):
        url = build_tarball_url(
            "https://host/", "windows", "1.0", "gfx1100", with_tests=True
        )
        self.assertEqual(
            url, "https://host/therock-dist-windows-gfx1100-tests-1.0.tar.gz"
        )


class StatusFromReferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from read_status_json import StatusDocument

        cls.status = StatusDocument(_load_reference())

    def test_release_metadata(self):
        self.assertEqual(self.status.rocm_version, "7.13.0a20260408")
        self.assertEqual(self.status.build_date, "20260408")
        self.assertEqual(self.status.release_type, "nightly")
        self.assertEqual(self.status.schema_version, "2.0")

    def test_completion(self):
        self.assertIsNone(self.status.completed_at)
        self.assertFalse(self.status.is_complete)

    def test_overall_status(self):
        self.assertEqual(self.status.overall_status, "in_progress")

    def test_build_id(self):
        self.assertEqual(self.status.build_id, ("7.13.0a20260408", "20260408"))

    def test_platforms(self):
        self.assertEqual(self.status.platforms(), ["linux", "windows"])

    def test_platform_absent_returns_none(self):
        self.assertIsNone(self.status.platform("darwin"))

    def test_pipelines_raw_tree(self):
        run_id = self.status.pipelines["rocm"]["build"]["linux"]["run_id"]
        self.assertEqual(run_id, 12345678)


class PlatformStatusFromReferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from read_status_json import StatusDocument

        cls.status = StatusDocument(_load_reference())
        cls.linux = cls.status.platform("linux")
        cls.windows = cls.status.platform("windows")

    def test_platform_types(self):
        self.assertIsInstance(self.linux, PlatformStatus)

    def test_status_and_architectures(self):
        self.assertEqual(self.linux.status, "in_progress")
        self.assertEqual(
            self.linux.architectures, ["gfx942", "gfx1101", "gfx1200", "gfx1201"]
        )

    def test_url_lookup(self):
        self.assertEqual(
            self.linux.url("wheels"), "https://rocm.nightlies.amd.com/whl-multi-arch/"
        )
        self.assertIsNone(self.linux.url("does-not-exist"))

    def test_pipeline_build_status(self):
        self.assertEqual(self.linux.pipeline_build_status("rocm"), Status.success)
        self.assertEqual(self.linux.pipeline_build_status("jax"), Status.in_progress)
        # Windows does not run jax; the pipeline is absent.
        self.assertIsNone(self.windows.pipeline_build_status("jax"))

    def test_pipeline_test_counts(self):
        counts = self.linux.pipeline_test_counts("rocm")
        self.assertEqual(
            counts,
            {
                "success": 1,
                "failure": 1,
                "in_progress": 1,
                "cancelled": 0,
                "skipped": 0,
            },
        )

    def test_native_package_status(self):
        self.assertEqual(self.linux.native_package_status("rpm"), Status.success)
        self.assertEqual(self.linux.native_package_status("deb"), Status.in_progress)
        # Native packages are linux-only; absent on windows.
        self.assertIsNone(self.windows.native_package_status("rpm"))

    def test_tarball_url(self):
        url = self.linux.tarball_url("7.13.0a20260408", "gfx942")
        self.assertEqual(
            url,
            "https://rocm.nightlies.amd.com/tarball-multi-arch/"
            "therock-dist-linux-gfx942-7.13.0a20260408.tar.gz",
        )

    def test_tarball_url_platform_override(self):
        url = self.linux.tarball_url("1.0", "gfx90a", platform="windows")
        self.assertIn("therock-dist-windows-gfx90a-1.0.tar.gz", url)

    def test_tarball_url_with_tests(self):
        url = self.linux.tarball_url("1.0", "gfx942", with_tests=True)
        self.assertIn("therock-dist-linux-gfx942-tests-1.0.tar.gz", url)


class LoadStatusTest(unittest.TestCase):
    def test_load_from_local_path(self):
        data = _load_reference()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(data, handle)
            path = handle.name
        try:
            status = load_status(path)
            self.assertEqual(status.rocm_version, "7.13.0a20260408")
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()
