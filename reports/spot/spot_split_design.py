"""Spot node-pool SPLIT DESIGN for clusters with team-dedicated node pools
(the "Korea BU" pattern: namespace + node-pool segregation per team).

Produces present-state vs future-state design: for every team's on-demand
pool, a paired spot pool sized to take --spot-target of the nodes while an
on-demand floor (--od-floor) absorbs stateful/critical pods and eviction
waves. Everything is read-only ARM data (node labels/taints are visible via
agentPoolProfiles - no kubectl needed); the changes themselves are delivered
as az CLI commands for the platform team and YAML snippets for the BUs.

Outputs:
  reports/aks_spot_split_<cluster>_<ts>.xlsx   multi-tab design workbook
  reports/aks_spot_split_<cluster>_<ts>.md     design doc with Mermaid
        current/future diagrams - convert for hand-off with:
        uv run python aks_report.py convert <file>.md --to pdf

Usage:
  uv run python aks_report.py spot-design --cluster aks-kr-dev-01
  uv run python spot_split_design.py --cluster aks-kr-dev-01 --teams teams.csv
  uv run python spot_split_design.py --cluster aks-kr-dev-01 --spot-target 0.7 --od-floor 0.2
"""
import csv
import datetime as dt
import math
import os
import re
import sys

import pandas as pd

from azrep import excel
from azrep.armextras import retail_vm_prices
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

TEAM_LABEL_KEYS = ("team", "bu", "squad", "tenant", "group", "owner", "project",
                   "dept", "business-unit", "business_unit", "app", "application")
GENERIC_POOL_NAMES = {"wrk", "usr", "user", "np", "np1", "np2", "nodepool",
                      "nodepool1", "pool", "default", "app", "apps", "worker",
                      "workers", "general", "linux", "win", "system", "sys"}
SPOT_TAINT = "kubernetes.azure.com/scalesetpriority=spot:NoSchedule"
POOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,11}$")
HOURS = 730


# --------------------------------------------------------------- team mapping
def parse_labels(joined):
    out = {}
    for part in (joined or "").split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def team_from_pool(pool, overrides):
    """Returns (team, source). Precedence: teams.csv -> labels -> taints -> name."""
    name = (pool["pool"] or "").lower()
    if name in overrides:
        return overrides[name]["team"], "teams.csv"
    labels = parse_labels(pool.get("node_labels"))
    for k, v in labels.items():
        base = k.split("/")[-1]
        if base in TEAM_LABEL_KEYS and v and v.lower() not in ("true", "spot"):
            return v.lower(), "node label %s=%s" % (k, v)
    for taint in (pool.get("taints") or "").split(";"):
        taint = taint.strip()
        m = re.match(r"^(?:dedicated|team|reserved-for|tenant)=([^:]+):", taint, re.I)
        if m:
            return m.group(1).lower(), "taint %s" % taint
    if name and name not in GENERIC_POOL_NAMES:
        stripped = re.sub(r"(pool|np|\d+)$", "", name) or name
        return stripped, "pool name heuristic"
    return "shared", "unattributed (no label/taint/name signal)"


def load_overrides(path):
    if not path:
        return {}
    if not os.path.exists(path):
        sys.exit("teams file not found: %s" % path)
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            low = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            if low.get("pool"):
                out[low["pool"].lower()] = {
                    "team": (low.get("team") or low["pool"]).lower(),
                    "namespaces": low.get("namespaces", ""),
                    "workload_type": low.get("workload_type", ""),
                    "criticality": low.get("criticality", ""),
                }
    return out


# ------------------------------------------------------------- future sizing
def spot_pool_name(team, pool, used):
    base = re.sub(r"[^a-z0-9]", "", (team or pool or "spot").lower())[:10] or "spot"
    if not base[0].isalpha():
        base = "s" + base[:9]
    cand = (base + "sp")[:12]
    i = 2
    while cand in used or not POOL_NAME_RE.match(cand):
        cand = (base[:9] + "sp%d" % i)[:12]
        i += 1
    used.add(cand)
    return cand


def alternate_sizes(vm_size):
    """Cheap heuristic suggestions for capacity depth - validate in-region."""
    m = re.match(r"^standard_([a-z]+)(\d+)([a-z]*)_?(v\d+)?$", (vm_size or "").lower())
    if not m:
        return ""
    fam, cpu, feat, ver = m.group(1).upper(), int(m.group(2)), m.group(3), m.group(4) or ""
    mk = lambda f, c: "Standard_%s%d%s%s" % (f, c, feat, ("_%s" % ver) if ver else "")
    alts = [mk(fam, cpu * 2)]
    if fam == "D":
        alts.append(mk("E", cpu))
    elif fam == "E":
        alts.append(mk("D", cpu))
    sib = {"v3": "v5", "v4": "v5", "v5": "v6"}.get(ver)
    if sib:
        alts.append("Standard_%s%d%s_%s" % (fam, cpu, feat, sib))
    return ", ".join(dict.fromkeys(alts))


def plan_split(pool, team, args, used_names):
    count = max(int(pool["count"] or 0), 1)
    cur_max = int(pool["max_count"] or 0) or count
    od_keep = max(1, math.ceil(count * args.od_floor))
    spot_initial = max(1, count - od_keep)
    spot_max = max(spot_initial, math.ceil((cur_max - od_keep) * (1 + args.headroom)))
    name = spot_pool_name(team, pool["pool"], used_names)
    team_taints = [t.strip() for t in (pool.get("taints") or "").split(";")
                   if t.strip() and "scalesetpriority" not in t]
    return {
        "team": team, "od_pool": pool["pool"], "spot_pool": name,
        "vm_size": pool["vm_size"], "alternate_sizes": alternate_sizes(pool["vm_size"]),
        "zones": pool["zones"], "current_nodes": count, "current_max": cur_max,
        "od_keep_nodes": od_keep, "spot_initial_nodes": spot_initial,
        "spot_min": 0, "spot_max": spot_max,
        "spot_share_target": "%.0f%%" % (100.0 * spot_initial / count),
        "team_taints": team_taints,
        "labels": parse_labels(pool.get("node_labels")),
    }


def az_add_command(cl, plan):
    parts = [
        "az aks nodepool add",
        "--resource-group %s" % cl["resource_group"],
        "--cluster-name %s" % cl["cluster"],
        "--name %s" % plan["spot_pool"],
        "--mode User",
        "--priority Spot",
        "--eviction-policy Delete",
        "--spot-max-price -1",
        "--node-vm-size %s" % plan["vm_size"],
        "--node-count %d" % plan["spot_initial_nodes"],
        "--enable-cluster-autoscaler",
        "--min-count %d" % plan["spot_min"],
        "--max-count %d" % plan["spot_max"],
    ]
    if plan["zones"]:
        parts.append("--zones %s" % " ".join(plan["zones"].split(",")))
    labels = dict(plan["labels"])
    labels.setdefault("team", plan["team"])
    labels["pooltype"] = "spot"
    parts.append("--labels " + " ".join("%s=%s" % (k, v) for k, v in sorted(labels.items())))
    if plan["team_taints"]:
        parts.append("--node-taints \"%s\"" % ",".join(plan["team_taints"]))
    return " ".join(parts)


def az_shrink_command(cl, plan, pool):
    if pool["autoscaling"]:
        return ("az aks nodepool update --resource-group %s --cluster-name %s "
                "--name %s --update-cluster-autoscaler --min-count %d --max-count %d"
                % (cl["resource_group"], cl["cluster"], plan["od_pool"],
                   min(int(pool["min_count"] or 1), plan["od_keep_nodes"]),
                   max(plan["od_keep_nodes"], 1)))
    return ("az aks nodepool scale --resource-group %s --cluster-name %s "
            "--name %s --node-count %d"
            % (cl["resource_group"], cl["cluster"], plan["od_pool"],
               plan["od_keep_nodes"]))


def workload_yaml(plan):
    tol = ['      - key: "kubernetes.azure.com/scalesetpriority"',
           '        operator: "Equal"', '        value: "spot"',
           '        effect: "NoSchedule"']
    for t in plan["team_taints"]:
        m = re.match(r"^([^=]+)=([^:]+):(\w+)$", t)
        if m:
            tol += ['      - key: "%s"' % m.group(1),
                    '        operator: "Equal"', '        value: "%s"' % m.group(2),
                    '        effect: "%s"' % m.group(3)]
    team_label = next((("%s" % k, v) for k, v in plan["labels"].items()
                       if k.split("/")[-1] in TEAM_LABEL_KEYS), None)
    req = ""
    if team_label:
        req = ("            - matchExpressions:\n"
               "                - {key: \"%s\", operator: In, values: [\"%s\"]}\n"
               % team_label)
    return (
        "# team %s - schedule on team pools, PREFER spot, spread od/spot + zones\n"
        "spec:\n  template:\n    spec:\n      tolerations:\n%s\n"
        "      affinity:\n        nodeAffinity:\n"
        "%s"
        "          preferredDuringSchedulingIgnoredDuringExecution:\n"
        "            - weight: 100\n              preference:\n"
        "                matchExpressions:\n"
        "                  - {key: \"kubernetes.azure.com/scalesetpriority\", operator: In, values: [\"spot\"]}\n"
        "      topologySpreadConstraints:\n"
        "        - maxSkew: 1\n          topologyKey: topology.kubernetes.io/zone\n"
        "          whenUnsatisfiable: ScheduleAnyway\n"
        "          labelSelector: {matchLabels: {app: <YOUR-APP-LABEL>}}\n"
        "        - maxSkew: 2\n          topologyKey: kubernetes.azure.com/agentpool\n"
        "          whenUnsatisfiable: ScheduleAnyway\n"
        "          labelSelector: {matchLabels: {app: <YOUR-APP-LABEL>}}\n"
        "---\n"
        "apiVersion: policy/v1\nkind: PodDisruptionBudget\n"
        "metadata: {name: <app>-pdb, namespace: <team-namespace>}\n"
        "spec:\n  minAvailable: 50%%\n  selector: {matchLabels: {app: <YOUR-APP-LABEL>}}\n"
        % (plan["team"], "\n".join(tol),
           ("          requiredDuringSchedulingIgnoredDuringExecution:\n"
            "            nodeSelectorTerms:\n" + req) if req else "")
    )


# ------------------------------------------------------------------ document
def mermaid_doc(cl, mapping, plans, overrides, args, gen_ts):
    cur, fut = ["flowchart LR"], ["flowchart LR"]
    cur.append('  C["%s\\n%s / %s"]' % (cl["cluster"], cl["location"], cl["environment"]))
    fut.append('  C["%s (future)"]' % cl["cluster"])
    for i, row in enumerate(mapping):
        nid = "P%d" % i
        cur.append('  C --> %s["%s\\n%s x%d %s\\nteam: %s"]'
                   % (nid, row["pool"], row["vm_size"], row["nodes"],
                      row["priority"], row["team"]))
    for i, p in enumerate(plans):
        fut.append('  C --> O%d["%s (on-demand floor)\\n%s x%d"]'
                   % (i, p["od_pool"], p["vm_size"], p["od_keep_nodes"]))
        fut.append('  C --> S%d["%s (SPOT, new)\\n%s %d-%d nodes"]'
                   % (i, p["spot_pool"], p["vm_size"], p["spot_min"], p["spot_max"]))
        fut.append("  O%d -. team %s .- S%d" % (i, p["team"], i))
    lines = [
        "# Spot Node-Pool Split Design - %s" % cl["cluster"],
        "",
        "Generated %s | subscription **%s** | region **%s** | environment **%s**" %
        (gen_ts, cl["subscription"], cl["location"], cl["environment"]),
        "",
        "Parameters: spot target %.0f%%, on-demand floor %.0f%%, autoscaler headroom +%.0f%%."
        % (args.spot_target * 100, args.od_floor * 100, args.headroom * 100),
        "",
        "## Present state",
        "",
        "```mermaid", *cur, "```",
        "",
        "## Future state",
        "",
        "Each team keeps a small on-demand floor (stateful/critical pods, eviction "
        "buffer) and gains a paired spot pool carrying the bulk of dev capacity. "
        "AKS adds the spot taint automatically; team taints are preserved so "
        "namespace/pool segregation is unchanged.",
        "",
        "```mermaid", *fut, "```",
        "",
        "## Per-team changes",
        "",
        "| Team | OD pool (floor) | New spot pool | VM size | Now | Floor | Spot init | Spot max |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in plans:
        lines.append("| %s | %s x%d | %s | %s | %d | %d | %d | %d |"
                     % (p["team"], p["od_pool"], p["od_keep_nodes"], p["spot_pool"],
                        p["vm_size"], p["current_nodes"], p["od_keep_nodes"],
                        p["spot_initial_nodes"], p["spot_max"]))
    lines += [
        "",
        "## Rollout",
        "",
        "1. **Prerequisites** - confirm spot vCPU quota per family/region, PDBs and >=2 "
        "replicas on the workloads that will tolerate spot, graceful shutdown <=25s "
        "(spot eviction notice is ~30s).",
        "2. **Pilot** - lowest-criticality team first%s: create its spot pool, BU adds "
        "tolerations/affinity/spread from the workbook, observe 1-2 weeks." %
        (" (per teams.csv criticality)" if overrides else ""),
        "3. **Expand** - remaining teams; set spot pool min-count to 0.",
        "4. **Shrink** - reduce each on-demand pool to its floor (commands in the "
        "workbook), then track results with `uv run python aks_report.py spot-detail` "
        "and `uv run python aks_report.py cost`.",
        "",
        "Convert this document for hand-off: `uv run python aks_report.py convert <this file> --to pdf`",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- main
def main(argv=None):
    p = base_parser("Spot node-pool split design (team-dedicated pools)")
    p.add_argument("--teams", help="optional teams.csv: pool,team,namespaces,workload_type,criticality")
    p.add_argument("--spot-target", type=float, default=0.6,
                   help="fraction of each team pool's nodes to move to spot")
    p.add_argument("--od-floor", type=float, default=0.25,
                   help="fraction of nodes kept as on-demand floor (min 1 node)")
    p.add_argument("--headroom", type=float, default=0.3,
                   help="extra autoscaler max-count to absorb eviction churn")
    p.add_argument("--no-prices", action="store_true", help="skip retail price lookups")
    p.add_argument("--no-md", action="store_true", help="skip the Mermaid design doc")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        sys.exit("No clusters in scope.")
    if len(clusters) > 1:
        if not sys.stdin.isatty():
            sys.exit("Scope matched %d clusters - narrow with --cluster <name>. Matches: %s"
                     % (len(clusters), ", ".join(c["cluster"] for c in clusters[:15])))
        for i, c in enumerate(clusters, 1):
            print("  %2d) %-40s %-25s env=%s" % (i, c["cluster"], c["subscription"][:25],
                                                 c["environment"]))
        cl = clusters[int(input("Design for which cluster [number]: ")) - 1]
    else:
        cl = clusters[0]
    cpools = [q for q in pools if q["cluster"] == cl["cluster"]]
    overrides = load_overrides(args.teams)
    log("Designing spot split for %s (%d node pools)" % (cl["cluster"], len(cpools)))

    used_names = {q["pool"].lower() for q in cpools}
    mapping, plans, skipped = [], [], []
    for q in cpools:
        team, source = team_from_pool(q, overrides)
        ov = overrides.get((q["pool"] or "").lower(), {})
        mapping.append({
            "pool": q["pool"], "mode": q["mode"], "priority": q["priority"],
            "vm_size": q["vm_size"], "nodes": q["count"],
            "autoscaling": "%s (%s-%s)" % (q["autoscaling"], q["min_count"], q["max_count"])
            if q["autoscaling"] else "off",
            "zones": q["zones"], "team": team, "attribution": source,
            "namespaces": ov.get("namespaces", "(unknown - not visible via ARM; supply teams.csv)"),
            "workload_type": ov.get("workload_type", ""),
            "criticality": ov.get("criticality", ""),
            "taints": q["taints"], "labels": q["node_labels"],
        })
        if q["mode"].lower() == "system":
            skipped.append((q["pool"], "system pool - must stay on-demand (AKS requirement)"))
        elif q["priority"].lower() == "spot":
            skipped.append((q["pool"], "already a spot pool - no change"))
        elif q["os_type"].lower() == "windows":
            skipped.append((q["pool"], "Windows pool - design covers Linux pools; handle separately"))
        elif q["power_state"].lower() == "stopped":
            skipped.append((q["pool"], "pool is stopped"))
        else:
            plans.append((plan_split(q, team, args, used_names), q))

    if not plans:
        log("No eligible user pools to split (see CurrentState tab for why).")
    prices = {}
    if not args.no_prices:
        need = sorted({(cl["location"], pl["vm_size"]) for pl, _ in plans})
        log("Retail price lookup for %d (region,size) combos..." % len(need))
        for region, size in need:
            prices[(region, size)] = retail_vm_prices(region, size) or {}

    plan_rows, cmd_rows, yaml_rows, sav_rows = [], [], [], []
    for pl, q in plans:
        plan_rows.append({k: (", ".join(v) if isinstance(v, list) else
                              (str(v) if isinstance(v, dict) else v))
                          for k, v in pl.items() if k != "labels"})
        cmd_rows.append({"order": 1, "phase": "pilot/expand", "team": pl["team"],
                         "purpose": "create spot pool %s" % pl["spot_pool"],
                         "command": az_add_command(cl, pl)})
        cmd_rows.append({"order": 2, "phase": "shrink", "team": pl["team"],
                         "purpose": "reduce %s to on-demand floor (%d)"
                                    % (pl["od_pool"], pl["od_keep_nodes"]),
                         "command": az_shrink_command(cl, pl, q)})
        yaml_rows.append({"team": pl["team"], "applies_to": "deployments moving to spot",
                          "yaml": workload_yaml(pl)})
        pr = prices.get((cl["location"], pl["vm_size"]), {})
        sav_rows.append({"team": pl["team"], "vm_size": pl["vm_size"],
                         "nodes_moved": pl["spot_initial_nodes"],
                         "od_hr": pr.get("od_hr"), "spot_hr": pr.get("spot_hr")})

    sav = pd.DataFrame(sav_rows)
    if not sav.empty:
        n = len(sav)
        sav["discount %"] = ["=IF(OR(D%d=\"\",E%d=\"\",D%d=0),\"\",1-E%d/D%d)" % (r, r, r, r, r)
                             for r in range(2, n + 2)]
        sav["est monthly saving (USD)"] = ["=IF(OR(D%d=\"\",E%d=\"\"),\"\",C%d*%d*(D%d-E%d))"
                                           % (r, r, r, HOURS, r, r) for r in range(2, n + 2)]

    prereq = pd.DataFrame([
        ("Cluster autoscaler expander", cl.get("autoscaler_expander") or "(default: random)",
         "consider 'priority' + priority-expander ConfigMap (BU applies; needs kubectl) "
         "or rely on workload spot affinity"),
        ("balance-similar-node-groups", str(cl.get("autoscaler_balance_similar_node_groups") or ""),
         "set true when multiple similar spot pools exist"),
        ("Kubernetes version", cl.get("current_kubernetes_version") or cl.get("kubernetes_version"),
         "spot pools inherit control-plane version; upgrade first if near EOL"),
        ("SKU tier", cl.get("sku_tier"), "Free tier has no uptime SLA - acceptable in dev"),
        ("Spot vCPU quota", "check per family/region",
         "az vm list-usage --location %s -o table | findstr -i spot" % cl["location"]),
        ("Eviction handling", "~30s notice, simulate before pilot",
         "az vmss simulate-eviction (platform team) on a pilot node"),
        ("Namespace segregation", "unchanged",
         "team taints/labels are copied to the spot pools, so namespace->pool pinning is preserved"),
    ], columns=["item", "current", "guidance"])

    rollout = pd.DataFrame([
        (0, "Prerequisites", "platform+BU", "quota, PDBs, >=2 replicas, graceful shutdown <=25s, "
         "pick pilot team (lowest criticality in teams.csv)"),
        (1, "Pilot", "platform", "create pilot team's spot pool (AzCommands order 1); BU applies "
         "WorkloadChanges YAML to stateless deployments; observe eviction rate & pending pods 1-2 weeks"),
        (2, "Expand", "platform+BU", "create remaining spot pools; BUs migrate; keep od pools untouched"),
        (3, "Shrink", "platform", "reduce od pools to the floor (AzCommands order 2); set spot min-count 0"),
        (4, "Steady state", "all", "review monthly with `uv run python aks_report.py spot-detail` "
         "and `uv run python aks_report.py cost`; tune --od-floor down as confidence grows"),
    ], columns=["phase", "name", "owner", "actions"])

    risks = pd.DataFrame([
        ("Eviction bursts", "whole spot pool can drain at once",
         "on-demand floor + PDBs + zone/pool topology spread + autoscaler headroom"),
        ("Capacity unavailability", "a single SKU may be unavailable as spot",
         "alternate_sizes column lists same-family fallbacks; create a second spot pool if needed"),
        ("Stateful/licensed workloads", "not spot-safe",
         "keep on the od floor pool; teams.csv workload_type marks batch vs api"),
        ("Quota", "spot quota is separate from regular vCPU quota",
         "request spot quota per family before Phase 2"),
        ("DaemonSets", "run on every node incl. spot - cost scales with node count",
         "review daemonset resource requests"),
        ("Scheduler drift", "pods may still prefer od pool when spot is full",
         "preferred (not required) spot affinity keeps workloads running - by design"),
    ], columns=["risk", "detail", "mitigation"])

    assessment = pd.DataFrame()
    try:
        from spot_cluster_report import assess_clusters
        pools_by_cluster = {}
        for q in cpools:
            pools_by_cluster.setdefault(q["cluster"], []).append(q)
        assessment = pd.DataFrame(assess_clusters([cl], pools_by_cluster))
    except Exception as e:  # keep the design report independent of that module
        log("note: spot assessment skipped (%s)" % e)

    # ---- write workbook ----
    wb = excel.new_workbook()
    excel.add_readme(wb, "Spot Node-Pool Split Design: %s" % cl["cluster"], [
        "Generated: %s   Subscription: %s   Region: %s   Env: %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), cl["subscription"],
         cl["location"], cl["environment"]),
        "Parameters: spot-target %.0f%%, od-floor %.0f%% (min 1 node), headroom +%.0f%%" %
        (args.spot_target * 100, args.od_floor * 100, args.headroom * 100),
        "",
        "Read-only design: node labels/taints come from ARM agentPoolProfiles (no kubectl).",
        "Namespace details are only as good as teams.csv - ARM cannot see namespaces.",
        "AzCommands = platform team (az CLI only). WorkloadChanges = BU teams (kubectl).",
        "System pools, existing spot pools, Windows and stopped pools are listed but not split.",
        "Savings use PUBLIC retail prices: EA/MCA discounts and RI/SP interplay not reflected.",
    ])
    excel.add_table(wb, "CurrentState", pd.DataFrame(mapping), max_width=60)
    excel.add_table(wb, "TeamMapping", pd.DataFrame(
        [{"pool": m["pool"], "team": m["team"], "attribution": m["attribution"],
          "namespaces": m["namespaces"], "workload_type": m["workload_type"],
          "criticality": m["criticality"]} for m in mapping]), max_width=70)
    if plan_rows:
        excel.add_table(wb, "FutureStatePools", pd.DataFrame(plan_rows),
                        int_cols=("current_nodes", "current_max", "od_keep_nodes",
                                  "spot_initial_nodes", "spot_min", "spot_max"))
        excel.add_table(wb, "AzCommands", pd.DataFrame(cmd_rows).sort_values(
            ["order", "team"]), max_width=120)
        excel.add_table(wb, "WorkloadChanges", pd.DataFrame(yaml_rows), max_width=110)
        excel.add_table(wb, "Savings", sav,
                        formats={"od_hr": "#,##0.0000", "spot_hr": "#,##0.0000",
                                 "est monthly saving (USD)": "#,##0.00"},
                        pct_cols=("discount %",), int_cols=("nodes_moved",))
    if skipped:
        excel.add_table(wb, "NotSplit", pd.DataFrame(skipped, columns=["pool", "reason"]),
                        max_width=80)
    excel.add_table(wb, "ClusterPrereqs", prereq, max_width=90)
    if not assessment.empty:
        excel.add_table(wb, "SpotAssessment", assessment, max_width=80,
                        fail_cols=("severity",), fail_values=("HIGH",),
                        warn_values=("WARN",))
    excel.add_table(wb, "RolloutPlan", rollout, max_width=100)
    excel.add_table(wb, "Risks", risks, max_width=90)

    stem = "aks_spot_split_%s" % cl["cluster"]
    path = excel.save(wb, out_path(args, stem, env_filter))
    log("Workbook written: %s" % path)

    if not args.no_md and plans:
        md = mermaid_doc(cl, mapping, [pl for pl, _ in plans], overrides, args,
                         dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        md_path = os.path.splitext(path)[0] + ".md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        log("Design doc written: %s (convert: uv run python aks_report.py convert %s --to pdf)"
            % (md_path, os.path.basename(md_path)))
    return path


if __name__ == "__main__":
    main()
