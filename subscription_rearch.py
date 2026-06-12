"""Subscription re-architecture review for cost savings (one subscription).

Points at exactly ONE subscription and inventories ALL its resources (not just
AKS) from subscription-level Reader data, then produces a multi-tab styled XLSX
of evidence plus a companion Markdown narrative designed to drive a
re-architecture-for-cost-savings exercise. Strictly read-only: it only issues
GET/POST query calls (Resource Graph, Cost Management query, public retail
prices). It estimates savings from the subscription's own actual last-month
cost per resource, joined onto orphan/idle/redundancy findings and Azure
Advisor cost recommendations.

Tabs: ReadMe, Summary, CostTrend, CostByRG, Findings, Orphaned, Compute,
Storage, PaaS&Network, Advisor, RawResources, RawCosts. With --no-cost the
cost-only tabs (CostTrend, CostByRG, RawCosts) and cost columns are omitted.

Usage:
  python subscription_rearch.py --subs contoso-platform
  python subscription_rearch.py --subs 00000000-0000-0000-0000-000000000000 --months 6
  python subscription_rearch.py --subs contoso-platform --no-cost --top 20
"""
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict

import pandas as pd

from azrep import arg, excel
from azrep.costmgmt import CostClient, default_window
from azrep.http_client import connect, log
from azrep.subs import (base_parser, is_prod, load_subscriptions, out_path,
                        pick_scope, resolve_env)

# Azure Resource Graph uses a stricter KQL subset than Kusto/Log Analytics:
#  - it rejects line comments (// ...), so each query starts with the table name;
#  - `kind` is a reserved keyword, so it is projected BARE (never `k = kind`).
# Keep every construct to simple project/extend/where with lowercase functions.

ALL_RESOURCES_KQL = """
resources
| project id, name, type = tolower(type), kind, resourceGroup, location,
    subscriptionId,
    sku_name = tostring(sku.name),
    sku_tier = tostring(sku.tier),
    sku_capacity = tostring(sku.capacity),
    tags
"""

DISKS_KQL = """
resources
| where type =~ 'microsoft.compute/disks'
| extend diskState = tostring(properties.diskState)
| where diskState =~ 'Unattached' or diskState =~ 'Reserved'
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    diskSizeGB = toint(properties.diskSizeGB),
    diskState,
    timeCreated = tostring(properties.timeCreated)
"""

PUBLIC_IPS_KQL = """
resources
| where type =~ 'microsoft.network/publicipaddresses'
| where isnull(properties.ipConfiguration) and isnull(properties.natGateway)
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    allocationMethod = tostring(properties.publicIPAllocationMethod)
"""

NICS_KQL = """
resources
| where type =~ 'microsoft.network/networkinterfaces'
| where isnull(properties.virtualMachine) and isempty(properties.privateEndpoint)
| project id, name, resourceGroup, location
"""

LBS_KQL = """
resources
| where type =~ 'microsoft.network/loadbalancers'
| extend poolCount = array_length(properties.backendAddressPools)
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    poolCount
"""

VMS_KQL = """
resources
| where type =~ 'microsoft.compute/virtualmachines'
| extend powerState = tostring(properties.extended.instanceView.powerState.code)
| project id, name, resourceGroup, location,
    vmSize = tostring(properties.hardwareProfile.vmSize),
    powerState, tags
"""

VMSS_KQL = """
resources
| where type =~ 'microsoft.compute/virtualmachinescalesets'
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    sku_capacity = tostring(sku.capacity),
    tags
"""

SNAPSHOTS_KQL = """
resources
| where type =~ 'microsoft.compute/snapshots'
| extend timeCreated = todatetime(properties.timeCreated)
| where timeCreated < ago(90d)
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    diskSizeGB = toint(properties.diskSizeGB),
    timeCreated = tostring(properties.timeCreated)
"""

ASP_KQL = """
resources
| where type =~ 'microsoft.web/serverfarms'
| extend numberOfSites = toint(properties.numberOfSites)
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    sku_tier = tostring(sku.tier),
    sku_capacity = tostring(sku.capacity),
    numberOfSites
"""

GATEWAYS_KQL = """
resources
| where type =~ 'microsoft.network/applicationgateways'
    or type =~ 'microsoft.network/natgateways'
    or type =~ 'microsoft.network/vpngateways'
    or type =~ 'microsoft.network/virtualnetworkgateways'
    or type =~ 'microsoft.network/azurefirewalls'
    or type =~ 'microsoft.network/bastionhosts'
| project id, name, type = tolower(type), resourceGroup, location,
    sku_name = tostring(sku.name),
    sku_tier = tostring(sku.tier),
    gwType = tostring(properties.gatewayType)
"""

STORAGE_KQL = """
resources
| where type =~ 'microsoft.storage/storageaccounts'
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    sku_tier = tostring(sku.tier),
    kind,
    accessTier = tostring(properties.accessTier)
"""

SQL_KQL = """
resources
| where type =~ 'microsoft.sql/servers/databases'
| where name != 'master'
| project id, name, resourceGroup, location,
    sku_name = tostring(sku.name),
    sku_tier = tostring(sku.tier),
    sku_capacity = tostring(sku.capacity),
    serviceObjective = tostring(properties.currentServiceObjectiveName)
"""

ADVISOR_KQL = """
advisorresources
| where type =~ 'microsoft.advisor/recommendations'
| project id,
    category = tostring(properties.category),
    impact = tostring(properties.impact),
    problem = tostring(properties.shortDescription.problem),
    solution = tostring(properties.shortDescription.solution),
    impactedResource = tostring(properties.resourceMetadata.resourceId),
    impactedType = tostring(properties.impactedField),
    annualSavings = tostring(properties.extendedProperties.annualSavingsAmount),
    savingsCurrency = tostring(properties.extendedProperties.savingsCurrency)
"""

# Savings heuristics surfaced in the legend tab and the markdown narrative.
GRS_FACTOR = 0.5            # GRS/RAGRS/GZRS -> LRS roughly halves storage cost
SCHEDULE_FACTOR = 0.65      # nights+weekends start/stop on nonprod compute
GEO_REDUNDANT = ("grs", "ragrs", "gzrs", "ra-grs")
FLAT_RATE_TYPES = {
    "microsoft.network/azurefirewalls": "Azure Firewall",
    "microsoft.network/applicationgateways": "Application Gateway",
    "microsoft.network/vpngateways": "VPN gateway",
    "microsoft.network/virtualnetworkgateways": "Virtual network gateway",
    "microsoft.network/natgateways": "NAT gateway",
    "microsoft.network/bastionhosts": "Bastion host",
}
CATEGORY_ORDER = ["ORPHANED", "RIGHTSIZE", "TIER", "REDUNDANCY", "CONSOLIDATE",
                  "SCHEDULE", "COMMITMENT", "ADVISOR"]


def _q(session, kql, sub_id):
    return arg.query(session, kql, [sub_id])


def _tags_json(tags):
    if not isinstance(tags, dict) or not tags:
        return ""
    return json.dumps(tags, sort_keys=True)


def _rg_of(resource_id):
    parts = str(resource_id or "").lower().split("/")
    if "resourcegroups" in parts:
        i = parts.index("resourcegroups")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _name_of(resource_id):
    return str(resource_id or "").rstrip("/").rsplit("/", 1)[-1]


def _short_type(typ):
    t = str(typ or "").lower()
    return t.split("/")[-1] if "/" in t else t


def _money(v):
    return None if v is None else round(float(v), 2)


def _parse_advisor_saving(rec):
    raw = rec.get("annualSavings")
    if raw in (None, "", "None"):
        return None
    try:
        return _money(float(raw) / 12.0)
    except (ValueError, TypeError):
        return None


# --- cost ----------------------------------------------------------------

def load_costs(session, sub_id, months):
    """Returns (per_resource_cost {lower id: last-month USD}, service_trend df,
    raw_cost rows, last_full_month, prev_full_month, mtd_month). All read-only."""
    d_from, d_to = default_window(months)
    cc = CostClient(session)
    scope = "/subscriptions/%s" % sub_id
    cur_month = dt.date.today().strftime("%Y-%m")

    # Per-resource actual cost over the window; the last full month feeds findings.
    rid_df = cc.query(scope, "ActualCost", "Monthly", ("ResourceId", "ServiceName"),
                      None, d_from, d_to)
    svc_df = cc.query(scope, "ActualCost", "Monthly", ("ServiceName",),
                      None, d_from, d_to)

    months_seen = []
    if not rid_df.empty:
        rid_df["Month"] = rid_df["Period"].str[:7]
        months_seen = sorted(rid_df["Month"].unique())
    elif not svc_df.empty:
        svc_df["Month"] = svc_df["Period"].str[:7]
        months_seen = sorted(svc_df["Month"].unique())
    full = [m for m in months_seen if m != cur_month]
    last_full = full[-1] if full else (months_seen[-1] if months_seen else None)
    prev_full = full[-2] if len(full) >= 2 else None

    per_res = {}
    raw_rows = []
    if not rid_df.empty:
        rid_df["rid_l"] = rid_df["ResourceId"].astype(str).str.lower()
        for (rid, mo), grp in rid_df.groupby(["rid_l", "Month"]):
            usd = float(grp["CostUSD"].sum())
            raw_rows.append({"resource_id": rid, "month": mo, "USD": _money(usd)})
            if mo == last_full:
                per_res[rid] = per_res.get(rid, 0.0) + usd

    if "Month" not in svc_df.columns and not svc_df.empty:
        svc_df["Month"] = svc_df["Period"].str[:7]
    return per_res, svc_df, raw_rows, last_full, prev_full, cur_month


# --- findings engine -----------------------------------------------------

def _f(sev, rid, name, rg, evidence, saving, action, effort, risk):
    return {"severity": sev, "resource_id": rid, "resource_name": name,
            "resource_group": rg, "evidence": evidence,
            "est_monthly_saving": _money(saving) if saving is not None else None,
            "action": action, "effort": effort, "risk": risk}


def f_orphan_disks(ctx):
    out = []
    for d in ctx["disks"]:
        state = d.get("diskState") or ""
        sev = "FAIL" if state.lower() == "unattached" else "WARN"
        c = ctx["cost"].get((d.get("id") or "").lower())
        out.append(_f(sev, d.get("id"), d.get("name"), d.get("resourceGroup"),
                      "Managed disk %s (%s, %s GB) created %s" % (
                          state, d.get("sku_name") or "", d.get("diskSizeGB"),
                          (d.get("timeCreated") or "")[:10]),
                      c, "Delete or snapshot-then-delete the disk", "low",
                      "low" if sev == "FAIL" else "med"))
    return out


def f_orphan_pips(ctx):
    out = []
    for p in ctx["pips"]:
        c = ctx["cost"].get((p.get("id") or "").lower())
        out.append(_f("FAIL", p.get("id"), p.get("name"), p.get("resourceGroup"),
                      "Public IP (%s, %s) not associated to any NIC/NAT/LB" % (
                          p.get("sku_name") or "", p.get("allocationMethod") or ""),
                      c, "Release the unused public IP", "low", "low"))
    return out


def f_orphan_nics(ctx):
    out = []
    for n in ctx["nics"]:
        c = ctx["cost"].get((n.get("id") or "").lower())
        out.append(_f("WARN", n.get("id"), n.get("name"), n.get("resourceGroup"),
                      "NIC not attached to a VM and not a private-endpoint NIC",
                      c, "Delete the dangling NIC", "low", "low"))
    return out


def f_empty_lbs(ctx):
    out = []
    for lb in ctx["lbs"]:
        if (lb.get("poolCount") or 0) != 0:
            continue
        c = ctx["cost"].get((lb.get("id") or "").lower())
        sku = (lb.get("sku_name") or "").lower()
        note = "Standard LB bills per rule/hour" if sku == "standard" else \
               "Basic LB is retiring; plan migration"
        out.append(_f("WARN", lb.get("id"), lb.get("name"), lb.get("resourceGroup"),
                      "Load balancer (%s) has no backend pool members; %s" % (
                          lb.get("sku_name") or "", note),
                      c, "Delete the empty load balancer", "low", "med"))
    return out


def f_stopped_vms(ctx):
    out = []
    for v in ctx["vms"]:
        ps = (v.get("powerState") or "").lower()
        c = ctx["cost"].get((v.get("id") or "").lower())
        if ps == "powerstate/stopped":
            out.append(_f("FAIL", v.get("id"), v.get("name"), v.get("resourceGroup"),
                          "VM %s is Stopped (not deallocated) - still billing compute" %
                          (v.get("vmSize") or ""),
                          c, "Deallocate (Stop-AzVM) or delete the VM", "low", "med"))
        elif ps == "powerstate/deallocated":
            out.append(_f("WARN", v.get("id"), v.get("name"), v.get("resourceGroup"),
                          "VM %s is deallocated; compute free but its disks still bill" %
                          (v.get("vmSize") or ""),
                          None, "Delete the VM and orphan disks if no longer needed",
                          "low", "med"))
    return out


def f_old_snapshots(ctx):
    out = []
    for s in ctx["snapshots"]:
        c = ctx["cost"].get((s.get("id") or "").lower())
        out.append(_f("WARN", s.get("id"), s.get("name"), s.get("resourceGroup"),
                      "Snapshot (%s GB) older than 90 days, created %s" % (
                          s.get("diskSizeGB"), (s.get("timeCreated") or "")[:10]),
                      c, "Delete stale snapshot or move to archive", "low", "med"))
    return out


def f_empty_asps(ctx):
    out = []
    for a in ctx["asps"]:
        if (a.get("numberOfSites") or 0) != 0:
            continue
        c = ctx["cost"].get((a.get("id") or "").lower())
        out.append(_f("FAIL", a.get("id"), a.get("name"), a.get("resourceGroup"),
                      "App Service plan (%s/%s) hosts 0 apps" % (
                          a.get("sku_tier") or "", a.get("sku_name") or ""),
                      c, "Delete the empty App Service plan", "low", "low"))
    return out


def f_storage_redundancy(ctx):
    out = []
    for s in ctx["storage"]:
        sku = (s.get("sku_name") or "").lower()
        if not any(g in sku for g in GEO_REDUNDANT):
            continue
        c = ctx["cost"].get((s.get("id") or "").lower())
        saving = (c * GRS_FACTOR) if c is not None else None
        env = ctx["env_of"](s)
        sev = "WARN" if not is_prod(env) else "INFO"
        out.append(_f(sev, s.get("id"), s.get("name"), s.get("resourceGroup"),
                      "Storage account redundancy %s (env=%s)" % (
                          s.get("sku_name") or "", env or "(unknown)"),
                      saving, "Drop to LRS if geo-redundancy is not required (~50%)",
                      "low", "med"))
    return out


def f_flat_rate_inventory(ctx):
    """Flat-rate expensive network appliances; firewall/redundant gateway in
    nonprod is the classic re-architecture target."""
    out = []
    by_type = defaultdict(list)
    for g in ctx["gateways"]:
        by_type[g.get("type")].append(g)
    for g in ctx["gateways"]:
        typ = g.get("type")
        label = FLAT_RATE_TYPES.get(typ, _short_type(typ))
        c = ctx["cost"].get((g.get("id") or "").lower())
        env = ctx["env_of"](g)
        sev = "INFO"
        action = "Review whether this flat-rate appliance is still required"
        if typ == "microsoft.network/azurefirewalls" and not is_prod(env):
            sev = "WARN"
            action = "Azure Firewall in nonprod is costly; consider NSG/UDR or sharing"
        out.append(_f(sev, g.get("id"), g.get("name"), g.get("resourceGroup"),
                      "%s (%s/%s, env=%s) - flat hourly + data cost" % (
                          label, g.get("sku_name") or "", g.get("sku_tier") or "",
                          env or "(unknown)"),
                      c, action, "med", "med"))
    return out


def f_sql_tier(ctx):
    out = []
    for s in ctx["sql"]:
        tier = (s.get("sku_tier") or "").lower()
        slo = (s.get("serviceObjective") or "")
        env = ctx["env_of"](s)
        is_premium = tier in ("premium", "businesscritical") or "p" == slo[:1].lower()
        if not is_premium:
            continue
        c = ctx["cost"].get((s.get("id") or "").lower())
        sev = "WARN" if not is_prod(env) else "INFO"
        out.append(_f(sev, s.get("id"), s.get("name"), s.get("resourceGroup"),
                      "SQL database tier %s (%s, env=%s)" % (
                          s.get("sku_tier") or "", slo or s.get("sku_name") or "",
                          env or "(unknown)"),
                      None, "Downscale to a lower tier/SLO if perf allows",
                      "med", "med"))
    return out


def f_consolidate(ctx):
    """Multiple instances of resources that are commonly fronted/shared."""
    out = []
    low_asps = [a for a in ctx["asps"] if (a.get("numberOfSites") or 0) <= 1]
    by_rg = defaultdict(list)
    for a in low_asps:
        by_rg[(a.get("resourceGroup") or "").lower(), a.get("location")].append(a)
    for (rg, loc), grp in by_rg.items():
        if len(grp) > 1:
            saving = sum(ctx["cost"].get((a.get("id") or "").lower(), 0.0)
                         for a in grp[1:]) or None
            out.append(_f("WARN", grp[0].get("id"),
                          ", ".join(a.get("name") for a in grp), rg,
                          "%d low-utilization App Service plans in %s/%s" % (
                              len(grp), rg, loc),
                          saving, "Consolidate apps onto one shared plan",
                          "med", "med"))

    for typ, label, action in (
        ("microsoft.network/applicationgateways", "Application Gateway",
         "Front shared ingress through a single Application Gateway/Front Door"),
        ("microsoft.network/azurefirewalls", "Azure Firewall",
         "Consolidate to a single hub firewall"),
        ("microsoft.network/bastionhosts", "Bastion host",
         "Centralize Bastion in a hub VNet")):
        items = [g for g in ctx["gateways"] if g.get("type") == typ]
        if len(items) > 1:
            saving = sum(ctx["cost"].get((g.get("id") or "").lower(), 0.0)
                         for g in items[1:]) or None
            out.append(_f("WARN", items[0].get("id"),
                          ", ".join(g.get("name") for g in items), "",
                          "%d %s instances in this subscription" % (len(items), label),
                          saving, action, "med", "high"))
    return out


def f_schedule(ctx):
    """Nonprod compute that could run on a start/stop schedule."""
    out = []
    for v in ctx["vms"]:
        if (v.get("powerState") or "").lower() != "powerstate/running":
            continue
        env = ctx["env_of"](v)
        if is_prod(env) or not env:
            continue
        c = ctx["cost"].get((v.get("id") or "").lower())
        saving = (c * SCHEDULE_FACTOR) if c is not None else None
        out.append(_f("WARN", v.get("id"), v.get("name"), v.get("resourceGroup"),
                      "Running nonprod VM %s (env=%s) with no obvious schedule" % (
                          v.get("vmSize") or "", env),
                      saving, "Add nights/weekends auto start-stop (~65%)",
                      "med", "low"))
    for c in ctx["clusters"]:
        env = ctx["env_of"](c)
        if is_prod(env) or not env:
            continue
        cost = ctx["cost"].get((c.get("id") or "").lower())
        saving = (cost * SCHEDULE_FACTOR) if cost is not None else None
        out.append(_f("WARN", c.get("id"), c.get("name"), c.get("resourceGroup"),
                      "Nonprod AKS cluster (env=%s) running continuously" % env,
                      saving, "Stop the cluster off-hours (az aks stop) (~65%)",
                      "med", "med"))
    return out


def f_commitment(ctx):
    """Steady compute spenders are reservation / savings-plan candidates."""
    out = []
    steady = ctx["steady_compute"]
    for rid, info in steady:
        adv = ctx["advisor_saving_for"].get(rid)
        out.append(_f("INFO", rid, _name_of(rid), _rg_of(rid),
                      "Steady compute spend ~$%.0f/mo across %d full months (low variance)" % (
                          info["avg"], info["months"]),
                      adv, "Evaluate 1-year reservation or savings plan", "low", "low"))
    return out


def f_advisor(ctx):
    out = []
    for r in ctx["advisor_cost"]:
        rid = r.get("impactedResource") or r.get("id")
        out.append(_f("WARN", rid, _name_of(rid), _rg_of(rid),
                      "Advisor (%s): %s" % (r.get("impact") or "", r.get("problem") or ""),
                      _parse_advisor_saving(r),
                      r.get("solution") or "Apply the Advisor recommendation",
                      "low", "low"))
    return out


FINDINGS = [
    ("orphan_disks", "ORPHANED", "Unattached / reserved managed disks", f_orphan_disks),
    ("orphan_pips", "ORPHANED", "Public IPs not associated", f_orphan_pips),
    ("orphan_nics", "ORPHANED", "Dangling network interfaces", f_orphan_nics),
    ("empty_lbs", "ORPHANED", "Load balancers with empty backend pools", f_empty_lbs),
    ("stopped_vms", "ORPHANED", "Stopped-but-not-deallocated VMs", f_stopped_vms),
    ("old_snapshots", "ORPHANED", "Snapshots older than 90 days", f_old_snapshots),
    ("empty_asps", "ORPHANED", "App Service plans with zero apps", f_empty_asps),
    ("storage_redundancy", "REDUNDANCY", "Geo-redundant storage in nonprod",
     f_storage_redundancy),
    ("flat_rate", "TIER", "Flat-rate network appliances", f_flat_rate_inventory),
    ("sql_tier", "RIGHTSIZE", "Premium/high SQL tiers", f_sql_tier),
    ("consolidate", "CONSOLIDATE", "Consolidatable shared services", f_consolidate),
    ("schedule", "SCHEDULE", "Schedulable nonprod compute", f_schedule),
    ("commitment", "COMMITMENT", "Reservation / savings-plan candidates", f_commitment),
    ("advisor", "ADVISOR", "Azure Advisor cost recommendations", f_advisor),
]


def run_findings(ctx):
    rows = []
    for fid, category, _desc, fn in FINDINGS:
        try:
            for r in fn(ctx) or []:
                r["category"] = category
                r["check"] = fid
                rows.append(r)
        except Exception as e:  # one finding must never kill the report
            log("  finding %s failed: %s" % (fid, e))
    # sort by est saving desc (blanks last) then severity
    sev_rank = {"FAIL": 0, "WARN": 1, "INFO": 2}
    rows.sort(key=lambda r: (
        0 if r["est_monthly_saving"] is not None else 1,
        -(r["est_monthly_saving"] or 0.0),
        sev_rank.get(r["severity"], 9)))
    return rows


# --- steady compute detection (commitment candidates) --------------------

def steady_compute(raw_cost_rows, full_months, top):
    """Resource ids with compute-like spend in every full month and low variance."""
    by_rid = defaultdict(dict)
    for r in raw_cost_rows:
        rid = r["resource_id"]
        low = rid.lower()
        if not any(t in low for t in ("/virtualmachines/",
                                      "/virtualmachinescalesets/",
                                      "/managedclusters/")):
            continue
        by_rid[rid][r["month"]] = (by_rid[rid].get(r["month"], 0.0) +
                                   (r["USD"] or 0.0))
    out = []
    for rid, by_month in by_rid.items():
        vals = [by_month.get(m, 0.0) for m in full_months]
        if not full_months or any(v <= 1.0 for v in vals):
            continue
        avg = sum(vals) / len(vals)
        if avg < 50:
            continue
        spread = (max(vals) - min(vals)) / avg if avg else 1.0
        if spread <= 0.25:
            out.append((rid, {"avg": avg, "months": len(full_months)}))
    out.sort(key=lambda kv: kv[1]["avg"], reverse=True)
    return out[:top]


# --- markdown narrative --------------------------------------------------

def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "x")).strip("_") or "x"


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


def _infer_rg_purpose(rg, resources):
    name = rg.lower()
    for token, purpose in (("network", "networking / connectivity"),
                           ("vnet", "networking / connectivity"),
                           ("data", "data / databases"), ("sql", "data / databases"),
                           ("aks", "Kubernetes / containers"),
                           ("mc_", "AKS node infrastructure"),
                           ("web", "web / app hosting"), ("app", "application hosting"),
                           ("sec", "security / identity"), ("hub", "shared hub")):
        if token in name:
            return purpose
    types = defaultdict(int)
    for r in resources:
        types[_short_type(r.get("type"))] += 1
    if types:
        dom = max(types.items(), key=lambda kv: kv[1])[0]
        return "dominant resource: %s" % dom
    return "(mixed)"


def write_md(path, sub_name, sub_id, args, ctx, findings, rg_cost, svc_trend,
             months, last_full):
    cost_on = not args.no_cost
    res = ctx["resources"]
    res_by_rg = defaultdict(list)
    for r in res:
        res_by_rg[(r.get("resourceGroup") or "").lower()].append(r)

    fin_by_cat = defaultdict(list)
    for f in findings:
        fin_by_cat[f["category"]].append(f)
    total_saving = sum(f["est_monthly_saving"] or 0.0 for f in findings)
    last_cost = sum(rg_cost.values()) if rg_cost else 0.0

    rg_sorted = sorted(rg_cost.items(), key=lambda kv: kv[1], reverse=True) \
        if cost_on else [(rg, 0.0) for rg in sorted(res_by_rg)]

    with open(path, "w", encoding="utf-8") as f:
        f.write("# Subscription re-architecture review - %s\n\n" % sub_name)
        f.write("Generated: %s\n\n" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        f.write("Subscription: %s (`%s`)\n\n" % (sub_name, sub_id))
        f.write("Data sources: Azure Resource Graph, Cost Management query API"
                "%s, Azure Advisor, public retail prices. All read-only "
                "(GET/POST query only); nothing was modified.\n\n"
                % ("" if cost_on else " (skipped: --no-cost)"))

        # 2. Executive summary
        f.write("## Executive summary\n\n")
        if cost_on and last_full:
            f.write("- Spend in the last full month (%s): **$%.0f**\n" % (last_full, last_cost))
            if len(months) >= 2:
                f.write("- Window trend (%s): %s\n" % (
                    ", ".join(months),
                    " -> ".join("$%.0f" % svc_trend.get(m, 0.0) for m in months)))
        f.write("- Total estimated monthly savings identified: **$%.0f**\n" % total_saving)
        f.write("- Findings: %d (%d FAIL, %d WARN, %d INFO)\n\n" % (
            len(findings),
            sum(1 for x in findings if x["severity"] == "FAIL"),
            sum(1 for x in findings if x["severity"] == "WARN"),
            sum(1 for x in findings if x["severity"] == "INFO")))
        top5 = [x for x in findings if x["est_monthly_saving"]][:5]
        if top5:
            f.write("Biggest opportunities:\n\n")
            for x in top5:
                f.write("- %s: %s (~$%.0f/mo)\n" % (
                    x["category"], x["evidence"], x["est_monthly_saving"]))
            f.write("\n")

        # 3. Current-state architecture
        f.write("## Current-state architecture\n\n")
        capped = rg_sorted[:args.top]
        tail = rg_sorted[args.top:]
        for rg, cost in capped:
            rrs = res_by_rg.get(rg, [])
            f.write("### Resource group: %s\n\n" % (rg or "(none)"))
            f.write("- Inferred purpose: %s\n" % _infer_rg_purpose(rg, rrs))
            if cost_on:
                f.write("- Monthly cost: $%.0f\n" % cost)
            f.write("- Resources: %d\n" % len(rrs))
            tcount = defaultdict(int)
            for r in rrs:
                tcount[_short_type(r.get("type"))] += 1
            f.write("- Types: %s\n\n" % ", ".join(
                "%s=%d" % (t, n) for t, n in sorted(tcount.items(),
                                                    key=lambda kv: -kv[1])[:8]))
            if cost_on:
                top_res = sorted(rrs, key=lambda r: ctx["cost"].get(
                    (r.get("id") or "").lower(), 0.0), reverse=True)[:args.top]
                rows = [(r.get("name"), _short_type(r.get("type")),
                         "$%.0f" % ctx["cost"].get((r.get("id") or "").lower(), 0.0))
                        for r in top_res
                        if ctx["cost"].get((r.get("id") or "").lower(), 0.0) > 0]
                if rows:
                    f.write(_md_table(["resource", "type", "monthly cost"], rows))
                    f.write("\n\n")
        if tail:
            f.write("Long tail: %d more resource groups%s.\n\n" % (
                len(tail),
                " totaling $%.0f/mo" % sum(c for _, c in tail) if cost_on else ""))

        # Mermaid overview
        f.write("### Overview diagram\n\n```mermaid\ngraph TB\n")
        for rg, cost in capped[:8]:
            rid = _safe_id(rg)
            f.write('  subgraph sg_%s["%s%s"]\n' % (
                rid, rg or "(none)", " $%.0f/mo" % cost if cost_on else ""))
            rrs = res_by_rg.get(rg, [])
            top_res = sorted(rrs, key=lambda r: ctx["cost"].get(
                (r.get("id") or "").lower(), 0.0), reverse=True)[:5] if cost_on \
                else rrs[:5]
            for r in top_res:
                nid = "n_%s" % _safe_id(r.get("id"))
                rcost = ctx["cost"].get((r.get("id") or "").lower(), 0.0)
                label = "%s\\n%s%s" % (r.get("name"), _short_type(r.get("type")),
                                       "\\n$%.0f/mo" % rcost if cost_on and rcost else "")
                f.write('    %s["%s"]\n' % (nid, label))
            f.write("  end\n")
        f.write("```\n\n")

        # 4. Cost breakdown
        if cost_on:
            f.write("## Cost breakdown\n\n")
            f.write("### By service (monthly USD)\n\n")
            svc_rows = ctx["svc_matrix"]
            if svc_rows:
                f.write(_md_table(["service"] + months,
                                  [[s] + ["$%.0f" % v.get(m, 0.0) for m in months]
                                   for s, v in svc_rows]))
                f.write("\n\n")
            f.write("### By resource group (last full month)\n\n")
            f.write(_md_table(["resource group", "monthly cost"],
                              [[rg or "(none)", "$%.0f" % c] for rg, c in capped]))
            f.write("\n\n")

        # 5. Findings & recommendations
        f.write("## Findings & recommendations\n\n")
        for cat in CATEGORY_ORDER:
            fs = fin_by_cat.get(cat)
            if not fs:
                continue
            f.write("### %s\n\n" % cat)
            f.write("%s\n\n" % CATEGORY_INTRO.get(cat, ""))
            rows = [(x["resource_name"], x["evidence"],
                     "$%.0f" % x["est_monthly_saving"] if x["est_monthly_saving"]
                     else "", x["action"], x["effort"], x["risk"]) for x in fs]
            f.write(_md_table(["resource", "evidence", "est monthly saving",
                               "action", "effort", "risk"], rows))
            sub = sum(x["est_monthly_saving"] or 0.0 for x in fs)
            f.write("\n\nSubtotal: ~$%.0f/mo\n\n" % sub)

        # 6. Target-state moves
        f.write("## Suggested target-state moves\n\n")
        moves = []
        if fin_by_cat.get("ORPHANED"):
            moves.append("Sweep and delete orphaned disks, IPs, NICs, empty load "
                         "balancers and zero-app plans.")
        if fin_by_cat.get("REDUNDANCY"):
            moves.append("Drop geo-redundancy (GRS->LRS) on nonprod storage.")
        if any("Application Gateway" in x["evidence"] for x in fin_by_cat.get("CONSOLIDATE", [])):
            moves.append("Front shared ingress through a single Application "
                         "Gateway/Front Door.")
        if any("App Service" in x["evidence"] for x in fin_by_cat.get("CONSOLIDATE", [])):
            moves.append("Consolidate low-utilization App Service plans onto one shared plan.")
        if fin_by_cat.get("SCHEDULE"):
            moves.append("Introduce start/stop automation for nonprod compute "
                         "(VMs and AKS).")
        if fin_by_cat.get("COMMITMENT"):
            moves.append("Cover steady compute with 1-year reservations or savings plans.")
        if fin_by_cat.get("TIER") or fin_by_cat.get("RIGHTSIZE"):
            moves.append("Right-size premium SQL/network tiers down to need.")
        if fin_by_cat.get("ADVISOR"):
            moves.append("Action the Azure Advisor cost recommendations.")
        for m in moves:
            f.write("- %s\n" % m)
        if not moves:
            f.write("- No material savings findings; the subscription looks lean.\n")
        f.write("\n")

        # 7. Appendix
        f.write("## Appendix: resource count by type\n\n")
        tcount = defaultdict(int)
        for r in res:
            tcount[r.get("type")] += 1
        f.write(_md_table(["type", "count"],
                          sorted(tcount.items(), key=lambda kv: -kv[1])))
        f.write("\n")
    return path


CATEGORY_INTRO = {
    "ORPHANED": "Resources that are provisioned and billing but attached to "
                "nothing - the safest savings to action first.",
    "RIGHTSIZE": "Resources provisioned larger/higher than their workload needs.",
    "TIER": "Flat-rate or premium-tier resources whose tier may exceed the need.",
    "REDUNDANCY": "Geo-redundancy paid for where local redundancy would do.",
    "CONSOLIDATE": "Duplicated shared services that could be centralized.",
    "SCHEDULE": "Nonprod compute that does not need to run 24x7.",
    "COMMITMENT": "Steady spend that is cheaper under a reservation or savings plan.",
    "ADVISOR": "Microsoft's own cost recommendations for this subscription.",
}


# --- main ----------------------------------------------------------------

def build_parser():
    p = base_parser("Subscription re-architecture review for cost savings (one subscription)")
    p.add_argument("--months", type=int, default=3, help="full months of cost history")
    p.add_argument("--top", type=int, default=15,
                   help="top-cost resources / resource groups shown in the markdown")
    p.add_argument("--no-cost", action="store_true",
                   help="skip Cost Management; findings then lack actual-cost columns")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    if len(sel) != 1:
        print("This report inspects exactly ONE subscription; %d are in scope.\n"
              "Narrow with --subs <id-or-name> (or pick a single subscription at "
              "the prompt)." % len(sel), file=sys.stderr)
        sys.exit(2)
    sub = sel[0]
    sub_id = sub["subscription_id"]
    sub_name = sub["subscription_name"] or sub_id
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]

    session = connect(min_interval=0.1)
    log("Inventorying subscription %s (%s)..." % (sub_name, sub_id))

    resources = _q(session, ALL_RESOURCES_KQL, sub_id)
    disks = _q(session, DISKS_KQL, sub_id)
    pips = _q(session, PUBLIC_IPS_KQL, sub_id)
    nics = _q(session, NICS_KQL, sub_id)
    lbs = _q(session, LBS_KQL, sub_id)
    vms = _q(session, VMS_KQL, sub_id)
    vmss = _q(session, VMSS_KQL, sub_id)
    snapshots = _q(session, SNAPSHOTS_KQL, sub_id)
    asps = _q(session, ASP_KQL, sub_id)
    gateways = _q(session, GATEWAYS_KQL, sub_id)
    storage = _q(session, STORAGE_KQL, sub_id)
    sql = _q(session, SQL_KQL, sub_id)
    advisor = _q(session, ADVISOR_KQL, sub_id)
    clusters = [r for r in resources
                if r.get("type") == "microsoft.containerservice/managedclusters"]
    log("Found %d resources, %d Advisor recommendations." % (len(resources), len(advisor)))

    # RG-tag map for environment inference (RG tags -> name).
    rg_tags = {}
    for r in _q(session, arg.RG_TAGS_KQL, sub_id):
        rg_tags[(r.get("name") or "").lower()] = r.get("tags") or {}

    def env_of(r):
        rg = (r.get("resourceGroup") or "").lower()
        return resolve_env(r.get("tags") or {}, rg_tags.get(rg, {}), env_keys,
                           names=(r.get("name"), r.get("resourceGroup")))

    cost_on = not args.no_cost
    per_res, svc_df, raw_cost, last_full, prev_full, mtd = ({}, pd.DataFrame(), [],
                                                            None, None, None)
    months, svc_trend, svc_matrix, rg_cost = [], {}, [], {}
    if cost_on:
        per_res, svc_df, raw_cost, last_full, prev_full, mtd = load_costs(
            session, sub_id, args.months)
        if not svc_df.empty:
            months = sorted(svc_df["Month"].unique())
            svc_trend = svc_df.groupby("Month")["CostUSD"].sum().to_dict()
            piv = svc_df.pivot_table(index="ServiceName", columns="Month",
                                     values="CostUSD", aggfunc="sum").fillna(0.0)
            piv["__t"] = piv.sum(axis=1)
            piv = piv.sort_values("__t", ascending=False).drop(columns="__t")
            top_svcs = piv.head(12)
            svc_matrix = [(s, {m: float(top_svcs.loc[s].get(m, 0.0)) for m in months})
                          for s in top_svcs.index]
            other = piv.iloc[12:]
            if not other.empty:
                svc_matrix.append(("Other", {m: float(other[m].sum()) for m in months}))
        # by-RG derived from per-resource last-month cost
        for rid, usd in per_res.items():
            rg_cost[_rg_of(rid)] = rg_cost.get(_rg_of(rid), 0.0) + usd

    ctx = {
        "cost": per_res, "resources": resources, "disks": disks, "pips": pips,
        "nics": nics, "lbs": lbs, "vms": vms, "vmss": vmss, "snapshots": snapshots,
        "asps": asps, "gateways": gateways, "storage": storage, "sql": sql,
        "clusters": clusters, "advisor_cost": [a for a in advisor
                                               if (a.get("category") or "").lower() == "cost"],
        "env_of": env_of, "svc_matrix": svc_matrix,
        "steady_compute": steady_compute(
            raw_cost, [m for m in months if m != mtd], args.top) if cost_on else [],
        "advisor_saving_for": {}, "advisor_other": [
            a for a in advisor if (a.get("category") or "").lower() != "cost"],
    }
    # Advisor cost savings, keyed BOTH by the impacted resource id (lowercased)
    # and by the exact steady-compute id so f_commitment can look them up.
    for a in ctx["advisor_cost"]:
        rid = (a.get("impactedResource") or "").lower()
        sv = _parse_advisor_saving(a)
        if rid and sv:
            ctx["advisor_saving_for"][rid] = sv
    for rid, _info in ctx["steady_compute"]:
        sv = ctx["advisor_saving_for"].get(rid.lower())
        if sv:
            ctx["advisor_saving_for"][rid] = sv

    findings = run_findings(ctx)
    total_saving = sum(f["est_monthly_saving"] or 0.0 for f in findings)

    build_workbook_and_save(args, sub_name, sub_id, ctx, findings, months,
                            svc_df, rg_cost, raw_cost, last_full, prev_full, mtd,
                            advisor, total_saving, env_filter)


def build_workbook_and_save(args, sub_name, sub_id, ctx, findings, months, svc_df,
                            rg_cost, raw_cost, last_full, prev_full, mtd, advisor,
                            total_saving, env_filter):
    cost_on = not args.no_cost
    res = ctx["resources"]
    sev_counts = defaultdict(int)
    for f in findings:
        sev_counts[f["severity"]] += 1
    last_cost = sum(rg_cost.values()) if rg_cost else 0.0
    mtd_cost = svc_df[svc_df["Month"] == mtd]["CostUSD"].sum() \
        if cost_on and not svc_df.empty else 0.0
    prev_cost = svc_df[svc_df["Month"] == prev_full]["CostUSD"].sum() \
        if cost_on and prev_full and not svc_df.empty else 0.0
    mom = ((last_cost - prev_cost) / prev_cost) if prev_cost else None

    wb = excel.new_workbook()
    readme = [
        "Generated: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Subscription: %s (%s)" % (sub_name, sub_id),
        "Resources: %d   Findings: %d   Est monthly savings: $%.0f" % (
            len(res), len(findings), total_saving),
        "",
        "Read-only: Resource Graph + Cost Management query + Advisor + retail "
        "prices (GET/POST query only). Nothing in Azure was modified.",
        "",
        "Savings-estimate heuristics:",
        "  orphan delete          = its actual last-full-month cost",
        "  stopped-not-deallocated = its compute cost (still billing)",
        "  GRS/RAGRS/GZRS -> LRS   = ~50% of the storage account's cost",
        "  empty App Service plan  = full plan cost",
        "  snapshot                = its actual cost",
        "  nonprod start/stop      = ~65% of compute cost (nights+weekends)",
        "  reservation/savings plan = blank (use Advisor's number where present)",
        "  Where no cost row matched, the saving is blank (never 0).",
    ]
    if not cost_on:
        readme.append("")
        readme.append("--no-cost: Cost Management was skipped; cost columns and the "
                      "CostTrend/CostByRG/RawCosts tabs are omitted.")
    excel.add_readme(wb, "Subscription re-architecture review", readme)

    # Summary
    summ = [("Subscription", sub_name),
            ("Subscription id", sub_id),
            ("Total resources", len(res)),
            ("Resource groups", len({(r.get("resourceGroup") or "").lower() for r in res})),
            ("Distinct resource types", len({r.get("type") for r in res})),
            ("Regions", len({r.get("location") for r in res if r.get("location")}))]
    if cost_on:
        summ += [("MTD cost (USD)", _money(mtd_cost)),
                 ("Last full month (%s) cost" % (last_full or "-"), _money(last_cost)),
                 ("MoM vs %s" % (prev_full or "-"),
                  "%.1f%%" % (mom * 100) if mom is not None else "n/a")]
    summ += [("Findings FAIL", sev_counts["FAIL"]),
             ("Findings WARN", sev_counts["WARN"]),
             ("Findings INFO", sev_counts["INFO"]),
             ("Total estimated monthly savings (USD)", _money(total_saving))]
    excel.add_table(wb, "Summary", pd.DataFrame(summ, columns=["Metric", "Value"]),
                    section="summary")
    top10 = [(f["severity"], f["category"], f["resource_name"], f["evidence"],
              f["est_monthly_saving"]) for f in findings[:10]]
    excel.add_table(wb, "TopFindings", pd.DataFrame(top10, columns=[
        "severity", "category", "resource", "evidence", "Est saving (USD)"]),
        money_cols=("Est saving (USD)",), fail_cols=("severity",), section="summary")

    # Cost Trend
    if cost_on and not svc_df.empty and ctx["svc_matrix"]:
        rows = []
        for s, v in ctx["svc_matrix"]:
            row = {"Service": s}
            row.update({m: _money(v.get(m, 0.0)) for m in months})
            rows.append(row)
        trend = pd.DataFrame(rows, columns=["Service"] + months)
        ws = excel.add_table(wb, "CostTrend", trend, money_cols=tuple(months),
                             section="summary")
        excel.add_total_row(ws, trend, list(months), label_col="Service")
        if len(months) >= 2:
            excel.add_line_chart(ws, "Monthly cost by service", len(trend) + 1,
                                 2, 1 + len(months), "B%d" % (len(trend) + 4))

    # Cost by RG
    if cost_on and rg_cost:
        top_in_rg = {}
        for r in res:
            rg = (r.get("resourceGroup") or "").lower()
            c = ctx["cost"].get((r.get("id") or "").lower(), 0.0)
            if c > top_in_rg.get(rg, (None, -1.0))[1]:
                top_in_rg[rg] = (r.get("name"), c)
        rcount = defaultdict(int)
        for r in res:
            rcount[(r.get("resourceGroup") or "").lower()] += 1
        rgrows = []
        for rg, c in sorted(rg_cost.items(), key=lambda kv: kv[1], reverse=True):
            rgrows.append({"resource_group": rg or "(none)",
                           "Last month (USD)": _money(c),
                           "top resource": (top_in_rg.get(rg) or ("", 0))[0],
                           "resources": rcount.get(rg, 0)})
        rgdf = pd.DataFrame(rgrows, columns=[
            "resource_group", "Last month (USD)", "top resource", "resources"])
        ws = excel.add_table(wb, "CostByRG", rgdf, money_cols=("Last month (USD)",),
                             int_cols=("resources",), section="summary")
        excel.add_total_row(ws, rgdf, ["Last month (USD)"], label_col="resource_group")

    # Findings (detail)
    fcols = ["severity", "category", "check", "resource_name", "resource_group",
             "evidence", "Est saving (USD)", "action", "effort", "risk", "resource_id"]
    frows = [{"severity": f["severity"], "category": f["category"], "check": f["check"],
              "resource_name": f["resource_name"], "resource_group": f["resource_group"],
              "evidence": f["evidence"], "Est saving (USD)": f["est_monthly_saving"],
              "action": f["action"], "effort": f["effort"], "risk": f["risk"],
              "resource_id": f["resource_id"]} for f in findings]
    fdf = pd.DataFrame(frows, columns=fcols) if frows else pd.DataFrame(columns=fcols)
    excel.add_table(wb, "Findings", fdf, money_cols=("Est saving (USD)",),
                    fail_cols=("severity",), max_width=90)

    # Orphaned (detail)
    orphan_checks = {"orphan_disks", "orphan_pips", "orphan_nics", "empty_lbs",
                     "stopped_vms", "old_snapshots", "empty_asps"}
    odf = fdf[fdf["check"].isin(orphan_checks)] if not fdf.empty else fdf
    excel.add_table(wb, "Orphaned",
                    odf[["severity", "check", "resource_name", "resource_group",
                         "evidence", "Est saving (USD)", "resource_id"]]
                    if not odf.empty else pd.DataFrame(columns=[
                        "severity", "check", "resource_name", "resource_group",
                        "evidence", "Est saving (USD)", "resource_id"]),
                    money_cols=("Est saving (USD)",), fail_cols=("severity",),
                    max_width=90)

    # Compute (detail)
    comp_rows = []
    for v in ctx["vms"]:
        comp_rows.append({"kind": "VM", "name": v.get("name"),
                          "resource_group": v.get("resourceGroup"),
                          "sku_or_size": v.get("vmSize"), "count": "",
                          "power_state": v.get("powerState"), "env": ctx["env_of"](v),
                          "Monthly cost (USD)": ctx["cost"].get((v.get("id") or "").lower())})
    for s in ctx["vmss"]:
        comp_rows.append({"kind": "VMSS", "name": s.get("name"),
                          "resource_group": s.get("resourceGroup"),
                          "sku_or_size": s.get("sku_name"), "count": s.get("sku_capacity"),
                          "power_state": "", "env": ctx["env_of"](s),
                          "Monthly cost (USD)": ctx["cost"].get((s.get("id") or "").lower())})
    for c in ctx["clusters"]:
        comp_rows.append({"kind": "AKS", "name": c.get("name"),
                          "resource_group": c.get("resourceGroup"),
                          "sku_or_size": c.get("sku_tier"), "count": "",
                          "power_state": "", "env": ctx["env_of"](c),
                          "Monthly cost (USD)": ctx["cost"].get((c.get("id") or "").lower())})
    comp_cols = ["kind", "name", "resource_group", "sku_or_size", "count",
                 "power_state", "env", "Monthly cost (USD)"]
    excel.add_table(wb, "Compute", pd.DataFrame(comp_rows, columns=comp_cols),
                    money_cols=("Monthly cost (USD)",))

    # Storage (detail) - accounts then disks
    st_rows = [{"name": s.get("name"), "resource_group": s.get("resourceGroup"),
                "redundancy": s.get("sku_name"), "kind": s.get("kind"),
                "access_tier": s.get("accessTier"),
                "Monthly cost (USD)": ctx["cost"].get((s.get("id") or "").lower())}
               for s in ctx["storage"]]
    disk_rows = [{"name": d.get("name"), "resource_group": d.get("resourceGroup"),
                  "sku": d.get("sku_name"), "size_gb": d.get("diskSizeGB"),
                  "state": d.get("diskState"),
                  "Monthly cost (USD)": ctx["cost"].get((d.get("id") or "").lower())}
                 for d in ctx["disks"]]
    excel.add_table(wb, "Storage", pd.DataFrame(st_rows, columns=[
        "name", "resource_group", "redundancy", "kind", "access_tier",
        "Monthly cost (USD)"]), money_cols=("Monthly cost (USD)",))
    excel.add_table(wb, "StorageDisks", pd.DataFrame(disk_rows, columns=[
        "name", "resource_group", "sku", "size_gb", "state", "Monthly cost (USD)"]),
        money_cols=("Monthly cost (USD)",), int_cols=("size_gb",))

    # PaaS & Network (detail)
    pn_rows = []
    for a in ctx["asps"]:
        pn_rows.append({"kind": "AppServicePlan", "name": a.get("name"),
                        "resource_group": a.get("resourceGroup"),
                        "sku_or_tier": "%s/%s" % (a.get("sku_tier") or "", a.get("sku_name") or ""),
                        "capacity_or_sites": a.get("numberOfSites"),
                        "Monthly cost (USD)": ctx["cost"].get((a.get("id") or "").lower())})
    for s in ctx["sql"]:
        pn_rows.append({"kind": "SQLDatabase", "name": s.get("name"),
                        "resource_group": s.get("resourceGroup"),
                        "sku_or_tier": "%s/%s" % (s.get("sku_tier") or "",
                                                  s.get("serviceObjective") or ""),
                        "capacity_or_sites": s.get("sku_capacity"),
                        "Monthly cost (USD)": ctx["cost"].get((s.get("id") or "").lower())})
    for g in ctx["gateways"]:
        pn_rows.append({"kind": FLAT_RATE_TYPES.get(g.get("type"), _short_type(g.get("type"))),
                        "name": g.get("name"), "resource_group": g.get("resourceGroup"),
                        "sku_or_tier": "%s/%s" % (g.get("sku_name") or "", g.get("sku_tier") or ""),
                        "capacity_or_sites": "",
                        "Monthly cost (USD)": ctx["cost"].get((g.get("id") or "").lower())})
    excel.add_table(wb, "PaaS&Network", pd.DataFrame(pn_rows, columns=[
        "kind", "name", "resource_group", "sku_or_tier", "capacity_or_sites",
        "Monthly cost (USD)"]), money_cols=("Monthly cost (USD)",), max_width=60)

    # Advisor (detail) - cost first, then others
    adv_rows = []
    for a in ctx["advisor_cost"] + ctx["advisor_other"]:
        rid = a.get("impactedResource") or a.get("id")
        adv_rows.append({"category": a.get("category"), "impact": a.get("impact"),
                         "problem": a.get("problem"), "solution": a.get("solution"),
                         "resource": _name_of(rid),
                         "Annual saving (USD)": _parse_advisor_saving(a) and
                         round(_parse_advisor_saving(a) * 12, 2),
                         "resource_id": rid})
    excel.add_table(wb, "Advisor", pd.DataFrame(adv_rows, columns=[
        "category", "impact", "problem", "solution", "resource",
        "Annual saving (USD)", "resource_id"]),
        money_cols=("Annual saving (USD)",), max_width=80)

    # Raw Resources (reference)
    rr_rows = [{"id": r.get("id"), "name": r.get("name"), "type": r.get("type"),
                "kind": r.get("kind"), "resource_group": r.get("resourceGroup"),
                "location": r.get("location"),
                "sku": r.get("sku_name") or r.get("sku_tier") or "",
                "env": ctx["env_of"](r), "tags": _tags_json(r.get("tags")),
                "Monthly cost (USD)": ctx["cost"].get((r.get("id") or "").lower())}
               for r in res]
    excel.add_table(wb, "RawResources", pd.DataFrame(rr_rows, columns=[
        "id", "name", "type", "kind", "resource_group", "location", "sku", "env",
        "tags", "Monthly cost (USD)"]), money_cols=("Monthly cost (USD)",),
        max_width=80, section="reference")

    # Raw Costs (reference) - includes (unmatched) ids
    if cost_on:
        known = {(r.get("id") or "").lower() for r in res}
        rc_rows = [{"resource_id": r["resource_id"], "month": r["month"],
                    "USD": r["USD"],
                    "cluster": "(unmatched)" if r["resource_id"] not in known else ""}
                   for r in raw_cost]
        excel.add_table(wb, "RawCosts", pd.DataFrame(rc_rows, columns=[
            "resource_id", "month", "USD", "cluster"]), money_cols=("USD",),
            max_width=90, section="reference")

    xlsx_path = excel.save(wb, out_path(args, "rearch", env_filter))
    log("Report written: %s" % xlsx_path)

    md_path = os.path.splitext(xlsx_path)[0] + ".md"
    svc_trend = svc_df.groupby("Month")["CostUSD"].sum().to_dict() \
        if cost_on and not svc_df.empty else {}
    write_md(md_path, sub_name, sub_id, args, ctx, findings, rg_cost, svc_trend,
             months, last_full)
    log("Narrative written: %s" % md_path)


if __name__ == "__main__":
    main()
