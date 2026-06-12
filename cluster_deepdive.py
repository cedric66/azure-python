"""Deep-dive cost & config analysis of ONE AKS cluster.

What you get (multi-tab xlsx):
  Summary           key facts, 3-month amortized/actual totals, spot share, flags
  DailyCost         daily amortized cost split by pricing model (spot/on-demand/RI/SP) + chart
  CostByMeter       meter x month matrix - this is where SKU changes show up
  CostByNodePool    cost per node pool (VMSS) per month
  AmortizedVsActual monthly comparison - the delta is reservation/savings-plan reallocation
  SKUChanges        detected SKU/meter/pool appearances, removals and big swings
  NodePools         current node pool configuration
  Utilization       daily node CPU/memory from platform metrics + chart
  ActivityLog       control-plane writes on the cluster (90-day retention)

Cost source: Cost Management Query API scoped to the cluster's node resource group
(MC_*) where the compute/disk/IP charges live, plus the managed-cluster resource
itself (uptime SLA fee). AmortizedCost = reservation & savings-plan purchases spread
across the resources that consumed them - the "true" cost of a cluster.

Usage:
  python cluster_deepdive.py                          # interactive picker
  python cluster_deepdive.py --cluster my-aks-dev-01
  python cluster_deepdive.py --cluster-id /subscriptions/.../managedClusters/x
"""
import datetime as dt
import re
import sys

import pandas as pd

import azrep.subs as subs_runtime
from azrep import excel
from azrep.armextras import activity_events, cluster_metrics
from azrep.costmgmt import CostClient, default_window, dim_in
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

VMSS_RE = re.compile(r"/virtualmachinescalesets/aks-(.+?)-\d+-vmss$", re.I)


def find_cluster(clusters, args):
    if len(clusters) == 1 and not args.cluster and not args.cluster_id:
        return clusters[0]
    if args.cluster_id:
        m = [c for c in clusters if c["id"].lower() == args.cluster_id.lower()]
        if not m:
            sys.exit("Cluster id not found in the selected scope: %s" % args.cluster_id)
        return m[0]
    if args.cluster:
        m = [c for c in clusters if c["cluster"].lower() == args.cluster.lower()]
        if not m:
            sys.exit("Cluster '%s' not found in the selected scope" % args.cluster)
        if len(m) > 1:
            print("Multiple clusters named '%s':" % args.cluster)
            for i, c in enumerate(m, 1):
                print("  %d) %s  (%s)" % (i, c["id"], c["subscription"]))
            return m[int(input("Which one [number]: ")) - 1]
        return m[0]
    print("\nClusters in scope:")
    for i, c in enumerate(clusters, 1):
        print("  %3d) %-40s %-28s env=%-10s %s" % (i, c["cluster"], c["subscription"][:28],
                                                   c["environment"], c["location"]))
    return clusters[int(input("Analyze which cluster [number]: ")) - 1]


def pool_from_resource_id(rid):
    m = VMSS_RE.search(rid or "")
    if m:
        return m.group(1)
    parts = (rid or "").split("/")
    return parts[-1] if parts else "(unknown)"


def month_pivot(df, key_col):
    """meter/pool x month USD matrix from a daily or monthly cost frame."""
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["Month"] = d["Period"].str[:7]
    p = d.pivot_table(index=key_col, columns="Month", values="CostUSD", aggfunc="sum").fillna(0.0)
    return p.sort_values(by=list(p.columns)[-1] if len(p.columns) else key_col, ascending=False)


def detect_sku_changes(meter_piv, pool_piv, events, threshold_usd=1.0):
    rows = []
    cur = dt.date.today().strftime("%Y-%m")
    for piv, kind in ((meter_piv, "Meter/SKU"), (pool_piv, "Node pool (VMSS)")):
        if piv.empty or len(piv.columns) < 2:
            continue
        months = sorted(piv.columns)
        full = [m for m in months if m != cur]
        for name, r in piv.iterrows():
            active = [m for m in months if r[m] > threshold_usd]
            if not active:
                continue
            active_full = [m for m in active if m != cur]
            status, note = None, ""
            if active[0] != months[0]:
                status, note = "NEW", "first significant cost in %s" % active[0]
            elif full and active[-1] < full[-1]:
                status, note = "REMOVED", "no significant cost after %s" % active[-1]
            elif len(active_full) >= 2:
                # compare full months only; the partial MTD month would distort the trend
                base, latest = r[active_full[0]], r[active_full[-1]]
                if base > threshold_usd:
                    chg = (latest - base) / base
                    if chg > 0.5:
                        status, note = "GROWN", "+%.0f%% (%s vs %s, full months)" % (
                            chg * 100, active_full[-1], active_full[0])
                    elif chg < -0.5:
                        status, note = "SHRUNK", "%.0f%% (%s vs %s, full months)" % (
                            chg * 100, active_full[-1], active_full[0])
            if status:
                rows.append({"kind": kind, "name": str(name), "status": status,
                             "first_month_usd": round(float(r[active[0]]), 2),
                             "last_month_usd": round(float(r[active[-1]]), 2),
                             "note": note})
    for e in events:
        op = e["operation"].lower()
        if "agentpools" in op or ("managedclusters" in op and op.endswith("/write")):
            rows.append({"kind": "Activity log", "name": e["resource"],
                         "status": "CONFIG WRITE", "first_month_usd": None,
                         "last_month_usd": None,
                         "note": "%s by %s (%s)" % (e["timestamp"], e["caller"] or "unknown",
                                                    e["status"])})
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["kind", "name", "status", "first_month_usd", "last_month_usd", "note"])


def main(argv=None):
    p = base_parser("Deep-dive analysis of one AKS cluster")
    p.add_argument("--months", type=int, default=3, help="full months of cost history")
    p.add_argument("--metric-days", type=int, default=30, help="utilization lookback days")
    p.add_argument("--no-activity", action="store_true", help="skip activity log")
    p.add_argument("--no-metrics", action="store_true", help="skip utilization metrics")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    # Deep dive writes one workbook for one cluster; use its picker as step 3.
    subs_runtime.PROMPT_CLUSTER_FILTER = False
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        sys.exit("No clusters in scope.")
    cl = find_cluster(clusters, args)
    log("Analyzing %s (%s / %s)" % (cl["cluster"], cl["subscription"], cl["location"]))

    d_from, d_to = default_window(args.months)
    cost = CostClient(session)
    sub_scope = "/subscriptions/%s" % cl["subscription_id"]
    node_rg_scope = "%s/resourceGroups/%s" % (sub_scope, cl["node_resource_group"])

    log("Cost 1/4: daily amortized by meter & pricing model (node resource group)...")
    by_meter = cost.query(node_rg_scope, "AmortizedCost", "Daily",
                          ("Meter", "PricingModel"), date_from=d_from, date_to=d_to)
    log("Cost 2/4: daily amortized by resource (node pool mapping)...")
    by_res = cost.query(node_rg_scope, "AmortizedCost", "Daily", ("ResourceId",),
                        date_from=d_from, date_to=d_to)
    log("Cost 3/4: managed cluster resource fee (uptime SLA etc.)...")
    fee = cost.query(sub_scope, "AmortizedCost", "Monthly", ("Meter",),
                     filt=dim_in("ResourceId", [cl["id"].lower()]),
                     date_from=d_from, date_to=d_to)
    log("Cost 4/4: monthly ACTUAL cost for amortized-vs-actual comparison...")
    actual = cost.query(node_rg_scope, "ActualCost", "Monthly", ("Meter",),
                        date_from=d_from, date_to=d_to)

    events = []
    if not args.no_activity:
        log("Activity log: control-plane writes (90 days)...")
        raw_events = activity_events(session, cl["subscription_id"], cl["resource_group"])
        raw_events += activity_events(session, cl["subscription_id"], cl["node_resource_group"])
        seen = set()
        for e in raw_events:
            k = (e["timestamp"], e["operation"], e["resource_id"])
            if k not in seen:
                seen.add(k)
                events.append(e)

    metrics = {}
    if not args.no_metrics and cl["power_state"].lower() != "stopped":
        log("Platform metrics: node CPU / memory (%d days)..." % args.metric_days)
        metrics = cluster_metrics(session, cl["id"], days=args.metric_days, want_series=True)

    # ---- derive frames ----
    daily = by_meter.pivot_table(index="Period", columns="PricingModel",
                                 values="CostUSD", aggfunc="sum").fillna(0.0).sort_index() \
        if not by_meter.empty else pd.DataFrame()
    meter_piv = month_pivot(by_meter, "Meter")
    by_res["pool"] = by_res["ResourceId"].map(pool_from_resource_id) if not by_res.empty else None
    pool_piv = month_pivot(by_res, "pool") if not by_res.empty else pd.DataFrame()
    sku_changes = detect_sku_changes(meter_piv, pool_piv, events)

    am_month = by_meter.copy()
    if not am_month.empty:
        am_month["Month"] = am_month["Period"].str[:7]
        am_month = am_month.groupby("Month")["CostUSD"].sum()
    ac_month = actual.copy()
    if not ac_month.empty:
        ac_month["Month"] = ac_month["Period"].str[:7]
        ac_month = ac_month.groupby("Month")["CostUSD"].sum()
    ava = pd.DataFrame({"Month": sorted(set(am_month.index if len(am_month) else [])
                                        | set(ac_month.index if len(ac_month) else []))})
    ava["Amortized (USD)"] = ava["Month"].map(am_month).fillna(0.0) if len(am_month) else 0.0
    ava["Actual (USD)"] = ava["Month"].map(ac_month).fillna(0.0) if len(ac_month) else 0.0
    ava["Delta (USD)"] = ["=B%d-C%d" % (i, i) for i in range(2, len(ava) + 2)]

    total_am = float(by_meter["CostUSD"].sum()) if not by_meter.empty else 0.0
    total_ac = float(actual["CostUSD"].sum()) if not actual.empty else 0.0
    total_fee = float(fee["CostUSD"].sum()) if not fee.empty else 0.0
    spot_usd = float(by_meter.loc[by_meter["PricingModel"].str.lower() == "spot", "CostUSD"].sum()) \
        if not by_meter.empty and "PricingModel" in by_meter else 0.0
    ri_usd = float(by_meter.loc[by_meter["PricingModel"].str.lower().isin(
        ["reservation", "reservations", "savingsplan", "savings plan"]), "CostUSD"].sum()) \
        if not by_meter.empty and "PricingModel" in by_meter else 0.0
    months_idx = sorted(am_month.index) if len(am_month) else []
    mom = ""
    if len(months_idx) >= 3:
        prev, last = am_month[months_idx[-3]], am_month[months_idx[-2]]
        if prev > 0:
            mom = "%+.1f%% (%s vs %s, full months)" % ((last - prev) / prev * 100,
                                                       months_idx[-2], months_idx[-3])
    cpu = metrics.get("node_cpu_usage_percentage", {})
    mem = metrics.get("node_memory_working_set_percentage", {})

    summary = pd.DataFrame([
        ("Cluster", cl["cluster"]),
        ("Resource id", cl["id"]),
        ("Subscription", "%s (%s)" % (cl["subscription"], cl["subscription_id"])),
        ("Environment", cl["environment"]),
        ("Region", cl["location"]),
        ("Power state", cl["power_state"]),
        ("Kubernetes version", cl["kubernetes_version"] or cl["current_kubernetes_version"]),
        ("SKU tier", cl["sku_tier"]),
        ("Node pools / nodes", "%d pools / %d nodes (%d spot nodes)"
         % (cl["node_pools"], cl["total_nodes"], cl["spot_nodes"])),
        ("VM sizes", cl["vm_sizes"]),
        ("", ""),
        ("Cost window", "%s to %s (amortized = true cost incl. RI/SP allocation)" % (d_from, d_to)),
        ("Amortized total (USD)", round(total_am + total_fee, 2)),
        ("  of which node resource group", round(total_am, 2)),
        ("  of which cluster fee (uptime SLA)", round(total_fee, 2)),
        ("Actual/billed total, node RG (USD)", round(total_ac, 2)),
        ("Amortized - actual delta (USD)", round(total_am - total_ac, 2)),
        ("Spot cost (USD / % of node RG)", "%.2f / %.1f%%" % (spot_usd, spot_usd / total_am * 100 if total_am else 0)),
        ("RI + Savings Plan covered (USD / %)", "%.2f / %.1f%%" % (ri_usd, ri_usd / total_am * 100 if total_am else 0)),
        ("Month-over-month", mom or "n/a (need 2 full months)"),
        ("SKU/meter change signals", len(sku_changes)),
        ("", ""),
        ("Node CPU %% (avg / p95 / max, %dd)" % args.metric_days,
         "%.1f / %.1f / %.1f" % (cpu.get("avg") or 0, cpu.get("p95") or 0, cpu.get("max") or 0)
         if cpu.get("avg") is not None else "n/a"),
        ("Node memory %% (avg / p95 / max)",
         "%.1f / %.1f / %.1f" % (mem.get("avg") or 0, mem.get("p95") or 0, mem.get("max") or 0)
         if mem.get("avg") is not None else "n/a"),
    ], columns=["Item", "Value"])

    # ---- workbook ----
    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Cluster Deep-Dive: %s" % cl["cluster"], [
        "Generated: %s   Window: %s to %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), d_from, d_to),
        "Amortized cost spreads reservation/savings-plan purchases across the resources that",
        "consumed them - use it as the true cost of the cluster. Actual cost is as billed.",
        "Cluster costs = everything in the node resource group (%s)" % cl["node_resource_group"],
        "plus the managed cluster resource fee. Current month is partial (MTD).",
        "SKU changes are inferred from meters/VMSS appearing, disappearing or swinging >50%,",
        "cross-checked against Activity Log writes (90-day retention).",
    ])
    excel.add_table(wb, "Summary", summary, max_width=110, section="summary")

    if not daily.empty:
        ddf = daily.reset_index()
        ncols = len(ddf.columns)
        ddf["Total (USD)"] = ["=SUM(B%d:%s%d)" % (i, excel.get_column_letter(ncols), i)
                              for i in range(2, len(ddf) + 2)]
        ws = excel.add_table(wb, "DailyCost", ddf,
                             money_cols=tuple(c for c in ddf.columns if c != "Period"))
        excel.add_line_chart(ws, "Daily amortized cost by pricing model", len(ddf) + 1,
                             2, ncols, "B%d" % (len(ddf) + 4))
    if not meter_piv.empty:
        mdf = meter_piv.reset_index()
        mcols = list(mdf.columns)
        last_l = excel.get_column_letter(len(mcols))
        mdf["Total (USD)"] = ["=SUM(B%d:%s%d)" % (i, last_l, i) for i in range(2, len(mdf) + 2)]
        excel.add_table(wb, "CostByMeter", mdf,
                        money_cols=tuple(c for c in mdf.columns if c != "Meter"))
    if not pool_piv.empty:
        pdf2 = pool_piv.reset_index()
        pcols = list(pdf2.columns)
        last_l = excel.get_column_letter(len(pcols))
        pdf2["Total (USD)"] = ["=SUM(B%d:%s%d)" % (i, last_l, i) for i in range(2, len(pdf2) + 2)]
        excel.add_table(wb, "CostByNodePool", pdf2,
                        money_cols=tuple(c for c in pdf2.columns if c != "pool"))
    excel.add_table(wb, "AmortizedVsActual", ava,
                    money_cols=("Amortized (USD)", "Actual (USD)", "Delta (USD)"))
    excel.add_table(wb, "SKUChanges", sku_changes, fail_cols=("status",),
                    fail_values=("REMOVED",), warn_values=("NEW", "GROWN", "SHRUNK"),
                    money_cols=("first_month_usd", "last_month_usd"), max_width=80)
    excel.add_table(wb, "NodePools",
                    pd.DataFrame([q for q in pools if q["cluster"] == cl["cluster"]]),
                    int_cols=("count", "min_count", "max_count", "os_disk_gb", "max_pods"))

    if metrics and cpu.get("series"):
        u = pd.DataFrame(cpu["series"], columns=["ts", "cpu_avg", "cpu_max"])
        u["ts"] = pd.to_datetime(u["ts"])
        if mem.get("series"):
            m2 = pd.DataFrame(mem["series"], columns=["ts", "mem_avg", "mem_max"])
            m2["ts"] = pd.to_datetime(m2["ts"])
            u = u.merge(m2, on="ts", how="outer")
        u = u.set_index("ts").resample("D").mean().reset_index()
        u["ts"] = u["ts"].dt.strftime("%Y-%m-%d")
        u = u.rename(columns={"ts": "Date", "cpu_avg": "CPU avg %", "cpu_max": "CPU max %",
                              "mem_avg": "Mem avg %", "mem_max": "Mem max %"})
        ws = excel.add_table(wb, "Utilization", u,
                             formats={c: "0.0" for c in u.columns if c != "Date"})
        excel.add_line_chart(ws, "Daily node CPU/memory %% (platform metrics)",
                             len(u) + 1, 2, len(u.columns), "B%d" % (len(u) + 4), y_title="%")

    excel.add_table(wb, "ActivityLog",
                    pd.DataFrame(events) if events else pd.DataFrame(
                        columns=["timestamp", "operation", "status", "caller", "resource"]),
                    max_width=70)

    path = excel.save(wb, out_path(args, "aks_deepdive_%s" % cl["cluster"], env_filter))
    log("Cost Management calls used: %d" % cost.calls)
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
