"""AKS cost optimization priority report.

Combines 3-month amortized cluster cost, pricing-model split, current node-pool
shape, and Azure Monitor platform metrics to rank review candidates.

Tabs: ReadMe, ExecutiveSummary, SavingsCandidates, ClusterCostUtilization,
PricingModelSplit, RawMonthly.

Usage:
  python optimization_report.py --all
  python optimization_report.py --nonprod
  python optimization_report.py --env dev --days 14
"""
import datetime as dt
from collections import defaultdict

import pandas as pd

from azrep import excel
from azrep.armextras import cluster_metrics
from azrep.costmgmt import CostClient, default_window, dim_in
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope

RG_CHUNK = 30


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def last_full_month(months):
    cur = dt.date.today().strftime("%Y-%m")
    full = [m for m in sorted(months) if m != cur]
    return full[-1] if full else (sorted(months)[-1] if months else None)


def prev_and_last_full(months):
    cur = dt.date.today().strftime("%Y-%m")
    full = [m for m in sorted(months) if m != cur]
    if len(full) >= 2:
        return full[-2], full[-1]
    return None, full[-1] if full else None


def pool_max_nodes(q):
    if q.get("autoscaling") and q.get("max_count") not in (None, ""):
        try:
            return int(q["max_count"])
        except (TypeError, ValueError):
            pass
    return int(q.get("count") or 0)


def utilization_flag(power, cpu, mem, args):
    if (power or "").lower() == "stopped":
        return "STOPPED"
    if cpu.get("avg") is None:
        return "NO DATA"
    if cpu["avg"] < args.idle_cpu and (mem.get("avg") or 100) < args.idle_mem:
        return "IDLE"
    if (cpu.get("p95") or 0) < args.underutil_cpu_p95 and \
            (mem.get("p95") or 100) < args.underutil_mem_p95:
        return "UNDERUTILIZED"
    return "OK"


def main(argv=None):
    p = base_parser("AKS cost optimization priority report")
    p.add_argument("--months", type=int, default=3, help="full months of cost history")
    p.add_argument("--days", type=int, default=14, help="utilization lookback days")
    p.add_argument("--no-metrics", action="store_true", help="skip Azure Monitor metrics")
    p.add_argument("--min-monthly-cost", type=float, default=100.0,
                   help="minimum average monthly cost for optimization flags")
    p.add_argument("--idle-cpu", type=float, default=5.0)
    p.add_argument("--idle-mem", type=float, default=20.0)
    p.add_argument("--underutil-cpu-p95", type=float, default=20.0)
    p.add_argument("--underutil-mem-p95", type=float, default=40.0)
    p.add_argument("--rightsizing-savings-pct", type=float, default=0.25,
                   help="screening estimate for underutilized high-cost clusters")
    p.add_argument("--spot-savings-pct", type=float, default=0.50,
                   help="screening estimate for non-prod on-demand user-pool spend")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.15)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    by_sub = defaultdict(list)
    for c in clusters:
        if c["node_resource_group"]:
            by_sub[c["subscription_id"]].append(c)
    rg_map = {(c["subscription_id"], c["node_resource_group"].lower()): c for c in clusters}
    id_map = {c["id"].lower(): c for c in clusters}

    d_from, d_to = default_window(args.months)
    cost = CostClient(session)
    pm_rows, fee_rows = [], []
    log("Cost window %s to %s; querying amortized cost by node RG and pricing model..."
        % (d_from, d_to))
    for i, (sid, cls) in enumerate(sorted(by_sub.items()), 1):
        rgs = sorted({c["node_resource_group"].lower() for c in cls})
        scope = "/subscriptions/%s" % sid
        log("[%d/%d] %s: %d clusters, %d node RGs"
            % (i, len(by_sub), cls[0]["subscription"], len(cls), len(rgs)))
        for ch in chunks(rgs, RG_CHUNK):
            f = dim_in("ResourceGroupName", ch)
            df = cost.query(scope, "AmortizedCost", "Monthly",
                            ("ResourceGroupName", "PricingModel"), f, d_from, d_to)
            if not df.empty:
                df["subscription_id"] = sid
                pm_rows.append(df)
        df_fee = cost.query(scope, "AmortizedCost", "Monthly", ("ResourceId",),
                            dim_in("ResourceType",
                                   ["microsoft.containerservice/managedclusters"]),
                            d_from, d_to)
        if not df_fee.empty:
            df_fee["subscription_id"] = sid
            fee_rows.append(df_fee)

    if pm_rows:
        pm = pd.concat(pm_rows, ignore_index=True)
        key = list(zip(pm["subscription_id"], pm["ResourceGroupName"].str.lower()))
        pm["cluster"] = [rg_map.get(k, {}).get("cluster", "(unmatched)") for k in key]
        pm["cluster_id"] = [rg_map.get(k, {}).get("id", "") for k in key]
        pm["subscription"] = [rg_map.get(k, {}).get("subscription", "") for k in key]
        pm["environment"] = [rg_map.get(k, {}).get("environment", "") for k in key]
        pm["Month"] = pm["Period"].str[:7]
    else:
        pm = pd.DataFrame(columns=["cluster", "cluster_id", "subscription", "environment",
                                   "Month", "PricingModel", "CostUSD"])

    fees = pd.concat(fee_rows, ignore_index=True) if fee_rows else pd.DataFrame()
    if not fees.empty:
        fees["cluster_id"] = fees["ResourceId"].str.lower().map(
            lambda x: id_map.get(x, {}).get("id"))
        fees["cluster"] = fees["ResourceId"].str.lower().map(
            lambda x: id_map.get(x, {}).get("cluster"))
        fees = fees.dropna(subset=["cluster_id"])
        fees["Month"] = fees["Period"].str[:7]

    if pm.empty:
        log("No cost rows returned - check Cost Management Reader access.")
        return

    pools_by_cluster = defaultdict(list)
    for q in pools:
        pools_by_cluster[q["cluster"]].append(q)

    months = sorted(set(pm["Month"]) | (set(fees["Month"]) if not fees.empty else set()))
    last_m = last_full_month(months)
    prev_m, last_full = prev_and_last_full(months)
    current_m = dt.date.today().strftime("%Y-%m")
    full_months = [m for m in months if m != current_m] or months

    node_month = pm.groupby(["cluster_id", "Month"])["CostUSD"].sum()
    fee_month = fees.groupby(["cluster_id", "Month"])["CostUSD"].sum() if not fees.empty else pd.Series(dtype=float)
    split = pm.pivot_table(index=["cluster_id", "cluster", "subscription", "environment"],
                           columns="PricingModel", values="CostUSD", aggfunc="sum") \
        .fillna(0.0).reset_index()

    metric_rows = {}
    if not args.no_metrics:
        log("Querying utilization metrics for %d clusters (%d days)..."
            % (len(clusters), args.days))
        for i, c in enumerate(clusters, 1):
            if i % 25 == 0:
                log("  %d/%d..." % (i, len(clusters)))
            m = {} if c["power_state"].lower() == "stopped" else \
                cluster_metrics(session, c["id"], days=args.days)
            cpu = m.get("node_cpu_usage_percentage", {})
            mem = m.get("node_memory_working_set_percentage", {})
            metric_rows[c["id"]] = {
                "cpu_avg %": round(cpu["avg"], 1) if cpu.get("avg") is not None else None,
                "cpu_p95 %": round(cpu["p95"], 1) if cpu.get("p95") is not None else None,
                "mem_avg %": round(mem["avg"], 1) if mem.get("avg") is not None else None,
                "mem_p95 %": round(mem["p95"], 1) if mem.get("p95") is not None else None,
                "samples": cpu.get("points") or 0,
                "utilization_flag": utilization_flag(c["power_state"], cpu, mem, args),
            }

    cluster_rows, candidate_rows = [], []
    split_by_cluster = split.set_index("cluster_id") if not split.empty else pd.DataFrame()
    for c in clusters:
        ps = pools_by_cluster.get(c["cluster"], [])
        month_values = {m: float(node_month.get((c["id"], m), 0.0))
                        + float(fee_month.get((c["id"], m), 0.0))
                        for m in months}
        window_total = sum(month_values.values())
        avg_full = sum(month_values[m] for m in full_months) / len(full_months) if full_months else 0.0
        last_cost = month_values.get(last_m, 0.0) if last_m else 0.0
        mom_pct = None
        if prev_m and last_full and month_values.get(prev_m, 0) > 0:
            mom_pct = (month_values[last_full] - month_values[prev_m]) / month_values[prev_m]

        split_row = split_by_cluster.loc[c["id"]] if c["id"] in split_by_cluster.index else {}
        spot_usd = float(split_row.get("Spot", 0.0)) if hasattr(split_row, "get") else 0.0
        ondemand_usd = float(split_row.get("OnDemand", 0.0)) if hasattr(split_row, "get") else 0.0
        ri_sp_usd = sum(float(split_row.get(k, 0.0)) for k in
                        ("Reservation", "Reservations", "SavingsPlan", "Savings Plan")) \
            if hasattr(split_row, "get") else 0.0
        pricing_total = spot_usd + ondemand_usd + ri_sp_usd
        spot_pct = spot_usd / pricing_total if pricing_total else 0.0
        ri_sp_pct = ri_sp_usd / pricing_total if pricing_total else 0.0
        regular_user_nodes = sum(int(q.get("count") or 0) for q in ps
                                 if q["mode"].lower() == "user"
                                 and q["priority"].lower() != "spot"
                                 and q["os_type"].lower() != "windows")
        max_nodes = sum(pool_max_nodes(q) for q in ps)
        metrics = metric_rows.get(c["id"], {
            "cpu_avg %": None, "cpu_p95 %": None, "mem_avg %": None,
            "mem_p95 %": None, "samples": 0,
            "utilization_flag": "SKIPPED" if args.no_metrics else "NO DATA",
        })

        flags = []
        if c["power_state"].lower() == "stopped" and avg_full >= args.min_monthly_cost:
            flags.append("STOPPED_BILLING")
            candidate_rows.append({
                "cluster": c["cluster"], "subscription": c["subscription"],
                "environment": c["environment"], "candidate": "STOPPED_BILLING",
                "priority": "HIGH",
                "avg_monthly_cost": avg_full,
                "estimated_monthly_saving": avg_full,
                "reason": "Cluster is stopped but recent amortized cost still exists; review disks/IPs/fee.",
            })
        if metrics["utilization_flag"] in ("IDLE", "UNDERUTILIZED") and avg_full >= args.min_monthly_cost:
            flags.append(metrics["utilization_flag"])
            candidate_rows.append({
                "cluster": c["cluster"], "subscription": c["subscription"],
                "environment": c["environment"], "candidate": "RIGHTSIZE_OR_SCALE_DOWN",
                "priority": "HIGH" if metrics["utilization_flag"] == "IDLE" else "MEDIUM",
                "avg_monthly_cost": avg_full,
                "estimated_monthly_saving": avg_full * args.rightsizing_savings_pct,
                "reason": "%s: cpu avg/p95=%s/%s, mem avg/p95=%s/%s"
                % (metrics["utilization_flag"], metrics["cpu_avg %"], metrics["cpu_p95 %"],
                   metrics["mem_avg %"], metrics["mem_p95 %"]),
            })
        if not is_prod(c["environment"]) and regular_user_nodes > 0 and spot_pct < 0.05 \
                and ondemand_usd >= args.min_monthly_cost:
            flags.append("SPOT_REVIEW")
            candidate_rows.append({
                "cluster": c["cluster"], "subscription": c["subscription"],
                "environment": c["environment"], "candidate": "SPOT_REVIEW",
                "priority": "MEDIUM",
                "avg_monthly_cost": avg_full,
                "estimated_monthly_saving": (ondemand_usd / max(len(full_months), 1))
                * args.spot_savings_pct,
                "reason": "%d non-prod regular Linux user nodes and low spot share." %
                regular_user_nodes,
            })
        if is_prod(c["environment"]) and ri_sp_pct < 0.05 and ondemand_usd >= args.min_monthly_cost * 3:
            flags.append("COMMITMENT_REVIEW")
            candidate_rows.append({
                "cluster": c["cluster"], "subscription": c["subscription"],
                "environment": c["environment"], "candidate": "RI_SP_COMMITMENT_REVIEW",
                "priority": "MEDIUM",
                "avg_monthly_cost": avg_full,
                "estimated_monthly_saving": None,
                "reason": "High on-demand prod spend with low RI/SP amortized allocation.",
            })
        if mom_pct is not None and mom_pct > 0.50 and last_cost >= args.min_monthly_cost:
            flags.append("COST_SPIKE")
            candidate_rows.append({
                "cluster": c["cluster"], "subscription": c["subscription"],
                "environment": c["environment"], "candidate": "COST_SPIKE",
                "priority": "MEDIUM",
                "avg_monthly_cost": avg_full,
                "estimated_monthly_saving": None,
                "reason": "Last full month increased %.0f%% vs previous full month." % (mom_pct * 100),
            })

        cluster_rows.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "location": c["location"],
            "power_state": c["power_state"],
            "nodes": c["total_nodes"],
            "max_nodes": max_nodes,
            "spot_nodes": c["spot_nodes"],
            "regular_user_nodes": regular_user_nodes,
            "avg_monthly_cost": avg_full,
            "last_full_month_cost": last_cost,
            "window_total": window_total,
            "MoM %": mom_pct,
            "Spot %": spot_pct,
            "RI+SP %": ri_sp_pct,
            "utilization_flag": metrics["utilization_flag"],
            "cpu_avg %": metrics["cpu_avg %"],
            "cpu_p95 %": metrics["cpu_p95 %"],
            "mem_avg %": metrics["mem_avg %"],
            "mem_p95 %": metrics["mem_p95 %"],
            "samples": metrics["samples"],
            "optimization_flags": ", ".join(flags),
        })

    ccu = pd.DataFrame(cluster_rows).sort_values(
        ["avg_monthly_cost"], ascending=False)
    cand = pd.DataFrame(candidate_rows) if candidate_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "candidate", "priority",
                 "avg_monthly_cost", "estimated_monthly_saving", "reason"])
    if not cand.empty:
        cand = cand.sort_values(["priority", "estimated_monthly_saving"],
                                ascending=[True, False], na_position="last")

    raw = pm.groupby(["cluster_id", "cluster", "subscription", "environment", "Month",
                      "PricingModel"])["CostUSD"].sum().reset_index() \
        .rename(columns={"CostUSD": "Amortized node RG cost"})
    if not fees.empty:
        fee_raw = fees.groupby(["cluster_id", "Month"])["CostUSD"].sum().reset_index() \
            .rename(columns={"CostUSD": "Cluster fee"})
        raw = raw.merge(fee_raw, on=["cluster_id", "Month"], how="left")
        raw["Cluster fee"] = raw["Cluster fee"].fillna(0.0)
    else:
        raw["Cluster fee"] = 0.0

    pm_cols = [c for c in ("OnDemand", "Spot", "Reservation", "SavingsPlan")
               if c in split.columns]
    other_pm = [c for c in split.columns
                if c not in ["cluster_id", "cluster", "subscription", "environment"] + pm_cols]
    split = split[["cluster", "subscription", "environment", "cluster_id"] + pm_cols + other_pm]
    if not split.empty:
        pm_start = excel.get_column_letter(5)
        pm_end = excel.get_column_letter(split.shape[1])
        split["Total"] = ["=SUM(%s%d:%s%d)" % (pm_start, r, pm_end, r)
                          for r in range(2, len(split) + 2)]

    exec_summary = pd.DataFrame([
        ("Clusters in scope", len(clusters)),
        ("Cost window", "%s to %s" % (d_from, d_to)),
        ("Cost Management calls", cost.calls),
        ("Window amortized total", float(ccu["window_total"].sum())),
        ("Average full-month run rate", float(ccu["avg_monthly_cost"].sum())),
        ("Optimization findings", len(cand)),
        ("High priority findings", int((cand["priority"] == "HIGH").sum()) if not cand.empty else 0),
        ("Estimated monthly saving total", float(cand["estimated_monthly_saving"].fillna(0).sum())
         if not cand.empty else 0.0),
        ("Stopped clusters with cost", int((cand["candidate"] == "STOPPED_BILLING").sum())
         if not cand.empty else 0),
        ("Rightsize/scale-down candidates", int((cand["candidate"] == "RIGHTSIZE_OR_SCALE_DOWN").sum())
         if not cand.empty else 0),
        ("Spot review candidates", int((cand["candidate"] == "SPOT_REVIEW").sum())
         if not cand.empty else 0),
        ("RI/SP commitment review candidates", int((cand["candidate"] == "RI_SP_COMMITMENT_REVIEW").sum())
         if not cand.empty else 0),
    ], columns=["Item", "Value"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Cost Optimization Priority Report", [
        "Generated: %s   Scope: %s   Cost window: %s to %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all", d_from, d_to),
        "",
        "This report combines amortized Cost Management data with AKS platform metrics",
        "to rank review candidates. It is a screening report, not an automatic action",
        "plan: pod requests/limits and workload criticality are not visible without",
        "kubectl/Container Insights data.",
        "",
        "Estimated savings are intentionally simple: stopped-billing uses recent monthly",
        "run-rate; rightsizing uses --rightsizing-savings-pct; spot review uses",
        "--spot-savings-pct against on-demand spend. Validate top rows with",
        "cluster_deepdive.py before changing node pools.",
    ])
    excel.add_table(wb, "ExecutiveSummary", exec_summary,
                    max_width=90)
    ws_cand = excel.add_table(wb, "SavingsCandidates", cand,
                              money_cols=("avg_monthly_cost", "estimated_monthly_saving"),
                              fail_cols=("priority",), fail_values=("HIGH",),
                              warn_values=("MEDIUM",), max_width=100)
    if not cand.empty:
        saving_col = list(cand.columns).index("estimated_monthly_saving") + 1
        excel.add_bar_chart(ws_cand, "Estimated monthly saving by candidate",
                            len(cand) + 1, saving_col, "B%d" % (len(cand) + 4),
                            y_title="USD")
    ws_ccu = excel.add_table(wb, "ClusterCostUtilization", ccu,
                             money_cols=("avg_monthly_cost", "last_full_month_cost", "window_total"),
                             pct_cols=("MoM %", "Spot %", "RI+SP %"),
                             int_cols=("nodes", "max_nodes", "spot_nodes", "regular_user_nodes", "samples"),
                             fail_cols=("utilization_flag",),
                             fail_values=("STOPPED", "IDLE"),
                             warn_values=("UNDERUTILIZED", "NO DATA", "SKIPPED"),
                             colorscale_cols=("avg_monthly_cost", "MoM %"), max_width=80)
    avg_col = list(ccu.columns).index("avg_monthly_cost") + 1
    excel.add_bar_chart(ws_ccu, "Average monthly cost by cluster",
                        min(len(ccu), 25) + 1, avg_col, "B%d" % (len(ccu) + 5),
                        y_title="USD")
    excel.add_table(wb, "PricingModelSplit", split,
                    money_cols=tuple([c for c in split.columns
                                      if c not in ("cluster", "subscription",
                                                   "environment", "cluster_id")]))
    excel.add_table(wb, "RawMonthly", raw,
                    money_cols=("Amortized node RG cost", "Cluster fee"))

    path = excel.save(wb, out_path(args, "aks_optimization", env_filter))
    log("Cost Management calls used: %d" % cost.calls)
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
