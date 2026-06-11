"""Fleet cost report: 3-month amortized cost trend for every AKS cluster in scope.

Designed for 25 subscriptions / 500 clusters without melting the Cost Management
API: costs are queried at SUBSCRIPTION scope grouped by node resource group
(3-4 queries per subscription, not per cluster) and joined back to clusters.

Tabs: ReadMe, ClusterCosts (per-cluster monthly trend, MoM, spot share, RI/SP
coverage, cluster fee), PricingModelSplit, TopMovers, MeterChanges (SKU change
signals fleet-wide), BySubscription, RawMonthly (+ RawDaily with --granularity daily).

Usage:
  python fleet_cost.py                        # interactive scope prompt
  python fleet_cost.py --nonprod
  python fleet_cost.py --env dev --months 3
  python fleet_cost.py --all --actual         # adds billed-vs-amortized delta
  python fleet_cost.py --all --granularity Daily
"""
import datetime as dt
from collections import defaultdict

import pandas as pd

from azrep import excel
from azrep.costmgmt import CostClient, default_window, dim_in
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

RG_CHUNK = 30
PM_ORDER = ["OnDemand", "Spot", "Reservation", "SavingsPlan"]


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def last_full_prev(months):
    """(prev_full, last_full) month labels; current MTD month is excluded."""
    cur = dt.date.today().strftime("%Y-%m")
    full = [m for m in months if m != cur]
    if len(full) >= 2:
        return full[-2], full[-1]
    return (None, full[-1]) if full else (None, None)


def main(argv=None):
    p = base_parser("Fleet-wide AKS amortized cost trend")
    p.add_argument("--months", type=int, default=3, help="full months of history")
    p.add_argument("--granularity", default="Monthly", choices=["Monthly", "Daily"])
    p.add_argument("--actual", action="store_true",
                   help="also query billed (actual) cost for the RI/SP delta column")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, _pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope - nothing to report.")
        return

    by_sub = defaultdict(list)
    for c in clusters:
        if c["node_resource_group"]:
            by_sub[c["subscription_id"]].append(c)
    rg_map = {(c["subscription_id"], c["node_resource_group"].lower()): c for c in clusters}
    id_map = {c["id"].lower(): c for c in clusters}

    d_from, d_to = default_window(args.months)
    cost = CostClient(session)
    pm_rows, meter_rows, fee_rows, actual_rows = [], [], [], []
    n_subs = len(by_sub)
    est = sum(2 * ((len(v) + RG_CHUNK - 1) // RG_CHUNK) + 1 + (1 if args.actual else 0)
              for v in by_sub.values())
    log("Cost window %s to %s; ~%d Cost Management queries across %d subscriptions."
        % (d_from, d_to, est, n_subs))
    log("Cost Management is QPU-throttled tenant-wide; the client paces itself and "
        "honors retry-after on 429, so a big fleet just takes a few minutes.")

    for i, (sid, cls) in enumerate(sorted(by_sub.items()), 1):
        rgs = sorted({c["node_resource_group"].lower() for c in cls})
        log("[%d/%d] %s: %d clusters, %d node RGs"
            % (i, n_subs, cls[0]["subscription"], len(cls), len(rgs)))
        scope = "/subscriptions/%s" % sid
        for ch in chunks(rgs, RG_CHUNK):
            f = dim_in("ResourceGroupName", ch)
            df = cost.query(scope, "AmortizedCost", args.granularity,
                            ("ResourceGroupName", "PricingModel"), f, d_from, d_to)
            if not df.empty:
                df["subscription_id"] = sid
                pm_rows.append(df)
            df2 = cost.query(scope, "AmortizedCost", "Monthly",
                             ("ResourceGroupName", "Meter"), f, d_from, d_to)
            if not df2.empty:
                df2["subscription_id"] = sid
                meter_rows.append(df2)
            if args.actual:
                df4 = cost.query(scope, "ActualCost", "Monthly",
                                 ("ResourceGroupName",), f, d_from, d_to)
                if not df4.empty:
                    df4["subscription_id"] = sid
                    actual_rows.append(df4)
        df3 = cost.query(scope, "AmortizedCost", "Monthly", ("ResourceId",),
                         dim_in("ResourceType", ["microsoft.containerservice/managedclusters"]),
                         d_from, d_to)
        if not df3.empty:
            df3["subscription_id"] = sid
            fee_rows.append(df3)

    def attach(frames):
        if not frames:
            return pd.DataFrame(columns=["cluster", "subscription", "environment",
                                         "location", "Period", "PricingModel", "Meter",
                                         "CostUSD"])
        df = pd.concat(frames, ignore_index=True)
        key = list(zip(df["subscription_id"], df["ResourceGroupName"].str.lower()))
        df["cluster"] = [rg_map.get(k, {}).get("cluster", "(unmatched)") for k in key]
        df["subscription"] = [rg_map.get(k, {}).get("subscription", "") for k in key]
        df["environment"] = [rg_map.get(k, {}).get("environment", "") for k in key]
        df["location"] = [rg_map.get(k, {}).get("location", "") for k in key]
        return df

    pm = attach(pm_rows)
    met = attach(meter_rows)
    act = attach(actual_rows) if args.actual else pd.DataFrame()
    fees = pd.concat(fee_rows, ignore_index=True) if fee_rows else pd.DataFrame()
    if not fees.empty:
        fees["cluster"] = fees["ResourceId"].str.lower().map(
            lambda x: id_map.get(x, {}).get("cluster"))
        fees = fees.dropna(subset=["cluster"])

    if pm.empty:
        log("No cost rows returned - check that you have Cost Management Reader access.")
        return

    pm["Month"] = pm["Period"].str[:7]
    cm = pm.pivot_table(index=["cluster", "subscription", "environment", "location"],
                        columns="Month", values="CostUSD", aggfunc="sum").fillna(0.0)
    months = sorted(cm.columns)
    cm = cm[months].reset_index()
    prev_m, last_m = last_full_prev(months)
    # sort BEFORE adding formula columns - formulas are anchored to row numbers
    cm = cm.sort_values(last_m or months[-1], ascending=False).reset_index(drop=True)

    nmeta = 4
    first_l = excel.get_column_letter(nmeta + 1)
    last_l = excel.get_column_letter(nmeta + len(months))
    n = len(cm)
    cm["Window total (USD)"] = ["=SUM(%s%d:%s%d)" % (first_l, r, last_l, r)
                                for r in range(2, n + 2)]
    if prev_m and last_m:
        pl = excel.get_column_letter(nmeta + 1 + months.index(prev_m))
        ll = excel.get_column_letter(nmeta + 1 + months.index(last_m))
        cm["MoM %"] = ["=IF(%s%d=0,\"\",(%s%d-%s%d)/%s%d)" % (pl, r, ll, r, pl, r, pl, r)
                       for r in range(2, n + 2)]
    spot = pm[pm["PricingModel"].astype(str).str.lower() == "spot"] \
        .groupby("cluster")["CostUSD"].sum()
    risp = pm[pm["PricingModel"].astype(str).str.lower().isin(
        ["reservation", "reservations", "savingsplan", "savings plan"])] \
        .groupby("cluster")["CostUSD"].sum()
    cm["Spot (USD)"] = cm["cluster"].map(spot).fillna(0.0)
    cm["RI+SP (USD)"] = cm["cluster"].map(risp).fillna(0.0)
    tot_l = excel.get_column_letter(nmeta + len(months) + 1)
    spot_l = excel.get_column_letter(len(cm.columns) - 1)
    cm["Spot %"] = ["=IF(%s%d=0,\"\",%s%d/%s%d)" % (tot_l, r, spot_l, r, tot_l, r)
                    for r in range(2, n + 2)]
    if not fees.empty:
        cm["Cluster fee (USD)"] = cm["cluster"].map(
            fees.groupby("cluster")["CostUSD"].sum()).fillna(0.0)
    if args.actual and not act.empty:
        adel = act.groupby("cluster")["CostUSD"].sum()
        amor = pm.groupby("cluster")["CostUSD"].sum()
        cm["Amortized-Actual (USD)"] = cm["cluster"].map(amor - adel).fillna(0.0)

    # pricing model split (window totals per cluster)
    split = pm.pivot_table(index=["cluster", "subscription", "environment"],
                           columns="PricingModel", values="CostUSD", aggfunc="sum") \
        .fillna(0.0).reset_index()
    pm_cols = [c for c in PM_ORDER if c in split.columns] + \
              [c for c in split.columns if c not in PM_ORDER + ["cluster", "subscription", "environment"]]
    split = split[["cluster", "subscription", "environment"] + pm_cols]
    sn = len(split)
    sl_first, sl_last = excel.get_column_letter(4), excel.get_column_letter(3 + len(pm_cols))
    split["Total (USD)"] = ["=SUM(%s%d:%s%d)" % (sl_first, r, sl_last, r)
                            for r in range(2, sn + 2)]
    if "Spot" in split.columns:
        spl = excel.get_column_letter(3 + 1 + pm_cols.index("Spot") - 0)
        ttl = excel.get_column_letter(3 + len(pm_cols) + 1)
        split["Spot %"] = ["=IF(%s%d=0,\"\",%s%d/%s%d)" % (ttl, r, spl, r, ttl, r)
                           for r in range(2, sn + 2)]

    # top movers (last full vs previous full month)
    movers = pd.DataFrame()
    if prev_m and last_m:
        mv = pm.pivot_table(index=["cluster", "subscription", "environment"],
                            columns="Month", values="CostUSD", aggfunc="sum").fillna(0.0)
        for col in (prev_m, last_m):
            if col not in mv.columns:
                mv[col] = 0.0
        mv = mv.reset_index()[["cluster", "subscription", "environment", prev_m, last_m]]
        mv["delta_abs"] = (mv[last_m] - mv[prev_m]).abs()
        mv = mv.sort_values("delta_abs", ascending=False).head(50).drop(columns="delta_abs")
        mn = len(mv)
        mv["Delta (USD)"] = ["=E%d-D%d" % (r, r) for r in range(2, mn + 2)]
        mv["Delta %"] = ["=IF(D%d=0,\"\",(E%d-D%d)/D%d)" % (r, r, r, r) for r in range(2, mn + 2)]
        movers = mv

    # meter / SKU change signals
    chg_rows = []
    if not met.empty:
        met["Month"] = met["Period"].str[:7]
        mm = sorted(met["Month"].unique())
        cur = dt.date.today().strftime("%Y-%m")
        full_mm = [m for m in mm if m != cur]
        if len(mm) >= 2 and full_mm:
            piv = met.pivot_table(index=["cluster", "Meter"], columns="Month",
                                  values="CostUSD", aggfunc="sum").fillna(0.0)
            for (clname, meter), r in piv.iterrows():
                active = [m for m in mm if m in r.index and r[m] > 5.0]
                if not active:
                    continue
                # compare full months only - the current MTD month would make
                # every meter look like it SHRUNK
                active_full = [m for m in active if m != cur]
                first_v = float(r[active[0]])
                last_v = float(r[active_full[-1]]) if active_full else float(r[active[-1]])
                status = None
                if active[0] != mm[0]:
                    status = "NEW"
                elif active[-1] < full_mm[-1]:
                    status = "REMOVED"
                elif len(active_full) >= 2:
                    first_v = float(r[active_full[0]])
                    if first_v > 5 and (last_v - first_v) / first_v > 0.5:
                        status = "GROWN"
                    elif first_v > 5 and (last_v - first_v) / first_v < -0.5:
                        status = "SHRUNK"
                if status:
                    chg_rows.append({"cluster": clname, "meter": meter, "status": status,
                                     "first_active_month": active[0],
                                     "last_active_month": active[-1],
                                     "first_usd": round(first_v, 2),
                                     "last_usd": round(last_v, 2)})
    chg = pd.DataFrame(chg_rows) if chg_rows else pd.DataFrame(
        columns=["cluster", "meter", "status", "first_active_month",
                 "last_active_month", "first_usd", "last_usd"])

    bysub = pm.pivot_table(index="subscription", columns="Month", values="CostUSD",
                           aggfunc="sum").fillna(0.0).reset_index()
    bn = len(bysub)
    bl = excel.get_column_letter(1 + len(months))
    bysub["Window total (USD)"] = ["=SUM(B%d:%s%d)" % (r, bl, r) for r in range(2, bn + 2)]

    raw_m = pm.groupby(["cluster", "subscription", "environment", "Month",
                        "PricingModel"])["CostUSD"].sum().reset_index() \
        .rename(columns={"CostUSD": "Amortized (USD)"})

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Fleet Cost Report (amortized)", [
        "Generated: %s   Window: %s to %s   Scope: %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), d_from, d_to, env_filter or "all"),
        "Clusters: %d   Cost Management queries: %d" % (len(clusters), cost.calls),
        "",
        "Amortized cost spreads reservation & savings-plan purchases across consuming",
        "resources - this is the 'true' cluster cost. A cluster's cost = its node resource",
        "group (MC_*). 'Cluster fee' is the managed cluster resource itself (uptime SLA).",
        "The current month is partial (MTD). Currency: USD (CostUSD).",
        "Only clusters that exist today are included; deleted clusters' history is not.",
        "MeterChanges flags meters that appeared (NEW), disappeared (REMOVED) or moved",
        ">50% between the first and last month with >$5 spend - SKU change signals.",
    ])
    excel.add_table(wb, "ClusterCosts", cm,
                    money_cols=tuple(months) + ("Window total (USD)", "Spot (USD)",
                                                "RI+SP (USD)", "Cluster fee (USD)",
                                                "Amortized-Actual (USD)"),
                    pct_cols=("MoM %", "Spot %"), colorscale_cols=("MoM %",))
    excel.add_table(wb, "PricingModelSplit", split,
                    money_cols=tuple(pm_cols) + ("Total (USD)",), pct_cols=("Spot %",))
    if not movers.empty:
        ws_movers = excel.add_table(wb, "TopMovers", movers,
                                    money_cols=(prev_m, last_m, "Delta (USD)"), pct_cols=("Delta %",),
                                    colorscale_cols=("Delta %",))
        delta_col = list(movers.columns).index("Delta (USD)") + 1
        excel.add_bar_chart(ws_movers, "Top cost movers - delta USD",
                            len(movers) + 1, delta_col, "B%d" % (len(movers) + 4),
                            y_title="USD")
    excel.add_table(wb, "MeterChanges", chg, fail_cols=("status",),
                    fail_values=("REMOVED",), warn_values=("NEW", "GROWN", "SHRUNK"),
                    money_cols=("first_usd", "last_usd"), max_width=70)
    ws = excel.add_table(wb, "BySubscription", bysub,
                         money_cols=tuple(months) + ("Window total (USD)",))
    total_col = list(bysub.columns).index("Window total (USD)") + 1
    excel.add_bar_chart(ws, "Window amortized cost by subscription",
                        len(bysub) + 1, total_col, "B%d" % (len(bysub) + 5),
                        y_title="USD")
    excel.add_total_row(ws, bysub, list(months) + ["Window total (USD)"],
                        label_col="subscription")
    excel.add_table(wb, "RawMonthly", raw_m, money_cols=("Amortized (USD)",))
    if args.granularity == "Daily":
        raw_d = pm.groupby(["cluster", "subscription", "Period"])["CostUSD"].sum() \
            .reset_index().rename(columns={"Period": "Date", "CostUSD": "Amortized (USD)"})
        excel.add_table(wb, "RawDaily", raw_d, money_cols=("Amortized (USD)",))

    path = excel.save(wb, out_path(args, "aks_fleet_cost", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
