"""Workload spot-readiness analysis from an offline kubectl export bundle.

Answers two questions from YAML alone, with no cluster access and no Azure scope:

  1. Why does a workload fail on a spot node pool? (the singleton / PDB-deadlock
     class of failure, plus placement mistakes that leave pods Pending)
  2. Which on-demand workloads are safe to move to spot?

Input is a bundle produced by examples/export_workloads.sh - a directory, a
.tar.gz of one, or a single multi-doc YAML file. Because `kubectl get -o yaml`
includes the status subresource, an offline bundle still carries the evidence
that matters: PodDisruptionBudget status.disruptionsAllowed, pod
spec.nodeName, restart counts and scheduling conditions.

Tabs: ReadMe, Scorecard, WorkloadRisk, Findings, RemediationGuide,
NodeInventory, Coverage.

Org constraint: no Karpenter/NAP and no Cilium - every remediation here is
kube-scheduler + cluster autoscaler + descheduler + topologySpreadConstraints.

Usage:
  python aks_report.py workload-spot --bundle spot-export-prod-20260731.tar.gz
  python aks_report.py workload-spot --bundle workloads/ --namespace team-a,team-b
"""
import argparse
import datetime as dt
import math
import os
import tarfile
import tempfile

import pandas as pd
import yaml

from azrep import excel
from azrep.http_client import log

SPOT_LABEL = "kubernetes.azure.com/scalesetpriority"
POOL_LABEL = "kubernetes.azure.com/agentpool"
ZONE_LABEL = "topology.kubernetes.io/zone"
SPOT_TAINT = "kubernetes.azure.com/scalesetpriority"

# Azure gives a spot node ~30s of preemption notice. A pod that asks for longer
# is not gracefully drained - it is SIGKILLed when the node goes.
DEFAULT_NOTICE_SECONDS = 30

# Spread keys that actually separate failure domains. scalesetpriority is NOT
# one of them: that label exists only on spot nodes, so on-demand nodes have no
# value for the key and fall outside every topology domain.
GOOD_SPREAD_KEYS = (POOL_LABEL, "agentpool", "kubernetes.io/hostname", ZONE_LABEL,
                    "failure-domain.beta.kubernetes.io/zone")

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job")

SEVERITY_WEIGHT = {"CRITICAL": 5, "HIGH": 3, "MEDIUM": 1, "LOW": 0}

# Files the export script writes, and what analysis loses when one is missing.
BUNDLE_FILES = [
    ("deployments.yaml", "Deployment", "no Deployment analysis at all"),
    ("statefulsets.yaml", "StatefulSet", "stateful singletons invisible"),
    ("daemonsets.yaml", "DaemonSet", "per-node agents invisible"),
    ("cronjobs.yaml", "CronJob", "batch retry-on-preemption checks skipped"),
    ("pdb.yaml", "PodDisruptionBudget",
     "PDB deadlock cannot be detected - this is the most common spot blocker"),
    ("hpa.yaml", "HorizontalPodAutoscaler",
     "HPA-managed replicas=1 workloads are misread as singletons (false positives)"),
    ("pods.yaml", "Pod",
     "no live placement: cannot tell where a pod actually landed, only where its spec allows"),
    ("pvc.yaml", "PersistentVolumeClaim", "ReadWriteOnce detach/attach risk not scored"),
    ("nodes.yaml", "Node",
     "spot vs on-demand nodes unknown - every placement finding degrades to spec-only"),
    ("priorityclasses.yaml", "PriorityClass", "preemption-order checks skipped"),
    ("storageclasses.yaml", "StorageClass", "volume backing type unknown"),
    ("events.yaml", "Event", "recent scheduling failures unavailable (~1h TTL anyway)"),
]


# --------------------------------------------------------------------------
# bundle loading
# --------------------------------------------------------------------------

def _docs(text):
    out = []
    for d in yaml.safe_load_all(text):
        if isinstance(d, dict):
            out.append(d)
    return out


def _flatten(doc):
    """kubectl -o yaml over many objects returns kind: List with .items."""
    if str(doc.get("kind", "")).endswith("List") and isinstance(doc.get("items"), list):
        return [i for i in doc["items"] if isinstance(i, dict)]
    return [doc]


def load_bundle(path):
    """-> (objects_by_kind, meta). Accepts a directory, a .tar.gz or one YAML file."""
    meta = {"source": path, "exported_at": "", "scope": "", "files": {}, "tmpdir": None}
    if os.path.isfile(path) and path.endswith((".tar.gz", ".tgz")):
        tmp = tempfile.mkdtemp(prefix="spot-bundle-")
        with tarfile.open(path) as tf:
            tf.extractall(tmp, filter="data")
        entries = [os.path.join(tmp, e) for e in os.listdir(tmp)]
        dirs = [e for e in entries if os.path.isdir(e)]
        path, meta["tmpdir"] = (dirs[0] if len(dirs) == 1 else tmp), tmp

    objs = {}
    if os.path.isfile(path):
        files = [path]
    else:
        files = sorted(os.path.join(path, f) for f in os.listdir(path)
                       if f.endswith((".yaml", ".yml", ".skipped")))
        for f in ("exported_at.txt", "scope.txt"):
            p = os.path.join(path, f)
            if os.path.exists(p):
                meta[f.split(".")[0]] = open(p).read().strip()

    for f in files:
        base = os.path.basename(f)
        if base.endswith(".skipped"):
            meta["files"][base[:-len(".skipped")]] = "SKIPPED"
            continue
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
            found = 0
            for doc in _docs(text):
                for o in _flatten(doc):
                    kind = str(o.get("kind") or "")
                    if kind and kind != "List":
                        objs.setdefault(kind, []).append(o)
                        found += 1
            meta["files"][base] = "ok (%d objects)" % found
        except yaml.YAMLError as e:
            meta["files"][base] = "PARSE ERROR: %s" % str(e).splitlines()[0][:120]
    return objs, meta


# --------------------------------------------------------------------------
# small accessors
# --------------------------------------------------------------------------

def _md(o):
    return o.get("metadata") or {}


def _labels(o):
    return _md(o).get("labels") or {}


def ns_name(o):
    return "%s/%s" % (_md(o).get("namespace") or "default", _md(o).get("name") or "")


def selector_matches(sel, labels):
    """Standard LabelSelector semantics (matchLabels + matchExpressions)."""
    if not isinstance(sel, dict):
        return False
    labels = labels or {}
    for k, v in (sel.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for exp in sel.get("matchExpressions") or []:
        key, op = exp.get("key"), exp.get("operator")
        vals = exp.get("values") or []
        if op == "In" and labels.get(key) not in vals:
            return False
        if op == "NotIn" and labels.get(key) in vals:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def index_nodes(nodes):
    """-> {node_name: {pool, spot, zone, instance_type, taints}}"""
    idx = {}
    for n in nodes:
        lab = _labels(n)
        idx[_md(n).get("name") or ""] = {
            "pool": lab.get(POOL_LABEL) or lab.get("agentpool") or "",
            "spot": str(lab.get(SPOT_LABEL, "")).lower() == "spot",
            "zone": lab.get(ZONE_LABEL) or "",
            "instance_type": lab.get("node.kubernetes.io/instance-type") or "",
            "taints": [t.get("key") for t in ((n.get("spec") or {}).get("taints") or [])],
        }
    return idx


def tolerates_spot(tolerations):
    """True when the pod can land on a spot-tainted node (explicitly or via a blanket)."""
    for t in tolerations or []:
        key, op = t.get("key"), t.get("operator") or "Equal"
        if not key and op == "Exists":
            return True          # blanket toleration: tolerates every taint
        if key == SPOT_TAINT:
            return op == "Exists" or str(t.get("value", "")).lower() == "spot"
    return False


def _selector_terms(spec):
    """All (key, values) node constraints from nodeSelector + REQUIRED nodeAffinity."""
    out = []
    for k, v in (spec.get("nodeSelector") or {}).items():
        out.append((k, [str(v)], "nodeSelector"))
    aff = ((spec.get("affinity") or {}).get("nodeAffinity") or {})
    req = aff.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    for term in req.get("nodeSelectorTerms") or []:
        for exp in term.get("matchExpressions") or []:
            if exp.get("operator") in ("In", "Exists"):
                out.append((exp.get("key"), [str(v) for v in exp.get("values") or []],
                            "required nodeAffinity"))
    return out


def targets_spot(spec, spot_pools):
    """-> (bool, evidence). Hard-pinned to spot capacity with no on-demand fallback."""
    for key, vals, where in _selector_terms(spec):
        if key == SPOT_LABEL and (not vals or "spot" in [v.lower() for v in vals]):
            return True, "%s pins %s" % (where, SPOT_LABEL)
        if key in (POOL_LABEL, "agentpool") and vals and all(v in spot_pools for v in vals):
            return True, "%s pins agentpool %s (spot pool)" % (where, ",".join(vals))
    return False, ""


def spread_keys(spec):
    keys = [str(c.get("topologyKey") or "") for c in
            (spec.get("topologySpreadConstraints") or [])]
    anti = ((spec.get("affinity") or {}).get("podAntiAffinity") or {})
    for field in ("requiredDuringSchedulingIgnoredDuringExecution",
                  "preferredDuringSchedulingIgnoredDuringExecution"):
        for t in anti.get(field) or []:
            term = t.get("podAffinityTerm") or t
            keys.append(str(term.get("topologyKey") or ""))
    return [k for k in keys if k]


def pdb_allowed(pdb, replicas):
    """Disruptions a PDB permits at `replicas`, computed from spec (-> int or None).

    replicas=1 + minAvailable=1 yields 0: the classic deadlock that blocks drain,
    descheduler and autoscaler consolidation while giving zero protection against
    preemption, which is involuntary and ignores PDBs entirely.
    """
    spec = pdb.get("spec") or {}
    if replicas is None:
        return None
    mn, mx = spec.get("minAvailable"), spec.get("maxUnavailable")
    if mn is not None:
        if isinstance(mn, str) and mn.endswith("%"):
            return replicas - math.ceil(replicas * float(mn[:-1]) / 100.0)
        return replicas - int(mn)
    if mx is not None:
        if isinstance(mx, str) and mx.endswith("%"):
            return math.floor(replicas * float(mx[:-1]) / 100.0)
        return int(mx)
    return None


def template_spec(o):
    """Pod spec out of any workload kind (CronJob nests two levels deeper)."""
    spec = o.get("spec") or {}
    if o.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
    tmpl = spec.get("template") or {}
    return tmpl.get("spec") or {}, _labels(tmpl)


# --------------------------------------------------------------------------
# workload assembly
# --------------------------------------------------------------------------

def build_workloads(objs, nodes, namespaces=()):
    """-> list of workload dicts with everything the rules need pre-joined."""
    pods = objs.get("Pod") or []
    pdbs = objs.get("PodDisruptionBudget") or []
    hpas = objs.get("HorizontalPodAutoscaler") or []
    pvcs = {ns_name(p): p for p in objs.get("PersistentVolumeClaim") or []}
    spot_pools = {v["pool"] for v in nodes.values() if v["spot"] and v["pool"]}

    out = []
    for kind in WORKLOAD_KINDS:
        for o in objs.get(kind) or []:
            ns = _md(o).get("namespace") or "default"
            if namespaces and ns not in namespaces:
                continue
            spec = o.get("spec") or {}
            pspec, plabels = template_spec(o)
            sel = spec.get("selector") or {}

            hpa = None
            for h in hpas:
                ref = ((h.get("spec") or {}).get("scaleTargetRef") or {})
                if (_md(h).get("namespace") or "default") == ns and \
                        ref.get("kind") == kind and ref.get("name") == _md(o).get("name"):
                    hpa = h
                    break

            if kind == "DaemonSet":
                replicas = None
            elif kind in ("CronJob", "Job"):
                replicas = 1
            else:
                replicas = spec.get("replicas")
                replicas = 1 if replicas is None else int(replicas)
            if hpa is not None:
                replicas = int((hpa.get("spec") or {}).get("minReplicas") or 1)

            # An empty selector matches EVERY pod under LabelSelector semantics,
            # which is right for a PDB but wrong here: CronJob/Job carry no
            # spec.selector, so an unguarded match would hand them the whole
            # namespace's pods. Fall back to the pod template labels instead.
            has_sel = bool(sel.get("matchLabels") or sel.get("matchExpressions"))
            mine = [p for p in pods
                    if (_md(p).get("namespace") or "default") == ns
                    and ((has_sel and selector_matches(sel, _labels(p)))
                         or (plabels and _labels(p) and
                             all(_labels(p).get(k) == v for k, v in plabels.items())))]
            my_pdbs = [b for b in pdbs
                       if (_md(b).get("namespace") or "default") == ns
                       and selector_matches((b.get("spec") or {}).get("selector") or {},
                                            plabels)]

            claims = []
            for v in pspec.get("volumes") or []:
                c = (v.get("persistentVolumeClaim") or {}).get("claimName")
                if c:
                    claims.append(pvcs.get("%s/%s" % (ns, c)))
            for vct in spec.get("volumeClaimTemplates") or []:
                claims.append(vct)
            rwo = [c for c in claims if c and "ReadWriteOnce" in
                   ((c.get("spec") or {}).get("accessModes") or [])]

            placed = [nodes.get((p.get("spec") or {}).get("nodeName") or "")
                      for p in mine]
            placed = [n for n in placed if n]
            out.append({
                "kind": kind, "namespace": ns, "name": _md(o).get("name") or "",
                "replicas": replicas, "hpa": hpa is not None,
                "spec": pspec, "workload_spec": spec, "pod_labels": plabels,
                "tolerates_spot": tolerates_spot(pspec.get("tolerations")),
                "targets_spot": targets_spot(pspec, spot_pools)[0],
                "spot_pools": spot_pools,
                "pdbs": my_pdbs, "pods": mine, "rwo_claims": rwo,
                "nodes_placed": placed,
                "on_spot": sum(1 for n in placed if n["spot"]),
                "pools_used": sorted({n["pool"] for n in placed if n["pool"]}),
            })
    return out


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def _r(rid, sev, title, evidence, fix, needs="spec"):
    return {"rule_id": rid, "severity": sev, "title": title, "evidence": evidence,
            "remediation": fix, "based_on": needs}


def evaluate(w, assume_spot=False):
    """-> list of findings for one workload. `assume_spot` scores an on-demand
    workload as if it were moved to spot (that is the SPOT CANDIDATE screen)."""
    f = []
    spec, kind, reps = w["spec"], w["kind"], w["replicas"]
    spot = w["tolerates_spot"] or assume_spot
    singleton = reps == 1 and kind in ("Deployment", "StatefulSet")

    # --- placement -------------------------------------------------------
    bad_keys = [k for k in spread_keys(spec) if SPOT_LABEL in k]
    if bad_keys:
        f.append(_r("SPOT-002", "CRITICAL", "Spread key exists only on spot nodes",
                    "topologyKey %s - on-demand nodes carry no such label, so they form "
                    "no topology domain and the constraint cannot balance across them"
                    % ", ".join(bad_keys),
                    "Spread on %s (or kubernetes.io/hostname) instead; that label is "
                    "present on every node." % POOL_LABEL))

    pinned, why = targets_spot(spec, w["spot_pools"])
    if pinned and not w["tolerates_spot"]:
        f.append(_r("SPOT-006", "CRITICAL", "Targets spot capacity without tolerating its taint",
                    "%s but no toleration for %s - the pod can never schedule and stays "
                    "Pending forever" % (why, SPOT_TAINT),
                    "Add a toleration for key %s (value spot, effect NoSchedule), or drop "
                    "the spot pin." % SPOT_TAINT))
    elif pinned:
        f.append(_r("SPOT-003", "HIGH", "Hard-pinned to spot with no on-demand fallback",
                    "%s - when spot capacity is unavailable the pod stays Pending rather "
                    "than falling back to on-demand" % why,
                    "Replace the hard pin with preferredDuringSchedulingIgnoredDuringExecution "
                    "weighted toward spot, keeping the toleration so on-demand remains legal."))

    if spot and reps and reps >= 2 and not any(k in GOOD_SPREAD_KEYS
                                               for k in spread_keys(spec)):
        f.append(_r("SPOT-001", "HIGH", "Replicas can all land on one spot pool",
                    "%d replicas with no topologySpreadConstraints or podAntiAffinity on a "
                    "real failure domain - one reclaim can take every replica" % reps,
                    "Add topologySpreadConstraints with topologyKey %s (maxSkew 1, "
                    "whenUnsatisfiable ScheduleAnyway) and a second on "
                    "topology.kubernetes.io/zone." % POOL_LABEL))

    blanket = any(not t.get("key") and (t.get("operator") == "Exists")
                  for t in spec.get("tolerations") or [])
    if blanket:
        f.append(_r("SPOT-005", "MEDIUM", "Blanket toleration makes spot eligibility accidental",
                    "toleration with operator: Exists and no key - this pod tolerates every "
                    "taint, so it is spot-eligible whether or not the team intended it",
                    "Scope the toleration to the taints actually needed (%s) so spot "
                    "eligibility is a deliberate choice." % SPOT_TAINT))

    # --- singleton -------------------------------------------------------
    if singleton and spot:
        f.append(_r("SPOT-010", "CRITICAL", "Single replica on preemptible capacity",
                    "replicas=1%s and the pod tolerates spot - every preemption is a 100%% "
                    "outage of this service, with ~%ds notice"
                    % (" (HPA minReplicas=1)" if w["hpa"] else "", DEFAULT_NOTICE_SECONDS),
                    "Run at least 2 replicas with a spread constraint, or keep this "
                    "workload on on-demand via preferred nodeAffinity."))

    for b in w["pdbs"]:
        allowed = pdb_allowed(b, reps)
        st = b.get("status") or {}
        obs = st.get("disruptionsAllowed")
        if allowed is not None and allowed <= 0:
            f.append(_r("SPOT-011", "CRITICAL", "PDB permits zero voluntary disruptions",
                        "PDB %s with replicas=%d allows %d disruption(s); it blocks node "
                        "drain, the descheduler and autoscaler consolidation, and gives no "
                        "protection at all against preemption, which is involuntary"
                        % (ns_name(b), reps, allowed),
                        "Either raise replicas to >= 2 (keeping minAvailable=1) or relax the "
                        "PDB to maxUnavailable=1. A PDB on a singleton only blocks the safe "
                        "path; it cannot stop a reclaim."))
        if obs is not None and int(obs) <= 0:
            f.append(_r("SPOT-060", "CRITICAL", "PDB is blocking disruptions right now",
                        "PDB %s status.disruptionsAllowed=%s (currentHealthy=%s, "
                        "desiredHealthy=%s) - observed live, not inferred"
                        % (ns_name(b), obs, st.get("currentHealthy"),
                           st.get("desiredHealthy")),
                        "Same fix as SPOT-011; this row is the confirmed instance.",
                        needs="status"))
    if singleton and not w["pdbs"] and spot:
        f.append(_r("SPOT-013", "LOW", "Singleton has no PodDisruptionBudget",
                    "replicas=1 and no PDB - voluntary disruptions (upgrades, drains) are "
                    "unbounded; note a PDB would not help against preemption either",
                    "The fix is replicas >= 2, not a PDB."))

    if kind == "Deployment" and singleton and \
            ((w["workload_spec"].get("strategy") or {}).get("type") == "Recreate"):
        f.append(_r("SPOT-012", "MEDIUM", "Recreate strategy compounds a singleton restart",
                    "strategy.type=Recreate with replicas=1 - the old pod is terminated "
                    "before the replacement is scheduled, so recovery from a reclaim is "
                    "strictly serial",
                    "Use RollingUpdate with maxUnavailable=0 once replicas >= 2."))

    # --- state -----------------------------------------------------------
    if spot and w["rwo_claims"]:
        f.append(_r("SPOT-020", "HIGH", "ReadWriteOnce volume on preemptible capacity",
                    "%d ReadWriteOnce claim(s) - an Azure Disk must detach from the lost "
                    "node before it can attach elsewhere, which routinely takes minutes and "
                    "is unrelated to pod scheduling speed" % len(w["rwo_claims"]),
                    "Keep RWO-backed workloads on on-demand, or move to a ReadWriteMany "
                    "class (Azure Files) if the app tolerates it."))
    if spot and kind == "StatefulSet":
        f.append(_r("SPOT-021", "HIGH", "StatefulSet on preemptible capacity",
                    "StatefulSets recover serially with stable identity and volume "
                    "reattachment, so a reclaim costs far more than for a Deployment",
                    "Prefer on-demand for StatefulSets; if spot is required, ensure "
                    "podManagementPolicy and quorum size tolerate losing a member at will."))

    # --- lifecycle -------------------------------------------------------
    tgps = spec.get("terminationGracePeriodSeconds")
    if spot and tgps is not None and int(tgps) > DEFAULT_NOTICE_SECONDS:
        f.append(_r("SPOT-040", "HIGH", "Grace period exceeds the preemption notice",
                    "terminationGracePeriodSeconds=%s but Azure gives a spot node only ~%ds "
                    "of notice - the remaining %ss never happens, the process is SIGKILLed"
                    % (tgps, DEFAULT_NOTICE_SECONDS, int(tgps) - DEFAULT_NOTICE_SECONDS),
                    "Bring shutdown under %ds (preStop drain + fast flush), or keep the "
                    "workload on on-demand." % DEFAULT_NOTICE_SECONDS))
    containers = spec.get("containers") or []
    if spot and containers and not any(c.get("readinessProbe") for c in containers):
        f.append(_r("SPOT-042", "MEDIUM", "No readiness probe on a spot-eligible workload",
                    "reschedules are routine on spot; without a readiness probe the Service "
                    "sends traffic to a pod that has not finished starting",
                    "Add a readinessProbe to every container that serves traffic."))
    if spot and containers and not any((c.get("lifecycle") or {}).get("preStop")
                                       for c in containers):
        f.append(_r("SPOT-041", "LOW", "No preStop hook to drain in-flight requests",
                    "on preemption the pod has ~%ds; without a preStop sleep/drain, "
                    "in-flight requests are cut when endpoints have not yet converged"
                    % DEFAULT_NOTICE_SECONDS,
                    "Add a short preStop (e.g. sleep 5-10s) so endpoint removal lands "
                    "before the process exits."))

    # --- priority --------------------------------------------------------
    pc = spec.get("priorityClassName") or ""
    if spot and pc in ("system-cluster-critical", "system-node-critical"):
        f.append(_r("SPOT-031", "HIGH", "Cluster-critical workload tolerates spot",
                    "priorityClassName=%s on preemptible capacity" % pc,
                    "Critical components belong on on-demand; remove the spot toleration."))
    elif spot and not pc:
        f.append(_r("SPOT-030", "LOW", "No priorityClass to order recovery",
                    "when a spot pool is reclaimed many pods reschedule at once; with no "
                    "priorityClass the order is arbitrary",
                    "Assign a priorityClass so user-facing pods win the scramble."))

    # --- batch -----------------------------------------------------------
    if spot and kind in ("CronJob", "Job"):
        jspec = w["workload_spec"]
        if kind == "CronJob":
            jspec = (jspec.get("jobTemplate") or {}).get("spec") or {}
        if jspec.get("backoffLimit") == 0:
            f.append(_r("SPOT-050", "MEDIUM", "Batch job cannot survive one preemption",
                        "backoffLimit=0 on spot - a single reclaim mid-run fails the job "
                        "permanently",
                        "Raise backoffLimit (>= 3) and make the job idempotent/resumable; "
                        "batch is otherwise the ideal spot workload."))

    # --- observed state --------------------------------------------------
    if singleton and w["on_spot"]:
        f.append(_r("SPOT-061", "CRITICAL", "Singleton is running on a spot node right now",
                    "the single pod is scheduled on a node labelled %s=spot (pools: %s)"
                    % (SPOT_LABEL, ", ".join(w["pools_used"]) or "?"),
                    "Immediate: scale to 2 with a spread constraint, or move to on-demand.",
                    needs="status"))
    if reps and reps >= 2 and len(w["pools_used"]) == 1 and w["on_spot"]:
        f.append(_r("SPOT-064", "HIGH", "All replicas concentrated on one spot pool",
                    "%d pods observed, all in pool %s" % (len(w["pods"]), w["pools_used"][0]),
                    "Add the spread constraint from SPOT-001; concentration is the "
                    "observed consequence of not having one.", needs="status"))

    for p in w["pods"]:
        st = p.get("status") or {}
        if st.get("phase") == "Pending":
            for c in st.get("conditions") or []:
                if c.get("reason") == "Unschedulable":
                    f.append(_r("SPOT-063", "CRITICAL", "Pod is Pending and unschedulable",
                                "%s: %s" % (ns_name(p),
                                            str(c.get("message") or "")[:220]),
                                "Read the scheduler message literally - taint/toleration "
                                "mismatch and insufficient spot capacity are the two common "
                                "causes.", needs="status"))
                    break
        for cs in st.get("containerStatuses") or []:
            term = (cs.get("lastState") or {}).get("terminated") or {}
            if int(cs.get("restartCount") or 0) >= 3 and term.get("reason"):
                f.append(_r("SPOT-062", "MEDIUM", "Restart pattern consistent with reclaim",
                            "%s container %s: restartCount=%s, last termination reason=%s "
                            "exitCode=%s" % (ns_name(p), cs.get("name"),
                                             cs.get("restartCount"), term.get("reason"),
                                             term.get("exitCode")),
                            "Correlate with node churn (aks_report.py spot-eviction) before "
                            "concluding preemption; crashloops look the same here.",
                            needs="status"))
                break
    return f


def orphan_pdb_findings(objs, workloads):
    """PDBs whose selector matches nothing - protection the team thinks it has."""
    covered = {id(b) for w in workloads for b in w["pdbs"]}
    rows = []
    for b in objs.get("PodDisruptionBudget") or []:
        if id(b) in covered:
            continue
        rows.append({
            "workload": "(pdb) %s" % ns_name(b), "kind": "PodDisruptionBudget",
            "namespace": _md(b).get("namespace") or "default",
            "name": _md(b).get("name") or "", "applies_when": "now",
            **_r("SPOT-014", "MEDIUM", "PDB matches no workload in the bundle",
                 "selector %s selected no pod template - either the workload is outside "
                 "the export scope or the selector is wrong, in which case the PDB "
                 "protects nothing"
                 % str((b.get("spec") or {}).get("selector") or {})[:160],
                 "Fix the selector, or delete the PDB so the gap is visible."),
        })
    return rows


# --------------------------------------------------------------------------
# scoring and rows
# --------------------------------------------------------------------------

def verdict(w, findings, hypo):
    """Six-state read; on-demand workloads are scored as spot CANDIDATES. A
    workload that TARGETS spot without tolerating it is already on the spot path
    (and unschedulable there), so it is graded on it - never offered as an
    on-demand candidate."""
    worst = max([SEVERITY_WEIGHT[f["severity"]] for f in findings] or [0])
    if w["tolerates_spot"] or w.get("targets_spot"):
        if worst >= 5:
            return "BLOCKED ON SPOT"
        if worst >= 3:
            return "AT RISK ON SPOT"
        return "SPOT OK"
    hworst = max([SEVERITY_WEIGHT[f["severity"]] for f in hypo] or [0])
    return "KEEP ON DEMAND" if hworst >= 5 else \
        ("SPOT CANDIDATE" if hworst < 3 else "SPOT WITH CHANGES")


def workload_rows(workloads, findings_by_key, hypo_by_key):
    rows = []
    for w in workloads:
        key = (w["kind"], w["namespace"], w["name"])
        fs, hy = findings_by_key[key], hypo_by_key[key]
        on_spot_path = w["tolerates_spot"] or w.get("targets_spot")
        shown = fs if on_spot_path else hy
        rows.append({
            "verdict": verdict(w, fs, hy),
            "kind": w["kind"], "namespace": w["namespace"], "name": w["name"],
            "replicas": w["replicas"], "hpa_managed": "Yes" if w["hpa"] else "No",
            "tolerates_spot": "Yes" if w["tolerates_spot"] else "No",
            "pods_observed": len(w["pods"]),
            "pods_on_spot": w["on_spot"],
            "pools_observed": ", ".join(w["pools_used"]),
            "pdb": ", ".join(ns_name(b) for b in w["pdbs"]),
            # Stays numeric (None, not "", when there is no PDB): 0 here is the
            # headline number of the whole report, and a text column of "0" would
            # neither sort nor filter as one in Excel.
            "pdb_disruptions_allowed": next(
                ((b.get("status") or {}).get("disruptionsAllowed")
                 for b in w["pdbs"]
                 if (b.get("status") or {}).get("disruptionsAllowed") is not None), None),
            "rwo_volumes": len(w["rwo_claims"]),
            "grace_seconds": w["spec"].get("terminationGracePeriodSeconds"),
            "spread_keys": ", ".join(spread_keys(w["spec"])),
            "priority_class": w["spec"].get("priorityClassName") or "",
            "risk_score": sum(SEVERITY_WEIGHT[f["severity"]] for f in shown),
            "critical": sum(f["severity"] == "CRITICAL" for f in shown),
            "high": sum(f["severity"] == "HIGH" for f in shown),
            "top_finding": shown[0]["title"] if shown else "",
            "rule_ids": ", ".join(sorted({f["rule_id"] for f in shown})),
            "status_evidence": "Yes" if any(f["based_on"] == "status" for f in shown)
                               else "No (spec only)",
        })
    rows.sort(key=lambda r: (-r["risk_score"], r["namespace"], r["name"]))
    return rows


def finding_rows(workloads, findings_by_key, hypo_by_key):
    rows = []
    for w in workloads:
        key = (w["kind"], w["namespace"], w["name"])
        on_spot_path = w["tolerates_spot"] or w.get("targets_spot")
        fs = findings_by_key[key] if on_spot_path else hypo_by_key[key]
        for f in fs:
            rows.append({
                "workload": "%s/%s" % (w["namespace"], w["name"]), "kind": w["kind"],
                "namespace": w["namespace"], "name": w["name"],
                "applies_when": "now" if on_spot_path else "if moved to spot",
                **f,
            })
    rows.sort(key=lambda r: (-SEVERITY_WEIGHT[r["severity"]], r["rule_id"], r["workload"]))
    return rows


REMEDIATION = [
    ("SPOT-001", "Spread replicas across pools", """topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.azure.com/agentpool
    whenUnsatisfiable: ScheduleAnyway
    labelSelector: {matchLabels: {app: <app>}}"""),
    ("SPOT-002", "Never spread on scalesetpriority", """# WRONG - label exists only on spot nodes
#   topologyKey: kubernetes.azure.com/scalesetpriority
# RIGHT
    topologyKey: kubernetes.azure.com/agentpool"""),
    ("SPOT-003", "Prefer, do not pin", """affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - {key: kubernetes.azure.com/scalesetpriority, operator: In, values: [spot]}"""),
    ("SPOT-006", "Toleration required to use spot", """tolerations:
  - key: kubernetes.azure.com/scalesetpriority
    operator: Equal
    value: spot
    effect: NoSchedule"""),
    ("SPOT-010", "Two replicas beat any PDB", """spec:
  replicas: 2        # a singleton cannot be protected from preemption"""),
    ("SPOT-011", "Relax the deadlocked PDB", """spec:
  maxUnavailable: 1  # with replicas >= 2; minAvailable: 1 on replicas: 1 = 0 allowed"""),
    ("SPOT-020", "Keep RWO off spot", """affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        preference:
          matchExpressions:
            - {key: kubernetes.azure.com/scalesetpriority, operator: DoesNotExist}"""),
    ("SPOT-040", "Fit shutdown in the notice window", """spec:
  terminationGracePeriodSeconds: 30   # Azure gives ~30s; longer is not honoured"""),
    ("SPOT-050", "Let batch retry", """spec:
  backoffLimit: 6    # spot is ideal for batch, but only if a retry is free"""),
]


def remediation_rows(findings):
    hit = {}
    for f in findings:
        hit.setdefault(f["rule_id"], []).append(f["workload"])
    rows = []
    for rid, title, snippet in REMEDIATION:
        if rid not in hit:
            continue
        rows.append({"rule_id": rid, "change": title,
                     "workloads_affected": len(set(hit[rid])),
                     "examples": ", ".join(sorted(set(hit[rid]))[:5]),
                     "yaml": snippet})
    if not rows:
        rows.append({"rule_id": "(no data)", "change": "No rule with a canned fix fired",
                     "workloads_affected": 0, "examples": "",
                     "yaml": "See the Findings tab; every finding carries its own "
                             "remediation text."})
    return rows


def node_rows(nodes):
    if not nodes:
        return [{"node": "(no data)", "pool": "", "priority": "",
                 "zone": "", "instance_type": "",
                 "note": "nodes.yaml was not in the bundle (cluster-scoped read may have "
                         "been denied). Spot vs on-demand is therefore unknown and every "
                         "placement finding on this report is spec-derived only."}]
    rows = []
    for name, n in sorted(nodes.items()):
        rows.append({"node": name, "pool": n["pool"],
                     "priority": "spot" if n["spot"] else "on-demand",
                     "zone": n["zone"], "instance_type": n["instance_type"],
                     "note": ", ".join(t for t in n["taints"] if t)})
    return rows


def coverage_rows(meta, objs):
    rows = []
    for fname, kind, consequence in BUNDLE_FILES:
        state = meta["files"].get(fname, "MISSING")
        n = len(objs.get(kind) or [])
        rows.append({"file": fname, "kind": kind,
                     "status": "OK" if state.startswith("ok") else
                               ("SKIPPED" if state == "SKIPPED" else state),
                     "objects": n,
                     "impact_if_absent": "" if n else consequence})
    return rows


def scorecard_cards(rows, meta, nodes):
    total = len(rows)
    blocked = sum(r["verdict"] == "BLOCKED ON SPOT" for r in rows)
    at_risk = sum(r["verdict"] == "AT RISK ON SPOT" for r in rows)
    on_spot = sum(r["tolerates_spot"] == "Yes" for r in rows)
    cand = sum(r["verdict"] == "SPOT CANDIDATE" for r in rows)
    singles = sum(1 for r in rows if r["replicas"] == 1
                  and r["tolerates_spot"] == "Yes" and r["kind"] in ("Deployment",
                                                                     "StatefulSet"))
    deadlock = sum(1 for r in rows if "SPOT-011" in r["rule_ids"]
                   or "SPOT-060" in r["rule_ids"])
    status_pct = (sum(r["status_evidence"] == "Yes" for r in rows) / total) if total else 0
    return [
        {"label": "Workloads analysed", "value": total,
         "caption": "from %s" % (meta.get("scope") or "the export bundle"), "rag": "neutral"},
        {"label": "Blocked on spot", "value": blocked,
         "caption": "spot-tolerant with a CRITICAL finding",
         "rag": "bad" if blocked else "good"},
        {"label": "At risk on spot", "value": at_risk,
         "caption": "spot-tolerant with a HIGH finding",
         "rag": "warn" if at_risk else "good"},
        {"label": "Singletons on spot", "value": singles,
         "caption": "replicas=1 tolerating preemption - a reclaim is a full outage",
         "rag": "bad" if singles else "good"},
        {"label": "PDB deadlocks", "value": deadlock,
         "caption": "disruptions allowed = 0: blocks drain, not preemption",
         "rag": "bad" if deadlock else "good"},
        {"label": "Spot candidates", "value": cand,
         "caption": "%d already spot-tolerant; these are the safe additions" % on_spot,
         "rag": "good" if cand else "neutral"},
        {"label": "Live-state coverage", "value": "%.0f%%" % (status_pct * 100),
         "caption": "findings backed by status, not spec inference"
                    if nodes else "no nodes.yaml - spec-only run",
         "rag": "good" if status_pct >= 0.5 else "warn"},
        {"label": "Bundle age", "value": meta.get("exported_at") or "unknown",
         "caption": "a point-in-time snapshot; placement changes after export",
         "rag": "neutral"},
    ]


# --------------------------------------------------------------------------

def main(argv=None):
    global DEFAULT_NOTICE_SECONDS
    p = argparse.ArgumentParser(
        description="Workload spot-readiness analysis from an offline kubectl export",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--bundle", required=True,
                   help="export directory, .tar.gz, or a single multi-doc YAML file")
    p.add_argument("--out", default="out", help="output directory")
    p.add_argument("--namespace", default="",
                   help="comma-separated namespaces to analyse; omitted means all")
    p.add_argument("--notice-seconds", type=int, default=DEFAULT_NOTICE_SECONDS,
                   help="spot preemption notice window used by the grace-period rule")
    args = p.parse_args(argv)

    DEFAULT_NOTICE_SECONDS = args.notice_seconds

    if not os.path.exists(args.bundle):
        raise SystemExit("Bundle not found: %s" % args.bundle)
    objs, meta = load_bundle(args.bundle)
    log("Loaded bundle %s: %s" % (args.bundle, ", ".join(
        "%s=%d" % (k, len(v)) for k, v in sorted(objs.items())) or "nothing"))

    nodes = index_nodes(objs.get("Node") or [])
    spot_nodes = sum(1 for n in nodes.values() if n["spot"])
    log("Nodes: %d (%d spot, %d on-demand)" % (len(nodes), spot_nodes,
                                               len(nodes) - spot_nodes))

    namespaces = {s.strip() for s in args.namespace.split(",") if s.strip()}
    workloads = build_workloads(objs, nodes, namespaces)
    if not workloads:
        raise SystemExit("No Deployment/StatefulSet/DaemonSet/CronJob/Job found in %s - "
                         "check the bundle was produced by examples/export_workloads.sh"
                         % args.bundle)
    log("Workloads in scope: %d" % len(workloads))

    findings_by_key, hypo_by_key = {}, {}
    for w in workloads:
        key = (w["kind"], w["namespace"], w["name"])
        findings_by_key[key] = evaluate(w, assume_spot=False)
        hypo_by_key[key] = evaluate(w, assume_spot=True)

    wrows = workload_rows(workloads, findings_by_key, hypo_by_key)
    frows = finding_rows(workloads, findings_by_key, hypo_by_key)
    frows.extend(orphan_pdb_findings(objs, workloads))

    wb = excel.new_workbook()
    excel.add_readme(wb, "Workload Spot Readiness", [
        "Generated: %s   Bundle: %s   Exported: %s   Scope: %s" % (
            dt.datetime.now().strftime("%Y-%m-%d %H:%M"), args.bundle,
            meta.get("exported_at") or "unknown", meta.get("scope") or "unknown"),
        "",
        "This report is built entirely from an offline kubectl export. No cluster",
        "access and no Azure scope are used. It answers two questions:",
        "  1. Why does a workload fail on a spot node pool?",
        "  2. Which on-demand workloads can move to spot safely?",
        "",
        "The central finding class is the singleton: replicas=1 protected by a PDB with",
        "minAvailable=1. That combination allows ZERO voluntary disruptions - it blocks",
        "node drain, the descheduler and cluster-autoscaler consolidation - while giving",
        "NO protection against preemption, which is involuntary and ignores PDBs. The",
        "only real fix is a second replica plus a spread constraint.",
        "",
        "Verdicts:",
        "  BLOCKED ON SPOT   - tolerates spot today and has a CRITICAL finding.",
        "  AT RISK ON SPOT   - tolerates spot today and has a HIGH finding.",
        "  SPOT OK           - tolerates spot with no HIGH/CRITICAL finding.",
        "  SPOT CANDIDATE    - on-demand today; would be clean on spot as written.",
        "  SPOT WITH CHANGES - on-demand today; needs the listed changes first.",
        "  KEEP ON DEMAND    - on-demand today and would be CRITICAL on spot.",
        "",
        "Tabs:",
        "  Scorecard        - the one-pager.",
        "  WorkloadRisk     - one row per workload with every input pre-joined.",
        "  Findings         - one row per (workload, rule) with evidence and the fix.",
        "  RemediationGuide - the YAML to apply, per rule that actually fired.",
        "  NodeInventory    - spot vs on-demand nodes as seen in the bundle.",
        "  Coverage         - what the export contained, and what a gap costs.",
        "",
        "LIMITATIONS - read before acting:",
        "  - A bundle is a point-in-time snapshot. Pod placement changes constantly on",
        "    spot; the spec findings stay true, the status findings may not.",
        "  - Events carry a ~1h TTL. Absence of eviction events proves nothing.",
        "  - True eviction counts are only visible on-node (Scheduled Events / IMDS).",
        "    For node-level preemption evidence run: aks_report.py spot-eviction.",
        "  - FALSE POSITIVES to check before acting on a singleton finding: an",
        "    HPA-managed Deployment (handled - minReplicas is used instead of replicas);",
        "    a leader-elected active/passive pair deployed as two workloads; a workload",
        "    that is genuinely allowed to be down (batch, dev).",
        "  - Namespaces the exporter could not read appear as SKIPPED on Coverage. A",
        "    clean report over a partial bundle is not a clean estate.",
        "  - Remediations assume kube-scheduler + cluster autoscaler + descheduler +",
        "    topologySpreadConstraints. No Karpenter/NAP and no Cilium are used.",
    ])

    excel.add_scorecard(wb, "Scorecard", scorecard_cards(wrows, meta, nodes),
                        title="Spot readiness at a glance")
    excel.add_table(wb, "WorkloadRisk", pd.DataFrame(wrows), section="summary",
                    fail_cols=("verdict",),
                    fail_values=("BLOCKED ON SPOT", "KEEP ON DEMAND"),
                    warn_values=("AT RISK ON SPOT", "SPOT WITH CHANGES"),
                    int_cols=("replicas", "pods_observed", "pods_on_spot",
                              "pdb_disruptions_allowed", "rwo_volumes",
                              "grace_seconds", "risk_score", "critical", "high"),
                    colorscale_cols=("risk_score",), max_width=50)
    excel.add_table(wb, "Findings", pd.DataFrame(frows), fail_cols=("severity",),
                    fail_values=("CRITICAL",), warn_values=("HIGH", "MEDIUM"),
                    max_width=70)
    excel.add_table(wb, "RemediationGuide", pd.DataFrame(remediation_rows(frows)),
                    int_cols=("workloads_affected",), max_width=70)
    excel.add_table(wb, "NodeInventory", pd.DataFrame(node_rows(nodes)),
                    section="reference", max_width=50)
    excel.add_table(wb, "Coverage", pd.DataFrame(coverage_rows(meta, objs)),
                    section="reference", fail_cols=("status",),
                    fail_values=("MISSING",), warn_values=("SKIPPED",),
                    int_cols=("objects",), max_width=70)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "aks_workload_spot_readiness_%s.xlsx"
                        % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    excel.save(wb, path)
    log("Report written: %s" % path)
    return path


if __name__ == "__main__":
    main()
