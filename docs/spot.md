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

`spot-eviction` assesses per-pool preemption risk from two Reader-scope,
kubectl-free signals and recommends lower-eviction SKUs:

- **EvictionSnapshot** — per-pool roll-up of `healthresources`
  `VirtualMachinePreempted` annotations (a named, platform-initiated eviction
  event; a current, *ephemeral* snapshot). Raw rows stay on `RawHealthResources`.
- **ChurnTrend** — node-RG Activity Log VMSS delete/deallocate ops aggregated per
  day (durable but noisy; mixes evictions with autoscale-down). Churn is
  attributed to the pool its scale-set name resolves to; anything that does not
  resolve is reported per cluster in a separate *unattributed* column rather than
  charged to every pool.
- **SkuAlternatives** — from the ARG `SpotResources` banded eviction rate
  (`0-5`…`20+` % next-hour) per SKU/region, the report finds same-vCPU/mem
  in-region SKUs with a *lower* band as swap candidates, with a retail price
  delta and (with `--placement-score`) a forward-looking High/Med/Low placement
  score. A swap means a new pool + drain (spot priority is immutable), so every
  row carries a `verify_before_move` caveat.

`RiskAssessment` scores each spot pool HIGH/MED/LOW from those signals plus the
pool's eviction band, whether it is prod, whether its bid is price-capped and
whether it is single-zone, and spells out the reasons in a `Risk Reason` column.

Tabs: `Scorecard`, `RiskAssessment`, `SkuAlternatives`, `EvictionSnapshot`,
`ChurnTrend`, `RemediationGuide`, `SpotPoolInventory`, `EvictionRates`,
`RawHealthResources`, `RawActivityLog`, `Limitations`.

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

Watch the log line `Spot eviction rates: N rows from ARG, M kept after region
filter …` — it tells you whether the `SpotResources` data behind the
`SkuAlternatives`/`EvictionRates` tabs came back.

Isolate the `SpotResources` data source directly (fastest check when those tabs
are empty):

```bash
az extension add --name resource-graph   # one-time
az graph query -q "SpotResources | where type =~ 'microsoft.compute/skuspotevictionrate/location' | project skuName=tostring(sku.name), location, evictionRate=tostring(properties.evictionRate) | limit 25" -o table
```

Full verification runbook (does `healthresources` emit for AKS VMSS-Uniform
nodes? does `SpotResources`/Placement Score return data in your tenant?):
[docs/spot_eviction_verification.md](spot_eviction_verification.md).
