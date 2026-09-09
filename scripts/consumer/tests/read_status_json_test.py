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

import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CONSUMER_DIR = Path(__file__).resolve().parent.parent
if str(CONSUMER_DIR) not in sys.path:
    sys.path.insert(0, str(CONSUMER_DIR))

from read_status_json import (  # noqa: E402
    SUPPORTED_SCHEMA_MAJOR,
    PlatformStatus,
    Status,
    UnsupportedSchemaError,
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
        self.assertEqual(data["schema_version"], "2.1")


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
        self.assertEqual(self.status.schema_version, "2.1")

    def test_build_provenance(self):
        self.assertEqual(self.status.build_variant, "release")
        self.assertEqual(
            self.status.therock_commit, "db2fd412ed7fcadf306cdcf19f09cdd998544197"
        )

    def test_pipeline_enable_flags(self):
        self.assertTrue(self.status.pytorch_enabled)
        self.assertTrue(self.status.jax_enabled)

    def test_trigger_ownership(self):
        self.assertEqual(self.status.trigger_workflow_run_id, 12340000)
        self.assertEqual(self.status.trigger_run_attempt, 1)

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


class MissingMetadataFieldsTest(unittest.TestCase):
    """A pre-2.1 document lacks the build-provenance keys entirely; the accessors
    must distinguish absent from empty, and honor the disable-only flag default."""

    def setUp(self):
        from read_status_json import StatusDocument

        self.status = StatusDocument({})

    def test_absent_build_provenance_is_none(self):
        # None (key absent) is distinct from "" (present, no signal yet).
        self.assertIsNone(self.status.build_variant)
        self.assertIsNone(self.status.therock_commit)

    def test_empty_build_provenance_is_not_none(self):
        from read_status_json import StatusDocument

        status = StatusDocument({"build_variant": "", "therock_commit": ""})
        self.assertEqual(status.build_variant, "")
        self.assertEqual(status.therock_commit, "")

    def test_absent_enable_flags_default_to_true(self):
        self.assertTrue(self.status.pytorch_enabled)
        self.assertTrue(self.status.jax_enabled)

    def test_absent_trigger_ownership_is_none(self):
        self.assertIsNone(self.status.trigger_workflow_run_id)
        self.assertIsNone(self.status.trigger_run_attempt)


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

    def test_local_pointer_file_is_not_followed(self):
        # A local file whose body looks like a "<date>/status.json" pointer must
        # raise JSONDecodeError, not silently load a sibling. The pointer
        # fallback is for raw GitHub symlinks only; local paths follow symlinks
        # natively and never reach it.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "real.json").write_text(json.dumps(_load_reference()))
            pointer = base / "latest.json"
            pointer.write_text("real.json")
            with self.assertRaises(json.JSONDecodeError):
                load_status(str(pointer))


def _fake_raw_urlopen(base_dir: Path):
    """A urlopen stand-in that emulates raw.githubusercontent.com over base_dir.

    The URL host is ignored; the path after it is resolved as a file under
    base_dir. A symlink is served the way raw does it -- as its target path text,
    not the file it points to -- so the reader's pointer-following fallback is
    exercised end to end.
    """

    def fake_urlopen(url, timeout=None):
        rel = url.split("://", 1)[1].split("/", 1)[1]
        path = base_dir / rel
        if path.is_symlink():
            return io.BytesIO(os.readlink(path).encode("utf-8"))
        return io.BytesIO(path.read_bytes())

    return fake_urlopen


class LoadStatusSymlinkTest(unittest.TestCase):
    """Back to front: a published symlink layout served the raw way resolves.

    Mirrors what the producer writes (a dated status.json plus a latest.json
    symlink whose target is "<date>/status.json") and what raw serves for that
    symlink (the bare target path), then checks load_status follows it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        dated_dir = self.base / "20260408"
        dated_dir.mkdir()
        (dated_dir / "status.json").write_text(json.dumps(_load_reference()))
        # Same relative target the producer's _update_symlinks writes.
        (self.base / "latest.json").symlink_to("20260408/status.json")

    def test_follows_symlink_pointer(self):
        with mock.patch(
            "read_status_json.urllib.request.urlopen", _fake_raw_urlopen(self.base)
        ):
            status = load_status("https://raw.example/latest.json")
        self.assertEqual(status.rocm_version, "7.13.0a20260408")

    def test_direct_dated_document_needs_no_fallback(self):
        with mock.patch(
            "read_status_json.urllib.request.urlopen", _fake_raw_urlopen(self.base)
        ):
            status = load_status("https://raw.example/20260408/status.json")
        self.assertEqual(status.rocm_version, "7.13.0a20260408")

    def test_malformed_json_is_not_treated_as_pointer(self):
        (self.base / "broken.json").write_text("{ not valid json")
        with mock.patch(
            "read_status_json.urllib.request.urlopen", _fake_raw_urlopen(self.base)
        ):
            with self.assertRaises(json.JSONDecodeError):
                load_status("https://raw.example/broken.json")


class SchemaMajorGateTest(unittest.TestCase):
    """load_status accepts any minor within the supported major and rejects a
    different (or missing) major, since a major bump is a breaking layout change.
    """

    def _load_with_version(self, schema_version):
        doc = {"rocm_version": "7.0.0"}
        if schema_version is not None:
            doc["schema_version"] = schema_version
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(doc, handle)
            path = handle.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        return load_status(path)

    def test_accepts_current_minor(self):
        status = self._load_with_version(f"{SUPPORTED_SCHEMA_MAJOR}.1")
        self.assertEqual(status.rocm_version, "7.0.0")

    def test_accepts_newer_minor(self):
        # A reader must tolerate a newer minor: it only adds optional fields.
        status = self._load_with_version(f"{SUPPORTED_SCHEMA_MAJOR}.99")
        self.assertEqual(status.rocm_version, "7.0.0")

    def test_rejects_newer_major(self):
        with self.assertRaises(UnsupportedSchemaError):
            self._load_with_version(f"{SUPPORTED_SCHEMA_MAJOR + 1}.0")

    def test_rejects_older_major(self):
        with self.assertRaises(UnsupportedSchemaError):
            self._load_with_version(f"{SUPPORTED_SCHEMA_MAJOR - 1}.0")

    def test_rejects_missing_schema_version(self):
        with self.assertRaises(UnsupportedSchemaError):
            self._load_with_version(None)

    def test_error_is_a_value_error(self):
        # Subclassing ValueError keeps existing `except ValueError` handlers working.
        with self.assertRaises(ValueError):
            self._load_with_version("3.0")


if __name__ == "__main__":
    unittest.main()
