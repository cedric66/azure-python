"""Utilization & idle report from Azure Monitor PLATFORM metrics (free, no
Container Insights / kubectl needed): node CPU and memory working set per
cluster, plus stopped-but-still-billing detection.

One metrics call per cluster - paced, so ~500 clusters take a few minutes.

Tabs: ReadMe, Utilization, IdleCandidates, Stopped, Summary.

Usage: python utilization_idle.py --nonprod --days 14
"""
import datetime as dt

import pandas as pd

from azrep import excel
from azrep.armextras import cluster_metrics
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

METRICS = ("node_cpu_usage_percentage", "node_memory_working_set_percentage",
           "kube_node_status_allocatable_cpu_cores")


def classify(power, cpu, mem):
    if power.lower() == "stopped":
        return "STOPPED"
    if cpu.get("avg") is None:
        return "NO DATA"
    if cpu["avg"] < 5 and (mem.get("avg") or 100) < 20:
        return "IDLE"
    if (cpu.get("p95") or 0) < 20 and (mem.get("p95") or 100) < 40:
        return "UNDERUTILIZED"
    return "OK"


def main(argv=None):
    p = base_parser("AKS utilization & idle clusters (platform metrics)")
    p.add_argument("--days", type=int, default=14, help="metrics lookback window")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.15)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    log("Querying platform metrics for %d clusters (%d days, paced)..."
        % (len(clusters), args.days))
    rows = []
    for i, c in enumerate(clusters, 1):
        if i % 25 == 0:
            log("  %d/%d..." % (i, len(clusters)))
        m = {} if c["power_state"].lower() == "stopped" else \
            cluster_metrics(session, c["id"], days=args.days, metrics=METRICS)
        cpu = m.get("node_cpu_usage_percentage", {})
        mem = m.get("node_memory_working_set_percentage", {})
        cores = m.get("kube_node_status_allocatable_cpu_cores", {})
        rows.append({
            "cluster": c["cluster"], "subscription": c["subscription"],
            "environment": c["environment"], "location": c["location"],
            "power_state": c["power_state"], "nodes": c["total_nodes"],
            "vm_sizes": c["vm_sizes"],
            "allocatable_cores_avg": round(cores["avg"], 1) if cores.get("avg") is not None else None,
            "cpu_avg %": round(cpu["avg"], 1) if cpu.get("avg") is not None else None,
            "cpu_p95 %": round(cpu["p95"], 1) if cpu.get("p95") is not None else None,
            "cpu_max %": round(cpu["max"], 1) if cpu.get("max") is not None else None,
            "mem_avg %": round(mem["avg"], 1) if mem.get("avg") is not None else None,
            "mem_p95 %": round(mem["p95"], 1) if mem.get("p95") is not None else None,
            "mem_max %": round(mem["max"], 1) if mem.get("max") is not None else None,
            "samples": cpu.get("points") or 0,
            "flag": classify(c["power_state"], cpu, mem),
        })
    u = pd.DataFrame(rows).sort_values(["flag", "cpu_avg %"], na_position="last")

    idle = u[u["flag"].isin(["IDLE", "UNDERUTILIZED"])].copy()
    stopped = u[u["flag"] == "STOPPED"].copy()
    summary = u.groupby("flag").agg(clusters=("cluster", "count"),
                                    nodes=("nodes", "sum")).reset_index()

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Utilization & Idle Clusters", [
        "Generated: %s   Scope: %s   Lookback: %d days" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all", args.days),
        "",
        "Source: Azure Monitor platform metrics for managed clusters (node CPU usage %,",
        "node memory working set %), aggregated hourly. avg = mean of hourly averages,",
        "p95 = 95th percentile of hourly averages, max = highest hourly maximum.",
        "",
        "Flags: IDLE (avg CPU <5%% and avg mem <20%%), UNDERUTILIZED (p95 CPU <20%% and",
        "p95 mem <40%%), STOPPED (deallocated - but disks, IPs and the cluster fee still",
        "bill), NO DATA (no metrics in window - very new, stopped mid-window, or empty).",
        "These are screening signals, not right-sizing advice: node metrics miss pod",
        "requests/limits, so a 'busy' cluster can still be over-requested. Validate the",
        "candidates with cluster_deepdive.py before acting.",
    ])
    excel.add_table(wb, "Utilization", u, fail_cols=("flag",),
                    colorscale_cols=("cpu_avg %", "mem_avg %"),
                    int_cols=("nodes", "samples"))
    excel.add_table(wb, "IdleCandidates", idle, fail_cols=("flag",),
                    int_cols=("nodes", "samples"))
    excel.add_table(wb, "Stopped", stopped, int_cols=("nodes",))
    excel.add_table(wb, "Summary", summary, int_cols=("clusters", "nodes"))

    path = excel.save(wb, out_path(args, "aks_utilization", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
