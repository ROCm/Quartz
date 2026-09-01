#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Tests for example_consume_status.py, the runnable tutorial consumer.

Standard-library only (unittest), matching the example it exercises. The example
lives under docs/status-json/ (a non-importable package path because of the
hyphen), so run these directly or via discover:

    python3 docs/status-json/tests/example_consume_status_test.py

    python3 -m unittest discover -s docs/status-json/tests -p '*_test.py'

Fixtures are minimal status documents built inline rather than the full schema
reference: these tests cover the example's gate and I/O glue (ready_platform,
set_github_outputs, main's ready/not-ready branching), which touch only a handful
of fields. The read helper itself is covered by read_status_json_test.py.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

EXAMPLE_DIR = Path(__file__).resolve().parent.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

import example_consume_status as example  # noqa: E402


def _doc(build_status: str = "success", schema_version: str = "2.0") -> dict:
    """A minimal status document with just the fields the example reads.

    Gates on summary.<PLATFORM>.<PIPELINE>.build.status (linux/rocm by default),
    so only that path plus the release metadata needs to be present.
    """
    return {
        "schema_version": schema_version,
        "rocm_version": "7.13.0a20260408",
        "build_date": "20260408",
        "summary": {
            "overall_status": "in_progress",
            "linux": {
                "status": "in_progress",
                "architectures": ["gfx942"],
                "urls": {},
                "rocm": {"build": {"status": build_status}},
            },
        },
    }


def _read_outputs(path: str) -> dict[str, str]:
    """Parse a $GITHUB_OUTPUT file (name=value per line) into a dict."""
    result: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        name, _, value = line.partition("=")
        result[name] = value
    return result


class SetGithubOutputsTest(unittest.TestCase):
    def _output_path(self) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_writes_and_appends(self):
        path = self._output_path()
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": path}):
            example.set_github_outputs(ready="true", rocm_version="7.13.0a20260408")
            example.set_github_outputs(build_date="20260408")
        self.assertEqual(
            _read_outputs(path),
            {
                "ready": "true",
                "rocm_version": "7.13.0a20260408",
                "build_date": "20260408",
            },
        )

    def test_noop_without_env(self):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(example.set_github_outputs(ready="true"))


class ReadyPlatformTest(unittest.TestCase):
    def test_success_returns_platform(self):
        platform = example.ready_platform(example.StatusDocument(_doc()))
        self.assertIsNotNone(platform)
        self.assertEqual(platform.name, "linux")

    def test_platform_absent_returns_none(self):
        doc = _doc()
        del doc["summary"]["linux"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(example.ready_platform(example.StatusDocument(doc)))

    def test_build_not_success_returns_none(self):
        doc = example.StatusDocument(_doc(build_status="failure"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(example.ready_platform(doc))


class MainTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.close()
        self.output_path = handle.name
        self.addCleanup(lambda: Path(self.output_path).unlink(missing_ok=True))

    def _run(self, *, load_return=None, load_error=None):
        load_mock = mock.Mock()
        if load_error is not None:
            load_mock.side_effect = load_error
        else:
            load_mock.return_value = load_return
        with mock.patch.dict(
            os.environ, {"GITHUB_OUTPUT": self.output_path}
        ), mock.patch.object(example, "load_status", load_mock), mock.patch.object(
            example, "process"
        ) as process_mock, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(
            io.StringIO()
        ):
            example.main()
        return _read_outputs(self.output_path), process_mock

    def test_ready_build_writes_outputs_and_processes(self):
        outputs, process_mock = self._run(load_return=example.StatusDocument(_doc()))
        self.assertEqual(outputs["ready"], "true")
        self.assertEqual(outputs["rocm_version"], "7.13.0a20260408")
        self.assertEqual(outputs["build_date"], "20260408")
        process_mock.assert_called_once()

    def test_fetch_failure_reports_not_ready_without_processing(self):
        outputs, process_mock = self._run(load_error=OSError("boom"))
        self.assertEqual(outputs["ready"], "false")
        self.assertNotIn("rocm_version", outputs)
        process_mock.assert_not_called()

    def test_unsupported_schema_fails_without_processing(self):
        # A new schema major is permanent, not transient: main() exits non-zero
        # (via sys.exit) instead of reporting ready=false, and never reaches
        # process(). The guard runs before ready_platform, so bailing here proves
        # nothing downstream ran.
        with self.assertRaises(SystemExit) as caught:
            self._run(load_return=example.StatusDocument(_doc(schema_version="3.0")))
        self.assertNotEqual(caught.exception.code, 0)

    def test_failed_gate_writes_build_id_but_does_not_process(self):
        outputs, process_mock = self._run(
            load_return=example.StatusDocument(_doc(build_status="failure"))
        )
        self.assertEqual(outputs["ready"], "false")
        # The gate-failure path writes all three outputs before returning, unlike
        # the not_ready() shortcuts which emit only ready=false.
        self.assertEqual(outputs["rocm_version"], "7.13.0a20260408")
        self.assertEqual(outputs["build_date"], "20260408")
        process_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
