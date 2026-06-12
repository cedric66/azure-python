"""Cluster 360 report: every AKS cluster across all subscriptions in one
intelligently categorized workbook.

Joins, per cluster: inventory (Resource Graph), Kubernetes version support
status (one AKS API call per region), the governance scorecard checks,
3-month amortized cost with MoM trend and pricing-model split (Cost Management
subscription-scope queries, never per cluster), node CPU/memory utilization
(Azure Monitor platform metrics, one paced call per running cluster) and node
image staleness - then assigns each cluster a category and a 0-100 health score.

Categories (first match wins):
  UPGRADE NOW      control-plane minor is out of support in its region
  STOPPED BILLING  cluster is stopped but disks, IPs and the fee keep billing
  SECURITY GAP     security-critical governance checks failing
  IDLE CAPACITY    running far below capacity (platform metrics)
  COST HOTSPOT     >25% month-over-month growth above --hotspot-min-usd
  UPGRADE SOON     LTS-only / oldest-supported / preview version or stale node images
  HYGIENE REVIEW   governance score below 70%
  HEALTHY          none of the above

Rate limiting is inherited from the shared clients: Resource Graph/ARM retry
with exponential backoff on 429/5xx, Cost Management is QPU-paced and queried
at subscription scope grouped by node resource group (a handful of queries per
subscription), and Monitor metrics calls are paced at ~0.15s. Use --no-cost
and/or --no-metrics for a faster Resource-Graph-only sweep.

Usage:
  python cluster_360.py --all
  python cluster_360.py --all --no-metrics
  python cluster_360.py --nonprod --months 3 --days 14
  python cluster_360.py --all --no-cost --no-metrics   # Resource Graph only
"""
import datetime as dt
from collections import defaultdict

import pandas as pd

from azrep import excel
from azrep.armextras import aks_supported_versions, cluster_metrics, node_image_date
from azrep.costmgmt import CostClient, default_window, dim_in
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope
from fleet_cost import RG_CHUNK, chunks, last_full_prev
from governance import CHECKS as GOV_CHECKS
from utilization_idle import classify as util_classify
from version_eol import minor

UTIL_METRICS = ("node_cpu_usage_percentage", "node_memory_working_set_percentage")
SECURITY_CHECKS = {"api_server_locked_down", "local_accounts_disabled",
                   "aad_integration", "rbac_enabled", "managed_identity"}
PM_ORDER = ["OnDemand", "Spot", "Reservation", "SavingsPlan"]

CATEGORIES = [
    ("UPGRADE NOW", "Kubernetes minor is out of support in its region",
     "Upgrade the control plane (and pools) to a supported minor immediately."),
    ("STOPPED BILLING", "Cluster is stopped/deallocated but disks, public IPs and the "
     "cluster fee keep billing",
     "Delete the cluster or accept the standing cost deliberately."),
    ("SECURITY GAP", "Security-critical checks failing: API lockdown, local accounts, "
     "Entra ID, RBAC, managed identity (any FAIL on prod, 3+ elsewhere)",
     "Close the failing security checks; see ActionItems for each one."),
    ("IDLE CAPACITY", "Platform metrics show the cluster is idle or heavily "
     "underutilized", "Downsize node pools, consolidate workloads, or stop the cluster."),
    ("COST HOTSPOT", "Cost grew >25% between the last two full months above the "
     "--hotspot-min-usd floor", "Run the deepdive report to find the meter/pool driving it."),
    ("UPGRADE SOON", "Version is LTS-only / oldest-supported / preview, or node images "
     "are stale", "Schedule an upgrade and enable auto-upgrade + node OS channels."),
    ("HYGIENE REVIEW", "Governance score below 70%",
     "Work through the failed checks in ActionItems; some may be accepted trade-offs."),
    ("HEALTHY", "No risk signal fired", "No action needed."),
]
CATEGORY_RANK = {name: i for i, (name, _, _) in enumerate(CATEGORIES)}
SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def version_status(cluster, supported_by_region):
    """SUPPORTED / OUT OF SUPPORT / LTS ONLY / OLDEST SUPPORTED / PREVIEW."""
    ver = cluster["current_kubernetes_version"] or cluster["kubernetes_version"]
    mv = minor(ver)
    sup = supported_by_region.get(cluster["location"]) or {}
    info = sup.get(mv)
    if info is None:
        return "OUT OF SUPPORT", "minor %s is not supported in %s" % (mv, cluster["location"])
    if info["is_preview"]:
        return "PREVIEW", "running a preview version (no production SLA)"
    if "KubernetesOfficial" not in info["support_plans"]:
        return "LTS ONLY", ("community support ended; needs Premium tier + LTS "
                            "(cluster tier: %s)" % cluster["sku_tier"])
    minors = sorted((k for k in sup if k.replace(".", "").isdigit() or "." in k),
                    key=lambda x: [int(y) for y in x.split(".") if y.isdigit()])
    if minors and mv == minors[0]:
        return "OLDEST SUPPORTED", "next AKS release will drop this minor"
    return "SUPPORTED", ""


def categorize(sig, hotspot_min):
    if sig["version_status"] == "OUT OF SUPPORT":
        return "UPGRADE NOW"
    if (sig["power_state"] or "").lower() == "stopped":
        return "STOPPED BILLING"
    if sig["security_fails"] >= (1 if is_prod(sig["environment"]) else 3):
        return "SECURITY GAP"
    if sig["util_flag"] in ("IDLE", "UNDERUTILIZED"):
        return "IDLE CAPACITY"
    if (sig["mom_growth"] is not None and sig["mom_growth"] > 0.25
            and (sig["last_full_usd"] or 0) >= hotspot_min):
        return "COST HOTSPOT"
    if sig["version_status"] in ("LTS ONLY", "OLDEST SUPPORTED", "PREVIEW") or sig["image_stale"]:
        return "UPGRADE SOON"
    if sig["gov_score"] is not None and sig["gov_score"] < 0.7:
        return "HYGIENE REVIEW"
    return "HEALTHY"


def health_score(sig, image_warn_days):
    s = 100.0
    s -= {"OUT OF SUPPORT": 30, "LTS ONLY": 20, "PREVIEW": 15,
          "OLDEST SUPPORTED": 10}.get(sig["version_status"], 0)
    if sig["gov_score"] is not None:
        s -= (1 - sig["gov_score"]) * 30
    s -= min(sig["security_fails"] * 5, 15)
    s -= {"STOPPED": 10, "IDLE": 15, "UNDERUTILIZED": 8}.get(sig["util_flag"], 0)
    if sig["image_age"] is not None and sig["image_age"] > image_warn_days:
        s -= 10
    if sig["mom_growth"] is not None:
        if sig["mom_growth"] > 0.5:
            s -= 10
        elif sig["mom_growth"] > 0.25:
            s -= 5
    return max(0, int(round(s)))


def build_findings(sig, image_warn_days, hotspot_min):
    """One row per signal so ActionItems explains every flag, not just the category."""
    f = []

    def add(area, sev, finding, rec):
        f.append({"area": area, "severity": sev, "finding": finding, "recommendation": rec})

    if sig["version_status"] != "SUPPORTED":
        sev = "HIGH" if sig["version_status"] == "OUT OF SUPPORT" else "MEDIUM"
        add("version", sev, "Kubernetes %s: %s (%s)"
            % (sig["kubernetes_version"], sig["version_status"], sig["version_note"]),
            "Upgrade to a supported minor; enable an auto-upgrade channel.")
    if (sig["power_state"] or "").lower() == "stopped":
        add("cost", "MEDIUM", "Cluster stopped - disks, public IPs and the cluster fee still bill",
            "Delete the cluster or document why it is parked.")
    for cid, desc, detail in sig["gov_fail_detail"]:
        sec = cid in SECURITY_CHECKS
        add("security" if sec else "governance", "HIGH" if sec else "LOW",
            "%s: %s" % (cid, detail or desc), desc)
    if sig["util_flag"] in ("IDLE", "UNDERUTILIZED"):
        add("utilization", "MEDIUM" if sig["util_flag"] == "IDLE" else "LOW",
            "%s: CPU avg %s%%, mem avg %s%% over the metrics window"
            % (sig["util_flag"], sig["cpu_avg"], sig["mem_avg"]),
            "Downsize pools or consolidate; validate with the deepdive report first.")
    if (sig["mom_growth"] is not None and sig["mom_growth"] > 0.25
            and (sig["last_full_usd"] or 0) >= hotspot_min):
        add("cost", "MEDIUM", "Cost grew %.0f%% month-over-month to $%.0f"
            % (sig["mom_growth"] * 100, sig["last_full_usd"]),
            "Run the deepdive report; check MeterChanges for SKU drift.")
    if sig["image_stale"]:
        add("patching", "MEDIUM", "Oldest node image is %d days old (warn at %d)"
            % (sig["image_age"], image_warn_days),
            "Set the node OS upgrade channel so security patches are applied.")
    f.sort(key=lambda x: SEVERITY_RANK[x["severity"]])
    return f


def query_costs(session, clusters, months):
    """Subscription-scope amortized cost grouped by node RG + pricing model,
    plus the managed-cluster fee. Returns (pm_df, fee_by_cluster)."""
    by_sub = defaultdict(list)
    for c in clusters:
        if c["node_resource_group"]:
            by_sub[c["subscription_id"]].append(c)
    rg_map = {(c["subscription_id"], c["node_resource_group"].lower()): c for c in clusters}
    id_map = {c["id"].lower(): c for c in clusters}

    d_from, d_to = default_window(months)
    cost = CostClient(session)
    est = sum((len({c["node_resource_group"].lower() for c in v}) + RG_CHUNK - 1) // RG_CHUNK + 1
              for v in by_sub.values())
    log("Cost window %s to %s; ~%d Cost Management queries across %d subscriptions "
        "(QPU-paced, retry-after honored on 429)." % (d_from, d_to, est, len(by_sub)))

    pm_rows, fee_rows = [], []
    for i, (sid, cls) in enumerate(sorted(by_sub.items()), 1):
        rgs = sorted({c["node_resource_group"].lower() for c in cls})
        log("[%d/%d] %s: %d clusters, %d node RGs"
            % (i, len(by_sub), cls[0]["subscription"], len(cls), len(rgs)))
        scope = "/subscriptions/%s" % sid
        for ch in chunks(rgs, RG_CHUNK):
            df = cost.query(scope, "AmortizedCost", "Monthly",
                            ("ResourceGroupName", "PricingModel"),
                            dim_in("ResourceGroupName", ch), d_from, d_to)
            if not df.empty:
                df["subscription_id"] = sid
                pm_rows.append(df)
        df = cost.query(scope, "AmortizedCost", "Monthly", ("ResourceId",),
                        dim_in("ResourceType", ["microsoft.containerservice/managedclusters"]),
                        d_from, d_to)
        if not df.empty:
            df["subscription_id"] = sid
            fee_rows.append(df)

    if not pm_rows:
        return pd.DataFrame(), {}, (d_from, d_to, cost.calls)
    pm = pd.concat(pm_rows, ignore_index=True)
    key = list(zip(pm["subscription_id"], pm["ResourceGroupName"].str.lower()))
    for field in ("cluster", "subscription", "environment"):
        pm[field] = [rg_map.get(k, {}).get(field, "(unmatched)") for k in key]
    pm["Month"] = pm["Period"].str[:7]

    fee = {}
    if fee_rows:
        fdf = pd.concat(fee_rows, ignore_index=True)
        fdf["cluster"] = fdf["ResourceId"].str.lower().map(
            lambda x: id_map.get(x, {}).get("cluster"))
        fee = fdf.dropna(subset=["cluster"]).groupby("cluster")["CostUSD"].sum().to_dict()
    return pm, fee, (d_from, d_to, cost.calls)


def main(argv=None):
    p = base_parser("AKS Cluster 360: all clusters, all subscriptions, categorized")
    p.add_argument("--months", type=int, default=3, help="full months of cost history")
    p.add_argument("--days", type=int, default=14, help="metrics lookback window")
    p.add_argument("--image-warn-days", type=int, default=60,
                   help="flag node images older than this many days")
    p.add_argument("--hotspot-min-usd", type=float, default=250.0,
                   help="minimum last-full-month USD before MoM growth flags COST HOTSPOT")
    p.add_argument("--no-cost", action="store_true", help="skip Cost Management queries")
    p.add_argument("--no-metrics", action="store_true", help="skip Monitor metrics calls")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.15)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope - nothing to report.")
        return

    pools_by_cluster = defaultdict(list)
    for q in pools:
        pools_by_cluster[q["cluster_id"].lower()].append(q)

    regions = sorted({c["location"] for c in clusters})
    log("Fetching supported AKS versions for %d region(s)..." % len(regions))
    region_sub = {}
    for c in clusters:
        region_sub.setdefault(c["location"], c["subscription_id"])
    supported = {r: aks_supported_versions(session, region_sub[r], r) for r in regions}

    pm, fee, cost_meta = (pd.DataFrame(), {}, None)
    if not args.no_cost:
        pm, fee, cost_meta = query_costs(session, clusters, args.months)
        if pm.empty:
            log("No cost rows returned - check Cost Management Reader access; "
                "continuing without cost.")
    months = sorted(pm["Month"].unique()) if not pm.empty else []
    prev_m, last_m = last_full_prev(months) if months else (None, None)
    cost_by_cluster = pm.pivot_table(index="cluster", columns="Month", values="CostUSD",
                                     aggfunc="sum").fillna(0.0) if not pm.empty else None
    spot_by_cluster = (pm[pm["PricingModel"].astype(str).str.lower() == "spot"]
                       .groupby("cluster")["CostUSD"].sum().to_dict()) if not pm.empty else {}

    metrics = {}
    if not args.no_metrics:
        running = [c for c in clusters if (c["power_state"] or "").lower() != "stopped"]
        log("Querying platform metrics for %d running clusters (%d days, paced)..."
            % (len(running), args.days))
        for i, c in enumerate(running, 1):
            if i % 25 == 0:
                log("  %d/%d..." % (i, len(running)))
            metrics[c["id"].lower()] = cluster_metrics(session, c["id"], days=args.days,
                                                       metrics=UTIL_METRICS)

    today = dt.date.today()
    sigs, action_rows = [], []
    for c in clusters:
        ps = pools_by_cluster.get(c["id"].lower(), [])
        vstat, vnote = version_status(c, supported)

        gov, fails = {}, []
        for cid, desc, fn in GOV_CHECKS:
            try:
                status, detail = fn(c, ps)
            except Exception as e:
                status, detail = "N-A", "check error: %s" % e
            gov[cid] = status
            if status == "FAIL":
                fails.append((cid, desc, detail))
        n_pass = sum(1 for v in gov.values() if v == "PASS")
        n_fail = len(fails)
        gov_score = n_pass / (n_pass + n_fail) if (n_pass + n_fail) else None

        m = metrics.get(c["id"].lower(), {})
        cpu = m.get("node_cpu_usage_percentage", {})
        mem = m.get("node_memory_working_set_percentage", {})
        util_flag = util_classify(c["power_state"] or "", cpu, mem) if not args.no_metrics \
            else ("STOPPED" if (c["power_state"] or "").lower() == "stopped" else "NOT CHECKED")

        ages = [a for a in ((today - d).days for d in
                            (node_image_date(q["node_image_version"]) for q in ps) if d)
                if a is not None]
        image_age = max(ages) if ages else None

        window_usd = spot_usd = last_full_usd = None
        mom_growth = None
        month_usd = {}
        if cost_by_cluster is not None and c["cluster"] in cost_by_cluster.index:
            row = cost_by_cluster.loc[c["cluster"]]
            month_usd = {mo: float(row.get(mo, 0.0)) for mo in months}
            window_usd = sum(month_usd.values())
            spot_usd = float(spot_by_cluster.get(c["cluster"], 0.0))
            if prev_m and last_m:
                prev_v, last_v = month_usd.get(prev_m, 0.0), month_usd.get(last_m, 0.0)
                last_full_usd = last_v
                if prev_v > 0:
                    mom_growth = (last_v - prev_v) / prev_v

        sig = {
            "cluster": c["cluster"], "subscription": c["subscription"],
            "subscription_id": c["subscription_id"], "environment": c["environment"],
            "location": c["location"], "power_state": c["power_state"],
            "sku_tier": c["sku_tier"], "kubernetes_version":
                c["current_kubernetes_version"] or c["kubernetes_version"],
            "version_status": vstat, "version_note": vnote,
            "gov_score": gov_score, "gov_fails": n_fail,
            "security_fails": sum(1 for cid, _, _ in fails if cid in SECURITY_CHECKS),
            "gov_fail_detail": fails,
            "util_flag": util_flag,
            "cpu_avg": round(cpu["avg"], 1) if cpu.get("avg") is not None else None,
            "mem_avg": round(mem["avg"], 1) if mem.get("avg") is not None else None,
            "image_age": image_age,
            "image_stale": image_age is not None and image_age > args.image_warn_days,
            "month_usd": month_usd, "window_usd": window_usd, "spot_usd": spot_usd,
            "last_full_usd": last_full_usd, "mom_growth": mom_growth,
            "fee_usd": fee.get(c["cluster"]),
            "node_pools": c["node_pools"], "total_nodes": c["total_nodes"],
            "spot_nodes": c["spot_nodes"], "vm_sizes": c["vm_sizes"],
            "private_cluster": c["private_cluster"],
            "upgrade_channel": c["upgrade_channel"] or "(none)",
            "node_os_upgrade_channel": c["node_os_upgrade_channel"] or "(none)",
        }
        sig["category"] = categorize(sig, args.hotspot_min_usd)
        sig["health_score"] = health_score(sig, args.image_warn_days)
        findings = build_findings(sig, args.image_warn_days, args.hotspot_min_usd)
        sig["top_findings"] = "; ".join(x["finding"] for x in findings[:3])
        for x in findings:
            action_rows.append({"cluster": c["cluster"], "subscription": c["subscription"],
                                "environment": c["environment"], "category": sig["category"],
                                **x})
        sigs.append(sig)

    # ---- master Cluster360 table ----
    pre = ["cluster", "subscription", "environment", "location", "category",
           "health_score", "top_findings", "power_state", "sku_tier",
           "kubernetes_version", "version_status", "gov_score", "gov_fails",
           "security_fails", "util_flag", "cpu_avg %", "mem_avg %",
           "max_image_age_days"]
    post = ["node_pools", "total_nodes", "spot_nodes", "vm_sizes", "private_cluster",
            "upgrade_channel", "node_os_upgrade_channel", "subscription_id"]
    rows = []
    for s in sigs:
        r = {"cluster": s["cluster"], "subscription": s["subscription"],
             "environment": s["environment"], "location": s["location"],
             "category": s["category"], "health_score": s["health_score"],
             "top_findings": s["top_findings"], "power_state": s["power_state"],
             "sku_tier": s["sku_tier"], "kubernetes_version": s["kubernetes_version"],
             "version_status": s["version_status"], "gov_score": s["gov_score"],
             "gov_fails": s["gov_fails"], "security_fails": s["security_fails"],
             "util_flag": s["util_flag"], "cpu_avg %": s["cpu_avg"],
             "mem_avg %": s["mem_avg"], "max_image_age_days": s["image_age"],
             "node_pools": s["node_pools"], "total_nodes": s["total_nodes"],
             "spot_nodes": s["spot_nodes"], "vm_sizes": s["vm_sizes"],
             "private_cluster": s["private_cluster"],
             "upgrade_channel": s["upgrade_channel"],
             "node_os_upgrade_channel": s["node_os_upgrade_channel"],
             "subscription_id": s["subscription_id"]}
        for mo in months:
            r[mo] = s["month_usd"].get(mo, 0.0)
        if months:
            r["Spot %"] = (s["spot_usd"] / s["window_usd"]
                           if s["window_usd"] else None)
            r["Cluster fee (USD)"] = s["fee_usd"] or 0.0
        rows.append(r)
    master = pd.DataFrame(rows).sort_values(
        ["category", "health_score", "cluster"],
        key=lambda col: col.map(CATEGORY_RANK) if col.name == "category" else col
    ).reset_index(drop=True)

    cost_cols = []
    if months:
        # sort BEFORE adding formula columns - formulas are anchored to row numbers
        n = len(master)
        first_l = excel.get_column_letter(len(pre) + 1)
        last_l = excel.get_column_letter(len(pre) + len(months))
        master["Window total (USD)"] = ["=SUM(%s%d:%s%d)" % (first_l, r, last_l, r)
                                        for r in range(2, n + 2)]
        if prev_m and last_m:
            pl = excel.get_column_letter(len(pre) + 1 + months.index(prev_m))
            ll = excel.get_column_letter(len(pre) + 1 + months.index(last_m))
            master["MoM %"] = ["=IF(%s%d=0,\"\",(%s%d-%s%d)/%s%d)" % (pl, r, ll, r, pl, r, pl, r)
                               for r in range(2, n + 2)]
        cost_cols = months + ["Window total (USD)"] + (["MoM %"] if prev_m and last_m else []) \
            + ["Spot %", "Cluster fee (USD)"]
    master = master[pre + cost_cols + post]

    actions = pd.DataFrame(action_rows) if action_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "category", "area",
                 "severity", "finding", "recommendation"])
    if not actions.empty:
        actions = actions.sort_values(
            ["severity", "cluster"], key=lambda col: col.map(SEVERITY_RANK)
            if col.name == "severity" else col).reset_index(drop=True)
        actions = actions[["cluster", "subscription", "environment", "category",
                           "area", "severity", "finding", "recommendation"]]

    sdf = pd.DataFrame(sigs)
    summary = sdf.groupby("category").agg(
        clusters=("cluster", "count"), nodes=("total_nodes", "sum"),
        avg_health=("health_score", "mean"),
        window_cost_usd=("window_usd", "sum")).reset_index()
    summary["avg_health"] = summary["avg_health"].round(0).astype(int)
    summary = summary.sort_values("category", key=lambda col: col.map(CATEGORY_RANK)) \
        .reset_index(drop=True)

    def rollup(col):
        g = sdf.groupby(col).agg(
            clusters=("cluster", "count"), nodes=("total_nodes", "sum"),
            needs_action=("category", lambda s: int((s != "HEALTHY").sum())),
            avg_health=("health_score", "mean"),
            window_cost_usd=("window_usd", "sum")).reset_index()
        g["avg_health"] = g["avg_health"].round(0).astype(int)
        return g.sort_values("window_cost_usd", ascending=False).reset_index(drop=True)

    by_sub_df = rollup("subscription")
    by_env_df = rollup("environment")

    legend = pd.DataFrame(CATEGORIES, columns=["category", "meaning", "typical action"])

    # ---- workbook ----
    wb = excel.new_workbook()
    cost_line = "Cost: skipped (--no-cost)" if args.no_cost else (
        "Cost: amortized USD, window %s to %s, %d Cost Management queries"
        % (cost_meta[0], cost_meta[1], cost_meta[2]) if cost_meta else
        "Cost: no rows returned (missing Cost Management Reader?)")
    excel.add_readme(wb, "AKS Cluster 360", [
        "Generated: %s   Scope: %s   Clusters: %d   Subscriptions: %d" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all",
         len(sigs), len(sel)),
        cost_line,
        "Metrics: %s" % ("skipped (--no-metrics)" if args.no_metrics
                         else "Azure Monitor platform metrics, %d-day window" % args.days),
        "",
        "One row per cluster on Cluster360, joined from Resource Graph inventory,",
        "regional AKS supported-version lists, the governance checks, Cost Management",
        "(subscription-scope, grouped by node resource group - never per cluster) and",
        "platform metrics. Each cluster gets one category (first matching rule wins,",
        "see CategoryLegend) and a 0-100 health score (deductions for version, failed",
        "checks, idle capacity, stale images and cost growth).",
        "",
        "ActionItems lists every individual finding - a cluster shows all of its",
        "issues there even though it appears under a single category.",
        "The current month is partial (MTD); MoM compares the last two full months.",
        "All checks are control-plane signals; validate before acting (some FAILs are",
        "deliberate trade-offs, and node metrics miss pod requests/limits).",
    ])
    ws = excel.add_table(wb, "Summary", summary, section="summary",
                         int_cols=("clusters", "nodes", "avg_health"),
                         money_cols=("window_cost_usd",),
                         fail_cols=("category",),
                         fail_values=("UPGRADE NOW", "SECURITY GAP", "STOPPED BILLING"),
                         warn_values=("COST HOTSPOT", "IDLE CAPACITY", "UPGRADE SOON",
                                      "HYGIENE REVIEW"))
    excel.add_bar_chart(ws, "Clusters per category", len(summary) + 1, 2,
                        "A%d" % (len(summary) + 4), y_title="clusters")
    excel.add_table(wb, "SummaryBySubscription", by_sub_df, section="summary",
                    int_cols=("clusters", "nodes", "needs_action", "avg_health"),
                    money_cols=("window_cost_usd",))
    excel.add_table(wb, "SummaryByEnvironment", by_env_df, section="summary",
                    int_cols=("clusters", "nodes", "needs_action", "avg_health"),
                    money_cols=("window_cost_usd",))
    excel.add_table(wb, "Cluster360", master,
                    fail_cols=("category", "version_status", "util_flag"),
                    fail_values=("UPGRADE NOW", "SECURITY GAP", "STOPPED BILLING",
                                 "OUT OF SUPPORT", "IDLE", "STOPPED"),
                    warn_values=("COST HOTSPOT", "IDLE CAPACITY", "UPGRADE SOON",
                                 "HYGIENE REVIEW", "LTS ONLY", "OLDEST SUPPORTED",
                                 "PREVIEW", "UNDERUTILIZED"),
                    pct_cols=("gov_score", "Spot %", "MoM %"),
                    money_cols=tuple(months) + (("Window total (USD)",
                                                 "Cluster fee (USD)") if months else ()),
                    int_cols=("health_score", "gov_fails", "security_fails",
                              "max_image_age_days", "node_pools", "total_nodes",
                              "spot_nodes"),
                    max_width=70)
    excel.add_table(wb, "ActionItems", actions, fail_cols=("severity",),
                    fail_values=("HIGH",), warn_values=("MEDIUM",), max_width=90)
    excel.add_table(wb, "NodePools", pd.DataFrame(pools),
                    int_cols=("count", "min_count", "max_count", "os_disk_gb", "max_pods"))
    if not pm.empty:
        split = pm.pivot_table(index=["cluster", "subscription", "environment"],
                               columns="PricingModel", values="CostUSD",
                               aggfunc="sum").fillna(0.0).reset_index()
        pm_cols = [c for c in PM_ORDER if c in split.columns] + \
                  [c for c in split.columns
                   if c not in PM_ORDER + ["cluster", "subscription", "environment"]]
        split = split[["cluster", "subscription", "environment"] + pm_cols]
        sl_first, sl_last = excel.get_column_letter(4), excel.get_column_letter(3 + len(pm_cols))
        split["Total (USD)"] = ["=SUM(%s%d:%s%d)" % (sl_first, r, sl_last, r)
                                for r in range(2, len(split) + 2)]
        excel.add_table(wb, "PricingModelSplit", split,
                        money_cols=tuple(pm_cols) + ("Total (USD)",))
    if not args.no_metrics:
        util = master[["cluster", "subscription", "environment", "power_state",
                       "total_nodes", "cpu_avg %", "mem_avg %", "util_flag"]]
        excel.add_table(wb, "Utilization", util, fail_cols=("util_flag",),
                        colorscale_cols=("cpu_avg %", "mem_avg %"),
                        int_cols=("total_nodes",))
    if not pm.empty:
        raw = pm.groupby(["cluster", "subscription", "environment", "Month",
                          "PricingModel"])["CostUSD"].sum().reset_index() \
            .rename(columns={"CostUSD": "Amortized (USD)"})
        excel.add_table(wb, "RawMonthlyCost", raw, money_cols=("Amortized (USD)",),
                        section="reference")
    excel.add_table(wb, "CategoryLegend", legend, max_width=95, section="reference")

    path = excel.save(wb, out_path(args, "aks_cluster_360", env_filter))
    log("Report written: %s" % path)
    log("Categories: %s" % ", ".join("%s=%d" % (r["category"], r["clusters"])
                                     for _, r in summary.iterrows()))


if __name__ == "__main__":
    main()
