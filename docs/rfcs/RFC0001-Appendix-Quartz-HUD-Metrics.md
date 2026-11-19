# TheRock HUD — Quartz Metric-to-Schema Traceability

Traces every TheRock HUD dashboard metric back to the Quartz ClickHouse schema (`therock_workflow_runs`, `therock_workflow_jobs`). Gaps are flagged where the schema doesn't cover a required field.

---

## 1. Metrics by Tab

### CI HUD (commit-centric CI health on `main`)

| Metric | ClickHouse Column | Table | Status |
|---|---|---|---|
| Commit SHA | `commit_sha` | `therock_workflow_runs` | ✅ |
| Commit message | — | — | **GAP** |
| Commit author | `triggered_by` | `therock_workflow_runs` | **Partial** — `triggered_by = "schedule"` for scheduled builds, not the commit author |
| Author avatar | — | — | **GAP** |
| Branch | `branch` | `therock_workflow_runs` | ✅ |
| PR number + title | `pr_number`, `pr_title` | `therock_workflow_runs` | ✅ (`pr_title` requires separate API call — not in webhook payload) |
| Run status / conclusion | `status`, `result` | `therock_workflow_runs` | ✅ |
| Run URL | `html_url` (ALIAS) | `therock_workflow_runs` | ✅ |
| Job name (raw) | `job_name` | `therock_workflow_jobs` | ✅ — HUD parses via `classifyCIJob()` at query time |
| Job status / conclusion | `status`, `result` | `therock_workflow_jobs` | ✅ |
| Job URL | `html_url` (ALIAS) | `therock_workflow_jobs` | ✅ |
| Architecture | `architecture` | `therock_workflow_jobs` | ✅ |
| Test name | `test_name` | `therock_workflow_jobs` | ✅ |
| Shard aggregation | `test_shard`, `test_total_shards` | `therock_workflow_jobs` | ✅ |
| Job timing | `started_at`, `completed_at` | `therock_workflow_jobs` | ✅ |

### Release Nightly (per-workflow build status by architecture)

| Metric | ClickHouse Column | Status |
|---|---|---|
| ROCm version | `rocm_version` | ✅ |
| Release type | `release_type` (Enum) | ✅ |
| Run ID / number | `run_id`, `run_number` | ✅ |
| Architecture | `architecture` | ✅ |
| Platform (Linux/Windows) | `platform` (Enum) | ✅ |
| Duration | `execution_time_seconds` (ALIAS) | ✅ |
| Queue time | `queue_time_seconds` (ALIAS) | ✅ |
| Python version | `python_version` | ✅ |
| PyTorch version | `torch_version` | ✅ |
| JAX version | — | **GAP** |
| Package format (deb/rpm) | — | **GAP** (low priority — native packages only) |
| Tarball / index URLs | Not stored; constructed from `rocm_version` + `architecture` | N/A — config-derived |
| Historical version dropdown | `rocm_version` + `created_at` | ✅ |

### CI/Nightly (`ci_nightly.yml` runs with job breakdown)

All fields covered: `run_id`, `run_number`, `status`, `result`, `trigger_event`, `total_duration_seconds` (ALIAS), `commit_sha`, `branch` on runs; `job_name`, `platform`, `architecture`, `test_name` on jobs. ✅

### Issues Tab

**Not in Quartz schema.** Currently fetched directly from GitHub Issues API. Requires a `therock_issues` table if HUD should be fully ClickHouse-backed (see RFC0002). Fields needed: `issue_number`, `title`, `state`, `author`, `author_avatar_url`, `labels` (with `name`, `color`, `description`), `assignees` (with `login`, `avatar_url`), `created_at`, `updated_at`, `closed_at`, `html_url`, `comments` (count), `body` (truncated preview for search).

### Bump PRs Tab

**Not in Quartz schema.** Same situation as Issues. Requires a `therock_pull_requests` table. Fields needed: `pr_number`, `title`, `state`, `author`, `head_ref`, `base_ref`, `is_draft`, `labels`, `created_at`, `updated_at`, `merged_at`, `html_url`. The HUD also shows a **submodule inventory** view — each row is a `.gitmodules` entry (`name`, `path`, `url`, `currentSha` from the git tree), cross-referenced with open/merged bump PRs. This data comes from the GitHub Contents + Git Tree APIs, not from PRs alone.

### ROCm Systems / ROCm Libraries Tabs

Same data model as CI HUD + Issues + Bump PRs, filtered by `repository`. Schema supports this via the `repository` column in the primary key. ✅

---

## 2. Gap Summary

| Missing Field | Tabs Affected | Resolution |
|---|---|---|
| `commit_message` | CI HUD | Add `commit_message String` to `therock_workflow_runs` |
| `commit_author` | CI HUD | Add `commit_author String` (distinct from `triggered_by`) |
| `commit_author_avatar_url` | CI HUD  | Add column or construct from GitHub username |
| `jax_version` | Release Nightly | Add `jax_version Nullable(String)` to `therock_workflow_jobs` or use `extra_info` |
| `therock_issues` table | Issues  | TBD |
| `therock_pull_requests` table | Bump PRs | TBD |
