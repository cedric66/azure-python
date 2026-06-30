"""Smaller ARM read APIs: Monitor metrics, Activity Log, AKS supported versions,
and the public Azure Retail Prices API (no auth)."""
import datetime as dt
import re
import time

import requests

from .http_client import log

METRICS_API = "2018-01-01"
ACTIVITY_API = "2015-04-01"
AKS_API = "2024-05-01"


def _p95(values):
    if not values:
        return None
    v = sorted(values)
    return v[int(round(0.95 * (len(v) - 1)))]


def cluster_metrics(session, resource_id, days=14, interval="PT1H",
                    metrics=("node_cpu_usage_percentage", "node_memory_working_set_percentage"),
                    want_series=False):
    """Platform metrics (free, no Container Insights required). Returns
    {metric: {avg, p95, max, points, series?}} or {} if unavailable (e.g. stopped)."""
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    start = end - dt.timedelta(days=days)
    data = session.get(resource_id + "/providers/microsoft.insights/metrics",
                       params={"api-version": METRICS_API,
                               "metricnames": ",".join(metrics),
                               "timespan": "%s/%s" % (start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                      end.strftime("%Y-%m-%dT%H:%M:%SZ")),
                               "interval": interval,
                               "aggregation": "average,maximum"},
                       ok404=True)
    out = {}
    for m in (data or {}).get("value", []):
        pts = []
        for ts in m.get("timeseries", []):
            pts.extend(ts.get("data", []))
        avgs = [p["average"] for p in pts if p.get("average") is not None]
        maxs = [p["maximum"] for p in pts if p.get("maximum") is not None]
        rec = {"avg": sum(avgs) / len(avgs) if avgs else None,
               "p95": _p95(avgs),
               "max": max(maxs) if maxs else None,
               "points": len(avgs)}
        if want_series:
            rec["series"] = [(p["timeStamp"], p.get("average"), p.get("maximum")) for p in pts]
        out[m["name"]["value"]] = rec
    return out


def activity_events(session, subscription_id, resource_group, days=90):
    """Activity Log writes/deletes on Microsoft.ContainerService resources in a
    resource group. Retention is 90 days, which covers the 3-month cost window."""
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=min(days, 89))).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = ("/subscriptions/%s/providers/Microsoft.Insights/eventtypes/management/values"
           % subscription_id)
    rows = session.get_paged(url, params={
        "api-version": ACTIVITY_API,
        "$filter": "eventTimestamp ge '%s' and resourceGroupName eq '%s'" % (start, resource_group),
        "$select": "eventTimestamp,operationName,caller,status,subStatus,resourceId,correlationId",
    })
    out = []
    for e in rows:
        op = ((e.get("operationName") or {}).get("value") or "")
        if "MICROSOFT.CONTAINERSERVICE" not in op.upper():
            continue
        if not any(x in op.upper() for x in ("/WRITE", "/DELETE", "/ACTION")):
            continue
        status = (e.get("status") or {}).get("value") or ""
        if status not in ("Succeeded", "Accepted", "Started", "Failed"):
            continue
        out.append({
            "timestamp": e.get("eventTimestamp"),
            "operation": op,
            "status": status,
            "caller": e.get("caller") or "",
            "resource": (e.get("resourceId") or "").split("/")[-1],
            "resource_id": e.get("resourceId") or "",
        })
    out.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return out


def vmss_churn_events(session, subscription_id, node_resource_group, days=30):
    """Best-effort spot-eviction proxy from the AKS node resource group's Activity Log.

    True spot evictions are only cleanly visible on-node (Scheduled Events / IMDS),
    which subscription-Reader access cannot reach. What IS visible here is VMSS
    instance delete/deallocate churn in the node RG (MC_*), which mixes real
    evictions with normal autoscale-down - so counts are approximate and must be
    labelled as eviction+autoscale, never as a clean eviction count. Note this is
    deliberately scoped to Microsoft.Compute ops, unlike activity_events() which
    keeps only Microsoft.ContainerService control-plane writes.

    Returns {"count": int, "last_event_ts": str|None, "rows": [...]}."""
    start = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=min(days, 89))).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = ("/subscriptions/%s/providers/Microsoft.Insights/eventtypes/management/values"
           % subscription_id)
    rows = session.get_paged(url, params={
        "api-version": ACTIVITY_API,
        "$filter": "eventTimestamp ge '%s' and resourceGroupName eq '%s'" % (
            start, node_resource_group),
        "$select": "eventTimestamp,operationName,status,resourceId",
    })
    out = []
    for e in rows:
        op = ((e.get("operationName") or {}).get("value") or "")
        opu = op.upper()
        if "MICROSOFT.COMPUTE/VIRTUALMACHINESCALESETS" not in opu:
            continue
        if not any(x in opu for x in ("/DELETE", "/DEALLOCATE")):
            continue
        status = (e.get("status") or {}).get("value") or ""
        if status not in ("Succeeded", "Accepted", "Started", "Failed"):
            continue
        out.append({
            "timestamp": e.get("eventTimestamp"),
            "operation": op,
            "status": status,
            "resource": (e.get("resourceId") or "").split("/")[-1],
        })
    out.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return {"count": len(out),
            "last_event_ts": out[0]["timestamp"] if out else None,
            "rows": out}


def aks_supported_versions(session, subscription_id, location):
    """Supported Kubernetes versions for a region: {minor: {patches, support_plans,
    is_preview, is_default}}."""
    data = session.get("/subscriptions/%s/providers/Microsoft.ContainerService/locations/%s/kubernetesVersions"
                       % (subscription_id, location),
                       params={"api-version": AKS_API}, ok404=True)
    out = {}
    for v in (data or {}).get("values", []):
        out[str(v.get("version"))] = {
            "patches": sorted((v.get("patchVersions") or {}).keys()),
            "support_plans": (v.get("capabilities") or {}).get("supportPlan") or [],
            "is_preview": bool(v.get("isPreview")),
            "is_default": bool(v.get("isDefault")),
        }
    return out


_IMG_DATE1 = re.compile(r"(\d{6})\.(\d{2})\.\d+$")      # ...-202405.27.0
_IMG_DATE2 = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})$")  # ...-2024.05.27


def node_image_date(version_str):
    if not version_str:
        return None
    m = _IMG_DATE1.search(version_str)
    if m:
        ym, d = m.groups()
        try:
            return dt.date(int(ym[:4]), int(ym[4:6]), int(d))
        except ValueError:
            return None
    m = _IMG_DATE2.search(version_str)
    if m:
        y, mo, d = m.groups()
        try:
            return dt.date(int(y), int(mo), int(d))
        except ValueError:
            return None
    return None


_retail_cache = {}


def _retail_get(url, params):
    return requests.get(url, params=params, timeout=60).json()


def retail_vm_prices(region, vm_size, currency="USD"):
    """Public Retail Prices API (prices.azure.com, no auth): Linux on-demand vs spot
    hourly price for a VM size in a region. Returns dict or None."""
    key = (region, vm_size, currency)
    if key in _retail_cache:
        return _retail_cache[key]
    time.sleep(0.25)
    flt = ("serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
           "and armRegionName eq '%s' and armSkuName eq '%s'" % (region, vm_size))
    items, url, params = [], "https://prices.azure.com/api/retail/prices", \
        {"$filter": flt, "currencyCode": currency}
    try:
        for _ in range(5):
            data = _retail_get(url, params)
            items.extend(data.get("Items") or [])
            url = data.get("NextPageLink")
            params = None
            if not url:
                break
    except Exception as e:  # network/parse problems should not kill the report
        log("  retail price lookup failed for %s/%s: %s" % (region, vm_size, e))
        _retail_cache[key] = None
        return None
    linux = [i for i in items
             if "Windows" not in (i.get("productName") or "")
             and "Low Priority" not in (i.get("meterName") or "")
             and (i.get("unitPrice") or 0) > 0
             and i.get("type") == "Consumption"]
    od = [i for i in linux if "Spot" not in (i.get("meterName") or "")]
    sp = [i for i in linux if "Spot" in (i.get("meterName") or "")]
    rec = None
    if od:
        rec = {"od_hr": min(i["unitPrice"] for i in od),
               "spot_hr": min(i["unitPrice"] for i in sp) if sp else None,
               "currency": currency}
    _retail_cache[key] = rec
    return rec


# --- VM SKU capability lookup (ephemeral OS disk + SKU modernization) -------
# A small capability map is the one shared helper for ephemeral-OS-disk
# eligibility and same-shape SKU modernization. It carries the approx cache/temp
# disk size (GiB) for the most common AKS D/E families, so ephemeral-OS-disk
# eligibility (cache/temp disk >= OS disk size) can be screened without an extra
# API call. For SKUs not listed here callers fall back to the live
# compute SKUs API (vm_sku_capabilities) which may return None.
#
# Values are best-effort public specs for the v3/v4/v5 generations. Pricing and
# exact cache sizes drift over time; every recommendation using this map must
# carry a verify-before-move caveat (same convention as the spot reports).
_SKU_CAP = {
    # Standard_D2s_v3: 8GiB temp  -> generally too small for a 128GiB OS disk
    "standard_d2s_v3": {"vcpu": 2, "mem": 8, "cache_gb": 16, "temp_gb": 16, "arch": "x64"},
    "standard_d4s_v3": {"vcpu": 4, "mem": 16, "cache_gb": 100, "temp_gb": 100, "arch": "x64"},
    "standard_d8s_v3": {"vcpu": 8, "mem": 32, "cache_gb": 200, "temp_gb": 200, "arch": "x64"},
    "standard_d16s_v3": {"vcpu": 16, "mem": 64, "cache_gb": 400, "temp_gb": 400, "arch": "x64"},
    "standard_d32s_v3": {"vcpu": 32, "mem": 128, "cache_gb": 800, "temp_gb": 800, "arch": "x64"},
    # v4 (Dsv4): cache/temp is smaller than v3 for the same vCPU
    "standard_d2s_v4": {"vcpu": 2, "mem": 8, "cache_gb": 0, "temp_gb": 0, "arch": "x64"},
    "standard_d4s_v4": {"vcpu": 4, "mem": 16, "cache_gb": 0, "temp_gb": 0, "arch": "x64"},
    "standard_d8s_v4": {"vcpu": 8, "mem": 32, "cache_gb": 0, "temp_gb": 0, "arch": "x64"},
    "standard_d16s_v4": {"vcpu": 16, "mem": 64, "cache_gb": 0, "temp_gb": 0, "arch": "x64"},
    # v5 (Dsv5): temp disk removed (ephemeral OS uses cache only)
    "standard_d2s_v5": {"vcpu": 2, "mem": 8, "cache_gb": 75, "temp_gb": 0, "arch": "x64"},
    "standard_d4s_v5": {"vcpu": 4, "mem": 16, "cache_gb": 150, "temp_gb": 0, "arch": "x64"},
    "standard_d8s_v5": {"vcpu": 8, "mem": 32, "cache_gb": 300, "temp_gb": 0, "arch": "x64"},
    "standard_d16s_v5": {"vcpu": 16, "mem": 64, "cache_gb": 600, "temp_gb": 0, "arch": "x64"},
    "standard_d32s_v5": {"vcpu": 32, "mem": 128, "cache_gb": 1200, "temp_gb": 0, "arch": "x64"},
    # Dasv4 (AMD EPYC 2nd gen)
    "standard_d2as_v4": {"vcpu": 2, "mem": 8, "cache_gb": 0, "temp_gb": 0, "arch": "x64"},
    "standard_d4as_v4": {"vcpu": 4, "mem": 16, "cache_gb": 0, "temp_gb": 0, "arch": "x64"},
    "standard_d8as_v4": {"vcpu": 8, "mem": 32, "cache_gb": 0, "temp_gb": 0, "arch": "x64"},
    # Dasv5 (AMD EPYC 5th gen)
    "standard_d2as_v5": {"vcpu": 2, "mem": 8, "cache_gb": 75, "temp_gb": 0, "arch": "x64"},
    "standard_d4as_v5": {"vcpu": 4, "mem": 16, "cache_gb": 150, "temp_gb": 0, "arch": "x64"},
    "standard_d8as_v5": {"vcpu": 8, "mem": 32, "cache_gb": 300, "temp_gb": 0, "arch": "x64"},
    "standard_d16as_v5": {"vcpu": 16, "mem": 64, "cache_gb": 600, "temp_gb": 0, "arch": "x64"},
    # Dpsv5 / Dplds v5 (ARM64 Ampere Altra) - multi-arch images required
    "standard_d2ps_v5": {"vcpu": 2, "mem": 8, "cache_gb": 75, "temp_gb": 0, "arch": "arm64"},
    "standard_d4ps_v5": {"vcpu": 4, "mem": 16, "cache_gb": 150, "temp_gb": 0, "arch": "arm64"},
    "standard_d8ps_v5": {"vcpu": 8, "mem": 32, "cache_gb": 300, "temp_gb": 0, "arch": "arm64"},
    "standard_d16ps_v5": {"vcpu": 16, "mem": 64, "cache_gb": 600, "temp_gb": 0, "arch": "arm64"},
    # E-series (memory optimized)
    "standard_e2s_v3": {"vcpu": 2, "mem": 16, "cache_gb": 16, "temp_gb": 16, "arch": "x64"},
    "standard_e4s_v3": {"vcpu": 4, "mem": 32, "cache_gb": 100, "temp_gb": 100, "arch": "x64"},
    "standard_e8s_v3": {"vcpu": 8, "mem": 64, "cache_gb": 200, "temp_gb": 200, "arch": "x64"},
    "standard_e2s_v5": {"vcpu": 2, "mem": 16, "cache_gb": 75, "temp_gb": 0, "arch": "x64"},
    "standard_e4s_v5": {"vcpu": 4, "mem": 32, "cache_gb": 150, "temp_gb": 0, "arch": "x64"},
    "standard_e8s_v5": {"vcpu": 8, "mem": 64, "cache_gb": 300, "temp_gb": 0, "arch": "x64"},
    "standard_e16s_v5": {"vcpu": 16, "mem": 128, "cache_gb": 600, "temp_gb": 0, "arch": "x64"},
    "standard_e2as_v5": {"vcpu": 2, "mem": 16, "cache_gb": 75, "temp_gb": 0, "arch": "x64"},
    "standard_e4as_v5": {"vcpu": 4, "mem": 32, "cache_gb": 150, "temp_gb": 0, "arch": "x64"},
    "standard_e8as_v5": {"vcpu": 8, "mem": 64, "cache_gb": 300, "temp_gb": 0, "arch": "x64"},
    # F-series (compute optimized)
    "standard_f2s_v2": {"vcpu": 2, "mem": 4, "cache_gb": 16, "temp_gb": 16, "arch": "x64"},
    "standard_f4s_v2": {"vcpu": 4, "mem": 8, "cache_gb": 32, "temp_gb": 32, "arch": "x64"},
    "standard_f8s_v2": {"vcpu": 8, "mem": 16, "cache_gb": 64, "temp_gb": 64, "arch": "x64"},
    "standard_f16s_v2": {"vcpu": 16, "mem": 32, "cache_gb": 128, "temp_gb": 128, "arch": "x64"},
}


def _norm_sku(vm_size):
    # lowercase only - keep underscores so the key matches the _SKU_CAP map
    # (Azure SKU names are case-insensitive but the underscore form is the key).
    return (vm_size or "").strip().lower()


def azure_sku(sku_key):
    """Reverse _norm_sku -> the Azure SKU form for Retail Prices (Standard_D4s_v3)."""
    # sku_key is like 'standard_d4s_v3'; the Azure armSkuName keeps the same
    # underscores with only the first letter capitalised.
    return sku_key[:1].upper() + sku_key[1:]


def sku_capabilities(vm_size):
    """Approx capabilities (vcpu, mem_gb, cache_gb, temp_gb, arch) for a VM SKU
    from a static map of common AKS D/E/F families. Returns None when the SKU
    is not in the map - callers then fall back to the compute SKUs API or treat
    eligibility as unknown. Values are best-effort public specs and drift; any
    recommendation built from this map must carry a verify-before-move caveat."""
    return _SKU_CAP.get(_norm_sku(vm_size))


def ephemeral_os_disk_eligible(vm_size, os_disk_gb):
    """True when the SKU cache/temp disk is large enough to hold the OS disk.
    Ephemeral OS disks are free (use the VM cache/temp/local NVMe), so an
    ephemeral-capable pool on a Managed OS disk is a savings candidate. Eligible
    only when cache OR temp >= os_disk_gb; returns None when unknown."""
    cap = sku_capabilities(vm_size)
    if not cap:
        return None
    try:
        need = int(os_disk_gb or 0)
    except (TypeError, ValueError):
        need = 0
    if need <= 0:
        need = 128
    return int(need <= max(cap["cache_gb"], cap["temp_gb"]))


def modernize_sku(vm_size, region, currency="USD"):
    """Find the cheapest same-vCPU/mem, same-arch SKU in-region from the retail
    Prices API (and the static capability map). Returns a dict
    {new_sku, new_cap, od_hr, est_pct_off} or None. v3 SKUs prefer the matching
    v5 (or Dasv5), never crossing arch; ARM64 candidates require multi-arch
    images so they are surfaced but never auto-recommended. Caller must re-verify
    regional availability and quota."""
    cur_cap = sku_capabilities(vm_size)
    if not cur_cap:
        return None
    cur_key = _norm_sku(vm_size)
    arch = cur_cap["arch"]
    candidates = []
    for sku, cap in _SKU_CAP.items():
        if sku == cur_key:
            continue
        if cap["vcpu"] != cur_cap["vcpu"] or cap["mem"] != cur_cap["mem"]:
            continue
        if cap["arch"] != arch:
            continue
        if sku <= cur_key:
            # generation index must advance (v4 after v3, v5 after v4, as after _)
            continue
        price = retail_vm_prices(region, azure_sku(sku), currency)
        od = price.get("od_hr") if price else None
        candidates.append((sku, cap, od))
    if not candidates:
        return None
    cur_price = retail_vm_prices(region, vm_size, currency)
    if not cur_price:
        return None
    cur_od = cur_price["od_hr"]
    priced = [c for c in candidates if c[2]]
    if not priced:
        return None
    new_sku, new_cap, new_od = min(priced, key=lambda c: c[2])
    if new_od >= cur_od:
        return None
    return {"new_sku": azure_sku(new_sku),
            "new_cap": new_cap, "od_hr": new_od,
            "est_pct_off": (cur_od - new_od) / cur_od}

