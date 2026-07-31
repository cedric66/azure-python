"""Spot node eviction risk: VMSS preemption snapshot + churn with remediation.

Layered report combining two Reader-scope signals:
1. PRIMARY: Azure Resource Graph healthresources VirtualMachinePreempted annotations
   (current, ephemeral snapshot; annotation vanishes after node replacement)
2. SECONDARY: Activity Log VMSS delete/deallocate churn (durable but noisy; mixes
   eviction with autoscale-down; ~90d retention)
3. DISCRIMINATOR: pool spotMaxPrice config (-1 = capacity-reclaim only; >0 = price
   eviction also possible; None/non-spot = n/a)

4. SOLUTION: SpotResources (ARG) banded eviction rate per SKU/region -> same-size
   in-region SKUs with a lower band (the swap-candidate columns); optional Spot
   Placement Score API (--placement-score, preview) for a forward-looking
   High/Med/Low confirmation.

The SpotResources band is also a RISK input, not just a solution one: the health
snapshot is ephemeral, so a pool parked on a 15-20/20+ SKU reads as risky even
when no annotation is currently live.

Tabs: ReadMe (method + limitations), Scorecard (KPI cards), SpotPoolRisk (the
report: one row per spot pool - config, both observed signals, the SKU eviction
band, the verdict and the swap candidate), ChurnTrend, RemediationGuide, then
reference EvictionRates and RawEvidence.

Usage:
  python aks_report.py spot-eviction --all
  python aks_report.py spot-eviction --env dev --days 30
  python aks_report.py spot-eviction --nonprod --only-spot-clusters
  python aks_report.py spot-eviction --cluster aks-prod-01 --no-eviction-scan
  python aks_report.py spot-eviction --all --placement-score
"""
import re
from collections import defaultdict

import pandas as pd

from azrep import arg, excel
from azrep.armextras import (azure_sku, retail_vm_prices, sku_capabilities,
                            spot_placement_score, vmss_churn_events)
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import (base_parser, is_prod, load_subscriptions, out_path,
                        pick_scope)

VMSS_RE = re.compile(r"^aks-([a-z0-9]+)-[a-z0-9]+-vmss$", re.I)
DAYS_DEFAULT = 14
UNATTRIBUTED = "(unattributed)"


def pool_from_vmss_name(vmss_name):
    """AKS names a pool's scale set aks-<pool>-<nodepool-hash>-vmss. Lowercased:
    the regex is case-insensitive but pool names compare lowercase everywhere."""
    m = VMSS_RE.match(vmss_name or "")
    if m:
        return m.group(1).lower()
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


def _utc_timestamps(values):
    """Parse each timestamp independently and normalise the result to UTC."""
    return pd.to_datetime(pd.Series(list(values), dtype=object), errors="coerce",
                          utc=True, format="mixed").dropna()


def age_days(values, now=None):
    """Whole days since the newest parseable timestamp in `values`, or None.
    ARG's occurredTime is normally UTC-aware, but a naive or mixed-offset value
    would otherwise raise on the aware/naive subtraction, so normalise to UTC.
    format="mixed" parses each value on its own terms - without it pandas infers
    ONE format from the first element and silently coerces the rest to NaT, which
    would drop the newest timestamp and understate the age."""
    ts = _utc_timestamps(values)
    if ts.empty:
        return None
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    return max(int((now - ts.max()).days), 0)


def zone_count(pool):
    """Number of availability zones on a pool. fleet flattens zones as a
    comma-joined string ("1,2,3"); 0 means the pool is non-zonal."""
    raw = str(pool.get("zones") or "")
    return len([z for z in re.split(r"[,\s]+", raw) if z])


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


def _risk_band(has_preemption, preempt_age_days, churn_ops_per_day, price_capped,
               is_prod_env, eviction_band=None, zones=0):
    """Additive reliability score -> HIGH/MED/LOW band + reason string.

    Drivers (preemption, churn, an elevated SKU eviction band) decide whether a
    pool is scored at all; prod/price-cap/single-zone are modifiers on top. Note
    the asymmetry with band_rank's use in best_alternative: there an unknown
    band ranks WORST (never recommend what we cannot read), here it scores ZERO
    (a data gap is not evidence of risk)."""
    rank = band_rank(eviction_band)
    band_elevated = rank in (2, 3, 4)
    if not has_preemption and churn_ops_per_day == 0 and not band_elevated:
        return "LOW", "No preemption, churn, or elevated eviction rate"
    score, reasons = 0, []
    if rank in (3, 4):
        score += 2
        reasons.append("high eviction-rate SKU (%s%%)" % eviction_band)
    elif rank == 2:
        score += 1
        reasons.append("elevated eviction-rate SKU (%s%%)" % eviction_band)
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
    if zones == 1:
        score += 1
        reasons.append("single-zone spot pool")
    band = "HIGH" if score >= 4 else "MED" if score >= 2 else "LOW"
    return band, "; ".join(reasons)


def eviction_rows(health_rows, rg_map):
    """Flatten healthresources annotations, attributing each to a cluster via the
    node RG in the target resource id and to a pool via the VMSS name."""
    rows = []
    for h in health_rows:
        rid = h.get("targetResourceId", "").lower()
        node_rg = rg_from_resource_id(rid)
        info = rg_map.get((h.get("subscriptionId"), node_rg.lower()), {})
        rows.append({
            "subscription": info.get("subscription", ""),
            "cluster_id": info.get("cluster_id", "") or "(unmatched)",
            "cluster_name": info.get("cluster", ""),
            "pool_name": pool_from_vmss_name(extract_vmss_name(rid)),
            "instance_id": extract_instance_id(rid),
            "vmss_name": extract_vmss_name(rid),
            "occurred_time": h.get("occurredTime", ""),
            "annotation_context": h.get("annotationContext", ""),
            "annotation_category": h.get("annotationCategory", ""),
            "annotation_summary": h.get("annotationSummary", ""),
        })
    return rows


def preemption_by_pool(eviction_df):
    """(cluster_id, pool) -> roll-up of the live preemption snapshot. Folded into
    the SpotPoolRisk row for that pool; the raw annotations stay on RawEvidence.
    Instance IDs restart at 0 per scale set, so count within the VMSS."""
    out = {}
    if eviction_df.empty:
        return out
    for (cid, pool), grp in eviction_df.groupby(["cluster_id", "pool_name"], sort=False):
        times = _utc_timestamps(grp["occurred_time"])
        out[(cid, pool)] = {
            "Preempted Instances": int(grp.drop_duplicates(
                subset=["vmss_name", "instance_id"]).shape[0]),
            "First Seen": times.min().strftime("%Y-%m-%d %H:%M") if not times.empty else "",
            "Last Seen": times.max().strftime("%Y-%m-%d %H:%M") if not times.empty else "",
            "Age (days)": age_days(grp["occurred_time"]) if not times.empty else None,
            "Contexts": ", ".join(sorted({str(v) for v in grp["annotation_context"] if v})),
        }
    return out


def churn_daily_rows(churn_df, days):
    """Fleet-wide daily VMSS churn (the ChurnTrend series). Per-pool rates live
    on SpotPoolRisk; a per-cluster pivot would be unusable at fleet width."""
    if churn_df.empty:
        return []
    df = churn_df.copy()
    df["date"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True).dt.date
    df = df[df["date"].notna()]
    if df.empty:
        return []
    # Pool names are only unique inside a cluster ("spot" is common), so count
    # cluster/pool pairs rather than bare names in the fleet-wide daily roll-up.
    df["pool_key"] = list(zip(df["cluster_id"], df["pool_name"]))
    agg = df.groupby("date").agg(ops=("timestamp", "size"),
                                 clusters=("cluster_id", "nunique"),
                                 pools=("pool_key", "nunique")).reset_index()
    agg = agg.sort_values("date")
    return [{"Date": str(r.date), "Churn Ops": int(r.ops),
             "Clusters Affected": int(r.clusters), "Pools Affected": int(r.pools)}
            for r in agg.itertuples(index=False)]


def pool_churn_rates(churn_df, days):
    """(cluster_id, pool) -> ops/day, plus (cluster_id, UNATTRIBUTED) -> ops/day
    for churn whose VMSS name did not parse to a pool. Splitting per pool is what
    stops a 2-spot-pool cluster reporting the whole cluster's churn twice."""
    rates = defaultdict(float)
    if churn_df.empty:
        return rates
    span = max(days, 1)
    for (cid, pool), grp in churn_df.groupby(["cluster_id", "pool_name"], sort=False):
        rates[(cid, pool)] = len(grp) / span
    return rates


ALT_EMPTY = {"Recommended SKU": "", "Recommended Band %": "", "Arch Note": "",
             "Price Delta $/hr": "", "Placement Score": "", "SKU Swap Status": "",
             "verify_before_move": ""}


def best_alternative(vm_size, region, by_region, cur_band, placement_by_region,
                     currency):
    """Best same-vCPU/mem in-region SKU with a strictly LOWER eviction band, as the
    swap-candidate half of a SpotPoolRisk row. Candidates come from the region's own
    SpotResources rows (so they are region-available) filtered to matching
    capabilities in the static _SKU_CAP map; ARM64 is surfaced with an Arch Note but
    never silently preferred. Retail price delta is best-effort. This SCREENS - it
    does not plan the migration (spot priority is immutable, so a swap means a new
    pool + drain), hence verify_before_move on every candidate."""
    cur_key = (vm_size or "").strip().lower()
    cur_cap = sku_capabilities(vm_size)
    if not cur_cap:
        return dict(ALT_EMPTY, **{
            "SKU Swap Status": "SKU not in capability map - verify manually",
            "verify_before_move": "Confirm SKU specs + regional availability by hand"})
    if cur_band is None or band_rank(cur_band) == 99:
        return dict(ALT_EMPTY, **{
            "SKU Swap Status": "Eviction rate data unavailable"})
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
        return dict(ALT_EMPTY, **{
            "SKU Swap Status": "No lower-eviction alternative in region"
            if cur_band is not None else "Eviction rate data unavailable"})
    candidates.sort(key=lambda t: t[0])  # safest band first
    _, best_key, best_band, best_cap = candidates[0]
    best_sku = azure_sku(best_key)
    cur_od = (retail_vm_prices(region, vm_size, currency) or {}).get("od_hr")
    new_od = (retail_vm_prices(region, best_sku, currency) or {}).get("od_hr")
    return {
        "Recommended SKU": best_sku,
        "Recommended Band %": best_band,
        "Arch Note": ("needs multi-arch images (ARM64)"
                      if best_cap["arch"] != cur_cap["arch"] else ""),
        "Price Delta $/hr": (round(new_od - cur_od, 4)
                             if cur_od is not None and new_od is not None else ""),
        "Placement Score": placement_by_region.get((region, best_key), ""),
        "SKU Swap Status": "SWAP CANDIDATE",
        "verify_before_move": "Spot priority is immutable: create new pool + "
                              "drain; re-verify quota/availability/price",
    }


def pool_rows(clusters, pools_by_cluster, preempt_by_pool, churn_rates,
              eviction_rate, placement_by_region, currency):
    """THE report table: one row per spot pool, left to right - identity, config,
    both observed signals, the durable SKU eviction band, the verdict, then the
    swap candidate. Replaces what used to be four separate tabs on the same key."""
    by_region = defaultdict(dict)  # region -> {sku_key: band}
    for (sku_key, region), band in eviction_rate.items():
        by_region[region][sku_key] = band
    rows = []
    for c in clusters:
        cid = c["id"].lower()
        spot_pools = pools_by_cluster.get(cid, [])
        if not spot_pools:
            continue
        parsed_pools = {str(p["pool"]).lower() for p in spot_pools}
        # churn we could not tie to a pool is cluster-level context, shown in its
        # own column so it is never silently added onto every pool's rate
        unattributed = sum(v for (k_cid, k_pool), v in churn_rates.items()
                           if k_cid == cid and k_pool not in parsed_pools)
        for pool in spot_pools:
            pool_key = str(pool["pool"]).lower()
            snap = preempt_by_pool.get((cid, pool_key), {})
            preempt_count = int(snap.get("Preempted Instances", 0) or 0)
            preempt_age = snap.get("Age (days)") if preempt_count else None

            churn = churn_rates.get((cid, pool_key), 0.0)
            price_cap = pool.get("spot_max_price")
            price_capped = price_cap is not None and price_cap > 0
            price_cap_label = "Capacity-only" if price_cap == -1 else (
                "Price-capped (%.2f)" % price_cap if price_capped else "N/A")
            vm_size = pool.get("vm_size", "")
            region = str(pool.get("location", "")).lower()
            evband = eviction_rate.get((str(vm_size).strip().lower(), region))
            zones = zone_count(pool)

            band, reason = _risk_band(preempt_count > 0, preempt_age, churn,
                                     price_capped, is_prod(c["environment"]),
                                     eviction_band=evband, zones=zones)
            rows.append(dict({
                "Subscription": c["subscription"],
                "Cluster": c["cluster"],
                "Pool": pool["pool"],
                "Environment": c["environment"],
                "Region": region,
                "VM Size": vm_size,
                "Nodes": pool.get("count", 0),
                "Zones": zones,
                "Eviction Policy": pool.get("eviction_policy", ""),
                "Spot Max Price": price_cap if price_cap is not None else "",
                "Capacity/Bidding": price_cap_label,
                "Eviction Band %": evband if evband is not None else "(no data)",
                "Preemptions": preempt_count,
                "Last Preemption": snap.get("Last Seen", ""),
                "Preemption Age (days)": preempt_age if preempt_age is not None else "",
                "Churn (ops/day)": round(churn, 2),
                "Cluster Churn Unattributed (ops/day)": round(unattributed, 2),
                "Risk Band": band,
                "Risk Reason": reason,
            }, **best_alternative(vm_size, region, by_region, evband,
                                  placement_by_region, currency)))
    return rows


def raw_evidence_rows(eviction_df, churn_df):
    """Both raw signals in one reference tab, tagged by Source. They answer the
    same question ("what happened to this node?") at the same grain, so a reader
    chasing one pool should not have to join two sheets by hand."""
    rows = []
    if not eviction_df.empty:
        for r in eviction_df.to_dict("records"):
            detail = "; ".join(str(v) for v in (r.get("annotation_context"),
                                                r.get("annotation_category"),
                                                r.get("annotation_summary")) if v)
            rows.append({
                "Source": "Resource Health",
                "Subscription": r.get("subscription", ""),
                "Cluster": r.get("cluster_name", "") or r.get("cluster_id", ""),
                "Pool": r.get("pool_name", ""),
                "Timestamp": r.get("occurred_time", ""),
                "Resource": r.get("vmss_name", ""),
                "Instance": r.get("instance_id", ""),
                "Event": "VirtualMachinePreempted",
                "Detail": detail,
            })
    if not churn_df.empty:
        for r in churn_df.to_dict("records"):
            rows.append({
                "Source": "Activity Log",
                "Subscription": r.get("subscription", ""),
                "Cluster": r.get("cluster", ""),
                "Pool": r.get("pool_name", ""),
                "Timestamp": r.get("timestamp", ""),
                "Resource": r.get("resource", ""),
                "Instance": "",
                "Event": r.get("operation", ""),
                "Detail": r.get("status", ""),
            })
    rows.sort(key=lambda r: str(r["Timestamp"]), reverse=True)
    return rows


REMEDIATION = [
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
        "Strategy": "Lower-Eviction SKU Swap",
        "Trigger": "SpotPoolRisk shows a SWAP CANDIDATE (same-size in-region SKU, "
                  "lower eviction band)",
        "Actions": "1) Confirm the candidate SKU's quota + zone availability; "
                  "2) Create a NEW spot pool on it (priority is immutable); "
                  "3) Cordon/drain the old pool, then delete it",
        "Config Requirements": "Spare quota in the target SKU family; multi-arch images "
                              "if the candidate is ARM64",
        "Effort": "Medium (new pool + drain; disruptive, schedule it)",
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

LIMITATIONS = [
    {"Item": "VERIFY: healthresources annotation emission",
     "Description": "VMSS-Uniform pool support is undocumented. Test with known eviction."},
    {"Item": "Snapshot ephemerality",
     "Description": "Annotations vanish after node replacement; absence != no evictions. "
                    "The SKU eviction band is the durable counterweight in the risk score."},
    {"Item": "Activity Log mixes signals",
     "Description": "VMSS delete/deallocate includes both eviction and autoscale-down."},
    {"Item": "Churn attribution is best-effort",
     "Description": "Ops are tied to a pool via the VMSS name; anything that does not "
                    "parse stays in the cluster-level unattributed column."},
    {"Item": "Retention window",
     "Description": "Activity Log retained ~90 days; full history requires Log Analytics archival."},
    {"Item": "Attribution heuristic",
     "Description": "Cluster recovered from node RG path; pool from VMSS name regex."},
    {"Item": "spotMaxPrice is config-driven",
     "Description": "Current pool setting from ARM; market price not checked."},
    {"Item": "Eviction bands are coarse + regional",
     "Description": "SpotResources rates are 5%-wide buckets, region-aggregate "
                    "(not per-zone), trailing ~28 days. Screening only."},
    {"Item": "SKU candidates limited to capability map",
     "Description": "Same-vCPU/mem match uses a static _SKU_CAP map; SKUs outside it "
                    "are not proposed. Verify specs/availability/quota."},
    {"Item": "Placement Score is preview + opt-in",
     "Description": "--placement-score calls a preview API (High/Med/Low); may return "
                    "DataNotFoundOrStale. Absent unless flag is set."},
]

BAND_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2}


def scorecard_cards(risk_df, eviction_df, churn_df, spot_cluster_count,
                    swap_candidates, days):
    """KPI cards. Counts are POOL-level (risk_df is one row per spot pool) except
    the churn average, which is per spot cluster scanned - including the
    zero-churn ones, or the fleet average reads high."""
    # VMSS instance IDs restart at zero for every scale set. A bare nunique()
    # therefore undercounts the fleet whenever two pools both have instance "0".
    instance_key = ["cluster_id", "vmss_name", "instance_id"]
    total_preemptions = (len(eviction_df.drop_duplicates(subset=instance_key))
                         if not eviction_df.empty else 0)
    avg_churn = (len(churn_df) / max(spot_cluster_count, 1) / max(days, 1)
                 if not churn_df.empty else 0.0)
    high_risk = int((risk_df["Risk Band"] == "HIGH").sum()) if not risk_df.empty else 0
    capacity_reclaim = int(((risk_df["Capacity/Bidding"] == "Capacity-only") &
                            (risk_df["Churn (ops/day)"] > 0)).sum()) if not risk_df.empty else 0
    price_cap = int(((pd.to_numeric(risk_df["Spot Max Price"], errors="coerce") > 0) &
                     (risk_df["Churn (ops/day)"] > 0)).sum()) if not risk_df.empty else 0
    return [
        {"label": "Preempted Instances (Snapshot)", "value": total_preemptions,
         "caption": "Distinct instances with a live VirtualMachinePreempted annotation",
         "rag": "bad" if total_preemptions > 5 else "warn" if total_preemptions else "neutral"},
        {"label": "Avg Churn (ops/day/cluster)", "value": "%.2f" % avg_churn,
         "caption": "VMSS delete/deallocate ops per spot cluster scanned (proxy)",
         "rag": "warn" if avg_churn > 2 else "neutral"},
        {"label": "Spot Pools at HIGH Risk", "value": high_risk,
         "caption": "Prod on spot, high-eviction SKU, live preemptions or churn",
         "rag": "bad" if high_risk > 0 else "neutral"},
        {"label": "Capacity-Reclaim Candidates", "value": capacity_reclaim,
         "caption": "Pools with spotMaxPrice==-1 and observed churn",
         "rag": "warn" if capacity_reclaim > 0 else "neutral"},
        {"label": "Price-Cap Tuning Candidates", "value": price_cap,
         "caption": "Pools with spotMaxPrice>0 and observed churn",
         "rag": "warn" if price_cap > 0 else "neutral"},
        {"label": "SKU Swap Candidates", "value": swap_candidates,
         "caption": "Pools with a lower-eviction same-size SKU available in-region",
         "rag": "warn" if swap_candidates > 0 else "neutral"},
    ]


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

    pools_by_cluster = defaultdict(list)
    for p in pools:
        if str(p.get("priority", "")).lower() == "spot":
            pools_by_cluster[p["cluster_id"].lower()].append(p)
    spot_clusters = [c for c in clusters if pools_by_cluster.get(c["id"].lower())]
    log("%d cluster(s) in scope, %d with spot node pools" % (len(clusters),
                                                             len(spot_clusters)))

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

    ev_cols = ["subscription", "cluster_id", "cluster_name", "pool_name", "instance_id",
               "vmss_name", "occurred_time", "annotation_context", "annotation_category",
               "annotation_summary"]
    eviction_df = pd.DataFrame(eviction_rows(health_rows, rg_map) or None, columns=ev_cols)
    log("Preemption annotations: %d row(s), %d matched to a cluster in scope"
        % (len(eviction_df),
           int((eviction_df["cluster_id"] != "(unmatched)").sum()) if not eviction_df.empty else 0))

    churn_rows = []
    if not args.no_eviction_scan:
        for c in spot_clusters:
            nrg = c.get("node_resource_group", "")
            if not nrg:
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
                    "pool_name": pool_from_vmss_name(event.get("resource", "")),
                    "resource": event.get("resource", ""),
                    "timestamp": event.get("timestamp", ""),
                    "operation": event.get("operation", ""),
                    "status": event.get("status", ""),
                })

    churn_cols = ["cluster_id", "cluster", "subscription", "node_resource_group",
                  "pool_name", "resource", "timestamp", "operation", "status"]
    churn_df = pd.DataFrame(churn_rows or None, columns=churn_cols)
    churn_rates = pool_churn_rates(churn_df, args.days)

    log("Resource Graph: fetching spot eviction rates by SKU/region...")
    in_scope_regions = {str(p.get("location", "")).lower()
                        for ps in pools_by_cluster.values() for p in ps}
    in_scope_regions.discard("")
    eviction_rate, evrate_rows, evrate_note = {}, [], ""
    if in_scope_regions:
        raw_evrate = arg.query(session, arg.SPOT_EVICTION_RATE_KQL, list(sub_ids))
        for r in raw_evrate:
            loc = str(r.get("location", "")).lower()
            if loc not in in_scope_regions:
                continue
            sku_key = str(r.get("skuName", "")).lower()
            band = r.get("evictionRate", "")
            eviction_rate[(sku_key, loc)] = band
            evrate_rows.append({"SKU": azure_sku(sku_key), "Region": loc,
                               "Eviction Band %": band})
        log("Spot eviction rates: %d rows from ARG, %d kept after region filter "
            "(%d spot-pool region(s) in scope: %s)"
            % (len(raw_evrate), len(evrate_rows), len(in_scope_regions),
               ", ".join(sorted(in_scope_regions))))
        if not raw_evrate:
            evrate_note = ("ARG returned 0 rows for the SpotResources table. It is not "
                          "surfaced in every tenant/cloud. Isolate with: az graph query "
                          "-q \"SpotResources | where type =~ "
                          "'microsoft.compute/skuspotevictionrate/location' | limit 5\"")
        elif not evrate_rows:
            evrate_note = ("ARG returned %d SpotResources row(s) but none for a region "
                          "holding a spot node pool (in scope: %s). Nothing to report; "
                          "the fleet's spot pools live elsewhere."
                          % (len(raw_evrate), ", ".join(sorted(in_scope_regions))))
    else:
        evrate_note = ("No spot node pool in scope, so the SpotResources query was "
                      "skipped (unfiltered it returns every SKU in every region). Note "
                      "RawEvidence can still show preemptions: that query is fleet-wide "
                      "and also catches non-AKS spot VMs and pools that no longer exist "
                      "- those rows read as cluster '(unmatched)'.")
        log(evrate_note)

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

    # One row per spot pool: config, both observed signals, the durable eviction
    # band, the verdict and the swap candidate. These were four tabs on the same
    # key (cluster_id, pool) and forced the reader to join them by hand.
    pool_cols = ["Subscription", "Cluster", "Pool", "Environment", "Region", "VM Size",
                 "Nodes", "Zones", "Eviction Policy", "Spot Max Price",
                 "Capacity/Bidding", "Eviction Band %", "Preemptions",
                 "Last Preemption", "Preemption Age (days)", "Churn (ops/day)",
                 "Cluster Churn Unattributed (ops/day)", "Risk Band", "Risk Reason",
                 "Recommended SKU", "Recommended Band %", "Arch Note",
                 "Price Delta $/hr", "Placement Score", "SKU Swap Status",
                 "verify_before_move"]
    risk_df = pd.DataFrame(
        pool_rows(clusters, pools_by_cluster, preemption_by_pool(eviction_df),
                  churn_rates, eviction_rate, placement_by_region, args.currency) or None,
        columns=pool_cols)
    if not risk_df.empty:
        risk_df = risk_df.sort_values(
            by=["Risk Band", "Churn (ops/day)", "Preemptions"],
            key=lambda x: x.map(BAND_ORDER) if x.name == "Risk Band" else x,
            ascending=[True, False, False])
    swap_candidates = (int((risk_df["SKU Swap Status"] == "SWAP CANDIDATE").sum())
                       if not risk_df.empty else 0)

    trend_cols = ["Date", "Churn Ops", "Clusters Affected", "Pools Affected"]
    trend_df = pd.DataFrame(churn_daily_rows(churn_df, args.days) or None,
                            columns=trend_cols)

    # A blank reference sheet reads as "no risk"; say which of the three causes it
    # actually was (no spot pool in scope / table empty / nothing in our regions).
    evrate_df = pd.DataFrame(evrate_rows or None, columns=["SKU", "Region", "Eviction Band %"])
    if evrate_df.empty:
        evrate_df = pd.DataFrame([{"SKU": "(no data)", "Region": "",
                                   "Eviction Band %": evrate_note}])

    ev_cols_raw = ["Source", "Subscription", "Cluster", "Pool", "Timestamp",
                   "Resource", "Instance", "Event", "Detail"]
    evidence_df = pd.DataFrame(raw_evidence_rows(eviction_df, churn_df) or None,
                               columns=ev_cols_raw)

    wb = excel.new_workbook()
    excel.add_readme(wb, "Spot Node Eviction Risk", [
        "This report identifies spot-node preemption risk across your AKS fleet using "
        "two observed signals plus one durable, forward-looking one:",
        "",
        "PRIMARY SIGNAL: Azure Resource Graph healthresources table tracks "
        "VirtualMachinePreempted annotations - a named platform-initiated eviction "
        "event. This is a current, EPHEMERAL snapshot: the annotation vanishes after "
        "the node is replaced, so absence does not mean no evictions are happening.",
        "",
        "SECONDARY SIGNAL: Activity Log VMSS delete/deallocate operations (node RG, "
        "~90-day retention). This is durable but NOISY: the count mixes true spot "
        "evictions with normal autoscaler-driven scale-downs. It is a proxy only. Ops "
        "are attributed to a pool via the VMSS name; whatever does not parse is shown "
        "separately as cluster-level unattributed churn, never spread across pools.",
        "",
        "DISCRIMINATOR: pool spotMaxPrice configuration:",
        "  -1 = Capacity-reclaim only (Azure deallocates when capacity needed)",
        "  >0 = Price-capped bidding (Azure deallocates if your bid < spot price)",
        "  None/non-spot = Not applicable",
        "",
        "RISK BAND: an additive score. An elevated SKU eviction band, live preemptions "
        "or observed churn make a pool eligible for scoring; prod-on-spot, a price cap "
        "and single-zone placement then add to it. Because the annotation snapshot is "
        "ephemeral, a pool parked on a 15-20/20+ SKU scores as risky even with no live "
        "annotation - that is the intended counterweight to the blind spot below.",
        "",
        "VERIFY: healthresources emission on VMSS-Uniform pools is undocumented. "
        "Test with a known preemption to confirm the annotation captures it.",
        "",
        "SOLUTION SIGNAL (the swap-candidate columns on SpotPoolRisk): SpotResources "
        "(ARG) publishes a banded eviction rate (0-5 / 5-10 / 10-15 / 15-20 / 20+ %, "
        "next-hour chance) per VM SKU per region. For each spot pool we find "
        "same-vCPU/mem in-region SKUs with a LOWER band and surface the best one as a "
        "swap candidate (price delta from Retail Prices). With --placement-score we "
        "also call the Spot Placement Score API (preview, High/Medium/Low, "
        "forward-looking) to confirm the top pick. A swap is disruptive (spot priority "
        "is immutable -> new pool + drain): this SCREENS, it is not a migration plan. "
        "Every row carries verify_before_move.",
        "",
        "TABS: Scorecard (KPI cards) -> SpotPoolRisk (the report: one row per spot "
        "pool) -> ChurnTrend (daily churn series) -> RemediationGuide (what to do) -> "
        "EvictionRates + RawEvidence (reference).",
        "",
        "LIMITATIONS:",
    ] + ["  - %s: %s" % (lim["Item"], lim["Description"]) for lim in LIMITATIONS])

    excel.add_scorecard(wb, "Scorecard",
                        scorecard_cards(risk_df, eviction_df, churn_df,
                                        len(spot_clusters), swap_candidates, args.days),
                        section="summary", per_row=3, title=None)

    ws_risk = excel.add_table(wb, "SpotPoolRisk", risk_df, section="summary",
                             int_cols=("Preemptions", "Nodes", "Zones"),
                             fail_cols=("Risk Band",), fail_values=("HIGH",),
                             warn_values=("MED",))
    if len(risk_df) > 1:
        excel.add_bar_chart(ws_risk, "VMSS churn by spot pool (ops/day, proxy)",
                            len(risk_df) + 1, pool_cols.index("Churn (ops/day)") + 1,
                            "AB2", y_title="ops/day",
                            cat_col=pool_cols.index("Pool") + 1)
    ws_trend = excel.add_table(wb, "ChurnTrend", trend_df, section="summary",
                              int_cols=("Churn Ops", "Clusters Affected", "Pools Affected"))
    if len(trend_df) > 1:
        excel.add_line_chart(ws_trend, "Daily VMSS churn ops (eviction proxy)",
                             len(trend_df) + 1, 2, 2, "F2", y_title="ops")
    excel.add_table(wb, "RemediationGuide", pd.DataFrame(REMEDIATION), section="detail")
    excel.add_table(wb, "EvictionRates", evrate_df, section="reference")
    excel.add_table(wb, "RawEvidence", evidence_df, section="reference")

    path = excel.save(wb, out_path(args, "aks_spot_eviction", env_filter))
    log("Report written: %s" % path)
    return path


if __name__ == "__main__":
    main()
