# Quartz

Quartz is the central CI/CD data hub for the ROCm ecosystem. It aggregates the
build and test results of [ROCm/TheRock](https://github.com/ROCm/TheRock) and of
the downstream projects that depend on it. The aggregated data is exposed through
two paths: machine-readable status files for automated consumers, and analytics
dashboards for human viewing. For each ROCm nightly and prerelease, the status
files publish TheRock's build and test results.

Quartz accepts two kinds of data. TheRock reports the outcome of each release
build. Downstream projects report their own build and test results against a
given release. The first is available now. The second is planned.

Producers (TheRock and, later, downstream projects) and consumers (downstream
projects and the analytics dashboards) interact with Quartz through GitHub Actions
workflows and files in this repository.

## How to interact with Quartz

| Interaction | Direction | Status | Guide |
|---|---|---|---|
| Poll `status.json` for ROCm release results | Read | Available | [docs/status-json/](docs/status-json/) |
| Push notifications on new results | Read | Planned | n/a |
| Report downstream build/test results back | Write | Planned | n/a |

Today the only way to interact with Quartz is to poll the published
`status.json` files. If you maintain a downstream project and want to react to
new ROCm nightlies or prereleases, see
**[docs/status-json/](docs/status-json/)**. It covers what `status.json`
contains, the available endpoints, how to poll it, and a copy-paste example
GitHub Actions workflow.

## Contributing

Quartz stores machine-generated data alongside its source code. Automated jobs
commit status files to `main` continuously, so on a busy day `main` collects
hundreds of bot commits. To keep the history of the *source code*
readable - and not buried under that automated churn - human changes do not land
on `main` directly.

Instead, open your pull request against **`staging`**:

1. Branch off `staging` and make your change.
2. Open a pull request targeting `staging` (not `main`).
3. Once it is reviewed and merged into `staging`, automation promotes it to
   `main`.

> **Note:** the automatic `staging` -> `main` promotion is planned, not yet
> live. Until it lands, a maintainer promotes `staging` to `main` manually. Open
> your PR against `staging` regardless, so no manual rebasing is needed once the
> automation is in place.

Do not open pull requests against `main`; they will be redirected to `staging`.

## Reference

- **RFC-0011** - the authoritative design and full delivery plan:
  [`docs/rfcs/RFC0011-Quartz-CICD-Datahub.md`](https://github.com/ROCm/TheRock/blob/main/docs/rfcs/RFC0011-Quartz-CICD-Datahub.md)
  in ROCm/TheRock. Discussion:
  [ROCm/TheRock#3782](https://github.com/ROCm/TheRock/discussions/3782).
- **[docs/status-json/](docs/status-json/)** - the consumer guide for `status.json`.
