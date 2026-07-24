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

| Interaction                                 | Direction | Status    | Guide                                  |
| ------------------------------------------- | --------- | --------- | -------------------------------------- |
| Poll `status.json` for ROCm release results | Read      | Available | [docs/status-json/](docs/status-json/) |
| Push notifications on new results           | Read      | Planned   | n/a                                    |
| Report downstream build/test results back   | Write     | Planned   | n/a                                    |

Today the only way to interact with Quartz is to poll the published
`status.json` files. If you maintain a downstream project and want to react to
new ROCm nightlies or prereleases, see
**[docs/status-json/](docs/status-json/)**. It covers what `status.json`
contains, the available endpoints, how to poll it, and a copy-paste example
GitHub Actions workflow.

## Contributing

**Open your pull requests against `develop`, not `main`.** See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the branching model and contribution
policies.

## Reference

- **RFC-0011** - the authoritative design and full delivery plan:
  [`docs/rfcs/RFC0011-Quartz-CICD-Datahub.md`](https://github.com/ROCm/TheRock/blob/main/docs/rfcs/RFC0011-Quartz-CICD-Datahub.md)
  in ROCm/TheRock. Discussion:
  [ROCm/TheRock#3782](https://github.com/ROCm/TheRock/discussions/3782).
- **[docs/status-json/](docs/status-json/)** - the consumer guide for `status.json`.
