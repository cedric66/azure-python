"""AKS spot savings report: day-by-day cost after spot adoption.

This report answers: "after a cluster added spot node pools, did it actually
save money?" It uses subscription-scope Cost Management queries only.

Tabs: ReadMe, SpotSavingsSummary, FleetDailyTrend, SpotSavingsDaily,
SpotSavingsByPool, PriceReference, RawDailyCost.

Usage:
  python spot_savings.py --all
  python spot_savings.py --env dev --spot-trend-days 30 --spot-baseline-days 30
  python spot_savings.py --cluster aks-dev-01 --lookback-days 120
"""
import datetime as dt
from collections import defaultdict

import pandas as pd

from azrep import excel
from azrep.costmgmt import CostClient, dim_in
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope
from spot_cluster_report import (PM_ORDER, chunks, pool_from_resource_id,
                                 price_reference_rows, retail_price_lookup)

RG_CHUNK = 30
DAYS_PER_MONTH = 30.4375
SPOT_THRESHOLD_USD = 0.01


def date_range(d_from, d_to):
    days = []
    cur = d_from
    while cur <= d_to:
        days.append(cur.strftime("%Y-%m-%d"))
        cur += dt.timedelta(days=1)
    return days


def parse_date(value):
    if isinstance(value, dt.date):
        return value
    return dt.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def analysis_window(today, trend_days, baseline_days, trim_days, lookback_days):
    d_to = today - dt.timedelta(days=max(0, trim_days))
    lookback = lookback_days or (trend_days + baseline_days)
    lookback = max(1, lookback)
    d_from = d_to - dt.timedelta(days=lookback - 1)
    return d_from, d_to


def attach_resource_rows(frames, rg_map):
    cols = ["cluster_id", "cluster", "subscription", "environment", "location",
            "ResourceGroupName", "ResourceId", "PricingModel", "Period", "Date",
            "Cost", "CostUSD", "Currency"]
    if not frames:
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=cols)
    key = list(zip(df["subscription_id"], df["ResourceGroupName"].str.lower()))
    df["cluster_id"] = [rg_map.get(k, {}).get("id", "") for k in key]
    df["cluster"] = [rg_map.get(k, {}).get("cluster", "(unmatched)") for k in key]
    df["subscription"] = [rg_map.get(k, {}).get("subscription", "") for k in key]
    df["environment"] = [rg_map.get(k, {}).get("environment", "") for k in key]
    df["location"] = [rg_map.get(k, {}).get("location", "") for k in key]
    df["Date"] = df["Period"].str[:10]
    return df[df["cluster_id"] != ""]


def attach_fee_rows(frames, id_map):
    cols = ["cluster_id", "cluster", "subscription", "environment", "Date",
            "ResourceId", "PricingModel", "Cost", "CostUSD", "Currency"]
    if not frames:
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["cluster_id"] = df["ResourceId"].str.lower().map(
        lambda rid: id_map.get(rid, {}).get("id", ""))
    df["cluster"] = df["ResourceId"].str.lower().map(
        lambda rid: id_map.get(rid, {}).get("cluster", ""))
    df["subscription"] = df["ResourceId"].str.lower().map(
        lambda rid: id_map.get(rid, {}).get("subscription", ""))
    df["environment"] = df["ResourceId"].str.lower().map(
        lambda rid: id_map.get(rid, {}).get("environment", ""))
    df["Date"] = df["Period"].str[:10]
    return df[df["cluster_id"] != ""]


def collect_daily_cost(session, clusters, d_from, d_to):
    by_sub = defaultdict(list)
    for c in clusters:
        if c["node_resource_group"]:
            by_sub[c["subscription_id"]].append(c)
    rg_map = {(c["subscription_id"], c["node_resource_group"].lower()): c for c in clusters}
    id_map = {c["id"].lower(): c for c in clusters}

    cost = CostClient(session)
    res_rows, fee_rows = [], []
    n_subs = len(by_sub)
    for i, (sid, cls) in enumerate(sorted(by_sub.items()), 1):
        rgs = sorted({c["node_resource_group"].lower() for c in cls})
        scope = "/subscriptions/%s" % sid
        log("[%d/%d] %s: daily cost for %d node resource group(s)"
            % (i, n_subs, cls[0]["subscription"], len(rgs)))
        for ch in chunks(rgs, RG_CHUNK):
            df = cost.query(scope, "AmortizedCost", "Daily",
                            ("ResourceGroupName", "ResourceId", "PricingModel"),
                            dim_in("ResourceGroupName", ch), d_from, d_to)
            if not df.empty:
                df["subscription_id"] = sid
                res_rows.append(df)
        df = cost.query(scope, "AmortizedCost", "Daily", ("ResourceId", "PricingModel"),
                        dim_in("ResourceType", ["microsoft.containerservice/managedclusters"]),
                        d_from, d_to)
        if not df.empty:
            df["subscription_id"] = sid
            fee_rows.append(df)
    return {
        "res": attach_resource_rows(res_rows, rg_map),
        "fees": attach_fee_rows(fee_rows, id_map),
        "calls": cost.calls,
    }


def build_daily_breakdown(res, fees, clusters, d_from, d_to):
    dates = date_range(d_from, d_to)
    cluster_cols = ["cluster_id", "cluster", "subscription", "environment", "location"]
    cluster_rows = [{k: c[k if k != "cluster_id" else "id"] for k in cluster_cols}
                    for c in clusters]
    value_map = {}
    if not res.empty:
        grouped = res.groupby(["cluster_id", "Date", "PricingModel"], dropna=False)["CostUSD"].sum()
        value_map = grouped.to_dict()
    fee_map = {}
    if not fees.empty:
        fee_map = fees.groupby(["cluster_id", "Date"], dropna=False)["CostUSD"].sum().to_dict()

    rows = []
    for c in cluster_rows:
        for day in dates:
            row = dict(c)
            row["Date"] = day
            for pm in PM_ORDER:
                row[pm] = float(value_map.get((c["cluster_id"], day, pm), 0.0))
            row["Cluster fee"] = float(fee_map.get((c["cluster_id"], day), 0.0))
            row["Compute total (USD)"] = sum(row[pm] for pm in PM_ORDER)
            row["Total (USD)"] = row["Compute total (USD)"] + row["Cluster fee"]
            row["Spot %"] = row["Spot"] / row["Compute total (USD)"] \
                if row["Compute total (USD)"] else 0.0
            rows.append(row)
    return pd.DataFrame(rows)


def build_spot_estimates(res, pools, prices):
    cols = ["cluster_id", "cluster", "subscription", "environment", "location", "Date",
            "pool", "vm_size", "ResourceId", "spot_hr", "od_hr", "actual_spot_cost",
            "estimated_spot_node_hours", "od_counterfactual", "estimated_spot_saving",
            "price_status"]
    if res.empty:
        return pd.DataFrame(columns=cols)
    cfg = {(p["cluster_id"], p["pool"]): p for p in pools}
    rows = []
    r = res.copy()
    r["pool"] = r["ResourceId"].map(pool_from_resource_id)
    spot = r[(r["PricingModel"].astype(str).str.lower() == "spot")
             & (r["pool"] != "")
             & (r["CostUSD"].astype(float) > 0.0)]
    grouped = spot.groupby(["cluster_id", "cluster", "subscription", "environment",
                            "location", "Date", "pool", "ResourceId"], dropna=False)
    for keys, grp in grouped:
        p = cfg.get((keys[0], keys[6]), {})
        size = p.get("vm_size", "")
        pr = prices.get((keys[4], size)) if prices else None
        spot_hr = (pr or {}).get("spot_hr")
        od_hr = (pr or {}).get("od_hr")
        actual = float(grp["CostUSD"].sum())
        if spot_hr and od_hr and spot_hr > 0:
            hours = actual / float(spot_hr)
            od_cost = hours * float(od_hr)
            saving = od_cost - actual
            status = "priced"
        else:
            hours, od_cost, saving = None, None, None
            status = "price_missing"
        rows.append({
            "cluster_id": keys[0],
            "cluster": keys[1],
            "subscription": keys[2],
            "environment": keys[3],
            "location": keys[4],
            "Date": keys[5],
            "pool": keys[6],
            "vm_size": size,
            "ResourceId": keys[7],
            "spot_hr": spot_hr,
            "od_hr": od_hr,
            "actual_spot_cost": actual,
            "estimated_spot_node_hours": hours,
            "od_counterfactual": od_cost,
            "estimated_spot_saving": saving,
            "price_status": status,
        })
    return pd.DataFrame(rows, columns=cols)


def classify_verdict(saving, counterfactual, has_spot_cost, priced_rows):
    if not has_spot_cost:
        return "NO_SPOT_COST"
    if not priced_rows:
        return "PRICE_MISSING"
    band = max(1.0, abs(float(counterfactual or 0.0)) * 0.02)
    if saving > band:
        return "SAVING"
    if saving < -band:
        return "COST_UP"
    return "FLAT"


def annotate_daily_and_summary(daily, estimates, clusters, pools, trend_days=30,
                               baseline_days=30, min_baseline_days=3,
                               spot_threshold=SPOT_THRESHOLD_USD):
    if daily.empty:
        return daily, pd.DataFrame()
    daily = daily.sort_values(["cluster_id", "Date"]).reset_index(drop=True).copy()
    if not estimates.empty:
        est_day = estimates.groupby(["cluster_id", "Date"], dropna=False).agg({
            "actual_spot_cost": "sum",
            "od_counterfactual": "sum",
            "estimated_spot_saving": "sum",
        }).reset_index()
    else:
        est_day = pd.DataFrame(columns=["cluster_id", "Date", "actual_spot_cost",
                                        "od_counterfactual", "estimated_spot_saving"])
    daily = daily.merge(est_day, on=["cluster_id", "Date"], how="left")
    for col in ("actual_spot_cost", "od_counterfactual", "estimated_spot_saving"):
        daily[col] = daily[col].fillna(0.0)

    pool_summary = defaultdict(lambda: {"spot_pools": 0, "spot_nodes": 0})
    for p in pools:
        if str(p.get("priority", "")).lower() == "spot":
            pool_summary[p["cluster_id"]]["spot_pools"] += 1
            pool_summary[p["cluster_id"]]["spot_nodes"] += int(p.get("count") or 0)

    summaries, annotated = [], []
    max_date = parse_date(daily["Date"].max())
    trend_start = max_date - dt.timedelta(days=trend_days - 1)

    for c in clusters:
        cid = c["id"]
        df = daily[daily["cluster_id"] == cid].copy()
        if df.empty:
            continue
        df["_date"] = df["Date"].map(parse_date)
        spot_dates = df.loc[df["Spot"] > spot_threshold, "_date"]
        first_spot = spot_dates.min() if not spot_dates.empty else None
        if first_spot:
            baseline_start = first_spot - dt.timedelta(days=baseline_days)
            baseline = df[(df["_date"] < first_spot) & (df["_date"] >= baseline_start)]
            post_first = df[(df["_date"] >= first_spot)
                            & (df["_date"] < first_spot + dt.timedelta(days=trend_days))]
        else:
            baseline_start, baseline, post_first = None, df.iloc[0:0], df.iloc[0:0]
        last_n = df[df["_date"] >= trend_start]

        baseline_ok = len(baseline) >= min_baseline_days
        baseline_avg_total = float(baseline["Total (USD)"].mean()) if baseline_ok else None
        baseline_avg_od = float(baseline["OnDemand"].mean()) if baseline_ok else None
        post_avg_total = float(post_first["Total (USD)"].mean()) if len(post_first) else None
        last_avg_total = float(last_n["Total (USD)"].mean()) if len(last_n) else None
        last_avg_od = float(last_n["OnDemand"].mean()) if len(last_n) else None
        last_spot = last_n[last_n["Spot"] > spot_threshold]

        actual_spot = float(last_spot["actual_spot_cost"].sum()) if len(last_spot) else 0.0
        counterfactual = float(last_spot["od_counterfactual"].sum()) if len(last_spot) else 0.0
        saving = float(last_spot["estimated_spot_saving"].sum()) if len(last_spot) else 0.0
        priced_rows = bool(counterfactual > 0.0)
        verdict = classify_verdict(saving, counterfactual, len(last_spot) > 0, priced_rows)
        total_delta = (last_avg_total - baseline_avg_total) \
            if baseline_avg_total is not None and last_avg_total is not None else None
        od_delta = (last_avg_od - baseline_avg_od) \
            if baseline_avg_od is not None and last_avg_od is not None else None
        note = []
        if first_spot and first_spot == df["_date"].min():
            note.append("first spot cost is at lookback start; actual adoption may be earlier")
        if first_spot and len(baseline) < min_baseline_days:
            note.append("pre-spot baseline is too short")
        if first_spot and len(post_first) < trend_days:
            note.append("post-adoption window is shorter than requested trend")
        if verdict == "PRICE_MISSING":
            note.append("retail prices missing or disabled; counterfactual unavailable")
        if total_delta is not None:
            note.append("whole-cluster total delta is contextual and may include workload/capacity changes")

        summaries.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "location": c["location"],
            "current_spot_pools": pool_summary[cid]["spot_pools"],
            "current_spot_nodes": pool_summary[cid]["spot_nodes"],
            "first_observed_spot_cost_date": first_spot.strftime("%Y-%m-%d") if first_spot else "",
            "baseline_start": baseline_start.strftime("%Y-%m-%d") if baseline_start else "",
            "baseline_days_used": len(baseline),
            "post_first_days_used": len(post_first),
            "last_%d_days_used" % trend_days: len(last_n),
            "last_%d_spot_days" % trend_days: len(last_spot),
            "pre_spot_avg_daily_total": baseline_avg_total,
            "post_first_avg_daily_total": post_avg_total,
            "last_%d_avg_daily_total" % trend_days: last_avg_total,
            "total_delta_vs_pre_per_day": total_delta,
            "projected_monthly_total_delta": total_delta * DAYS_PER_MONTH
            if total_delta is not None else None,
            "on_demand_delta_vs_pre_per_day": od_delta,
            "last_%d_actual_spot_cost" % trend_days: actual_spot,
            "last_%d_od_counterfactual" % trend_days: counterfactual,
            "last_%d_estimated_spot_saving" % trend_days: saving if priced_rows else None,
            "projected_monthly_spot_saving": (saving / len(last_spot) * DAYS_PER_MONTH)
            if len(last_spot) and priced_rows else None,
            "last_%d_spot_share" % trend_days: (
                float(last_spot["Spot"].sum()) / float(last_spot["Compute total (USD)"].sum())
                if len(last_spot) and float(last_spot["Compute total (USD)"].sum()) else 0.0),
            "verdict": verdict,
            "note": "; ".join(note),
        })

        cum_saving, cum_total_delta = 0.0, 0.0
        phases, days_since, total_deltas, cum_savings, cum_total_deltas = [], [], [], [], []
        for _idx, r in df.iterrows():
            day = r["_date"]
            if first_spot:
                offset = (day - first_spot).days
                phase = "before_spot" if offset < 0 else "after_spot"
            else:
                offset, phase = None, "no_spot_observed"
            td = None
            if baseline_avg_total is not None:
                td = float(r["Total (USD)"]) - baseline_avg_total
            if first_spot and day >= first_spot:
                cum_saving += float(r["estimated_spot_saving"])
                if td is not None:
                    cum_total_delta += td
            phases.append(phase)
            days_since.append(offset)
            total_deltas.append(td)
            cum_savings.append(cum_saving if first_spot and day >= first_spot else None)
            cum_total_deltas.append(cum_total_delta if first_spot and day >= first_spot
                                    and td is not None else None)
        df["phase"] = phases
        df["days_since_first_spot_cost"] = days_since
        df["total_delta_vs_pre_avg"] = total_deltas
        df["cumulative_estimated_spot_saving"] = cum_savings
        df["cumulative_total_delta_vs_pre"] = cum_total_deltas
        annotated.append(df.drop(columns=["_date"]))

    out_daily = pd.concat(annotated, ignore_index=True) if annotated else daily
    return out_daily, pd.DataFrame(summaries)


def fleet_trend_rows(daily):
    if daily.empty:
        return pd.DataFrame()
    cols = PM_ORDER + ["Cluster fee", "Compute total (USD)", "Total (USD)",
                       "actual_spot_cost", "od_counterfactual", "estimated_spot_saving"]
    trend = daily.groupby("Date", dropna=False)[cols].sum().reset_index()
    trend["Spot %"] = trend.apply(
        lambda r: r["Spot"] / r["Compute total (USD)"] if r["Compute total (USD)"] else 0.0,
        axis=1)
    return trend


def pool_savings_rows(estimates, trend_days):
    cols = ["cluster", "subscription", "environment", "location", "pool", "vm_size",
            "ResourceId", "spot_days", "spot_hr", "od_hr", "actual_spot_cost",
            "estimated_spot_node_hours", "od_counterfactual", "estimated_spot_saving",
            "price_status"]
    if estimates.empty:
        return pd.DataFrame(columns=cols)
    max_date = parse_date(estimates["Date"].max())
    trend_start = max_date - dt.timedelta(days=trend_days - 1)
    e = estimates[estimates["Date"].map(parse_date) >= trend_start].copy()
    rows = []
    for keys, grp in e.groupby(["cluster", "subscription", "environment", "location",
                                "pool", "vm_size", "ResourceId"], dropna=False):
        rows.append({
            "cluster": keys[0],
            "subscription": keys[1],
            "environment": keys[2],
            "location": keys[3],
            "pool": keys[4],
            "vm_size": keys[5],
            "ResourceId": keys[6],
            "spot_days": grp["Date"].nunique(),
            "spot_hr": next((x for x in grp["spot_hr"] if pd.notna(x)), None),
            "od_hr": next((x for x in grp["od_hr"] if pd.notna(x)), None),
            "actual_spot_cost": float(grp["actual_spot_cost"].sum()),
            "estimated_spot_node_hours": float(grp["estimated_spot_node_hours"].sum())
            if grp["estimated_spot_node_hours"].notna().any() else None,
            "od_counterfactual": float(grp["od_counterfactual"].sum())
            if grp["od_counterfactual"].notna().any() else None,
            "estimated_spot_saving": float(grp["estimated_spot_saving"].sum())
            if grp["estimated_spot_saving"].notna().any() else None,
            "price_status": "priced" if (grp["price_status"] == "priced").any()
            else "price_missing",
        })
    return pd.DataFrame(rows, columns=cols).sort_values(
        "estimated_spot_saving", ascending=False, na_position="last")


def main(argv=None):
    p = base_parser("AKS spot savings after adoption")
    p.add_argument("--spot-trend-days", type=int, default=30,
                   help="trailing days to use for the current spot-savings verdict")
    p.add_argument("--spot-baseline-days", type=int, default=30,
                   help="days immediately before first observed spot cost for total-cost context")
    p.add_argument("--lookback-days", type=int, default=0,
                   help="daily cost lookback; default is baseline + trend days")
    p.add_argument("--trim-days", type=int, default=2,
                   help="drop the most recent N days because Cost Management data can lag")
    p.add_argument("--min-baseline-days", type=int, default=3,
                   help="minimum pre-spot days required before showing total-cost delta")
    p.add_argument("--spot-threshold-usd", type=float, default=SPOT_THRESHOLD_USD,
                   help="minimum daily Spot USD to count as spot adoption")
    p.add_argument("--currency", default="USD",
                   help="currency for Retail Prices API counterfactual")
    p.add_argument("--no-retail-prices", action="store_true",
                   help="skip Retail Prices API; total trend still works but savings verdict is PRICE_MISSING")
    args = p.parse_args(argv)

    today = dt.date.today()
    d_from, d_to = analysis_window(today, args.spot_trend_days, args.spot_baseline_days,
                                   args.trim_days, args.lookback_days)
    if d_to < d_from:
        raise SystemExit("Invalid date window after --trim-days")

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.15)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    log("Collecting daily amortized cost from %s to %s for %d cluster(s)..."
        % (d_from, d_to, len(clusters)))
    cost = collect_daily_cost(session, clusters, d_from, d_to)
    daily = build_daily_breakdown(cost["res"], cost["fees"], clusters, d_from, d_to)

    prices = {}
    if not args.no_retail_prices:
        spot_pools = [p0 for p0 in pools if str(p0.get("priority", "")).lower() == "spot"]
        prices = retail_price_lookup(spot_pools, args.currency) if spot_pools else {}
    estimates = build_spot_estimates(cost["res"], pools, prices)
    daily, summary = annotate_daily_and_summary(
        daily, estimates, clusters, pools, args.spot_trend_days, args.spot_baseline_days,
        args.min_baseline_days, args.spot_threshold_usd)
    summary = summary.sort_values(["verdict", "last_%d_estimated_spot_saving" %
                                   args.spot_trend_days],
                                  ascending=[True, False], na_position="last") \
        if not summary.empty else summary
    fleet_trend = fleet_trend_rows(daily)
    by_pool = pool_savings_rows(estimates, args.spot_trend_days)
    prdf = price_reference_rows(prices) if prices else pd.DataFrame(
        columns=["region", "vm_size", "od_hr", "spot_hr", "discount %"])

    raw = cost["res"].copy()
    if not raw.empty:
        raw["pool"] = raw["ResourceId"].map(pool_from_resource_id)

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Spot Savings Report", [
        "Generated: %s   Scope: %s   Daily cost window: %s to %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all",
         d_from, d_to),
        "Clusters in scope: %d   Cost Management calls: %d   Trailing days trimmed: %d" %
        (len(clusters), cost["calls"], args.trim_days),
        "",
        "First spot adoption is inferred from the first day Cost Management reports",
        "Spot spend above --spot-threshold-usd for the cluster. ARG exposes only",
        "current node-pool state, so this is a cost-observed date, not the ARM",
        "agent-pool creation timestamp.",
        "",
        "The headline verdict uses a retrospective retail counterfactual: actual",
        "Spot VMSS spend is converted to estimated spot node-hours using public",
        "Retail Prices API spot rates, then priced at the public on-demand rate.",
        "This approximates what the same spot usage would have cost as on-demand.",
        "",
        "Whole-cluster before/after total cost is included as context only. It can",
        "move because workload, autoscaler capacity, RI/SP coverage, disks, IPs or",
        "cluster fees changed; it is not used as the spot-savings verdict.",
        "",
        "Recent Cost Management data can lag, so the newest --trim-days are excluded",
        "from averages by default. Currency: USD for actual CostUSD; retail rates use",
        "--currency.",
    ])
    excel.add_table(wb, "SpotSavingsSummary", summary, section="summary",
                    money_cols=("pre_spot_avg_daily_total", "post_first_avg_daily_total",
                                "last_%d_avg_daily_total" % args.spot_trend_days,
                                "total_delta_vs_pre_per_day", "projected_monthly_total_delta",
                                "on_demand_delta_vs_pre_per_day",
                                "last_%d_actual_spot_cost" % args.spot_trend_days,
                                "last_%d_od_counterfactual" % args.spot_trend_days,
                                "last_%d_estimated_spot_saving" % args.spot_trend_days,
                                "projected_monthly_spot_saving"),
                    pct_cols=("last_%d_spot_share" % args.spot_trend_days,),
                    int_cols=("current_spot_pools", "current_spot_nodes",
                              "baseline_days_used", "post_first_days_used",
                              "last_%d_days_used" % args.spot_trend_days,
                              "last_%d_spot_days" % args.spot_trend_days),
                    fail_cols=("verdict",),
                    fail_values=("COST_UP", "PRICE_MISSING"),
                    warn_values=("FLAT", "BASELINE_MISSING", "NO_SPOT_COST"),
                    max_width=90)
    ws_trend = excel.add_table(wb, "FleetDailyTrend", fleet_trend, section="summary",
                               money_cols=tuple(PM_ORDER + ["Cluster fee",
                                                            "Compute total (USD)",
                                                            "Total (USD)",
                                                            "actual_spot_cost",
                                                            "od_counterfactual",
                                                            "estimated_spot_saving"]),
                               pct_cols=("Spot %",))
    if len(fleet_trend) > 1:
        total_col = list(fleet_trend.columns).index("Total (USD)") + 1
        spot_col = list(fleet_trend.columns).index("Spot") + 1
        excel.add_line_chart(ws_trend, "Fleet daily total and spot cost",
                             len(fleet_trend) + 1, spot_col, total_col,
                             "B%d" % (len(fleet_trend) + 4), y_title="USD")
    excel.add_table(wb, "SpotSavingsDaily", daily.drop(columns=["cluster_id"]),
                    money_cols=tuple(PM_ORDER + ["Cluster fee", "Compute total (USD)",
                                                 "Total (USD)", "actual_spot_cost",
                                                 "od_counterfactual",
                                                 "estimated_spot_saving",
                                                 "total_delta_vs_pre_avg",
                                                 "cumulative_estimated_spot_saving",
                                                 "cumulative_total_delta_vs_pre"]),
                    pct_cols=("Spot %",),
                    int_cols=("days_since_first_spot_cost",),
                    max_width=80)
    excel.add_table(wb, "SpotSavingsByPool", by_pool,
                    money_cols=("actual_spot_cost", "od_counterfactual",
                                "estimated_spot_saving"),
                    formats={"spot_hr": "#,##0.0000", "od_hr": "#,##0.0000"},
                    int_cols=("spot_days",), max_width=90)
    excel.add_table(wb, "PriceReference", prdf, section="reference",
                    formats={"od_hr": "#,##0.0000", "spot_hr": "#,##0.0000"},
                    pct_cols=("discount %",))
    excel.add_table(wb, "RawDailyCost", raw, section="reference",
                    money_cols=("Cost", "CostUSD"), max_width=90)

    path = excel.save(wb, out_path(args, "aks_spot_savings", env_filter))
    log("Cost Management calls used: %d" % cost["calls"])
    log("Report written: %s" % path)
    return path


if __name__ == "__main__":
    main()
