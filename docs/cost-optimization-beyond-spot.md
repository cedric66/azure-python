# Cost & efficiency optimization beyond spot — improvement plan

Status: proposal / plan only. No code in this change.
Audience: contributors to the AKS Reporting Toolkit.
Scope: subscription **Reader**-only, fleet-wide (~25 subs / ~500 clusters), no
kubectl against the fleet — same constraints every report module already lives
under. Org constraints carried through: **no Karpenter/NAP, no Cilium**; node
elasticity comes from cluster autoscaler + descheduler + topology spread only.

---

## 1. Why this doc

The branch `feature/spotJune2026` has pushed spot adoption hard (`spot`,
`spot-design`, `spot-savings`, `sandbox spot-sim`). Spot is one lever. This plan
inventories the **other** cost/efficiency levers an AKS fleet has, checks each
against what we can actually see at Reader scope, and proposes concrete,
low-duplication additions to the toolkit. The goal is a ranked menu, not a
rewrite.

---

## 2. What the toolkit already covers (don't rebuild these)

| Lever | Where it lives today |
|---|---|
| Spot adoption + realized savings | `spot`, `spot-design`, `spot-savings`, `sandbox spot-sim` |
| Reservations / Savings Plans (steady OnDemand → commit) | `fleet_cost.CommitmentOpportunity`, `optimization_report` RI/SP candidate |
| Idle / underutilized / stopped-but-billing | `utilization_idle`, `optimization_report`, `cluster_360` |
| Rightsizing screen (CPU/mem platform metrics) | `optimization_report` `RIGHTSIZE_OR_SCALE_DOWN` |
| Cost spikes / movers / SKU meter changes | `fleet_cost` `TopMovers`, `MeterChanges` |
| Orphaned resources (single sub) | `subscription_rearch` (advisorresources + orphan KQL) |

So the candidate engine in `optimization_report.py` and the commitment logic in
`fleet_cost.py` are the two places new **cost** levers should plug into; ARG-only
config levers are better as a new dedicated report.

---

## 3. The levers beyond spot

Each lever below lists: the saving mechanic, whether the data is already in hand
(see `azrep/fleet.py` flatten + `azrep/costmgmt.py` + `azrep/armextras.py`), the
proposed toolkit change, and the main caveat. Levers are grouped by
impact/effort so we can sequence them.

### Tier 1 — high impact, data already in ARG, low build effort

These three are nearly free to build because the signal is a config field we
already flatten, and the saving is concrete (not a screening %).

#### 1.1 Right-tier the control plane (Free vs Standard vs Premium)
- **Mechanic:** `sku_tier` is captured per cluster (`fleet.py:70`). Standard
  (Uptime SLA) bills ~$0.10/hr ≈ **~$73/mo per cluster**; Premium (LTS) ~$0.60/hr
  ≈ **~$438/mo per cluster** on top. A non-prod cluster on a paid tier is pure
  waste; a prod cluster on Free has no financially-backed SLA (risk flag, not a
  saving).
- **Data:** ARG only — `sku_tier` + `environment` (already resolved per cluster).
  No Cost Management call needed for the estimate; multiply tier rate × cluster
  count × 730 hr.
- **Proposed change:** new candidate type `CONTROL_PLANE_TIER` in
  `optimization_report` (non-prod on Standard/Premium → downgrade; prod on Free →
  risk note, zero saving), or a row group in the new efficiency report (§4).
- **Caveat:** verify the current per-tier hourly price (changes over time); LTS
  Premium may be a deliberate support choice — flag, don't auto-recommend.

#### 1.2 Non-prod start/stop scheduling
- **Mechanic:** non-prod clusters that run 24/7 but are only used in business
  hours. AKS supports `az aks stop/start`; a nightly + weekend stop is roughly a
  **60–70% cut on node compute** for that cluster (deallocated nodes stop
  billing; disks/IPs/fee remain). The descheduler/topology-spread direction this
  org already uses pairs naturally with scheduled scale-down.
- **Data:** `environment` (non-prod) + `power_state` (not already stopped) from
  ARG; node-compute run-rate from the amortized node-RG cost we already pull. The
  saving estimate = node compute × business-hours fraction avoided.
- **Proposed change:** candidate type `OFFHOURS_STOP_CANDIDATE`. Reuse
  `optimization_report`'s loaded cost + `is_prod()`; estimate `avg_monthly_cost ×
  off_hours_pct` with a tunable `--offhours-pct` (default ~0.65).
- **Caveat:** can't see usage calendar at Reader scope — this is "candidate by
  environment + run-rate," validate against actual usage before scheduling. Stop
  ≠ free (managed disks, public IPs, cluster fee still bill — say so, like the
  existing STOPPED note in `utilization_idle`).

#### 1.3 Ephemeral OS disk conversion
- **Mechanic:** `os_disk_type` is captured per pool (`fleet.py:155`,
  Managed vs Ephemeral). Managed OS disks bill as Premium/Standard SSD per node,
  per month; **Ephemeral OS disks are free** (use the VM cache/temp/local NVMe)
  and faster, for any VM SKU whose cache/temp disk ≥ the OS disk size.
- **Data:** ARG only — `os_disk_type`, `vm_size`, `os_disk_gb`, `count` per pool.
  Eligibility = SKU cache/temp size ≥ `os_disk_gb` (needs a small static SKU
  capability lookup or the retail/compute SKU API).
- **Proposed change:** pool-level rows in the efficiency report flagging
  `MANAGED_OS_DISK_ON_EPHEMERAL_CAPABLE_SKU`; saving = managed-disk meter per node
  × node count.
- **Caveat:** Ephemeral OS disk is **immutable on an existing pool** — same
  pattern as spot priority: it means create a new pool + migrate. SKU
  cache-size eligibility must be confirmed; some SKUs have no temp disk at all.

### Tier 2 — high impact, needs a price/metric join (medium effort)

#### 2.1 VM generation & family modernization (incl. ARM64)
- **Mechanic:** price arbitrage at equal vCPU/mem. Dv3/Dsv3 → Dsv5/Dasv5 is
  typically cheaper for the same shape; **ARM64 Ampere (Dpsv5/Dpds)** is often
  ~30–50% better price-perf where workloads have multi-arch images. This is
  distinct from rightsizing (same shape, cheaper SKU — not fewer resources).
- **Data:** `vm_sizes`/`vm_size` per pool (ARG) + `retail_vm_prices()` already in
  `armextras.py` for $/hr per SKU/region. Need a vCPU/mem map per SKU to find the
  cheapest same-shape SKU in-region.
- **Proposed change:** `SKU_MODERNIZATION` rows — current SKU vs cheapest
  equivalent newer-gen (and an ARM64 column gated on a compatibility caveat);
  saving = (od_hr_old − od_hr_new) × nodes × 730. Reuses the retail-price plumbing
  and the per-pool node counts the spot reports already build.
- **Caveat:** ARM64 needs multi-arch container images and node taints/affinity —
  surface as "candidate, verify image support," never auto-apply. Regional SKU
  availability and quota must be checked. Don't conflate with rightsizing.

#### 2.2 Autoscaler & floor (min_count) hygiene
- **Mechanic:** three cheap wins from config we already flatten:
  (a) **user pools without autoscaling** (`autoscaling=False`) sit at a fixed
  `count` and never scale down; (b) **min_count too high** relative to measured
  utilization = a permanently over-provisioned floor; (c) **autoscaler profile**
  not tuned for bin-packing (`expander` not `least-waste`,
  `balanceSimilarNodeGroups` off, slack `scale-down-utilization-threshold` /
  `scale-down-unneeded-time`).
- **Data:** ARG only for (a) and (c) — every `autoscaler_*` field and the per-pool
  `autoscaling/min_count/max_count` are already flattened
  (`fleet.py:108-122,150-152`). (b) joins min_count to the platform-metric
  utilization `optimization_report` already pulls.
- **Proposed change:** a governance-style `AutoscalerHygiene` tab (pure ARG,
  cheap) listing per-pool findings + a fleet best-practice scorecard; optionally a
  `SCALE_FLOOR_REVIEW` cost candidate when min_count floor × node cost is high and
  utilization is low.
- **Caveat:** system pools need a sane floor; don't flag system-pool min_count the
  same as user pools. This stays advisory — autoscaler settings interact with
  workload PDBs we can't see.

### Tier 3 — supporting / hidden-cost surfacing

#### 3.1 Container Insights / Log Analytics ingestion cost
- **Mechanic:** `addon_monitoring` is captured. Container Insights data ingestion
  into Log Analytics is billed per GB and for chatty clusters can rival or exceed
  node compute — a classic hidden AKS cost. Levers: cost-optimized data collection
  presets, Basic Logs tables, sampling.
- **Data:** the monitoring meters show up in Cost Management
  (`microsoft.operationalinsights/*`). Caveat: the workspace is frequently in a
  *different* RG than the node RG, so our node-RG-scoped cost query may miss it —
  this needs a workspace-aware or meter-name-based query at sub scope.
- **Proposed change:** a `MonitoringCost` tab (or candidate) that ties ingestion
  spend to clusters with monitoring enabled; flag clusters where ingestion >> a
  threshold share of cluster cost. Mark workspace-attribution as approximate.
- **Caveat:** attribution is the hard part — be explicit it's fleet/sub-level, not
  always clean per-cluster.

#### 3.2 Node-pool fragmentation / consolidation
- **Mechanic:** many tiny pools, single-node user pools, or several pools on the
  same VM size that could merge → fewer reserved-instance / autoscaler floors and
  better bin-packing (descheduler-friendly, no Karpenter needed).
- **Data:** ARG pool list only (counts, vm_size, mode, taints/labels, zones).
- **Proposed change:** `PoolFragmentation` tab — clusters with N user pools above
  a threshold, same-SKU mergeable pools, sub-min single-node pools.
- **Caveat:** taints/labels/zone pinning often exist for a reason; advisory only.

#### 3.3 Defender / addon cost-vs-value on non-prod
- **Mechanic:** `defender` and other addon flags are captured. Defender for
  Containers bills per vCPU-hour; enabling it on throwaway non-prod can be
  disproportionate.
- **Proposed change:** a small non-prod-addon audit row group (low priority).
- **Caveat:** security posture call, not purely cost — present as a review item.

#### 3.4 Fleet-wide orphaned node-RG resources
- **Mechanic:** `subscription_rearch` already finds orphans but is single-sub.
  Orphaned disks/IPs/NICs/snapshots in `MC_*` node RGs are a recurring fleet leak.
- **Proposed change:** lift the orphan KQL subset to a fleet-wide node-RG sweep
  (or document running `rearch` per sub). Lower priority — `rearch` covers it
  per-sub today.

---

## 4. Recommended shape: one new report + two candidate types

Rather than scatter these, the highest-leverage build is:

**A new ARG-cheap report `cost_efficiency.py` (key `efficiency`)** that bundles
the config-driven levers needing little/no Cost Management traffic:
control-plane tier (1.1), ephemeral OS disk (1.3), SKU modernization (2.1, with
retail prices), autoscaler/floor hygiene (2.2), pool fragmentation (3.2). It
mirrors the layered pattern: `Scorecard` (KPI cards: total addressable monthly
saving, count by lever, prod/non-prod split) → per-lever summary tabs →
`Recommendations` (one ranked $ row per action with a `verify_before_move`
caveat) → reference. This complements the **cost-heavy** `optimization_report`
(which stays the metrics+cost screen) instead of duplicating it.

**Plus two new candidate types added to `optimization_report`'s existing engine**
where the cost/metrics are already loaded: `OFFHOURS_STOP_CANDIDATE` (1.2) and
`CONTROL_PLANE_TIER` (1.1) — both reuse `avg_monthly_cost` and `is_prod()` that
the loop already computes.

Sequencing by value/effort:

1. **Control-plane tier + off-hours stop** — biggest $/effort, ARG + already-loaded
   cost; ship as `optimization_report` candidates first (smallest diff).
2. **Ephemeral OS disk + autoscaler hygiene** — pure ARG; the core of the new
   `efficiency` report.
3. **SKU modernization** — reuses retail-price plumbing; add once the SKU
   vCPU/mem capability lookup exists (also unlocks better ephemeral eligibility).
4. **Monitoring cost + fragmentation** — supporting tabs once the report exists.

---

## 5. How it slots into the codebase (for whoever builds it)

Follow the standard module contract (see `CLAUDE.md` → Conventions):

- `cost_efficiency.py`: docstring (first line = argparse description), `main(argv)`
  using `base_parser()` → `load_subscriptions` → `pick_scope` → `connect()` →
  `load_fleet()` → build DataFrames → `excel` workbook → `excel.save(...)`.
  Layered tabs via `section=` (summary green → detail → reference gray). Use
  `excel.add_scorecard` for the KPI one-pager, `add_table` + `add_bar_chart` for
  lever tabs, matching `fleet_cost`/`spot_savings`.
- Wire into `aks_report.REPORTS` (key `efficiency`, aliases, module, title,
  description) — the launcher/menu come from that list.
- A SKU capability map (vCPU/mem/cache size) is the one new shared helper several
  levers want; natural home is `azrep/armextras.py` next to `retail_vm_prices`
  (compute SKUs API `Microsoft.Compute/skus`, or a small static table).
- **Tests:** add the module to `tests/smoke_test.py` import list + patch tuple,
  add a `run(...)` stanza asserting the new sheets + one behavioral check. The
  smoke fixture already has the shape we need (a stopped cluster, a non-prod
  cluster, mixed SKUs, a Standard-tier cluster can be added) — extend it minimally.
- Keep estimates honest and labelled "screening / verify before move," exactly
  like the existing optimization ReadMe and `spot_savings` verdict caveats.

---

## 6. Explicitly out of scope / constraints

- **Pod-level rightsizing** (requests vs usage) needs Container Insights/kubectl
  the fleet path deliberately avoids — node-level platform metrics remain the
  proxy (`utilization_idle`/`optimization_report`). Don't add a kubectl fleet path.
- **Karpenter/NAP and Cilium** are off-limits per org policy — consolidation and
  bin-packing recommendations stay within cluster autoscaler + descheduler +
  topology spread.
- All saving figures are **estimates from Reader-visible data** (config + amortized
  cost + retail price). Immutable-after-create properties (spot priority, ephemeral
  OS disk) mean the action is "new pool + migrate," never an in-place edit — call
  that out in every recommendation, as the spot reports already do.
- Pricing constants (tier $/hr, discounts) drift — keep them as named tunables
  with `--flag` overrides and a "verify current pricing" note, mirroring
  `fleet_cost`'s `--commit-discount`.

---

## 7. One-line summary per lever (the menu)

| # | Lever | Saving basis | Data ready? | Build home |
|---|---|---|---|---|
| 1.1 | Right-tier control plane | ~$73–438/mo per non-prod cluster | ARG (sku_tier) | optimization candidate |
| 1.2 | Non-prod off-hours stop | ~60–70% of non-prod node compute | ARG + cost | optimization candidate |
| 1.3 | Ephemeral OS disk | managed-disk meter per node | ARG (os_disk_type) | efficiency report |
| 2.1 | SKU modernization (incl. ARM64) | (old−new) $/hr × nodes | ARG + retail prices | efficiency report |
| 2.2 | Autoscaler / floor hygiene | over-provisioned floor + bin-packing | ARG (+ metrics) | efficiency report |
| 3.1 | Monitoring ingestion cost | per-GB Log Analytics | Cost Mgmt (sub scope) | new tab/candidate |
| 3.2 | Pool fragmentation | fewer floors, better packing | ARG (pools) | efficiency report |
| 3.3 | Non-prod Defender/addons | per-vCPU Defender | ARG (flags) | audit row group |
| 3.4 | Fleet orphaned node-RG resources | orphan disks/IPs/snapshots | ARG (extend rearch) | extend rearch |
</content>
</invoke>
