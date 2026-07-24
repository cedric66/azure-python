# AKS Reporting Toolkit

One front-door Python script, `aks_report.py`, for AKS reports across many Azure
subscriptions using **subscription-level read access only** (no kubectl). Report
modules live under `reports/<category>/`; day-to-day usage goes through the
launcher. Every report writes a formatted multi-tab `.xlsx` into `out/` (override
with `--out`).

Built for scale: ~25 subscriptions / ~500 clusters. Inventory comes from Azure
Resource Graph (a handful of calls for the whole fleet); cost comes from
subscription-scope Cost Management queries (~3 per subscription, not per cluster);
429 throttling is handled automatically.

## Quick start

```bash
uv sync                                   # install deps (see docs/install.md)
uv run python aks_report.py               # interactive menu: report → scope
```

Or skip the menu:

```bash
uv run python aks_report.py inventory --all
uv run python aks_report.py cost --subs contoso-platform --env dev
uv run python aks_report.py spot-eviction --all --days 14
uv run python aks_report.py list          # every report key + description
```

Scope flags (`--all`, `--subs`, `--env`, `--nonprod`, `--cluster`,
`--cluster-contains`, …) work on every report — see
[docs/usage.md](docs/usage.md).

## Documentation

| Guide | What's in it |
|---|---|
| [Install & setup](docs/install.md) | Linux / Windows / Docker setup, the Python-interpreter note, dependency baseline |
| [Usage & scope](docs/usage.md) | The launcher, choosing scope, the `subscriptions.csv` input, worked examples, rate limits, troubleshooting |
| [Report reference](docs/reports.md) | Every report: what it answers, data sources, and sample output fields |
| [Sandbox & policy testing](docs/sandbox.md) | The `sandbox` family: deploy, kubectl, Gatekeeper tests, clone, impact, spot-sim, upgrade rehearsal, conformance, rearch |
| [Spot reports & eviction testing](docs/spot.md) | Spot cost/savings/eviction reporting **and how to test `spot-eviction`** |
| [Vulnerability report](docs/vulnerabilities.md) | Prisma/CVE enrichment + layer classification |
| [Lifecycle & EOL radars](docs/lifecycle.md) | Public lifecycle data (endoflife.date, MS Learn, GitHub) — no Azure access |
| [Output formats](docs/output-formats.md) | Workbook layout, charts, Markdown→DOCX/PDF, architecture-design companions |

Deep dives:
[spot-eviction verification runbook](docs/spot_eviction_verification.md) ·
[cost-optimization design note / backlog](docs/cost-optimization-beyond-spot.md)
(the levers behind the `efficiency` and `optimization` reports, plus what's still unbuilt).

## Reports at a glance

Run any of these as `aks_report.py <key>` (aliases in
[docs/reports.md](docs/reports.md)):

- **Estate** — `inventory`, `360`
- **Cost** — `cost`, `deepdive`, `efficiency`, `optimization`, `utilization`, `tags`
- **Spot** — `spot`, `spot-design`, `spot-savings`, `spot-eviction`
- **Security & policy** — `governance`, `conformance`, `policy`, `policy-components`, `vulnerabilities`
- **Lifecycle** — `version`, `container-eol`, `aks-lifecycle`
- **Platform** — `design`, `network`, `rearch`

Plus the `sandbox` command family and a `convert` (Markdown→DOCX/PDF) command.

## Layout

```
aks_report.py          launcher (maps each report key → reports.<category>.<module>)
azrep/                 shared library (HTTP client, ARG, Cost Mgmt, Excel, sandbox, …)
reports/<category>/    report modules: estate, cost, spot, security, lifecycle, platform
examples/              *.example.* config/rule templates (incl. subscriptions.example.csv)
manifests/, policies/  vendored descheduler + sample policy test manifests
docs/                  this documentation
out/                   generated .xlsx (gitignored)
```

Contributor map: [CLAUDE.md](CLAUDE.md).
