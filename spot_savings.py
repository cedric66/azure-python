"""AKS spot savings report: day-by-day cost after spot adoption.

This report answers: "after a cluster added spot node pools, did it actually
save money?" It uses subscription-scope Cost Management queries only.

Tabs: ReadMe, BeforeSpot, AfterSpot, SavingsProjection, ActualVsProjection,
SpotSavingsSummary, FleetDailyTrend, SpotSavingsDaily, SpotSavingsByPool,
PriceReference, RawDailyCost.

Usage:
  python spot_savings.py --all
  python spot_savings.py --env dev --spot-trend-days 30 --spot-baseline-days 30
  python spot_savings.py --cluster aks-dev-01 --lookback-days 120
  python spot_savings.py --cluster aks-dev-01 --project-move-od-nodes 3
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
            "cluster_id": c["id"],
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


def spot_windows(daily, trend_days, baseline_days, spot_threshold):
    if daily.empty:
        return {}
    out = {}
    max_date = parse_date(daily["Date"].max())
    trend_start = max_date - dt.timedelta(days=trend_days - 1)
    for cid, df in daily.groupby("cluster_id", dropna=False):
        d = df.copy()
        d["_date"] = d["Date"].map(parse_date)
        spot_dates = d.loc[d["Spot"] > spot_threshold, "_date"]
        first_spot = spot_dates.min() if not spot_dates.empty else None
        if first_spot:
            before_start = first_spot - dt.timedelta(days=baseline_days)
            before_end = first_spot - dt.timedelta(days=1)
            after_start = max(first_spot, trend_start)
            after_end = max_date
        else:
            before_start = before_end = after_start = after_end = None
        out[cid] = {
            "first_spot": first_spot,
            "before_start": before_start,
            "before_end": before_end,
            "after_start": after_start,
            "after_end": after_end,
            "max_date": max_date,
        }
    return out


def _pool_lookup(pools):
    return {(p["cluster_id"], p["pool"]): p for p in pools}


def _pool_price(prices, location, vm_size):
    return prices.get((location, vm_size)) if prices else None


def enrich_vmss_cost(res, pools, prices):
    cols = ["cluster_id", "cluster", "subscription", "environment", "location", "Date",
            "pool", "vm_size", "node_type_mode", "current_priority",
            "current_nodes_now", "ResourceId", "billing_type", "CostUSD",
            "od_hr", "spot_hr", "node_equiv_hours_at_retail", "node_equiv_source"]
    if res.empty:
        return pd.DataFrame(columns=cols)
    cfg = _pool_lookup(pools)
    rows = []
    r = res.copy()
    r["pool"] = r["ResourceId"].map(pool_from_resource_id)
    r = r[r["pool"] != ""]
    for q in r.itertuples(index=False):
        p = cfg.get((q.cluster_id, q.pool), {})
        vm_size = p.get("vm_size") or ""
        pr = _pool_price(prices, q.location, vm_size) or {}
        billing = str(q.PricingModel or "")
        low = billing.lower()
        hourly = None
        source = "unavailable"
        if low == "spot" and pr.get("spot_hr"):
            hourly = float(pr["spot_hr"])
            source = "spot_retail_rate"
        elif low == "ondemand" and pr.get("od_hr"):
            hourly = float(pr["od_hr"])
            source = "od_retail_rate"
        node_hours = float(q.CostUSD) / hourly if hourly else None
        rows.append({
            "cluster_id": q.cluster_id,
            "cluster": q.cluster,
            "subscription": q.subscription,
            "environment": q.environment,
            "location": q.location,
            "Date": q.Date,
            "pool": q.pool,
            "vm_size": vm_size or "(historical/unknown)",
            "node_type_mode": p.get("mode") or "(current mode unknown)",
            "current_priority": p.get("priority") or "(current priority unknown)",
            "current_nodes_now": p.get("count"),
            "ResourceId": q.ResourceId,
            "billing_type": billing,
            "CostUSD": float(q.CostUSD),
            "od_hr": pr.get("od_hr"),
            "spot_hr": pr.get("spot_hr"),
            "node_equiv_hours_at_retail": node_hours,
            "node_equiv_source": source,
        })
    return pd.DataFrame(rows, columns=cols)


def _period_mask(df, start, end):
    if start is None or end is None or df.empty:
        return pd.Series([False] * len(df), index=df.index)
    dates = df["Date"].map(parse_date)
    return (dates >= start) & (dates <= end)


def _window_total_row(cluster, window, daily, pools):
    if daily.empty:
        compute = fee = total = 0.0
    else:
        compute = float(daily["Compute total (USD)"].sum())
        fee = float(daily["Cluster fee"].sum())
        total = float(daily["Total (USD)"].sum())
    current_nodes = sum(int(p.get("count") or 0)
                        for p in pools if p["cluster_id"] == cluster["id"])
    return {
        "cluster": cluster["cluster"],
        "subscription": cluster["subscription"],
        "environment": cluster["environment"],
        "location": cluster["location"],
        "window_start": window["start"].strftime("%Y-%m-%d") if window["start"] else "",
        "window_end": window["end"].strftime("%Y-%m-%d") if window["end"] else "",
        "days_used": window["days"],
        "row_type": "cluster_total",
        "pool": "(cluster total)",
        "vm_size": "all",
        "node_type_mode": "all",
        "current_priority": "all",
        "billing_type": "All",
        "current_nodes_now": current_nodes,
        "avg_node_equiv_at_retail": None,
        "node_count_source": "current_arg_current_state",
        "actual_vmss_cost_usd": compute,
        "cluster_fee_usd": fee,
        "end_to_end_cost_usd": total,
    }


def period_cost_table(daily, vmss, clusters, pools, windows, phase):
    cols = ["cluster", "subscription", "environment", "location", "window_start",
            "window_end", "days_used", "row_type", "pool", "vm_size",
            "node_type_mode", "current_priority", "billing_type",
            "current_nodes_now", "avg_node_equiv_at_retail", "node_count_source",
            "actual_vmss_cost_usd", "cluster_fee_usd", "end_to_end_cost_usd"]
    rows = []
    by_cluster = {c["id"]: c for c in clusters}
    for cid, c in by_cluster.items():
        w = windows.get(cid) or {}
        if phase == "before":
            start, end = w.get("before_start"), w.get("before_end")
        else:
            start, end = w.get("after_start"), w.get("after_end")
        if not start or not end:
            continue
        d = daily[(daily["cluster_id"] == cid) & _period_mask(daily, start, end)]
        v = vmss[(vmss["cluster_id"] == cid) & _period_mask(vmss, start, end)]
        window = {"start": start, "end": end, "days": len(set(d["Date"]))}
        if window["days"] <= 0:
            continue
        for keys, grp in v.groupby(["pool", "vm_size", "node_type_mode", "current_priority",
                                    "billing_type"], dropna=False):
            current_nodes = next((x for x in grp["current_nodes_now"] if pd.notna(x)), None)
            node_hours = grp["node_equiv_hours_at_retail"].dropna()
            avg_equiv = float(node_hours.sum()) / (24.0 * window["days"]) \
                if len(node_hours) else None
            if pd.notna(current_nodes):
                source = "current_arg_current_state"
            elif avg_equiv is not None:
                source = "retail_cost_node_equiv"
            else:
                source = "unavailable"
            rows.append({
                "cluster": c["cluster"],
                "subscription": c["subscription"],
                "environment": c["environment"],
                "location": c["location"],
                "window_start": start.strftime("%Y-%m-%d"),
                "window_end": end.strftime("%Y-%m-%d"),
                "days_used": window["days"],
                "row_type": "node_pool",
                "pool": keys[0],
                "vm_size": keys[1],
                "node_type_mode": keys[2],
                "current_priority": keys[3],
                "billing_type": keys[4],
                "current_nodes_now": int(current_nodes) if pd.notna(current_nodes) else None,
                "avg_node_equiv_at_retail": round(avg_equiv, 1) if avg_equiv is not None else None,
                "node_count_source": source,
                "actual_vmss_cost_usd": float(grp["CostUSD"].sum()),
                "cluster_fee_usd": None,
                "end_to_end_cost_usd": None,
            })
        rows.append(_window_total_row(c, window, d, pools))
    return pd.DataFrame(rows, columns=cols)


def _projectable_pools(pools, prices, cluster_id):
    rows = []
    for p in pools:
        if p["cluster_id"] != cluster_id:
            continue
        if str(p.get("priority", "")).lower() == "spot":
            continue
        if str(p.get("mode", "")).lower() != "user":
            continue
        if str(p.get("os_type", "")).lower() == "windows":
            continue
        if int(p.get("count") or 0) <= 0:
            continue
        pr = _pool_price(prices, p["location"], p["vm_size"]) or {}
        if not (pr.get("od_hr") and pr.get("spot_hr")):
            continue
        rows.append((p, float(pr["od_hr"]), float(pr["spot_hr"])))
    return rows


def _projection_for_cluster(pools, prices, cluster_id, move_override, project_days):
    candidates = _projectable_pools(pools, prices, cluster_id)
    current_od_nodes = sum(int(p.get("count") or 0) for p, _od, _sp in candidates)
    spot_nodes = sum(int(p.get("count") or 0) for p in pools
                     if p["cluster_id"] == cluster_id
                     and str(p.get("priority", "")).lower() == "spot")
    if move_override is None:
        remaining = float(current_od_nodes)
        assumption = "theoretical max: all priced regular Linux User nodes move to spot"
    else:
        remaining = min(float(move_override), float(current_od_nodes))
        assumption = "user override: move up to %.1f regular Linux User nodes to spot" % remaining
    projected = 0.0
    moved = 0.0
    for p, od_hr, spot_hr in sorted(candidates, key=lambda x: x[1] - x[2], reverse=True):
        if remaining <= 0:
            break
        n = min(float(p.get("count") or 0), remaining)
        projected += n * max(0.0, od_hr - spot_hr) * 24.0 * float(project_days)
        moved += n
        remaining -= n
    return {
        "current_regular_user_od_nodes": current_od_nodes,
        "current_spot_nodes": spot_nodes,
        "project_move_to_spot_nodes": moved,
        "project_on_demand_nodes_after": max(0.0, current_od_nodes - moved),
        "projected_saving_usd": projected,
        "projected_monthly_saving_usd": projected / float(project_days) * DAYS_PER_MONTH
        if project_days else None,
        "projection_assumption": assumption if candidates else "no priced regular Linux User OD pools",
    }


def savings_projection_table(summary, daily, clusters, pools, prices, windows,
                             trend_days, baseline_days, project_days, move_override):
    cols = ["cluster", "subscription", "environment", "location",
            "first_observed_spot_cost_date", "before_window", "after_window",
            "before_days", "after_days", "before_end_to_end_cost_usd",
            "after_end_to_end_cost_usd", "actual_total_saving_vs_before_rate_usd",
            "actual_total_delta_pct", "actual_spot_cost_usd",
            "od_counterfactual_usd", "counterfactual_spot_saving_usd", "verdict",
            "current_regular_user_od_nodes", "current_spot_nodes",
            "project_move_to_spot_nodes", "project_on_demand_nodes_after",
            "project_days", "projected_saving_usd", "projected_monthly_saving_usd",
            "projection_assumption"]
    sidx = summary.set_index("cluster_id") if not summary.empty and "cluster_id" in summary else pd.DataFrame()
    rows = []
    for c in clusters:
        cid = c["id"]
        w = windows.get(cid) or {}
        first_spot = w.get("first_spot")
        before_start, before_end = w.get("before_start"), w.get("before_end")
        after_start, after_end = w.get("after_start"), w.get("after_end")
        d = daily[daily["cluster_id"] == cid]
        before = d[_period_mask(d, before_start, before_end)]
        after = d[_period_mask(d, after_start, after_end)]
        before_days = len(set(before["Date"]))
        after_days = len(set(after["Date"]))
        before_total = float(before["Total (USD)"].sum()) if before_days else None
        after_total = float(after["Total (USD)"].sum()) if after_days else None
        before_avg = before_total / before_days if before_days and before_total is not None else None
        saving_vs_before_rate = (before_avg * after_days - after_total) \
            if before_avg is not None and after_total is not None else None
        delta_pct = (saving_vs_before_rate / (before_avg * after_days)) \
            if saving_vs_before_rate is not None and before_avg and after_days else None
        srow = sidx.loc[cid] if cid in sidx.index else {}
        proj = _projection_for_cluster(pools, prices, cid, move_override, project_days)
        rows.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "location": c["location"],
            "first_observed_spot_cost_date": first_spot.strftime("%Y-%m-%d")
            if first_spot else "",
            "before_window": "%s to %s" % (
                before_start.strftime("%Y-%m-%d") if before_start else "",
                before_end.strftime("%Y-%m-%d") if before_end else ""),
            "after_window": "%s to %s" % (
                after_start.strftime("%Y-%m-%d") if after_start else "",
                after_end.strftime("%Y-%m-%d") if after_end else ""),
            "before_days": before_days,
            "after_days": after_days,
            "before_end_to_end_cost_usd": before_total,
            "after_end_to_end_cost_usd": after_total,
            "actual_total_saving_vs_before_rate_usd": saving_vs_before_rate,
            "actual_total_delta_pct": delta_pct,
            "actual_spot_cost_usd": float(srow.get("last_%d_actual_spot_cost" % trend_days, 0.0))
            if hasattr(srow, "get") else 0.0,
            "od_counterfactual_usd": float(srow.get("last_%d_od_counterfactual" % trend_days, 0.0))
            if hasattr(srow, "get") else 0.0,
            "counterfactual_spot_saving_usd": srow.get(
                "last_%d_estimated_spot_saving" % trend_days) if hasattr(srow, "get") else None,
            "verdict": srow.get("verdict", "") if hasattr(srow, "get") else "",
            "current_regular_user_od_nodes": proj["current_regular_user_od_nodes"],
            "current_spot_nodes": proj["current_spot_nodes"],
            "project_move_to_spot_nodes": proj["project_move_to_spot_nodes"],
            "project_on_demand_nodes_after": proj["project_on_demand_nodes_after"],
            "project_days": project_days,
            "projected_saving_usd": proj["projected_saving_usd"],
            "projected_monthly_saving_usd": proj["projected_monthly_saving_usd"],
            "projection_assumption": proj["projection_assumption"],
        })
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df = df.sort_values(["verdict", "projected_monthly_saving_usd"],
                            ascending=[True, False],
                            na_position="last").reset_index(drop=True)
    return df


def projection_chart_rows(daily, projection, project_days):
    cols = ["Date", "Actual total USD", "OD counterfactual total USD",
            "Modeled future total USD"]
    if daily.empty:
        return pd.DataFrame(columns=cols)
    hist = daily.groupby("Date", dropna=False).agg({
        "Total (USD)": "sum",
        "actual_spot_cost": "sum",
        "od_counterfactual": "sum",
    }).reset_index().sort_values("Date")
    rows = []
    for _idx, r in hist.iterrows():
        counter = float(r["Total (USD)"]) - float(r["actual_spot_cost"]) + float(r["od_counterfactual"])
        rows.append({
            "Date": r["Date"],
            "Actual total USD": float(r["Total (USD)"]),
            "OD counterfactual total USD": counter if float(r["od_counterfactual"]) else None,
            "Modeled future total USD": None,
        })
    max_date = parse_date(hist["Date"].max())
    trailing = hist.tail(min(30, len(hist)))
    base = float(trailing["Total (USD)"].mean()) if len(trailing) else 0.0
    daily_saving = 0.0
    if project_days and not projection.empty:
        daily_saving = float(projection["projected_saving_usd"].fillna(0.0).sum()) / project_days
    rows.append({
        "Date": max_date.strftime("%Y-%m-%d"),
        "Actual total USD": None,
        "OD counterfactual total USD": None,
        "Modeled future total USD": base,
    })
    for i in range(1, project_days + 1):
        day = max_date + dt.timedelta(days=i)
        rows.append({
            "Date": day.strftime("%Y-%m-%d"),
            "Actual total USD": None,
            "OD counterfactual total USD": None,
            "Modeled future total USD": max(0.0, base - daily_saving),
        })
    return pd.DataFrame(rows, columns=cols)


def add_actual_projection_chart(ws, chart_data):
    if len(chart_data) <= 1:
        return None
    ch = excel.add_line_chart(ws, "Actual total cost vs counterfactual/projection",
                              len(chart_data) + 1, 2, 4,
                              "B%d" % (len(chart_data) + 4), y_title="USD")
    for idx in (1, 2):
        if idx < len(ch.series):
            ch.series[idx].graphicalProperties.line.dashStyle = "dash"
    return ch


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
    p.add_argument("--project-days", type=int, default=30,
                   help="days for the modeled future projection")
    p.add_argument("--project-move-od-nodes", type=float, default=None,
                   help="override how many current regular Linux User OD nodes to model moving to spot per cluster")
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
        priced_pools = [p0 for p0 in pools if p0.get("vm_size")]
        prices = retail_price_lookup(priced_pools, args.currency) if priced_pools else {}
    estimates = build_spot_estimates(cost["res"], pools, prices)
    daily, summary = annotate_daily_and_summary(
        daily, estimates, clusters, pools, args.spot_trend_days, args.spot_baseline_days,
        args.min_baseline_days, args.spot_threshold_usd)
    summary = summary.sort_values(["verdict", "last_%d_estimated_spot_saving" %
                                   args.spot_trend_days],
                                  ascending=[True, False], na_position="last") \
        if not summary.empty else summary
    windows = spot_windows(daily, args.spot_trend_days, args.spot_baseline_days,
                           args.spot_threshold_usd)
    vmss = enrich_vmss_cost(cost["res"], pools, prices)
    before_spot = period_cost_table(daily, vmss, clusters, pools, windows, "before")
    after_spot = period_cost_table(daily, vmss, clusters, pools, windows, "after")
    projection = savings_projection_table(
        summary, daily, clusters, pools, prices, windows, args.spot_trend_days,
        args.spot_baseline_days, args.project_days, args.project_move_od_nodes)
    chart_data = projection_chart_rows(daily, projection, args.project_days)
    fleet_trend = fleet_trend_rows(daily)
    by_pool = pool_savings_rows(estimates, args.spot_trend_days)
    prdf = price_reference_rows(prices) if prices else pd.DataFrame(
        columns=["region", "vm_size", "od_hr", "spot_hr", "discount %"])

    raw = cost["res"].drop(
        columns=["UsageDate", "subscription_id", "cluster_id"], errors="ignore").copy()
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
        "cluster fees changed; it is not used as the spot-savings verdict. In",
        "SavingsProjection, actual_total_saving_vs_before_rate_usd applies the pre-spot",
        "daily rate across the after-window day count, so it is workload-confounded.",
        "",
        "The BeforeSpot and AfterSpot tables use actual Cost Management rows by",
        "PricingModel. current_nodes_now is current ARG inventory only; historical",
        "node counts are not available from subscription-reader APIs. avg_node_equiv",
        "is shown only as a cost/rate annotation where retail rates are available.",
        "",
        "Recent Cost Management data can lag, so the newest --trim-days are excluded",
        "from averages by default. Currency: USD for actual CostUSD; retail rates use",
        "--currency.",
    ])
    period_money = ("actual_vmss_cost_usd", "cluster_fee_usd", "end_to_end_cost_usd")
    period_ints = ("days_used", "current_nodes_now")
    excel.add_table(wb, "BeforeSpot", before_spot, section="summary",
                    money_cols=period_money, int_cols=period_ints,
                    formats={"avg_node_equiv_at_retail": "0.0"}, max_width=90)
    excel.add_table(wb, "AfterSpot", after_spot, section="summary",
                    money_cols=period_money, int_cols=period_ints,
                    formats={"avg_node_equiv_at_retail": "0.0"}, max_width=90)
    excel.add_table(wb, "SavingsProjection", projection, section="summary",
                    money_cols=("before_end_to_end_cost_usd", "after_end_to_end_cost_usd",
                                "actual_total_saving_vs_before_rate_usd",
                                "actual_spot_cost_usd", "od_counterfactual_usd",
                                "counterfactual_spot_saving_usd",
                                "projected_saving_usd", "projected_monthly_saving_usd"),
                    pct_cols=("actual_total_delta_pct",),
                    int_cols=("before_days", "after_days",
                              "current_regular_user_od_nodes", "current_spot_nodes",
                              "project_days"),
                    formats={"project_move_to_spot_nodes": "0.0",
                             "project_on_demand_nodes_after": "0.0"},
                    fail_cols=("verdict",),
                    fail_values=("COST_UP", "PRICE_MISSING"),
                    warn_values=("FLAT", "BASELINE_MISSING", "NO_SPOT_COST"),
                    max_width=100)
    ws_chart = excel.add_table(wb, "ActualVsProjection", chart_data, section="summary",
                               money_cols=("Actual total USD",
                                           "OD counterfactual total USD",
                                           "Modeled future total USD"))
    add_actual_projection_chart(ws_chart, chart_data)
    summary_display = summary.drop(columns=["cluster_id"], errors="ignore")
    excel.add_table(wb, "SpotSavingsSummary", summary_display, section="summary",
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
    fleet_disp = fleet_trend.rename(columns={
        "Cluster fee": "cluster_fee_usd", "Compute total (USD)": "compute_total_usd",
        "Total (USD)": "total_usd", "Spot %": "spot_share"})
    ws_trend = excel.add_table(wb, "FleetDailyTrend", fleet_disp, section="summary",
                               money_cols=tuple(PM_ORDER + ["cluster_fee_usd",
                                                            "compute_total_usd",
                                                            "total_usd",
                                                            "actual_spot_cost",
                                                            "od_counterfactual",
                                                            "estimated_spot_saving"]),
                               pct_cols=("spot_share",))
    if len(fleet_trend) > 1:
        total_col = list(fleet_disp.columns).index("total_usd") + 1
        spot_col = list(fleet_disp.columns).index("Spot") + 1
        excel.add_line_chart(ws_trend, "Fleet daily total and spot cost",
                             len(fleet_trend) + 1, spot_col, total_col,
                             "B%d" % (len(fleet_trend) + 4), y_title="USD")
    daily_disp = daily.drop(columns=["cluster_id"]).rename(columns={
        "Cluster fee": "cluster_fee_usd", "Compute total (USD)": "compute_total_usd",
        "Total (USD)": "total_usd", "Spot %": "spot_share"})
    excel.add_table(wb, "SpotSavingsDaily", daily_disp,
                    money_cols=tuple(PM_ORDER + ["cluster_fee_usd", "compute_total_usd",
                                                 "total_usd", "actual_spot_cost",
                                                 "od_counterfactual",
                                                 "estimated_spot_saving",
                                                 "total_delta_vs_pre_avg",
                                                 "cumulative_estimated_spot_saving",
                                                 "cumulative_total_delta_vs_pre"]),
                    pct_cols=("spot_share",),
                    int_cols=("days_since_first_spot_cost",),
                    max_width=80)
    excel.add_table(wb, "SpotSavingsByPool", by_pool,
                    money_cols=("actual_spot_cost", "od_counterfactual",
                                "estimated_spot_saving"),
                    formats={"spot_hr": "#,##0.0000", "od_hr": "#,##0.0000",
                             "estimated_spot_node_hours": "#,##0.0"},
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
