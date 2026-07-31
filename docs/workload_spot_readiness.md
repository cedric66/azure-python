# Workload spot readiness

_[← Back to README](../README.md)_

`workload-spot` answers two questions from exported Kubernetes YAML alone, with
**no cluster access and no Azure scope**:

1. **Why does this workload fail on a spot node pool?** — usually the
   singleton / PDB-deadlock class, or a placement mistake that leaves pods Pending.
2. **Which on-demand workloads are safe to move to spot?**

Every other report in this toolkit reads Azure at Reader scope. This one reads a
directory of YAML. It runs offline, on a laptop, against a bundle someone else
exported for you.

```bash
uv run python aks_report.py workload-spot --bundle workloads/spot-export-prod-20260731.tar.gz
uv run python aks_report.py workload-spot --bundle workloads/ --namespace team-a,team-b
```

`--bundle` takes a directory, a `.tar.gz` of one, or a single multi-doc YAML file.

| Flag | Default | Meaning |
|---|---|---|
| `--bundle` | *(required)* | Directory, `.tar.gz`, or single multi-doc YAML file |
| `--namespace` | all | Comma-separated namespace filter |
| `--notice-seconds` | `30` | Preemption notice budget; grace periods above it trip SPOT-040 |
| `--out` | `out/` | Output directory |

## Why YAML is enough

`kubectl get <kind> -o yaml` includes the **status subresource**. That single
fact is what makes an offline bundle worth analysing rather than a lossy
snapshot of intent:

| Field, in the export | What it proves |
|---|---|
| `PodDisruptionBudget.status.disruptionsAllowed` | The **actual** live disruption budget, not the declared one. `0` is the smoking gun. |
| `PodDisruptionBudget.status.currentHealthy` / `desiredHealthy` | Whether the budget is zero because of a config deadlock or a transient outage |
| `Pod.spec.nodeName` | Where each replica really landed — so "all 4 replicas on one spot pool" is observed, not inferred |
| `Pod.status.conditions[PodScheduled]` | The scheduler's own message for a Pending pod, verbatim |
| `Pod.status.containerStatuses[].restartCount` + `lastState.terminated` | Restart history consistent with node reclaim (`exitCode: 137`) |
| `Node.spec.taints` / `metadata.labels` | Which pools are actually spot, in this cluster, right now |

So the report grades each finding by what it rests on: `spec` (would be true of
the YAML anywhere) or `status` (observed in this cluster at export time). The
`status_evidence` column on **WorkloadRisk** and `based_on` on **Findings** carry
this, and it is the difference between "this is risky in principle" and "this
broke last Tuesday".

## Getting a bundle

Send `examples/export_workloads.sh` to whoever has cluster access:

```bash
./export_workloads.sh                  # whole cluster
./export_workloads.sh team-a team-b    # named namespaces only
```

It is strictly read-only — `kubectl get` only, no mutating verbs — so it passes
change control, and it exports **no Secrets and no ConfigMaps**. Anything RBAC
denies is written as a `<file>.skipped` marker instead of being silently
dropped, so the **Coverage** tab can tell "there are none" from "we were not
allowed to look". The script prints a redaction check before you share the
tarball.

Drop the result in `workloads/` (gitignored — live exports carry env vars,
internal hostnames and private registry paths). Sanitised fixtures used by the
test suite live in `tests/fixtures/workloads/`.

## Tabs

| Tab | What it is |
|---|---|
| `ReadMe` | Method, limitations, what a missing file costs you |
| `Scorecard` | KPI cards: workloads scored, blocked on spot, spot candidates, singletons on spot, zero-budget PDBs |
| `WorkloadRisk` | **The report.** One row per workload with every input pre-joined: verdict, replicas, HPA floor, pods observed and how many are on spot, the PDB and its live budget, RWO volumes, grace period, spread keys, risk score, and the rules that fired |
| `Findings` | One row per (workload, rule): evidence, remediation, `based_on`, and `applies_when` (`now` vs `if moved to spot`) |
| `RemediationGuide` | The YAML to apply, per rule that actually fired — nothing for rules that did not |
| `NodeInventory` | Spot vs on-demand nodes as seen in the bundle: pool, SKU, zone, taints |
| `Coverage` | What the export contained, what is `MISSING`, what is `SKIPPED` (RBAC), and the analytical cost of each gap |

## Verdicts

A workload is graded on the spot path if it **tolerates** the spot taint *or*
**targets** spot (nodeSelector/affinity) — a workload pinned to spot without the
toleration is already broken there and must never be offered as an on-demand
keeper. Otherwise it is scored hypothetically, as a candidate.

| Verdict | Meaning |
|---|---|
| `BLOCKED ON SPOT` | On the spot path with a CRITICAL finding |
| `AT RISK ON SPOT` | On the spot path with a HIGH finding |
| `SPOT OK` | On the spot path, nothing above MEDIUM |
| `KEEP ON DEMAND` | On-demand, and moving it would raise a CRITICAL |
| `SPOT WITH CHANGES` | On-demand, movable once the HIGH findings are fixed |
| `SPOT CANDIDATE` | On-demand and safe to move as-is |

`risk_score` is additive over findings: CRITICAL 5, HIGH 3, MEDIUM 1, LOW 0. It
ranks, it does not threshold — the verdict comes from the worst single finding,
not the sum, so ten LOWs never outrank one CRITICAL.

## Rule catalog

`based_on: status` rules need the pod/PDB status in the bundle; `spec` rules
fire from the manifests alone.

### Placement (SPOT-001…006)

| Rule | Sev | Based on | Finding |
|---|---|---|---|
| SPOT-002 | CRITICAL | spec | Spread key exists only on spot nodes |
| SPOT-006 | CRITICAL | spec | Targets spot capacity without tolerating its taint |
| SPOT-001 | HIGH | spec | Replicas can all land on one spot pool |
| SPOT-003 | HIGH | spec | Hard-pinned to spot with no on-demand fallback |
| SPOT-005 | MEDIUM | spec | Blanket toleration makes spot eligibility accidental |

**SPOT-002 is the classic mistake.** A `topologySpreadConstraints` entry keyed on
`kubernetes.azure.com/scalesetpriority` looks like it spreads across spot and
on-demand. It does not: that label exists **only on spot nodes**, so on-demand
nodes have no value for the key and form no topology domain at all. Every
replica can pile onto one spot pool while the constraint reports satisfied.
Spread on `kubernetes.azure.com/agentpool` (or `topology.kubernetes.io/zone`)
instead.

### Availability — the singleton class (SPOT-010…014)

| Rule | Sev | Based on | Finding |
|---|---|---|---|
| SPOT-010 | CRITICAL | spec | Single replica on preemptible capacity |
| SPOT-011 | CRITICAL | spec | PDB permits zero voluntary disruptions |
| SPOT-012 | MEDIUM | spec | Recreate strategy compounds a singleton restart |
| SPOT-014 | MEDIUM | spec | PDB matches no workload in the bundle |
| SPOT-013 | LOW | spec | Singleton has no PodDisruptionBudget |

**This is the answer to "why is my single-replica pod failing on spot".**
`replicas: 1` with a PDB of `minAvailable: 1` yields
`status.disruptionsAllowed: 0`, and that is the worst of both worlds:

- **It gives zero protection against preemption.** A spot reclaim is an
  *involuntary* disruption. The kubelet is told the node is going; the PDB is
  not consulted. The pod dies either way.
- **It blocks everything voluntary.** Node drain, upgrades, the descheduler and
  cluster-autoscaler consolidation all respect PDBs, so they stall on this
  workload — the pod gets stranded on the node nobody can drain.

Raising `minAvailable` does not help. Lowering it to `0` only unblocks drains;
the singleton still dies on reclaim. The only real fix is **two replicas plus a
spread constraint on `kubernetes.azure.com/agentpool`** — then `minAvailable: 1`
finally means something. If the workload genuinely cannot run two replicas
(a leader-elected singleton, a licence-bound process), it does not belong on
spot; keep it on on-demand.

### State (SPOT-020…021)

| Rule | Sev | Based on | Finding |
|---|---|---|---|
| SPOT-020 | HIGH | spec | ReadWriteOnce volume on preemptible capacity |
| SPOT-021 | HIGH | spec | StatefulSet on preemptible capacity |

An RWO disk detaches and reattaches on reclaim, and StatefulSets recover
serially — both turn a ~30s preemption into minutes.

### Priority and shutdown (SPOT-030…042)

| Rule | Sev | Based on | Finding |
|---|---|---|---|
| SPOT-031 | HIGH | spec | Cluster-critical workload tolerates spot |
| SPOT-040 | HIGH | spec | Grace period exceeds the preemption notice |
| SPOT-042 | MEDIUM | spec | No readiness probe on a spot-eligible workload |
| SPOT-030 | LOW | spec | No priorityClass to order recovery |
| SPOT-041 | LOW | spec | No preStop hook to drain in-flight requests |

Azure gives roughly **30 seconds** of preemption notice. A
`terminationGracePeriodSeconds` above that is not honoured — the pod is
SIGKILLed when the node goes. `--notice-seconds` tunes the budget.

### Batch (SPOT-050)

| Rule | Sev | Based on | Finding |
|---|---|---|---|
| SPOT-050 | MEDIUM | spec | Batch job cannot survive one preemption |

`backoffLimit: 0` on spot means a single reclaim fails the job permanently.

### Observed state (SPOT-060…064)

| Rule | Sev | Based on | Finding |
|---|---|---|---|
| SPOT-060 | CRITICAL | status | PDB is blocking disruptions right now |
| SPOT-061 | CRITICAL | status | Singleton is running on a spot node right now |
| SPOT-063 | CRITICAL | status | Pod is Pending and unschedulable |
| SPOT-064 | HIGH | status | All replicas concentrated on one spot pool |
| SPOT-062 | MEDIUM | status | Restart pattern consistent with reclaim |

These are the ones worth leading a conversation with: they are not "your YAML
could bite you", they are "here is what it did".

## False positives to expect

- **HPA-managed workloads.** `replicas: 1` in a Deployment that an HPA drives to
  a floor of 2 is not a singleton. The report reads the HPA's `minReplicas` and
  reports the effective floor; `hpa_managed` on WorkloadRisk tells you when this
  applied.
- **Deliberate singletons.** Leader-elected controllers and licence-bound
  processes are correctly flagged and correctly *not* moved to spot. Fix is
  "keep on on-demand", not "add a replica".
- **`SPOT-014` orphan PDBs.** A PDB whose selector matches nothing in the bundle
  may simply match a workload in a namespace you did not export. Check
  **Coverage** before deleting anything.
- **`SPOT-062` restart counts.** `exitCode: 137` is SIGKILL — reclaim, OOMKill
  and a failed liveness probe all look alike. It is a correlation prompt, not a
  verdict. Cross-check against `spot-eviction`, which reads Azure-side
  preemption and VMSS churn.

## What YAML cannot tell you

- **Actual eviction events.** Spot preemption is only visible on-node
  (Scheduled Events / IMDS) or Azure-side. Use
  [`spot-eviction`](spot.md) for that — it reads Resource Health and node-RG
  VMSS churn at Reader scope, and pairs naturally with this report: this one
  says *which workloads cannot survive a reclaim*, that one says *how often
  reclaims happen on those pools*.
- **Whether resource requests fit the spot SKU.** Requests are in the export;
  allocatable capacity per node is only meaningful with live utilization.
- **Anything about a workload not in the bundle.** The Coverage tab is not
  decoration — a missing `pdb.yaml` silently removes the entire SPOT-011/060
  finding class.

## Constraints

Every remediation this report emits is **kube-scheduler + cluster autoscaler +
descheduler + topologySpreadConstraints**. No Karpenter/NAP, no Cilium — the
org does not run them, so nothing here assumes them.

## Testing

```bash
uv run python tests/smoke_test.py    # includes chk_workload_spot
```

The fixture bundle in `tests/fixtures/workloads/` is a sanitised 12-file export
that exercises the interesting paths: a singleton behind a zero-budget PDB, a
`scalesetpriority` spread key, a workload pinned to spot with no toleration
(both pods Pending), an HPA-floored "singleton", a stateful singleton with an
RWO claim, a clean on-demand candidate, an orphan PDB, and an
`events.yaml.skipped` marker for the Coverage RBAC path.
