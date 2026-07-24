# Spot Eviction Signal Verification Runbook

Verifies the load-bearing open question behind the `spot-eviction` report
(`spot_eviction.py`, subscription-Reader scope, kubectl-free): does Azure
Resource Graph's `healthresources` table actually emit a
`VirtualMachinePreempted` annotation for AKS spot **node instances** — which
run as **VMSS Uniform** orchestration mode, not Flexible — and does that
annotation survive AKS's automatic node replacement (typically within
minutes of an eviction)?

## 1. Purpose

The report's primary signal — `EvictionSnapshot`, built from
`microsoft.resourcehealth/resourceannotations` records with
`annotationName == 'VirtualMachinePreempted'` — is a **named, Platform-
Initiated eviction event**. It is materially better evidence than the
fallback signal (Activity Log VMSS delete/deallocate churn, which conflates
evictions with ordinary autoscale-down). But Microsoft's public docs on
Resource Health annotations are written mostly with VM/VMSS *Flexible*
orchestration in mind, and are silent on whether the annotation is emitted
— or retained long enough to be queryable — for VMSS *Uniform* instances,
which is the only mode AKS node pools use. If `healthresources` does not
emit this annotation for AKS nodes, or the annotation is cleared the moment
AKS replaces the evicted node (which it does automatically, fast), then the
two-signal design is dead on arrival and the report must ship in a
degraded, churn-only mode from day one. This must be checked against a real
tenant (ideally with an actual or simulated eviction) **before** the report
ships EvictionSnapshot as a trustworthy tab, not after.

## 2. Ready-to-run ARG query — tenant-wide VirtualMachinePreempted scan

Filter **only** on `annotationName`. Do not additionally constrain on
`context` or `category` string values — Microsoft's own docs disagree on
casing/spacing ("Platform Initiated" vs "Platform-Initiated" vs
"PlatformInitiated"), so an exact match on those fields can silently return
zero rows even when the annotation exists.

### Resource Graph Explorer (portal) snippet

```kql
resourcehealthresources
| where type =~ 'microsoft.resourcehealth/resourceannotations'
| where properties.annotationName =~ 'VirtualMachinePreempted'
| project targetResourceId = tostring(properties.targetResourceId),
          occurredTime = todatetime(properties.occurredTime),
          context = tostring(properties.context),
          category = tostring(properties.category),
          summary = tostring(properties.summary)
| order by occurredTime desc
```

> Note: depending on the ARG provider build, the table may surface as
> `resourcehealthresources` or `healthresources` — if the first name errors
> with "table not found", retry with `healthresources` substituted in.

### `az graph query` CLI one-liner

```bash
az graph query -q "resourcehealthresources | where type =~ 'microsoft.resourcehealth/resourceannotations' | where properties.annotationName =~ 'VirtualMachinePreempted' | project targetResourceId = tostring(properties.targetResourceId), occurredTime = todatetime(properties.occurredTime), context = tostring(properties.context), category = tostring(properties.category), summary = tostring(properties.summary) | order by occurredTime desc"
```

If this errors with "argument -q/--graph-query: ... extension required" or
similar, install the extension first:

```bash
az extension add --name resource-graph
```

By default `az graph query` scopes to subscriptions you're logged into and
have access to; add `--subscriptions <sub1> <sub2> ...` to widen or narrow
scope explicitly (Resource Health annotations are tenant/subscription
scoped, not resource-group scoped, so scope this as broadly as your Reader
access allows).

## 3. Second query — restrict to AKS node VMSS instances

Same annotation filter, narrowed to targets that look like an AKS node pool
scale-set instance (AKS-managed VMSS names are prefixed `aks-`, and the
target for a VM-scoped annotation is the individual instance under
`virtualMachines/<instanceId>`, not the VMSS resource itself):

```kql
resourcehealthresources
| where type =~ 'microsoft.resourcehealth/resourceannotations'
| where properties.annotationName =~ 'VirtualMachinePreempted'
| where properties.targetResourceId contains '/virtualMachineScaleSets/aks-'
    and properties.targetResourceId contains '/virtualMachines/'
| project targetResourceId = tostring(properties.targetResourceId),
          occurredTime = todatetime(properties.occurredTime),
          context = tostring(properties.context),
          category = tostring(properties.category),
          summary = tostring(properties.summary)
| order by occurredTime desc
```

```bash
az graph query -q "resourcehealthresources | where type =~ 'microsoft.resourcehealth/resourceannotations' | where properties.annotationName =~ 'VirtualMachinePreempted' | where properties.targetResourceId contains '/virtualMachineScaleSets/aks-' and properties.targetResourceId contains '/virtualMachines/' | project targetResourceId = tostring(properties.targetResourceId), occurredTime = todatetime(properties.occurredTime), context = tostring(properties.context), category = tostring(properties.category), summary = tostring(properties.summary) | order by occurredTime desc"
```

If query #2 returns rows that query #1 also returns (i.e. the tenant-wide
scan already contains AKS-shaped target IDs), that's a strong positive
signal on its own, even before you force a fresh eviction.

## 4. Forcing / observing an eviction in the sandbox

Resource Health data can be sparse if nothing has been evicted recently, so
the reliable path is to force one and immediately re-poll.

**Using this repo's sandbox tooling (preferred):** the sandbox family
already has `sandbox spot-sim`, which deploys a spot node pool plus a
descheduler and can drive a VMSS `simulateEviction` call against a sandbox
node instance as part of its scenario matrix. Run it against the sandbox
spot pool, note the timestamp and the target instance resource ID it acts
on, then immediately re-run the two ARG queries above (both the tenant-wide
scan and the AKS-VMSS-scoped one) and check for a matching row.

**Raw Azure primitives**, if you want to trigger an eviction outside
`spot-sim` or need the underlying commands it wraps:

```bash
# VMSS instance (this is what AKS node pools use — Uniform orchestration)
az vmss simulate-eviction \
  --resource-group <node-resource-group> \
  --name <vmss-name> \
  --instance-id <instance-id>

# Standalone VM equivalent (not the AKS case, but useful to sanity-check
# the annotation mechanism works at all, isolating whether a negative
# result is Uniform-VMSS-specific)
az vm simulate-eviction \
  --resource-group <rg> \
  --name <vm-name>
```

A real capacity- or price-driven eviction (i.e. Azure evicts the spot
instance on its own, not simulated) also produces the signal and is worth
capturing opportunistically if one happens during testing — it rules out
any doubt that `simulate-eviction` behaves differently from a genuine
preemption.

Re-poll on a short interval (e.g. every 30–60s for the first 10 minutes)
immediately after the eviction, since AKS starts replacing the evicted node
right away and the open question includes whether the annotation is still
present once that replacement lands.

## 5. Interpreting results

| Observation | Conclusion | Action |
|---|---|---|
| Query #3 (AKS-VMSS-scoped) returns rows matching your simulated/observed eviction, and they're still present a few minutes later | `healthresources` **does** emit for Uniform VMSS AKS nodes | Two-signal mode confirmed; `EvictionSnapshot` is a trustworthy primary signal, ship as designed |
| Zero rows in either query, even polled immediately after a known eviction | `healthresources` does **not** emit for Uniform VMSS, or the annotation is too ephemeral to ever observe | Report degrades to Activity-Log-churn-only; document this loudly in the report's ReadMe tab, treat `--no-eviction-scan` as the expected default posture, and lean on `ChurnTrend` (VMSS delete/deallocate proxy, same caveat as `spot_savings`'s `vmss_churn_approx` — it conflates evictions with autoscale-down) |
| Rows appear right after the eviction, then vanish within minutes on re-poll | Confirms snapshot ephemerality | Document that `EvictionSnapshot` only shows currently/very-recently-preempted instances — it is a point-in-time snapshot, not history. See §6 for how to get real history |

## 6. Persistence check — getting history beyond the live snapshot

`healthresources` in ARG reflects **current** resource health state; it is
not designed as a history table. Two durable fallbacks:

- **ARG change tracking (~14 days):** query `resourcechanges` (or the
  Resource Graph "change history" / availability change-tracking API) for
  the same target resource IDs to see whether a health/annotation change
  was recorded around the eviction timestamp. This gives you roughly a
  2-week lookback window at Reader scope, no extra services required.
- **Activity Log (~90 days):** the same source `spot_savings`/
  `spot_eviction` already use for the churn fallback
  (`armextras.vmss_churn_events`) — VMSS delete/deallocate ops on the node
  RG. Longer retention than ARG change tracking, but it's a churn proxy,
  not a clean eviction signal.

For real long-term eviction history, the durable answer is **piping
Resource Health events to Log Analytics** (via a diagnostic
setting/Activity Log export or the Resource Health alerts-to-Log-Analytics
path) and querying them there with normal retention policies — that is out
of scope for this Reader-only, kubectl-free report, but worth a one-line
pointer for anyone who wants eviction history beyond what ARG can hold.

## 7. Caveats

- Everything above stays at subscription-**Reader** scope and is
  **kubectl-free**, matching the rest of this report family — no
  in-cluster access is used or required.
- True on-node eviction visibility (Scheduled Events via IMDS, polled from
  inside the node) is explicitly **out of scope by design**. This runbook
  and the report it backs only ever see what Azure Resource Health and the
  Activity Log expose at the control-plane/API level.
- Findings from this verification should be logged back into the report's
  own documentation (its ReadMe tab / this repo's `CLAUDE.md`) once
  confirmed, per this repo's convention of keeping the codebase map
  accurate — whichever branch of §5 turns out true changes what
  `EvictionSnapshot` is allowed to claim.
