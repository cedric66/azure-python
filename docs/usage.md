# Usage & scope

_[← Back to the README](../README.md)_

How to drive the launcher: the single entry point, choosing scope, the subscriptions input file, worked examples, and operational notes.

## Single Entry Point

Use this one script:

```bash
uv run python aks_report.py
```

It opens a menu, then asks for subscription -> environment -> cluster scope.
You can also skip the menu:

```bash
uv run python aks_report.py inventory --all
uv run python aks_report.py cost --subs contoso-platform --env dev
uv run python aks_report.py deepdive --env dev --cluster aks-dev-01
uv run python aks_report.py design --cluster aks-dev-01 --all
uv run python aks_report.py design --subs contoso-platform --rg rg-apps-dev
uv run python aks_report.py network --nonprod
uv run python aks_report.py optimization --cluster-contains payments
uv run python aks_report.py spot-design --cluster aks-dev-01
uv run python aks_report.py spot-eviction --all --days 14
uv run python aks_report.py spot-eviction --nonprod --only-spot-clusters
uv run python aks_report.py spot-eviction --all --placement-score
uv run python aks_report.py spot-savings --cluster aks-dev-01
uv run python aks_report.py spot-savings --all
uv run python aks_report.py spot-savings --all --only-spot-clusters
uv run python aks_report.py spot-savings --nonprod-spot   # non-prod clusters that run spot (BU before/after slide)
uv run python aks_report.py spot-savings --all --no-eviction-scan
uv run python aks_report.py convert README.md --to all --config examples/report_style.example.yaml
uv run python aks_report.py sandbox plan examples/sandbox.example.yaml
uv run python aks_report.py list
```

## Choosing Scope

Every script uses the same narrowing model:

1. subscription
2. environment
3. cluster

If you do not specify one of those dimensions, it means **all** for that
dimension. For example, `--env dev` means all dev clusters across all included
subscriptions; `--subs <one-sub>` with no environment means every environment in
that one subscription.

When you run a script without scope flags, it prompts in that order:

```
Scope step 1/3 - subscription   [Enter = all subscriptions]
Scope step 2/3 - environment    [Enter = all environments]
Scope step 3/3 - cluster        [Enter = all clusters]
```

Or skip the prompt with flags:

```bash
uv run python aks_report.py inventory --all
uv run python aks_report.py inventory --subs 00000000-0000-0000-0000-000000000001
uv run python aks_report.py inventory --subs contoso-platform --env dev
uv run python aks_report.py inventory --env sit
uv run python aks_report.py inventory --nonprod
uv run python aks_report.py inventory --env dr --cluster-prefix aks-r
uv run python aks_report.py inventory --cluster aks-dev-01
uv run python aks_report.py inventory --cluster-prefix aks-d
uv run python aks_report.py inventory --cluster-contains payments
```

Fleet-level reports treat a blank cluster filter as all clusters. `deepdive`
is the exception because it makes several Cost Management queries per cluster; if
you do not pass `--cluster`/`--cluster-id`, it asks which single cluster to
analyze after subscription and environment narrowing.

A cluster's environment = cluster tags -> resource group tags -> name inference
(tag keys checked: `environment`, `env`, `stage`; override with
`--env-tag-keys`). If no tag is present, the scripts infer from cluster,
resource-group, or AKS node resource-group names. Subscription names are not
used for environment inference because one subscription can contain many
environments.

Default name inference examples:

| Name token | Environment |
|---|---|
| `dev`, `development`, `-d-`, `-d01` | `dev` |
| `sit`, `-s-`, `-s01` | `sit` |
| `dr`, `-r-`, `-r01` | `dr` |
| `uat`, `-u-`, `qa`, `-q-` | `uat`, `qa` |
| `prod`, `prd`, `production`, `-p-` | `prod` |

Override short-code mapping if your naming is different:

```bash
uv run python aks_report.py inventory --env-code-map d=dev,s=sit,r=dr,p=prod,t=tr
```

Disable name inference entirely:

```bash
uv run python aks_report.py inventory --no-name-env
```

Unknown-env clusters are **excluded** from `--nonprod` by default (safer); add
`--include-unknown-env` to include them. `--nonprod` treats only
`prod,production,prd,live` as production by default. If DR should be excluded
from non-prod in your estate, run:

```bash
uv run python aks_report.py cost --nonprod --prod-values prod,production,prd,live,dr
```

## Input file

`subscriptions.csv` (copy the template from
[`examples/subscriptions.example.csv`](../examples/subscriptions.example.csv) — the
local `subscriptions.csv` is gitignored so real subscription IDs never get committed):

```csv
subscription_id,subscription_name,include
00000000-...,contoso-platform,Y
00000000-...,contoso-data,N
```

- `include=N` rows are ignored without deleting them from the file.
- A subscription can contain clusters from many environments. Environments are
  resolved per cluster from AKS tags, resource-group tags, or name inference.

## Usage examples

```bash
uv run python aks_report.py inventory --all
uv run python aks_report.py 360 --all                        # full estate, categorized
uv run python aks_report.py 360 --all --no-metrics           # skip Monitor calls
uv run python aks_report.py 360 --all --no-cost --no-metrics # Resource Graph only, fastest
uv run python aks_report.py deepdive --env dev              # interactive cluster picker
uv run python aks_report.py deepdive --cluster my-aks --all
uv run python aks_report.py design --cluster my-aks --all
uv run python aks_report.py design --subs contoso-platform --rg rg-apps-dev
uv run python aks_report.py design --subs contoso-platform --all
uv run python aks_report.py cost --nonprod
uv run python aks_report.py cost --subs contoso-platform --env dev
uv run python aks_report.py cost --env sit --cluster-prefix aks-s
uv run python aks_report.py cost --all --actual --granularity Daily
uv run python aks_report.py version --all
uv run python aks_report.py spot --nonprod
uv run python aks_report.py spot --subs contoso-platform --env dev
uv run python aks_report.py spot --subs contoso-platform --only-spot-clusters
uv run python aks_report.py spot-design --cluster aks-dev-01
uv run python aks_report.py spot-eviction --all --days 14
uv run python aks_report.py spot-eviction --nonprod --only-spot-clusters
uv run python aks_report.py spot-eviction --all --placement-score
uv run python aks_report.py utilization --env dev --days 14
uv run python aks_report.py governance --all
uv run python aks_report.py conformance --golden golden.yaml --all
uv run python aks_report.py policy --all
uv run python aks_report.py network --all
uv run python aks_report.py tags --all --required-tags environment,owner,costcenter,application
uv run python aks_report.py optimization --nonprod --days 14
uv run python aks_report.py efficiency --all
uv run python aks_report.py efficiency --nonprod --no-retail-prices
uv run python aks_report.py container-eol
uv run python aks_report.py container-eol --products ubuntu,golang,dotnet
uv run python aks_report.py aks-lifecycle --releases 52
uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules examples/vulnerability_classification.example.json
uv run python aks_report.py vulnerabilities --cves cves.txt --offline
```

## Reading the cost numbers

- **Amortized cost** spreads reservation & savings-plan purchases across the
  resources that consumed them - this is the "true" cost of a cluster, and the
  default everywhere. `--actual` / the AmortizedVsActual tab show billed cost;
  the delta is your RI/SP benefit allocation.
- A cluster's cost = everything in its node resource group (`MC_*`) plus the
  managed-cluster resource fee (uptime SLA). Node-pool costs are mapped from
  VMSS names (`aks-<pool>-xxxx-vmss`).
- `PricingModel` splits Spot / OnDemand / Reservation / SavingsPlan.
- The current month is always partial (MTD). Trend comparisons (MoM, SKU
  GROWN/SHRUNK) only use full months for exactly that reason.
- Currency: CostUSD. Cost data lags usage by up to ~24-48h.
- Spot savings estimates use public retail prices - EA/MCA discounts are not
  reflected; treat them as screening numbers.

## Rate limits (handled for you)

- **Cost Management** is the strict one: QPU-throttled per tenant (12/10s,
  60/min, 600/hr; ~1 QPU per month of data per query). The client paces calls,
  watches the `qpu-remaining` header, and honors `retry-after` on 429.
  Expect `fleet_cost.py` over 25 subs / 500 clusters to take **5-15 minutes** -
  that's pacing, not a hang; progress prints per subscription.
- Resource Graph / ARM reads: exponential backoff with jitter on 429/5xx.
- `cluster_360.py` inherits all of the above: subscription-scope cost queries
  (never per cluster), one AKS versions call per region, one paced Monitor call
  per running cluster. `--no-cost` / `--no-metrics` skip the slow sources.
- `utilization_idle.py` and `optimization_report.py` make one Monitor call per
  cluster when metrics are enabled, paced (~0.15s).
- `vulnerability_report.py` batches NVD CVE lookups up to 100 CVEs per request
  and uses `--nvd-delay` between requests. Add `--offline` for fully local
  Prisma/classification-rule classification.
- Reruns are independent; if a run dies mid-way just rerun it.

## Troubleshooting

- `DefaultAzureCredential failed` -> run `az login` (and `az account set` if
  your default tenant differs).
- Cost tabs empty / 401 on cost queries -> missing Cost Management read on that
  subscription type.
- Lots of `HTTP 429 ... backing off` lines -> normal under tenant-wide load;
  the script recovers by itself.
- A subscription in the CSV that you cannot read -> Resource Graph silently
  returns nothing for it; check counts on the ReadMe tab of each report.

## Testing without Azure

```bash
uv run python tests/smoke_test.py
uv run python tests/test_sandbox.py
uv run python tests/test_spot_split.py
uv run python tests/test_vulnerability_report.py
```

Runs the launcher and report modules end-to-end against mocked Azure responses,
plus focused spot-design and Prisma XLSX/JSON-rules vulnerability tests. The
tests validate generated workbooks, including sheet presence, SKU-change
detection, EOL flags, governance failures, golden-config drift, policy blind
spots, subnet capacity, tag gaps, optimization candidates, spot split design,
vulnerability classification, and sandbox planning. `test_sandbox.py` covers
the sandbox command family offline: clone field mapping, policy impact payloads
and teardown, Gatekeeper test-case expectations, kubeconfig fetch, spot-sim ARM
payloads and the scenario matrix, and upgrade hop computation - no Azure access
and no az/kubectl binaries needed.
