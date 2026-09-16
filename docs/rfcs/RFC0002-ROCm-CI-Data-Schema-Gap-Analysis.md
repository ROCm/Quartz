# RFC0002: ROCm CI Data Schema Gap Analysis

**Author:** geomin12
**Date:** 2026-04-30
**Status:** Draft

---

## Overview

This RFC analyzes the gap between the Quartz ClickHouse schema (v2.2.0) and the `rocm-ci-data-retrieval` schema to identify data that Quartz could benefit from but currently lacks. The goal is to ensure Quartz can serve as a comprehensive CI/CD data warehouse for the ROCm ecosystem.

---

## Schema Comparison

### Quartz Schema (Current)

Quartz uses two primary tables:
- `therock_workflow_runs` — Workflow-level metadata
- `therock_workflow_jobs` — Job-level execution data

### rocm-ci-data-retrieval Schema (Source)

A single table tracking GitHub Actions workflow test execution metadata:

| Column | Type | Description |
|--------|------|-------------|
| `os` | VARCHAR(50) | Operating system |
| `target` | VARCHAR(255) | Build/test target |
| `branch` | VARCHAR(255) | Git branch |
| `runner_name` | VARCHAR(255) | GitHub Actions runner |
| `test_name` | VARCHAR(255) | Test identifier |
| `shard_number` | INT | Current shard |
| `total_shards` | INT | Total shards |
| `created_at` | TIMESTAMP | Job creation time |
| `started_at` | TIMESTAMP | Job start time |
| `completed_at` | TIMESTAMP | Job completion time |
| `status` | VARCHAR(50) | Job status |
| `conclusion` | VARCHAR(50) | Job conclusion/result |
| `job_id` | VARCHAR(100) | GitHub job ID |
| `run_id` | VARCHAR(100) | GitHub run ID |
| `queue_time_seconds` | FLOAT | Time spent in queue |
| `job_time_seconds` | FLOAT | Job execution duration |
| `test_type` | VARCHAR(100) | Type of test |
| `repo` | VARCHAR(255) | Repository name |
| `owner` | VARCHAR(255) | Repository owner |
| `workflow_id` | VARCHAR(100) | Workflow ID |
| `html_url` | VARCHAR(500) | Link to job |

---

## Gap Analysis

### Fields Already Covered by Quartz

| rocm-ci-data Field | Quartz Equivalent | Table | Notes |
|--------------------|-------------------|-------|-------|
| `branch` | `branch` | Both | ✅ Identical |
| `runner_name` | `runner_name` | `therock_workflow_jobs` | ✅ Identical |
| `test_name` | `test_name` | `therock_workflow_jobs` | ✅ Identical |
| `shard_number` | `test_shard` | `therock_workflow_jobs` | ✅ Same concept |
| `total_shards` | `test_total_shards` | `therock_workflow_jobs` | ✅ Same concept |
| `created_at` | `created_at` | Both | ✅ Identical |
| `started_at` | `started_at` | Both | ✅ Identical |
| `completed_at` | `completed_at` | Both | ✅ Identical |
| `status` | `status` | Both | ✅ Quartz uses Enum8 |
| `conclusion` | `result` | Both | ✅ Quartz uses Nullable Enum8 |
| `job_id` | `job_id` | `therock_workflow_jobs` | ✅ Identical |
| `run_id` | `run_id` | Both | ✅ Identical |
| `test_type` | `test_type` | `therock_workflow_jobs` | ✅ Quartz uses Enum8 (unknown/smoke/full) |
| `repo` | `repository` | Both | ✅ Similar |
| `workflow_id` | `workflow_id` | `therock_workflow_runs` | ✅ Identical |
| `html_url` | `html_url` (ALIAS) | Both | ✅ Computed field |

### Fields Missing from Quartz (GAPS)

| rocm-ci-data Field | Status | Priority | Recommendation |
|--------------------|--------|----------|----------------|
| `os` | **GAP** | High | Add to `therock_workflow_jobs` |
| `target` | **GAP** | High | Add to `therock_workflow_jobs` |
| `owner` | **GAP** | Medium | Add to both tables |

---

## Detailed Gap Analysis

### 1. `os` — Operating System

**Current State:** Quartz has `platform` (Enum8: unknown/linux/windows) but lacks granular OS information.

**Gap:** The `os` field in rocm-ci-data provides more specific OS information (e.g., "ubuntu-22.04", "windows-2022", "rhel-8") rather than just the platform family.

**Recommendation:** Add `os String` column to `therock_workflow_jobs`:
```sql
os String CODEC(ZSTD(1))
```

**Use Cases:**
- Track CI failures by specific OS version
- Identify OS-specific regressions
- Support matrix builds across multiple Linux distributions

---

### 2. `target` — Build/Test Target

**Current State:** Quartz has no direct equivalent. Partial overlap with `architecture` and `build_variant`.

**Gap:** The `target` field captures the specific build or test target (e.g., "gfx90a", "gfx1100", "mi300x") which is critical for ROCm's GPU-centric CI.

**Recommendation:** Add `target String` column to `therock_workflow_jobs`:
```sql
target String CODEC(ZSTD(1))
```

**Use Cases:**
- Track build/test status per GPU target
- Analyze failure rates across different hardware targets
- Enable target-specific dashboards and filtering

---

### 3. `owner` — Repository Owner

**Current State:** Quartz stores `repository` but not the owner separately.

**Gap:** The `owner` field allows filtering by organization (e.g., "ROCm", "RadeonOpenCompute").

**Recommendation:** Either:
- Add `owner String` to both tables, OR
- Store as `repository` in format `owner/repo` and parse at query time

**Use Cases:**
- Support for forked repos data collection

---

## Summary: Required Schema Changes

### therock_workflow_jobs — New Columns

```sql
-- Add after `platform` column
os String DEFAULT '' CODEC(ZSTD(1)),
target String DEFAULT '' CODEC(ZSTD(1)),
```