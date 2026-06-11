"""Detailed AKS spot cluster configuration and cost report.

This complements spot_opportunity.py. The opportunity report asks "where might
spot help?" This report asks "how are spot clusters configured today, what do
they cost, and what risks are visible from subscription-level data?"

Tabs: ReadMe, ClusterSpotSummary, SpotNodePools, OnDemandNodePools,
NodePoolSkuSummary, AutoscalerConfig, SpotAssessment, CostByCluster,
CostTrend, CostByNodePool, OtherCostItems, CostByMeter, RawResourceCost.

Usage:
  python spot_cluster_report.py --subs contoso-dev --env dev
  python spot_cluster_report.py --subs contoso-dev --only-spot-clusters
"""
import datetime as dt
import re
from collections import defaultdict

import pandas as pd

from azrep import excel
from azrep.costmgmt import CostClient, default_window, dim_in
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope

RG_CHUNK = 30
PM_ORDER = ["OnDemand", "Spot", "Reservation", "SavingsPlan"]
VMSS_RE = re.compile(r"/virtualmachinescalesets/aks-(.+?)-\d+-vmss$", re.I)


def chunks(values, size):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def pool_from_resource_id(resource_id):
    m = VMSS_RE.search(resource_id or "")
    if m:
        return m.group(1)
    return ""


def resource_name(resource_id):
    parts = str(resource_id or "").split("/")
    return parts[-1] if parts else ""


def resource_category(resource_id):
    rid = str(resource_id or "").lower()
    if "/virtualmachinescalesets/" in rid:
        return "node_pool_vmss"
    if "/disks/" in rid:
        return "managed_disks"
    if "/publicipaddresses/" in rid:
        return "public_ips"
    if "/loadbalancers/" in rid:
        return "load_balancers"
    if "/networkinterfaces/" in rid:
        return "network_interfaces"
    if "/managedclusters/" in rid:
        return "managed_cluster_fee"
    return "other"


def pool_max_nodes(pool):
    if pool.get("autoscaling") and pool.get("max_count") not in (None, ""):
        try:
            return int(pool["max_count"])
        except (TypeError, ValueError):
            pass
    return int(pool.get("count") or 0)


def pool_min_nodes(pool):
    if pool.get("autoscaling") and pool.get("min_count") not in (None, ""):
        try:
            return int(pool["min_count"])
        except (TypeError, ValueError):
            pass
    return int(pool.get("count") or 0)


def zones_count(pool):
    zones = [z for z in str(pool.get("zones") or "").split(",") if z.strip()]
    return len(zones)


def vm_family(size):
    text = str(size or "")
    m = re.match(r"Standard_([A-Za-z]+)", text)
    if not m:
        return text
    return m.group(1).lower()


def cost_attach(frames, rg_map):
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    key = list(zip(df["subscription_id"], df["ResourceGroupName"].str.lower()))
    df["cluster_id"] = [rg_map.get(k, {}).get("id", "") for k in key]
    df["cluster"] = [rg_map.get(k, {}).get("cluster", "(unmatched)") for k in key]
    df["subscription"] = [rg_map.get(k, {}).get("subscription", "") for k in key]
    df["environment"] = [rg_map.get(k, {}).get("environment", "") for k in key]
    df["location"] = [rg_map.get(k, {}).get("location", "") for k in key]
    df["Month"] = df["Period"].str[:7]
    return df


def collect_cost(session, clusters, months):
    by_sub = defaultdict(list)
    for c in clusters:
        if c["node_resource_group"]:
            by_sub[c["subscription_id"]].append(c)
    rg_map = {(c["subscription_id"], c["node_resource_group"].lower()): c for c in clusters}
    id_map = {c["id"].lower(): c for c in clusters}

    d_from, d_to = default_window(months)
    cost = CostClient(session)
    pm_rows, meter_rows, res_rows, fee_rows = [], [], [], []
    for i, (sid, cls) in enumerate(sorted(by_sub.items()), 1):
        rgs = sorted({c["node_resource_group"].lower() for c in cls})
        scope = "/subscriptions/%s" % sid
        log("[%d/%d] %s: cost for %d node resource group(s)"
            % (i, len(by_sub), cls[0]["subscription"], len(rgs)))
        for ch in chunks(rgs, RG_CHUNK):
            f = dim_in("ResourceGroupName", ch)
            df = cost.query(scope, "AmortizedCost", "Monthly",
                            ("ResourceGroupName", "PricingModel"), f, d_from, d_to)
            if not df.empty:
                df["subscription_id"] = sid
                pm_rows.append(df)
            df = cost.query(scope, "AmortizedCost", "Monthly",
                            ("ResourceGroupName", "ResourceId"), f, d_from, d_to)
            if not df.empty:
                df["subscription_id"] = sid
                res_rows.append(df)
            df = cost.query(scope, "AmortizedCost", "Monthly",
                            ("ResourceGroupName", "Meter"), f, d_from, d_to)
            if not df.empty:
                df["subscription_id"] = sid
                meter_rows.append(df)
        df = cost.query(scope, "AmortizedCost", "Monthly", ("ResourceId",),
                        dim_in("ResourceType", ["microsoft.containerservice/managedclusters"]),
                        d_from, d_to)
        if not df.empty:
            df["subscription_id"] = sid
            fee_rows.append(df)

    pm = cost_attach(pm_rows, rg_map)
    res = cost_attach(res_rows, rg_map)
    meter = cost_attach(meter_rows, rg_map)
    fees = pd.concat(fee_rows, ignore_index=True) if fee_rows else pd.DataFrame()
    if not fees.empty:
        fees["cluster_id"] = fees["ResourceId"].str.lower().map(
            lambda rid: id_map.get(rid, {}).get("id", ""))
        fees["cluster"] = fees["ResourceId"].str.lower().map(
            lambda rid: id_map.get(rid, {}).get("cluster", ""))
        fees["subscription"] = fees["ResourceId"].str.lower().map(
            lambda rid: id_map.get(rid, {}).get("subscription", ""))
        fees["environment"] = fees["ResourceId"].str.lower().map(
            lambda rid: id_map.get(rid, {}).get("environment", ""))
        fees["Month"] = fees["Period"].str[:7]
        fees = fees[fees["cluster_id"] != ""]
    return {"pm": pm, "res": res, "meter": meter, "fees": fees,
            "from": d_from, "to": d_to, "calls": cost.calls}


def pricing_split(pm, fees):
    if pm.empty:
        cols = ["cluster_id", "cluster", "subscription", "environment", "OnDemand", "Spot",
                "Reservation", "SavingsPlan", "Cluster fee", "Total (USD)", "Spot %"]
        return pd.DataFrame(columns=cols)
    split = pm.pivot_table(index=["cluster_id", "cluster", "subscription", "environment"],
                           columns="PricingModel", values="CostUSD", aggfunc="sum") \
        .fillna(0.0).reset_index()
    for col in PM_ORDER:
        if col not in split.columns:
            split[col] = 0.0
    pm_cols = [c for c in PM_ORDER if c in split.columns] + [
        c for c in split.columns
        if c not in ["cluster_id", "cluster", "subscription", "environment"] + PM_ORDER
    ]
    split = split[["cluster_id", "cluster", "subscription", "environment"] + pm_cols]
    fee_sum = fees.groupby("cluster_id")["CostUSD"].sum() if not fees.empty else pd.Series(dtype=float)
    split["Cluster fee"] = split["cluster_id"].map(fee_sum).fillna(0.0)
    split["Total (USD)"] = split[pm_cols].sum(axis=1) + split["Cluster fee"]
    split["Spot %"] = split.apply(
        lambda r: (float(r.get("Spot", 0.0)) / (float(r["Total (USD)"]) or 1.0))
        if float(r["Total (USD)"]) else 0.0, axis=1)
    return split


def cost_trend(pm, fees):
    if pm.empty:
        return pd.DataFrame(columns=["cluster", "subscription", "environment", "Month",
                                     "OnDemand", "Spot", "Reservation", "SavingsPlan",
                                     "Cluster fee", "Total (USD)", "Spot %"])
    trend = pm.pivot_table(index=["cluster_id", "cluster", "subscription", "environment", "Month"],
                           columns="PricingModel", values="CostUSD", aggfunc="sum") \
        .fillna(0.0).reset_index()
    for col in PM_ORDER:
        if col not in trend.columns:
            trend[col] = 0.0
    fee_m = fees.groupby(["cluster_id", "Month"])["CostUSD"].sum() if not fees.empty else pd.Series(dtype=float)
    trend["Cluster fee"] = [float(fee_m.get((r.cluster_id, r.Month), 0.0))
                            for r in trend.itertuples()]
    trend["Total (USD)"] = trend[PM_ORDER].sum(axis=1) + trend["Cluster fee"]
    trend["Spot %"] = trend.apply(
        lambda r: (r["Spot"] / r["Total (USD)"]) if r["Total (USD)"] else 0.0, axis=1)
    return trend[["cluster", "subscription", "environment", "Month"] + PM_ORDER +
                 ["Cluster fee", "Total (USD)", "Spot %"]]


def build_pool_rows(pools, want_spot):
    rows = []
    for q in pools:
        is_spot_pool = str(q.get("priority", "")).lower() == "spot"
        if is_spot_pool != want_spot:
            continue
        rows.append({
            "cluster": q["cluster"],
            "subscription": q["subscription"],
            "environment": q["environment"],
            "location": q["location"],
            "pool": q["pool"],
            "mode": q["mode"],
            "priority": q["priority"],
            "vm_size": q["vm_size"],
            "vm_family": vm_family(q["vm_size"]),
            "nodes": q["count"],
            "autoscaling": q["autoscaling"],
            "min_count": q["min_count"],
            "max_count": q["max_count"],
            "effective_min_nodes": pool_min_nodes(q),
            "effective_max_nodes": pool_max_nodes(q),
            "zones": q["zones"],
            "zones_count": zones_count(q),
            "eviction_policy": q["eviction_policy"],
            "spot_max_price": q["spot_max_price"],
            "spot_price_mode": ("pay_up_to_on_demand" if q["spot_max_price"] in (-1, "-1")
                                else ("capped" if q["spot_max_price"] not in (None, "") else "")),
            "taints": q["taints"],
            "spot_taint_present": q.get("spot_taint_present"),
            "expected_spot_taint": "kubernetes.azure.com/scalesetpriority=spot:NoSchedule"
            if is_spot_pool else "",
            "node_labels": q.get("node_labels"),
            "os_type": q["os_type"],
            "os_sku": q["os_sku"],
            "max_pods": q["max_pods"],
            "power_state": q["power_state"],
            "node_public_ip_enabled": q["node_public_ip_enabled"],
            "vnet_subnet_id": q["vnet_subnet_id"],
            "pod_subnet_id": q["pod_subnet_id"],
        })
    return rows


def sku_summary(pools):
    rows = []
    if not pools:
        return rows
    df = pd.DataFrame(pools)
    df["effective_min_nodes"] = [pool_min_nodes(q) for q in pools]
    df["effective_max_nodes"] = [pool_max_nodes(q) for q in pools]
    df["zones_count"] = [zones_count(q) for q in pools]
    df["vm_family"] = df["vm_size"].map(vm_family)
    for keys, grp in df.groupby(["cluster", "subscription", "environment", "priority",
                                 "mode", "vm_size", "vm_family"], dropna=False):
        rows.append({
            "cluster": keys[0],
            "subscription": keys[1],
            "environment": keys[2],
            "priority": keys[3],
            "mode": keys[4],
            "vm_size": keys[5],
            "vm_family": keys[6],
            "node_pools": len(grp),
            "current_nodes": int(grp["count"].sum()),
            "effective_min_nodes": int(grp["effective_min_nodes"].sum()),
            "effective_max_nodes": int(grp["effective_max_nodes"].sum()),
            "zones_count_max": int(grp["zones_count"].max()),
            "pools": ", ".join(sorted(str(x) for x in grp["pool"])),
        })
    return rows


def autoscaler_rows(clusters, pools_by_cluster):
    rows = []
    for c in clusters:
        ps = pools_by_cluster.get(c["cluster"], [])
        rows.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "spot_pools": c["spot_pools"],
            "autoscaling_pools": c["autoscaling_pools"],
            "expander": c.get("autoscaler_expander"),
            "balance_similar_node_groups": c.get("autoscaler_balance_similar_node_groups"),
            "scan_interval": c.get("autoscaler_scan_interval"),
            "scale_down_delay_after_add": c.get("autoscaler_scale_down_delay_after_add"),
            "scale_down_unneeded_time": c.get("autoscaler_scale_down_unneeded_time"),
            "scale_down_utilization_threshold": c.get("autoscaler_scale_down_utilization_threshold"),
            "skip_nodes_with_local_storage": c.get("autoscaler_skip_nodes_with_local_storage"),
            "skip_nodes_with_system_pods": c.get("autoscaler_skip_nodes_with_system_pods"),
            "autoscaled_spot_pools": sum(1 for p in ps if p["priority"].lower() == "spot" and p["autoscaling"]),
            "autoscaled_on_demand_pools": sum(1 for p in ps if p["priority"].lower() != "spot" and p["autoscaling"]),
            "cluster_max_nodes": sum(pool_max_nodes(p) for p in ps),
            "spot_max_nodes": sum(pool_max_nodes(p) for p in ps if p["priority"].lower() == "spot"),
            "on_demand_max_nodes": sum(pool_max_nodes(p) for p in ps if p["priority"].lower() != "spot"),
        })
    return rows


def add_assessment(rows, c, severity, check, result, evidence, recommendation):
    rows.append({
        "cluster": c["cluster"],
        "subscription": c["subscription"],
        "environment": c["environment"],
        "severity": severity,
        "check": check,
        "result": result,
        "evidence": evidence,
        "recommendation": recommendation,
    })


def assess_clusters(clusters, pools_by_cluster):
    rows = []
    for c in clusters:
        ps = pools_by_cluster.get(c["cluster"], [])
        spot = [p for p in ps if p["priority"].lower() == "spot"]
        ondemand = [p for p in ps if p["priority"].lower() != "spot"]
        system_ondemand = [p for p in ondemand if p["mode"].lower() == "system" and p["count"] > 0]
        user_ondemand = [p for p in ondemand if p["mode"].lower() == "user" and p["count"] > 0]
        spot_families = {vm_family(p["vm_size"]) for p in spot if p["vm_size"]}
        spot_zones = {z.strip() for p in spot for z in str(p.get("zones") or "").split(",") if z.strip()}
        spot_max_nodes = sum(pool_max_nodes(p) for p in spot)

        add_assessment(rows, c, "INFO" if spot else "WARN", "has_spot_node_pool",
                       "PASS" if spot else "FAIL",
                       "%d spot pool(s), %d spot node(s)" % (
                           len(spot), sum(p["count"] for p in spot)),
                       "Use spot only for disruption-tolerant workloads; keep critical workloads on regular pools.")
        add_assessment(rows, c, "HIGH" if is_prod(c["environment"]) and spot else "INFO",
                       "spot_in_prod", "FAIL" if is_prod(c["environment"]) and spot else "PASS",
                       "environment=%s, spot_pools=%d" % (c["environment"], len(spot)),
                       "Avoid spot in prod unless workloads are explicitly designed for eviction.")
        add_assessment(rows, c, "HIGH" if not system_ondemand else "INFO",
                       "system_on_demand_pool", "PASS" if system_ondemand else "FAIL",
                       "%d system on-demand pool(s)" % len(system_ondemand),
                       "Keep at least one regular System pool for core services.")
        if spot:
            add_assessment(rows, c, "WARN" if not user_ondemand else "INFO",
                           "regular_user_fallback", "PASS" if user_ondemand else "WARN",
                           "%d regular user pool(s)" % len(user_ondemand),
                           "Maintain regular capacity for workloads that cannot tolerate spot eviction.")
            add_assessment(rows, c, "WARN" if len(spot_zones) < 2 else "INFO",
                           "spot_multi_zone", "PASS" if len(spot_zones) >= 2 else "WARN",
                           "spot zones=%s" % (", ".join(sorted(spot_zones)) or "(none)"),
                           "Spread spot pools across zones where the region supports it.")
            add_assessment(rows, c, "WARN" if len(spot_families) < 2 else "INFO",
                           "spot_multi_vm_family", "PASS" if len(spot_families) >= 2 else "WARN",
                           "spot VM families=%s" % (", ".join(sorted(spot_families)) or "(none)"),
                           "Use multiple VM families/SKUs to reduce spot capacity concentration.")
            capped = [p for p in spot if p.get("spot_max_price") not in (-1, "-1", None, "")]
            add_assessment(rows, c, "WARN" if capped else "INFO",
                           "spot_price_cap", "WARN" if capped else "PASS",
                           "%d capped spot pool(s)" % len(capped),
                           "Price caps can cause eviction when price rises; -1 pays up to on-demand.")
            no_auto = [p for p in spot if not p["autoscaling"]]
            add_assessment(rows, c, "WARN" if no_auto else "INFO",
                           "spot_autoscaling", "WARN" if no_auto else "PASS",
                           "%d spot pool(s) without autoscaling" % len(no_auto),
                           "Autoscaling makes spot pools more elastic and easier to cap.")
            positive_min = [p for p in spot if pool_min_nodes(p) > 0]
            add_assessment(rows, c, "WARN" if positive_min else "INFO",
                           "spot_min_capacity", "WARN" if positive_min else "PASS",
                           "effective spot min nodes=%d, max nodes=%d" % (
                               sum(pool_min_nodes(p) for p in spot), spot_max_nodes),
                           "Keep spot min at 0 unless workload scheduling requires always-on spot capacity.")
            missing_taint = [p for p in spot if not p.get("spot_taint_present")]
            add_assessment(rows, c, "WARN" if missing_taint else "INFO",
                           "spot_taint_visible", "WARN" if missing_taint else "PASS",
                           "spot pools without visible taint=%d" % len(missing_taint),
                           "Ensure workloads use tolerations for the AKS spot taint.")
            add_assessment(rows, c, "WARN" if not c.get("autoscaler_expander") else "INFO",
                           "autoscaler_expander", "WARN" if not c.get("autoscaler_expander") else "PASS",
                           "expander=%s" % (c.get("autoscaler_expander") or "(default/unknown)"),
                           "For mixed spot/on-demand pools, review expander behavior; priority expander may be useful.")
            b = str(c.get("autoscaler_balance_similar_node_groups") or "").lower()
            multi_similar = len(spot) >= 2 and len({p["vm_size"] for p in spot}) < len(spot)
            add_assessment(rows, c, "WARN" if multi_similar and b not in ("true", "1") else "INFO",
                           "balance_similar_node_groups",
                           "WARN" if multi_similar and b not in ("true", "1") else "PASS",
                           "balanceSimilarNodeGroups=%s" % (c.get("autoscaler_balance_similar_node_groups") or "(default/unknown)"),
                           "Enable balance-similar-node-groups when using similar pools across zones.")
    return rows


def nodepool_cost_rows(res, pools):
    if res.empty:
        return []
    cfg = {(p["cluster"], p["pool"]): p for p in pools}
    r = res.copy()
    r["pool"] = r["ResourceId"].map(pool_from_resource_id)
    r = r[r["pool"] != ""]
    rows = []
    for keys, grp in r.groupby(["cluster", "subscription", "environment", "pool"], dropna=False):
        p = cfg.get((keys[0], keys[3]), {})
        rows.append({
            "cluster": keys[0],
            "subscription": keys[1],
            "environment": keys[2],
            "pool": keys[3],
            "priority": p.get("priority", ""),
            "mode": p.get("mode", ""),
            "vm_size": p.get("vm_size", ""),
            "nodes": p.get("count"),
            "autoscaling": p.get("autoscaling"),
            "effective_max_nodes": pool_max_nodes(p) if p else None,
            "window_cost": float(grp["CostUSD"].sum()),
            "months": ", ".join(sorted(set(grp["Month"]))),
            "resource_count": grp["ResourceId"].nunique(),
        })
    return rows


def other_cost_rows(res, fees):
    rows = []
    if not res.empty:
        r = res.copy()
        r["category"] = r["ResourceId"].map(resource_category)
        r["resource_name"] = r["ResourceId"].map(resource_name)
        r = r[r["category"] != "node_pool_vmss"]
        for keys, grp in r.groupby(["cluster", "subscription", "environment", "category",
                                    "resource_name", "ResourceId"], dropna=False):
            rows.append({
                "cluster": keys[0], "subscription": keys[1], "environment": keys[2],
                "category": keys[3], "resource_name": keys[4], "resource_id": keys[5],
                "window_cost": float(grp["CostUSD"].sum()),
                "months": ", ".join(sorted(set(grp["Month"]))),
            })
    if not fees.empty:
        for keys, grp in fees.groupby(["cluster", "subscription", "environment", "ResourceId"],
                                      dropna=False):
            rows.append({
                "cluster": keys[0], "subscription": keys[1], "environment": keys[2],
                "category": "managed_cluster_fee",
                "resource_name": resource_name(keys[3]),
                "resource_id": keys[3],
                "window_cost": float(grp["CostUSD"].sum()),
                "months": ", ".join(sorted(set(grp["Month"]))),
            })
    return rows


def meter_cost_rows(meter):
    if meter.empty:
        return []
    rows = []
    for keys, grp in meter.groupby(["cluster", "subscription", "environment", "Meter"],
                                   dropna=False):
        rows.append({
            "cluster": keys[0], "subscription": keys[1], "environment": keys[2],
            "meter": keys[3], "window_cost": float(grp["CostUSD"].sum()),
            "months": ", ".join(sorted(set(grp["Month"]))),
        })
    return rows


def cluster_summary_rows(clusters, pools_by_cluster, split):
    split_by_cluster = split.set_index("cluster_id") if not split.empty else pd.DataFrame()
    rows = []
    for c in clusters:
        ps = pools_by_cluster.get(c["cluster"], [])
        spot = [p for p in ps if p["priority"].lower() == "spot"]
        ondemand = [p for p in ps if p["priority"].lower() != "spot"]
        system_ondemand = [p for p in ondemand if p["mode"].lower() == "system"]
        row = split_by_cluster.loc[c["id"]] if c["id"] in split_by_cluster.index else {}
        total = float(row.get("Total (USD)", 0.0)) if hasattr(row, "get") else 0.0
        rows.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "location": c["location"],
            "has_spot": bool(spot),
            "spot_pools": len(spot),
            "spot_nodes": sum(p["count"] for p in spot),
            "on_demand_pools": len(ondemand),
            "on_demand_nodes": sum(p["count"] for p in ondemand),
            "system_on_demand": bool(system_ondemand),
            "total_nodes": c["total_nodes"],
            "spot_vm_sizes": ", ".join(sorted({p["vm_size"] for p in spot if p["vm_size"]})),
            "on_demand_vm_sizes": ", ".join(sorted({p["vm_size"] for p in ondemand if p["vm_size"]})),
            "spot_vm_families": ", ".join(sorted({vm_family(p["vm_size"]) for p in spot if p["vm_size"]})),
            "spot_multi_zone": len({z.strip() for p in spot for z in str(p.get("zones") or "").split(",") if z.strip()}) >= 2,
            "spot_multi_vm_family": len({vm_family(p["vm_size"]) for p in spot if p["vm_size"]}) >= 2,
            "spot_max_nodes": sum(pool_max_nodes(p) for p in spot),
            "cluster_max_nodes": sum(pool_max_nodes(p) for p in ps),
            "autoscaling_pools": c["autoscaling_pools"],
            "autoscaler_expander": c.get("autoscaler_expander"),
            "balance_similar_node_groups": c.get("autoscaler_balance_similar_node_groups"),
            "sku_tier": c["sku_tier"],
            "support_plan": c["support_plan"],
            "power_state": c["power_state"],
            "OnDemand": float(row.get("OnDemand", 0.0)) if hasattr(row, "get") else 0.0,
            "Spot": float(row.get("Spot", 0.0)) if hasattr(row, "get") else 0.0,
            "Reservation": float(row.get("Reservation", 0.0)) if hasattr(row, "get") else 0.0,
            "SavingsPlan": float(row.get("SavingsPlan", 0.0)) if hasattr(row, "get") else 0.0,
            "Cluster fee": float(row.get("Cluster fee", 0.0)) if hasattr(row, "get") else 0.0,
            "Total (USD)": total,
            "Spot %": float(row.get("Spot %", 0.0)) if hasattr(row, "get") else 0.0,
        })
    return rows


def main(argv=None):
    p = base_parser("Detailed AKS spot cluster configuration and cost report")
    p.add_argument("--months", type=int, default=3, help="full months of cost history")
    p.add_argument("--only-spot-clusters", action="store_true",
                   help="include only clusters that currently have at least one spot node pool")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.15)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if args.only_spot_clusters:
        spot_cluster_ids = {p["cluster_id"] for p in pools if p["priority"].lower() == "spot"}
        clusters = [c for c in clusters if c["id"] in spot_cluster_ids]
        pools = [p for p in pools if p["cluster_id"] in spot_cluster_ids]
    if not clusters:
        log("No clusters in scope.")
        return

    pools_by_cluster = defaultdict(list)
    for p0 in pools:
        pools_by_cluster[p0["cluster"]].append(p0)

    log("Collecting spot configuration and amortized cost for %d cluster(s)..." % len(clusters))
    cost = collect_cost(session, clusters, args.months)
    split = pricing_split(cost["pm"], cost["fees"])
    trend = cost_trend(cost["pm"], cost["fees"])

    summary = pd.DataFrame(cluster_summary_rows(clusters, pools_by_cluster, split))
    spot_pools = pd.DataFrame(build_pool_rows(pools, True))
    ondemand_pools = pd.DataFrame(build_pool_rows(pools, False))
    sku = pd.DataFrame(sku_summary(pools))
    autoscaler = pd.DataFrame(autoscaler_rows(clusters, pools_by_cluster))
    assessment = pd.DataFrame(assess_clusters(clusters, pools_by_cluster))
    pool_cost = pd.DataFrame(nodepool_cost_rows(cost["res"], pools))
    other_cost = pd.DataFrame(other_cost_rows(cost["res"], cost["fees"]))
    meter_cost = pd.DataFrame(meter_cost_rows(cost["meter"]))
    raw_res = cost["res"].copy()
    if not raw_res.empty:
        raw_res["resource_category"] = raw_res["ResourceId"].map(resource_category)
        raw_res["resource_name"] = raw_res["ResourceId"].map(resource_name)

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Spot Cluster Configuration and Cost Report", [
        "Generated: %s   Scope: %s   Cost window: %s to %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all",
         cost["from"], cost["to"]),
        "Clusters in scope: %d   Node pools: %d   Cost Management calls: %d" %
        (len(clusters), len(pools), cost["calls"]),
        "",
        "This report focuses on actual spot configuration: spot pools, regular pools,",
        "autoscaler settings, price caps, eviction policy, zones, VM families, taints,",
        "and cost split between Spot / OnDemand / Reservation / SavingsPlan.",
        "",
        "Cost comes from the AKS node resource group (MC_*) plus the managed-cluster",
        "resource fee. Pool cost is inferred from VMSS resource ids; disks, public IPs",
        "and other non-VMSS charges are shown in OtherCostItems.",
        "",
        "No kubectl access is used, so pod tolerations, priority expander ConfigMaps,",
        "PodDisruptionBudgets and application workload criticality are not visible.",
    ])
    excel.add_table(wb, "ClusterSpotSummary", summary,
                    money_cols=("OnDemand", "Spot", "Reservation", "SavingsPlan",
                                "Cluster fee", "Total (USD)"),
                    pct_cols=("Spot %",),
                    int_cols=("spot_pools", "spot_nodes", "on_demand_pools",
                              "on_demand_nodes", "total_nodes", "spot_max_nodes",
                              "cluster_max_nodes", "autoscaling_pools"))
    excel.add_table(wb, "SpotNodePools", spot_pools,
                    int_cols=("nodes", "min_count", "max_count", "effective_min_nodes",
                              "effective_max_nodes", "zones_count", "max_pods"),
                    fail_cols=("spot_taint_present",), fail_values=(False,))
    excel.add_table(wb, "OnDemandNodePools", ondemand_pools,
                    int_cols=("nodes", "min_count", "max_count", "effective_min_nodes",
                              "effective_max_nodes", "zones_count", "max_pods"))
    excel.add_table(wb, "NodePoolSkuSummary", sku,
                    int_cols=("node_pools", "current_nodes", "effective_min_nodes",
                              "effective_max_nodes", "zones_count_max"))
    excel.add_table(wb, "AutoscalerConfig", autoscaler,
                    int_cols=("spot_pools", "autoscaling_pools", "autoscaled_spot_pools",
                              "autoscaled_on_demand_pools", "cluster_max_nodes",
                              "spot_max_nodes", "on_demand_max_nodes"))
    excel.add_table(wb, "SpotAssessment", assessment,
                    fail_cols=("severity", "result"), fail_values=("HIGH", "FAIL"),
                    warn_values=("WARN",), max_width=100)
    excel.add_table(wb, "CostByCluster", split.drop(columns=["cluster_id"]) if not split.empty else split,
                    money_cols=("OnDemand", "Spot", "Reservation", "SavingsPlan",
                                "Cluster fee", "Total (USD)"),
                    pct_cols=("Spot %",))
    excel.add_table(wb, "CostTrend", trend,
                    money_cols=tuple(PM_ORDER + ["Cluster fee", "Total (USD)"]),
                    pct_cols=("Spot %",))
    excel.add_table(wb, "CostByNodePool", pool_cost,
                    money_cols=("window_cost",),
                    int_cols=("nodes", "effective_max_nodes", "resource_count"))
    excel.add_table(wb, "OtherCostItems", other_cost,
                    money_cols=("window_cost",), max_width=90)
    excel.add_table(wb, "CostByMeter", meter_cost,
                    money_cols=("window_cost",), max_width=70)
    excel.add_table(wb, "RawResourceCost", raw_res,
                    money_cols=("Cost", "CostUSD"), max_width=90)

    path = excel.save(wb, out_path(args, "aks_spot_clusters", env_filter))
    log("Cost Management calls used: %d" % cost["calls"])
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
