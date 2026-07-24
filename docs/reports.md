# Report reference

_[← Back to the README](../README.md)_

Every report the launcher can run, what it answers, its data sources, and sample output fields.

## Report Modules

| Script | What it answers | Data sources |
|---|---|---|
| `cluster_deepdive.py` | One cluster: 3-month daily amortized cost trend + chart, actual vs amortized, cost per meter & node pool, spot/RI/SP split, **SKU change detection**, utilization, activity log | Cost Mgmt, ARG, Monitor, Activity Log |
| `architecture_design.py` | Actual-state design snapshot for one cluster, a resource group, cluster set, or full subscription; XLSX (incl. relationship map) plus Mermaid Markdown doc, editable draw.io diagram and self-contained HTML design view | ARG |
| `cluster_360.py` | `360`: every cluster from every subscription in one categorized workbook - joins inventory, version/EOL status, governance checks, amortized cost trend, utilization and node-image staleness; assigns each cluster a category (UPGRADE NOW, STOPPED BILLING, SECURITY GAP, IDLE CAPACITY, COST HOTSPOT, UPGRADE SOON, HYGIENE REVIEW, HEALTHY) and a 0-100 health score, with an ActionItems tab explaining every finding | ARG, AKS locations API, Cost Mgmt, Monitor |
| `fleet_inventory.py` | Every cluster detail: versions, tiers, node pools, networking, security, addons, tags | Resource Graph only |
| `fleet_cost.py` | Layered fleet cost report. Story tabs first: a `Scorecard` KPI-card one-pager (fleet spend, last full month + MoM, annualized run-rate, spot & RI/SP coverage, prod share, top-5 concentration, cost-spike count, commitment opportunity), a `CommitmentOpportunity` tab ranking clusters whose steady OnDemand spend is a reservation/savings-plan candidate (baseline = min OnDemand over full months × an assumed `--commit-discount`, with a verify-SKU caveat), and `SummaryByEnvironment`/`SummaryBySubscription` roll-ups (env carries a prod/non-prod tier + fleet share %). Then per-cluster monthly amortized trend, MoM %, spot share, RI/SP coverage, top movers, and fleet-wide SKU change signals | Cost Mgmt, ARG |
| `version_eol.py` | Out-of-support / LTS-only Kubernetes versions per region, node image staleness | ARG, AKS locations API |
| `container_os_eol.py` | EOL radar for container base images and runtimes (Alpine, Debian, UBI/RHEL, Java/Temurin, Python, Node.js): what is safe to build on, what is security-only, what to move to next | endoflife.date (no Azure access) |
| `aks_lifecycle.py` | AKS release calendar GA/EOL dates, managed add-ons, retirements/deprecations, GA and preview features, behavior changes, per-version component breaking changes | Microsoft Learn pages + Azure/AKS GitHub release notes (no Azure access) |
| `spot_cluster_report.py` | One spot workbook, layered: a `Scorecard` KPI one-pager (spot spend & share, adoption, candidate savings screen, prod-on-spot, HIGH findings), `Summary` with an OD-vs-spot bar chart, ranked `Candidates` (savings chart, per-pool `od_hr_source`, cluster risk band, verify caveat) and a `FleetCostTrend` monthly line chart; then spot/on-demand pool configuration, autoscaler profile, zones, taints, eviction/price settings (incl. a Deallocate eviction-policy check), pool/resource cost breakup and assessment. Candidate savings price the OD side at each cluster's **actual billed $/node-hour** (amortized cost / billed node-hours, so EA/MCA + RI/SP discounts are included) with retail list as fallback; the spot side stays retail (formerly `spot_opportunity.py`) | Cost Mgmt, ARG, public Retail Prices API |
| `spot_split_design.py` | `spot-design`: present vs future node-pool split design for team-dedicated clusters (Korea pattern) - team auto-detect from labels/taints (+`teams.csv` override), on-demand floor + paired spot pool sizing, ready-to-run `az aks nodepool add` commands, BU workload YAML (tolerations/affinity/spread/PDB), rollout plan, savings, Mermaid design doc convertible via `convert` | ARG, Retail Prices API |
| `spot_savings.py` | `spot-savings`: layered FinOps story for spot adoption. Story tabs first: a `Scorecard` KPI-card page separating invoiced Spot spend (billed fact) from estimated avoided cost (retail model), with coverage %, achieved-vs-achievable discount, untapped runway and prod-on-spot risk; a `BeforeAfterByEnv` management slide showing per-environment (BU) monthly cost before spot (priced all-on-demand) vs after (actual), the saving and saving %, with a side-by-side bar chart and a fleet total; a `Recommendations` tab ranking eligible on-demand pools to move to spot (sized by retail saving, each with a reliability band and a loud "verify workload suitability" caveat); a `CoverageRisk` tab of per-cluster spot exposure + an additive risk band plus a best-effort VMSS-churn eviction proxy; a `RealizedSavings` fact-vs-model table, and a `MonthlySavings` tab rolling the last 3 calendar months up per cluster (plus a fleet total) — spot spend, counterfactual, saving and savings rate per month, the current month marked month-to-date, and a `savings_from_spot_pool` Yes/No flag that is "Yes" only when that month actually carried Spot VMSS spend. Then `SpotTimeline` (actual vs counterfactual + cumulative saving), `TopSavers` standings and a `SavingsByEnv` prod/non-prod roll-up (environment inferred from the cluster/RG name: `-d-`/`-s-`/`-r-`/`-p-`/`-u-` -> dev/sit/dr/prod/uat), with `SavingsProjection`, before/after detail, `ActualVsProjection`, pool-level breakup and raw extracts in the appendix. Savings use an "avoided cost" method priced from **actual amortized billing**: each dollar of Spot VMSS spend is converted to billed node-hours (Cost Management `UsageQuantity`) and re-priced at the cluster's own actual On-Demand/RI effective rate — back-tracked from its pre-spot history (per VM size), falling back to the OD/RI pool it still runs, then to retail list price. `rate_basis`/the Scorecard's "Actual-rate basis" card show which was used, so the headline reflects negotiated/RI/Savings-Plan pricing rather than list price. The full fleet is kept by default; pass `--only-spot-clusters` to restrict to clusters with a current spot node pool, `--nonprod-spot` for the management shortcut "non-prod clusters that run spot" (= `--nonprod --only-spot-clusters`), or `--no-eviction-scan` to skip the Activity Log churn proxy | Cost Mgmt, ARG, public Retail Prices API, Activity Log |
| `spot_eviction.py` | Spot node eviction risk assessment: per-pool preemption frequency (healthresources VirtualMachinePreempted), VMSS-churn counter (Activity Log), spot_max_price (capacity-reclaim vs price-eviction) discriminator; **SkuAlternatives** — same-size in-region SKUs with a lower banded eviction rate (SpotResources) as swap candidates, optional forward-looking Spot Placement Score (`--placement-score`); Scorecard, per-pool RiskAssessment, EvictionSnapshot, ChurnTrend, RemediationGuide, SpotPoolInventory with per-pool HIGH/MED/LOW risk bands | Azure Resource Health, ARG (+ SpotResources eviction rates), Activity Log (node-RG VMSS delete/deallocate events), Retail Prices, opt-in Spot Placement Score API |
| `utilization_idle.py` | Node CPU/memory avg/p95/max per cluster, idle & stopped-but-billing clusters | ARG, Monitor platform metrics |
| `governance.py` | 17-check hygiene scorecard (private API, local accounts, kubenet, zones, autoscaler, tiers, ...) | Resource Graph only |
| `conformance.py` | `conformance`: fleet drift against a golden baseline YAML (same schema as the sandbox config; every key you set becomes a rule) - per-cluster scorecard, fail details, failures by rule | Resource Graph only |
| `policy_report.py` | Policy assignments incl. inherited, compliance per cluster, Kubernetes-policy **blind spots** (k8s policies assigned but addon off) | Policy/PolicyInsights, ARG |
| `policy_components.py` | `policy-components`: drill ONE compliance initiative (assignment) -> groups -> member policies to the individual **non-compliant components** (e.g. the failing Kubernetes namespace/kind/name), with resource-level fallback for policies that have no components; interactive selection or `--initiative/--group/--policy` flags (`--list` to discover) | Policy/PolicyInsights componentPolicyStates + policyStates, ARG |
| `network_ip_capacity.py` | Network model, API exposure, subnet IP pressure, Azure CNI pod IP demand, subnet NSG/route/NAT metadata | Resource Graph only |
| `tag_chargeback.py` | Required tag coverage, owner/cost-center/application gaps, tag value normalization, chargeback readiness | Resource Graph only |
| `optimization_report.py` | Prioritized cost-optimization queue combining amortized cost, utilization, spot/RI/SP signals, stopped-billing, **control-plane tier** and **off-hours stop** candidates | Cost Mgmt, ARG, Monitor |
| `cost_efficiency.py` | `efficiency`: ARG-cheap config-driven cost levers beyond spot - control-plane tier (Free/Standard/Premium), ephemeral OS disk conversion, SKU generation/family modernization (incl. ARM64), autoscaler & floor (min_count) hygiene, pool fragmentation; ranked recommendations with verify-before-move caveats | Resource Graph, Retail Prices API |
| `subscription_rearch.py` | `rearch`: ONE subscription, ALL resources (not just AKS) - orphan/idle disks, public IPs, NICs, empty load balancers, stopped-not-deallocated VMs, stale snapshots, empty App Service plans, app gateways with no backend targets, subnet-less NAT gateways, database-less SQL elastic pools, VNet-link-less private DNS zones, unassociated NSGs/route tables, empty availability sets and resource groups (orphan filters adapted from the MIT `dolevshor/azure-orphan-resources` ARG catalog), geo-redundant nonprod storage, flat-rate firewalls/gateways, premium SQL, plus Azure Advisor cost recs; findings carry actual last-month cost and an estimated monthly saving, and a companion `.md` narrative (current-state per RG + Mermaid, findings by category, target-state moves) drives a re-architecture-for-cost-savings exercise | Resource Graph, Cost Mgmt, Azure Advisor, Retail Prices API |
| `vulnerability_report.py` | Prisma XLSX/CVE-list enrichment and base-image/application/platform classification with remediation guidance | Prisma XLSX, classification rules, NVD/CISA KEV/EPSS |

## Report Field Examples

These examples use the offline smoke-test data. Your dates, costs, cluster
names, and subscription names will differ, but the field shapes are the same.

Common fields used across reports:

| Field | Meaning |
|---|---|
| `cluster` | AKS managed cluster name. |
| `subscription` / `subscription_id` | Friendly subscription name from `subscriptions.csv` or Azure, plus the GUID where shown. |
| `environment` | Resolved environment from cluster tag, resource-group tag, or name inference. |
| `environment_source` | Where the environment came from, for example `cluster_tag:environment`, `resource_group_tag:env`, or `name`. |
| `location` | Azure region of the cluster or network resource. |
| `resource_group` | Resource group that owns the AKS managed-cluster resource. |
| `node_resource_group` | AKS-managed `MC_*` resource group where VMSS/node resources live. |
| `pool` | AKS agent pool name. |
| `mode` | `System` or `User` node-pool mode. |
| `priority` | `Regular` or `Spot` node-pool priority. |
| `count`, `nodes`, `current_nodes` | Current node count. |
| `max_nodes` | Autoscaler max node count, or current count when autoscaler is off. |
| `PricingModel` | Cost category such as `OnDemand`, `Spot`, `Reservation`, or `SavingsPlan`. |
| `Period` / `Month` | Daily or monthly cost period. Current month is month-to-date. |
| `* %` fields | Excel percentage fields, usually formulas in the workbook. |

### Architecture Design Report

Command: `uv run python aks_report.py design --cluster aks-dev-01 --all`

Sheets created: `Summary`, `Clusters`, `NodePools`, `Network`,
`Subnets`, `Resources`, `ResourceCounts`, `Components`, `Diagrams`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Summary` | `Item, Value` | `Azure resources in design scope, 18` | High-level counts for selected subscriptions, clusters, node pools, resources, and generated document mode. |
| `Clusters` | `cluster, subscription, environment, location, resource_group, node_resource_group, kubernetes_version, sku_tier, node_pools, total_nodes, identity_type, addon_azure_policy, private_cluster` | `aks-dev-01, contoso-platform, dev, eastus, rg-apps-dev, MC_rg-apps-dev_aks-dev-01_eastus, 1.29.4, Free, 3, 7, SystemAssigned, false, false` | Cluster-level design facts for the selected scope. |
| `NodePools` | `cluster, pool, mode, vm_size, priority, count, autoscaling, min_count, max_count, os_sku, zones` | `aks-dev-01, sys, System, Standard_D4s_v3, Regular, 2, false, blank, blank, Ubuntu, blank` | Per-pool compute settings including autoscaler bounds, OS, zones, and spot usage. |
| `Network` | `cluster, network_plugin, network_plugin_mode, network_policy, outbound_type, load_balancer_sku, private_cluster, authorized_ip_ranges, public_fqdn, private_fqdn` | `aks-dev-01, kubenet, blank, blank, loadBalancer, standard, false, 0, aksdev01.hcp.eastus.azmk8s.io, blank` | Network and API-server design state visible from ARM. |
| `Subnets` | `subnet_id, subscription, resource_group, vnet, subnet, prefixes, referenced_by_aks, nsg_id, route_table_id, nat_gateway_id` | `<subnetId>, contoso-platform, rg-network, vnet-dev, aks-dev-nodes, 10.10.1.0/28, true, <nsgId>, blank, blank` | Referenced AKS subnets plus network controls. |
| `Resources` | `subscription, resourceGroup, name, type, component_class, location, sku_name, sku_tier, provisioning_state, id` | `contoso-platform, MC_rg-apps-dev_aks-dev-01_eastus, kubernetes, microsoft.network/loadbalancers, Load balancer, eastus, standard, blank, Succeeded, <resourceId>` | Azure resources in the design scope. |
| `ResourceCounts` | `subscription, resourceGroup, component_class, type, count` | `contoso-platform, MC_rg-apps-dev_aks-dev-01_eastus, Load balancer, microsoft.network/loadbalancers, 1` | Resource-type rollup by resource group. |
| `Components` | `cluster, component, name, resource_group, type, sku_or_size, state, details` | `aks-dev-01, Node pool, sys, MC_rg-apps-dev_aks-dev-01_eastus, System, Standard_D4s_v3, Running, nodes=2; autoscaling=false` | Human-readable design components that connect cluster, pools, API, addons, and nearby Azure resources. |
| `Relationships` | `source_type, source, relation, target_type, target, details` | `node pool, aks-dev-01/sys, nodes in, subnet, vnet-dev/aks-dev-nodes, 10.10.1.0/28` | Every relationship the design can see: containment, node pools, subnet usage, vnet membership, subnet attachments (NSG/route table/NAT gateway), co-located resources. |
| `Diagrams` | `cluster, diagram` | `aks-dev-01, flowchart LR ...` | Mermaid diagram source used in the Markdown design document. |

### Inventory Report

Command: `uv run python aks_report.py inventory --all`

Sheets created: `Clusters`, `NodePools`, `NetworkSecurity`, `Addons`, `Tags`,
`Summary`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Clusters` | `cluster, subscription, environment, location, kubernetes_version, sku_tier, node_pools, total_nodes, vm_sizes, private_cluster` | `aks-dev-01, contoso-platform, dev, eastus, 1.29.4, Free, 3, 7, Standard_D4as_v4; Standard_D4s_v3, false` | High-level AKS inventory: version, tier, node-pool count, total node count, VM families, and whether the API server is private. |
| `NodePools` | `cluster, pool, mode, vm_size, priority, count, autoscaling, min_count, max_count, os_sku, zones` | `aks-dev-01, sys, System, Standard_D4s_v3, Regular, 2, false, blank, blank, Ubuntu, blank` | Per-pool compute settings including autoscaler bounds, OS, zones, and spot usage. |
| `NetworkSecurity` | `cluster, network_plugin, network_policy, outbound_type, private_cluster, authorized_ip_ranges, rbac_enabled, aad_managed, local_accounts_disabled` | `aks-dev-01, kubenet, blank, loadBalancer, false, 0, true, false, false` | API exposure, network model, and identity/security settings visible from ARM. |
| `Addons` | `cluster, addon_monitoring, addon_azure_policy, addon_keyvault_csi, addon_appgw_ingress, addon_virtual_node` | `aks-dev-01, false, false, false, false, false` | Whether common AKS addons are enabled. |
| `Tags` | `cluster, subscription, tag, value` | `aks-dev-01, contoso-platform, environment, dev` | Raw cluster tags used for ownership, environment, and chargeback. |

### Cluster Deep Dive Report

Command: `uv run python aks_report.py deepdive --cluster aks-dev-01 --all`

Sheets created: `Summary`, `DailyCost`, `CostByMeter`, `CostByNodePool`,
`AmortizedVsActual`, `SKUChanges`, `NodePools`, `Utilization`, `ActivityLog`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `DailyCost` | `Period, OnDemand, Reservation, Spot, Total (USD)` | `2026-03-05, 195, 36, 24, =SUM(B2:D2)` | Daily cost split by pricing model for the selected cluster. |
| `CostByMeter` | `Meter, 2026-03, 2026-04, 2026-05, 2026-06, Total (USD)` | `D4s v3, 260, 265, 270, 90, =SUM(B2:E2)` | Monthly cost by Azure meter/SKU, useful for SKU drift. |
| `CostByNodePool` | `pool, 2026-03, 2026-04, 2026-05, 2026-06, Total (USD)` | `wrk, 260, 265, 270, 90, =SUM(B2:E2)` | Monthly cost mapped back to AKS node-pool names. |
| `AmortizedVsActual` | `Month, Amortized (USD), Actual (USD), Delta (USD)` | `2026-03, 425, 365, =B2-C2` | Compares true amortized cost against billed actual cost. |
| `SKUChanges` | `kind, name, status, first_month_usd, last_month_usd, note` | `Meter/SKU, D8s v5, NEW, 110, 35, first significant cost in 2026-05` | Flags meters or pool SKUs that appeared, disappeared, grew, or shrank. |
| `Utilization` | `Date, CPU avg %, CPU max %, Mem avg %, Mem max %` | `2026-06-07, 4.75, 14.75, 13.75, 23.75` | Daily platform metrics for the selected cluster. |
| `ActivityLog` | `timestamp, operation, status, caller, resource, resource_id` | `2026-05-20T10:00:00Z, agentPools/write, Succeeded, ops@contoso.com, wrk, <agentPoolId>` | Recent control-plane write operations that may explain cost or SKU changes. |

### Fleet Cost Report

Command: `uv run python aks_report.py cost --all`

Sheets created: `ClusterCosts`, `PricingModelSplit`, `TopMovers`,
`MeterChanges`, `SummaryBySubscription`, `RawMonthly`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `ClusterCosts` | `cluster, subscription, environment, location, 2026-03, 2026-04, 2026-05, 2026-06, Window total (USD), MoM %, Spot (USD), RI+SP (USD), Spot %, Cluster fee (USD), Amortized-Actual (USD)` | `aks-prod-01, contoso-shared, prod, westeurope, 1030, 1051, 1533, 477, formula, formula, 105, 666, formula, 243, 666` | Per-cluster monthly trend, total, month-over-month change, spot spend, reservation/savings-plan allocation, managed-cluster fee, and amortized-vs-actual delta. |
| `PricingModelSplit` | `cluster, subscription, environment, OnDemand, Spot, Reservation, Total (USD), Spot %` | `aks-dev-01, contoso-platform, dev, 1014, 141, 200, formula, formula` | Shows whether spend is regular, spot, or covered by commitments. |
| `TopMovers` | `cluster, subscription, environment, previous_month, last_full_month, Delta (USD), Delta %` | `aks-prod-01, contoso-shared, prod, 1051, 1533, formula, formula` | Clusters with the biggest month-over-month cost movement. |
| `MeterChanges` | `cluster, meter, status, first_active_month, last_active_month, first_usd, last_usd` | `aks-dev-01, D2s v3, REMOVED, 2026-03, 2026-03, 100, 100` | Detects SKU/meter changes across the fleet. |
| `RawMonthly` | `cluster, subscription, environment, Month, PricingModel, Amortized (USD)` | `aks-dev-01, contoso-platform, dev, 2026-03, OnDemand, 325` | Raw monthly cost rows used to build the summary tabs. |

### Version And EOL Report

Command: `uv run python aks_report.py version --all`

Sheets created: `VersionStatus`, `NodeImageAge`, `SupportedVersions`,
`Summary`, `SummaryByEnv`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `VersionStatus` | `cluster, subscription, environment, location, control_plane_version, minor, status, upgrade_channel, node_os_channel, pool_version_drift, note, power_state, sku_tier` | `aks-dev-01, contoso-platform, dev, eastus, 1.29.4, 1.29, OUT OF SUPPORT, (none), (none), blank, minor 1.29 is not in the supported list for eastus, Running, Free` | Control-plane support status against AKS-supported versions in that region. |
| `NodeImageAge` | `cluster, subscription, environment, pool, node_image_version, image_date, age_days, status` | `aks-dev-01, contoso-platform, dev, sys, AKSUbuntu-2204..., 2026-01-07, 154, STALE` | Node image freshness by pool. |
| `SupportedVersions` | `region, minor, support_plans, is_preview, is_default, patches` | `eastus, 1.30, AKSLongTermSupport, false, false, 1.30.9` | Region-specific AKS versions returned by Azure. |

### Container & OS EOL Radar

Command: `uv run python aks_report.py container-eol`

Sheets created: `Summary`, `EolRadar`, `OsBaseImages`, `LanguageRuntimes`,
`RawLifecycle`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Summary` | `product, group, container_image, recommended_target, supported, security_only, eol, cycles_tracked, next_eol_cycle, next_eol_date, next_eol_days, lifecycle_note` | `Python, Language runtime, python / python-slim, 3.14 (latest 3.14.6), 3, 2, 12, 17, 3.10, 2026-10-31, 142, ~5 years per minor; ...` | One row per product: what to build on next and which version falls off support first. |
| `EolRadar` | `product, group, cycle, latest_patch, status, security_support_until, days_to_eol, active_support_until, recommended_target, container_image` | `Alpine Linux, OS base image, 3.21, 3.21.7, EOL <180 DAYS, 2026-11-01, 143, blank, 3.24 (latest 3.24.0), alpine` | All live versions across all products sorted by soonest EOL; recently dead versions stay visible for 180 days. |
| `OsBaseImages` / `LanguageRuntimes` | `product, group, cycle, codename, latest_patch, released, lts, active_support_until, security_support_until, extended_support, days_to_eol, status, recommended_target, container_image` | `Debian, OS base image, 12, Bookworm, 12.14, 2023-06-10, blank, 2026-06-10, 2026-06-10, 2028-06-30, -1, EOL, 13 (latest 13.5), debian / debian-slim` | Full lifecycle table per group; status is EOL / EOL <90 DAYS / EOL <180 DAYS / SECURITY ONLY / SUPPORTED. |

### AKS Lifecycle & Release Radar

Command: `uv run python aks_report.py aks-lifecycle`

Sheets created: `Summary`, `ReleaseCalendar`, `Announcements`, `GAFeatures`,
`PreviewFeatures`, `BehaviorChanges`, `Addons`, `OpenSourceIntegrations`,
`BreakingChanges`, `ComponentUpdates`, `RawReleaseNotes`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `ReleaseCalendar` | `kubernetes_version, support_track, upstream_release, aks_preview, aks_ga, end_of_life, lts_or_platform_support_until, days_to_final_eol, status` | `1.32, Community, Dec 2024, Feb 2025, Apr 2025, Mar 2026, Until 1.36 GA, -72, EOL` | AKS GA/EOL dates per Kubernetes minor for the community and LTS tracks. |
| `Announcements` | `release, published, kind, item, link` | `2026-05-29, 2026-06-04, RETIREMENT, Windows Server Annual Channel for Containers retired on AKS..., https://learn.microsoft.com/...` | Release-note announcements classified as RETIREMENT / DEPRECATION / GA / PREVIEW / NOTICE. |
| `GAFeatures` | `release, published, item, link` | `2026-05-29, 2026-06-04, Customized OS disk size ... is now Generally Available, https://...` | Features that went GA in the scanned window; `PreviewFeatures` and `BehaviorChanges` share the shape. |
| `Addons` | `addon, description, docs, docs_url, github_url` | `keda, Use event-driven autoscaling..., Simplified application autoscaling..., https://learn.microsoft.com/..., https://github.com/...` | Managed add-ons documented on the AKS integrations page. |
| `BreakingChanges` | `kubernetes_version, managed_addons, aks_components_ccp, os_components, breaking_changes, link` | `1.34, aci-connector-linux 1.6.2 ..., addon-override-manager ..., Linux - Ubuntu 22.04 ..., kube-egress-gateway-daemon v0.0.21 -> v0.0.22, https://...` | Per-version component matrix and breaking changes from the supported-versions page. |

### Spot Report

Command: `uv run python aks_report.py spot --subs contoso-platform --env dev`

Sheets created: `Summary`, `SpotNodePools`, `OnDemandNodePools`,
`NodePoolSkuSummary`, `AutoscalerConfig`, `SpotAssessment`, `Candidates`,
`CostByCluster`, `CostTrend`, `CostByNodePool`, `OtherCostItems`,
`CostByMeter`, `PriceReference`, `RawResourceCost`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Summary` | `cluster, subscription, environment, has_spot, spot_pools, spot_nodes, on_demand_pools, on_demand_nodes, system_on_demand, spot_vm_sizes, on_demand_vm_sizes, spot_multi_zone, spot_multi_vm_family, spot_max_nodes, cluster_max_nodes, autoscaler_expander, OnDemand, Spot, Reservation, SavingsPlan, Cluster fee, Total (USD), Spot %` | `aks-dev-01, contoso-platform, dev, true, 1, 2, 2, 5, true, Standard_D4as_v4, Standard_D4s_v3, true, false, 2, 12, priority, 1014, 141, 200, 0, 0, 1355, 10.4%` | One-row cluster view of spot/on-demand shape, capacity caps, autoscaler signal, and cost split. |
| `Candidates` | `cluster, subscription, environment, location, pool, vm_size, nodes, autoscaling, taints, od_hr, spot_hr, Spot discount %, Est monthly OD cost, Est monthly saving` | `aks-dev-01, contoso-platform, dev, eastus, wrk, Standard_D4s_v3, 3, true, blank, 0.192, 0.041, formula, formula, formula` | User-mode regular pools that may be spot candidates, with retail-price savings estimates (skipped with `--no-retail-prices`). |
| `PriceReference` | `region, vm_size, od_hr, spot_hr, discount %` | `eastus, Standard_D4as_v4, 0.192, 0.041, formula` | Retail hourly prices used by the candidate estimate. |
| `SpotNodePools` | `cluster, pool, mode, priority, vm_size, vm_family, nodes, autoscaling, min_count, max_count, effective_min_nodes, effective_max_nodes, zones, zones_count, eviction_policy, spot_max_price, spot_price_mode, taints, spot_taint_present, expected_spot_taint` | `aks-dev-01, spt, User, Spot, Standard_D4as_v4, d, 2, false, blank, blank, 2, 2, blank, 0, Delete, -1, pay_up_to_on_demand, kubernetes.azure.com/scalesetpriority=spot:NoSchedule, true, kubernetes.azure.com/scalesetpriority=spot:NoSchedule` | Every spot pool with SKU, mode, node count, autoscaling bounds, zones, eviction policy, price cap, and taint visibility. |
| `OnDemandNodePools` | `cluster, pool, mode, priority, vm_size, nodes, autoscaling, min_count, max_count, effective_max_nodes, zones_count, os_sku, power_state` | `aks-dev-01, sys, System, Regular, Standard_D4s_v3, 2, false, blank, blank, 2, 0, Ubuntu, Running` | Regular pools that provide system and fallback capacity. |
| `NodePoolSkuSummary` | `cluster, priority, mode, vm_size, vm_family, node_pools, current_nodes, effective_min_nodes, effective_max_nodes, zones_count_max, pools` | `aks-dev-01, Spot, User, Standard_D4as_v4, d, 1, 2, 2, 2, 0, spt` | Capacity by SKU/family, priority, and pool mode. |
| `AutoscalerConfig` | `cluster, spot_pools, autoscaling_pools, expander, balance_similar_node_groups, scan_interval, scale_down_unneeded_time, autoscaled_spot_pools, cluster_max_nodes, spot_max_nodes, on_demand_max_nodes` | `aks-dev-01, 1, 1, priority, true, 10s, 10m, 0, 12, 2, 10` | Cluster autoscaler profile and effective max capacity split by spot/on-demand. |
| `SpotAssessment` | `cluster, subscription, environment, severity, check, result, evidence, recommendation` | `aks-dev-01, contoso-platform, dev, WARN, spot_multi_vm_family, WARN, spot VM families=d, Use multiple VM families/SKUs to reduce spot capacity concentration.` | Independent review findings for prod spot, system fallback, zones, VM families, caps, autoscaling, taints, and autoscaler settings. |
| `CostByCluster` | `cluster, subscription, environment, OnDemand, Spot, Reservation, SavingsPlan, Cluster fee, Total (USD), Spot %` | `aks-dev-01, contoso-platform, dev, 1014, 141, 200, 0, 0, 1355, 10.4%` | Window-level amortized cost split by pricing model plus managed-cluster fee. |
| `CostTrend` | `cluster, subscription, environment, Month, OnDemand, Spot, Reservation, SavingsPlan, Cluster fee, Total (USD), Spot %` | `aks-dev-01, contoso-platform, dev, 2026-03, 325, 40, 60, 0, 0, 425, 9.4%` | Monthly trend for spot/on-demand/RI/SP cost. |
| `CostByNodePool` | `cluster, pool, priority, mode, vm_size, nodes, autoscaling, effective_max_nodes, window_cost, months, resource_count` | `aks-dev-01, spt, Spot, User, Standard_D4as_v4, 2, false, 2, 141, 2026-03; 2026-04; 2026-05; 2026-06, 1` | Cost inferred from VMSS resource IDs and joined back to node-pool config. |
| `OtherCostItems` | `cluster, category, resource_name, resource_id, window_cost, months` | `aks-dev-01, managed_disks, agentdisks, <diskId>, 84, 2026-03; 2026-04; 2026-05; 2026-06` | Non-node-pool costs in the node resource group plus managed-cluster fee. |
| `CostByMeter` | `cluster, meter, window_cost, months` | `aks-dev-01, Standard HDD Managed Disks, 84, 2026-03; 2026-04; 2026-05; 2026-06` | Meter-level cost used to spot disk/LB/IP or SKU-related spend. |

### Spot Split Design Report

Command: `uv run python aks_report.py spot-design --cluster aks-dev-01`

Sheets created: `CurrentState`, `TeamMapping`, `FutureStatePools`,
`AzCommands`, `WorkloadChanges`, `Savings`, `NotSplit`, `ClusterPrereqs`,
`SpotAssessment`, `RolloutPlan`, `Risks`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `CurrentState` | `pool, mode, priority, vm_size, current_nodes, max_count, team, labels, taints` | `paypool, User, Regular, Standard_D8s_v3, 4, 6, payments, team=payments, dedicated=payments:NoSchedule` | Current team-dedicated pool shape inferred from ARM node labels, taints, names, and optional `teams.csv`. |
| `FutureStatePools` | `team, od_pool, spot_pool, vm_size, current_nodes, od_keep_nodes, spot_initial_nodes, spot_max` | `payments, paypool, paypoolsp, Standard_D8s_v3, 4, 1, 3, 6` | Proposed on-demand floor and paired spot pool sizing. |
| `AzCommands` | `order, phase, team, pool, command` | `1, pilot/expand, payments, paypoolsp, az aks nodepool add ... --priority Spot` | Platform-team commands to add spot pools and later shrink on-demand pools. |
| `WorkloadChanges` | `team, applies_to, yaml` | `payments, deployments moving to spot, tolerations/affinity/spread/PDB YAML` | BU-owned Kubernetes changes needed to prefer spot safely. |
| `Savings` | `team, vm_size, nodes_moved, od_hr, spot_hr, discount %, est monthly saving (USD)` | `payments, Standard_D8s_v3, 3, 0.48, 0.10, 79%, 832` | Public retail-price estimate for screening the split design. |

### Utilization And Idle Report

Command: `uv run python aks_report.py utilization --all --days 14`

Sheets created: `Utilization`, `IdleCandidates`, `Stopped`, `Summary`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Utilization` | `cluster, subscription, environment, location, power_state, nodes, vm_sizes, allocatable_cores_avg, cpu_avg %, cpu_p95 %, cpu_max %, mem_avg %, mem_p95 %, mem_max %, samples, flag` | `aks-dev-01, contoso-platform, dev, eastus, Running, 7, Standard_D4as_v4; Standard_D4s_v3, 30, 5, 7, 17, 14, 16, 26, 72, IDLE` | Platform CPU/memory metrics, sample count, and an idle/OK flag. |
| `IdleCandidates` | same as `Utilization` | `aks-dev-01, contoso-platform, dev, eastus, Running, 7, ..., IDLE` | Subset of clusters with low utilization. |
| `Stopped` | same as `Utilization` | `aks-dev-02, contoso-platform, dev, eastus2, Stopped, 7, ..., samples=0, STOPPED` | Stopped clusters that may still have attached billing resources. |

### Governance Report

Command: `uv run python aks_report.py governance --all`

Sheets created: `Scorecard`, `FailDetails`, `FailuresByCheck`, `CheckLegend`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Scorecard` | `cluster, subscription, environment, location, api_server_locked_down, local_accounts_disabled, aad_integration, rbac_enabled, managed_identity, paid_tier_for_prod, no_spot_in_prod, autoscaler_on_user_pools, multi_zone, network_policy_set, not_kubenet, monitoring_addon, azure_policy_addon, auto_upgrade_channel, node_os_channel, env_tagged, workload_identity, Score` | `aks-dev-01, contoso-platform, dev, eastus, FAIL, FAIL, FAIL, PASS, PASS, N-A, N-A, FAIL, FAIL, FAIL, FAIL, FAIL, FAIL, FAIL, FAIL, PASS, FAIL, formula` | PASS/FAIL/N-A hygiene checks and overall score. |
| `FailDetails` | `cluster, subscription, environment, check, description, detail` | `aks-dev-01, contoso-platform, dev, api_server_locked_down, Private cluster or authorized IP ranges on the API server, API server is reachable from any internet IP` | Human-readable reason for every failed check. |
| `FailuresByCheck` | `check, failing_clusters` | `azure_policy_addon, 2` | Fleet-wide count of failures by control. |
| `CheckLegend` | `check, description` | `api_server_locked_down, Private cluster or authorized IP ranges on the API server` | Meaning of each governance check. |

### Azure Policy Report

Command: `uv run python aks_report.py policy --all`

Sheets created: `Assignments`, `ClusterCompliance`, `NonCompliantDetail`,
`KubernetesBlindSpots`, `Summary`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Assignments` | `subscription, assignment, scope, inherited, enforcement, definition, category, is_initiative` | `contoso-platform, K8s pod security baseline, /providers/Microsoft.Management/managementGroups/corp, true, Default, Kubernetes cluster pod security baseline, Kubernetes, true` | Policy or initiative assignments visible at the subscription, including inherited management-group assignments. |
| `ClusterCompliance` | `cluster, subscription, environment, Compliant, NonCompliant, NonCompliant %` | `aks-dev-01, contoso-platform, dev, 0, 1, formula` | Latest PolicyInsights compliance counts by cluster. |
| `NonCompliantDetail` | `cluster, subscription, environment, policy, category, assignment, compliance, action, reference_id` | `aks-dev-01, contoso-platform, dev, Audit HTTPS ingress in AKS, Kubernetes, tls, NonCompliant, audit, blank` | Individual non-compliant policy states for AKS clusters. |
| `KubernetesBlindSpots` | `cluster, subscription, environment, policy_addon_enabled, k8s_policies_assigned_in_sub, status` | `aks-dev-01, contoso-platform, dev, false, true, BLIND SPOT` | Flags clusters where Kubernetes-category policies are assigned but the Azure Policy addon is off. |

### Network And IP Capacity Report

Command: `uv run python aks_report.py network --all`

Sheets created: `ClusterNetwork`, `SubnetCapacity`, `PoolSubnetUse`, `Issues`,
`Summary`, `SummaryByModel`, `SummaryBySubnetStatus`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `ClusterNetwork` | `cluster, subscription, environment, location, network_model, network_plugin, network_plugin_mode, network_policy, outbound_type, lb_sku, private_cluster, authorized_ip_ranges, current_nodes, max_nodes, pools, node_subnets, pod_subnets` | `aks-dev-01, contoso-platform, dev, eastus, kubenet, kubenet, blank, blank, loadBalancer, standard, false, 0, 7, 9, 3, 1, 0` | Cluster-level networking model, API exposure, and subnet count. |
| `SubnetCapacity` | `subnet_id, subscription, resource_group, vnet, subnet_name, location, roles, prefixes, usable_ipv4, current_ips_needed, max_ips_needed, current_utilization, max_utilization, clusters, pools, nsg_attached, route_table_attached, nat_gateway_attached, status, note` | `<subnetId>, contoso-shared, rg-network-prod, vnet-prod, aks-prod-nodes, westeurope, node; pod, 10.30.1.0/24, 251, 1221, 1221, 4.86, 4.86, 1, 3, true, true, false, CRITICAL, blank` | Subnet IP capacity and network controls such as NSG, route table, and NAT gateway. |
| `PoolSubnetUse` | `cluster, pool, mode, priority, network_model, vm_size, current_nodes, max_nodes, max_pods, node_subnet_id, pod_subnet_id, node_ips_current, node_ips_at_max, pod_ips_current, pod_ips_at_max, warning` | `aks-dev-01, sys, System, Regular, kubenet, Standard_D4s_v3, 2, 2, 110, <subnetId>, blank, 2, 2, 0, 0, kubenet cluster; plan Azure CNI migration` | IP demand at node-pool level. |
| `Issues` | `cluster, subscription, environment, object, severity, issue` | `aks-dev-01, contoso-platform, dev, sys, WARN, kubenet cluster; plan Azure CNI migration before retirement pressure` | Actionable network/IP warnings. |

### Tags And Chargeback Report

Command: `uv run python aks_report.py tags --all`

Sheets created: `TagMatrix`, `MissingTags`, `TagCoverage`, `TagValues`,
`RawTags`, `Summary`, `SummaryBySubscription`, `SummaryByEnvironment`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `TagMatrix` | `cluster, subscription, environment, environment_source, location, resource_group, owner, owner_source, costcenter, costcenter_source, application, application_source, missing_required_tags, missing_tag_list, chargeback_status` | `aks-dev-01, contoso-platform, dev, cluster, eastus, rg-apps-dev, blank, blank, blank, blank, blank, blank, 3, owner; costcenter; application, PARTIAL` | One row per cluster showing required tag values, their source, and chargeback readiness. |
| `MissingTags` | `cluster, subscription, environment, location, resource_group, missing_tag, impact` | `aks-dev-01, contoso-platform, dev, eastus, rg-apps-dev, owner, cost allocation blind spot` | Missing required tag findings. |
| `TagCoverage` | `tag, clusters_present, clusters_missing, coverage, from_cluster_tag, from_resource_group_tag, from_resolved_env, from_name` | `owner, 0, 3, 0, 0, 0, 0, 0` | Coverage by required tag and source type. |
| `TagValues` | `tag, value, source, clusters, subscriptions` | `environment, dev, cluster, 1, 1` | Distinct tag values and how widely they appear. |
| `RawTags` | `cluster, subscription, environment, scope, tag, value` | `aks-dev-01, contoso-platform, dev, cluster, environment, dev` | Raw tag rows from cluster and resource-group scopes. |

### Optimization Report

Command: `uv run python aks_report.py optimization --all --days 14`

Sheets created: `Summary`, `SavingsCandidates`,
`ClusterCostUtilization`, `PricingModelSplit`, `RawMonthly`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `SavingsCandidates` | `cluster, subscription, environment, candidate, priority, avg_monthly_cost, estimated_monthly_saving, reason` | `aks-dev-02, contoso-platform, dev, STOPPED_BILLING, HIGH, 173, 173, Cluster is stopped but recent amortized cost still exists` | Prioritized savings queue with estimated monthly impact. |
| `ClusterCostUtilization` | `cluster, subscription, environment, location, power_state, nodes, max_nodes, spot_nodes, regular_user_nodes, avg_monthly_cost, last_full_month_cost, window_total, MoM %, Spot %, RI+SP %, utilization_flag, cpu_avg %, cpu_p95 %, mem_avg %, mem_p95 %, samples, optimization_flags` | `aks-prod-01, contoso-shared, prod, westeurope, Running, 11, 11, 2, 6, 1277.67, 1606, 4334, 42.88%, 2.57%, 16.28%, OK, 47, 49, 64, 66, 72, blank` | Combined cost, utilization, and optimization signals by cluster. |
| `PricingModelSplit` | `cluster, subscription, environment, cluster_id, OnDemand, Spot, Reservation, Total` | `aks-dev-01, contoso-platform, dev, <clusterId>, 1014, 141, 200, formula` | Pricing model mix used to find spot or commitment opportunities. |
| `RawMonthly` | `cluster_id, cluster, subscription, environment, Month, PricingModel, Amortized node RG cost, Cluster fee` | `<clusterId>, aks-dev-01, contoso-platform, dev, 2026-03, OnDemand, 325, 0` | Raw cost inputs for the optimization calculations. |

Candidate types include `STOPPED_BILLING`, `RIGHTSIZE_OR_SCALE_DOWN`, `SPOT_REVIEW`,
`RI_SP_COMMITMENT_REVIEW`, `COST_SPIKE`, plus the two config-driven types
`CONTROL_PLANE_TIER` (non-prod on Standard/Premium - downgrade to Free) and
`OFFHOURS_STOP_CANDIDATE` (non-prod, running, off-hours stop schedule). New
flags: `--no-tier-candidates` and `--offhours-pct` (default 0.65).

### Cost Efficiency Report (Beyond Spot)

Command: `uv run python aks_report.py efficiency --all`

An ARG-cheap companion to `optimization`: the config-driven cost levers that
need little or no Cost Management traffic because the saving signal is a
flattened config field (`sku_tier`, `os_disk_type`, `vm_size`, autoscaler
profile, pool counts). Sheets: `Scorecard`, `ControlPlaneTier`,
`EphemeralOSDisk`, `SKUModernization`, `AutoscalerHygiene`,
`PoolFragmentation`, `Recommendations`, `NodePools`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `ControlPlaneTier` | `cluster, subscription, environment, location, sku_tier, est_monthly_tier_cost, status, est_monthly_saving, verify_before_move` | `aks-dev-02, contoso-platform, dev, eastus2, Standard, 8.76, DOWNGRADE TO FREE, 8.76, Premium/LTS may be a deliberate support choice` | Control-plane fee by `sku.tier`; non-prod paid-tier clusters are downgrade candidates. |
| `EphemeralOSDisk` | `cluster, subscription, environment, pool, mode, vm_size, os_disk_type, os_disk_gb, nodes, est_monthly_saving, action, verify_before_move` | `aks-dev-01, contoso-platform, dev, wrk, User, Standard_D4s_v3, Managed, 128, 3, 27, CREATE NEW POOL (ephemeral) + MIGRATE, Ephemeral is immutable on an existing pool` | Pools on managed OS disks that could move to free ephemeral (cache/temp >= OS disk). |
| `SKUModernization` | `cluster, pool, current_sku, recommended_sku, new_generation, nodes, current_od_hr, new_od_hr, est_pct_off, est_monthly_saving, verify_before_move` | `aks-dev-01, wrk, Standard_D4s_v3, Standard_D4s_v5, x64 newer-gen, 3, 0.192, 0.154, 0.21, 83, Verify regional quota, SKU availability` | Cheaper same-vCPU/mem newer-gen SKU from the Retail Prices API; ARM64 flagged separately. |
| `AutoscalerHygiene` | `cluster, pool, mode, count, autoscaling, min_count, max_count, autoscaler_expander, finding, verify_before_move` | `aks-dev-01, sys, System, 2, no, , , priority, SYSTEM_POOL_NO_AUTOSCALE, Advisory only` | Per-pool autoscaler / floor findings (no-autoscale, min_count == count, expander). |
| `PoolFragmentation` | `cluster, total_pools, user_pools, single_node_user_pools, distinct_user_skus, same_sku_mergeable, findings, recommendation` | `aks-prod-01, 3, 2, 0, 2, 0, OK, No fragmentation concerns.` | Consolidation opportunities (many tiny / single-node / mergeable pools). |
| `Recommendations` | `rank, lever, cluster, subscription, environment, est_monthly_saving_usd, verify_before_move` | `1, CONTROL_PLANE_TIER, aks-dev-02, contoso-platform, dev, 8.76, Premium/LTS may be a deliberate support choice` | Ranked $-impact actions (tier + ephemeral + SKU modernization). |
| `NodePools` | `cluster, subscription, environment, location, pool, mode, vm_size, priority, count, ...` | `aks-dev-01, contoso-platform, dev, eastus, sys, System, Standard_D4s_v3, Regular, 2` | Reference: the full pool inventory the levers were evaluated against. |

Pricing proxies are named constants (`TIER_HOURLY`,
`MANAGED_OS_DISK_USD_PER_NODE_MONTH`) and drift - verify current pricing. SKU
modernization uses the public Retail Prices API (no auth); pass
`--no-retail-prices` for a fully offline / ARG-only run.

### CVE / Prisma Vulnerability Report

Command: `uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules examples/vulnerability_classification.example.json`

Sheets created: `Summary`, `PrismaFindings`, `Classification`,
`Remediation`, `ByImage`, `ByPackage`, `ByLayer`, `CVEReference`,
`ClassificationRules`, `InputColumns`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Summary` | `Item, Value` | `application rows, 4` | Counts for CVEs, Prisma findings, classification layers, KEV hits, and loaded JSON classification rule files. |
| `BySeverity` | `severity, base_image, application, platform, unknown` | `Critical, 0, 1, 0, 0` | Severity × ownership-layer finding grid with a clustered bar chart; shows which layer carries the Critical/High load. |
| `PrismaFindings` | `sheet, row, finding_id, cve, compliance, result, severity, package, package_version, package_license, fixed_version, package_type, image, registry, repository, image_tag, hostname, distro, cvss, risk_factors, cause, image_id, vulnerability_link, purl` | `Vulnerabilities, 2, PRISMA-1, CVE-2026-1234, Vulnerability, fail, High, openssl, 3.0.1, OpenSSL, 3.0.8, OS Package, registry/app:1.0, registry, app, 1.0, host01, Ubuntu, 8.1, has fix, OS package, sha256:..., https://..., pkg:deb/ubuntu/openssl` | Normalized rows parsed from the Prisma XLSX export. Header names are matched flexibly. |
| `Classification` | `cve, package, package_type, image, layer, confidence, signal, needs_review, evidence, kev, cvss_score, cvss_severity` | `CVE-2026-1234, openssl, OS Package, registry/app:1.0, base_image, 0.90, pkgtype_os, , package type indicates an OS package in the container image, false, 8.1, HIGH` | Ownership layer for each finding plus the deciding `signal` token and a `needs_review` flag (unknown/low-confidence rows) so verdicts are auditable. |
| `Remediation` | `cve, layer, image, package, package_version, fixed_version, severity, kev, remediation` | `CVE-2026-1234, base_image, registry/app:1.0, openssl, 3.0.1, 3.0.8, High, false, Update the Dockerfile FROM image...` | Practical fix guidance, including Prisma fixed version and KEV action when available. |
| `ByImage` | `image, layer, findings, cves` | `registry/app:1.0, application, 3, 2` | Rollup by container image and classified layer. |
| `ByPackage` | `package, layer, findings, cves` | `openjdk-17-jre, platform, 1, 1` | Rollup by affected package/component and classified layer. |
| `ByLayer` | `layer, findings, cves` | `base_image, 5, 4` | Layer-level finding and distinct-CVE counts, with a findings-by-layer bar chart. |
| `CVEReference` | `cve, nvd_status, published, cvss_score, cvss_severity, cwe, cpe_parts, affected_products, kev, epss, description, references` | `CVE-2026-1234, Analyzed, 2026-01-15, 8.1, HIGH, CWE-78, a; o, debian:openssl, false, 0.12, summary, https://...` | Internet-enriched reference data from NVD/CISA KEV/EPSS, or sparse rows in `--offline` mode. |
| `ClassificationRules` | `file, type, name, layer, match` | `examples/vulnerability_classification.example.json, classification_rule, Java runtimes are platform, platform, {"package": ["openjdk"]}` | Optional local classification rules used for the run. These are not Azure Policy. |
| `InputColumns` | `source, columns` | `prisma.xlsx, CVE ID, Severity, Package Name, Package Type, Image` | Original Prisma headers detected so you can confirm parser alignment. |
