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
