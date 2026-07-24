# Sandbox AKS & policy testing

_[← Back to the README](../README.md)_

The `sandbox` command family: deploy a throwaway cluster, run Gatekeeper policy tests, clone/impact against the fleet, simulate spot conversion, rehearse upgrades, and check fleet drift.

## Sandbox AKS and Policy Testing

For the Azure sandbox where you have admin access, use the same launcher with
the `sandbox` command. The sandbox workflow is driven by a separate config file
so you can explicitly supply the subscription, resource group, AKS node pools,
and policies to test.

Start by copying the example. Both JSON and YAML configs are supported
(`load_config` picks the parser by extension) - if you prefer JSON throughout,
use `examples/sandbox.example.json`; policy definitions under `policies/` are plain
JSON either way:

```bash
cp examples/sandbox.example.json sandbox.json     # JSON workflow
cp examples/sandbox.example.yaml sandbox.yaml     # or YAML, same schema
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
cp examples/sandbox.example.yaml golden.yaml          # edit down to your baseline keys
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
