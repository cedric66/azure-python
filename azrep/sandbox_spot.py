"""Spot conversion simulation on the sandbox cluster: pool split + workload scenario matrix.

Splits an on-demand pool into OD + Spot (spot priority is immutable, so this always creates
a NEW spot pool and shrinks the OD pool), deploys the descheduler for rebalancing, then runs
a matrix of deployments modeled on how app teams actually write YAML - success and failure
combinations - and reports where pods actually land.

Org constraint: no Karpenter/NAP, no Cilium - only kube-scheduler + cluster autoscaler +
descheduler + topologySpreadConstraints. Spreading OD/spot uses the always-present
kubernetes.azure.com/agentpool node label as the topology key: the scalesetpriority label
exists ONLY on spot nodes, so it cannot act as a spread key (a classic app-team mistake).
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from azrep import kubectl as kctl
from azrep.http_client import log
from azrep.sandbox import AKS_API, clean_dict, cluster_id, wait_for_provisioning

COMPUTE_API = "2024-07-01"
SPOT_LABEL = "kubernetes.azure.com/scalesetpriority"
POOL_LABEL = "kubernetes.azure.com/agentpool"
SPOT_TOLERATION = {"key": SPOT_LABEL, "operator": "Equal", "value": "spot",
                   "effect": "NoSchedule"}
PAUSE_IMAGE = "mcr.microsoft.com/oss/kubernetes/pause:3.10"
DESCHEDULER_MANIFEST = Path(__file__).resolve().parent.parent / "manifests/spot/descheduler.yaml"
SETTLE_TIMEOUT = 900
POLL_S = 30


# ------------------------------------------------------------------ pool split
def list_pools(session, cfg):
    return session.get_paged("%s/agentPools" % cluster_id(cfg),
                             params={"api-version": AKS_API})


def pool_candidates(pools):
    out = []
    for p in pools:
        props = p.get("properties") or {}
        if str(props.get("mode") or "User").lower() == "system":
            continue
        if str(props.get("scaleSetPriority") or "").lower() == "spot":
            continue
        power = props.get("powerState") or {}
        if str(power.get("code") or "Running").lower() != "running":
            continue
        out.append(p)
    return out


def pick_pool(pools, args):
    candidates = pool_candidates(pools)
    if not candidates:
        sys.exit("No running on-demand User pools to split. Deploy one first "
                 "(System pools cannot become spot).")
    if args.pool:
        match = [p for p in candidates if p["name"] == args.pool]
        if not match:
            sys.exit("--pool %s is not a running on-demand User pool (candidates: %s)"
                     % (args.pool, ", ".join(p["name"] for p in candidates)))
        return match[0]
    if len(candidates) == 1 or not sys.stdin.isatty():
        log("Using pool %s." % candidates[0]["name"])
        return candidates[0]
    print("\nOn-demand User pools:")
    for i, p in enumerate(candidates, 1):
        props = p["properties"]
        print("  %d) %-12s %-20s count=%s autoscale=%s"
              % (i, p["name"], props.get("vmSize"), props.get("count"),
                 props.get("enableAutoScaling")))
    choice = input("Pool to split [1]: ").strip() or "1"
    try:
        return candidates[int(choice) - 1]
    except (ValueError, IndexError):
        sys.exit("Invalid choice: %s" % choice)


def flat_pool(raw):
    """Adapt an agentPools API item to the flattened-pool shape plan_split expects."""
    props = dict(raw.get("properties") or {})
    taints = props.get("nodeTaints") or []
    labels = props.get("nodeLabels") or {}
    return {
        "pool": raw["name"],
        "count": int(props.get("count") or 0),
        "max_count": props.get("maxCount"),
        "min_count": props.get("minCount"),
        "autoscaling": bool(props.get("enableAutoScaling")),
        "vm_size": props.get("vmSize") or "",
        "zones": ",".join(str(z) for z in (props.get("availabilityZones") or [])),
        "taints": "; ".join(taints),
        "node_labels": ", ".join("%s=%s" % (k, labels[k]) for k in sorted(labels)),
    }


def build_plan(od_raw, pools, args):
    import spot_split_design
    if not 0 < args.spot_share < 1:
        sys.exit("--spot-share must be between 0 and 1 (exclusive).")
    split_args = argparse.Namespace(od_floor=1 - args.spot_share, headroom=0.3)
    used = {p["name"] for p in pools}
    plan = spot_split_design.plan_split(flat_pool(od_raw), "sandbox", split_args, used)
    if args.vm_size:
        plan["vm_size"] = args.vm_size
    return plan


def create_spot_pool(session, cfg, plan, od_raw):
    od = od_raw.get("properties") or {}
    labels = dict(plan["labels"])
    labels["pooltype"] = "spot"
    properties = clean_dict({
        "mode": "User",
        "vmSize": plan["vm_size"],
        "count": plan["spot_initial_nodes"],
        "enableAutoScaling": True,
        "minCount": plan["spot_min"],
        "maxCount": plan["spot_max"],
        "scaleSetPriority": "Spot",
        "scaleSetEvictionPolicy": "Delete",
        "spotMaxPrice": -1,
        "osType": od.get("osType", "Linux"),
        "osSKU": od.get("osSKU"),
        "maxPods": od.get("maxPods"),
        "orchestratorVersion": od.get("orchestratorVersion"),
        "availabilityZones": [z for z in plan["zones"].split(",") if z],
        "nodeLabels": labels,
        "nodeTaints": plan["team_taints"] or None,  # AKS auto-adds the spot taint
        "vnetSubnetID": od.get("vnetSubnetID"),
        "podSubnetID": od.get("podSubnetID"),
    })
    url = "%s/agentPools/%s" % (cluster_id(cfg), plan["spot_pool"])
    log("Creating spot pool %s (%s x%d, max %d)..."
        % (plan["spot_pool"], plan["vm_size"], plan["spot_initial_nodes"], plan["spot_max"]))
    session.put(url, params={"api-version": AKS_API}, payload={"properties": properties})
    wait_for_provisioning(session, url, AKS_API, what="spot pool %s" % plan["spot_pool"])


def shrink_od_pool(session, cfg, plan, od_raw):
    url = "%s/agentPools/%s" % (cluster_id(cfg), plan["od_pool"])
    props = dict((session.get(url, params={"api-version": AKS_API}) or {}).get("properties") or {})
    for ro in ("provisioningState", "powerState", "currentOrchestratorVersion",
               "nodeImageVersion"):
        props.pop(ro, None)
    if props.get("enableAutoScaling"):
        props["minCount"] = min(int(props.get("minCount") or 1), plan["od_keep_nodes"])
        props["maxCount"] = max(plan["od_keep_nodes"], 1)
        log("Shrinking OD pool %s autoscaler to %d..%d..."
            % (plan["od_pool"], props["minCount"], props["maxCount"]))
    else:
        props["count"] = plan["od_keep_nodes"]
        log("Scaling OD pool %s to %d node(s)..." % (plan["od_pool"], plan["od_keep_nodes"]))
    session.put(url, params={"api-version": AKS_API}, payload={"properties": props})
    wait_for_provisioning(session, url, AKS_API, what="OD pool %s" % plan["od_pool"])


# ------------------------------------------------------------------ scenarios
def base_deployment(name, namespace, replicas):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [{
                        "name": "app",
                        "image": PAUSE_IMAGE,
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
                    }],
                },
            },
        },
    }


def pod_spec(dep):
    return dep["spec"]["template"]["spec"]


def with_toleration(dep):
    pod_spec(dep).setdefault("tolerations", []).append(dict(SPOT_TOLERATION))
    return dep


def with_spot_affinity(dep, required):
    term = {"matchExpressions": [{"key": SPOT_LABEL, "operator": "In", "values": ["spot"]}]}
    aff = pod_spec(dep).setdefault("affinity", {}).setdefault("nodeAffinity", {})
    if required:
        aff["requiredDuringSchedulingIgnoredDuringExecution"] = {"nodeSelectorTerms": [term]}
    else:
        aff["preferredDuringSchedulingIgnoredDuringExecution"] = [
            {"weight": 100, "preference": term}]
    return dep


def with_od_pin(dep):
    term = {"matchExpressions": [{"key": SPOT_LABEL, "operator": "NotIn", "values": ["spot"]}]}
    aff = pod_spec(dep).setdefault("affinity", {}).setdefault("nodeAffinity", {})
    aff["requiredDuringSchedulingIgnoredDuringExecution"] = {"nodeSelectorTerms": [term]}
    return dep


def with_pool_spread(dep, plan, when):
    """Spread OD/spot on the agentpool label, fenced to the two pools of the split.
    scalesetpriority cannot be the key: OD nodes do not carry that label at all."""
    name = dep["metadata"]["name"]
    term = {"matchExpressions": [{"key": POOL_LABEL, "operator": "In",
                                  "values": [plan["od_pool"], plan["spot_pool"]]}]}
    aff = pod_spec(dep).setdefault("affinity", {}).setdefault("nodeAffinity", {})
    aff["requiredDuringSchedulingIgnoredDuringExecution"] = {"nodeSelectorTerms": [term]}
    pod_spec(dep)["topologySpreadConstraints"] = [{
        "maxSkew": 1,
        "topologyKey": POOL_LABEL,
        "whenUnsatisfiable": when,
        "labelSelector": {"matchLabels": {"app": name}},
    }]
    return dep


def pdb(name, namespace, min_available):
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": "%s-pdb" % name, "namespace": namespace},
        "spec": {"minAvailable": min_available,
                 "selector": {"matchLabels": {"app": name}}},
    }


def v_all_running(pods, expect_spot=None, min_spot=None, max_spot=None):
    running = [p for p in pods if p["phase"] == "Running"]
    pending = [p for p in pods if p["phase"] == "Pending"]
    on_spot = sum(1 for p in running if p["spot"])
    if pending:
        return ("FAIL", "%d pod(s) Pending: %s" % (len(pending), pending[0]["message"][:200]))
    if not running:
        return ("FAIL", "no running pods")
    detail = "%d/%d pods on spot" % (on_spot, len(running))
    if expect_spot is not None and on_spot != (len(running) if expect_spot else 0):
        want = "all" if expect_spot else "none"
        return ("FAIL", detail + " (expected %s on spot)" % want)
    if min_spot is not None and on_spot < min_spot:
        return ("FAIL", detail + " (expected >= %d on spot)" % min_spot)
    if max_spot is not None and on_spot > max_spot:
        return ("FAIL", detail + " (expected <= %d on spot)" % max_spot)
    return ("PASS", detail)


def v_skew(pods, plan):
    running = [p for p in pods if p["phase"] == "Running"]
    status, detail = v_all_running(pods)
    if status == "FAIL":
        return (status, detail)
    od = sum(1 for p in running if p["pool"] == plan["od_pool"])
    spot = sum(1 for p in running if p["pool"] == plan["spot_pool"])
    skew = abs(od - spot)
    detail = "od=%d spot=%d skew=%d" % (od, spot, skew)
    return ("PASS", detail) if skew <= 1 else ("FAIL", detail + " (maxSkew 1 violated)")


def v_expect_pending(pods, plan):
    pending = [p for p in pods if p["phase"] == "Pending"]
    if not pending:
        on_spot = sum(1 for p in pods if p["spot"])
        return ("FAIL", "everything scheduled (%d on spot); the missing toleration was "
                        "not caught" % on_spot)
    return ("PASS", "%d pod(s) Pending as expected - missing spot toleration blocks the "
                    "spread: %s" % (len(pending), pending[0]["message"][:200]))


def v_single_replica(pods, plan):
    status, detail = v_all_running(pods, expect_spot=True)
    if status == "FAIL":
        return (status, detail)
    return ("WARN", detail + " - single replica on evictable capacity: a spot eviction "
                             "is an outage (anti-pattern)")


def v_pdb_blocked(pods, plan, pdb_status):
    status, detail = v_all_running(pods)
    if status == "FAIL":
        return (status, detail)
    allowed = (pdb_status or {}).get("disruptionsAllowed")
    if allowed == 0:
        return ("WARN", detail + " - PDB minAvailable 100%% leaves disruptionsAllowed=0: "
                                 "descheduler/upgrades cannot evict these pods")
    return ("FAIL", detail + " - expected the PDB to block evictions (disruptionsAllowed=%s)"
            % allowed)


SCENARIOS = [
    {"id": "baseline-no-toleration", "replicas": 4,
     "desc": "Plain deployment, no spot awareness",
     "expect": "all pods stay on on-demand (spot taint repels them)",
     "build": lambda plan, ns: [base_deployment("baseline-no-toleration", ns, 4)],
     "verdict": lambda pods, plan, extra: v_all_running(pods, expect_spot=False)},
    {"id": "toleration-only", "replicas": 4,
     "desc": "Spot toleration, no affinity/spread",
     "expect": "everything schedulable; scheduler decides the mix",
     "build": lambda plan, ns: [with_toleration(base_deployment("toleration-only", ns, 4))],
     "verdict": lambda pods, plan, extra: v_all_running(pods)},
    {"id": "spot-preferred", "replicas": 4,
     "desc": "Toleration + preferred nodeAffinity to spot",
     "expect": "pods gravitate to spot but survive a spot outage",
     "build": lambda plan, ns: [with_spot_affinity(
         with_toleration(base_deployment("spot-preferred", ns, 4)), required=False)],
     "verdict": lambda pods, plan, extra: v_all_running(pods, min_spot=1)},
    {"id": "spot-required", "replicas": 3,
     "desc": "Toleration + required nodeAffinity to spot",
     "expect": "all pods on spot; Pending during a spot outage",
     "build": lambda plan, ns: [with_spot_affinity(
         with_toleration(base_deployment("spot-required", ns, 3)), required=True)],
     "verdict": lambda pods, plan, extra: v_all_running(pods, expect_spot=True)},
    {"id": "topology-spread", "replicas": 4,
     "desc": "Toleration + topologySpread maxSkew 1 on the agentpool label (DoNotSchedule)",
     "expect": "balanced od/spot split",
     "build": lambda plan, ns: [with_pool_spread(
         with_toleration(base_deployment("topology-spread", ns, 4)), plan, "DoNotSchedule")],
     "verdict": lambda pods, plan, extra: v_skew(pods, plan)},
    {"id": "spread-soft", "replicas": 4,
     "desc": "Same spread but whenUnsatisfiable ScheduleAnyway",
     "expect": "keeps running even with zero spot capacity",
     "build": lambda plan, ns: [with_pool_spread(
         with_toleration(base_deployment("spread-soft", ns, 4)), plan, "ScheduleAnyway")],
     "verdict": lambda pods, plan, extra: v_all_running(pods)},
    {"id": "spread-missing-toleration", "replicas": 4,
     "desc": "MISCONFIG: spread on agentpool but no spot toleration",
     "expect": "pods go Pending - the spread demands spot placement the taint forbids",
     "build": lambda plan, ns: [with_pool_spread(
         base_deployment("spread-missing-toleration", ns, 4), plan, "DoNotSchedule")],
     "verdict": lambda pods, plan, extra: v_expect_pending(pods, plan)},
    {"id": "single-replica-spot", "replicas": 1,
     "desc": "ANTI-PATTERN: 1 replica pinned to spot, no PDB",
     "expect": "runs, but flagged - an eviction is a full outage",
     "build": lambda plan, ns: [with_spot_affinity(
         with_toleration(base_deployment("single-replica-spot", ns, 1)), required=True)],
     "verdict": lambda pods, plan, extra: v_single_replica(pods, plan)},
    {"id": "pdb-too-strict", "replicas": 2,
     "desc": "MISCONFIG: spread + PDB minAvailable 100%",
     "expect": "runs, but evictions blocked - descheduler/upgrades cannot move these pods",
     "build": lambda plan, ns: [
         with_pool_spread(with_toleration(base_deployment("pdb-too-strict", ns, 2)),
                          plan, "ScheduleAnyway"),
         pdb("pdb-too-strict", ns, "100%")],
     "verdict": lambda pods, plan, extra: v_pdb_blocked(pods, plan, extra.get("pdb"))},
    {"id": "od-pinned-critical", "replicas": 2,
     "desc": "Critical workload pinned off spot (NotIn matches unlabeled OD nodes)",
     "expect": "never lands on spot",
     "build": lambda plan, ns: [with_od_pin(base_deployment("od-pinned-critical", ns, 2))],
     "verdict": lambda pods, plan, extra: v_all_running(pods, expect_spot=False)},
]


def scenario_namespace(sid):
    return "spot-sim-%s" % sid


def apply_scenario(kubeconfig, scenario, plan):
    import yaml
    ns = scenario_namespace(scenario["id"])
    kctl.ensure_namespace(kubeconfig, ns)
    docs = scenario["build"](plan, ns)
    text = yaml.safe_dump_all(docs, sort_keys=False)
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="spotsim-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        rc, _, err = kctl.apply_manifest(kubeconfig, path)
        if rc != 0:
            sys.exit("Applying scenario %s failed: %s" % (scenario["id"], err.strip()[:300]))
    finally:
        os.unlink(path)
    return text


def node_map(kubeconfig):
    data = kctl.kubectl_json(kubeconfig, ["get", "nodes"]) or {}
    out = {}
    for n in data.get("items") or []:
        labels = (n.get("metadata") or {}).get("labels") or {}
        out[n["metadata"]["name"]] = {
            "spot": labels.get(SPOT_LABEL) == "spot",
            "pool": labels.get(POOL_LABEL, ""),
        }
    return out


def pod_rows(kubeconfig, namespace, nodes):
    data = kctl.kubectl_json(kubeconfig, ["get", "pods", "-n", namespace]) or {}
    rows = []
    for p in data.get("items") or []:
        node = (p.get("spec") or {}).get("nodeName") or ""
        info = nodes.get(node, {})
        message = ""
        for cond in ((p.get("status") or {}).get("conditions") or []):
            if cond.get("type") == "PodScheduled" and cond.get("status") != "True":
                message = cond.get("message") or ""
        rows.append({
            "pod": (p.get("metadata") or {}).get("name", ""),
            "node": node,
            "pool": info.get("pool", ""),
            "spot": bool(info.get("spot")),
            "phase": (p.get("status") or {}).get("phase", ""),
            "message": message,
        })
    return rows


def pdb_status(kubeconfig, namespace, name):
    data = kctl.kubectl_json(kubeconfig, ["get", "pdb", name, "-n", namespace])
    return (data or {}).get("status") or {}


def settle(kubeconfig, scenarios, timeout=SETTLE_TIMEOUT):
    """Wait until pod placements stop changing (autoscaler scale-ups take minutes)."""
    deadline = time.time() + timeout
    previous, stable = None, 0
    while True:
        nodes = node_map(kubeconfig)
        state = {}
        for sc in scenarios:
            for row in pod_rows(kubeconfig, scenario_namespace(sc["id"]), nodes):
                state[row["pod"]] = (row["phase"], row["node"])
        running = sum(1 for v in state.values() if v[0] == "Running")
        log("  settle: %d pods, %d running" % (len(state), running))
        stable = stable + 1 if state == previous and state else 0
        if stable >= 2 or time.time() > deadline:
            return
        previous = state
        time.sleep(POLL_S)


def collect_results(kubeconfig, scenarios, plan):
    nodes = node_map(kubeconfig)
    results, placements = [], []
    for sc in scenarios:
        ns = scenario_namespace(sc["id"])
        pods = pod_rows(kubeconfig, ns, nodes)
        extra = {}
        if sc["id"] == "pdb-too-strict":
            extra["pdb"] = pdb_status(kubeconfig, ns, "pdb-too-strict-pdb")
        status, detail = sc["verdict"](pods, plan, extra) if pods else \
            ("FAIL", "no pods found")
        results.append((sc["id"], sc["desc"], sc["expect"], status, detail))
        for row in pods:
            placements.append((sc["id"], row["pod"], row["node"], row["pool"],
                               "spot" if row["spot"] else "od", row["phase"],
                               row["message"][:200]))
    return results, placements


# ------------------------------------------------------------------ eviction
def find_spot_vmss(session, cfg, plan):
    body = session.get(cluster_id(cfg), params={"api-version": AKS_API}) or {}
    node_rg = (body.get("properties") or {}).get("nodeResourceGroup")
    if not node_rg:
        return None, None
    url = "/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute" \
          "/virtualMachineScaleSets" % (cfg["subscription_id"], node_rg)
    for vmss in session.get_paged(url, params={"api-version": COMPUTE_API}):
        if ("-%s-" % plan["spot_pool"]) in vmss["name"]:
            return node_rg, vmss["name"]
    return node_rg, None


def simulate_eviction(session, cfg, plan):
    node_rg, vmss = find_spot_vmss(session, cfg, plan)
    if not vmss:
        log("WARNING: spot pool VMSS not found in %s; skipping eviction simulation." % node_rg)
        return False
    base = "/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Compute" \
           "/virtualMachineScaleSets/%s" % (cfg["subscription_id"], node_rg, vmss)
    instances = session.get_paged("%s/virtualMachines" % base,
                                  params={"api-version": COMPUTE_API})
    if not instances:
        log("WARNING: no spot instances to evict; skipping eviction simulation.")
        return False
    inst = instances[0]["instanceId"]
    log("Simulating spot eviction of %s instance %s..." % (vmss, inst))
    session.post("%s/virtualmachines/%s/simulateEviction" % (base, inst),
                 params={"api-version": COMPUTE_API}, payload=None)
    return True


def rebalance_watch(kubeconfig, scenarios, plan, timeout=SETTLE_TIMEOUT):
    """After an eviction: record where pods went, then whether they rebalance to spot."""
    log("Waiting for the eviction to land (~60s) and pods to reschedule...")
    time.sleep(60)
    settle(kubeconfig, scenarios, timeout=timeout // 2)
    nodes = node_map(kubeconfig)
    after = {sc["id"]: sum(1 for r in pod_rows(kubeconfig, scenario_namespace(sc["id"]), nodes)
                           if r["spot"] and r["phase"] == "Running")
             for sc in scenarios}
    log("Waiting for autoscaler + descheduler rebalancing (descheduler interval 2m)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(POLL_S * 2)
        nodes = node_map(kubeconfig)
        if any(info["spot"] for info in nodes.values()):
            break
    settle(kubeconfig, scenarios, timeout=timeout // 2)
    nodes = node_map(kubeconfig)
    rows = []
    for sc in scenarios:
        final = sum(1 for r in pod_rows(kubeconfig, scenario_namespace(sc["id"]), nodes)
                    if r["spot"] and r["phase"] == "Running")
        rows.append((sc["id"], after[sc["id"]], final,
                     "rebalanced" if final > after[sc["id"]] else "unchanged"))
    return rows


# ------------------------------------------------------------------ output
def price_rows(plan, location):
    from azrep.armextras import retail_vm_prices
    prices = retail_vm_prices(location, plan["vm_size"])
    if not prices or not prices.get("od_hr"):
        return []
    od_hr, spot_hr = prices["od_hr"], prices.get("spot_hr") or 0
    discount = (1 - spot_hr / od_hr) if od_hr and spot_hr else 0
    moved = plan["spot_initial_nodes"]
    return [
        ("vm_size", plan["vm_size"]),
        ("on_demand_usd_hr", round(od_hr, 4)),
        ("spot_usd_hr", round(spot_hr, 4)),
        ("spot_discount", "%.0f%%" % (discount * 100)),
        ("nodes_moved_to_spot", moved),
        ("est_monthly_saving_usd", round((od_hr - spot_hr) * moved * 730, 2)),
    ]


def write_md(path, cfg, plan, results, scenario_yaml, prices):
    lines = ["# Spot adoption scenarios - %s" % cfg["cluster"]["name"], "",
             "Pool split: `%s` (on-demand, keeps %d) + `%s` (spot, %d initial / %d max)."
             % (plan["od_pool"], plan["od_keep_nodes"], plan["spot_pool"],
                plan["spot_initial_nodes"], plan["spot_max"]), "",
             "Rebalancing: cluster autoscaler + descheduler "
             "(no Karpenter; spread key is `%s` because the `%s` label only exists on "
             "spot nodes)." % (POOL_LABEL, SPOT_LABEL), ""]
    if prices:
        lines += ["| %s | %s |" % p for p in [("metric", "value"), ("---", "---")] + prices]
        lines.append("")
    for sid, desc, expect, status, detail in results:
        lines += ["## %s - %s" % (sid, status), "", desc + ".",
                  "", "Expected: %s." % expect, "Observed: %s." % detail, "",
                  "```yaml", scenario_yaml.get(sid, "").rstrip(), "```", ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    log("Wrote %s" % path)


def write_xlsx(args, cfg, plan, results, placements, rebalance, prices):
    import pandas as pd
    from azrep import excel
    wb = excel.new_workbook()
    excel.add_readme(wb, "Spot conversion simulation", [
        "Cluster: %s | OD pool %s -> spot pool %s (%s)."
        % (cfg["cluster"]["name"], plan["od_pool"], plan["spot_pool"], plan["vm_size"]),
        "Scenario matrix models app-team deployment YAML (success and failure combos); "
        "FAIL rows are real scheduling outcomes, WARN rows are accepted-but-risky patterns.",
        "Rebalancing relies on cluster autoscaler + descheduler only (no Karpenter/Cilium).",
    ])
    if results:
        excel.add_table(wb, "Scenarios",
                        pd.DataFrame(results, columns=["scenario", "description", "expected",
                                                       "status", "detail"]),
                        fail_cols=("status",), section="summary")
    if placements:
        excel.add_table(wb, "Placement",
                        pd.DataFrame(placements, columns=["scenario", "pod", "node", "pool",
                                                          "capacity", "phase", "pending_reason"]))
    if rebalance:
        excel.add_table(wb, "Rebalance",
                        pd.DataFrame(rebalance, columns=["scenario", "spot_pods_after_eviction",
                                                         "spot_pods_final", "outcome"]))
    if prices:
        excel.add_table(wb, "PriceDelta", pd.DataFrame(prices, columns=["metric", "value"]),
                        section="reference")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    path = "%s/spot_sim_%s_%s.xlsx" % (args.out, cfg["cluster"]["name"],
                                       time.strftime("%Y%m%d_%H%M%S"))
    excel.save(wb, path)
    log("Wrote %s" % path)


def pick_scenarios(args):
    if args.scenarios.strip().lower() == "none":
        return []
    if args.scenarios.strip().lower() == "all":
        return SCENARIOS
    wanted = {w.strip() for w in args.scenarios.split(",") if w.strip()}
    unknown = wanted - {s["id"] for s in SCENARIOS}
    if unknown:
        sys.exit("Unknown scenario(s): %s. Available: %s"
                 % (", ".join(sorted(unknown)), ", ".join(s["id"] for s in SCENARIOS)))
    return [s for s in SCENARIOS if s["id"] in wanted]


def run(session, cfg, args):
    pools = list_pools(session, cfg)
    od_raw = pick_pool(pools, args)
    plan = build_plan(od_raw, pools, args)
    prices = [] if args.no_prices else price_rows(plan, cfg["location"])
    log("Plan: split %s -> keep %d on-demand, spot pool %s with %d node(s) (max %d)."
        % (plan["od_pool"], plan["od_keep_nodes"], plan["spot_pool"],
           plan["spot_initial_nodes"], plan["spot_max"]))

    create_spot_pool(session, cfg, plan, od_raw)
    shrink_od_pool(session, cfg, plan, od_raw)

    scenarios = pick_scenarios(args)
    results, placements, rebalance, scenario_yaml = [], [], [], {}
    if scenarios:
        kubeconfig = kctl.fetch_kubeconfig(cfg, session=session)
        log("Deploying descheduler (%s)..." % DESCHEDULER_MANIFEST.name)
        rc, _, err = kctl.apply_manifest(kubeconfig, DESCHEDULER_MANIFEST)
        if rc != 0:
            sys.exit("Descheduler apply failed: %s" % err.strip()[:300])
        for sc in scenarios:
            scenario_yaml[sc["id"]] = apply_scenario(kubeconfig, sc, plan)
        log("Scenarios applied; waiting for scheduling to settle...")
        settle(kubeconfig, scenarios)
        results, placements = collect_results(kubeconfig, scenarios, plan)
        if args.simulate_eviction and simulate_eviction(session, cfg, plan):
            rebalance = rebalance_watch(kubeconfig, scenarios, plan)
        if not args.keep:
            for sc in scenarios:
                kctl.delete_namespace(kubeconfig, scenario_namespace(sc["id"]))
            kctl.delete_manifest(kubeconfig, DESCHEDULER_MANIFEST)

    for sid, _desc, _expect, status, detail in results:
        log("  %-26s %-5s %s" % (sid, status, detail))
    write_xlsx(args, cfg, plan, results, placements, rebalance, prices)
    if args.md:
        md_path = "%s/spot_sim_%s_%s.md" % (args.out, cfg["cluster"]["name"],
                                            time.strftime("%Y%m%d_%H%M%S"))
        write_md(md_path, cfg, plan, results, scenario_yaml, prices)
    failed = [r for r in results if r[3] == "FAIL"]
    if failed:
        sys.exit(1)
