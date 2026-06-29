# AKS Reporting Toolkit — codebase map for Claude

This file is the codebase map. **Read it instead of re-exploring the repo, and
update it in the same change whenever you add/remove a module, tab, CLI flag,
shared helper, or convention.** Keep it accurate and terse; the README is the
user-facing doc, this is the contributor map.

## What this repo is

Python 3.12 toolkit (`uv` managed) that generates multi-tab styled `.xlsx`
reports about AKS clusters across many Azure subscriptions using only
subscription-level Reader access (no kubectl). Single entry point:
`uv run python aks_report.py <report> [scope flags]`. Built for ~25 subs /
~500 clusters; all rate limiting is handled in shared clients.

Exception to read-only: the `sandbox` command family targets the user's
full-access sandbox RG (one sub) and may write there - AND drive kubectl
against the sandbox cluster. Fleet access stays read-only even in sandbox
commands (clone/impact READ the fleet, WRITE only sandbox). Org constraint:
no Karpenter/NAP and no Cilium - spot/rebalancing designs use kube-scheduler +
cluster autoscaler + descheduler + topologySpreadConstraints only.

## Layout

```
aks_report.py        launcher: REPORTS list maps key/aliases -> module with main(argv)
azrep/               shared library
  http_client.py     AzureSession: bearer auth, 429 retry-after + QPU headers,
                     5xx backoff+jitter, optional pacing (connect(min_interval=)),
                     get_paged/post_paged. AzureApiError. log().
  arg.py             Resource Graph query() (paged, 1000-sub chunks) + KQL constants
                     (CLUSTERS_KQL, RG_TAGS_KQL, SUB_NAMES_KQL, SUBNETS_KQL)
  fleet.py           load_fleet(session, sel_subs, env_filter, include_unknown, env_keys)
                     -> (clusters, pools) flattened dicts; THE inventory entry point
  subs.py            subscriptions.csv loader, env resolution (cluster tag -> RG tag ->
                     name inference), scope picker/prompts, base_parser() common CLI,
                     out_path(), is_prod(), cluster filter globals
  costmgmt.py        CostClient: QPU-paced Cost Management query() -> DataFrame
                     (Cost, CostUSD, Period + groupings; with_quantity= adds a
                     UsageQuantity column = billed node-hours for VM/VMSS meters);
                     default_window(), dim_in(), f_and()
  armextras.py       cluster_metrics() (Monitor platform metrics), activity_events()
                     (ContainerService control-plane), vmss_churn_events() (node-RG
                     Microsoft.Compute VMSS delete/deallocate eviction proxy),
                     aks_supported_versions(), node_image_date(), retail_vm_prices()
  excel.py           new_workbook(), add_readme(), add_table() (sections: intro/summary/
                     detail/reference; fail/warn conditional formatting; money/pct/int),
                     add_scorecard() (merged-cell KPI cards w/ RAG fill, for exec
                     one-pagers), add_total_row(), add_line_chart(), add_bar_chart(),
                     add_grouped_bar_chart() (clustered multi-series, e.g. before/after), save()
  doc_export.py      markdown -> docx/pdf (`convert` command)
  drawio.py          .drawio diagram writer (used by architecture_design)
  sandbox.py         sandbox CLI front door: load_config(path, validate=) /
                     validate_config, require_yes + ensure_sandbox_safe gates,
                     ARM template deploy, policy apply/scan/cleanup,
                     wait_for_provisioning(); GATED_COMMANDS dispatch to sandbox_*
  kubectl.py         kubeconfig fetch (az aks get-credentials + kubelogin; ARM
                     listClusterUserCredential fallback) + subprocess kubectl
                     runner; module-level `_run` is the test seam
  sandbox_clone.py   `sandbox clone`: one fleet cluster (ARG) -> downsized sandbox YAML
  sandbox_k8s.py     `sandbox k8s-test`: Gatekeeper deny/allow/audit case harness
  sandbox_impact.py  `sandbox impact`: candidate staged DoNotEnforce in sandbox RG,
                     fleet ARM bodies (CLUSTERS_RAW_KQL) swept via
                     checkPolicyRestrictions -> impact XLSX
  sandbox_spot.py    `sandbox spot-sim`: new spot pool + OD shrink (plan_split reuse),
                     descheduler deploy, SCENARIOS 10-case workload matrix,
                     optional VMSS simulateEviction + rebalance watch
  sandbox_upgrade.py `sandbox upgrade-rehearsal`: hop_path(), REMOVED_APIS gate,
                     control-plane/pool upgrades + kubectl health gates
manifests/spot/descheduler.yaml  vendored pinned descheduler (Deployment; upstream has no DaemonSet)
policies/tests/      sample violating/compliant pod manifests for k8s-test
```

Sandbox CLI: `sandbox plan|deploy|policy-apply|scan|report|cleanup|kubeconfig|
kubectl|k8s-apply|k8s-delete|k8s-test|clone|impact|spot-sim|upgrade-rehearsal`.
Azure/cluster writes need `--yes` + sandbox-looking names; `clone` is read-only
(local YAML out), `kubeconfig`/`kubectl` need no `--yes`. Config schema extras:
`k8s_tests: {namespace, constraint_wait_seconds, cases: [{name, manifest,
expect: deny|allow|audit, constraint_contains?}]}`; pools accept
`node_taints`/`node_labels`.

## Report modules (each: module-level docstring, main(argv), wired in aks_report.REPORTS)

| key | module | data sources |
|---|---|---|
| 360 | cluster_360.py | ARG + AKS versions API + Cost Mgmt + Monitor; categorized estate view |
| inventory | fleet_inventory.py | ARG only |
| cost | fleet_cost.py | Cost Mgmt (sub-scope by node RG) + ARG; layered: Scorecard/CommitmentOpportunity/SummaryByEnvironment story tabs, then per-cluster detail |
| deepdive | cluster_deepdive.py | Cost Mgmt + ARG + Monitor + Activity Log (one cluster) |
| design | architecture_design.py | ARG; also writes .md (Mermaid) + .drawio + .html (self-contained, no JS) companions |
| version | version_eol.py | ARG + aks_supported_versions per region |
| spot | spot_cluster_report.py | Cost Mgmt + ARG + retail prices |
| spot-design | spot_split_design.py | ARG + retail prices |
| spot-savings | spot_savings.py | Cost Mgmt + ARG + retail prices + Activity Log (node-RG eviction proxy) |
| utilization | utilization_idle.py | ARG + Monitor (1 paced call/cluster) |
| governance | governance.py | ARG only; CHECKS list of (id, desc, fn(c, pools)->(status, detail)) |
| conformance | conformance.py | ARG only; rules built from a golden YAML (sandbox config schema, subset) via build_rules(); requires --golden |
| policy | policy_report.py | Policy/PolicyInsights + ARG |
| policy-components | policy_components.py | Policy assignments + policySetDefinition + PolicyInsights componentPolicyStates (+ policyStates fallback) + ARG; drills ONE initiative -> groups -> policies -> non-compliant components |
| network | network_ip_capacity.py | ARG only |
| tags | tag_chargeback.py | ARG only |
| optimization | optimization_report.py | Cost Mgmt + ARG + Monitor |
| rearch | subscription_rearch.py | ARG (all resources + advisorresources) + Cost Mgmt (per-ResourceId+ServiceName) + Advisor; ONE sub only; FINDINGS engine + .md narrative (Mermaid) |
| container-eol | container_os_eol.py | endoflife.date (no Azure) |
| aks-lifecycle | aks_lifecycle.py | MS Learn pages + GitHub releases (no Azure) |
| vulnerabilities | vulnerability_report.py | Prisma XLSX + NVD/KEV/EPSS (no Azure) |

`cluster_360.py` reuses across modules: `governance.CHECKS`,
`utilization_idle.classify`, `version_eol.minor`, `fleet_cost.{RG_CHUNK,chunks,last_full_prev}`.
Categories (first match wins): UPGRADE NOW, STOPPED BILLING, SECURITY GAP,
IDLE CAPACITY, COST HOTSPOT, UPGRADE SOON, HYGIENE REVIEW, HEALTHY; plus
0-100 health score. Flags: `--no-cost`, `--no-metrics`, `--months`, `--days`,
`--image-warn-days`, `--hotspot-min-usd`.

## Conventions (follow these when adding a report)

- Module pattern: docstring (first line becomes argparse description), constants,
  `main(argv=None)` using `base_parser()` -> `load_subscriptions` -> `pick_scope`
  -> `connect()` -> `load_fleet()` -> build DataFrames -> excel workbook ->
  `excel.save(wb, out_path(args, "stem", env_filter))`.
- Workbook layout enforced by excel.save(): ReadMe (intro/blue) -> Summary*
  (summary/green) -> detail tabs -> Raw*/legend (reference/gray). Pass `section=`.
- Excel formulas: cells starting with `=` are written as formulas. **Sort the
  DataFrame BEFORE adding formula columns** (formulas anchor to row numbers).
- Cost queries: always subscription scope grouped by node resource group
  (chunks of RG_CHUNK=30), never per cluster. Current month is MTD; MoM/trend
  comparisons use full months only (`fleet_cost.last_full_prev`).
- Metrics: `connect(min_interval=0.15)` and skip stopped clusters.
- Cluster identity: cluster names can collide across subscriptions; prefer
  keying by `c["id"].lower()` / pool `cluster_id` (governance/fleet_cost still
  key by name in places).
- Style: stdlib + pandas, `%`-formatting in log/strings, ~100-col lines, no
  type hints, comments only for non-obvious constraints.

## Gotchas

- ARG KQL is a stricter subset: no `//` comments; reserved keywords (e.g.
  `kind`) must be projected bare, not as alias targets. The smoke-test mock
  does NOT validate KQL — these errors only surface against real Azure.
- Smoke test (`tests/smoke_test.py`) monkeypatches `hc.AzureSession.request`,
  `hc.connect`, and sets `mod.connect = fake_connect` for every report module —
  **add new modules to its import list and patch tuple**, plus a `run(...)`
  stanza asserting sheets + a behavioral check. Fixture: 3 clusters
  (aks-dev-01 eastus 1.29 kubenet insecure; aks-dev-02 eastus2 stopped private;
  aks-prod-01 westeurope 1.32 with spot pool + cost growth 1030->1051->1533).
- `azure.identity` import lives inside `connect()` so offline tests never need it.
- environment is per cluster (tags -> RG tags -> name inference), never from
  subscription name. `(unknown)` env excluded from --nonprod by default.
- `excel.add_table` truncates sheet names to 31 chars; `fail_values`/`warn_values`
  match exact cell strings.
- Cost rows can be `(unmatched)` cluster when an RG isn't a known node RG.
- `fleet_cost` is LAYERED (summary story tabs first, then per-cluster detail, then
  raw). Summary tabs in creation/display order: `Scorecard` (built with
  `excel.add_scorecard` KPI cards, NOT a table - exec one-pager),
  `CommitmentOpportunity`, `SummaryByEnvironment`, `SummaryBySubscription`; detail is
  `ClusterCosts`/`PricingModelSplit`/`TopMovers`/`MeterChanges`; reference is
  `RawMonthly`(+`RawDaily`). `excel.save()` sorts by section and the sort is STABLE,
  so the summary-tab display order is just their CREATE order - the three new tabs are
  created right after `add_readme`, before the detail tables. `env_summary_rows`/
  `commitment_rows`/`scorecard_cards` are the builders; `scorecard_cards` takes the
  already-built `commit` frame so the headline KPI and the action tab agree.
  `CommitmentOpportunity` flags steady OnDemand spend as a reservation/savings-plan
  candidate: baseline = MIN OnDemand over FULL months (current MTD excluded),
  `Est monthly saving = baseline * --commit-discount` (default 0.30), status
  RESERVE CANDIDATE / COVERED (>= `COMMIT_COVERED` 0.70 existing RI+SP coverage),
  rows with baseline < `COMMIT_MIN_USD` (50) dropped. The pricing-model Cost query
  has NO meter axis, so storage/non-VM OnDemand spend is included in the baseline -
  it surfaces candidates, VM SKU eligibility must be verified (said so in the ReadMe).
  `is_prod` (azrep.subs) drives the `SummaryByEnvironment` prod/non-prod tier, the
  Scorecard prod-share KPI, and is the SAME env resolution every report uses (never
  from the subscription name). Smoke `chk_cost_story` asserts the hero cards, the
  env tiers, and a RESERVE CANDIDATE row; `chk_sections` now expects `Scorecard` as
  the first summary tab.
- Spot priority is IMMUTABLE on an existing agent pool — spot conversion always
  means create a new spot pool + shrink the OD pool (spot-sim does this). AKS
  auto-adds the `scalesetpriority=spot:NoSchedule` taint; don't send it in PUTs.
- `spot-savings` infers spot adoption from the first daily Cost Management row
  with Spot spend above a threshold (`first_spot_dates`). ARG has only current
  node-pool state, so this is cost-observed adoption, not an ARM creation
  timestamp. **The headline saving is priced from ACTUAL amortized billing, not
  retail list price** (`build_spot_estimates_actual`, the only estimator now - the
  old retail-only `build_spot_estimates` was removed). For each spot VMSS day we
  read billed node-hours from Cost Management `UsageQuantity` (CostClient.query
  gained a `with_quantity` flag adding a usageQuantity Sum aggregation; the
  spot_savings res query passes it) and re-price those hours at the cluster's own
  effective OD/RI $/node-hour = sum(CostUSD)/sum(UsageQuantity) (`_effective_rate_table`).
  The rate is chosen by a ladder (`_pick_od_hr`, recorded in `od_hr_source`):
  pre_spot_history (per VM size, then cluster blend, from the baseline window
  BEFORE that cluster's first spot day) -> concurrent_od_pool (the OD/RI pool it
  still runs in the trend window) -> retail_fallback (public price) -> price_missing.
  Amortized cost spreads RI/SP, so these rates carry those discounts; the saving
  thus varies cluster to cluster. `annotate_daily_and_summary` rolls od_hr_source
  up to a per-cluster `rate_basis` (dominant source by counterfactual $, via
  `rate_basis_label`/`_is_actual_basis`), surfaced in `RealizedSavings`/
  `SpotSavingsSummary` and the Scorecard "Actual-rate basis" card. If billing
  returns no node-hours, hours fall back to retail-derived (actual/retail spot_hr).
  Whole-cluster before/after total cost stays contextual and workload-confounded.
  **The full fleet in scope is kept by default** (no spot-pool filter). Pass
  `--only-spot-clusters` (keyed on pool `priority == "spot"` over `cluster_id`,
  same opt-in as `spot_cluster_report --only-spot-clusters`) to restrict to
  clusters with a current spot node pool; `--include-all-clusters` is a
  deprecated no-op kept for backward compatibility. `--nonprod-spot` is a
  management shortcut == `--nonprod --only-spot-clusters` (it sets
  `args.nonprod=True` BEFORE `pick_scope` so it also suppresses the interactive
  prompt, then forces the spot-only filter) for the "non-prod clusters that run
  spot" BU story. **Why default = full
  fleet:** a cluster whose spot pool was removed/decommissioned still shows
  Cost Mgmt spot spend in its history and the report's verdict is cost-observed,
  so filtering on current ARG spot-pool state drops real savings evidence.
  Environment is per cluster via `azrep.subs.resolve_env_detail` (cluster tags
  -> RG tags -> name inference using `ENV_CODE_MAP`: -d-/-s-/-r-/-p-/-u- ->
  dev/sit/dr/prod/uat), the SAME resolution every report uses; never from the
  subscription name. The report is LAYERED (green `section="summary"` story tabs
  first, then a detail/reference evidence appendix). Story tabs in order:
  `Scorecard` (built with `excel.add_scorecard` KPI cards, NOT a table) separates
  the hard billed fact (invoiced Spot spend, from `last_N_actual_spot_cost`) from
  the avoided-cost estimate (priced at each cluster's actual OD/RI rate), an
  "Actual-rate basis" card (priced clusters on real billing vs retail fallback),
  spot coverage %, realized-vs-achievable spot discount (achievable = hours-weighted
  `(od_hr-spot_hr)/od_hr` over priced estimate rows), untapped runway, prod-on-spot
  risk count, and the 3-state adoption read (Verified savers / Pricing gap / Not adopted);
  `BeforeAfterByEnv` (`before_after_rows`: the management/BU slide - one row per
  environment over the trend window, run-rated to a month: `monthly_actual_cost_usd`
  (after, actual) vs `monthly_on_demand_cost_usd` (before = after + the priced
  counterfactual `estimated_spot_saving`, so before>=after always and a no-retail
  run shows before==after / status "Pricing gap"), plus `monthly_saving_usd`,
  `saving_pct`, `annualized_saving_usd` and a status badge; a grouped before/after
  bar chart via `excel.add_grouped_bar_chart` over the env rows and an
  `add_total_row` fleet total. Saving is based on `estimated_spot_saving` (NOT
  `od_cf - actual_spot`) so unpriced spot can't fake a negative saving);
  `Recommendations` (`recommendation_rows`: one ranked row per eligible regular
  Linux User OD pool via `_projectable_pools`, `est_monthly_saving_usd =
  nodes*(od_hr-spot_hr)*24*30.4375`, with `cluster_risk_band` + a loud
  `verify_before_move` caveat - it is EMPTY without retail prices, so it is NOT in
  the smoke non-empty sheet list); `CoverageRisk` (`coverage_risk_rows`: per-
  cluster `spot_share_compute`, spot/total nodes, pool/family/zone diversity,
  price-cap, plus `vmss_churn_approx` and an additive `_risk_band` HIGH/MED/LOW -
  reuses the spot-risk spirit of `spot_cluster_report.assess_clusters`);
  `RealizedSavings` (`realized_savings_rows`: slim fact-vs-model per cluster with
  a `verdict_label` badge) and `MonthlySavings` (`monthly_savings_rows`: last 3
  calendar months - a fleet-total set of rows then per-cluster - with spot fact /
  od counterfactual / saving / `savings_rate_pct` per month; `month_status` marks the
  current month "MTD (partial)"; `savings_from_spot_pool` is a Yes/No flag, "Yes"
  only when that month carried actual Spot VMSS spend > `SPOT_THRESHOLD_USD`, else
  "No (no spot spend)" - it tracks spot attribution, NOT whether a saving could be
  priced, so the no-retail-prices path still shows "Yes" with a zero saving). To keep
  the 3-month roll-up populated, `analysis_window` floors the default daily-cost
  lookback at `MONTHLY_LOOKBACK_DAYS` (92); an explicit `--lookback-days` still wins.
  Then `SpotTimeline` (per-day Actual total / OD
  counterfactual / Cumulative realized saving + modeled-future column; two line
  charts), `TopSavers` (ranked standings: projected/annualized monthly saving,
  `savings_rate_pct`, status badge; bar chart) and `SavingsByEnv` (prod vs non-
  prod tier roll-up via `azrep.subs.is_prod()`; bar chart) also stay in summary.
  The detail appendix is `SavingsProjection`, `BeforeSpot`, `AfterSpot`,
  `ActualVsProjection`, `SpotSavingsSummary` (full per-cluster detail; keeps the
  raw `verdict` + `last_N_*` columns engineers and tests rely on), `FleetDailyTrend`,
  `SpotSavingsDaily/ByPool`; reference is `PriceReference`/`RawDailyCost`.
  `TopSavers`/`SavingsByEnv`/`SavingsProjection` carry annualized +
  `savings_rate_pct` (fractional, Excel % format). `VERDICT_LABEL` maps raw codes
  to badges: SAVING->"Verified saving", FLAT->"Inconclusive", COST_UP->"Needs
  review", PRICE_MISSING->"Pricing gap", NO_SPOT_COST->"Not adopted".
  `classify_verdict` is unchanged, but COST_UP/FLAT effectively never fire for the
  counterfactual (saving = `hours*(od_hr-spot_hr) >= 0`), so the Scorecard presents
  the honest 3-state read instead of the old 5-badge tally. The old
  `SpotSavingsHeadline` table (`headline_rows`) and `_leaderboard_chart` were removed.
- `spot-savings` eviction proxy: `CoverageRisk.vmss_churn_approx` comes from
  `armextras.vmss_churn_events(session, sub, node_resource_group, days)` - a NEW
  helper that reads the node RG's Activity Log for Microsoft.Compute VMSS
  delete/deallocate ops (UNLIKE `activity_events`, which keeps only
  Microsoft.ContainerService control-plane writes). True spot evictions are only
  visible on-node (Scheduled Events/IMDS), unreachable at Reader scope, so this
  proxy mixes evictions with autoscale-down - label it as such, never a clean
  eviction count. It runs once per spot cluster (node RG) unless `--no-eviction-scan`.
  The smoke `ACTIVITY` fixture carries one Microsoft.Compute VMSS delete event so
  the column is exercised; `cluster_deepdive`'s `activity_events` ignores it.
- Control-plane-only AKS upgrade = PUT the managed cluster WITHOUT
  `properties.agentPoolProfiles`; pools upgrade individually via agentPool
  `orchestratorVersion`. One minor hop at a time.
- checkPolicyRestrictions (api 2024-10-01) needs `includeAuditEffect: true` to
  surface audit-effect candidates; bodies evaluate as if in the sandbox RG, so
  rules on source RG name/tags don't simulate. `--effect-override` is the fallback.
- topologySpreadConstraints between OD/spot must key on
  `kubernetes.azure.com/agentpool` — the `scalesetpriority` label exists only on
  spot nodes, so it can't be a spread key (classic app-team mistake; a spot-sim
  scenario covers it).
- Kubeconfigs are written next to the sandbox config as `.kubeconfig-<cluster>`
  (gitignored); never touch ~/.kube/config. `azrep.kubectl._run` is the
  subprocess seam tests monkeypatch.
- Azure Policy -> Gatekeeper constraint replication takes up to ~15 min; the
  k8s-test harness polls (`constraint_wait_seconds`, default 300).
- NVD API 2.0 (vulnerability_report): `cveIds` batch param is valid (max 100),
  but `references` is a LIST of {url, source, tags} — the 1.x
  `references.referenceData` shape is gone. The online path is mocked in
  tests/test_vulnerability_report.py (`_fake_requests_get`) with real 2.0 shapes.
- `rearch` (subscription_rearch) uses the `advisorresources` ARG table (Cost
  category recs carry `properties.extendedProperties.annualSavingsAmount` as a
  STRING - parse defensively, /12 for monthly). It is single-sub: >1 sub in
  scope exits(2). The smoke mock routes its ARG queries by marker strings in
  `_rearch_kind`/`_rearch_routes` (NOT by table name, since ALL_RESOURCES shares
  `tolower(type)`+`kind` with architecture_design) and routes its Cost queries by
  `ServiceName` being in the grouping - if you add/rename a rearch ARG query or
  change its grouping, update those routers in tests/smoke_test.py too. Marker
  ORDER matters: "operationalstate" (APPGW_KQL) must stay ahead of
  "backendaddresspools" (LBS_KQL) since the app-gateway query projects
  properties.backendAddressPools as well. The 15 ORPHANED checks' KQL filters
  are adapted from dolevshor/azure-orphan-resources (MIT); empty-RG detection
  is computed in Python from RG_TAGS_KQL minus RGs seen in ALL_RESOURCES (no
  extra query).
- `policy-components` (policy_components) drills one initiative to its non-compliant
  COMPONENTS. componentPolicyStates is a SEPARATE data-plane resource with its own
  api-version `2022-04-01` (policyStates stays `2019-10-01`); component records only
  exist for resource-provider-mode policies (Kubernetes, Key Vault data plane, ...),
  so the report falls back to resource-level policyStates for members that emit no
  components (component_type `(resource)`). Assignment filtering is by
  PolicyAssignmentId (NOT assignment name) so MG-inherited initiatives resolve in
  every child sub. Selection is interactive unless `--initiative/--group/--policy`
  are given; `--all` forces no prompts (smoke runs it that way). The smoke mock
  does NOT validate api-versions or OData $filter - these only fail on real Azure.
  IMPORTANT mock-router ordering: PolicyInsights query URLs embed the assignment id
  in `$filter` (which contains the substring "policyAssignments"), so smoke_test's
  fake_request routes `componentpolicystates`/`policystates` BEFORE `policyassignments`
  (and componentpolicystates before policystates). Set-definition fixture (DEF_POD_SEC)
  carries `policyDefinitionGroups` + `policyDefinitions`; COMPONENTS is keyed by sub.

## Testing

```
uv run python tests/smoke_test.py            # offline end-to-end, all reports
uv run python tests/test_sandbox.py          # sandbox family: clone/impact/k8s-test/spot/upgrade
uv run python tests/test_spot_split.py
uv run python tests/test_spot_savings.py
uv run python tests/test_vulnerability_report.py
```

Run the smoke test after any report/module change; run test_sandbox.py after
any sandbox/kubectl change (it reuses the smoke fixture via `import smoke_test`).
No pytest; plain asserts.

## When you change something

1. Wire new reports into `aks_report.py` REPORTS (key, aliases, module, title,
   description) — the launcher and menu come from that list.
2. Update README.md (module table + usage examples) and tests/smoke_test.py.
3. Update THIS file (layout/table/conventions/gotchas as applicable).
