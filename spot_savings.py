"""AKS spot savings report: day-by-day cost after spot adoption.

This report answers: "after a cluster added spot node pools, did it actually
save money?" It uses subscription-scope Cost Management queries only.

Story tabs: ReadMe, Scorecard, Recommendations, CoverageRisk, RealizedSavings,
MonthlySavings, SpotTimeline, TopSavers, SavingsByEnv. Evidence appendix: SavingsProjection,
BeforeSpot, AfterSpot, ActualVsProjection, SpotSavingsSummary, FleetDailyTrend,
SpotSavingsDaily, SpotSavingsByPool, PriceReference, RawDailyCost.

Environments are inferred from each cluster/resource-group name: tokens like
-d-, -s-, -r-, -p-, -u- map to dev/sit/dr/prod/uat (see ENV_CODE_MAP in azrep/subs).
The SavingsByEnv tab rolls every cluster up to a prod vs non-prod tier so a
Business Unit owner can read savings by environment class. The full fleet in
scope is kept by default; pass --only-spot-clusters to restrict to clusters with
a current spot node pool.

Usage:
  python spot_savings.py --all
  python spot_savings.py --env dev --spot-trend-days 30 --spot-baseline-days 30
  python spot_savings.py --cluster aks-dev-01 --lookback-days 120
  python spot_savings.py --cluster aks-dev-01 --project-move-od-nodes 3
  python spot_savings.py --all --only-spot-clusters
"""
import datetime as dt
from collections import defaultdict

import pandas as pd

from azrep import excel
from azrep.armextras import vmss_churn_events
from azrep.costmgmt import CostClient, dim_in
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope
from spot_cluster_report import (PM_ORDER, chunks, pool_from_resource_id,
                                 price_reference_rows, retail_price_lookup, vm_family)

RG_CHUNK = 30
DAYS_PER_MONTH = 30.4375
SPOT_THRESHOLD_USD = 0.01
# Floor on the daily-cost lookback so the MonthlySavings tab always has a full
# quarter (2 complete calendar months + the current month-to-date) to roll up.
MONTHLY_LOOKBACK_DAYS = 92
MONTHLY_MONTHS = 3


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
    # Default lookback covers baseline+trend, but never less than a full quarter so
    # the MonthlySavings 3-month roll-up is populated; an explicit --lookback-days wins.
    lookback = lookback_days or max(trend_days + baseline_days, MONTHLY_LOOKBACK_DAYS)
    lookback = max(1, lookback)
    d_from = d_to - dt.timedelta(days=lookback - 1)
    return d_from, d_to


def rg_from_resource_id(resource_id):
    # Recover the resource group from an ARM id (.../resourceGroups/<rg>/...) so
    # we don't need a 3rd grouping dimension on the Cost Management query.
    parts = str(resource_id or "").split("/")
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def attach_resource_rows(frames, rg_map):
    cols = ["cluster_id", "cluster", "subscription", "environment", "location",
            "ResourceGroupName", "ResourceId", "PricingModel", "Period", "Date",
            "Cost", "CostUSD", "Currency", "UsageQuantity"]
    if not frames:
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return pd.DataFrame(columns=cols)
    if "UsageQuantity" not in df.columns:
        df["UsageQuantity"] = 0.0
    if "ResourceGroupName" not in df.columns:
        df["ResourceGroupName"] = df["ResourceId"].map(rg_from_resource_id)
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
            # Cost Management caps grouping at 2 dimensions; the node RG stays a
            # filter (filters don't count) and is recovered from ResourceId in
            # attach_resource_rows. with_quantity uses the 2nd aggregation slot.
            df = cost.query(scope, "AmortizedCost", "Daily",
                            ("ResourceId", "PricingModel"),
                            dim_in("ResourceGroupName", ch), d_from, d_to,
                            with_quantity=True)
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


NONSPOT_PRICING = ("ondemand", "reservation", "savingsplan")


def first_spot_dates(daily, spot_threshold=SPOT_THRESHOLD_USD):
    """Map cluster_id -> first date with Spot compute spend above the threshold (or
    None). Each cluster's own pre-spot baseline window is derived from this, so the
    'before it ran spot' comparison varies cluster to cluster."""
    out = {}
    if daily is None or daily.empty or "Spot" not in daily.columns:
        return out
    for cid, df in daily.groupby("cluster_id", dropna=False):
        sd = df.loc[df["Spot"] > spot_threshold, "Date"]
        out[cid] = parse_date(sd.min()) if not sd.empty else None
    return out


def _effective_rate_table(rows):
    """Actual effective $/node-hour from amortized billing: returns
    {cid: {"by_size": {vm_size: hr}, "blend": hr}} as sum(CostUSD)/sum(UsageQuantity).
    `rows` must be non-spot lines with a positive billed UsageQuantity."""
    out = {}
    if rows is None or rows.empty:
        return out
    by_size = defaultdict(dict)
    g = rows.groupby(["cluster_id", "vm_size"], dropna=False)[["CostUSD", "UsageQuantity"]].sum()
    for (cid, size), r in g.iterrows():
        if size and float(r["UsageQuantity"]) > 0:
            by_size[cid][size] = float(r["CostUSD"]) / float(r["UsageQuantity"])
    gb = rows.groupby("cluster_id", dropna=False)[["CostUSD", "UsageQuantity"]].sum()
    for cid, r in gb.iterrows():
        h = float(r["UsageQuantity"])
        out[cid] = {"by_size": dict(by_size.get(cid, {})),
                    "blend": float(r["CostUSD"]) / h if h > 0 else None}
    return out


def _pick_od_hr(cid, vm_size, location, baseline_tbl, concurrent_tbl, prices):
    """Choose the On-Demand/RI comparison rate for a cluster's spot node-hours,
    preferring the cluster's own pre-spot actual rate, then the actual rate of the
    OD/RI pool it still runs (concurrent), then public retail list price.
    Returns (od_hr, source); (None, 'price_missing') when nothing is derivable."""
    for tbl, src in ((baseline_tbl, "pre_spot_history"),
                     (concurrent_tbl, "concurrent_od_pool")):
        t = tbl.get(cid) or {}
        if vm_size and t.get("by_size", {}).get(vm_size):
            return t["by_size"][vm_size], src
        if t.get("blend"):
            return t["blend"], src + "_blend"
    pr = prices.get((location, vm_size)) if prices else None
    if pr and pr.get("od_hr"):
        return float(pr["od_hr"]), "retail_fallback"
    return None, "price_missing"


def build_spot_estimates_actual(res, pools, prices, first_spot, baseline_days,
                                trend_days, max_date, spot_threshold=SPOT_THRESHOLD_USD):
    """Spot savings priced from ACTUAL amortized billing, not list price.

    For every dollar of actual Spot VMSS spend we read the billed node-hours
    (Cost Management UsageQuantity) and re-price those same hours at the cluster's
    own actual On-Demand/RI effective rate. The rate comes - per VM size where the
    data exists - from the cluster's pre-spot history (the window before its first
    Spot day), falling back to the rate of the OD/RI pool it still runs in the
    trend window, then to public retail. od_hr_source records which was used, so a
    reviewer can see whether a row is real-history, concurrent or list-price based.
    Amortized cost already spreads RI/Savings-Plan, so these rates carry those
    discounts. Per-(cluster, pool, day) rows consumed by annotate_daily_and_summary."""
    cols = ["cluster_id", "cluster", "subscription", "environment", "location", "Date",
            "pool", "vm_size", "ResourceId", "spot_hr", "od_hr", "actual_spot_cost",
            "estimated_spot_node_hours", "od_counterfactual", "estimated_spot_saving",
            "price_status", "od_hr_source"]
    if res is None or res.empty:
        return pd.DataFrame(columns=cols)
    cfg = {(p["cluster_id"], p["pool"]): p for p in pools}
    r = res.copy()
    r["pool"] = r["ResourceId"].map(pool_from_resource_id)
    r = r[r["pool"] != ""].copy()
    if r.empty:
        return pd.DataFrame(columns=cols)
    r["CostUSD"] = pd.to_numeric(r["CostUSD"], errors="coerce").fillna(0.0)
    r["UsageQuantity"] = pd.to_numeric(r.get("UsageQuantity"), errors="coerce").fillna(0.0)
    r["vm_size"] = [cfg.get((cid, pool), {}).get("vm_size", "")
                    for cid, pool in zip(r["cluster_id"], r["pool"])]
    r["_date"] = r["Date"].map(parse_date)
    r["_pm"] = r["PricingModel"].astype(str).str.lower()

    nonspot = r[r["_pm"].isin(NONSPOT_PRICING) & (r["UsageQuantity"] > 0)]
    bl_frames = []
    for cid, grp in nonspot.groupby("cluster_id", dropna=False):
        fs = first_spot.get(cid)
        if not fs:
            continue
        m = (grp["_date"] < fs) & (grp["_date"] >= fs - dt.timedelta(days=baseline_days))
        if m.any():
            bl_frames.append(grp[m])
    baseline_tbl = _effective_rate_table(
        pd.concat(bl_frames, ignore_index=True) if bl_frames else None)
    trend_start = max_date - dt.timedelta(days=trend_days - 1)
    concurrent_tbl = _effective_rate_table(nonspot[nonspot["_date"] >= trend_start])

    spot = r[(r["_pm"] == "spot") & (r["CostUSD"] > 0.0)]
    grouped = spot.groupby(["cluster_id", "cluster", "subscription", "environment",
                            "location", "Date", "pool", "ResourceId"], dropna=False)
    rows = []
    for keys, grp in grouped:
        cid, loc, pool = keys[0], keys[4], keys[6]
        size = cfg.get((cid, pool), {}).get("vm_size", "")
        actual = float(grp["CostUSD"].sum())
        hours = float(grp["UsageQuantity"].sum())
        od_hr, source = _pick_od_hr(cid, size, loc, baseline_tbl, concurrent_tbl, prices)
        pr = prices.get((loc, size)) if prices else None
        if hours <= 0 and pr and pr.get("spot_hr"):   # billing returned no node-hours
            hours = actual / float(pr["spot_hr"])
        if od_hr and hours > 0:
            od_cost = hours * float(od_hr)
            saving = od_cost - actual
            status = "priced"
        else:
            od_cost, saving, status, source = None, None, "price_missing", "price_missing"
        rows.append({
            "cluster_id": cid, "cluster": keys[1], "subscription": keys[2],
            "environment": keys[3], "location": loc, "Date": keys[5], "pool": pool,
            "vm_size": size, "ResourceId": keys[7],
            "spot_hr": actual / hours if hours > 0 else None, "od_hr": od_hr,
            "actual_spot_cost": actual,
            "estimated_spot_node_hours": hours if hours > 0 else None,
            "od_counterfactual": od_cost, "estimated_spot_saving": saving,
            "price_status": status, "od_hr_source": source,
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

    # Per-cluster dominant rate basis (which OD/RI rate priced the headline saving),
    # weighted by counterfactual magnitude over the trend-window priced spot rows.
    basis_by_cluster = {}
    if not estimates.empty and "od_hr_source" in estimates.columns:
        e = estimates[estimates["Date"].map(parse_date) >= trend_start]
        e = e[e["price_status"].astype(str) == "priced"]
        for cid, grp in e.groupby("cluster_id", dropna=False):
            w = grp.groupby("od_hr_source")["od_counterfactual"].sum()
            if not w.empty:
                basis_by_cluster[cid] = str(w.idxmax())

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
            "rate_basis": rate_basis_label(basis_by_cluster.get(cid, "")),
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
            "annualized_projected_usd", "savings_rate_pct",
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
        # Per-cluster savings rate = realized spot saving / OD counterfactual.
        cf_v = float(srow.get("last_%d_od_counterfactual" % trend_days, 0.0)) \
            if hasattr(srow, "get") else 0.0
        sv_v = srow.get("last_%d_estimated_spot_saving" % trend_days) \
            if hasattr(srow, "get") else None
        srow_rate = _safe_div(sv_v, cf_v) if sv_v is not None else None
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
            "annualized_projected_usd": (proj["projected_monthly_saving_usd"] * 12.0)
            if proj["projected_monthly_saving_usd"] is not None else None,
            "savings_rate_pct": srow_rate,
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


# Plain-English badge for FinOps / Business-Unit audiences. The raw `verdict`
# column stays in every detail tab for engineers; these labels are for the
# presentation tabs (headline, standings) so a non-platform reader understands
# the status at a glance.
VERDICT_LABEL = {
    "SAVING": "Verified saving",
    "FLAT": "Inconclusive",
    "COST_UP": "Needs review",
    "PRICE_MISSING": "Pricing gap",
    "NO_SPOT_COST": "Not adopted",
}


def verdict_label(v):
    """Map a raw verdict code to a human badge (falls back to the input)."""
    return VERDICT_LABEL.get(str(v), str(v))


# Human label for the OD/RI rate that priced a cluster's headline saving. The raw
# od_hr_source codes stay in the per-row detail; these labels go on summary tabs so
# a reviewer can see at a glance whether the number is actual-billing or list-price.
RATE_BASIS_LABEL = {
    "pre_spot_history": "Actual (pre-spot history)",
    "pre_spot_history_blend": "Actual (pre-spot history, blended)",
    "concurrent_od_pool": "Actual (current OD/RI pool)",
    "concurrent_od_pool_blend": "Actual (current OD/RI pool, blended)",
    "retail_fallback": "Retail list price (fallback)",
    "price_missing": "No rate available",
    "": "n/a",
}


def rate_basis_label(code):
    return RATE_BASIS_LABEL.get(str(code), str(code))


def _is_actual_basis(code):
    """True when the rate basis came from real billing (history or concurrent),
    not the retail list-price fallback."""
    return str(code).startswith("Actual")


def _safe_div(num, den):
    if den in (0, None) or pd.isna(den) or float(den) == 0.0:
        return None
    return float(num) / float(den)


def top_savers_rows(projection, trend_days, top_n=15):
    """Per-cluster leaderboard by projected monthly saving, with annualized
    savings-rate and a human verdict badge. One row per cluster."""
    cols = ["cluster", "subscription", "environment", "location",
            "current_regular_user_od_nodes", "current_spot_nodes",
            "projected_monthly_saving_usd", "annualized_projected_usd",
            "counterfactual_spot_saving_usd", "savings_rate_pct",
            "status", "verdict"]
    if projection is None or projection.empty:
        return pd.DataFrame(columns=cols)
    df = projection.copy()
    df["annualized_projected_usd"] = df["projected_monthly_saving_usd"] * 12.0
    if "od_counterfactual_usd" in df.columns:
        df["savings_rate_pct"] = _safe_div_vec(
            df["counterfactual_spot_saving_usd"], df["od_counterfactual_usd"])
    else:
        df["savings_rate_pct"] = None
    df["status"] = df["verdict"].map(verdict_label).fillna(df["verdict"])
    df = df.sort_values("projected_monthly_saving_usd", ascending=False,
                        na_position="last").head(top_n).reset_index(drop=True)
    return df[cols]


def savings_by_env_rows(projection, trend_days):
    """Roll up spot savings by environment tier (prod vs non-prod), then by the
    fine-grained environment each cluster was inferred to (e.g. prod, dev, sit,
    dr, uat from -p-/-d-/-s-/-r-/-u- name tokens). One row per environment.

    Prod/non-prod is decided via azrep.subs.is_prod() on the resolved
    environment, so a Business Unit owner can read a single hero line per class.
    """
    cols = ["tier", "environment", "clusters", "verified_savers",
            "actual_spot_cost_usd", "od_counterfactual_usd",
            "counterfactual_spot_saving_usd", "projected_monthly_saving_usd",
            "annualized_projected_usd", "savings_rate_pct", "top_verdict"]
    if projection is None or projection.empty:
        return pd.DataFrame(columns=cols)
    df = projection.copy()
    df["tier"] = ["prod" if is_prod(e) else "non-prod" for e in df["environment"]]
    num_cols = ["actual_spot_cost_usd", "od_counterfactual_usd",
                "counterfactual_spot_saving_usd", "projected_monthly_saving_usd",
                "annualized_projected_usd"]
    for c in num_cols:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    rows = []
    for (tier, env), grp in df.groupby(["tier", "environment"], dropna=False):
        savers = int((grp["verdict"] == "SAVING").sum())
        cf = float(grp["od_counterfactual_usd"].fillna(0.0).sum())
        sv = float(grp["counterfactual_spot_saving_usd"].fillna(0.0).sum())
        rows.append({
            "tier": tier,
            "environment": env,
            "clusters": int(len(grp)),
            "verified_savers": savers,
            "actual_spot_cost_usd": float(grp["actual_spot_cost_usd"].fillna(0.0).sum()),
            "od_counterfactual_usd": cf,
            "counterfactual_spot_saving_usd": sv,
            "projected_monthly_saving_usd":
                float(grp["projected_monthly_saving_usd"].fillna(0.0).sum()),
            "annualized_projected_usd":
                float(grp["annualized_projected_usd"].fillna(0.0).sum()),
            "savings_rate_pct": _safe_div(sv, cf),
            "top_verdict": grp["verdict"].mode().iat[0] if not grp["verdict"].mode().empty else "",
        })
    out = pd.DataFrame(rows, columns=cols)
    if not out.empty:
        tier_rank = {"prod": 0, "non-prod": 1}
        out["_rank"] = out["tier"].map(tier_rank).fillna(9)
        out = out.sort_values(["_rank", "projected_monthly_saving_usd"],
                              ascending=[True, False]).drop(columns=["_rank"])
        out = out.reset_index(drop=True)
    return out


def _safe_div_vec(num, den):
    """Vectorized safe-division returning NaN where den is 0/NaN (not inf)."""
    num = pd.to_numeric(num, errors="coerce")
    den = pd.to_numeric(den, errors="coerce")
    out = pd.Series([None] * len(num), index=num.index, dtype=object)
    mask = den.notna() & (den != 0)
    out.loc[mask] = num.loc[mask] / den.loc[mask]
    return out


def timeline_rows(daily, projection, project_days):
    """Two-series timeline for a clean before/after savings visual:
      - 'Actual total USD': actual end-to-end cost per day (fleet sum)
      - 'OD counterfactual USD': what the same day would have cost if spot
        spend had been on-demand (total - spot + od_counterfactual)
      - 'Cumulative realized saving USD': running sum of estimated_spot_saving
        from first spot spend (may stop rising once spot is steady state)
      - 'Modeled future total USD': trailing-mean baseline minus projected
        daily saving, extended forward project_days.
    All series share one Date axis so the charts line up vertically.
    """
    cols = ["Date", "Actual total USD", "OD counterfactual USD",
            "Cumulative realized saving USD", "Modeled future total USD"]
    if daily is None or daily.empty:
        return pd.DataFrame(columns=cols)
    hist = daily.groupby("Date", dropna=False).agg({
        "Total (USD)": "sum",
        "actual_spot_cost": "sum",
        "od_counterfactual": "sum",
        "estimated_spot_saving": "sum",
    }).reset_index().sort_values("Date").reset_index(drop=True)
    rows = []
    cum = 0.0
    for _idx, r in hist.iterrows():
        saving = float(r["estimated_spot_saving"]) if pd.notna(r["estimated_spot_saving"]) else 0.0
        has_counter = pd.notna(r["od_counterfactual"]) and float(r["od_counterfactual"]) > 0
        cum += saving if has_counter else 0.0
        total = float(r["Total (USD)"])
        spot = float(r["actual_spot_cost"]) if pd.notna(r["actual_spot_cost"]) else 0.0
        counter = float(r["od_counterfactual"]) if pd.notna(r["od_counterfactual"]) else 0.0
        rows.append({
            "Date": r["Date"],
            "Actual total USD": total,
            "OD counterfactual USD": (total - spot + counter) if has_counter else None,
            "Cumulative realized saving USD": cum if has_counter else None,
            "Modeled future total USD": None,
        })
    max_date = parse_date(hist["Date"].max())
    trailing = hist.tail(min(30, len(hist)))
    base = float(trailing["Total (USD)"].mean()) if len(trailing) else 0.0
    daily_saving = 0.0
    if project_days and projection is not None and not projection.empty:
        daily_saving = float(projection["projected_saving_usd"].fillna(0.0).sum()) / project_days
    rows.append({
        "Date": max_date.strftime("%Y-%m-%d"),
        "Actual total USD": None, "OD counterfactual USD": None,
        "Cumulative realized saving USD": None,
        "Modeled future total USD": base,
    })
    for i in range(1, project_days + 1):
        day = max_date + dt.timedelta(days=i)
        rows.append({
            "Date": day.strftime("%Y-%m-%d"),
            "Actual total USD": None, "OD counterfactual USD": None,
            "Cumulative realized saving USD": None,
            "Modeled future total USD": max(0.0, base - daily_saving),
        })
    return pd.DataFrame(rows, columns=cols)


def _fmt_money_compact(v):
    """$1.2M / $12.3k / $540 for big-number cards (None/NaN -> 'n/a')."""
    if v is None or pd.isna(v):
        return "n/a"
    v = float(v)
    sign, a = ("-" if v < 0 else ""), abs(v)
    if a >= 1e6:
        return "%s$%.1fM" % (sign, a / 1e6)
    if a >= 1e3:
        return "%s$%.1fk" % (sign, a / 1e3)
    return "%s$%.0f" % (sign, a)


def _fmt_pct(frac):
    """0.68 -> '68%' (None/NaN -> 'n/a')."""
    if frac is None or pd.isna(frac):
        return "n/a"
    return "%.0f%%" % (float(frac) * 100.0)


def _window_slice(daily, trend_days):
    """Rows within the trailing trend_days of the daily frame (adds a throwaway _d)."""
    if daily is None or daily.empty:
        return daily
    max_date = parse_date(daily["Date"].max())
    start = max_date - dt.timedelta(days=trend_days - 1)
    d = daily.copy()
    d["_d"] = d["Date"].map(parse_date)
    return d[d["_d"] >= start]


SPOT_SHARE_HIGH = 0.50
SPOT_SHARE_MED = 0.25


def _risk_band(is_prod_env, spot_share, single_pool, single_family, price_capped,
               no_od_fallback):
    """Transparent additive reliability score -> HIGH/MED/LOW band + reason string.
    Caller passes '-' band itself for clusters with no spot exposure."""
    score, reasons = 0, []
    if is_prod_env:
        score += 2
        reasons.append("prod on spot")
    if spot_share >= SPOT_SHARE_HIGH:
        score += 2
        reasons.append("spot >=50% of compute")
    elif spot_share >= SPOT_SHARE_MED:
        score += 1
        reasons.append("spot >=25% of compute")
    if single_pool:
        score += 1
        reasons.append("single spot pool (drains together)")
    if single_family:
        score += 1
        reasons.append("single VM family")
    if price_capped:
        score += 1
        reasons.append("price-capped spot")
    if no_od_fallback:
        score += 1
        reasons.append("no on-demand fallback")
    band = "HIGH" if score >= 4 else "MED" if score >= 2 else "LOW"
    return band, "; ".join(reasons)


def coverage_risk_rows(clusters, pools, daily, churn_by_cluster, trend_days):
    """Per-cluster spot exposure + reliability risk. Structural signals are reliable
    (no extra calls); vmss_churn_approx is a best-effort eviction proxy (mixes real
    evictions with autoscale-down, not separable at Reader scope). risk_band is an
    additive, explainable score - see _risk_band."""
    cols = ["cluster_id", "cluster", "subscription", "environment", "is_prod",
            "spot_share_compute", "spot_nodes", "total_nodes", "spot_node_pct",
            "spot_pools", "spot_vm_families", "spot_zones", "price_capped_spot",
            "vmss_churn_approx", "risk_band", "risk_reasons"]
    win = _window_slice(daily, trend_days)
    rows = []
    for c in clusters:
        cid = c["id"]
        ps = [p for p in pools if p["cluster_id"] == cid]
        spot = [p for p in ps if str(p.get("priority", "")).lower() == "spot"]
        od_user = [p for p in ps if str(p.get("priority", "")).lower() != "spot"
                   and str(p.get("mode", "")).lower() == "user"
                   and int(p.get("count") or 0) > 0]
        spot_nodes = sum(int(p.get("count") or 0) for p in spot)
        total_nodes = sum(int(p.get("count") or 0) for p in ps)
        cd = win[win["cluster_id"] == cid] if win is not None and not win.empty else None
        spot_spend = float(cd["Spot"].sum()) if cd is not None and len(cd) else 0.0
        compute_spend = float(cd["Compute total (USD)"].sum()) if cd is not None and len(cd) else 0.0
        spot_share = spot_spend / compute_spend if compute_spend else 0.0
        families = {vm_family(p["vm_size"]) for p in spot if p.get("vm_size")}
        zones = {z.strip() for p in spot for z in str(p.get("zones") or "").split(",")
                 if z.strip()}
        price_capped = any(p.get("spot_max_price") not in (-1, "-1", None, "") for p in spot)
        prod = is_prod(c["environment"])
        churn = (churn_by_cluster or {}).get(cid)
        churn_count = churn.get("count") if isinstance(churn, dict) else None
        if spot_nodes <= 0 and spot_spend <= 0:
            band, reasons = "-", "no spot exposure"
        else:
            band, reasons = _risk_band(
                prod, spot_share, single_pool=(len(spot) == 1),
                single_family=(len(families) < 2), price_capped=price_capped,
                no_od_fallback=(not od_user))
        rows.append({
            "cluster_id": cid, "cluster": c["cluster"],
            "subscription": c["subscription"], "environment": c["environment"],
            "is_prod": "yes" if prod else "no", "spot_share_compute": spot_share,
            "spot_nodes": spot_nodes, "total_nodes": total_nodes,
            "spot_node_pct": (spot_nodes / total_nodes) if total_nodes else 0.0,
            "spot_pools": len(spot), "spot_vm_families": len(families),
            "spot_zones": len(zones),
            "price_capped_spot": "yes" if price_capped else "no",
            "vmss_churn_approx": churn_count, "risk_band": band, "risk_reasons": reasons,
        })
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        order = {"HIGH": 0, "MED": 1, "LOW": 2, "-": 3}
        df["_r"] = df["risk_band"].map(order).fillna(9)
        df = df.sort_values(["_r", "spot_share_compute"], ascending=[True, False]) \
               .drop(columns=["_r"]).reset_index(drop=True)
    return df


def recommendation_rows(clusters, pools, prices, risk_by_cluster, trend_days):
    """Ranked, actionable OD->spot moves: one row per eligible regular Linux User OD
    pool, sized by retail saving. Reader access cannot see workload suitability
    (replicas/PDBs/statefulness), so every row carries an explicit verify flag and
    the cluster's reliability band - a candidate list, not an instruction to move."""
    cols = ["rank", "cluster", "environment", "pool", "vm_size", "current_od_nodes",
            "est_monthly_saving_usd", "est_annual_saving_usd", "cluster_risk_band",
            "verify_before_move", "basis"]
    rows = []
    for c in clusters:
        for p, od_hr, spot_hr in _projectable_pools(pools, prices, c["id"]):
            nodes = int(p.get("count") or 0)
            monthly = nodes * (od_hr - spot_hr) * 24.0 * DAYS_PER_MONTH
            rows.append({
                "cluster": c["cluster"], "environment": c["environment"],
                "pool": p.get("pool"), "vm_size": p.get("vm_size"),
                "current_od_nodes": nodes, "est_monthly_saving_usd": monthly,
                "est_annual_saving_usd": monthly * 12.0,
                "cluster_risk_band": (risk_by_cluster or {}).get(c["id"], ""),
                "verify_before_move": "replicas / PDB / stateful?",
                "basis": "retail OD-spot delta x current nodes (suitability NOT verified)",
            })
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows).sort_values(
        "est_monthly_saving_usd", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    return df.reindex(columns=cols)


def realized_savings_rows(summary, trend_days):
    """Slim fact-vs-model per cluster for the trust story: invoiced Spot spend is a
    hard billed fact; the OD counterfactual and saving are priced from the cluster's
    actual amortized OD/RI rate (rate_basis shows which - actual history/current pool
    vs the retail list-price fallback)."""
    cols = ["cluster", "environment", "invoiced_spot_fact_usd",
            "od_counterfactual_model_usd", "est_saving_model_usd",
            "savings_rate_pct", "rate_basis", "status", "verdict"]
    if summary is None or summary.empty:
        return pd.DataFrame(columns=cols)
    ac = "last_%d_actual_spot_cost" % trend_days
    cf = "last_%d_od_counterfactual" % trend_days
    sv = "last_%d_estimated_spot_saving" % trend_days
    s = summary
    out = pd.DataFrame({
        "cluster": s["cluster"], "environment": s["environment"],
        "invoiced_spot_fact_usd": s[ac], "od_counterfactual_model_usd": s[cf],
        "est_saving_model_usd": s[sv], "savings_rate_pct": _safe_div_vec(s[sv], s[cf]),
        "rate_basis": s["rate_basis"] if "rate_basis" in s else "",
        "status": s["verdict"].map(verdict_label).fillna(s["verdict"]),
        "verdict": s["verdict"],
    })
    return out.sort_values("est_saving_model_usd", ascending=False,
                           na_position="last").reset_index(drop=True)


def monthly_savings_rows(daily, today, months=MONTHLY_MONTHS,
                         spot_threshold=SPOT_THRESHOLD_USD):
    """Calendar-month roll-up of spot savings for the last `months` months - a fleet
    total per month followed by per-cluster rows. Spot spend is the hard billed fact;
    od_counterfactual / saving are retail-rate estimates (0 when prices are unavailable).
    The current calendar month is month-to-date (partial). savings_from_spot_pool is
    'Yes' only when that month actually carried Spot VMSS spend, so a 'No (no spot spend)'
    month makes plain that its (zero) saving is not attributable to a spot node pool."""
    cols = ["month", "cluster", "subscription", "environment", "month_status",
            "spot_cost_usd", "od_counterfactual_usd", "estimated_saving_usd",
            "savings_rate_pct", "total_cost_usd", "savings_from_spot_pool"]
    if daily is None or daily.empty:
        return pd.DataFrame(columns=cols)
    d = daily.copy()
    d["month"] = d["Date"].str[:7]
    keep = sorted(d["month"].unique())[-months:]   # last N calendar months in window
    d = d[d["month"].isin(keep)]
    cur_month = today.strftime("%Y-%m")
    agg = {"actual_spot_cost": "sum", "od_counterfactual": "sum",
           "estimated_spot_saving": "sum", "Total (USD)": "sum"}
    per = d.groupby(["cluster", "subscription", "environment", "month"],
                    dropna=False).agg(agg).reset_index()
    fleet = d.groupby("month", dropna=False).agg(agg).reset_index()
    fleet["cluster"], fleet["subscription"], fleet["environment"] = \
        "(all clusters)", "", "(fleet)"

    def _emit(frame):
        out = []
        for _, r in frame.iterrows():
            spot, cf, sv = (float(r["actual_spot_cost"]), float(r["od_counterfactual"]),
                            float(r["estimated_spot_saving"]))
            out.append({
                "month": r["month"], "cluster": r["cluster"],
                "subscription": r["subscription"], "environment": r["environment"],
                "month_status": "MTD (partial)" if r["month"] == cur_month else "full",
                "spot_cost_usd": spot, "od_counterfactual_usd": cf,
                "estimated_saving_usd": sv, "savings_rate_pct": _safe_div(sv, cf),
                "total_cost_usd": float(r["Total (USD)"]),
                "savings_from_spot_pool": "Yes" if spot > spot_threshold
                else "No (no spot spend)",
            })
        return out

    rows = _emit(fleet.sort_values("month"))
    rows += _emit(per.sort_values(["cluster", "month"]))
    return pd.DataFrame(rows, columns=cols)


def before_after_rows(daily, clusters, pools, trend_days):
    """Per-environment (BU) before/after monthly cost - the management slide.

    Uses the SAME defensible counterfactual as the rest of the report: 'after' is
    what the fleet actually pays now; 'before' re-prices ONLY the Spot VMSS hours
    at the on-demand retail rate (everything else identical), so the delta is the
    spot saving attributable purely to the spot decision, while the two totals give
    whole-bill context. Costs are run-rated to a calendar month over the trailing
    trend window. One row per environment, sorted by saving; the chart is drawn over
    these rows and a fleet total is appended with add_total_row."""
    cols = ["environment", "clusters", "spot_clusters",
            "monthly_on_demand_cost_usd", "monthly_actual_cost_usd",
            "monthly_saving_usd", "saving_pct", "annualized_saving_usd", "status"]
    win = _window_slice(daily, trend_days)
    if win is None or win.empty:
        return pd.DataFrame(columns=cols)
    spot_ids = {p["cluster_id"] for p in pools
                if str(p.get("priority", "")).lower() == "spot"}
    cl_by_env, spot_by_env = defaultdict(set), defaultdict(set)
    for c in clusters:
        env = c["environment"] or "(unknown)"
        cl_by_env[env].add(c["id"])
        if c["id"] in spot_ids:
            spot_by_env[env].add(c["id"])

    w = win.copy()
    w["environment"] = w["environment"].fillna("").replace("", "(unknown)")
    days = w.groupby("environment")["Date"].nunique().to_dict()
    agg = w.groupby("environment").agg(
        total=("Total (USD)", "sum"),
        spot=("actual_spot_cost", "sum"),
        od_cf=("od_counterfactual", "sum"),
        saving=("estimated_spot_saving", "sum")).reset_index()
    rows = []
    for _idx, r in agg.iterrows():
        env = r["environment"]
        f = DAYS_PER_MONTH / float(days.get(env, 0) or 1)   # daily -> monthly run-rate
        after_mo = float(r["total"]) * f                     # actual, with spot
        # saving is the priced counterfactual delta (>=0); 'before' adds it back so
        # before = what the same workload would cost with those spot hours on-demand.
        saving_mo = float(r["saving"]) * f
        before_mo = after_mo + saving_mo
        priced = float(r["od_cf"]) > 0.0
        n_spot = len(spot_by_env.get(env, ()))
        if n_spot == 0 and float(r["spot"]) <= 0.0:
            status = "No spot adopted"
        elif not priced:
            status = "Pricing gap"
        elif saving_mo > 0:
            status = "Verified saving"
        else:
            status = "Inconclusive"
        rows.append({
            "environment": env,
            "clusters": len(cl_by_env.get(env, ())),
            "spot_clusters": n_spot,
            "monthly_on_demand_cost_usd": before_mo,
            "monthly_actual_cost_usd": after_mo,
            "monthly_saving_usd": saving_mo,
            "saving_pct": _safe_div(saving_mo, before_mo),
            "annualized_saving_usd": saving_mo * 12.0,
            "status": status,
        })
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df = df.sort_values("monthly_saving_usd", ascending=False,
                            na_position="last").reset_index(drop=True)
    return df


def scorecard_cards(summary, projection, daily, estimates, coverage_risk, trend_days):
    """Exec KPI cards (list of dicts for excel.add_scorecard). Separates the hard
    billed fact (invoiced Spot spend) from the avoided-cost estimate (priced from
    each cluster's actual amortized OD/RI rate), surfaces coverage, the achieved-vs-
    achievable spot-discount gap and how much of the saving is on a real-billing
    basis, and closes with the 3-state adoption read (savers / pricing gap / not
    adopted)."""
    s = summary if (summary is not None and not summary.empty) else pd.DataFrame()
    p = projection if (projection is not None and not projection.empty) else pd.DataFrame()
    cr = coverage_risk if (coverage_risk is not None and not coverage_risk.empty) else pd.DataFrame()
    ac = "last_%d_actual_spot_cost" % trend_days
    cf = "last_%d_od_counterfactual" % trend_days
    sv = "last_%d_estimated_spot_saving" % trend_days

    n_spot = int(len(s))
    n_saving = int((s["verdict"] == "SAVING").sum()) if not s.empty else 0
    n_gap = int((s["verdict"] == "PRICE_MISSING").sum()) if not s.empty else 0
    n_not = int((s["verdict"] == "NO_SPOT_COST").sum()) if not s.empty else 0
    invoiced = float(s[ac].fillna(0.0).sum()) if not s.empty else 0.0
    realized = float(s[sv].fillna(0.0).sum()) if not s.empty else 0.0
    counter_od = float(s[cf].fillna(0.0).sum()) if not s.empty else 0.0
    proj_monthly = float(p["projected_monthly_saving_usd"].fillna(0.0).sum()) if not p.empty else 0.0

    days = max(1, int(trend_days))
    inv_mo = invoiced / days * DAYS_PER_MONTH
    saved_mo = realized / days * DAYS_PER_MONTH
    realized_rate = _safe_div(realized, counter_od)

    achievable = None
    if estimates is not None and not estimates.empty and "price_status" in estimates:
        e = estimates[estimates["price_status"] == "priced"].copy()
        if len(e):
            od = pd.to_numeric(e["od_hr"], errors="coerce")
            sp = pd.to_numeric(e["spot_hr"], errors="coerce")
            hrs = pd.to_numeric(e["estimated_spot_node_hours"], errors="coerce").fillna(0.0)
            valid = od > 0
            if valid.any():
                disc = ((od - sp) / od)[valid]
                h = hrs[valid]
                wsum = float(h.sum())
                achievable = float((disc * h).sum() / wsum) if wsum else float(disc.mean())

    win = _window_slice(daily, trend_days)
    spot_spend = float(win["Spot"].sum()) if win is not None and not win.empty else 0.0
    compute_spend = float(win["Compute total (USD)"].sum()) if win is not None and not win.empty else 0.0
    coverage = spot_spend / compute_spend if compute_spend else 0.0

    prod_on_spot = 0
    if not cr.empty:
        prod_on_spot = int(((cr["is_prod"] == "yes") & (cr["spot_nodes"] > 0)).sum())

    # How much of the priced saving rests on actual billing vs the retail fallback.
    n_priced, n_actual = 0, 0
    if not s.empty and "rate_basis" in s:
        priced_mask = s[cf].fillna(0.0) > 0
        n_priced = int(priced_mask.sum())
        n_actual = int(s.loc[priced_mask, "rate_basis"].map(_is_actual_basis).sum())
    basis_rag = "neutral" if not n_priced else ("good" if n_actual == n_priced else "warn")

    if realized_rate is not None and achievable:
        rate_rag = "good" if realized_rate >= 0.8 * achievable else "warn"
    else:
        rate_rag = "neutral"

    return [
        {"label": "Invoiced Spot spend", "value": _fmt_money_compact(inv_mo) + "/mo",
         "caption": "actual billed (fact) - last %d-day run-rate" % days, "rag": "neutral"},
        {"label": "Est. avoided cost", "value": _fmt_money_compact(saved_mo) + "/mo",
         "caption": "actual-rate counterfactual - approx %s/yr" % _fmt_money_compact(saved_mo * 12),
         "rag": "good" if saved_mo > 0 else "neutral"},
        {"label": "Actual-rate basis", "value": "%d / %d" % (n_actual, n_priced),
         "caption": "saving priced from real billing vs retail fallback", "rag": basis_rag},
        {"label": "Verified savers", "value": "%d / %d" % (n_saving, n_spot),
         "caption": "clusters with a priced net saving", "rag": "good" if n_saving else "warn"},
        {"label": "Spot coverage", "value": _fmt_pct(coverage),
         "caption": "spot / compute spend (last %d days)" % days, "rag": "neutral"},
        {"label": "Savings rate vs achievable",
         "value": "%s / %s" % (_fmt_pct(realized_rate), _fmt_pct(achievable)),
         "caption": "realized vs retail spot discount", "rag": rate_rag},
        {"label": "Untapped saving", "value": _fmt_money_compact(proj_monthly) + "/mo",
         "caption": "if eligible OD pools move to spot", "rag": "warn" if proj_monthly > 0 else "good"},
        {"label": "Prod on spot", "value": str(prod_on_spot),
         "caption": "prod clusters leaning on spot - review", "rag": "bad" if prod_on_spot else "good"},
        {"label": "Pricing gap", "value": str(n_gap),
         "caption": "adopted but retail price unavailable", "rag": "warn" if n_gap else "neutral"},
        {"label": "Not adopted", "value": str(n_not),
         "caption": "clusters with no spot spend", "rag": "neutral"},
    ]


def main(argv=None):
    p = base_parser("AKS spot savings after adoption")
    p.add_argument("--spot-trend-days", type=int, default=30,
                   help="trailing days to use for the current spot-savings verdict")
    p.add_argument("--spot-baseline-days", type=int, default=30,
                   help="days immediately before first observed spot cost for total-cost context")
    p.add_argument("--lookback-days", type=int, default=0,
                   help="daily cost lookback; default is max(baseline + trend, 92) so the "
                        "MonthlySavings 3-month roll-up is fully populated")
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
    p.add_argument("--only-spot-clusters", action="store_true",
                   help="keep only clusters that currently have a spot node pool in ARG "
                        "(default keeps the full fleet in scope; environment is inferred from "
                        "the cluster/RG name using -p-/-d-/-r- tokens)")
    p.add_argument("--nonprod-spot", action="store_true",
                   help="management shortcut: restrict to NON-PROD clusters that currently "
                        "have a spot node pool (equivalent to --nonprod --only-spot-clusters); "
                        "best paired with the BeforeAfterByEnv tab for a BU savings story")
    p.add_argument("--include-all-clusters", action="store_true",
                   help="DEPRECATED no-op kept for backward compatibility; the full fleet is "
                        "now kept by default. Use --only-spot-clusters for the opposite filter.")
    p.add_argument("--no-eviction-scan", action="store_true",
                   help="skip the per-spot-cluster node-RG Activity Log scan that feeds the "
                        "CoverageRisk vmss_churn_approx column (eviction+autoscale proxy)")
    args = p.parse_args(argv)
    # --nonprod-spot is sugar for --nonprod --only-spot-clusters: forcing nonprod
    # before pick_scope also satisfies should_prompt_scope so it never prompts.
    if args.nonprod_spot:
        args.nonprod = True

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
    dropped_total = 0
    only_spot = args.only_spot_clusters or args.nonprod_spot
    if only_spot:
        spot_cluster_ids = {p["cluster_id"] for p in pools
                            if str(p.get("priority", "")).lower() == "spot"}
        kept = [c for c in clusters if c["id"] in spot_cluster_ids]
        dropped_total = len(clusters) - len(kept)
        clusters = kept
        pools = [p for p in pools if p["cluster_id"] in spot_cluster_ids]
        log("Spot-only filter: %d cluster(s) with a current spot node pool kept, "
            "%d without dropped" % (len(clusters), dropped_total))
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
    first_spot = first_spot_dates(daily, args.spot_threshold_usd)
    max_date = parse_date(daily["Date"].max()) if not daily.empty else today
    estimates = build_spot_estimates_actual(
        cost["res"], pools, prices, first_spot, args.spot_baseline_days,
        args.spot_trend_days, max_date, args.spot_threshold_usd)
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
    timeline = timeline_rows(daily, projection, args.project_days)
    top_savers = top_savers_rows(projection, args.spot_trend_days)
    by_env = savings_by_env_rows(projection, args.spot_trend_days)
    fleet_trend = fleet_trend_rows(daily)
    by_pool = pool_savings_rows(estimates, args.spot_trend_days)
    spot_cluster_ids_now = {p["cluster_id"] for p in pools
                            if str(p.get("priority", "")).lower() == "spot"}
    churn_by_cluster = {}
    if not args.no_eviction_scan:
        scan = [c for c in clusters if c["id"] in spot_cluster_ids_now
                and c.get("node_resource_group")]
        if scan:
            log("Scanning node-RG Activity Log for VMSS churn (eviction proxy) on "
                "%d spot cluster(s)..." % len(scan))
        for c in scan:
            try:  # a churn-scan hiccup must never kill the savings report
                churn_by_cluster[c["id"]] = vmss_churn_events(
                    session, c["subscription_id"], c["node_resource_group"],
                    args.spot_trend_days)
            except Exception as e:
                log("  VMSS churn scan failed for %s: %s" % (c["cluster"], e))
    coverage_risk = coverage_risk_rows(clusters, pools, daily, churn_by_cluster,
                                       args.spot_trend_days)
    risk_by_cluster = (dict(zip(coverage_risk["cluster_id"], coverage_risk["risk_band"]))
                       if not coverage_risk.empty else {})
    recommendations = recommendation_rows(clusters, pools, prices, risk_by_cluster,
                                          args.spot_trend_days)
    realized = realized_savings_rows(summary, args.spot_trend_days)
    monthly = monthly_savings_rows(daily, today)
    before_after = before_after_rows(daily, clusters, pools, args.spot_trend_days)
    cards = scorecard_cards(summary, projection, daily, estimates, coverage_risk,
                            args.spot_trend_days)
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
        "Clusters in scope: %d   Spot-pool clusters in scope: %d   Cost Management "
        "calls: %d   Trailing days trimmed: %d" %
        (len(clusters), len(clusters) - dropped_total, cost["calls"], args.trim_days),
        "Scope filter: %s   Environment tier is inferred from the cluster/RG name "
        "(-d-/-s-/-r-/-p-/-u- -> dev/sit/dr/prod/uat) and rolled up to prod vs "
        "non-prod in the BeforeAfterByEnv and SavingsByEnv tabs." %
        ("non-prod spot clusters only (--nonprod-spot)" if args.nonprod_spot
         else "spot-clusters only (--only-spot-clusters)" if only_spot
         else "full fleet in scope"),
        "",
        "WHAT THIS REPORT SHOWS",
        "This workbook answers a single FinOps question for business owners: after our",
        "AKS clusters added Azure Spot VMSS node pools, how much money have we saved,",
        "how much more could we save, and what reliability risk are we carrying? Read the",
        "tabs left to right - the green story tabs come first, then the evidence appendix.",
        "",
        "STORY (read these):",
        "  1. Scorecard       - the one exec page: KPI cards separating the hard billed",
        "     fact (invoiced Spot spend) from the avoided-cost estimate (priced at each",
        "     cluster's actual OD/RI rate), plus how much of the saving is on a real-",
        "     billing basis, coverage %, achieved-vs-achievable discount, untapped runway,",
        "     prod-on-spot risk, and the 3-state adoption read (savers / gap / not adopted).",
        "  2. BeforeAfterByEnv- the BU slide: per-environment monthly cost BEFORE spot",
        "     (priced at the actual OD/RI rate) vs AFTER (what we pay now), the saving and",
        "     saving %, with a side-by-side bar chart and a fleet total. The delta uses the",
        "     same actual-rate counterfactual as the rest of the report (spot isolated).",
        "  3. Recommendations - ranked OD->spot moves sized by retail saving. CANDIDATES",
        "     ONLY: Reader access cannot see replicas/PDBs/statefulness, so every row says",
        "     verify_before_move and carries the cluster reliability band - never move a",
        "     pool on this list without checking the workload first.",
        "  4. CoverageRisk    - per-cluster spot exposure + reliability band. Structural",
        "     signals are exact; vmss_churn_approx is a best-effort eviction proxy (it",
        "     mixes real evictions with autoscale-down - not separable at Reader scope).",
        "  5. RealizedSavings - per-cluster fact-vs-model: invoiced Spot $ (billed fact)",
        "     beside the OD counterfactual and saving (retail estimates), with a badge.",
        "  6. MonthlySavings  - last 3 calendar months (fleet total then per-cluster):",
        "     spot spend, counterfactual, saving and savings rate per month. The current",
        "     month is month-to-date (partial); savings_from_spot_pool flags Yes only when",
        "     that month actually carried Spot VMSS spend (else 'No (no spot spend)').",
        "",
        "EVIDENCE (appendix): SpotTimeline (actual vs counterfactual + cumulative saving),",
        "TopSavers / SavingsByEnv standings, SavingsProjection runway, BeforeSpot/AfterSpot",
        "due-diligence, SpotSavingsSummary technical detail, FleetDailyTrend, the daily/by-",
        "pool tables, and PriceReference/RawDailyCost.",
        "",
        "HOW WE COUNT SAVINGS (grounded in actual billing)",
        "We do NOT compare whole-cluster cost before vs after - that moves when",
        "workload, autoscaling, reservations, disks or cluster fees change, so it would",
        "pretend spot saved money it didn't. Instead, for every dollar actually spent on",
        "Spot VMSS we read the billed node-hours (Cost Management amortized usage) and",
        "re-price those same hours at the cluster's own ACTUAL On-Demand/RI rate. That",
        "rate is back-tracked from the cluster's real amortized history - per VM size",
        "where the data exists - so it reflects negotiated/RI/Savings-Plan pricing, not",
        "list price, and varies cluster to cluster. The difference is the saving",
        "attributable purely to spot ('avoided cost' method, isolating the spot decision).",
        "",
        "Each cluster's OD/RI rate is chosen by this ladder (rate_basis shows which):",
        "  1. Pre-spot history  - the cluster's actual $/node-hour in the window BEFORE",
        "     its first Spot day (the truest 'before it ran spot' comparison).",
        "  2. Current OD/RI pool - the actual rate of the OD/RI pool it still runs in the",
        "     trend window, when pre-spot history is too thin or out of retention.",
        "  3. Retail list price - last-resort fallback when neither actual rate exists.",
        "Invoiced Spot spend is a hard billed fact; the OD/RI rate is actual where",
        "rate_basis says 'Actual', otherwise a retail estimate. Status badges: Verified",
        "saving / Inconclusive / Needs review / Pricing gap / Not adopted.",
        "",
        "CONFIDENCE NOTES",
        "- First spot adoption is the first day Cost Management reports Spot spend",
        "  above --spot-threshold-usd; ARG only exposes current node-pool state, so this",
        "  is a cost-observed date, not the ARM agent-pool creation timestamp.",
        "- current_nodes_now is current ARG inventory; historical node counts are not",
        "  available from subscription-reader APIs. avg_node_equiv is a cost/rate",
        "  annotation where retail rates are available.",
        "- Whole-cluster before/after totals appear in SavingsProjection as context only",
        "  (labelled workload-confounded); they are NOT the spot-savings verdict.",
        "- CoverageRisk vmss_churn_approx counts node-RG VMSS delete/deallocate events from",
        "  the Activity Log over the trend window - a best-effort eviction proxy that also",
        "  includes autoscale-down; pass --no-eviction-scan to disable it.",
        "- Recent Cost Management data can lag, so the newest --trim-days are excluded",
        "  by default. Currency: USD for actual CostUSD; retail rates use --currency.",
    ])
    excel.add_scorecard(wb, "Scorecard", cards, section="summary",
                        title="Spot savings at a glance (fact vs model)")
    ws_ba = excel.add_table(wb, "BeforeAfterByEnv", before_after, section="summary",
                            money_cols=("monthly_on_demand_cost_usd",
                                        "monthly_actual_cost_usd",
                                        "monthly_saving_usd", "annualized_saving_usd"),
                            pct_cols=("saving_pct",),
                            int_cols=("clusters", "spot_clusters"),
                            fail_cols=("status",), fail_values=(),
                            warn_values=("Pricing gap", "No spot adopted"),
                            max_width=70)
    if len(before_after) > 1:
        od_col = list(before_after.columns).index("monthly_on_demand_cost_usd") + 1
        act_col = list(before_after.columns).index("monthly_actual_cost_usd") + 1
        excel.add_grouped_bar_chart(
            ws_ba, "Monthly cost by environment: on-demand (before) vs actual with spot (after)",
            len(before_after) + 1, od_col, act_col, "K2", y_title="USD / month")
    if not before_after.empty:
        excel.add_total_row(ws_ba, before_after,
                            ("clusters", "spot_clusters", "monthly_on_demand_cost_usd",
                             "monthly_actual_cost_usd", "monthly_saving_usd",
                             "annualized_saving_usd"))
    excel.add_table(wb, "Recommendations", recommendations, section="summary",
                    money_cols=("est_monthly_saving_usd", "est_annual_saving_usd"),
                    int_cols=("rank", "current_od_nodes"),
                    fail_cols=("cluster_risk_band",), fail_values=("HIGH",),
                    warn_values=("MED",), max_width=70)
    excel.add_table(wb, "CoverageRisk",
                    coverage_risk.drop(columns=["cluster_id"], errors="ignore"),
                    section="summary",
                    pct_cols=("spot_share_compute", "spot_node_pct"),
                    int_cols=("spot_nodes", "total_nodes", "spot_pools",
                              "spot_vm_families", "spot_zones", "vmss_churn_approx"),
                    fail_cols=("risk_band",), fail_values=("HIGH",), warn_values=("MED",),
                    max_width=70)
    excel.add_table(wb, "RealizedSavings", realized, section="summary",
                    money_cols=("invoiced_spot_fact_usd", "od_counterfactual_model_usd",
                                "est_saving_model_usd"),
                    pct_cols=("savings_rate_pct",),
                    fail_cols=("verdict",), fail_values=("COST_UP", "PRICE_MISSING"),
                    warn_values=("FLAT", "NO_SPOT_COST"), max_width=90)
    excel.add_table(wb, "MonthlySavings", monthly, section="summary",
                    money_cols=("spot_cost_usd", "od_counterfactual_usd",
                                "estimated_saving_usd", "total_cost_usd"),
                    pct_cols=("savings_rate_pct",),
                    fail_cols=("savings_from_spot_pool",),
                    warn_values=("No (no spot spend)",), fail_values=(), max_width=70)
    ws_timeline = excel.add_table(wb, "SpotTimeline", timeline, section="summary",
                                  money_cols=("Actual total USD", "OD counterfactual USD",
                                              "Cumulative realized saving USD",
                                              "Modeled future total USD"))
    if len(timeline) > 1:
        actual_col = list(timeline.columns).index("Actual total USD") + 1
        counter_col = list(timeline.columns).index("OD counterfactual USD") + 1
        cum_col = list(timeline.columns).index("Cumulative realized saving USD") + 1
        excel.add_line_chart(ws_timeline, "Actual cost vs on-demand counterfactual",
                             len(timeline) + 1, actual_col, counter_col,
                             "I%d" % (len(timeline) + 4), y_title="USD / day")
        excel.add_line_chart(ws_timeline, "Cumulative realized fleet savings",
                             len(timeline) + 1, cum_col, cum_col,
                             "I%d" % (len(timeline) + 22), y_title="USD")
    ws_top = excel.add_table(wb, "TopSavers", top_savers, section="summary",
                             money_cols=("projected_monthly_saving_usd",
                                         "annualized_projected_usd",
                                         "counterfactual_spot_saving_usd"),
                             pct_cols=("savings_rate_pct",),
                             int_cols=("current_regular_user_od_nodes", "current_spot_nodes"),
                             fail_cols=("verdict",),
                             fail_values=("COST_UP", "PRICE_MISSING"),
                             warn_values=("FLAT", "NO_SPOT_COST"),
                             max_width=100)
    if len(top_savers) > 1:
        monthly_col = list(top_savers.columns).index("projected_monthly_saving_usd") + 1
        excel.add_bar_chart(ws_top, "Projected monthly saving by cluster",
                            len(top_savers) + 1, monthly_col,
                            "I%d" % (len(top_savers) + 4), y_title="USD / month")
    period_money = ("actual_vmss_cost_usd", "cluster_fee_usd", "end_to_end_cost_usd")
    period_ints = ("days_used", "current_nodes_now")
    excel.add_table(wb, "BeforeSpot", before_spot, section="detail",
                    money_cols=period_money, int_cols=period_ints,
                    formats={"avg_node_equiv_at_retail": "0.0"}, max_width=90)
    excel.add_table(wb, "AfterSpot", after_spot, section="detail",
                    money_cols=period_money, int_cols=period_ints,
                    formats={"avg_node_equiv_at_retail": "0.0"}, max_width=90)
    excel.add_table(wb, "SavingsProjection", projection, section="detail",
                    money_cols=("before_end_to_end_cost_usd", "after_end_to_end_cost_usd",
                                "actual_total_saving_vs_before_rate_usd",
                                "actual_spot_cost_usd", "od_counterfactual_usd",
                                "counterfactual_spot_saving_usd",
                                "projected_saving_usd", "projected_monthly_saving_usd",
                                "annualized_projected_usd"),
                    pct_cols=("actual_total_delta_pct", "savings_rate_pct"),
                    int_cols=("before_days", "after_days",
                              "current_regular_user_od_nodes", "current_spot_nodes",
                              "project_days"),
                    formats={"project_move_to_spot_nodes": "0.0",
                             "project_on_demand_nodes_after": "0.0"},
                    fail_cols=("verdict",),
                    fail_values=("COST_UP", "PRICE_MISSING"),
                    warn_values=("FLAT", "BASELINE_MISSING", "NO_SPOT_COST"),
                    max_width=100)
    ws_env = excel.add_table(wb, "SavingsByEnv", by_env, section="summary",
                             money_cols=("actual_spot_cost_usd", "od_counterfactual_usd",
                                         "counterfactual_spot_saving_usd",
                                         "projected_monthly_saving_usd",
                                         "annualized_projected_usd"),
                             pct_cols=("savings_rate_pct",),
                             int_cols=("clusters", "verified_savers"),
                             fail_cols=("top_verdict",),
                             fail_values=("COST_UP", "PRICE_MISSING"),
                             warn_values=("FLAT", "NO_SPOT_COST"),
                             max_width=100)
    if len(by_env) > 1:
        env_monthly_col = list(by_env.columns).index("projected_monthly_saving_usd") + 1
        excel.add_bar_chart(
            ws_env, "Projected monthly saving by environment",
            len(by_env) + 1, env_monthly_col,
            "I%d" % (len(by_env) + 4), y_title="USD / month")
    ws_chart = excel.add_table(wb, "ActualVsProjection", chart_data, section="detail",
                               money_cols=("Actual total USD",
                                           "OD counterfactual total USD",
                                           "Modeled future total USD"))
    add_actual_projection_chart(ws_chart, chart_data)
    summary_display = summary.drop(columns=["cluster_id"], errors="ignore")
    excel.add_table(wb, "SpotSavingsSummary", summary_display, section="detail",
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
    ws_trend = excel.add_table(wb, "FleetDailyTrend", fleet_disp, section="detail",
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
