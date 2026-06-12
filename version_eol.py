"""Kubernetes version & EOL risk report.

Compares every cluster's version against the AKS supported-version list for its
region (one API call per distinct region, not per cluster) and measures node
image staleness per pool.

Tabs: ReadMe, VersionStatus, NodeImageAge, SupportedVersions, Summary.

Usage: python version_eol.py [--all|--env dev|--nonprod]
"""
import datetime as dt
import re

import pandas as pd

from azrep import excel
from azrep.armextras import aks_supported_versions, node_image_date
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

MINOR_RE = re.compile(r"^(\d+\.\d+)")


def minor(v):
    m = MINOR_RE.match(v or "")
    return m.group(1) if m else (v or "")


def main(argv=None):
    p = base_parser("AKS Kubernetes version & EOL risk")
    p.add_argument("--image-warn-days", type=int, default=60,
                   help="flag node images older than this many days")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    regions = sorted({c["location"] for c in clusters})
    log("Fetching supported AKS versions for %d region(s)..." % len(regions))
    region_sub = {}
    for c in clusters:
        region_sub.setdefault(c["location"], c["subscription_id"])
    supported = {r: aks_supported_versions(session, region_sub[r], r) for r in regions}

    rows = []
    for c in clusters:
        ver = c["current_kubernetes_version"] or c["kubernetes_version"]
        mv = minor(ver)
        sup = supported.get(c["location"]) or {}
        info = sup.get(mv)
        if info is None:
            status = "OUT OF SUPPORT"
            note = "minor %s is not in the supported list for %s" % (mv, c["location"])
        elif info["is_preview"]:
            status, note = "PREVIEW", "running a preview version"
        elif "KubernetesOfficial" not in info["support_plans"]:
            status = "LTS ONLY"
            note = ("community support ended; requires Premium tier + AKSLongTermSupport "
                    "(cluster tier: %s)" % c["sku_tier"])
        else:
            status, note = "SUPPORTED", ""
            sorted_minors = sorted((k for k in sup if MINOR_RE.match(k)),
                                   key=lambda x: [int(y) for y in x.split(".")])
            if sorted_minors and mv == sorted_minors[0]:
                status, note = "OLDEST SUPPORTED", "next AKS release will drop this minor"
        drift = ""
        pool_vers = {q["current_orchestrator_version"] or q["orchestrator_version"]
                     for q in pools if q["cluster"] == c["cluster"]}
        if pool_vers and any(minor(v) != mv for v in pool_vers if v):
            drift = "pools on " + ", ".join(sorted(v for v in pool_vers if v))
        rows.append({"cluster": c["cluster"], "subscription": c["subscription"],
                     "environment": c["environment"], "location": c["location"],
                     "control_plane_version": ver, "minor": mv, "status": status,
                     "upgrade_channel": c["upgrade_channel"] or "(none)",
                     "node_os_channel": c["node_os_upgrade_channel"] or "(none)",
                     "pool_version_drift": drift, "note": note,
                     "power_state": c["power_state"], "sku_tier": c["sku_tier"]})
    vs = pd.DataFrame(rows).sort_values(["status", "cluster"])

    today = dt.date.today()
    img_rows = []
    for q in pools:
        d = node_image_date(q["node_image_version"])
        age = (today - d).days if d else None
        img_rows.append({"cluster": q["cluster"], "subscription": q["subscription"],
                         "environment": q["environment"], "pool": q["pool"],
                         "node_image_version": q["node_image_version"],
                         "image_date": d.isoformat() if d else "",
                         "age_days": age,
                         "status": ("STALE" if age is not None and age > args.image_warn_days
                                    else ("OK" if age is not None else "UNKNOWN"))})
    img = pd.DataFrame(img_rows).sort_values("age_days", ascending=False, na_position="last") \
        if img_rows else pd.DataFrame()

    sup_rows = []
    for r in regions:
        for v, info in sorted((supported.get(r) or {}).items()):
            sup_rows.append({"region": r, "minor": v,
                             "support_plans": ", ".join(info["support_plans"]),
                             "is_preview": info["is_preview"],
                             "is_default": info["is_default"],
                             "patches": ", ".join(info["patches"])})
    supdf = pd.DataFrame(sup_rows)

    summ = vs.groupby(["status"]).agg(clusters=("cluster", "count")).reset_index()
    summ_env = vs.pivot_table(index="environment", columns="status", values="cluster",
                              aggfunc="count").fillna(0).astype(int).reset_index()

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Version & EOL Risk", [
        "Generated: %s   Scope: %s   Clusters: %d" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all", len(vs)),
        "",
        "Status meanings:",
        "  OUT OF SUPPORT - minor version absent from the region's supported list; upgrade now.",
        "  LTS ONLY       - only supported under Long-Term Support (needs Premium tier).",
        "  OLDEST SUPPORTED - in support but first to drop on the next AKS release.",
        "  PREVIEW        - preview version, no production SLA.",
        "NodeImageAge flags pools whose node image is older than %d days - usually a sign" % args.image_warn_days,
        "auto-upgrade/node OS channel is off and security patches are not being applied.",
    ])
    excel.add_table(wb, "VersionStatus", vs, fail_cols=("status",),
                    fail_values=("OUT OF SUPPORT",),
                    warn_values=("LTS ONLY", "OLDEST SUPPORTED", "PREVIEW"), max_width=60)
    excel.add_table(wb, "NodeImageAge", img, fail_cols=("status",),
                    fail_values=("STALE",), warn_values=("UNKNOWN",),
                    int_cols=("age_days",), max_width=60)
    excel.add_table(wb, "SupportedVersions", supdf, max_width=70, section="reference")
    excel.add_table(wb, "Summary", summ, int_cols=("clusters",), section="summary")
    excel.add_table(wb, "SummaryByEnv", summ_env, section="summary")

    path = excel.save(wb, out_path(args, "aks_version_eol", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
