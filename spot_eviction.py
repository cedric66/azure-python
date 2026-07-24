"""Spot node eviction risk: VMSS preemption snapshot + churn with remediation.

Layered report combining two Reader-scope signals:
1. PRIMARY: Azure Resource Graph healthresources VirtualMachinePreempted annotations
   (current, ephemeral snapshot; annotation vanishes after node replacement)
2. SECONDARY: Activity Log VMSS delete/deallocate churn (durable but noisy; mixes
   eviction with autoscale-down; ~90d retention)
3. DISCRIMINATOR: pool spotMaxPrice config (-1 = capacity-reclaim only; >0 = price
   eviction also possible; None/non-spot = n/a)

4. SOLUTION: SpotResources (ARG) banded eviction rate per SKU/region -> same-size
   in-region SKUs with a lower band (SkuAlternatives); optional Spot Placement Score
   API (--placement-score, preview) for a forward-looking High/Med/Low confirmation.

Story tabs: ReadMe, Scorecard (KPI cards), RiskAssessment, SkuAlternatives,
EvictionSnapshot, ChurnTrend, RemediationGuide. Then reference: SpotPoolInventory,
EvictionRates, RawHealthResources, RawActivityLog, Limitations.

Usage:
  python spot_eviction.py --all
  python spot_eviction.py --env dev --days 30
  python spot_eviction.py --nonprod --only-spot-clusters
  python spot_eviction.py --cluster aks-prod-01 --no-eviction-scan
  python spot_eviction.py --all --placement-score
"""
import datetime as dt
import re
from collections import defaultdict

import pandas as pd

from azrep import arg, excel
from azrep.armextras import (azure_sku, retail_vm_prices, sku_capabilities,
                            spot_placement_score, vmss_churn_events)
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import (base_parser, is_prod, load_subscriptions, out_path,
                        pick_scope, resolve_env_detail)

VMSS_RE = re.compile(r"^aks-([a-z0-9]+)-[a-z0-9]+-vmss$", re.I)
DAYS_DEFAULT = 14


def pool_from_vmss_name(vmss_name):
    m = VMSS_RE.match(vmss_name or "")
    if m:
        return m.group(1)
    return "(unparsed)"


def rg_from_resource_id(resource_id):
    parts = str(resource_id or "").split("/")
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def extract_instance_id(resource_id):
    parts = str(resource_id or "").split("/")
    for i, p in enumerate(parts):
        if p.lower() == "virtualmachines" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def extract_vmss_name(resource_id):
    parts = str(resource_id or "").split("/")
    for i, p in enumerate(parts):
        if p.lower() == "virtualmachinescalesets" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _risk_band(has_preemption, preempt_age_days, churn_ops_per_day, price_capped,
               is_prod_env):
    """Additive reliability score -> HIGH/MED/LOW band + reason string."""
    score, reasons = 0, []
    if not has_preemption and churn_ops_per_day == 0:
        return "LOW", "No recent preemption or churn"
    if is_prod_env:
        score += 2
        reasons.append("prod on spot")
    if has_preemption:
        score += 2
        reasons.append("active preemptions")
        if preempt_age_days is not None and preempt_age_days < 7:
            score += 1
            reasons.append("recent (< 7 days)")
    if churn_ops_per_day > 0:
        score += 1
        reasons.append("VMSS churn")
        if churn_ops_per_day > 2:
            score += 1
            reasons.append("high churn (> 2 ops/day)")
    if price_capped:
        score += 1
        reasons.append("price-capped bid")
    band = "HIGH" if score >= 4 else "MED" if score >= 2 else "LOW"
    return band, "; ".join(reasons)


# SpotResources reports the eviction rate as a banded string ("0-5", "5-10",
# "10-15", "15-20", "20+"); order them so lower band == safer.
EVICTION_ORDER = {"0-5": 0, "5-10": 1, "10-15": 2, "15-20": 3, "20+": 4, "20-100": 4}


def band_rank(band):
    """Ordinal for a SpotResources eviction band; unknown sorts as WORST so we
    never recommend an alternative whose rate we cannot read."""
    if band is None or band == "":
        return 99
    return EVICTION_ORDER.get(str(band).strip(), 99)


_SCORE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _score_rank(score):
    return _SCORE_ORDER.get(str(score).strip().lower(), 9)


def sku_alternative_rows(clusters, pools_by_cluster, eviction_rate,
                         placement_by_region, region_currency):
    """One row per spot pool: its current SKU eviction band and the best
    same-vCPU/mem in-region SKU with a strictly lower band. Candidates come from
    the region's own SpotResources rows (so they are region-available) filtered to
    matching capabilities in the static _SKU_CAP map; ARM64 is surfaced but flagged
    'needs multi-arch images', never silently preferred. Retail price delta is
    best-effort. Every row carries verify_before_move - this screens, it does not
    plan the migration (spot priority is immutable so a swap = new pool + drain)."""
    rows = []
    # index the eviction table by region -> {sku_key: band}
    by_region = defaultdict(dict)
    for (sku_key, region), band in eviction_rate.items():
        by_region[region][sku_key] = band
    for c in clusters:
        cid = c["id"].lower()
        for pool in pools_by_cluster.get(cid, []):
            vm_size = pool.get("vm_size", "")
            region = str(pool.get("location", "")).lower()
            cur_key = (vm_size or "").strip().lower()
            cur_cap = sku_capabilities(vm_size)
            cur_band = eviction_rate.get((cur_key, region))
            base = {"Cluster": c["cluster"], "Pool": pool["pool"],
                    "Region": region, "Current SKU": vm_size,
                    "Current Band %": cur_band if cur_band is not None else "(no data)",
                    "Nodes": pool.get("count", 0)}
            if not cur_cap:
                rows.append(dict(base, **{
                    "Recommended SKU": "", "Recommended Band %": "",
                    "Arch Note": "", "Price Delta $/hr": "",
                    "Placement Score": "", "Status": "SKU not in capability map - verify manually",
                    "verify_before_move": "Confirm SKU specs + regional availability by hand"}))
                continue
            candidates = []
            for sku_key, band in by_region.get(region, {}).items():
                if sku_key == cur_key:
                    continue
                cap = sku_capabilities(sku_key)
                if not cap:
                    continue
                if cap["vcpu"] != cur_cap["vcpu"] or cap["mem"] != cur_cap["mem"]:
                    continue
                if band_rank(band) >= band_rank(cur_band):
                    continue  # not strictly safer than current
                candidates.append((band_rank(band), sku_key, band, cap))
            if not candidates:
                rows.append(dict(base, **{
                    "Recommended SKU": "", "Recommended Band %": "",
                    "Arch Note": "", "Price Delta $/hr": "",
                    "Placement Score": "", "Status": "No lower-eviction alternative in region"
                    if cur_band is not None else "Eviction rate data unavailable",
                    "verify_before_move": ""}))
                continue
            candidates.sort(key=lambda t: t[0])  # safest band first
            best_rank, best_key, best_band, best_cap = candidates[0]
            best_sku = azure_sku(best_key)
            arch_note = ("needs multi-arch images (ARM64)"
                         if best_cap["arch"] != cur_cap["arch"] else "")
            cur_price = retail_vm_prices(region, vm_size, region_currency) or {}
            new_price = retail_vm_prices(region, best_sku, region_currency) or {}
            cur_od = cur_price.get("od_hr")
            new_od = new_price.get("od_hr")
            delta = (round(new_od - cur_od, 4)
                     if cur_od is not None and new_od is not None else "")
            score = placement_by_region.get((region, best_key), "")
            rows.append(dict(base, **{
                "Recommended SKU": best_sku,
                "Recommended Band %": best_band,
                "Arch Note": arch_note,
                "Price Delta $/hr": delta,
                "Placement Score": score,
                "Status": "SWAP CANDIDATE",
                "verify_before_move": "Spot priority is immutable: create new pool + "
                                     "drain; re-verify quota/availability/price"}))
    return rows


def main(argv=None):
    parser = base_parser(
        "Spot node eviction risk: Resource Health annotations + Activity Log churn")
    parser.add_argument("--days", type=int, default=DAYS_DEFAULT,
                        help="Activity Log lookback in days (default %d)" % DAYS_DEFAULT)
    parser.add_argument("--only-spot-clusters", action="store_true",
                        help="Filter to clusters with a current spot node pool")
    parser.add_argument("--no-eviction-scan", action="store_true",
                        help="Skip Activity Log churn scan (snapshot only)")
    parser.add_argument("--placement-score", action="store_true",
                        help="Also call the Spot Placement Score API (preview) to "
                             "confirm recommended SKUs forward-looking (extra POST "
                             "per sub/region)")
    parser.add_argument("--currency", default="USD",
                        help="Retail Prices currency for SKU price deltas (default USD)")
    args = parser.parse_args(argv or [])

    subs = load_subscriptions(args.csv)
    sel_subs, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.0)

    log("Loading fleet...")
    clusters, pools = load_fleet(session, sel_subs, env_filter, env_keys=None)

    if args.only_spot_clusters:
        spot_cluster_ids = {p["cluster_id"].lower() for p in pools
                            if str(p.get("priority", "")).lower() == "spot"}
        clusters = [c for c in clusters if c["id"].lower() in spot_cluster_ids]
        log("Filtered to %d clusters with spot pools" % len(clusters))

    cluster_by_id = {c["id"].lower(): c for c in clusters}
    pools_by_cluster = defaultdict(list)
    for p in pools:
        if str(p.get("priority", "")).lower() == "spot":
            pools_by_cluster[p["cluster_id"].lower()].append(p)

    log("Resource Graph: fetching health resource annotations...")
    sub_ids = {c["subscription_id"] for c in clusters}
    health_rows = arg.query(session, arg.HEALTHRESOURCES_KQL, list(sub_ids))

    rg_map = {}
    for c in clusters:
        nrg = c.get("node_resource_group", "").lower()
        if nrg:
            rg_map[(c["subscription_id"], nrg)] = {
                "cluster_id": c["id"].lower(),
                "cluster": c["cluster"],
                "subscription": c["subscription"],
            }

    eviction_rows = []
    for h in health_rows:
        rid = h.get("targetResourceId", "").lower()
        vmss_name = extract_vmss_name(rid)
        instance_id = extract_instance_id(rid)
        pool_name = pool_from_vmss_name(vmss_name)
        node_rg = rg_from_resource_id(rid)
        nrg_key = (h.get("subscriptionId"), node_rg.lower())
        cluster_info = rg_map.get(nrg_key, {})
        cluster_id = cluster_info.get("cluster_id", "")

        if not cluster_id:
            cluster_id = "(unmatched)"

        eviction_rows.append({
            "subscription": cluster_info.get("subscription", ""),
            "cluster_id": cluster_id,
            "cluster_name": cluster_info.get("cluster", ""),
            "pool_name": pool_name,
            "instance_id": instance_id,
            "vmss_name": vmss_name,
            "occurred_time": h.get("occurredTime", ""),
            "annotation_context": h.get("annotationContext", ""),
            "annotation_category": h.get("annotationCategory", ""),
            "annotation_summary": h.get("annotationSummary", ""),
        })

    eviction_df = pd.DataFrame(eviction_rows) if eviction_rows else pd.DataFrame(columns=[
        "subscription", "cluster_id", "cluster_name", "pool_name", "instance_id",
        "vmss_name", "occurred_time", "annotation_context", "annotation_category",
        "annotation_summary"])

    churn_rows = []
    if not args.no_eviction_scan:
        for c in clusters:
            nrg = c.get("node_resource_group", "")
            if not nrg:
                continue
            spot_pools_in_cluster = pools_by_cluster.get(c["id"].lower(), [])
            if not spot_pools_in_cluster:
                continue
            log("[churn] %s / %s" % (c["cluster"], nrg))
            churn_data = vmss_churn_events(session, c["subscription_id"], nrg,
                                          days=args.days)
            for event in churn_data.get("rows", []):
                churn_rows.append({
                    "cluster_id": c["id"].lower(),
                    "cluster": c["cluster"],
                    "subscription": c["subscription"],
                    "node_resource_group": nrg,
                    "timestamp": event.get("timestamp", ""),
                    "operation": event.get("operation", ""),
                    "status": event.get("status", ""),
                })

    churn_df = pd.DataFrame(churn_rows) if churn_rows else pd.DataFrame(columns=[
        "cluster_id", "cluster", "subscription", "node_resource_group",
        "timestamp", "operation", "status"])

    if not churn_df.empty:
        churn_df["date"] = pd.to_datetime(churn_df["timestamp"]).dt.date
        daily_churn = churn_df.groupby(["cluster_id", "date"]).size().reset_index(
            name="ops_count")
    else:
        daily_churn = pd.DataFrame(columns=["cluster_id", "date", "ops_count"])

    risk_rows = []
    for c in clusters:
        cluster_id = c["id"].lower()
        spot_pools_in_cluster = pools_by_cluster.get(cluster_id, [])
        if not spot_pools_in_cluster:
            continue

        for pool in spot_pools_in_cluster:
            preempt_for_pool = eviction_df[
                (eviction_df["cluster_id"] == cluster_id) &
                (eviction_df["pool_name"] == str(pool["pool"]).lower())
            ]
            preempt_count = len(preempt_for_pool)

            if preempt_count > 0:
                latest_preempt = pd.to_datetime(preempt_for_pool["occurred_time"]).max()
                preempt_age_days = (dt.datetime.now(dt.timezone.utc).replace(
                    microsecond=0).astimezone(latest_preempt.tzinfo) - latest_preempt).days
            else:
                preempt_age_days = None

            churn_for_pool = daily_churn[daily_churn["cluster_id"] == cluster_id]
            churn_ops_total = churn_for_pool["ops_count"].sum() if not churn_for_pool.empty else 0
            churn_ops_per_day = churn_ops_total / max(args.days, 1)

            price_cap = pool.get("spot_max_price")
            price_capped = price_cap is not None and price_cap > 0
            price_cap_label = "Capacity-only" if price_cap == -1 else (
                "Price-capped (%.2f)" % price_cap if price_cap else "N/A")

            band, reason = _risk_band(preempt_count > 0, preempt_age_days,
                                     churn_ops_per_day, price_capped, is_prod(c["environment"]))

            risk_rows.append({
                "Cluster": c["cluster"],
                "Pool": pool["pool"],
                "Preemptions": preempt_count,
                "Preemption Age (days)": preempt_age_days if preempt_age_days is not None else "",
                "Churn (ops/day)": round(churn_ops_per_day, 2),
                "Spot Max Price": price_cap if price_cap is not None else "",
                "Capacity/Bidding": price_cap_label,
                "Risk Band": band,
                "Risk Reason": reason,
            })

    risk_df = pd.DataFrame(risk_rows) if risk_rows else pd.DataFrame(columns=[
        "Cluster", "Pool", "Preemptions", "Preemption Age (days)", "Churn (ops/day)",
        "Spot Max Price", "Capacity/Bidding", "Risk Band", "Risk Reason"])
    risk_df = risk_df.sort_values(
        by=["Risk Band", "Churn (ops/day)"],
        key=lambda x: x.map({"HIGH": 0, "MED": 1, "LOW": 2}) if x.name == "Risk Band" else -x,
        ascending=[True, False])

    remediation_rows = [
        {
            "Strategy": "Capacity-Reclaim",
            "Trigger": "spotMaxPrice == -1 with VMSS churn observed",
            "Actions": "1) Verify pool can tolerate disruption via PodDisruptionBudget; "
                      "2) Add topologySpreadConstraints with kubernetes.azure.com/agentpool key; "
                      "3) Deploy descheduler (manifests/spot/descheduler.yaml)",
            "Config Requirements": "Cluster autoscaler + descheduler + topologySpreadConstraints; "
                                  "no Karpenter/Cilium",
            "Effort": "Medium (requires workload review + manifest changes)",
        },
        {
            "Strategy": "Price-Cap Tuning",
            "Trigger": "spotMaxPrice > 0 with persistent preemptions or high churn",
            "Actions": "1) Analyze your workload's tolerance for latency/disruption; "
                      "2) Increase spotMaxPrice cap incrementally or switch to capacity-only (-1); "
                      "3) Monitor via this report on cadence matching your SLA",
            "Config Requirements": "Pool config spotMaxPrice update via AZ CLI or Terraform",
            "Effort": "Low (config change, no deployment required)",
        },
        {
            "Strategy": "Proactive Hardening",
            "Trigger": "All spot pools (baseline resilience)",
            "Actions": "1) Multi-zone spot pools + zone affinity rules; "
                      "2) Pod anti-affinity + topologySpreadConstraints; "
                      "3) Reserved instances as fallback for critical workloads; "
                      "4) Autoscaler floor (min_count) to prevent total capacity collapse",
            "Config Requirements": "topologySpreadConstraints key on kubernetes.azure.com/agentpool; "
                                  "cluster autoscaler enabled",
            "Effort": "Medium (application-aware, requires cross-team input)",
        },
    ]

    remediation_df = pd.DataFrame(remediation_rows)

    inventory_rows = []
    for c in clusters:
        cluster_id = c["id"].lower()
        for pool in pools_by_cluster.get(cluster_id, []):
            zones = pool.get("zones", "").split(", ") if pool.get("zones") else []
            inventory_rows.append({
                "Cluster": c["cluster"],
                "Pool": pool["pool"],
                "Spot Max Price": pool.get("spot_max_price", ""),
                "Node Count": pool.get("count", 0),
                "VM Size": pool.get("vm_size", ""),
                "Zones": ", ".join(zones) if zones else "",
                "Priority": pool.get("priority", ""),
            })

    inventory_df = pd.DataFrame(inventory_rows) if inventory_rows else pd.DataFrame(columns=[
        "Cluster", "Pool", "Spot Max Price", "Node Count", "VM Size", "Zones", "Priority"])

    raw_health_df = eviction_df.copy() if not eviction_df.empty else eviction_df
    raw_activity_df = churn_df.copy() if not churn_df.empty else churn_df

    log("Resource Graph: fetching spot eviction rates by SKU/region...")
    in_scope_regions = {str(p.get("location", "")).lower()
                        for ps in pools_by_cluster.values() for p in ps}
    eviction_rate = {}
    evrate_rows = []
    for r in arg.query(session, arg.SPOT_EVICTION_RATE_KQL, list(sub_ids)):
        loc = str(r.get("location", "")).lower()
        if in_scope_regions and loc not in in_scope_regions:
            continue
        sku_key = str(r.get("skuName", "")).lower()
        band = r.get("evictionRate", "")
        eviction_rate[(sku_key, loc)] = band
        evrate_rows.append({"SKU": azure_sku(sku_key), "Region": loc,
                           "Eviction Band %": band})

    placement_by_region = {}
    if args.placement_score:
        want = defaultdict(set)  # (sub_id, region) -> set of candidate sku_key
        for c in clusters:
            for pool in pools_by_cluster.get(c["id"].lower(), []):
                region = str(pool.get("location", "")).lower()
                cur_cap = sku_capabilities(pool.get("vm_size", ""))
                if not cur_cap:
                    continue
                for (sku_key, loc) in eviction_rate:
                    if loc != region:
                        continue
                    cap = sku_capabilities(sku_key)
                    if cap and cap["vcpu"] == cur_cap["vcpu"] and cap["mem"] == cur_cap["mem"]:
                        want[(c["subscription_id"], region)].add(sku_key)
        for (sub_id, region), sku_keys in want.items():
            skus = [azure_sku(k) for k in sorted(sku_keys)]
            log("[placement] %s / %s (%d SKUs)" % (sub_id, region, len(skus)))
            for ps in spot_placement_score(session, sub_id, region, skus):
                key = (str(ps.get("region", region)).lower(),
                       str(ps.get("sku", "")).lower())
                cur = ps.get("score", "")
                prev = placement_by_region.get(key)
                if prev is None or _score_rank(cur) < _score_rank(prev):
                    placement_by_region[key] = cur

    sku_alt_rows = sku_alternative_rows(clusters, pools_by_cluster, eviction_rate,
                                       placement_by_region, args.currency)
    sku_alt_df = pd.DataFrame(sku_alt_rows) if sku_alt_rows else pd.DataFrame(columns=[
        "Cluster", "Pool", "Region", "Current SKU", "Current Band %", "Nodes",
        "Recommended SKU", "Recommended Band %", "Arch Note", "Price Delta $/hr",
        "Placement Score", "Status", "verify_before_move"])
    evrate_df = pd.DataFrame(evrate_rows) if evrate_rows else pd.DataFrame(columns=[
        "SKU", "Region", "Eviction Band %"])
    swap_candidates = int((sku_alt_df["Status"] == "SWAP CANDIDATE").sum()) \
        if not sku_alt_df.empty else 0

    wb = excel.new_workbook()
    excel.add_readme(wb, "Spot Node Eviction Risk", [
        "This report identifies spot-node preemption risk across your AKS fleet using "
        "two complementary signals:",
        "",
        "PRIMARY SIGNAL: Azure Resource Graph healthresources table tracks "
        "VirtualMachinePreempted annotations — a named platform-initiated eviction "
        "event. This is a current, EPHEMERAL snapshot: the annotation vanishes after "
        "the node is replaced, so absence does not mean no evictions are happening.",
        "",
        "SECONDARY SIGNAL: Activity Log VMSS delete/deallocate operations (node RG, "
        "~90-day retention). This is durable but NOISY: the count mixes true spot "
        "evictions with normal autoscaler-driven scale-downs. It is a proxy only.",
        "",
        "DISCRIMINATOR: pool spotMaxPrice configuration:",
        "  -1 = Capacity-reclaim only (Azure deallocates when capacity needed)",
        "  >0 = Price-capped bidding (Azure deallocates if your bid < spot price)",
        "  None/non-spot = Not applicable",
        "",
        "VERIFY: healthresources emission on VMSS-Uniform pools is undocumented. "
        "Test with a known preemption to confirm annotation captures it.",
        "",
        "SOLUTION SIGNAL (SkuAlternatives): SpotResources (ARG) publishes a banded "
        "eviction rate (0-5 / 5-10 / 10-15 / 15-20 / 20+ %, next-hour chance) per VM "
        "SKU per region. For each spot pool we find same-vCPU/mem in-region SKUs with "
        "a LOWER band and surface them as swap candidates (price delta from Retail "
        "Prices). With --placement-score we also call the Spot Placement Score API "
        "(preview, High/Medium/Low, forward-looking) to confirm the top pick. A swap "
        "is disruptive (spot priority is immutable -> new pool + drain): this SCREENS, "
        "it is not a migration plan. Every row carries verify_before_move.",
    ])

    total_preemptions = len(eviction_df)
    avg_churn = (churn_df.groupby("cluster_id").size().mean() / max(args.days, 1)
                 if not churn_df.empty else 0)
    high_risk = len(risk_df[risk_df["Risk Band"] == "HIGH"]) if not risk_df.empty else 0
    capacity_reclaim_candidates = len(
        risk_df[(risk_df["Capacity/Bidding"] == "Capacity-only") & 
                (risk_df["Churn (ops/day)"] > 0)]
    ) if not risk_df.empty else 0
    price_cap_candidates = len(
        risk_df[(pd.to_numeric(risk_df["Spot Max Price"], errors="coerce") > 0) &
                (risk_df["Churn (ops/day)"] > 0)]
    ) if not risk_df.empty else 0

    scorecard_cards = [
        {"label": "Preempted Instances (Snapshot)", "value": total_preemptions,
         "caption": "Current active VirtualMachinePreempted annotations",
         "rag": "bad" if total_preemptions > 5 else "warn" if total_preemptions > 0 else "neutral"},
        {"label": "Avg Churn (ops/day)", "value": "%.2f" % avg_churn,
         "caption": "Average VMSS delete/deallocate operations per day (proxy)",
         "rag": "warn" if avg_churn > 2 else "neutral"},
        {"label": "Clusters at HIGH Risk", "value": high_risk,
         "caption": "Prod on spot, high churn, or both signals present",
         "rag": "bad" if high_risk > 0 else "neutral"},
        {"label": "Capacity-Reclaim Candidates", "value": capacity_reclaim_candidates,
         "caption": "Pools with spotMaxPrice==-1 and observed churn",
         "rag": "warn" if capacity_reclaim_candidates > 0 else "neutral"},
        {"label": "Price-Cap Tuning Candidates", "value": price_cap_candidates,
         "caption": "Pools with spotMaxPrice>0 and observed churn",
         "rag": "warn" if price_cap_candidates > 0 else "neutral"},
        {"label": "SKU Swap Candidates", "value": swap_candidates,
         "caption": "Pools with a lower-eviction same-size SKU available in-region",
         "rag": "warn" if swap_candidates > 0 else "neutral"},
    ]
    excel.add_scorecard(wb, "Scorecard", scorecard_cards, section="summary",
                       per_row=3, title=None)

    excel.add_table(wb, "RiskAssessment", risk_df, section="summary",
                   int_cols=("Preemptions",))
    excel.add_table(wb, "SkuAlternatives", sku_alt_df, section="summary",
                   int_cols=("Nodes",))
    excel.add_table(wb, "EvictionSnapshot", raw_health_df, section="summary")
    excel.add_table(wb, "ChurnTrend", churn_df.sort_values("timestamp", ascending=False),
                   section="summary")
    excel.add_table(wb, "RemediationGuide", remediation_df, section="detail")
    excel.add_table(wb, "SpotPoolInventory", inventory_df, section="reference",
                   int_cols=("Node Count",))
    excel.add_table(wb, "EvictionRates", evrate_df, section="reference")
    excel.add_table(wb, "RawHealthResources", raw_health_df, section="reference")
    excel.add_table(wb, "RawActivityLog", raw_activity_df, section="reference")

    limitations_rows = [
        {
            "Item": "VERIFY: healthresources annotation emission",
            "Description": "VMSS-Uniform pool support is undocumented. Test with known eviction.",
        },
        {
            "Item": "Snapshot ephemerality",
            "Description": "Annotations vanish after node replacement; absence ≠ no evictions.",
        },
        {
            "Item": "Activity Log mixes signals",
            "Description": "VMSS delete/deallocate includes both eviction and autoscale-down.",
        },
        {
            "Item": "Retention window",
            "Description": "Activity Log retained ~90 days; full history requires "
                          "Log Analytics archival.",
        },
        {
            "Item": "Attribution heuristic",
            "Description": "Cluster recovered from node RG path; pool from VMSS name regex.",
        },
        {
            "Item": "spotMaxPrice is config-driven",
            "Description": "Current pool setting from ARM; market price not checked.",
        },
        {
            "Item": "Eviction bands are coarse + regional",
            "Description": "SpotResources rates are 5%-wide buckets, region-aggregate "
                          "(not per-zone), trailing ~28 days. Screening only.",
        },
        {
            "Item": "SKU candidates limited to capability map",
            "Description": "Same-vCPU/mem match uses a static _SKU_CAP map; SKUs "
                          "outside it are not proposed. Verify specs/availability/quota.",
        },
        {
            "Item": "Placement Score is preview + opt-in",
            "Description": "--placement-score calls a preview API (High/Med/Low); may "
                          "return DataNotFoundOrStale. Absent unless flag is set.",
        },
    ]
    limitations_df = pd.DataFrame(limitations_rows)
    excel.add_table(wb, "Limitations", limitations_df, section="reference")

    excel.save(wb, out_path(args, "spot_eviction", env_filter))
    log("Report saved.")


if __name__ == "__main__":
    main()
