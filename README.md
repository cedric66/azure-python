# AKS Reporting Toolkit

One front-door Python script, `aks_report.py`, for AKS reports across many Azure
subscriptions using **subscription-level read access only** (no kubectl). The
report-specific files stay in the project as modules, but day-to-day usage goes
through the launcher. Every report writes a formatted multi-tab `.xlsx` into
`reports/`.

Built for scale: ~25 subscriptions / ~500 clusters. Inventory comes from Azure
Resource Graph (a handful of calls for the whole fleet); cost comes from
subscription-scope Cost Management queries (~3 per subscription, not per
cluster); 429 throttling is handled automatically.

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
uv run python aks_report.py spot-savings --cluster aks-dev-01
uv run python aks_report.py spot-savings --all --include-all-clusters
uv run python aks_report.py convert README.md --to all --config report_style.example.yaml
uv run python aks_report.py sandbox plan sandbox.example.yaml
uv run python aks_report.py list
```

The files below are the modules the launcher calls.

## Report Modules

| Script | What it answers | Data sources |
|---|---|---|
| `cluster_deepdive.py` | One cluster: 3-month daily amortized cost trend + chart, actual vs amortized, cost per meter & node pool, spot/RI/SP split, **SKU change detection**, utilization, activity log | Cost Mgmt, ARG, Monitor, Activity Log |
| `architecture_design.py` | Actual-state design snapshot for one cluster, a resource group, cluster set, or full subscription; XLSX (incl. relationship map) plus Mermaid Markdown doc, editable draw.io diagram and self-contained HTML design view | ARG |
| `cluster_360.py` | `360`: every cluster from every subscription in one categorized workbook - joins inventory, version/EOL status, governance checks, amortized cost trend, utilization and node-image staleness; assigns each cluster a category (UPGRADE NOW, STOPPED BILLING, SECURITY GAP, IDLE CAPACITY, COST HOTSPOT, UPGRADE SOON, HYGIENE REVIEW, HEALTHY) and a 0-100 health score, with an ActionItems tab explaining every finding | ARG, AKS locations API, Cost Mgmt, Monitor |
| `fleet_inventory.py` | Every cluster detail: versions, tiers, node pools, networking, security, addons, tags | Resource Graph only |
| `fleet_cost.py` | Per-cluster monthly amortized trend, MoM %, spot share, RI/SP coverage, top movers, fleet-wide SKU change signals | Cost Mgmt, ARG |
| `version_eol.py` | Out-of-support / LTS-only Kubernetes versions per region, node image staleness | ARG, AKS locations API |
| `container_os_eol.py` | EOL radar for container base images and runtimes (Alpine, Debian, UBI/RHEL, Java/Temurin, Python, Node.js): what is safe to build on, what is security-only, what to move to next | endoflife.date (no Azure access) |
| `aks_lifecycle.py` | AKS release calendar GA/EOL dates, managed add-ons, retirements/deprecations, GA and preview features, behavior changes, per-version component breaking changes | Microsoft Learn pages + Azure/AKS GitHub release notes (no Azure access) |
| `spot_cluster_report.py` | One spot workbook: spot/on-demand pool configuration, autoscaler profile, zones, taints, eviction/price settings, pool/resource cost breakup, assessment, plus spot-candidate pools with retail-price savings (formerly `spot_opportunity.py`) | Cost Mgmt, ARG, public Retail Prices API |
| `spot_split_design.py` | `spot-design`: present vs future node-pool split design for team-dedicated clusters (Korea pattern) - team auto-detect from labels/taints (+`teams.csv` override), on-demand floor + paired spot pool sizing, ready-to-run `az aks nodepool add` commands, BU workload YAML (tolerations/affinity/spread/PDB), rollout plan, savings, Mermaid design doc convertible via `convert` | ARG, Retail Prices API |
| `spot_savings.py` | `spot-savings`: day-by-day cost after first observed Spot spend, with a presentation-ready `SpotSavingsHeadline` snapshot, three executive tables (`BeforeSpot`, `AfterSpot`, `SavingsProjection`), actual-vs-projection chart, last-30-day retail counterfactual savings and pool-level savings breakup. **By default only clusters with a current spot node pool are included** (crisp view); pass `--include-all-clusters` to restore full-fleet behavior | Cost Mgmt, ARG, public Retail Prices API |
| `utilization_idle.py` | Node CPU/memory avg/p95/max per cluster, idle & stopped-but-billing clusters | ARG, Monitor platform metrics |
| `governance.py` | 17-check hygiene scorecard (private API, local accounts, kubenet, zones, autoscaler, tiers, ...) | Resource Graph only |
| `conformance.py` | `conformance`: fleet drift against a golden baseline YAML (same schema as the sandbox config; every key you set becomes a rule) - per-cluster scorecard, fail details, failures by rule | Resource Graph only |
| `policy_report.py` | Policy assignments incl. inherited, compliance per cluster, Kubernetes-policy **blind spots** (k8s policies assigned but addon off) | Policy/PolicyInsights, ARG |
| `policy_components.py` | `policy-components`: drill ONE compliance initiative (assignment) -> groups -> member policies to the individual **non-compliant components** (e.g. the failing Kubernetes namespace/kind/name), with resource-level fallback for policies that have no components; interactive selection or `--initiative/--group/--policy` flags (`--list` to discover) | Policy/PolicyInsights componentPolicyStates + policyStates, ARG |
| `network_ip_capacity.py` | Network model, API exposure, subnet IP pressure, Azure CNI pod IP demand, subnet NSG/route/NAT metadata | Resource Graph only |
| `tag_chargeback.py` | Required tag coverage, owner/cost-center/application gaps, tag value normalization, chargeback readiness | Resource Graph only |
| `optimization_report.py` | Prioritized cost-optimization queue combining amortized cost, utilization, spot/RI/SP signals, stopped-billing candidates | Cost Mgmt, ARG, Monitor |
| `subscription_rearch.py` | `rearch`: ONE subscription, ALL resources (not just AKS) - orphan/idle disks, public IPs, NICs, empty load balancers, stopped-not-deallocated VMs, stale snapshots, empty App Service plans, app gateways with no backend targets, subnet-less NAT gateways, database-less SQL elastic pools, VNet-link-less private DNS zones, unassociated NSGs/route tables, empty availability sets and resource groups (orphan filters adapted from the MIT `dolevshor/azure-orphan-resources` ARG catalog), geo-redundant nonprod storage, flat-rate firewalls/gateways, premium SQL, plus Azure Advisor cost recs; findings carry actual last-month cost and an estimated monthly saving, and a companion `.md` narrative (current-state per RG + Mermaid, findings by category, target-state moves) drives a re-architecture-for-cost-savings exercise | Resource Graph, Cost Mgmt, Azure Advisor, Retail Prices API |
| `vulnerability_report.py` | Prisma XLSX/CVE-list enrichment and base-image/application/platform classification with remediation guidance | Prisma XLSX, classification rules, NVD/CISA KEV/EPSS |

## Setup (Local Linux)

Requires Python 3.12+ and subscription-level Azure read access. Dependencies are
managed by `uv` from `pyproject.toml` and `uv.lock`.

Install OS prerequisites, clone the repo, install `uv`, and sync the locked
environment:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git

git clone git@github.com:cedric66/azure-python.git
cd azure-python

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv python install 3.12
uv sync --frozen
mkdir -p reports exports
```

Configure `subscriptions.csv` with the subscriptions to scan, then authenticate
with one of the credential methods used by `DefaultAzureCredential`.

Azure CLI login:

```bash
az login
az account set --subscription "<subscription-id>"
```

Service-principal login:

```bash
export AZURE_CLIENT_ID="<app-id>"
export AZURE_CLIENT_SECRET="<secret>"
export AZURE_TENANT_ID="<tenant-id>"
```

Run the launcher through `uv`:

```bash
uv run python aks_report.py --help
uv run python aks_report.py list
uv run python aks_report.py inventory --all
uv run python aks_report.py cost --env dev
```

Common `uv` commands for this project:

```bash
uv sync --frozen                         # install exactly from uv.lock
uv sync                                  # resync after pulling repo changes
uv run python aks_report.py ...          # run reports in the project env
uv run python tests/smoke_test.py        # run the offline smoke test
uv run python tests/test_spot_split.py   # run the spot-design fixture test
uv run python tests/test_spot_savings.py # run the spot-savings math test
uv add <package>                         # add a runtime dependency
uv lock                                  # refresh uv.lock after dependency edits
uv tree                                  # inspect resolved dependencies
```

## Setup (Linux / Docker)

The intended runtime is Linux in Docker.

```bash
docker build -t aks-reporting .

# Recommended for containers: service-principal auth.
docker run --rm \
  -e AZURE_CLIENT_ID="<app-id>" \
  -e AZURE_CLIENT_SECRET="<secret>" \
  -e AZURE_TENANT_ID="<tenant-id>" \
  -v "$PWD/subscriptions.csv:/app/subscriptions.csv:ro" \
  -v "$PWD/reports:/app/reports" \
  aks-reporting cost --env dev
```

You can also run the interactive menu:

```bash
docker run --rm -it \
  -e AZURE_CLIENT_ID="<app-id>" \
  -e AZURE_CLIENT_SECRET="<secret>" \
  -e AZURE_TENANT_ID="<tenant-id>" \
  -v "$PWD/subscriptions.csv:/app/subscriptions.csv:ro" \
  -v "$PWD/reports:/app/reports" \
  aks-reporting
```

The Dockerfile uses `uv sync --frozen`, so the image is built from
`pyproject.toml` and `uv.lock`.

The base image does not include Azure CLI. `DefaultAzureCredential` works best
in the container through service-principal environment variables, managed
identity, or workload identity. If you want to use `az login` inside the
container, build a custom image that installs Azure CLI.

## Setup (Local Windows)

```powershell
cd azure-python
py -3.12 -m pip install uv
uv sync
az login
uv run python aks_report.py --help
```

Requires Python 3.12+. Dependencies are managed by `uv` through
`pyproject.toml` and `uv.lock`; local commands can be run as
`uv run python aks_report.py ...`. Auth uses `DefaultAzureCredential`:
`az login` works, as do service principal env vars (`AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`) or managed identity - no code
changes.

**Permissions:** `Reader` on each subscription. Cost reports additionally need
Cost Management read (included in Reader for most subscription types; if cost
queries return 401/403, ask for `Cost Management Reader`).

## Sandbox AKS and Policy Testing

For the Azure sandbox where you have admin access, use the same launcher with
the `sandbox` command. The sandbox workflow is driven by a separate config file
so you can explicitly supply the subscription, resource group, AKS node pools,
and policies to test.

Start by copying the example. Both JSON and YAML configs are supported
(`load_config` picks the parser by extension) - if you prefer JSON throughout,
use `sandbox.example.json`; policy definitions under `policies/` are plain
JSON either way:

```bash
cp sandbox.example.json sandbox.json     # JSON workflow
cp sandbox.example.yaml sandbox.yaml     # or YAML, same schema
```

Edit these values first:

```yaml
subscription_id: "00000000-0000-0000-0000-000000000000"
subscription_name: "my-aks-sandbox"
environment: "sandbox"
resource_group: "rg-aks-sandbox-dev"
location: "eastus"
cluster:
  name: "aks-sbx-policy-01"
  node_pools:
    - name: "sys"
      mode: "System"
      vm_size: "Standard_D4s_v5"
      count: 2
```

The same file also contains `policies.definitions` and
`policies.assignments`. Policy definitions can live as JSON files under
`policies/`, then the YAML decides where and how to assign them.

Typical lifecycle:

```bash
uv run python aks_report.py sandbox plan sandbox.yaml
uv run python aks_report.py sandbox deploy sandbox.yaml --yes --wait
uv run python aks_report.py sandbox policy-apply sandbox.yaml --yes
uv run python aks_report.py sandbox scan sandbox.yaml --yes
uv run python aks_report.py sandbox report sandbox.yaml
uv run python aks_report.py sandbox cleanup sandbox.yaml --yes
```

Write/delete commands require `--yes`. They also refuse to run unless the
resource group or cluster name looks like a sandbox/test/lab name, unless you
set `safety.allow_non_sandbox_names: true`.

Recommended policy flow:

1. Add or edit a policy JSON file under `policies/`.
2. Reference it from `sandbox.yaml` under `policies.definitions`.
3. Add an assignment under `policies.assignments`, usually with
   `enforcement_mode: "DoNotEnforce"` first.
4. Run `policy-apply`, then `scan`, then `report`.
5. Once the sandbox result looks right, promote the policy assignment through
   your normal production governance process.

The estate-wide read-only report remains:

```bash
uv run python aks_report.py policy --all
```

That report will show policy assignments and compliance across all included
subscriptions in `subscriptions.csv`, including the same policy after it has
been assigned outside the sandbox.

To drill a single compliance initiative down to the individual non-compliant
components (the failing Kubernetes objects, etc.):

```bash
uv run python aks_report.py policy-components --all --list             # discover initiatives
uv run python aks_report.py policy-components --all --initiative "pod security baseline"
uv run python aks_report.py policy-components --env dev --initiative NIST --group AC-6 --policy privileged
```

Run with no `--initiative` on a terminal to be prompted for the compliance name,
then the groups, then the policies; `--all` (or passing the flags) runs unattended.

### kubectl in the sandbox

Unlike the read-only fleet, the sandbox cluster is yours - the launcher can
drive kubectl against it (requires `kubectl`, and `az` + `kubelogin` for AAD
clusters; falls back to ARM `listClusterUserCredential` when `az` is missing).
Kubeconfigs are written next to the config file as `.kubeconfig-<cluster>`
(gitignored), never into `~/.kube/config`:

```bash
uv run python aks_report.py sandbox kubeconfig sandbox.yaml
uv run python aks_report.py sandbox kubectl sandbox.yaml -- get nodes -o wide
uv run python aks_report.py sandbox k8s-apply sandbox.yaml -f app.yaml --namespace demo --yes
uv run python aks_report.py sandbox k8s-delete sandbox.yaml -f app.yaml --namespace demo --yes
```

### Kubernetes policy tests (Gatekeeper)

ARM-side compliance (`scan`/`report`) cannot see admission behavior. The
`k8s-test` command can: it waits for the Azure Policy addon + Gatekeeper to
sync, applies the test manifests from the config's `k8s_tests` block, and
asserts the result per case - `deny` (webhook must reject), `allow` (must
admit), or `audit` (must admit, then appear in constraint violations):

```yaml
k8s_tests:
  namespace: "policy-test"
  constraint_wait_seconds: 300        # constraint replication can take ~15 min
  cases:
    - {name: deny-untrusted-registry,  manifest: policies/tests/pod-bad-registry.yaml,  expect: deny}
    - {name: allow-trusted-registry,   manifest: policies/tests/pod-good-registry.yaml, expect: allow}
```

```bash
uv run python aks_report.py sandbox policy-apply sandbox.yaml --yes   # includes the sample K8s assignment
uv run python aks_report.py sandbox k8s-test sandbox.yaml --yes --xlsx
```

The example config assigns the builtin "containers should only use allowed
images" policy (Gatekeeper-backed, enforced in the sandbox) so the sample
deny/allow pair under `policies/tests/` works out of the box.

### Clone a fleet cluster into the sandbox

Reproduce any fleet cluster in the sandbox to experiment safely. Clone reads
ONE cluster from Resource Graph (read-only) and writes a sandbox config
mirroring its shape - version, CNI/network model, security, addons, pools with
taints/labels/spot settings - downsized for cost (1 node per pool, autoscaler
0..2, Free tier, subnet IDs and authorized IP ranges stripped):

```bash
uv run python aks_report.py sandbox clone \
  --cluster-id /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ContainerService/managedClusters/<name> \
  --base sandbox.yaml --out clone.yaml
uv run python aks_report.py sandbox plan clone.yaml
uv run python aks_report.py sandbox deploy clone.yaml --yes --wait
```

`--keep-counts`, `--keep-subnets`, `--keep-sku-tier` trade cost for fidelity.
Not cloned: windowsProfile (Windows pools are skipped), maintenance windows,
diagnostic settings, AAD admin group IDs.

### Policy impact simulation (fleet what-if)

Before proposing a policy org-wide, measure its blast radius without touching
production: `impact` stages the candidate as a DoNotEnforce assignment in the
sandbox resource group, then evaluates every fleet cluster's verbatim ARM body
against it via `checkPolicyRestrictions` and writes an XLSX evidence pack
(summary by environment/subscription plus per-cluster results):

```bash
uv run python aks_report.py sandbox impact sandbox.yaml \
  --policy policies/audit-aks-private-api.json --params effect=Audit --all --yes
```

Caveats: bodies are evaluated as if they lived in the sandbox resource group,
so rules keyed on the source RG name/tags do not simulate; if an audit-effect
candidate yields zero hits, retry with `--effect-override` (stages the
definition with a Deny default - the assignment stays DoNotEnforce, so nothing
can actually block).

### Spot conversion simulation

`spot-sim` rehearses spot adoption on the sandbox cluster end to end. Spot
priority is immutable on existing pools, so it always creates a NEW spot pool
(sized by the same engine as `spot-design`) and shrinks the on-demand pool,
deploys the descheduler (`manifests/spot/descheduler.yaml`) for rebalancing,
then runs a ten-scenario matrix of deployments modeled on real app-team YAML -
success and failure combinations (missing toleration, required vs preferred
spot affinity, topology spread, too-strict PDB, single replica on spot) - and
reports where pods actually land:

```bash
uv run python aks_report.py sandbox spot-sim sandbox.yaml --pool usr --spot-share 0.6 --yes --md
uv run python aks_report.py sandbox spot-sim sandbox.yaml --yes --simulate-eviction   # VMSS simulateEviction + rebalance watch
```

`--md` writes a markdown guide embedding each scenario's YAML and observed
outcome - a copy-paste artifact for app teams adopting spot. Rebalancing uses
only kube-scheduler + cluster autoscaler + descheduler (no Karpenter, no
Cilium). Note the spread key: pods spread across
`kubernetes.azure.com/agentpool`, because the `scalesetpriority` label exists
only on spot nodes and therefore cannot act as a topology-spread key.

### Upgrade rehearsal

Rehearse a prod upgrade path on the (cloned) sandbox cluster: computes the
minor-by-minor hop path from the region's supported versions, blocks on an
offline deprecated-API scan of your manifests, then per hop upgrades the
control plane (control-plane-only PUT), gates on kubectl health (nodes Ready,
kubelet versions, no crash-looping workloads), upgrades pools sequentially
(spot last), and gates again. Hop timings land in an XLSX for sizing prod
maintenance windows:

```bash
uv run python aks_report.py sandbox upgrade-rehearsal clone.yaml --to 1.32 --manifests 'apps/*.yaml' --yes
uv run python aks_report.py sandbox upgrade-rehearsal clone.yaml --to next --control-plane-only --yes
```

### Golden-config conformance (fleet drift)

Declare your target architecture once as a golden YAML (same schema as the
sandbox config, subset allowed - every key you set becomes a rule), prove the
baseline actually deploys, then measure fleet drift against it:

```bash
cp sandbox.example.yaml golden.yaml          # edit down to your baseline keys
uv run python aks_report.py sandbox deploy golden.yaml --yes --wait   # baseline must deploy
uv run python aks_report.py conformance --golden golden.yaml --all    # fleet drift scorecard
```

### Subscription re-architecture (cost savings)

Point at exactly ONE subscription to inventory every resource (not just AKS),
price each finding from its actual last-month cost, and emit a workbook plus a
companion `.md` narrative that drives a re-architecture-for-cost-savings review.
Read-only (GET/POST query endpoints only):

```bash
uv run python aks_report.py rearch --subs contoso-platform           # workbook + narrative
uv run python aks_report.py rearch --subs 00000000-0000-0000-0000-000000000000 --months 6
uv run python aks_report.py rearch --subs contoso-platform --no-cost --top 20
```

It requires a single subscription in scope; with more than one it exits and
asks you to narrow with `--subs`.

## Input file

`subscriptions.csv` (edit the included template):

```csv
subscription_id,subscription_name,include
00000000-...,contoso-platform,Y
00000000-...,contoso-data,N
```

- `include=N` rows are ignored without deleting them from the file.
- A subscription can contain clusters from many environments. Environments are
  resolved per cluster from AKS tags, resource-group tags, or name inference.

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
uv run python aks_report.py utilization --env dev --days 14
uv run python aks_report.py governance --all
uv run python aks_report.py conformance --golden golden.yaml --all
uv run python aks_report.py policy --all
uv run python aks_report.py network --all
uv run python aks_report.py tags --all --required-tags environment,owner,costcenter,application
uv run python aks_report.py optimization --nonprod --days 14
uv run python aks_report.py container-eol
uv run python aks_report.py container-eol --products ubuntu,golang,dotnet
uv run python aks_report.py aks-lifecycle --releases 52
uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules vulnerability_classification.example.json
uv run python aks_report.py vulnerabilities --cves cves.txt --offline
```

## CVE / Prisma Vulnerability Report

Use `vulnerabilities` when you have a Prisma vulnerability export in `.xlsx`
format or a simple CVE list and want an Excel workbook that separates likely
base-image, application dependency, and platform/runtime-framework ownership.

```bash
uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules vulnerability_classification.example.json
uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules classification-rules/ --offline
uv run python aks_report.py vulnerabilities --cves cves.txt
```

Inputs:

- Prisma report: `.xlsx`. If `--sheet` is omitted, all sheets are scanned.
- CVE list: `.txt`, `.csv`, `.json`, or `.xlsx`.
- Classification rule files: optional JSON files or a directory of JSON files
  through `--classification-rules` / `--rules`. These are not Azure Policy.
  They are local override rules that teach the script your ownership model
  when Prisma context is ambiguous. The supplied
  `vulnerability_classification.example.json` shows the schema.
- Internet enrichment: NVD CVE 2.0, CISA KEV, and EPSS. Use `--offline` when
  running without internet; the report will classify from Prisma context and
  local classification rules only.

Prisma email/XLSX headers currently handled include: `registry`, `repository`,
`tag`, `id`, `distro`, `hostname`, `cve id`, `compliance`, `result`, `type`,
`severity`, `packages`, `package version`, `package license`, `cvss`,
`fix status`, `risk factors`, `cause`, `published`, `image id`,
`vulnerability link`, and `purl`.

Layer definitions:

- `base_image`: OS/base-image packages such as OpenSSL/glibc/curl from
  `deb`, `rpm`, or `apk` package managers.
- `application`: application/library dependencies such as npm, Maven, pip,
  NuGet, gem, Composer, or Cargo packages.
- `platform`: application runtime/framework layer such as Java/OpenJDK,
  Node.js, Python, .NET, Tomcat, or Spring Boot.

Example rule:

```json
{
  "classification_rules": [
    {
      "name": "Java runtimes are platform",
      "layer": "platform",
      "match": { "package": ["java", "openjdk", "jdk", "jre"] }
    }
  ]
}
```

## Internet Lifecycle Reports (no Azure access)

Two reports scrape public lifecycle sources instead of your subscriptions, so
they need internet access but no Azure credentials and no `subscriptions.csv`.

`container-eol` pulls https://endoflife.date for the base images and runtimes
container estates are usually built on - Alpine, Debian, Red Hat UBI (RHEL
lifecycle), Java (Eclipse Temurin), Python, Node.js - and groups them the way
an architect reviews them:

- `Summary`: one row per product - recommended build target, supported /
  security-only / EOL cycle counts, and the next EOL hit with days remaining.
- `EolRadar`: every live version across all products in one list, soonest EOL
  first, including versions that died in the last 180 days (`--radar-lookback-days`).
- `OsBaseImages` / `LanguageRuntimes`: full lifecycle tables per group.
- `RawLifecycle`: unmodified endoflife.date fields.

Add more endoflife.date products without code changes:

```bash
uv run python aks_report.py container-eol --products ubuntu,golang,dotnet
```

`aks-lifecycle` scrapes the Microsoft pages that announce AKS lifecycle
changes - the Learn supported-versions and integrations pages plus the weekly
`Azure/AKS` GitHub release notes (`--releases` controls the window, default 30):

- `ReleaseCalendar`: AKS preview/GA/EOL dates per Kubernetes minor, community
  and LTS tracks, with computed status (GA, EOL <90 DAYS, LTS ONLY, EOL).
- `Announcements`: retirements, deprecations and GA notices classified from the
  release notes - the rows flagged RETIREMENT/DEPRECATION are your to-do list.
- `GAFeatures` / `PreviewFeatures` / `BehaviorChanges`: what changed in the window.
- `Addons` / `OpenSourceIntegrations`: documented managed add-ons and integrations.
- `BreakingChanges` / `ComponentUpdates` / `RawReleaseNotes`: reference tabs.

## Markdown to DOCX/PDF

The launcher can also convert Markdown documentation to DOCX and PDF:

```bash
uv run python aks_report.py convert README.md --to docx
uv run python aks_report.py convert README.md --to pdf
uv run python aks_report.py convert README.md --to all --config report_style.example.yaml
```

The style is configurable through a YAML file. Start with:

```bash
cp report_style.example.yaml my_report_style.yaml
uv run python aks_report.py convert README.md --to all --config my_report_style.yaml
```

Configurable items include page size, margins, body/heading/code fonts, heading
sizes, colors, paragraph spacing, and table styling. This is intentionally like
a Terraform example file: copy it, edit values, and run the same command.

## Workbook Layout

Every workbook follows the same four-section tab layout, enforced at save time:

1. **ReadMe** (blue tab): what the report is, scope, caveats, and a "Tab
   sections" index of the workbook.
2. **Summary** (green tabs): a `Summary` tab first, then optional
   `SummaryBy<Dimension>` breakdowns.
3. **Detail** (plain tabs): findings and per-entity tables.
4. **Reference** (gray tabs): `Raw*` extracts and lookup/legend tabs
   (`PriceReference`, `SupportedVersions`, `CheckLegend`, ...), always last.

## XLSX Visualizations

Reports are still multi-sheet XLSX workbooks, and several sheets now include
native Excel charts:

- `deepdive`: daily cost and utilization trends.
- `cost`: top movers and subscription cost charts.
- `optimization`: estimated savings and cluster cost charts.

The charts are generated from workbook data, so they remain editable in Excel.

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

## Architecture Design Output

The design report creates a workbook and, unless `--no-doc` is supplied, three
companion files next to it:

- a Markdown document with Mermaid diagrams: one per-cluster architecture view
  plus a fleet-wide relationship chart (subscriptions, clusters, subnets, vnets
  and subnet attachments such as NSGs, route tables and NAT gateways);
- a `.drawio` file (open in <https://app.diagrams.net> or the draw.io desktop /
  VS Code app) with a "Fleet relationships" page and one editable architecture
  page per cluster;
- a self-contained `.html` design view (pure HTML/CSS, no JavaScript or CDN, so
  it opens in any browser even offline): the fleet overview shows subscription
  boxes with cluster cards next to VNet boxes whose subnet cards carry
  attachment chips (NSG/route table/NAT gateway) and "used by" chips
  (cluster/pool, nodes vs pods); each cluster then gets a section with nested
  subscription -> resource group boxes holding AKS/API-server/identity cards,
  node-pool cards (spot pools highlighted) and resource-count chips.

The workbook also has a `Relationships` tab listing every relationship as a
`source -> relation -> target` row (containment, node pools, subnet usage,
vnet membership, subnet attachments, co-located resources).

```bash
uv run python aks_report.py design --cluster aks-dev-01 --all
uv run python aks_report.py design --env dev
uv run python aks_report.py design --subs contoso-platform --rg rg-apps-dev
uv run python aks_report.py design --subs contoso-platform --all --no-doc
```

Scope behavior:

- `--cluster` / `--cluster-prefix` / `--cluster-contains`: designs the selected
  cluster or cluster set, including the AKS resource group, node resource group,
  node pools, network profile, referenced subnets, and nearby Azure resources.
- `--rg` / `--resource-group`: designs all Azure resources in the named resource
  group(s), and includes AKS clusters found in the selected subscription scope.
- `--subs` with no cluster/RG filter: creates a subscription-level resource
  inventory and resource-type summary.

Because this uses subscription-level ARM/Resource Graph data, it can describe
AKS control-plane state, node pools, VMSS, load balancers, public IPs, disks,
subnets, SKUs, identities, addons, and tags. It cannot see in-cluster Services,
Ingresses, namespaces, pods, or application-to-application traffic without
kubectl access.

## Dependency Baseline

`pyproject.toml` is the dependency source and `uv.lock` pins the resolved
environment for repeatable local and Docker runs on Python 3.12+:

| Package | Why it is used |
|---|---|
| `azure-identity` | Azure auth through service principal, managed identity, workload identity, or Azure CLI locally |
| `requests` | ARM, Resource Graph, Cost Management, Retail Prices REST calls |
| `pandas` | Cost and inventory aggregation before writing Excel |
| `openpyxl` | Multi-sheet XLSX generation, formatting, formulas, conditional formatting, native charts |
| `python-docx` | Markdown to DOCX export with configured styles |
| `reportlab` | Markdown to PDF export without Pandoc/LibreOffice in the Docker image |
| `PyYAML` | Human-editable style config files such as `report_style.example.yaml` |

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

## Troubleshooting

- `DefaultAzureCredential failed` -> run `az login` (and `az account set` if
  your default tenant differs).
- Cost tabs empty / 401 on cost queries -> missing Cost Management read on that
  subscription type.
- Lots of `HTTP 429 ... backing off` lines -> normal under tenant-wide load;
  the script recovers by itself.
- A subscription in the CSV that you cannot read -> Resource Graph silently
  returns nothing for it; check counts on the ReadMe tab of each report.

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
| `NodePools` | `cluster, pool, mode, vm_size, priority, count, autoscaling, min_count, max_count, zones, vnet_subnet_id, pod_subnet_id` | `aks-dev-01, sys, System, Standard_D4s_v3, Regular, 2, false, blank, blank, blank, <subnetId>, blank` | Compute shape and subnet mapping for each node pool. |
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

### CVE / Prisma Vulnerability Report

Command: `uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules vulnerability_classification.example.json`

Sheets created: `Summary`, `PrismaFindings`, `Classification`,
`Remediation`, `ByImage`, `ByPackage`, `ByLayer`, `CVEReference`,
`ClassificationRules`, `InputColumns`.

| Sheet | Sample headers | Example row | Field meaning |
|---|---|---|---|
| `Summary` | `Item, Value` | `application rows, 4` | Counts for CVEs, Prisma findings, classification layers, KEV hits, and loaded JSON classification rule files. |
| `PrismaFindings` | `sheet, row, finding_id, cve, compliance, result, severity, package, package_version, package_license, fixed_version, package_type, image, registry, repository, image_tag, hostname, distro, cvss, risk_factors, cause, image_id, vulnerability_link, purl` | `Vulnerabilities, 2, PRISMA-1, CVE-2026-1234, Vulnerability, fail, High, openssl, 3.0.1, OpenSSL, 3.0.8, OS Package, registry/app:1.0, registry, app, 1.0, host01, Ubuntu, 8.1, has fix, OS package, sha256:..., https://..., pkg:deb/ubuntu/openssl` | Normalized rows parsed from the Prisma XLSX export. Header names are matched flexibly. |
| `Classification` | `cve, package, package_type, image, layer, confidence, evidence, kev, cvss_score, cvss_severity` | `CVE-2026-1234, openssl, OS Package, registry/app:1.0, base_image, 0.85, package type/distro indicates OS package in container image, false, 8.1, HIGH` | Ownership layer and evidence for each Prisma finding or CVE row. |
| `Remediation` | `cve, layer, image, package, package_version, fixed_version, severity, kev, remediation` | `CVE-2026-1234, base_image, registry/app:1.0, openssl, 3.0.1, 3.0.8, High, false, Update the Dockerfile FROM image...` | Practical fix guidance, including Prisma fixed version and KEV action when available. |
| `ByImage` | `image, layer, findings, cves` | `registry/app:1.0, application, 3, 2` | Rollup by container image and classified layer. |
| `ByPackage` | `package, layer, findings, cves` | `openjdk-17-jre, platform, 1, 1` | Rollup by affected package/component and classified layer. |
| `ByLayer` | `layer, findings, cves` | `base_image, 5, 4` | Layer-level finding and distinct-CVE counts. |
| `CVEReference` | `cve, nvd_status, published, cvss_score, cvss_severity, cwe, cpe_parts, affected_products, kev, epss, description, references` | `CVE-2026-1234, Analyzed, 2026-01-15, 8.1, HIGH, CWE-78, a; o, debian:openssl, false, 0.12, summary, https://...` | Internet-enriched reference data from NVD/CISA KEV/EPSS, or sparse rows in `--offline` mode. |
| `ClassificationRules` | `file, type, name, layer, match` | `vulnerability_classification.example.json, classification_rule, Java runtimes are platform, platform, {"package": ["openjdk"]}` | Optional local classification rules used for the run. These are not Azure Policy. |
| `InputColumns` | `source, columns` | `prisma.xlsx, CVE ID, Severity, Package Name, Package Type, Image` | Original Prisma headers detected so you can confirm parser alignment. |
