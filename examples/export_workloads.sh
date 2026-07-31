#!/usr/bin/env bash
# Read-only export of workload state for spot-readiness analysis.
#
# SAFETY: this script only ever runs `kubectl get`. It performs no create,
# apply, patch, delete, scale, exec or port-forward. It can be read top to
# bottom in under a minute to confirm that.
#
# It does NOT export Secrets or ConfigMaps. Note however that Deployment specs
# can carry credentials inline in `env:` values - see the REDACTION note at the
# bottom before sending the archive on.
#
# Usage:
#   ./EXPORT.sh                     # all namespaces
#   ./EXPORT.sh team-a team-b       # only these namespaces
#
# Produces ./spot-export-<cluster>-<date>/ and a .tar.gz of the same.

set -euo pipefail

OUT="spot-export-$(kubectl config current-context 2>/dev/null | tr -c 'a-zA-Z0-9_.-' '-' | cut -c1-40)-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

if [ "$#" -gt 0 ]; then
  NS_ARGS=(); for ns in "$@"; do NS_ARGS+=(-n "$ns"); done
  SCOPE_DESC="namespaces: $*"
else
  NS_ARGS=(-A)
  SCOPE_DESC="all namespaces"
fi

echo "Exporting ($SCOPE_DESC) -> $OUT/"

# get <label> <file> <kubectl args...>
# Never fails the run: a missing RBAC grant or absent resource kind is recorded
# as a .skipped file so the analysis knows the difference between "none exist"
# and "we were not allowed to look".
get() {
  local label="$1" file="$2"; shift 2
  if kubectl get "$@" -o yaml > "$OUT/$file" 2>"$OUT/$file.err"; then
    rm -f "$OUT/$file.err"
    printf '  ok       %s\n' "$label"
  else
    mv "$OUT/$file.err" "$OUT/$file.skipped" 2>/dev/null || true
    rm -f "$OUT/$file"
    printf '  SKIPPED  %s (see %s.skipped)\n' "$label" "$file"
  fi
}

# --- context -------------------------------------------------------------
kubectl version -o yaml           > "$OUT/version.yaml"      2>/dev/null || true
echo "$SCOPE_DESC"                > "$OUT/scope.txt"
date -u +%Y-%m-%dT%H:%M:%SZ       > "$OUT/exported_at.txt"

# --- workloads -----------------------------------------------------------
get "deployments"   deployments.yaml   deploy "${NS_ARGS[@]}"
get "statefulsets"  statefulsets.yaml  sts    "${NS_ARGS[@]}"
get "daemonsets"    daemonsets.yaml    ds     "${NS_ARGS[@]}"
get "cronjobs"      cronjobs.yaml      cronjob "${NS_ARGS[@]}"

# --- the ones that actually explain spot failures ------------------------
# PDB carries status.disruptionsAllowed / currentHealthy / desiredHealthy.
# This is THE most important file in the bundle: replicas=1 + minAvailable=1
# yields disruptionsAllowed=0, which blocks drain, descheduler and autoscaler
# consolidation while giving zero protection against involuntary preemption.
get "poddisruptionbudgets" pdb.yaml pdb "${NS_ARGS[@]}"

# HPA is needed to avoid false positives: a Deployment with replicas=1 that is
# HPA-managed is not necessarily a singleton.
get "horizontalpodautoscalers" hpa.yaml hpa "${NS_ARGS[@]}"

# Pods give spec.nodeName (where the workload ACTUALLY landed, vs where its
# spec says it should), restartCount and lastState.terminated.reason.
get "pods" pods.yaml pods "${NS_ARGS[@]}"

# PVCs + StorageClasses: a singleton on a RWO Azure Disk has a multi-minute
# detach/attach on node loss, which spot turns into a routine event.
get "persistentvolumeclaims" pvc.yaml pvc "${NS_ARGS[@]}"

# --- cluster-scoped (may need cluster-reader; skipping is tolerated) ------
# Node labels are how we tell spot from on-demand:
#   kubernetes.azure.com/scalesetpriority=spot
#   kubernetes.azure.com/agentpool=<pool>
# plus the auto-applied taint scalesetpriority=spot:NoSchedule.
get "nodes"            nodes.yaml            nodes
get "priorityclasses"  priorityclasses.yaml  priorityclasses
get "storageclasses"   storageclasses.yaml   storageclasses

# --- best effort ---------------------------------------------------------
# Events default to a ~1h TTL, so this is a snapshot of the last hour only.
# Absence of eviction/preemption events here proves nothing.
get "events" events.yaml events "${NS_ARGS[@]}"

tar -czf "$OUT.tar.gz" "$OUT" 2>/dev/null && echo && echo "Archive: $OUT.tar.gz"
echo "Directory: $OUT/"
echo
echo "REDACTION: Deployment/StatefulSet specs can contain credentials in inline"
echo "env: values. Please skim $OUT/deployments.yaml before sending, or run:"
echo "  grep -niE 'password|secret|token|apikey|connectionstring' $OUT/*.yaml"
