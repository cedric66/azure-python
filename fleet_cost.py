"""Fleet cost report: 3-month amortized cost trend for every AKS cluster in scope.

Designed for 25 subscriptions / 500 clusters without melting the Cost Management
API: costs are queried at SUBSCRIPTION scope grouped by node resource group
(3-4 queries per subscription, not per cluster) and joined back to clusters.

Tabs: ReadMe, ClusterCosts (per-cluster monthly trend, MoM, spot share, RI/SP
coverage, cluster fee), PricingModelSplit, TopMovers, MeterChanges (SKU change
signals fleet-wide), SummaryBySubscription, RawMonthly (+ RawDaily with
--granularity daily).

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
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope

RG_CHUNK = 30
PM_ORDER = ["OnDemand", "Spot", "Reservation", "SavingsPlan"]
RISP_PM = ("reservation", "reservations", "savingsplan", "savings plan")
COMMIT_DISCOUNT_DEFAULT = 0.30   # indicative 1-yr savings-plan/reservation discount
COMMIT_MIN_USD = 50.0            # ignore trivial steady OnDemand baselines
COMMIT_COVERED = 0.70            # already-committed coverage that needs no further action
SPIKE_MOM = 0.25                 # MoM growth that counts as a cost spike on the scorecard


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


def _safe_div(num, den):
    return float(num) / float(den) if den else None


def _fmt_money_compact(v):
    """$1.2M / $12.3k / $540 for KPI cards (None/NaN -> 'n/a')."""
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
    if frac is None or pd.isna(frac):
        return "n/a"
    return "%.0f%%" % (float(frac) * 100.0)


def env_summary_rows(pm, months):
    """Per-environment amortized roll-up, one tier up from ClusterCosts. Adds a
    prod/non-prod `tier` (azrep.subs.is_prod, the same resolution every report uses)
    plus window total, fleet share % and MoM %. Formula columns anchor to row
    numbers, so the frame is sorted before they are added and not re-sorted after."""
    if pm.empty:
        return pd.DataFrame()
    ev = pm.copy()
    ev["environment"] = ev["environment"].replace("", "(unknown)").fillna("(unknown)")
    piv = ev.pivot_table(index="environment", columns="Month", values="CostUSD",
                         aggfunc="sum").fillna(0.0)
    for m in months:
        if m not in piv.columns:
            piv[m] = 0.0
    piv = piv[months].reset_index()
    piv.insert(1, "tier", ["prod" if is_prod(e) else "non-prod" for e in piv["environment"]])
    prev_m, last_m = last_full_prev(months)
    piv = piv.sort_values(last_m or months[-1], ascending=False).reset_index(drop=True)
    nmeta, n = 2, len(piv)
    first_l = excel.get_column_letter(nmeta + 1)
    last_l = excel.get_column_letter(nmeta + len(months))
    tot_l = excel.get_column_letter(nmeta + len(months) + 1)
    piv["Window total (USD)"] = ["=SUM(%s%d:%s%d)" % (first_l, r, last_l, r)
                                 for r in range(2, n + 2)]
    piv["Share %"] = ["=IF(SUM(%s$2:%s$%d)=0,\"\",%s%d/SUM(%s$2:%s$%d))"
                      % (tot_l, tot_l, n + 1, tot_l, r, tot_l, tot_l, n + 1)
                      for r in range(2, n + 2)]
    if prev_m and last_m:
        pl = excel.get_column_letter(nmeta + 1 + months.index(prev_m))
        ll = excel.get_column_letter(nmeta + 1 + months.index(last_m))
        piv["MoM %"] = ["=IF(%s%d=0,\"\",(%s%d-%s%d)/%s%d)" % (pl, r, ll, r, pl, r, pl, r)
                        for r in range(2, n + 2)]
    return piv


def commitment_rows(pm, months, risp, discount):
    """Steady OnDemand spend = reservation / savings-plan candidate. Baseline is the
    MIN OnDemand spend across FULL months (a conservative committed floor); the saving
    is an indicative baseline*discount. The pricing-model query has no meter axis, so
    storage/non-VM OnDemand spend is included here - this surfaces candidates, VM SKU
    eligibility and term must be verified before purchase."""
    if pm.empty:
        return pd.DataFrame()
    od = pm[pm["PricingModel"].astype(str).str.lower() == "ondemand"]
    if od.empty:
        return pd.DataFrame()
    cur = dt.date.today().strftime("%Y-%m")
    full = [m for m in months if m != cur] or list(months)
    piv = od.pivot_table(index=["cluster", "subscription", "environment"],
                         columns="Month", values="CostUSD", aggfunc="sum").fillna(0.0)
    for m in full:
        if m not in piv.columns:
            piv[m] = 0.0
    piv = piv.reset_index()
    full_cols = [m for m in full if m in piv.columns]
    base = piv[full_cols].min(axis=1)
    od_win = od.groupby("cluster")["CostUSD"].sum()
    out = piv[["cluster", "subscription", "environment"]].copy()
    out["OD avg/mo (USD)"] = piv[full_cols].mean(axis=1).round(2)
    out["Steady OD baseline (USD)"] = base.round(2)
    out["RI+SP (USD)"] = out["cluster"].map(risp).fillna(0.0).round(2)
    out["OD window (USD)"] = out["cluster"].map(od_win).fillna(0.0).round(2)
    denom = out["OD window (USD)"] + out["RI+SP (USD)"]
    out["Commitment coverage"] = [(_safe_div(r, d) or 0.0)
                                  for r, d in zip(out["RI+SP (USD)"], denom)]
    out["Est monthly saving (USD)"] = (base * float(discount)).round(2)
    out["Est annual saving (USD)"] = (base * float(discount) * 12.0).round(2)

    def status(b, c):
        if b < COMMIT_MIN_USD:
            return "LOW / SKIP"
        return "COVERED" if c >= COMMIT_COVERED else "RESERVE CANDIDATE"

    out["status"] = [status(b, c) for b, c in
                     zip(out["Steady OD baseline (USD)"], out["Commitment coverage"])]
    out = out[out["Steady OD baseline (USD)"] >= COMMIT_MIN_USD]
    return out.sort_values("Est monthly saving (USD)", ascending=False).reset_index(drop=True)


def scorecard_cards(pm, months, commit, discount):
    """Fleet exec KPI cards (list of dicts for excel.add_scorecard). The current MTD
    month is excluded from MoM/run-rate math; commitment opportunity comes from
    commitment_rows so the headline and the action tab agree."""
    prev_m, last_m = last_full_prev(months)
    total = float(pm["CostUSD"].sum())
    last_full = float(pm[pm["Month"] == last_m]["CostUSD"].sum()) if last_m else 0.0
    prev_full = float(pm[pm["Month"] == prev_m]["CostUSD"].sum()) if prev_m else 0.0
    mom = _safe_div(last_full - prev_full, prev_full)
    pml = pm["PricingModel"].astype(str).str.lower()
    spot_cov = _safe_div(float(pm[pml == "spot"]["CostUSD"].sum()), total)
    risp_cov = _safe_div(float(pm[pml.isin(RISP_PM)]["CostUSD"].sum()), total)
    prod_share = _safe_div(float(pm[pm["environment"].map(is_prod)]["CostUSD"].sum()), total)
    by_cl = pm.groupby("cluster")["CostUSD"].sum().sort_values(ascending=False)
    top5 = _safe_div(float(by_cl.head(5).sum()), total)
    n_spike = 0
    if prev_m and last_m:
        pcl = pm.pivot_table(index="cluster", columns="Month", values="CostUSD",
                             aggfunc="sum").fillna(0.0)
        if prev_m in pcl.columns and last_m in pcl.columns:
            growth = (pcl[last_m] - pcl[prev_m]) / pcl[prev_m].replace(0.0, float("nan"))
            n_spike = int((growth > SPIKE_MOM).sum())
    commit_total = float(commit["Est monthly saving (USD)"].sum()) if not commit.empty else 0.0
    n_cand = int((commit["status"] == "RESERVE CANDIDATE").sum()) if not commit.empty else 0

    mom_rag = "neutral"
    if mom is not None:
        mom_rag = "bad" if mom > 0.10 else "warn" if mom > 0 else "good"
    return [
        {"label": "Fleet amortized spend", "value": _fmt_money_compact(total),
         "caption": "all pricing models, window total", "rag": "neutral"},
        {"label": "Last full month", "value": _fmt_money_compact(last_full),
         "caption": last_m or "n/a", "rag": "neutral"},
        {"label": "Month-over-month", "value": _fmt_pct(mom),
         "caption": "%s vs %s (full months)" % (last_m or "n/a", prev_m or "n/a"),
         "rag": mom_rag},
        {"label": "Annualized run-rate", "value": _fmt_money_compact(last_full * 12.0),
         "caption": "last full month x12", "rag": "neutral"},
        {"label": "Spot coverage", "value": _fmt_pct(spot_cov),
         "caption": "spot / total amortized spend", "rag": "good" if (spot_cov or 0) > 0 else "warn"},
        {"label": "RI/SP coverage", "value": _fmt_pct(risp_cov),
         "caption": "reservation+savings plan / total",
         "rag": "good" if (risp_cov or 0) >= 0.30 else "warn"},
        {"label": "Commitment opportunity", "value": _fmt_money_compact(commit_total) + "/mo",
         "caption": "steady OD at %d%% disc - %d candidates" % (round(discount * 100), n_cand),
         "rag": "warn" if commit_total > 0 else "good"},
        {"label": "Prod share", "value": _fmt_pct(prod_share),
         "caption": "prod vs non-prod spend", "rag": "neutral"},
        {"label": "Cost concentration", "value": _fmt_pct(top5),
         "caption": "top-5 clusters of fleet spend",
         "rag": "warn" if (top5 or 0) >= 0.60 else "neutral"},
        {"label": "Cost spikes", "value": str(n_spike),
         "caption": ">%d%% MoM growth clusters" % round(SPIKE_MOM * 100),
         "rag": "warn" if n_spike else "good"},
    ]


def main(argv=None):
    p = base_parser("Fleet-wide AKS amortized cost trend")
    p.add_argument("--months", type=int, default=3, help="full months of history")
    p.add_argument("--granularity", default="Monthly", choices=["Monthly", "Daily"])
    p.add_argument("--actual", action="store_true",
                   help="also query billed (actual) cost for the RI/SP delta column")
    p.add_argument("--commit-discount", type=float, default=COMMIT_DISCOUNT_DEFAULT,
                   help="assumed reservation/savings-plan discount for the "
                        "CommitmentOpportunity saving estimate (default %.2f)"
                        % COMMIT_DISCOUNT_DEFAULT)
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
    risp = pm[pm["PricingModel"].astype(str).str.lower().isin(RISP_PM)] \
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

    # summary story layer: exec scorecard, commitment action, per-environment roll-up
    env_sum = env_summary_rows(pm, months)
    commit = commitment_rows(pm, months, risp, args.commit_discount)
    cards = scorecard_cards(pm, months, commit, args.commit_discount)

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
        "Scorecard is the exec one-pager; CommitmentOpportunity ranks clusters whose",
        "steady OnDemand spend is a reservation/savings-plan candidate (baseline = min",
        "OnDemand over full months, saving = baseline x %d%% assumed discount - storage and"
        % round(args.commit_discount * 100),
        "non-VM meters are included, so verify VM SKU eligibility before purchasing).",
        "SummaryByEnvironment rolls cost up per environment with a prod/non-prod tier.",
        "MeterChanges flags meters that appeared (NEW), disappeared (REMOVED) or moved",
        ">50% between the first and last month with >$5 spend - SKU change signals.",
    ])
    excel.add_scorecard(wb, "Scorecard", cards, section="summary",
                        title="AKS fleet cost - exec scorecard")
    if not commit.empty:
        excel.add_table(wb, "CommitmentOpportunity", commit, section="summary",
                        money_cols=("OD avg/mo (USD)", "Steady OD baseline (USD)",
                                    "RI+SP (USD)", "OD window (USD)",
                                    "Est monthly saving (USD)", "Est annual saving (USD)"),
                        pct_cols=("Commitment coverage",), fail_cols=("status",),
                        fail_values=(), warn_values=("RESERVE CANDIDATE",))
    if not env_sum.empty:
        ws_env = excel.add_table(wb, "SummaryByEnvironment", env_sum, section="summary",
                                 money_cols=tuple(months) + ("Window total (USD)",),
                                 pct_cols=("Share %", "MoM %"), colorscale_cols=("MoM %",))
        env_total_col = list(env_sum.columns).index("Window total (USD)") + 1
        excel.add_bar_chart(ws_env, "Window amortized cost by environment",
                            len(env_sum) + 1, env_total_col, "B%d" % (len(env_sum) + 5),
                            y_title="USD")
        excel.add_total_row(ws_env, env_sum, list(months) + ["Window total (USD)"],
                            label_col="environment")
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
    ws = excel.add_table(wb, "SummaryBySubscription", bysub, section="summary",
                         money_cols=tuple(months) + ("Window total (USD)",))
    total_col = list(bysub.columns).index("Window total (USD)") + 1
    excel.add_bar_chart(ws, "Window amortized cost by subscription",
                        len(bysub) + 1, total_col, "B%d" % (len(bysub) + 5),
                        y_title="USD")
    excel.add_total_row(ws, bysub, list(months) + ["Window total (USD)"],
                        label_col="subscription")
    excel.add_table(wb, "RawMonthly", raw_m, money_cols=("Amortized (USD)",),
                    section="reference")
    if args.granularity == "Daily":
        raw_d = pm.groupby(["cluster", "subscription", "Period"])["CostUSD"].sum() \
            .reset_index().rename(columns={"Period": "Date", "CostUSD": "Amortized (USD)"})
        excel.add_table(wb, "RawDaily", raw_d, money_cols=("Amortized (USD)",),
                        section="reference")

    path = excel.save(wb, out_path(args, "aks_fleet_cost", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
