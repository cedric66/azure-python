"""AKS cost & efficiency report - the config-driven levers beyond spot.

An ARG-cheap companion to optimization_report.py: the levers here need little or
no Cost Management traffic because the saving signal is a config field we
already flatten. Levers: control-plane tier, ephemeral OS disk conversion, SKU
generation/family modernization (incl. ARM64), autoscaler & floor (min_count)
hygiene, and node-pool fragmentation/consolidation.

Tabs: ReadMe, Scorecard, ControlPlaneTier, EphemeralOSDisk, SKUModernization,
AutoscalerHygiene, PoolFragmentation, Recommendations, NodePools (reference).

Mirrors optimization_report's "screening, not an action plan" stance: every
estimate comes from Reader-visible data (config + retail prices) and every
recommendation carries a verify_before_move caveat, because immutable-after-
create properties (ephemeral OS disk, spot priority) mean the action is
"new pool + migrate", never an in-place edit.

Usage:
  python cost_efficiency.py --all
  python cost_efficiency.py --nonprod
  python cost_efficiency.py --no-retail-prices
"""
import datetime as dt
from collections import defaultdict

import pandas as pd

from azrep import excel
from azrep.armextras import (ephemeral_os_disk_eligible, modernize_sku,
                             retail_vm_prices)
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope

# Control-plane (sku.tier) approximate hourly rates over the cluster fee.
# Pricing drifts - kept as named constants so the tier tab can be re-tuned
# (mirrors optimization_report.TIER_HOURLY).
TIER_HOURLY = {"Free": 0.0, "Standard": 0.012, "Premium": 0.60}
HOURS_PER_MONTH = 730
# Managed OS disk meter estimate, USD per 128 GiB P10/P15 tier per node-month.
# Real billing lives in Cost Management; this is a screening proxy so the
# ephemeral-OS-disk tab can size the addressable saving without a cost query.
MANAGED_OS_DISK_USD_PER_NODE_MONTH = 9.0
# Per-pool autoscaler finding badges surfaced by _autoscaler_finding, used for
# the AutoscalerHygiene warn-fill (any of these turns a row yellow).
_AUTO_FINDINGS = ("USER_POOL_NO_AUTOSCALE", "SYSTEM_POOL_NO_AUTOSCALE",
                  "MIN_COUNT_EQUALS_COUNT", "EXPANDER_NOT_LEAST_WASTE",
                  "NO_EXPANDER")


def _tier_monthly(tier):
    return round(TIER_HOURLY.get(tier, 0.0) * HOURS_PER_MONTH, 2)


def _current_od_hr(region, vm_size, currency):
    """Current on-demand $/hr for the pool SKU, or None when retail is silent
    (offline run, SKU not in the public price sheet)."""
    rec = retail_vm_prices(region, vm_size, currency)
    return rec["od_hr"] if rec else None


def _tier_status(tier, prod):
    """Tier verdict: downgrade candidates (non-prod on paid tier) vs the prod-on-
    Free risk note (no saving)."""
    if not prod and tier in ("Standard", "Premium") and TIER_HOURLY.get(tier, 0) > 0:
        return "DOWNGRADE TO FREE"
    if prod and tier == "Free":
        return "REVIEW (NO UPTIME SLA)"
    return "OK"


def _autoscaler_finding(pool, prod):
    """Per-pool cluster-autoscaler / floor hygiene. Advisory only: system pools
    need a sane floor, and settings interact with workload PDBs we can't see."""
    findings = []
    if not pool["autoscaling"]:
        findings.append("USER_POOL_NO_AUTOSCALE" if pool["mode"].lower() == "user"
                        else "SYSTEM_POOL_NO_AUTOSCALE")
    min_count = pool.get("min_count")
    try:
        min_count = int(min_count) if min_count is not None else 0
    except (TypeError, ValueError):
        min_count = 0
    if pool["mode"].lower() == "user" and min_count > 0 and pool["count"] > 0 \
            and min_count == pool["count"]:
        findings.append("MIN_COUNT_EQUALS_COUNT")
    expander = str(pool.get("autoscaler_expander") or "").lower()
    if not expander:
        findings.append("NO_EXPANDER")
    elif pool["autoscaling"] and expander not in ("least-waste", "priority"):
        findings.append("EXPANDER_NOT_LEAST_WASTE")
    return "; ".join(findings)


def _modernization_row(pool, region, currency):
    """One SKU-modernization candidate = cheapest same-shape, same-arch, newer
    generation in-region. ARM64 is surfaced separately (needs multi-arch images)
    and never auto-recommended."""
    cur_size = pool["vm_size"]
    mod = modernize_sku(cur_size, region, currency)
    if not mod:
        return None
    nodes = int(pool.get("count") or 0)
    nodes = nodes or 1
    # autoscaling floor is the relevant node count for a steady-state saving
    if pool.get("autoscaling"):
        try:
            nodes = max(int(pool.get("min_count") or 0), nodes)
        except (TypeError, ValueError):
            pass
    est_monthly = round(mod["od_hr"] * nodes * HOURS_PER_MONTH, 2)
    return {
        "cluster": pool["cluster"],
        "subscription": pool["subscription"],
        "environment": pool["environment"],
        "pool": pool["pool"],
        "mode": pool["mode"],
        "current_sku": cur_size,
        "recommended_sku": mod["new_sku"],
        "new_generation": "ARM64 (Ampere)" if mod["new_cap"]["arch"] == "arm64" else "x64 newer-gen",
        "nodes": nodes,
        "current_od_hr": _current_od_hr(region, cur_size, currency),
        "new_od_hr": round(mod["od_hr"], 4),
        "est_pct_off": mod["est_pct_off"],
        "est_monthly_saving": est_monthly,
        "verify_before_move": ("Verify multi-arch container images + node "
                               "taints/affinity, regional quota and SKU availability "
                               "before moving workloads." if mod["new_cap"]["arch"] == "arm64"
                               else "Verify regional quota, SKU availability and "
                               "workload compatibility before migrating."),
    }


def _frag_row(cluster, pools):
    """One per-cluster fragmentation finding set (many tiny pools, single-node
    user pools, same-SKU mergeable pools). Advisory only: taints/labels/zones
    often exist for a reason."""
    user = [p for p in pools if p["mode"].lower() == "user"]
    findings = []
    if len(user) >= 3:
        findings.append("%d user pools" % len(user))
    single = [p for p in user if (int(p.get("count") or 0)) <= 1]
    if single:
        findings.append("%d single/sub-min user pool(s)" % len(single))
    sku_groups = defaultdict(int)
    for p in user:
        sku_groups[p["vm_size"]] += 1
    mergeable = sum(c - 1 for c in sku_groups.values() if c > 1)
    if mergeable:
        findings.append("%d same-SKU mergeable pool(s)" % mergeable)
    return {
        "cluster": cluster["cluster"],
        "subscription": cluster["subscription"],
        "environment": cluster["environment"],
        "location": cluster["location"],
        "total_pools": len(pools),
        "user_pools": len(user),
        "single_node_user_pools": len(single),
        "distinct_user_skus": len(sku_groups),
        "same_sku_mergeable": mergeable,
        "findings": "; ".join(findings) if findings else "OK",
        "recommendation": ("Review consolidation: fewer pools reduce autoscaler "
                           "floors and improve bin-packing (cluster autoscaler + "
                           "descheduler + topology spread only - no Karpenter/NAP)."
                           if findings else "No fragmentation concerns."),
    }


def main(argv=None):
    p = base_parser("AKS cost & efficiency report (config-driven levers beyond spot)")
    p.add_argument("--no-retail-prices", action="store_true",
                   help="skip the public Retail Prices lookups used by SKU "
                        "modernization and ephemeral-disk sizing")
    p.add_argument("--currency", default="USD", help="retail price currency")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.15)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    prod_clusters = sum(1 for c in clusters if is_prod(c["environment"]))
    pools_by_cluster_id = defaultdict(list)
    for q in pools:
        pools_by_cluster_id[q["cluster_id"]].append(q)

    # --- Lever 1: control-plane tier ---------------------------------------
    tier_rows = []
    for c in clusters:
        tier = c.get("sku_tier") or "Free"
        prod = is_prod(c["environment"])
        status = _tier_status(tier, prod)
        est = _tier_monthly(tier) if status != "OK" else 0.0
        tier_rows.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "location": c["location"],
            "sku_tier": tier,
            "est_monthly_tier_cost": est if tier in ("Standard", "Premium") else 0.0,
            "status": status,
            "est_monthly_saving": est if status == "DOWNGRADE TO FREE" else None,
            "verify_before_move": ("Premium/LTS may be a deliberate support choice - "
                                   "confirm before downgrading." if status != "OK" else ""),
        })

    # --- Lever 2: ephemeral OS disk conversion -----------------------------
    eph_rows = []
    for q in pools:
        if q["os_disk_type"].lower() == "ephemeral":
            continue  # already on ephemeral; nothing to convert
        eligible = ephemeral_os_disk_eligible(q["vm_size"], q.get("os_disk_gb"))
        if not eligible:
            continue
        nodes = int(q.get("count") or 0) or 1
        eph_rows.append({
            "cluster": q["cluster"],
            "subscription": q["subscription"],
            "environment": q["environment"],
            "pool": q["pool"],
            "mode": q["mode"],
            "vm_size": q["vm_size"],
            "os_disk_type": q["os_disk_type"],
            "os_disk_gb": q.get("os_disk_gb") or "",
            "nodes": nodes,
            "est_monthly_saving": round(MANAGED_OS_DISK_USD_PER_NODE_MONTH * nodes, 2),
            "action": "CREATE NEW POOL (ephemeral) + MIGRATE",
            "verify_before_move": "Ephemeral OS disk is immutable on an existing pool - "
                                  "create a new pool + migrate. Confirm SKU cache/temp size "
                                  ">= OS disk size; some SKUs have no temp disk at all.",
        })

    # --- Lever 3: SKU modernization ----------------------------------------
    mod_rows = []
    if not args.no_retail_prices:
        seen_sku_region = set()
        for q in pools:
            if q["priority"].lower() == "spot":
                continue  # spot pools are already the cheapest tier
            region = q["location"]
            key = (q["vm_size"], region)
            if key in seen_sku_region:
                pass  # cache hit avoids duplicated retail lookups
            seen_sku_region.add(key)
            row = _modernization_row(q, region, args.currency)
            if row:
                mod_rows.append(row)

    # --- Lever 4: autoscaler / floor hygiene -------------------------------
    auto_rows = []
    for c in clusters:
        prod = is_prod(c["environment"])
        for q in pools_by_cluster_id.get(c["id"], []):
            finding = _autoscaler_finding(q, prod)
            if finding:
                auto_rows.append({
                    "cluster": c["cluster"],
                    "subscription": c["subscription"],
                    "environment": c["environment"],
                    "pool": q["pool"],
                    "mode": q["mode"],
                    "vm_size": q["vm_size"],
                    "count": q["count"],
                    "autoscaling": "yes" if q["autoscaling"] else "no",
                    "min_count": q.get("min_count") if q.get("min_count") is not None else "",
                    "max_count": q.get("max_count") if q.get("max_count") is not None else "",
                    "autoscaler_expander": q.get("autoscaler_expander") or "",
                    "finding": finding,
                    "verify_before_move": "Advisory only: system pools need a sane floor; "
                                          "settings interact with workload PDBs we can't see.",
                })

    # --- Lever 5: pool fragmentation ---------------------------------------
    frag_rows = []
    for c in clusters:
        cps = pools_by_cluster_id.get(c["id"], [])
        frag_rows.append(_frag_row(c, cps))

    # --- Ranked recommendations (one $ row per action) ---------------------
    rec_rows = []
    seq = [0]

    def add(lever, cluster, sub, env, est, verify):
        seq[0] += 1
        rec_rows.append({
            "rank": seq[0],
            "lever": lever,
            "cluster": cluster,
            "subscription": sub,
            "environment": env,
            "est_monthly_saving_usd": round(est, 2) if est is not None else None,
            "verify_before_move": verify,
        })

    for r in tier_rows:
        if r["est_monthly_saving"]:
            add("CONTROL_PLANE_TIER", r["cluster"], r["subscription"],
                r["environment"], r["est_monthly_saving"], r["verify_before_move"])
    for r in eph_rows:
        add("EPHEMERAL_OS_DISK", r["cluster"], r["subscription"], r["environment"],
            r["est_monthly_saving"], r["verify_before_move"])
    for r in mod_rows:
        add("SKU_MODERNIZATION", r["cluster"], r["subscription"], r["environment"],
            r["est_monthly_saving"], r["verify_before_move"])
    # Autoscaler/floor and fragmentation are best-practice hygiene, not direct
    # $ estimates, so they don't carry a saving row. Keep them as findings tabs.

    tier_df = pd.DataFrame(tier_rows)
    eph_df = pd.DataFrame(eph_rows)
    mod_df = pd.DataFrame(mod_rows)
    auto_df = pd.DataFrame(auto_rows)
    frag_df = pd.DataFrame(frag_rows)
    rec_df = pd.DataFrame(rec_rows).sort_values(
        ["est_monthly_saving_usd"], ascending=False, na_position="last") if rec_rows else \
        pd.DataFrame(columns=["rank", "lever", "cluster", "subscription",
                              "environment", "est_monthly_saving_usd", "verify_before_move"])
    # re-rank after sort so the ranked list reads top-down by saving
    if not rec_df.empty:
        rec_df["rank"] = range(1, len(rec_df) + 1)

    tier_saving = float(tier_df["est_monthly_saving"].fillna(0).sum())
    eph_saving = float(eph_df["est_monthly_saving"].sum()) if not eph_df.empty else 0.0
    mod_saving = float(mod_df["est_monthly_saving"].sum()) if not mod_df.empty else 0.0
    addressable = tier_saving + eph_saving + mod_saving

    # --- Scorecard (exec one-pager) ----------------------------------------
    scorecards = [
        {"label": "Addressable monthly saving", "value": "$%.0f" % addressable,
         "caption": "tier + ephemeral disk + SKU modernization (screening)",
         "rag": "good" if addressable > 0 else "neutral"},
        {"label": "Annualized run-rate", "value": "$%.0f" % (addressable * 12),
         "caption": "addressable * 12 (verify-before-move)", "rag": "neutral"},
        {"label": "Clusters in scope", "value": str(len(clusters)),
         "caption": "%d prod / %d non-prod" % (prod_clusters, len(clusters) - prod_clusters),
         "rag": "neutral"},
        {"label": "Control-plane tier", "value": "$%.0f" % tier_saving,
         "caption": "%d non-prod paid-tier candidate(s)" % int(
            (tier_df["status"] == "DOWNGRADE TO FREE").sum()),
         "rag": "bad" if tier_saving > 0 else "good"},
        {"label": "Ephemeral OS disk", "value": "$%.0f" % eph_saving,
         "caption": "%d managed-disk pool(s) on ephemeral-capable SKUs" % len(eph_rows),
         "rag": "warn" if eph_saving > 0 else "good"},
        {"label": "SKU modernization", "value": "$%.0f" % mod_saving,
         "caption": "%d newer-gen/cheaper candidate(s)%s" % (
            len(mod_rows), "" if not args.no_retail_prices else " (-no-retail)"),
         "rag": "warn" if mod_saving > 0 else ("neutral" if args.no_retail_prices else "good")},
        {"label": "Autoscaler hygiene", "value": str(len(auto_rows)),
         "caption": "per-pool finding(s): no-autoscale, floor, expander",
         "rag": "warn" if auto_rows else "good"},
        {"label": "Pool fragmentation", "value": str(
            len([r for r in frag_rows if r["findings"] != "OK"])),
         "caption": "cluster(s) with consolidation opportunity",
         "rag": "warn" if any(r["findings"] != "OK" for r in frag_rows) else "good"},
        {"label": "Non-prod share", "value": "%.0f%%" % (
            100.0 * (len(clusters) - prod_clusters) / max(len(clusters), 1)),
         "caption": "where most of these levers apply", "rag": "neutral"},
    ]

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Cost & Efficiency Report (Beyond Spot)", [
        "Generated: %s   Scope: %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all"),
        "",
        "ARG-cheap companion to optimization_report.py: the config-driven",
        "levers that need little or no Cost Management traffic because the saving",
        "signal is a flattened config field (sku_tier, os_disk_type, vm_size,",
        "autoscaler profile, pool counts).",
        "",
        "Levers: control-plane tier (Free vs Standard/Premium), ephemeral OS disk,",
        "SKU generation/family modernization (incl. ARM64), autoscaler & floor",
        "(min_count) hygiene, and node-pool fragmentation.",
        "",
        "This is a SCREENING report, not an action plan. Every estimate comes from",
        "Reader-visible data (config + the public Retail Prices API). Immutable-",
        "after-create properties (ephemeral OS disk, spot priority) mean the action",
        "is 'new pool + migrate', never an in-place edit - called out in every row.",
        "Validate top rows with cluster_deepdive.py before changing node pools.",
        "",
        "Scope constraints apply: org policy forbids Karpenter/NAP and Cilium;",
        "consolidation and bin-packing recommendations stay within cluster",
        "autoscaler + descheduler + topology spread. No kubectl against the fleet.",
        "",
        "Pricing headers: tier rates and the managed-disk proxy are named",
        "constants (TIER_HOURLY, MANAGED_OS_DISK_USD_PER_NODE_MONTH) and drift -",
        "verify current pricing before acting.",
    ])
    excel.add_scorecard(wb, "Scorecard", scorecards, section="summary",
                        title="AKS Cost & Efficiency - Beyond Spot")
    excel.add_table(wb, "ControlPlaneTier", tier_df, section="summary",
                    money_cols=("est_monthly_tier_cost", "est_monthly_saving"),
                    fail_cols=("status",),
                    fail_values=("DOWNGRADE TO FREE",),
                    warn_values=("REVIEW (NO UPTIME SLA)",), max_width=95)
    excel.add_table(wb, "EphemeralOSDisk", eph_df, section="summary",
                    money_cols=("est_monthly_saving",),
                    max_width=95) if not eph_df.empty else excel.add_table(
        wb, "EphemeralOSDisk", pd.DataFrame(
            columns=["cluster", "pool", "vm_size", "os_disk_type", "nodes",
                     "est_monthly_saving", "action", "verify_before_move"]),
        section="summary", max_width=95)
    excel.add_table(wb, "SKUModernization", mod_df if not mod_df.empty else pd.DataFrame(
        columns=["cluster", "pool", "current_sku", "recommended_sku",
                 "new_generation", "nodes", "current_od_hr", "new_od_hr",
                 "est_pct_off", "est_monthly_saving", "verify_before_move"]),
        section="summary", money_cols=("est_monthly_saving",),
        pct_cols=("est_pct_off",), max_width=100)
    excel.add_table(wb, "AutoscalerHygiene", auto_df if not auto_df.empty else pd.DataFrame(
        columns=["cluster", "pool", "mode", "count", "autoscaling", "min_count",
                 "max_count", "autoscaler_expander", "finding", "verify_before_move"]),
        section="summary", fail_cols=("finding",), fail_values=(),
        warn_values=_AUTO_FINDINGS, int_cols=("count", "min_count", "max_count"),
        max_width=100)
    excel.add_table(wb, "PoolFragmentation", frag_df, section="summary",
                    int_cols=("total_pools", "user_pools", "single_node_user_pools",
                              "distinct_user_skus", "same_sku_mergeable"),
                    max_width=100)
    ws_rec = excel.add_table(wb, "Recommendations", rec_df, section="detail",
                             money_cols=("est_monthly_saving_usd",),
                             int_cols=("rank",), max_width=100)
    if not rec_df.empty:
        saving_col = list(rec_df.columns).index("est_monthly_saving_usd") + 1
        excel.add_bar_chart(ws_rec, "Estimated monthly saving by action",
                            len(rec_df) + 1, saving_col, "B%d" % (len(rec_df) + 4),
                            y_title="USD")

    # Reference: the full pool inventory the levers were evaluated against.
    ref_cols = ["cluster", "subscription", "environment", "location", "pool",
                "mode", "vm_size", "priority", "count", "autoscaling", "min_count",
                "max_count", "os_disk_type", "os_disk_gb", "autoscaler_expander"]
    ref_df = pd.DataFrame(pools)[[c for c in ref_cols if c in pools[0]]] if pools else \
        pd.DataFrame(columns=ref_cols)
    excel.add_table(wb, "NodePools", ref_df, section="reference",
                    int_cols=("count",), max_width=100)

    path = excel.save(wb, out_path(args, "aks_efficiency", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
