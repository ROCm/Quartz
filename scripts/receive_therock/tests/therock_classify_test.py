#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for therock_classify version derivation."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.fspath(Path(__file__).parent.parent))

from therock_classify import (
    classify,
    derive_architectures,
    derive_deb_urls,
    derive_effective_owner_run_id,
    derive_platform_and_pipeline,
    derive_release_cdn_urls,
    derive_release_type,
    derive_release_version,
    derive_rpm_urls,
    derive_tarball_url,
)
from therock_types import ORCHESTRATOR_SPECS, WorkflowJobRecord, WorkflowRunRecord


def _make_record(
    *,
    rocm_version: str = "",
    release_type: str | None = None,
    inputs: dict | None = None,
) -> WorkflowRunRecord:
    """Minimal record carrying only the fields the version derivers read."""
    return WorkflowRunRecord(
        workflow_run_id=1,
        run_number=1,
        run_attempt=1,
        name="",
        display_title="",
        trigger_event="",
        path="",
        status="completed",
        conclusion="success",
        head_branch="",
        head_sha="",
        workflow_id=1,
        html_url="",
        created_at=None,
        run_started_at=None,
        updated_at=None,
        actor_login="",
        pr_number=None,
        pr_title=None,
        release_type=release_type,
        rocm_version=rocm_version,
        inputs=inputs or {},
        env={},
        parent_workflow=None,
        referenced_workflows=[],
        trigger_workflow_run_id=None,
        jobs=[],
    )


_SHA = "db2fd412ed7fcadf306cdcf19f09cdd998544197"


class DeriveReleaseVersionTest(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(derive_release_version(_make_record(rocm_version="")))
        self.assertIsNone(derive_release_version(_make_record(rocm_version="   ")))

    def test_wheel_versions_pass_through(self):
        for v in ("7.10.0", "7.10.0rc0", "7.10.0a20251124", "7.10.0.dev0+abc123"):
            with self.subTest(v=v):
                self.assertEqual(
                    derive_release_version(_make_record(rocm_version=v)), v
                )

    def test_framework_composite_stripped_to_rocm_part(self):
        rec = _make_record(rocm_version="torchvision-0.23.0a0+rocm7.14.0a20260520")
        self.assertEqual(derive_release_version(rec), "7.14.0a20260520")

    def test_torch_version_fallback_end_to_end(self):
        # test_pytorch_wheels_full.yml only carries `torch_version` (see
        # WorkflowRunRecord.from_dict's rocm_version fallback chain); confirm
        # the full path from raw dispatch inputs to a routable release_version.
        wr = WorkflowRunRecord.from_dict(
            {"inputs": {"torch_version": "2.12.0+rocm7.15.0a20260702"}}
        )
        self.assertEqual(derive_release_version(wr), "7.15.0a20260702")

    def test_native_nightly(self):
        rec = _make_record(rocm_version="7.14.0~20260617")
        self.assertEqual(derive_release_version(rec), "7.14.0a20260617")

    def test_native_prerelease_rpm(self):
        rec = _make_record(rocm_version="7.10.0~rc0")
        self.assertEqual(derive_release_version(rec), "7.10.0rc0")

    def test_native_prerelease_deb(self):
        rec = _make_record(rocm_version="7.10.0~pre0")
        self.assertEqual(derive_release_version(rec), "7.10.0rc0")

    def test_rpm_dev_uses_full_sha_from_ref(self):
        rec = _make_record(
            rocm_version="7.13.0~20260508gdb2fd412", inputs={"ref": _SHA}
        )
        self.assertEqual(derive_release_version(rec), f"7.13.0.dev0+{_SHA}")

    def test_rpm_dev_falls_back_to_short_sha_without_ref(self):
        rec = _make_record(rocm_version="7.13.0~20260508gdb2fd412")
        self.assertEqual(derive_release_version(rec), "7.13.0.dev0+db2fd412")

    def test_deb_dev_uses_full_sha_from_ref(self):
        rec = _make_record(
            rocm_version="7.13.0~dev20260508-25535422208", inputs={"ref": _SHA}
        )
        self.assertEqual(derive_release_version(rec), f"7.13.0.dev0+{_SHA}")

    def test_deb_dev_falls_back_to_date_runid_without_ref(self):
        rec = _make_record(rocm_version="7.13.0~dev20260508-25535422208")
        self.assertEqual(
            derive_release_version(rec), "7.13.0.dev0+20260508.25535422208"
        )

    def test_non_sha_ref_is_ignored(self):
        # A branch name in `ref` must not be used as the dev local segment.
        rec = _make_record(
            rocm_version="7.13.0~20260508gdb2fd412", inputs={"ref": "main"}
        )
        self.assertEqual(derive_release_version(rec), "7.13.0.dev0+db2fd412")


class DeriveReleaseTypeTest(unittest.TestCase):
    def test_returns_declared_type(self):
        self.assertEqual(derive_release_type(_make_record(release_type="dev")), "dev")

    def test_missing_type_returns_empty(self):
        self.assertEqual(derive_release_type(_make_record(release_type=None)), "")

    def test_independent_of_version_string(self):
        # A bare release-looking version can still be a dev publish.
        rec = _make_record(rocm_version="7.13.0", release_type="dev")
        self.assertEqual(derive_release_type(rec), "dev")


def _publish_outputs(result: str = "success") -> dict:
    """`toJSON(needs)`-shaped captured outputs with the publish job result."""
    return {"publish_to_release_buckets": {"result": result, "outputs": {}}}


def _release_record(
    *,
    path: str,
    release_type: str,
    release_version: str = "",
    source_run_id: str | None = None,
    captured_outputs: dict | None = None,
) -> WorkflowRunRecord:
    rec = _make_record(release_type=release_type)
    rec.path = path
    rec.classification.release_version = release_version
    rec.classification.source_run_id = source_run_id
    rec.captured_outputs = (
        captured_outputs if captured_outputs is not None else _publish_outputs()
    )
    return rec


class DeriveReleaseCdnUrlsTest(unittest.TestCase):
    def test_nightly_linux_has_dated_package_segment(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_release_linux.yml",
            release_type="nightly",
            release_version="7.14.0a20260619",
            source_run_id="27797822902",
        )
        urls = derive_release_cdn_urls(rec)
        base = "https://nightly.repo.amd.com/rocm/"
        self.assertEqual(urls.tarball_url, f"{base}core/tarball/")
        self.assertEqual(urls.wheels_url, f"{base}whl-next/")
        seg = f"{base}core/packages"
        self.assertEqual(urls.rpm_urls, {"rpm": f"{seg}/rpm/20260619-27797822902/"})
        self.assertEqual(urls.deb_urls, {"deb": f"{seg}/deb/20260619-27797822902/"})

    def test_prerelease_linux_has_no_dated_segment(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_release_linux.yml",
            release_type="prerelease",
            release_version="7.10.0rc2",
            source_run_id="27797822902",
        )
        urls = derive_release_cdn_urls(rec)
        base = "https://rc.repo.amd.com/rocm/"
        self.assertEqual(urls.rpm_urls, {"rpm": f"{base}core/packages/"})
        self.assertEqual(urls.deb_urls, {"deb": f"{base}core/packages/"})

    def test_windows_has_no_native_package_urls(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_release_windows.yml",
            release_type="nightly",
            release_version="7.14.0a20260619",
            source_run_id="27797822902",
        )
        urls = derive_release_cdn_urls(rec)
        self.assertEqual(
            urls.wheels_url, "https://nightly.repo.amd.com/rocm/whl-next/"
        )
        self.assertEqual(urls.rpm_urls, {})
        self.assertEqual(urls.deb_urls, {})

    def test_non_release_orchestrator_returns_none(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_build_portable_linux.yml",
            release_type="nightly",
            release_version="7.14.0a20260619",
            source_run_id="27797822902",
        )
        self.assertIsNone(derive_release_cdn_urls(rec))

    def test_dev_release_type_returns_none(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_release_linux.yml",
            release_type="dev",
            release_version="7.14.0a20260619",
            source_run_id="27797822902",
        )
        self.assertIsNone(derive_release_cdn_urls(rec))

    def test_publish_not_succeeded_returns_none(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_release_linux.yml",
            release_type="nightly",
            release_version="7.14.0a20260619",
            source_run_id="27797822902",
            captured_outputs=_publish_outputs("failure"),
        )
        self.assertIsNone(derive_release_cdn_urls(rec))

    def test_publish_job_absent_returns_none(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_release_linux.yml",
            release_type="nightly",
            release_version="7.14.0a20260619",
            source_run_id="27797822902",
            captured_outputs={},
        )
        self.assertIsNone(derive_release_cdn_urls(rec))

    def test_nightly_linux_missing_run_id_falls_back_to_base(self):
        rec = _release_record(
            path=".github/workflows/multi_arch_release_linux.yml",
            release_type="nightly",
            release_version="7.14.0a20260619",
            source_run_id=None,
        )
        urls = derive_release_cdn_urls(rec)
        base = "https://nightly.repo.amd.com/rocm/core/packages/"
        self.assertEqual(urls.rpm_urls, {"rpm": base})
        self.assertEqual(urls.deb_urls, {"deb": base})


def _native_record(*, pipeline_phase: str, repo_url: str | None) -> WorkflowRunRecord:
    rec = _make_record()
    rec.classification.pipeline_type = "native_packages"
    rec.classification.pipeline_phase = pipeline_phase
    if repo_url is not None:
        rec.captured_outputs = {
            "build_native_packages": {
                "result": "success",
                "outputs": {"package_repository_url": repo_url},
            }
        }
    return rec


class NativePackageUrlsTest(unittest.TestCase):
    def test_rpm_url_from_captured_output(self):
        url = "https://therock-nightly-artifacts.s3.amazonaws.com/27797822902-linux/packages/rpm/"
        rec = _native_record(pipeline_phase="rpm", repo_url=url)
        self.assertEqual(derive_rpm_urls(rec), {"rpm": url})

    def test_deb_url_from_captured_output(self):
        url = "https://therock-nightly-artifacts.s3.amazonaws.com/27797822902-linux/packages/deb/"
        rec = _native_record(pipeline_phase="deb", repo_url=url)
        self.assertEqual(derive_deb_urls(rec), {"deb": url})

    def test_missing_output_returns_empty(self):
        rec = _native_record(pipeline_phase="rpm", repo_url=None)
        self.assertEqual(derive_rpm_urls(rec), {})

    def test_wrong_pipeline_type_returns_empty(self):
        rec = _native_record(pipeline_phase="rpm", repo_url="https://x/repo/")
        rec.classification.pipeline_type = "rocm"
        self.assertEqual(derive_rpm_urls(rec), {})

    def test_phase_mismatch_returns_empty(self):
        # A deb-phase record must not yield an rpm URL, and vice versa.
        rec = _native_record(pipeline_phase="deb", repo_url="https://x/repo/")
        self.assertEqual(derive_rpm_urls(rec), {})

    def _reconstructable_record(self, pipeline_phase: str) -> WorkflowRunRecord:
        # the deriver must fall back to the per-run S3 repo path
        rec = _native_record(pipeline_phase=pipeline_phase, repo_url=None)
        rec.release_type = "nightly"
        rec.classification.platform = "linux"
        rec.classification.source_run_id = "27797822902"
        return rec

    def test_rpm_url_falls_back_to_reconstructed_s3_path(self):
        rec = self._reconstructable_record("rpm")
        self.assertEqual(
            derive_rpm_urls(rec),
            {
                "rpm": "https://therock-nightly-artifacts.s3.amazonaws.com/"
                "27797822902-linux/packages/rpm/x86_64/"
            },
        )

    def test_deb_url_falls_back_to_reconstructed_s3_path(self):
        rec = self._reconstructable_record("deb")
        self.assertEqual(
            derive_deb_urls(rec),
            {
                "deb": "https://therock-nightly-artifacts.s3.amazonaws.com/"
                "27797822902-linux/packages/deb/"
            },
        )

    def test_captured_output_preferred_over_reconstruction(self):
        # When the authoritative URL is present it wins over the fallback.
        rec = self._reconstructable_record("rpm")
        rec.captured_outputs = {
            "build_native_packages": {
                "result": "success",
                "outputs": {"package_repository_url": "https://authoritative/rpm/"},
            }
        }
        self.assertEqual(derive_rpm_urls(rec), {"rpm": "https://authoritative/rpm/"})

    def test_no_reconstruction_without_run_id(self):
        # Missing source_run_id -> no base -> still empty (no bogus URL).
        rec = self._reconstructable_record("rpm")
        rec.classification.source_run_id = None
        self.assertEqual(derive_rpm_urls(rec), {})


class DeriveTarballUrlTest(unittest.TestCase):
    def _build_record(self) -> WorkflowRunRecord:
        rec = _make_record()
        rec.classification.pipeline_type = "rocm"
        rec.classification.pipeline_phase = "build"
        rec.release_type = "nightly"
        rec.classification.platform = "linux"
        rec.classification.source_run_id = "27797822902"
        return rec

    def test_returns_single_tarballs_directory(self):
        # The status document only records the tarballs directory, so the
        # deriver emits one directory URL rather than a per-family map.
        rec = self._build_record()
        self.assertEqual(
            derive_tarball_url(rec),
            "https://therock-nightly-artifacts.s3.amazonaws.com/"
            "27797822902-linux/tarballs/",
        )

    def test_directory_is_independent_of_architectures_and_version(self):
        # Unlike the old per-family form, the directory needs neither the
        # release_version nor the architecture list.
        rec = self._build_record()
        rec.classification.architectures = []
        rec.classification.release_version = ""
        self.assertEqual(
            derive_tarball_url(rec),
            "https://therock-nightly-artifacts.s3.amazonaws.com/"
            "27797822902-linux/tarballs/",
        )

    def test_wrong_pipeline_returns_none(self):
        rec = self._build_record()
        rec.classification.pipeline_type = "native_packages"
        self.assertIsNone(derive_tarball_url(rec))

    def test_wrong_phase_returns_none(self):
        rec = self._build_record()
        rec.classification.pipeline_phase = "test"
        self.assertIsNone(derive_tarball_url(rec))

    def test_no_artifact_base_returns_none(self):
        rec = self._build_record()
        rec.classification.source_run_id = None
        self.assertIsNone(derive_tarball_url(rec))


def _path_record(path: str, inputs: dict | None = None) -> WorkflowRunRecord:
    rec = _make_record(inputs=inputs)
    rec.path = path
    return rec


def _job(name: str) -> WorkflowJobRecord:
    return WorkflowJobRecord(
        job_id=1,
        name=name,
        status="in_progress",
        conclusion=None,
        created_at=None,
        started_at=None,
        completed_at=None,
        runner_name="",
        labels=[],
        steps=[],
        summary="",
        metrics={},
    )


class DeriveArchitecturesTest(unittest.TestCase):
    def test_amdgpu_families_comma_string(self):
        rec = _make_record(inputs={"amdgpu_families": "gfx94x, gfx110x"})
        self.assertEqual(derive_architectures(rec), ["gfx94x", "gfx110x"])

    def test_amdgpu_families_list(self):
        rec = _make_record(inputs={"amdgpu_families": ["gfx94x", "gfx110x"]})
        self.assertEqual(derive_architectures(rec), ["gfx94x", "gfx110x"])

    def test_split_accepts_both_separators_regardless_of_key(self):
        # `_split_families` splits on `[;,]` for any key, not per-key: the
        # producer uses `,` for amdgpu_families and `;` for dist_amdgpu_families,
        # but either separator (and a mix) must parse from either key. Pins the
        # behavior so a future "split per-key" change is caught.
        semi = _make_record(inputs={"amdgpu_families": "gfx906;gfx908"})
        self.assertEqual(derive_architectures(semi), ["gfx906", "gfx908"])

        mixed = _make_record(inputs={"amdgpu_families": "gfx906;gfx908,gfx90a"})
        self.assertEqual(derive_architectures(mixed), ["gfx906", "gfx908", "gfx90a"])

        comma_in_dist = _make_record(inputs={"dist_amdgpu_families": "gfx900,gfx906"})
        self.assertEqual(derive_architectures(comma_in_dist), ["gfx900", "gfx906"])

    def test_empty_family_list_means_none(self):
        # A present list key wins even when empty ("none"): not rescued by
        # dist_amdgpu_families or the job-name fallback.
        rec = _make_record(
            inputs={"amdgpu_families": [], "dist_amdgpu_families": "gfx906;gfx908"}
        )
        self.assertEqual(derive_architectures(rec), [])

    def test_single_amdgpu_family_wins_when_set(self):
        rec = _make_record(
            inputs={"amdgpu_family": "gfx906", "dist_amdgpu_families": "gfx906;gfx908"}
        )
        self.assertEqual(derive_architectures(rec), ["gfx906"])

    def test_empty_single_family_falls_through_to_dist(self):
        # The artifacts / native / tarballs umbrella runs carry
        # `amdgpu_family: ""` and describe the fan-out only via the
        # semicolon-joined `dist_amdgpu_families`.
        rec = _make_record(
            inputs={
                "amdgpu_family": "",
                "dist_amdgpu_families": "gfx94X-dcgpu;gfx110X-all;gfx1151",
            }
        )
        self.assertEqual(
            derive_architectures(rec), ["gfx94X-dcgpu", "gfx110X-all", "gfx1151"]
        )

    def test_dist_amdgpu_families_without_single_key(self):
        rec = _make_record(inputs={"dist_amdgpu_families": "gfx900;gfx906"})
        self.assertEqual(derive_architectures(rec), ["gfx900", "gfx906"])

    def test_test_amdgpu_family_used_for_jax_multiarch(self):
        # JAX wheels are a single Multiarch build with no `amdgpu_families`
        # fan-out; the run's only family is `test_amdgpu_family`.
        rec = _make_record(inputs={"test_amdgpu_family": "gfx94X-dcgpu"})
        self.assertEqual(derive_architectures(rec), ["gfx94X-dcgpu"])

    def test_explicit_families_take_precedence_over_test_family(self):
        rec = _make_record(
            inputs={
                "amdgpu_families": "gfx906",
                "test_amdgpu_family": "gfx94X-dcgpu",
            }
        )
        self.assertEqual(derive_architectures(rec), ["gfx906"])

    def test_job_name_fallback_when_no_arch_input(self):
        rec = _make_record(inputs={})
        rec.jobs = [_job("Build (gfx94X-dcgpu, linux)"), _job("Build (gfx110X-all)")]
        self.assertEqual(derive_architectures(rec), ["gfx94X-dcgpu", "gfx110X-all"])


class DerivePlatformAndPipelineTest(unittest.TestCase):
    def test_setup_is_global_setup_leaf(self):
        rec = _path_record(".github/workflows/setup_multi_arch.yml")
        self.assertEqual(derive_platform_and_pipeline(rec), ("", "setup", "setup"))

    def test_orchestrators_classify_as_orchestrator_pipeline(self):
        # Orchestrators own no leaf; they classify to pipeline_type=orchestrator
        # with a phase naming the orchestrator and a platform ("" top-level,
        # "linux"/"windows" per-platform).
        cases = {
            "multi_arch_release.yml": ("", "orchestrator", "release"),
            "multi_arch_release_asan.yml": ("", "orchestrator", "release-asan"),
            "multi_arch_release_linux.yml": (
                "linux",
                "orchestrator",
                "release-linux",
            ),
            "multi_arch_release_windows.yml": (
                "windows",
                "orchestrator",
                "release-windows",
            ),
            "multi_arch_repackage.yml": ("", "orchestrator", "repackage"),
            "build_portable_linux_python_packages.yml": (
                "linux",
                "orchestrator",
                "python-packages",
            ),
            "build_windows_python_packages.yml": (
                "windows",
                "orchestrator",
                "python-packages",
            ),
        }
        for wf, expected in cases.items():
            with self.subTest(wf=wf):
                rec = _path_record(f".github/workflows/{wf}")
                self.assertEqual(derive_platform_and_pipeline(rec), expected)

    def test_orchestrator_lookalike_not_in_specs_raises(self):
        # An orchestrator-shaped filename that is NOT in ORCHESTRATOR_SPECS must
        # fall through to the raise, not be fuzzy-matched to a registered one.
        rec = _path_record(".github/workflows/multi_arch_release_macos.yml")
        with self.assertRaises(ValueError):
            derive_platform_and_pipeline(rec)

    def test_native_packages_fan_out_by_input(self):
        path = ".github/workflows/multi_arch_build_native_linux_packages.yml"
        rpm = _path_record(path, inputs={"native_package_type": "rpm"})
        deb = _path_record(path, inputs={"native_package_type": "deb"})
        self.assertEqual(
            derive_platform_and_pipeline(rpm), ("linux", "native_packages", "rpm")
        )
        self.assertEqual(
            derive_platform_and_pipeline(deb), ("linux", "native_packages", "deb")
        )

    def test_unregistered_workflow_raises(self):
        rec = _path_record(".github/workflows/does_not_exist.yml")
        with self.assertRaises(ValueError):
            derive_platform_and_pipeline(rec)

    def test_python_packages_are_per_platform_orchestrators(self):
        # Build sub-steps that package the ROCm wheels self-report (notify_quartz
        # in every workflow, each passing its own `reporting_workflow`). They are
        # captured as per-platform orchestrators -- no leaf, no completion signal
        # -- rather than raising like an unregistered workflow.
        for wf, platform in (
            ("build_portable_linux_python_packages.yml", "linux"),
            ("build_windows_python_packages.yml", "windows"),
        ):
            with self.subTest(wf=wf):
                rec = _path_record(f".github/workflows/{wf}")
                self.assertEqual(
                    derive_platform_and_pipeline(rec),
                    (platform, "orchestrator", "python-packages"),
                )

    def test_pytorch_wheels_platform_comes_from_runner_label(self):
        path = ".github/workflows/test_pytorch_wheels.yml"
        linux = _path_record(
            path,
            inputs={"amdgpu_family": "gfx94X-dcgpu", "test_runs_on": "linux-gfx942"},
        )
        windows = _path_record(
            path,
            inputs={
                "amdgpu_family": "gfx110X-all",
                "test_runs_on": "windows-gfx110X-gpu-rocm",
            },
        )
        self.assertEqual(
            derive_platform_and_pipeline(linux), ("linux", "pytorch", "test")
        )
        self.assertEqual(
            derive_platform_and_pipeline(windows), ("windows", "pytorch", "test")
        )

    def test_delegated_wheel_build_workflows_are_registered(self):
        cases = {
            "multi_arch_build_portable_linux_pytorch_wheels.yml": (
                "linux",
                "pytorch",
                "build",
            ),
            "multi_arch_build_windows_pytorch_wheels.yml": (
                "windows",
                "pytorch",
                "build",
            ),
            "multi_arch_build_linux_jax_wheels.yml": (
                "linux",
                "jax",
                "build",
            ),
        }
        for wf, expected in cases.items():
            with self.subTest(wf=wf):
                rec = _path_record(f".github/workflows/{wf}")
                self.assertEqual(derive_platform_and_pipeline(rec), expected)

    def test_pytorch_wheels_full_is_distinct_test_full_phase(self):
        # The full suite must NOT share the smoke test's `test` leaf: both can
        # run for the same build, so it routes to the sibling `test-full` phase.
        rec = _path_record(".github/workflows/test_pytorch_wheels_full.yml")
        self.assertEqual(
            derive_platform_and_pipeline(rec), ("linux", "pytorch", "test-full")
        )

    def test_test_artifacts_is_rocm_test_leaf_platform_from_runner(self):
        # `test_artifacts.yml` is dispatched per-arch by both release
        # orchestrators; platform comes from the `test_runs_on` runner label.
        path = ".github/workflows/test_artifacts.yml"
        linux = _path_record(
            path,
            inputs={"amdgpu_families": "gfx942", "test_runs_on": "linux-mi300-1gpu"},
        )
        windows = _path_record(
            path,
            inputs={"amdgpu_families": "gfx1100", "test_runs_on": "windows-strix-gpu"},
        )
        self.assertEqual(derive_platform_and_pipeline(linux), ("linux", "rocm", "test"))
        self.assertEqual(
            derive_platform_and_pipeline(windows), ("windows", "rocm", "test")
        )

    def test_test_artifacts_defaults_to_linux_without_windows_runner(self):
        rec = _path_record(
            ".github/workflows/test_artifacts.yml",
            inputs={"amdgpu_families": "gfx942"},
        )
        self.assertEqual(derive_platform_and_pipeline(rec), ("linux", "rocm", "test"))

    def test_test_linux_jax_wheels_is_jax_test_leaf(self):
        # JAX is linux-only; arch comes from `test_amdgpu_family`.
        rec = _path_record(
            ".github/workflows/test_linux_jax_wheels.yml",
            inputs={"test_amdgpu_family": "gfx94X-dcgpu"},
        )
        self.assertEqual(derive_platform_and_pipeline(rec), ("linux", "jax", "test"))
        self.assertEqual(derive_architectures(rec), ["gfx94X-dcgpu"])

    def test_test_rocm_wheels_is_rocm_test_leaf(self):
        # ROCm wheel tests share the rocm/test leaf; per-arch via singular
        # `amdgpu_family`, platform from the `test_runs_on` runner label.
        path = ".github/workflows/test_rocm_wheels.yml"
        linux = _path_record(
            path,
            inputs={"amdgpu_family": "gfx942", "test_runs_on": "linux-gfx942-1gpu"},
        )
        windows = _path_record(
            path,
            inputs={"amdgpu_family": "gfx1100", "test_runs_on": "windows-gfx1100"},
        )
        self.assertEqual(derive_platform_and_pipeline(linux), ("linux", "rocm", "test"))
        self.assertEqual(
            derive_platform_and_pipeline(windows), ("windows", "rocm", "test")
        )
        self.assertEqual(derive_architectures(linux), ["gfx942"])


def _leaf_run() -> WorkflowRunRecord:
    rec = _make_record()
    rec.path = ".github/workflows/multi_arch_build_portable_linux.yml"
    rec.classification.platform = "linux"
    rec.classification.pipeline_type = "rocm"
    rec.classification.pipeline_phase = "build"
    return rec


def _orchestrator_run(
    path: str = ".github/workflows/multi_arch_release.yml",
) -> WorkflowRunRecord:
    # Orchestrators classify to pipeline_type=orchestrator with a phase/platform
    # from ORCHESTRATOR_SPECS; derive_effective_owner_run_id keys off that tuple
    # to treat a top-level (finalizing) orchestrator as its own owner.
    spec = ORCHESTRATOR_SPECS[path.rsplit("/", 1)[-1]]
    rec = _make_record()
    rec.path = path
    rec.classification.platform = spec.platform
    rec.classification.pipeline_type = spec.pipeline_type
    rec.classification.pipeline_phase = spec.pipeline_phase
    return rec


class DeriveEffectiveOwnerRunIdTest(unittest.TestCase):
    def test_top_level_orchestrator_is_self(self):
        run = _orchestrator_run()
        run.workflow_run_id = 29079513704
        self.assertEqual(derive_effective_owner_run_id(run), 29079513704)

    def test_reusable_child_uses_github_parent(self):
        run = _leaf_run()
        run.parent_workflow = {
            "id": 29079513704,
            "name": "multi_arch_release_linux.yml",
        }
        self.assertEqual(derive_effective_owner_run_id(run), 29079513704)

    def test_artifact_run_id_overrides_immediate_parent(self):
        run = _leaf_run()
        run.inputs = {"artifact_run_id": "29079513704"}
        run.parent_workflow = {
            "id": 29080000000,
            "name": "multi_arch_release_linux_pytorch_wheels.yml",
        }
        self.assertEqual(derive_effective_owner_run_id(run), 29079513704)

    def test_falls_back_to_artifact_run_id_input(self):
        run = _leaf_run()
        run.inputs = {"artifact_run_id": "29079513704"}
        self.assertEqual(derive_effective_owner_run_id(run), 29079513704)

    def test_falls_back_to_find_links_url_run_id(self):
        # Upstream benc-uk dispatches that don't carry an `artifact_run_id`
        # input (e.g. the real PyTorch/JAX release triggers) still carry the
        # top-level run id baked into the artifact bucket path.
        run = _leaf_run()
        run.inputs = {
            "rocm_package_find_links_url": (
                "https://therock-nightly-artifacts.s3.amazonaws.com/"
                "29079513704-linux/python/index.html"
            )
        }
        self.assertEqual(derive_effective_owner_run_id(run), 29079513704)

    def test_falls_back_to_package_install_url_run_id(self):
        run = _leaf_run()
        run.inputs = {
            "package_install_url": (
                "https://therock-nightly-artifacts.s3.amazonaws.com/"
                "29079513704-linux/packages/deb"
            )
        }
        self.assertEqual(derive_effective_owner_run_id(run), 29079513704)

    def test_prefers_explicit_artifact_run_id_over_url(self):
        run = _leaf_run()
        run.inputs = {
            "artifact_run_id": "29079513704",
            "rocm_package_find_links_url": (
                "https://therock-nightly-artifacts.s3.amazonaws.com/"
                "11111111111-linux/python/index.html"
            ),
        }
        self.assertEqual(derive_effective_owner_run_id(run), 29079513704)

    def test_ignores_url_without_platform_suffix(self):
        run = _leaf_run()
        run.inputs = {
            "rocm_package_find_links_url": "https://example.invalid/stub/find-links/"
        }
        run.parent_workflow = {
            "id": 29080000000,
            "name": "multi_arch_release_linux_pytorch_wheels.yml",
        }
        self.assertEqual(derive_effective_owner_run_id(run), 29080000000)

    def test_no_signal_falls_back_to_raw_trigger_workflow_run_id(self):
        run = _leaf_run()
        run.trigger_workflow_run_id = 123456
        self.assertEqual(derive_effective_owner_run_id(run), 123456)


class ClassifyOwnerNormalizationOrderingTest(unittest.TestCase):
    """`classify()` must normalize the owner AFTER deriving source_run_id.

    `derive_source_run_id` reads `trigger_workflow_run_id` as its fallback, so it
    must see the *immediate* GitHub parent -- but `classify()` then overwrites
    that field with the resolved top-level owner. If someone reorders the two
    calls, source_run_id would resolve off the already-overwritten owner. This
    pins the ordering the "Normalize last" comment describes.
    """

    def test_source_run_id_sees_immediate_parent_not_resolved_owner(self):
        run = _make_record(
            release_type="nightly",
            # A URL input recovers the top-level owner (999) without an
            # `artifact_run_id`, which would otherwise short-circuit
            # derive_source_run_id and mask the ordering it depends on.
            inputs={
                "rocm_package_find_links_url": (
                    "https://therock-nightly-artifacts.s3.amazonaws.com/"
                    "999-linux/python/index.html"
                )
            },
        )
        run.path = ".github/workflows/multi_arch_build_portable_linux.yml"
        run.trigger_workflow_run_id = 111  # immediate GitHub parent

        classify(run)

        # source_run_id keyed off the immediate parent (111), proving it ran
        # before normalization; trigger_workflow_run_id is the resolved owner.
        self.assertEqual(run.classification.source_run_id, "111")
        self.assertEqual(run.trigger_workflow_run_id, 999)


if __name__ == "__main__":
    unittest.main()
