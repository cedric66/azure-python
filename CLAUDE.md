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
                     (Cost, CostUSD, Period + groupings); default_window(), dim_in(), f_and()
  armextras.py       cluster_metrics() (Monitor platform metrics), activity_events(),
                     aks_supported_versions(), node_image_date(), retail_vm_prices()
  excel.py           new_workbook(), add_readme(), add_table() (sections: intro/summary/
                     detail/reference; fail/warn conditional formatting; money/pct/int),
                     add_total_row(), add_line_chart(), add_bar_chart(), save()
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
| cost | fleet_cost.py | Cost Mgmt (sub-scope by node RG) + ARG |
| deepdive | cluster_deepdive.py | Cost Mgmt + ARG + Monitor + Activity Log (one cluster) |
| design | architecture_design.py | ARG; also writes .md (Mermaid) + .drawio + .html (self-contained, no JS) companions |
| version | version_eol.py | ARG + aks_supported_versions per region |
| spot | spot_cluster_report.py | Cost Mgmt + ARG + retail prices |
| spot-design | spot_split_design.py | ARG + retail prices |
| spot-savings | spot_savings.py | Cost Mgmt + ARG + retail prices |
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
- Spot priority is IMMUTABLE on an existing agent pool — spot conversion always
  means create a new spot pool + shrink the OD pool (spot-sim does this). AKS
  auto-adds the `scalesetpriority=spot:NoSchedule` taint; don't send it in PUTs.
- `spot-savings` infers spot adoption from the first daily Cost Management row
  with Spot spend above a threshold. ARG has only current node-pool state, so
  this is cost-observed adoption, not an ARM creation timestamp. The headline
  savings verdict is a retail-rate counterfactual for actual Spot VMSS spend;
  whole-cluster before/after total cost is contextual and workload-confounded.
  **The full fleet in scope is kept by default** (no spot-pool filter). Pass
  `--only-spot-clusters` (keyed on pool `priority == "spot"` over `cluster_id`,
  same opt-in as `spot_cluster_report --only-spot-clusters`) to restrict to
  clusters with a current spot node pool; `--include-all-clusters` is a
  deprecated no-op kept for backward compatibility. **Why default = full
  fleet:** a cluster whose spot pool was removed/decommissioned still shows
  Cost Mgmt spot spend in its history and the report's verdict is cost-observed,
  so filtering on current ARG spot-pool state drops real savings evidence.
  Environment is per cluster via `azrep.subs.resolve_env_detail` (cluster tags
  -> RG tags -> name inference using `ENV_CODE_MAP`: -d-/-s-/-r-/-p-/-u- ->
  dev/sit/dr/prod/uat), the SAME resolution every report uses; never from the
  subscription name. The report is structured as a FinOps value story: its
  summary tab `SpotSavingsHeadline` is a KPI scorecard (metric/value/unit/detail
  rows) carrying hero numbers (Spot clusters in scope, Verified savers, Realized
  spot saving, Annualized run-rate = realized * 365/trend_days, Fleet savings
  rate = realized / OD-counterfactual, Additional projected saving from unused
  runway), followed by a "Ranked cluster: <name> (<env>)" leaderboard block
  with an embedded bar chart (`_leaderboard_chart` — built directly with
  openpyxl Reference because the block sits partway down the sheet;
  `excel.add_bar_chart` hard-codes min_row=1 and would pull in hero rows), then
  a confidence tally ("%badge% (CODE)" rows with actual/counterfactual/saving
  detail). Next come `SpotTimeline` (per-day Actual total / OD counterfactual /
  Cumulative realized saving, plus a modeled-future column = future OD -
  projected saving; two line charts), `TopSavers` (ranked standings: projected
  monthly saving, annualized_projected_usd = projected * 365/project_days,
  savings_rate_pct = saving / OD-counterfactual, status = `verdict_label()`
  human badge; one bar chart) and `SavingsByEnv` (clusters rolled up to prod
  vs non-prod tiers via `azrep.subs.is_prod()` on the resolved environment;
  one row per environment with clusters / verified_savers / projected+annualized
  monthly saving / savings_rate_pct / top_verdict; prod sorts before non-prod;
  one bar chart on projected_monthly_saving_usd). `TopSavers`, `SavingsByEnv`
  and `SavingsProjection` all add the annualized + savings_rate_pct columns;
  savings_rate is fractional there (Excel % format) but scaled to integer pct
  in the headline for readability. Then the original `BeforeSpot`, `AfterSpot`,
  `SavingsProjection` and `ActualVsProjection`; current node counts are current
  ARG facts only, while `avg_node_equiv_at_retail` is an explicitly labeled
  cost/rate estimate. `VERDICT_LABEL` maps the raw verdict codes to human
  badges: SAVING->"Verified saving", FLAT->"Inconclusive", COST_UP->"Needs
  review", PRICE_MISSING->"Pricing gap", NO_SPOT_COST->"Not adopted".
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
