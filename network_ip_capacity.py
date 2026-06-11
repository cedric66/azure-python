"""AKS network and IP capacity report.

Uses subscription Reader-accessible Resource Graph data only: AKS cluster/pool
network configuration plus VNet subnet prefixes. No kubectl or node access.

Tabs: ReadMe, ClusterNetwork, SubnetCapacity, PoolSubnetUse, Issues, Summary.

Usage:
  python network_ip_capacity.py --all
  python network_ip_capacity.py --nonprod
  python network_ip_capacity.py --env dev
"""
import datetime as dt
import ipaddress

import pandas as pd

from azrep import arg, excel
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [x for x in v if x not in (None, "")]
    if isinstance(v, tuple):
        return [x for x in v if x not in (None, "")]
    if isinstance(v, str):
        s = v.strip()
        return [s] if s and s != "[]" else []
    return [v]


def _prefixes(row):
    prefixes = []
    prefixes.extend(str(x) for x in _as_list(row.get("addressPrefixes")))
    if row.get("addressPrefix"):
        prefixes.append(str(row["addressPrefix"]))
    # Keep order but remove duplicates/blanks.
    out, seen = [], set()
    for p in prefixes:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def usable_ipv4(prefixes):
    total, notes = 0, []
    for p in prefixes:
        try:
            net = ipaddress.ip_network(p, strict=False)
        except ValueError:
            notes.append("invalid prefix %s" % p)
            continue
        if net.version != 4:
            notes.append("IPv6 prefix not counted: %s" % p)
            continue
        total += max(int(net.num_addresses) - 5, 0)  # Azure reserves 5 IPv4s/subnet.
    return total or None, "; ".join(notes)


def _join_names(items, key):
    out = []
    for item in _as_list(items):
        if isinstance(item, dict):
            val = item.get(key) or item.get("name")
            if val:
                out.append(str(val))
        elif item:
            out.append(str(item))
    return ", ".join(sorted(set(out)))


def flatten_subnets(rows, sub_names):
    out = []
    for r in rows:
        prefixes = _prefixes(r)
        usable, note = usable_ipv4(prefixes)
        out.append({
            "subnet_id": (r.get("id") or "").lower(),
            "subnet_name": r.get("name") or "",
            "subscription_id": (r.get("subscriptionId") or "").lower(),
            "subscription": sub_names.get((r.get("subscriptionId") or "").lower())
            or (r.get("subscriptionId") or ""),
            "resource_group": r.get("resourceGroup") or "",
            "location": r.get("location") or "",
            "vnet": r.get("vnet") or "",
            "prefixes": ", ".join(prefixes),
            "usable_ipv4": usable,
            "nsg": r.get("nsgId") or "",
            "route_table": r.get("routeTableId") or "",
            "nat_gateway": r.get("natGatewayId") or "",
            "private_endpoint_policies": r.get("privateEndpointNetworkPolicies") or "",
            "service_endpoints": _join_names(r.get("serviceEndpoints"), "service"),
            "delegations": _join_names(r.get("delegations"), "serviceName"),
            "prefix_note": note,
        })
    return out


def pool_max_nodes(q):
    if q.get("autoscaling") and q.get("max_count") not in (None, ""):
        try:
            return int(q["max_count"])
        except (TypeError, ValueError):
            return int(q.get("count") or 0)
    return int(q.get("count") or 0)


def network_model(cluster):
    plugin = (cluster.get("network_plugin") or "").lower()
    mode = (cluster.get("network_plugin_mode") or "").lower()
    if plugin == "azure" and mode == "overlay":
        return "Azure CNI Overlay"
    if plugin == "azure":
        return "Azure CNI"
    if plugin == "kubenet":
        return "kubenet"
    return cluster.get("network_plugin") or "unknown"


def _pct(num, den):
    if not den:
        return None
    return float(num or 0) / float(den)


def capacity_status(max_pct, usable):
    if usable is None:
        return "UNKNOWN"
    if max_pct is None:
        return "UNKNOWN"
    if max_pct >= 0.90:
        return "CRITICAL"
    if max_pct >= 0.75:
        return "WARN"
    return "OK"


def main(argv=None):
    p = base_parser("AKS network and IP capacity")
    p.add_argument("--warn-pct", type=float, default=0.75,
                   help="subnet max-capacity warning threshold")
    p.add_argument("--critical-pct", type=float, default=0.90,
                   help="subnet max-capacity critical threshold")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    sub_ids = [s["subscription_id"] for s in sel]
    sub_names = {s["subscription_id"]: s["subscription_name"] or s["subscription_id"] for s in sel}
    log("Resource Graph: fetching VNet subnet metadata for %d subscription(s)..." % len(sel))
    subnet_rows = flatten_subnets(arg.query(session, arg.SUBNETS_KQL, sub_ids), sub_names)
    subnets = {s["subnet_id"]: s for s in subnet_rows if s["subnet_id"]}

    clusters_by_id = {c["id"]: c for c in clusters}
    pools_by_cluster = {}
    for q in pools:
        pools_by_cluster.setdefault(q["cluster_id"], []).append(q)

    cluster_rows = []
    for c in clusters:
        ps = pools_by_cluster.get(c["id"], [])
        max_nodes = sum(pool_max_nodes(q) for q in ps)
        cluster_rows.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "location": c["location"],
            "network_model": network_model(c),
            "network_plugin": c["network_plugin"],
            "network_plugin_mode": c["network_plugin_mode"],
            "network_policy": c["network_policy"],
            "network_dataplane": c["network_dataplane"],
            "outbound_type": c["outbound_type"],
            "lb_sku": c["lb_sku"],
            "private_cluster": c["private_cluster"],
            "authorized_ip_ranges": c["authorized_ip_ranges"],
            "service_cidrs": c.get("service_cidrs", ""),
            "pod_cidrs": c.get("pod_cidrs", ""),
            "dns_service_ip": c.get("dns_service_ip", ""),
            "ip_families": c.get("ip_families", ""),
            "current_nodes": c["total_nodes"],
            "max_nodes": max_nodes,
            "pools": c["node_pools"],
            "node_subnets": len({q["vnet_subnet_id"].lower() for q in ps
                                 if q.get("vnet_subnet_id")}),
            "pod_subnets": len({q["pod_subnet_id"].lower() for q in ps
                                if q.get("pod_subnet_id")}),
        })

    usage = {}
    pool_rows, issues = [], []

    def add_usage(subnet_id, role, cur, max_needed, q):
        if not subnet_id:
            return
        rec = usage.setdefault(subnet_id, {
            "subnet_id": subnet_id,
            "roles": set(),
            "clusters": set(),
            "pools": set(),
            "current_ips_needed": 0,
            "max_ips_needed": 0,
        })
        rec["roles"].add(role)
        rec["clusters"].add(q["cluster"])
        rec["pools"].add("%s/%s" % (q["cluster"], q["pool"]))
        rec["current_ips_needed"] += int(cur or 0)
        rec["max_ips_needed"] += int(max_needed or 0)

    for q in pools:
        c = clusters_by_id[q["cluster_id"]]
        model = network_model(c)
        current_nodes = int(q.get("count") or 0)
        max_nodes = pool_max_nodes(q)
        max_pods = int(q.get("max_pods") or 0)
        node_sid = (q.get("vnet_subnet_id") or "").lower()
        pod_sid = (q.get("pod_subnet_id") or "").lower()

        node_current = current_nodes if node_sid else 0
        node_max = max_nodes if node_sid else 0
        pod_current = pod_max = 0
        pod_capacity_sid = ""

        if model == "Azure CNI":
            pod_capacity_sid = pod_sid or node_sid
            pod_current = current_nodes * max_pods
            pod_max = max_nodes * max_pods
        elif model in ("Azure CNI Overlay", "kubenet"):
            pod_capacity_sid = ""

        add_usage(node_sid, "node", node_current, node_max, q)
        add_usage(pod_capacity_sid, "pod", pod_current, pod_max, q)

        warnings = []
        if model == "Azure CNI" and not node_sid:
            warnings.append("Azure CNI pool has no vnetSubnetID in ARG; capacity unknown")
        if model == "Azure CNI" and not max_pods:
            warnings.append("Azure CNI pool maxPods missing; pod IP demand unknown")
        if model == "kubenet":
            warnings.append("kubenet cluster; plan Azure CNI migration before retirement pressure")
        if not c["private_cluster"] and c["authorized_ip_ranges"] == 0:
            warnings.append("public API server has no authorized IP ranges")

        pool_rows.append({
            "cluster": q["cluster"],
            "subscription": q["subscription"],
            "environment": q["environment"],
            "location": q["location"],
            "pool": q["pool"],
            "mode": q["mode"],
            "priority": q["priority"],
            "network_model": model,
            "vm_size": q["vm_size"],
            "current_nodes": current_nodes,
            "max_nodes": max_nodes,
            "max_pods": max_pods,
            "node_subnet_id": node_sid,
            "pod_subnet_id": pod_sid,
            "node_ips_current": node_current,
            "node_ips_at_max": node_max,
            "pod_ips_current": pod_current,
            "pod_ips_at_max": pod_max,
            "warning": "; ".join(warnings),
        })
        for w in warnings:
            issues.append({
                "cluster": q["cluster"],
                "subscription": q["subscription"],
                "environment": q["environment"],
                "object": q["pool"],
                "severity": "WARN",
                "issue": w,
            })

    capacity_rows = []
    for subnet_id, u in usage.items():
        s = subnets.get(subnet_id, {})
        usable = s.get("usable_ipv4")
        cur_pct = _pct(u["current_ips_needed"], usable)
        max_pct = _pct(u["max_ips_needed"], usable)
        status = capacity_status(max_pct, usable)
        if usable is not None:
            if max_pct is not None and max_pct >= args.critical_pct:
                status = "CRITICAL"
            elif max_pct is not None and max_pct >= args.warn_pct:
                status = "WARN"
            else:
                status = "OK"
        capacity_rows.append({
            "subnet_id": subnet_id,
            "subscription": s.get("subscription", ""),
            "resource_group": s.get("resource_group", ""),
            "vnet": s.get("vnet", ""),
            "subnet_name": s.get("subnet_name", subnet_id.split("/")[-1]),
            "location": s.get("location", ""),
            "roles": ", ".join(sorted(u["roles"])),
            "prefixes": s.get("prefixes", ""),
            "usable_ipv4": usable,
            "current_ips_needed": u["current_ips_needed"],
            "max_ips_needed": u["max_ips_needed"],
            "current_utilization": cur_pct,
            "max_utilization": max_pct,
            "clusters": len(u["clusters"]),
            "pools": len(u["pools"]),
            "nsg_attached": bool(s.get("nsg")),
            "route_table_attached": bool(s.get("route_table")),
            "nat_gateway_attached": bool(s.get("nat_gateway")),
            "service_endpoints": s.get("service_endpoints", ""),
            "delegations": s.get("delegations", ""),
            "status": status,
            "note": s.get("prefix_note", "") or ("subnet not visible in Resource Graph"
                                                 if not s else ""),
        })
        if status != "OK":
            issues.append({
                "cluster": ", ".join(sorted(u["clusters"]))[:200],
                "subscription": s.get("subscription", ""),
                "environment": "",
                "object": s.get("subnet_name", subnet_id.split("/")[-1]),
                "severity": "CRITICAL" if status == "CRITICAL" else "WARN",
                "issue": "Subnet IP capacity %s: max demand %s of %s usable IPv4s"
                % (status, u["max_ips_needed"], usable or "unknown"),
            })

    cnet = pd.DataFrame(cluster_rows)
    cap = pd.DataFrame(capacity_rows).sort_values(
        ["status", "max_utilization"], ascending=[True, False]) if capacity_rows else pd.DataFrame(
        columns=["subnet_id", "subscription", "resource_group", "vnet", "subnet_name",
                 "location", "roles", "prefixes", "usable_ipv4", "current_ips_needed",
                 "max_ips_needed", "current_utilization", "max_utilization", "clusters",
                 "pools", "status", "note"])
    pooldf = pd.DataFrame(pool_rows)
    issuedf = pd.DataFrame(issues) if issues else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "object", "severity", "issue"])

    model_summary = cnet.groupby("network_model").agg(
        clusters=("cluster", "count"),
        nodes=("current_nodes", "sum"),
        max_nodes=("max_nodes", "sum")).reset_index()
    subnet_summary = cap.groupby("status").agg(
        subnets=("subnet_id", "count"),
        current_ips=("current_ips_needed", "sum"),
        max_ips=("max_ips_needed", "sum")).reset_index() if not cap.empty else pd.DataFrame(
        columns=["status", "subnets", "current_ips", "max_ips"])
    prod_public = int(sum(1 for c in cluster_rows
                          if is_prod(c["environment"]) and not c["private_cluster"]
                          and c["authorized_ip_ranges"] == 0))
    summary = pd.DataFrame([
        ("Clusters in scope", len(clusters)),
        ("Node pools in scope", len(pools)),
        ("Subnets referenced by AKS pools", len(cap)),
        ("Subnets WARN/CRITICAL", int(cap["status"].isin(["WARN", "CRITICAL"]).sum())
         if not cap.empty else 0),
        ("Azure CNI clusters", int((cnet["network_model"] == "Azure CNI").sum())),
        ("Azure CNI Overlay clusters", int((cnet["network_model"] == "Azure CNI Overlay").sum())),
        ("kubenet clusters", int((cnet["network_model"] == "kubenet").sum())),
        ("Prod public API with no IP allowlist", prod_public),
    ], columns=["Item", "Value"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Network and IP Capacity", [
        "Generated: %s   Scope: %s   Clusters: %d" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all", len(clusters)),
        "",
        "Source: Azure Resource Graph only. No kubectl, no node access.",
        "Azure CNI flat networking consumes pod IPs from the node subnet unless a pod",
        "subnet is configured. Azure CNI Overlay and kubenet consume only node IPs from",
        "the node subnet in this approximation. Usable IPv4 subtracts Azure's five",
        "reserved IPs per subnet. max utilization uses autoscaler maxCount when present.",
        "",
        "Treat this as a capacity screen: verify top WARN/CRITICAL subnets before making",
        "network changes, especially for custom CNI modes or dual-stack clusters.",
    ])
    excel.add_table(wb, "ClusterNetwork", cnet,
                    int_cols=("authorized_ip_ranges", "current_nodes", "max_nodes",
                              "pools", "node_subnets", "pod_subnets"))
    excel.add_table(wb, "SubnetCapacity", cap,
                    int_cols=("usable_ipv4", "current_ips_needed", "max_ips_needed",
                              "clusters", "pools"),
                    pct_cols=("current_utilization", "max_utilization"),
                    fail_cols=("status",), fail_values=("CRITICAL",),
                    warn_values=("WARN", "UNKNOWN"),
                    colorscale_cols=("max_utilization",), max_width=80)
    excel.add_table(wb, "PoolSubnetUse", pooldf,
                    int_cols=("current_nodes", "max_nodes", "max_pods",
                              "node_ips_current", "node_ips_at_max",
                              "pod_ips_current", "pod_ips_at_max"),
                    max_width=80)
    excel.add_table(wb, "Issues", issuedf, fail_cols=("severity",),
                    fail_values=("CRITICAL",), warn_values=("WARN",), max_width=100)
    excel.add_table(wb, "Summary", summary, max_width=60)
    excel.add_table(wb, "SummaryByModel", model_summary,
                    int_cols=("clusters", "nodes", "max_nodes"))
    excel.add_table(wb, "SummaryBySubnetStatus", subnet_summary,
                    int_cols=("subnets", "current_ips", "max_ips"))

    path = excel.save(wb, out_path(args, "aks_network_ip_capacity", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
