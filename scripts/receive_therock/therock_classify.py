# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Derived-field classifiers for TheRock workflow runs.

Resolution tiers per field:
  - Tier 1 (all fields): explicit `inputs` / `env` / `release_type` values,
    or the workflow `path` looked up in `WORKFLOW_SPECS` / `ORCHESTRATOR_SPECS`.
    Everything defaults to `""` (or `[]` / `None`) when its tier-1 source is
    absent.
  - Tier 2 (architectures only): the one field with a fallback heuristic --
    when no arch input is present, families are scraped from job names
    (`derive_architectures`). No other field has a tier-2.

`release_type` in particular is never inferred from the version string: a bare
`7.13.0` may ship to the dev bucket, so the version cannot tell you the type
(see `derive_release_type`).
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final

from therock_types import (
    ORCHESTRATOR_SPECS,
    RELEASE_VERSION_NIGHTLY_RE,
    WORKFLOW_SPECS,
    WorkflowRunRecord,
    parse_quartz_tracking_id,
)

# Per-platform orchestrator phases that publish a release to the CDN (and thus
# expose CDN URLs); other per-platform orchestrators (e.g. python-packages) do
# not.
RELEASE_CDN_PHASES: Final = frozenset({"release-linux", "release-windows"})

log = logging.getLogger(__name__)


def classify(wr: WorkflowRunRecord | None) -> None:
    if wr is None:
        return
    c = wr.classification
    c.platform, c.pipeline_type, c.pipeline_phase = derive_platform_and_pipeline(wr)
    c.architectures = derive_architectures(wr)
    c.release_version = derive_release_version(wr)
    wr.release_type = derive_release_type(wr)
    c.test_type = derive_test_type(wr)
    c.build_variant = derive_build_variant(wr)
    c.source_run_id = derive_source_run_id(wr)
    wr.tarball_url = derive_tarball_url(wr)
    wr.wheels_url = derive_wheels_url(wr)
    wr.artifacts_url = derive_artifacts_url(wr)
    wr.rpm_urls = derive_rpm_urls(wr)
    wr.deb_urls = derive_deb_urls(wr)
    cdn = derive_release_cdn_urls(wr)
    if cdn is not None:
        wr.tarball_url = cdn.tarball_url
        wr.wheels_url = cdn.wheels_url
        wr.rpm_urls = cdn.rpm_urls
        wr.deb_urls = cdn.deb_urls
    # Normalize last: `derive_source_run_id` above must still see the
    # *immediate* GitHub parent (its own tier-2 fallback), so the effective
    # (top-level) owner is resolved only after every other field is derived.
    # Every consumer downstream of classify() -- status.json, and eventually
    # the database -- then sees the same resolved owner instead of
    # recomputing it in its own place.
    wr.trigger_workflow_run_id = derive_effective_owner_run_id(wr)


def derive_platform_and_pipeline(
    wr: WorkflowRunRecord,
) -> tuple[str, str, str]:
    if not wr.path:
        raise ValueError(
            f"derive_platform_and_pipeline: wr.path is empty (workflow_run_id={wr.workflow_run_id!r}); "
            "every workflow run dispatched to Quartz must carry the workflow file path."
        )
    wf_file = PurePosixPath(wr.path).name
    orchestrator = ORCHESTRATOR_SPECS.get(wf_file)
    if orchestrator is not None:
        return (
            orchestrator.platform,
            orchestrator.pipeline_type,
            orchestrator.pipeline_phase,
        )
    candidates = WORKFLOW_SPECS.get(wf_file)
    if not candidates:
        raise ValueError(
            f"derive_platform_and_pipeline: workflow {wf_file!r} is not registered in "
            f"WORKFLOW_SPECS and is not an orchestrator (workflow_run_id={wr.workflow_run_id!r}). "
            "Every captured TheRock workflow needs a WorkflowSpec -- add one, or list the "
            "file in ORCHESTRATOR_SPECS if it only fans out to other workflows."
        )
    for spec in candidates:
        if spec.match_when and not all(
            wr.inputs.get(k) == v for k, v in spec.match_when.items()
        ):
            continue
        platform = (
            _platform_from_test_runs_on(wr)
            if spec.platform_from_test_runs_on
            else spec.platform
        )
        return (platform, spec.pipeline_type, spec.pipeline_phase)
    raise ValueError(
        f"derive_platform_and_pipeline: {wf_file!r} has {len(candidates)} candidate(s) "
        f"in WORKFLOW_SPECS but none matched inputs (workflow_run_id={wr.workflow_run_id!r}). "
        "Check the producer's match_when inputs (e.g. `native_package_type`) or the "
        "WORKFLOW_SPECS entries."
    )


def _platform_from_test_runs_on(wr: WorkflowRunRecord) -> str:
    """Resolve platform from the `test_runs_on` runner label.

    `test_artifacts.yml` is dispatched by both the linux and windows release
    orchestrators with no static platform input; the only signal is the runner
    label passed as `test_runs_on`.
    """
    runs_on = str(wr.inputs.get("test_runs_on") or "").lower()
    # Linux is the deliberate default when no runner label is present (or it
    # names no windows runner): test_artifacts runs on linux unless explicitly
    # dispatched onto a windows runner. This is intentional, not a missing
    # windows case -- see test_test_artifacts_defaults_to_linux_without_windows_runner.
    return "windows" if "windows" in runs_on else "linux"


# Shape of one GPU family token, e.g. "gfx942" or "gfx94X-dcgpu". Exposed
# (unlike the compiled regexes below) so other modules that need the same
# token shape in a different context -- e.g. therock_update_status_json's
# `_TEST_ARCH_JOB_RE`, which anchors it to a job's own "Test | <arch>"
# segment -- stay in sync with this one instead of drifting independently.
GPU_FAMILY_TOKEN: Final = r"gfx[0-9A-Za-z]+(?:-[0-9A-Za-z]+)?"
_GPU_FAMILY_RE: Final = re.compile(rf"\b({GPU_FAMILY_TOKEN})\b")


def _split_families(val: object) -> list[str]:
    """Normalize an arch-family input value to a clean list.

    Accepts a list or a string joined by commas and/or semicolons -- the
    producer workflows use `,` for `amdgpu_families` (comma-joined) and `;`
    for `dist_amdgpu_families` (semicolon-joined).
    """
    if isinstance(val, list):
        return [str(item).strip() for item in val if str(item).strip()]
    if isinstance(val, str):
        return [tok.strip() for tok in re.split(r"[;,]", val) if tok.strip()]
    return []


def derive_architectures(wr: WorkflowRunRecord) -> list[str]:
    # List-valued family keys: a present key wins even if empty
    # (`amdgpu_families: []` = "none"), so a build that explicitly targets no
    # families is not silently "rescued" by a later fallback.
    for key in ("amdgpu_families", "families"):
        if key in wr.inputs:
            return _split_families(wr.inputs.get(key))

    # Single-arch key (per-stage runs, e.g. math-libs `gfx906`): authoritative
    # only when non-empty. The artifacts / native-packages / tarballs umbrella
    # runs carry `amdgpu_family: ""` and describe their fan-out solely via
    # `dist_amdgpu_families`, so an empty value must fall through rather than
    # short-circuit to "no architectures".
    single = str(wr.inputs.get("amdgpu_family") or "").strip()
    if single:
        return _split_families(single)

    # JAX wheels are a single Multiarch build (no `amdgpu_families` fan-out);
    # the only family they name is `test_amdgpu_family`, the GPU the wheel is
    # validated on. Treat it as the run's architecture when present.
    test_family = str(wr.inputs.get("test_amdgpu_family") or "").strip()
    if test_family:
        return _split_families(test_family)

    # Full fan-out list, semicolon-joined (TheRock producer convention),
    # carried by the artifacts / native-packages / tarballs runs.
    dist = _split_families(wr.inputs.get("dist_amdgpu_families"))
    if dist:
        return dist

    # Last resort: extract families from job names. Runs only when no arch
    # input is present, so it cannot invent families from coincidental `gfx`
    # substrings in job names.
    jobs = wr.api_jobs if wr.api_jobs is not None else wr.jobs
    return list(
        dict.fromkeys(
            match for job in jobs for match in _GPU_FAMILY_RE.findall(job.name)
        )
    )


# ROCm version embedded as a local-version segment of a composite framework
# version, e.g. torch `2.13.0a0+rocm7.14.0a20260531` -> `7.14.0a20260531`.
_ROCM_VERSION_RE: Final = re.compile(r"rocm(\d[\w.+-]*)")


def _rocm_version_segment(v: str) -> str:
    """Reduce a composite framework version to its ROCm part.

    Framework wheels (torch/jax) carry the ROCm version as a local-version
    segment after +rocm. The release channel is determined by that ROCm
    part, not the framework version. Plain ROCm versions pass through.
    """
    if "+" in v:
        m = _ROCM_VERSION_RE.search(v)
        if m:
            return m.group(1)
    return v


# Native deb/rpm versions use a `~<suffix>` separator (see
# docs/packaging/versioning.md); the wheel (PEP 440) form is the canonical
# `release_version`, so native suffixes are normalized back to it.
#   nightly   `7.13.0~20260508`                 -> `7.13.0a20260508`
#   prerel.   `7.13.0~rc0` / `7.13.0~pre0`      -> `7.13.0rc0`
#   rpm dev   `7.13.0~20260508gdb2fd412`        -> `7.13.0.dev0+<sha>`
#   deb dev   `7.13.0~dev20260508-25535422208`  -> `7.13.0.dev0+<sha>`
# The dev local segment prefers the full git sha from `inputs.ref`; when that
# is absent it falls back to the identifier carried in the suffix itself.


def _normalize_native_version(v: str, wr: WorkflowRunRecord) -> str:
    base, _, suffix = v.partition("~")
    if suffix.startswith(("rc", "pre")):
        return f"{base}rc{suffix.removeprefix('rc').removeprefix('pre')}"
    if suffix.startswith("dev"):  # deb dev: dev<date>-<runid>
        return f"{base}.dev0+{_dev_local(wr, suffix.removeprefix('dev').replace('-', '.'))}"
    if "g" in suffix:  # rpm dev: <date>g<short-sha>
        return f"{base}.dev0+{_dev_local(wr, suffix.split('g', 1)[1])}"
    return f"{base}a{suffix}"  # nightly: <date>


_GIT_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")


def _dev_local(wr: WorkflowRunRecord, fallback: str) -> str:
    ref = str(wr.inputs.get("ref") or "").strip()
    return ref if _GIT_SHA_RE.fullmatch(ref) else fallback


def derive_release_version(wr: WorkflowRunRecord) -> str | None:
    raw = (wr.rocm_version or "").strip()
    if not raw:
        return None
    v = _rocm_version_segment(raw)
    return _normalize_native_version(v, wr) if "~" in v else v


def derive_release_type(wr: WorkflowRunRecord) -> str:
    """Return the declared `release_type`.

    Independent of the version string: a bare `7.13.0` may be published to
    the dev bucket, so the version cannot be used to infer the type.
    """
    return wr.release_type or ""


def derive_test_type(wr: WorkflowRunRecord) -> str:
    return str(wr.inputs.get("test_type") or "").strip()


def derive_build_variant(wr: WorkflowRunRecord) -> str:
    return str(wr.inputs.get("build_variant") or "").strip()


_ARTIFACTS_BUCKET_BY_RELEASE_TYPE: Final[dict[str, str]] = {
    "dev": "therock-dev-artifacts",
    "nightly": "therock-nightly-artifacts",
    "prerelease": "therock-prerelease-artifacts",
}


def derive_source_run_id(wr: WorkflowRunRecord) -> str | None:
    """Resolve the run id whose artifacts/outputs this record points to.

    Called once by `classify()` to populate `classification.source_run_id`;
    URL derivers read that field rather than recomputing this.
    """
    run_id = (
        wr.inputs.get("artifact_run_id")
        or wr.trigger_workflow_run_id
        or wr.workflow_run_id
    )
    return str(run_id) if run_id else None


# Top-level orchestrator phases that own the release themselves. The
# per-platform orchestrators (release-linux/release-windows) fan out beneath
# these and never own the release outright; other top-level orchestrators
# (e.g. python-packages) do not finalize a release either. Consumers such as
# therock_update_status_json key off this same tuple to decide which
# orchestrator run stamps the status.json document's completion signal.
# `release-asan` is intentionally absent: asan runs must never own or finalize
# the normal release document (they will get their own status.json file later).
FINALIZING_PHASES: Final = frozenset({"release"})


def is_top_level_orchestrator(wr: WorkflowRunRecord) -> bool:
    """A finalizing top-level orchestrator run (owns its release, no platform)."""
    c = wr.classification
    return (
        c.pipeline_type == "orchestrator"
        and c.platform == ""
        and c.pipeline_phase in FINALIZING_PHASES
    )


def derive_effective_owner_run_id(wr: WorkflowRunRecord) -> int | None:
    """Derive the top-level run that owns this run's release lineage.

    Called once by `classify()` to normalize `trigger_workflow_run_id` in
    place, so every downstream consumer (status.json today, the database
    later) sees the same resolved owner rather than each recomputing it.

    The top-level `multi_arch_release.yml` orchestrator is the root of ownership
    and therefore owns itself (it generates the id but does not carry it on its
    own inputs). Every descendant run carries the owner directly in the
    propagated `quartz_tracking_id`, so it is read from there rather than
    reconstructed from artifact ids, URLs, or the immediate GitHub parent.
    Returns None for runs outside a tracked release (CI, tracking disabled).
    """
    if is_top_level_orchestrator(wr):
        return wr.workflow_run_id
    run_id, _ = parse_quartz_tracking_id(wr.inputs)
    return run_id


def _run_output_base(wr: WorkflowRunRecord) -> str | None:
    bucket = _ARTIFACTS_BUCKET_BY_RELEASE_TYPE.get(wr.release_type or "")
    if not bucket:
        log.debug(
            "no artifacts bucket mapped for release_type=%r (workflow_run_id=%r); "
            "skipping artifact URLs.",
            wr.release_type,
            wr.workflow_run_id,
        )
        return None
    platform = wr.classification.platform
    if platform not in ("linux", "windows"):
        return None
    run_id = wr.classification.source_run_id
    if not run_id:
        return None
    return f"https://{bucket}.s3.amazonaws.com/{run_id}-{platform}"


def _summary_link_url(wr: WorkflowRunRecord, label: str) -> str | None:
    """Return the URL of a `[label](url)` markdown link from a job's step
    summary.
    """
    pattern = re.compile(
        rf"\[{re.escape(label)}\]\((https?://[^)\s]+)\)", re.IGNORECASE
    )
    jobs = wr.api_jobs if wr.api_jobs is not None else wr.jobs
    for job in jobs:
        m = pattern.search(job.summary or "")
        if m:
            return m.group(1)
    return None


def _artifact_base(wr: WorkflowRunRecord) -> str | None:
    """Base URL for the run's build artifacts (no trailing slash)."""
    url = _summary_link_url(wr, "Artifacts")
    if url:
        return (
            url[: -len("/index.html")]
            if url.endswith("/index.html")
            else url.rstrip("/")
        )
    base = _run_output_base(wr)
    if base is None:
        log.debug(
            "no artifact base: no `[Artifacts]` summary link and incomplete "
            "reconstruction inputs (workflow_run_id=%r).",
            wr.workflow_run_id,
        )
    return base


def derive_tarball_url(wr: WorkflowRunRecord) -> str | None:
    """Directory URL holding the run's dist tarballs (no trailing file name)."""
    c = wr.classification
    if c.pipeline_type != "rocm" or c.pipeline_phase != "build":
        return None
    base = _artifact_base(wr)
    return f"{base}/tarballs/" if base else None


_WHEELS_FIND_LINKS_OUTPUT_KEY: Final = "package_find_links_url"


def _captured_output_value(wr: WorkflowRunRecord, key: str) -> str | None:
    """First non-empty `<job>.outputs[key]` across captured job outputs.

    Job names vary by workflow, so match on the output key, not the job name.
    """
    for need_data in (wr.captured_outputs or {}).values():
        if not isinstance(need_data, dict):
            continue
        outs = need_data.get("outputs")
        if isinstance(outs, dict):
            val = outs.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def derive_wheels_url(wr: WorkflowRunRecord) -> str | None:
    c = wr.classification
    if c.pipeline_type != "rocm" or c.pipeline_phase != "build":
        return None
    url = _captured_output_value(wr, _WHEELS_FIND_LINKS_OUTPUT_KEY)
    if url:
        return url
    base = _artifact_base(wr)
    return f"{base}/python/" if base else None


def derive_artifacts_url(wr: WorkflowRunRecord) -> str | None:
    c = wr.classification
    if c.pipeline_type != "rocm" or c.pipeline_phase != "build":
        return None
    base = _artifact_base(wr)
    if not base:
        return None
    return f"{base}/index.html"


def derive_rpm_urls(wr: WorkflowRunRecord) -> dict[str, str]:
    return _native_package_urls(wr, pipeline_phase="rpm")


def derive_deb_urls(wr: WorkflowRunRecord) -> dict[str, str]:
    return _native_package_urls(wr, pipeline_phase="deb")


def _native_package_urls(
    wr: WorkflowRunRecord,
    *,
    pipeline_phase: str,
) -> dict[str, str]:
    c = wr.classification
    if c.pipeline_type != "native_packages" or c.pipeline_phase != pipeline_phase:
        return {}
    # Prefer the authoritative public repo URL emitted by the native build's
    # upload step (`upload_package_repo.py` -> job output
    # `package_repository_url`), when the build's notify_quartz forwards it.
    url = _captured_output_value(wr, "package_repository_url")
    if url:
        return {pipeline_phase: url}
    base = _run_output_base(wr)
    if not base:
        return {}
    repo = f"{base}/packages/{pipeline_phase}"
    if pipeline_phase == "rpm":
        repo = f"{repo}/x86_64"
    # Keep the trailing slash to match the authoritative captured-output form: a
    # PEP 503 simple index is a directory URL and clients fail to resolve it
    # without the trailing `/`.
    return {pipeline_phase: f"{repo}/"}


# CDN base per release stream, following the repo.amd.com layout from RFC0012:
# each stream is served at its own `<stream>.repo.amd.com/rocm/` subdomain.
# `dev` is intentionally absent: normal dev builds stay in the S3 artifact
# bucket, and release-triggered devreleases are out of scope for now.
_RELEASE_CDN_BASE: Final[dict[str, str]] = {
    "nightly": "https://nightly.repo.amd.com/rocm/",
    "prerelease": "https://rc.repo.amd.com/rocm/",
}


@dataclass
class ReleaseCdnUrls:
    """CDN release URLs replacing the per-run S3 URLs once a release is
    published. Directory/index URLs, not per-file links.
    """

    tarball_url: str | None = None
    wheels_url: str | None = None
    rpm_urls: dict[str, str] = field(default_factory=dict)
    deb_urls: dict[str, str] = field(default_factory=dict)


def _publish_succeeded(wr: WorkflowRunRecord) -> bool:
    # job name that publishes artifacts to the release buckets
    need = (wr.captured_outputs or {}).get("publish_to_release_buckets")
    return isinstance(need, dict) and need.get("result") == "success"


def derive_release_cdn_urls(wr: WorkflowRunRecord) -> ReleaseCdnUrls | None:
    """CDN URLs for a completed per-platform release orchestrator run.

    Returns None (leave the per-run S3 URLs untouched) unless all hold:
      - the workflow is a per-platform release orchestrator,
      - `release_type` is nightly or prerelease,
      - the `Publish to Release Buckets` job succeeded.

    nightly URLs carry a `<date>-<run_id>` segment for the native packages;
    prerelease packages split by OS downstream, so only the channel base
    `core/packages/` is exposed.
    """
    wf_file = PurePosixPath(wr.path).name if wr.path else ""
    # Only the per-platform release orchestrators publish CDN release URLs;
    # leaves, the top-level ("" platform) orchestrators, and other per-platform
    # orchestrators (e.g. python-packages) do not.
    orchestrator = ORCHESTRATOR_SPECS.get(wf_file)
    if orchestrator is None or orchestrator.pipeline_phase not in RELEASE_CDN_PHASES:
        return None
    platform = orchestrator.platform
    base = _RELEASE_CDN_BASE.get(wr.release_type or "")
    if base is None:
        return None
    if not _publish_succeeded(wr):
        return None

    urls = ReleaseCdnUrls(
        tarball_url=f"{base}core/tarball/",
        wheels_url=f"{base}whl-next/",
    )
    if platform != "linux":
        return urls

    # native deb/rpm are linux-only
    packages = f"{base}core/packages/"
    if wr.release_type == "nightly":
        segment = _nightly_package_segment(wr)
        if segment:
            # Packages are served under an `<os-profile>` directory (e.g.
            # ubuntu-2404, el9), a placeholder resolved by the consumer; the
            # index no longer splits by package format, so rpm and deb share it.
            os_profile_url = f"{packages}<os-profile>/{segment}/"
            urls.rpm_urls = {"rpm": os_profile_url}
            urls.deb_urls = {"deb": os_profile_url}
            return urls
    urls.rpm_urls = {"rpm": packages}
    urls.deb_urls = {"deb": packages}
    return urls


def _nightly_package_segment(wr: WorkflowRunRecord) -> str | None:
    """`<date>-<run_id>` path segment for nightly native linux packages.

    Date comes from the nightly release_version (`X.Y.ZaYYYYMMDD`); the run
    id is the orchestrator run that produced the artifacts.
    """
    run_id = wr.classification.source_run_id
    m = RELEASE_VERSION_NIGHTLY_RE.match(wr.classification.release_version or "")
    if not run_id or not m:
        return None
    return f"{m.group(1)}-{run_id}"
