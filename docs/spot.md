# Spot reports & eviction testing

_[← Back to the README](../README.md)_

Spot cost and risk reporting, and how to test the spot-eviction report end to end.

## Spot Report

`spot` covers both current-state spot configuration/cost and opportunity
screening in one workbook (`spot-detail` and `spot-opportunity` remain as
aliases):

```bash
uv run python aks_report.py spot --subs contoso-platform --env dev
uv run python aks_report.py spot --subs contoso-platform --only-spot-clusters
uv run python aks_report.py spot --nonprod --cluster-prefix aks-d
uv run python aks_report.py spot --nonprod --no-retail-prices   # skip retail lookups
```

Workbook tabs include:

- `Summary`: cluster environment, spot/on-demand node counts, VM
  SKUs, support plan, autoscaler expander, max capacity, and cost split.
- `SpotNodePools`: pool name, mode, SKU, node count, autoscaling min/max,
  zones, eviction policy, spot max price, taints, labels, and subnet IDs.
- `OnDemandNodePools`: the regular pools that provide fallback capacity.
- `NodePoolSkuSummary`: nodes and min/max capacity by SKU, mode, priority, and
  VM family.
- `AutoscalerConfig`: cluster autoscaler profile, expander, balance-similar
  setting, scale-down settings, and max spot/on-demand capacity.
- `SpotAssessment`: independent checks for prod spot, system on-demand pool,
  multi-zone, multi-VM-family, price caps, autoscaling, min spot capacity,
  spot taint visibility, and autoscaler configuration.
- `Candidates`: user-mode, Linux, non-spot pools with running nodes that could
  move to spot, with estimated savings from the public Retail Prices API
  (screening only - EA/MCA rates and RI/SP coverage are not reflected).
- `CostByCluster`, `CostTrend`, `CostByNodePool`, `OtherCostItems`,
  `CostByMeter`, `RawResourceCost`: amortized spot/on-demand/RI/SP cost, pool
  cost, and non-VMSS costs such as disks, public IPs, and cluster fee.
- `PriceReference`: retail on-demand vs spot price per (region, VM size) used
  by the candidate screening.

This report still uses subscription-level data only. It cannot verify pod
tolerations, priority-expander ConfigMaps, PDBs, or workload criticality without
kubectl access.

## Spot eviction report

`spot-eviction` assesses per-pool preemption risk from Reader-scope,
kubectl-free signals and recommends lower-eviction SKUs. Everything at the
`(cluster, pool)` grain lands on **one** sheet, `SpotPoolRisk` — one row per spot
pool carrying the pool config, both observed signals, the durable eviction band,
the HIGH/MED/LOW verdict with its `Risk Reason`, and the swap candidate:

- **Preemption columns** (`Preemptions`, `Last Preemption`, `Preemption Age
  (days)`) — `healthresources` `VirtualMachinePreempted` annotations (a named,
  platform-initiated eviction event; a current, *ephemeral* snapshot).
- **Churn columns** (`Churn (ops/day)`, `Cluster Churn Unattributed (ops/day)`) —
  node-RG Activity Log VMSS delete/deallocate ops (durable but noisy; mixes
  evictions with autoscale-down). Churn is attributed to the pool its scale-set
  name resolves to; anything that does not resolve is reported per cluster in the
  separate *unattributed* column rather than charged to every pool.
- **Swap columns** (`Recommended SKU`, `Recommended Band %`, `Arch Note`, `Price
  Delta $/hr`, `Placement Score`, `SKU Swap Status`, `verify_before_move`) — from
  the ARG `SpotResources` banded eviction rate (`0-5`…`20+` % next-hour) per
  SKU/region, the report finds same-vCPU/mem in-region SKUs with a *lower* band,
  with a retail price delta and (with `--placement-score`) a forward-looking
  High/Med/Low placement score. A swap means a new pool + drain (spot priority is
  immutable), so every row carries a `verify_before_move` caveat.

Tabs: `ReadMe` (method + limitations), `Scorecard`, `SpotPoolRisk`, `ChurnTrend`
(daily churn series + chart), `RemediationGuide`, then two reference sheets —
`EvictionRates` (the raw `SpotResources` bands) and `RawEvidence` (Resource
Health annotations ∪ Activity Log churn ops, tagged by a `Source` column).

`EvictionRates` never ships blank: when there is nothing to show it carries a
one-row diagnostic naming which of the three causes applied — no spot pool in
scope (the query is skipped), ARG returned zero `SpotResources` rows, or rows
came back but none for a region holding a spot pool.

Note that `EvictionRates` and the preemption rows are *independent* sources, so
one can be empty while the other is full. The `healthresources` query is
fleet-wide, not restricted to AKS node RGs, so it also catches standalone spot
VMs, non-AKS scale sets, and pools that no longer exist — those rows read as
cluster `(unmatched)`.

The run ends with `Report written: out/aks_spot_eviction_<scope>_<stamp>.xlsx`.

### Testing spot-eviction

Offline (no Azure) — proves the report and the SKU-swap logic build:

```bash
uv run python tests/smoke_test.py     # includes the spot-eviction check (chk_eviction)
```

Against real Azure (subscription Reader is enough):

```bash
# Full fleet, 14-day churn window  → out/aks_spot_eviction_*.xlsx
uv run python aks_report.py spot-eviction --all --days 14

# Only clusters that currently run a spot pool
uv run python aks_report.py spot-eviction --all --only-spot-clusters

# Non-prod spot clusters only
uv run python aks_report.py spot-eviction --nonprod --only-spot-clusters

# Add the forward-looking Spot Placement Score (preview API; extra POST per sub/region)
uv run python aks_report.py spot-eviction --all --placement-score

# Snapshot only (skip the Activity Log churn scan)
uv run python aks_report.py spot-eviction --all --no-eviction-scan
```

Two log lines settle where the data came from:

- `Spot eviction rates: N rows from ARG, M kept after region filter (K spot-pool
  region(s) in scope: …)` — whether the `SpotResources` data behind the
  `EvictionRates` tab and the swap columns came back. If there are no spot pools
  in scope you get `No spot node pool in scope, so the SpotResources query was
  skipped …` instead.
- `Preemption annotations: N row(s), M matched to a cluster in scope` — if `N` is
  non-zero but `M` is 0, those preemptions were *not* AKS node pools in scope
  (the query is fleet-wide), so they say nothing about AKS VMSS-Uniform emission.

Isolate the `SpotResources` data source directly (fastest check when
`EvictionRates` is empty):

```bash
az extension add --name resource-graph   # one-time
az graph query -q "SpotResources | where type =~ 'microsoft.compute/skuspotevictionrate/location' | project skuName=tostring(sku.name), location, evictionRate=tostring(properties.evictionRate) | limit 25" -o table
```

Full verification runbook (does `healthresources` emit for AKS VMSS-Uniform
nodes? does `SpotResources`/Placement Score return data in your tenant?):
[docs/spot_eviction_verification.md](spot_eviction_verification.md).
