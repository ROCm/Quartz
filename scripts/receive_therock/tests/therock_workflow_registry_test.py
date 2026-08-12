#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Static sanity checks over the workflow classification registry.

Two families of check:

1. *Spec references a real workflow.* `derive_platform_and_pipeline` raises at
   runtime when it meets an unregistered workflow, so that direction is already
   covered. The reverse mistake is not: a spec keyed on a workflow that does not
   exist in the rock repos (a typo, a renamed-upstream file) sits silently and
   never matches anything. `WorkflowRegistryCoverageTest` catches it.

2. *notify_quartz wiring is complete.* Every registered workflow must call the
   reusable `notify_quartz.yml` exactly twice -- once `started`, once
   `completed` -- and each call must pass `reporting_workflow` equal to its own
   filename, or the receiver cannot classify that run's events.
   `NotifyQuartzWiringTest` checks this identically for the local repo (live from
   `.github/workflows`) and for each upstream repo (from the hermetic snapshot).
"""

import json
import os
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, os.fspath(Path(__file__).parent.parent))

from therock_types import ORCHESTRATOR_SPECS, WORKFLOW_SPECS

_INVENTORY_PATH = Path(__file__).with_name("fixtures") / "rock_workflow_inventory.json"

_ALLOWED_PIPELINE_TYPES = frozenset(
    {"rocm", "pytorch", "jax", "native_packages", "setup", "orchestrator"}
)

_NOTIFY_QUARTZ = "notify_quartz.yml"

# Workflows (across all repos) that deliberately do NOT report to Quartz:
# security scanners, CI-only dispatchers, image publishers, and internal
# plumbing. Every workflow in every repo must be exactly one of
# {registered, notify_quartz.yml, excluded} -- see
# `test_workflows_are_partitioned`. Adding a workflow forces a decision:
# register it in WORKFLOW_SPECS / ORCHESTRATOR_SPECS, or list it here.
_EXCLUDED_WORKFLOWS = frozenset(
    {
        # Security scanners (upstream).
        "codeql.yml",
        "gitleaks.yml",
        "gitleaks_main.yml",
        "pre-commit.yml",
        "pre_commit_security.yml",
        # Internal plumbing / automation (upstream).
        "bump_submodules.yml",
        "copy_release.yml",
        "hip_tagging_automation.yml",
        "manifest-diff.yml",
        "receive_therock_data.yml",
        "therock-pr-bot.yml",
        "unit_tests.yml",
        # CI-only orchestrators / dispatchers (not the reporting workflows).
        "multi_arch_build_linux_jax_wheels_ci.yml",
        "multi_arch_build_portable_linux_pytorch_wheels_ci.yml",
        "multi_arch_build_windows_pytorch_wheels_ci.yml",
        "multi_arch_ci.yml",
        "multi_arch_ci_asan.yml",
        "multi_arch_ci_linux.yml",
        "multi_arch_ci_windows.yml",
        # Image publishers.
        "publish_build_manylinux_x86_64.yml",
        "publish_dockerfile.yml",
        "publish_no_rocm_image_ubi10.yml",
        "publish_no_rocm_image_ubuntu24_04.yml",
        "publish_no_rocm_image_ubuntu24_04_ocl_rt.yml",
        "publish_no_rocm_image_ubuntu24_04_rocgdb.yml",
        # Upstream test scaffolding (not Quartz-reported).
        "test_artifacts_structure.yml",
        # Registered separately in users/cgoea/fix_test_artifacts; excluded
        # here so this branch stays green standalone.
        "test_component.yml",
        "test_jax_dockerfile.yml",
        # Quartz's own local plumbing (not a rock producer workflow).
        "sync_develop_to_main.yml",
        "pre_commit.yml",
    }
)


def _load_doc() -> dict:
    return json.loads(_INVENTORY_PATH.read_text())


def _load_inventory() -> dict[str, list[str]]:
    return _load_doc()["repos"]


def _load_notify_quartz_calls() -> dict[str, dict[str, list[dict[str, str]]]]:
    """Snapshotted `{repo: {workflow: [notify_quartz `with:` params]}}`."""
    return _load_doc().get("notify_quartz_calls", {})


def _rock_workflows() -> set[str]:
    """Union of workflow filenames across all snapshotted rock repos."""
    return {name for names in _load_inventory().values() for name in names}


def _registered_keys() -> set[str]:
    return set(WORKFLOW_SPECS) | set(ORCHESTRATOR_SPECS)


def _local_workflow_texts() -> dict[str, str]:
    """`{filename: text}` for this repo's `.github/workflows/*.yml`."""
    workflow_dir = Path(__file__).resolve().parents[3] / ".github" / "workflows"
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in workflow_dir.glob("*.yml")
    }


def _notify_quartz_calls_from_text(text: str) -> list[dict[str, str]]:
    """`with:` params of every job that calls the reusable notify_quartz.yml.

    Same extraction the refresh script snapshots upstream, so local and upstream
    feed `_notify_quartz_offenders` in an identical shape.
    """
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return []
    calls: list[dict[str, str]] = []
    for job in doc.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses", "")
        if isinstance(uses, str) and uses.endswith(_NOTIFY_QUARTZ):
            with_params = job.get("with", {})
            if isinstance(with_params, dict):
                calls.append({str(k): str(v) for k, v in with_params.items()})
    return calls


def _local_notify_quartz_calls() -> dict[str, list[dict[str, str]]]:
    """Live `{workflow: [notify_quartz `with:` params]}` for this repo."""
    calls: dict[str, list[dict[str, str]]] = {}
    for name, text in _local_workflow_texts().items():
        found = _notify_quartz_calls_from_text(text)
        if found:
            calls[name] = found
    return calls


def _notify_quartz_offenders(
    label: str,
    inventory: list[str],
    calls_by_workflow: dict[str, list[dict[str, str]]],
) -> list[str]:
    """Registered workflows in `inventory` not correctly wired to notify_quartz.

    "Registered" = present in WORKFLOW_SPECS / ORCHESTRATOR_SPECS. Everything
    else (gitleaks, codeql, pre-commit, ...) is deliberately excluded: those do
    not report to Quartz. Each registered workflow must call notify_quartz
    exactly twice (started + completed) with `reporting_workflow` == its own
    filename.
    """
    registered = _registered_keys()
    problems: list[str] = []
    for filename in sorted(set(inventory) & registered):
        calls = calls_by_workflow.get(filename, [])
        phases = sorted(c.get("run_phase", "") for c in calls)
        if phases != ["completed", "started"]:
            problems.append(
                f"{label}/{filename}: expected notify_quartz phases "
                f"['started', 'completed'], got {phases}"
            )
        for call in calls:
            reported = call.get("reporting_workflow", "")
            if reported != filename:
                problems.append(
                    f"{label}/{filename}: reporting_workflow="
                    f"{reported!r} (expected {filename!r})"
                )
    return problems


class RockInventoryFixtureTest(unittest.TestCase):
    def test_inventory_fixture_is_present_and_populated(self):
        self.assertTrue(
            _INVENTORY_PATH.is_file(),
            f"missing rock inventory snapshot at {_INVENTORY_PATH}; regenerate "
            "with fixtures/refresh_rock_workflow_inventory.py",
        )
        inv = _load_inventory()
        self.assertTrue(inv, "inventory has no repos")
        for repo, names in inv.items():
            self.assertTrue(names, f"{repo}: empty workflow list in snapshot")


class WorkflowRegistryCoverageTest(unittest.TestCase):
    def test_registered_specs_reference_a_real_rock_workflow(self):
        """Every spec key must be a workflow that exists in a rock repo."""

        dangling = sorted(_registered_keys() - _rock_workflows())
        self.assertEqual(
            dangling,
            [],
            "Registry key(s) do not exist in any rock repo "
            f"(ROCm/TheRock, ROCm/rockrel): {dangling}. Fix the key, or refresh "
            "the inventory snapshot if the workflow was recently added upstream.",
        )

    def test_no_overlap_between_workflow_and_orchestrator_specs(self):
        overlap = sorted(set(WORKFLOW_SPECS) & set(ORCHESTRATOR_SPECS))
        self.assertEqual(
            overlap,
            [],
            f"Workflow(s) registered as both leaf and orchestrator: {overlap}. "
            "A file must be exactly one of the two.",
        )

    def test_workflows_are_partitioned(self):
        """Every workflow in every repo is registered, excluded, or notify_quartz.

        Forces a decision on each new workflow: report it to Quartz (register in
        WORKFLOW_SPECS / ORCHESTRATOR_SPECS) or opt out (add to
        `_EXCLUDED_WORKFLOWS`). A workflow in neither bucket is a silent gap --
        it would never be classified and never fail a test. Local is read live
        from `.github/workflows`; upstream from the hermetic snapshot.
        """
        workflows_by_repo = {"local": set(_local_workflow_texts())}
        for repo, names in _load_inventory().items():
            workflows_by_repo[repo] = set(names)

        accounted = _registered_keys() | _EXCLUDED_WORKFLOWS | {_NOTIFY_QUARTZ}
        unaccounted = sorted(
            f"{repo}/{name}"
            for repo, names in workflows_by_repo.items()
            for name in names - accounted
        )
        self.assertEqual(
            unaccounted,
            [],
            f"Workflow(s) in no bucket: {unaccounted}. Register each in "
            "WORKFLOW_SPECS / ORCHESTRATOR_SPECS, or add to "
            "_EXCLUDED_WORKFLOWS if it should not report to Quartz.",
        )

        all_workflows = set().union(*workflows_by_repo.values())
        stale_exclusions = sorted(_EXCLUDED_WORKFLOWS - all_workflows)
        self.assertEqual(
            stale_exclusions,
            [],
            f"_EXCLUDED_WORKFLOWS names workflow(s) in no repo: "
            f"{stale_exclusions}. Remove them.",
        )
        both = sorted(_EXCLUDED_WORKFLOWS & _registered_keys())
        self.assertEqual(
            both,
            [],
            f"Workflow(s) both registered and excluded: {both}. A file reports "
            "to Quartz or it does not -- pick one bucket.",
        )


class NotifyQuartzWiringTest(unittest.TestCase):
    """Every registered workflow must wire notify_quartz correctly.

    The local repo is parsed live from `.github/workflows`; upstream repos
    (ROCm/TheRock, ROCm/rockrel) come from the hermetic snapshot -- refresh it
    via `fixtures/refresh_rock_workflow_inventory.py`. Both feed the same
    `_notify_quartz_offenders` check. The upstream cases are expected RED until
    the notify_quartz rollout lands in those repos.
    """

    def _assert_no_offenders(self, offenders: list[str], label: str) -> None:
        self.assertFalse(
            offenders,
            f"{label}: registered workflow(s) not correctly wired to "
            "notify_quartz:\n  " + "\n  ".join(offenders),
        )

    def test_local_registered_workflows_notify_quartz(self):
        offenders = _notify_quartz_offenders(
            "local",
            sorted(_local_workflow_texts()),
            _local_notify_quartz_calls(),
        )
        self._assert_no_offenders(offenders, "local")

    def test_the_rock_registered_workflows_notify_quartz(self):
        repo = "ROCm/TheRock"
        offenders = _notify_quartz_offenders(
            repo,
            _load_inventory().get(repo, []),
            _load_notify_quartz_calls().get(repo, {}),
        )
        self._assert_no_offenders(offenders, repo)

    def test_rockrel_registered_workflows_notify_quartz(self):
        repo = "ROCm/rockrel"
        offenders = _notify_quartz_offenders(
            repo,
            _load_inventory().get(repo, []),
            _load_notify_quartz_calls().get(repo, {}),
        )
        self._assert_no_offenders(offenders, repo)

    def test_excluded_workflows_do_not_notify_quartz(self):
        """Excluded workflows must NOT call notify_quartz, in any repo.

        The inverse of the coverage above: a workflow opted out of Quartz that
        still emits notify_quartz would send events the receiver cannot classify
        (it is in no spec). Checked across local (live) and upstream (snapshot).
        """
        calls_by_repo = {"local": _local_notify_quartz_calls()}
        calls_by_repo.update(_load_notify_quartz_calls())
        offenders = sorted(
            f"{repo}/{name}"
            for repo, calls in calls_by_repo.items()
            for name in set(calls) & _EXCLUDED_WORKFLOWS
        )
        self.assertEqual(
            offenders,
            [],
            f"Excluded workflow(s) call notify_quartz: {offenders}. Either "
            "register them in WORKFLOW_SPECS / ORCHESTRATOR_SPECS, or drop the "
            "notify_quartz call.",
        )


class WorkflowSpecShapeTest(unittest.TestCase):
    """Each registered spec must carry a well-formed classification tuple."""

    def test_leaf_specs_have_valid_pipeline_fields(self):
        for wf_file, specs in WORKFLOW_SPECS.items():
            self.assertTrue(specs, f"{wf_file}: empty spec list")
            for spec in specs:
                self.assertIn(
                    spec.pipeline_type,
                    _ALLOWED_PIPELINE_TYPES,
                    f"{wf_file}: unknown pipeline_type {spec.pipeline_type!r}",
                )
                self.assertTrue(
                    spec.pipeline_phase,
                    f"{wf_file}: empty pipeline_phase",
                )
                # Platform is either statically set or resolved from the runner
                # label at classify time; a static platform must be a known one.
                if not spec.platform_from_test_runs_on:
                    self.assertIn(
                        spec.platform,
                        ("", "linux", "windows"),
                        f"{wf_file}: unexpected platform {spec.platform!r}",
                    )

    def test_orchestrator_specs_are_orchestrators(self):
        for wf_file, spec in ORCHESTRATOR_SPECS.items():
            self.assertEqual(
                spec.pipeline_type,
                "orchestrator",
                f"{wf_file}: orchestrator spec must have "
                f"pipeline_type='orchestrator', got {spec.pipeline_type!r}",
            )
            self.assertTrue(
                spec.pipeline_phase,
                f"{wf_file}: empty orchestrator pipeline_phase",
            )


if __name__ == "__main__":
    unittest.main()
