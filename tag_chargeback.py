"""AKS tag and chargeback-readiness report.

Checks required tags on clusters and their resource groups, shows value
distribution, and highlights clusters that are hard to allocate back to an
owner/application/cost center. Resource Graph only; no kubectl required.

Tabs: ReadMe, TagMatrix, MissingTags, TagCoverage, TagValues, RawTags, Summary.

Usage:
  python tag_chargeback.py --all
  python tag_chargeback.py --nonprod --required-tags environment,owner,costcenter,application
"""
import datetime as dt

import pandas as pd

from azrep import excel
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

DEFAULT_REQUIRED = ["environment", "owner", "costcenter", "application"]
ENV_KEYS = {"environment", "env", "stage"}


def _norm_key(k):
    return str(k or "").strip().lower()


def _tag_lookup(tags, key):
    low = {_norm_key(k): v for k, v in (tags or {}).items() if k is not None}
    return low.get(_norm_key(key))


def find_tag(cluster, key):
    val = _tag_lookup(cluster.get("tags") or {}, key)
    if val not in (None, ""):
        return str(val), "cluster"
    val = _tag_lookup(cluster.get("resource_group_tags") or {}, key)
    if val not in (None, ""):
        return str(val), "resource_group"
    if _norm_key(key) in ENV_KEYS and cluster.get("environment") != "(unknown)":
        return cluster["environment"], cluster.get("environment_source") or "resolved_env"
    return "", ""


def main(argv=None):
    p = base_parser("AKS tag and chargeback readiness")
    p.add_argument("--required-tags", default=",".join(DEFAULT_REQUIRED),
                   help="comma-separated required tag keys")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, _pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    required = [_norm_key(k) for k in args.required_tags.split(",") if _norm_key(k)]
    matrix_rows, missing_rows, value_rows, raw_rows = [], [], [], []
    for c in clusters:
        row = {
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "environment_source": c.get("environment_source", ""),
            "location": c["location"],
            "resource_group": c["resource_group"],
        }
        missing = []
        for tag in required:
            val, src = find_tag(c, tag)
            row[tag] = val
            row[tag + "_source"] = src
            if val:
                value_rows.append({
                    "tag": tag,
                    "value": val,
                    "source": src,
                    "cluster": c["cluster"],
                    "subscription": c["subscription"],
                    "environment": c["environment"],
                })
            else:
                missing.append(tag)
                missing_rows.append({
                    "cluster": c["cluster"],
                    "subscription": c["subscription"],
                    "environment": c["environment"],
                    "location": c["location"],
                    "resource_group": c["resource_group"],
                    "missing_tag": tag,
                    "impact": ("cost allocation blind spot" if tag in
                               ("owner", "costcenter", "cost_center", "application", "service")
                               else "reporting/filtering blind spot"),
                })
        row["missing_required_tags"] = len(missing)
        row["missing_tag_list"] = ", ".join(missing)
        row["chargeback_status"] = "READY" if not missing else (
            "PARTIAL" if len(missing) < len(required) else "NOT READY")
        matrix_rows.append(row)

        for scope, tags in (("cluster", c.get("tags") or {}),
                            ("resource_group", c.get("resource_group_tags") or {})):
            for k, v in tags.items():
                raw_rows.append({
                    "cluster": c["cluster"],
                    "subscription": c["subscription"],
                    "environment": c["environment"],
                    "scope": scope,
                    "tag": k,
                    "value": str(v),
                })

    matrix = pd.DataFrame(matrix_rows).sort_values(
        ["chargeback_status", "missing_required_tags", "cluster"],
        ascending=[False, False, True])
    missing = pd.DataFrame(missing_rows) if missing_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "location",
                 "resource_group", "missing_tag", "impact"])
    values = pd.DataFrame(value_rows) if value_rows else pd.DataFrame(
        columns=["tag", "value", "source", "cluster", "subscription", "environment"])
    raw = pd.DataFrame(raw_rows) if raw_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "scope", "tag", "value"])

    coverage_rows = []
    for tag in required:
        present = matrix[tag].astype(str).str.len() > 0
        cluster_src = matrix[tag + "_source"].eq("cluster")
        rg_src = matrix[tag + "_source"].eq("resource_group")
        resolved_src = matrix[tag + "_source"].eq("resolved_env")
        name_src = matrix[tag + "_source"].eq("name")
        csv_src = matrix[tag + "_source"].eq("subscription_csv")
        coverage_rows.append({
            "tag": tag,
            "clusters_present": int(present.sum()),
            "clusters_missing": int((~present).sum()),
            "coverage": float(present.mean()) if len(matrix) else 0.0,
            "from_cluster_tag": int(cluster_src.sum()),
            "from_resource_group_tag": int(rg_src.sum()),
            "from_resolved_env": int(resolved_src.sum()),
            "from_name": int(name_src.sum()),
            "from_subscription_csv": int(csv_src.sum()),
        })
    coverage = pd.DataFrame(coverage_rows).sort_values("coverage")

    if not values.empty:
        value_dist = values.groupby(["tag", "value", "source"]).agg(
            clusters=("cluster", "nunique"),
            subscriptions=("subscription", "nunique")).reset_index() \
            .sort_values(["tag", "clusters"], ascending=[True, False])
    else:
        value_dist = pd.DataFrame(columns=["tag", "value", "source",
                                           "clusters", "subscriptions"])

    by_sub = matrix.groupby(["subscription", "chargeback_status"]).agg(
        clusters=("cluster", "count")).reset_index()
    by_env = matrix.groupby(["environment", "chargeback_status"]).agg(
        clusters=("cluster", "count")).reset_index()
    summary = pd.DataFrame([
        ("Clusters in scope", len(matrix)),
        ("Required tags", ", ".join(required)),
        ("Chargeback READY clusters", int((matrix["chargeback_status"] == "READY").sum())),
        ("PARTIAL clusters", int((matrix["chargeback_status"] == "PARTIAL").sum())),
        ("NOT READY clusters", int((matrix["chargeback_status"] == "NOT READY").sum())),
        ("Missing tag findings", len(missing)),
        ("Distinct raw tag keys seen", raw["tag"].str.lower().nunique() if not raw.empty else 0),
    ], columns=["Item", "Value"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Tag and Chargeback Readiness", [
        "Generated: %s   Scope: %s   Clusters: %d" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all", len(matrix)),
        "",
        "A required tag can be present on the AKS cluster, inherited from the resource",
        "group, or for environment only resolved through the same tag/CSV logic used by",
        "the other reports. Cluster-level tags are still the strongest source for direct",
        "cost allocation because child resources in MC_* groups may not inherit tags.",
        "",
        "Use MissingTags for remediation and TagValues to normalize inconsistent owner,",
        "application, and cost-center values before trusting chargeback summaries.",
    ])
    excel.add_table(wb, "TagMatrix", matrix, int_cols=("missing_required_tags",),
                    fail_cols=("chargeback_status",), fail_values=("NOT READY",),
                    warn_values=("PARTIAL",), max_width=65)
    excel.add_table(wb, "MissingTags", missing, max_width=80)
    excel.add_table(wb, "TagCoverage", coverage,
                    int_cols=("clusters_present", "clusters_missing", "from_cluster_tag",
                              "from_resource_group_tag", "from_resolved_env",
                              "from_name", "from_subscription_csv"),
                    pct_cols=("coverage",), colorscale_cols=("coverage",))
    excel.add_table(wb, "TagValues", value_dist,
                    int_cols=("clusters", "subscriptions"), max_width=70)
    excel.add_table(wb, "RawTags", raw, max_width=80)
    excel.add_table(wb, "Summary", summary, max_width=80)
    excel.add_table(wb, "SummaryBySubscription", by_sub, int_cols=("clusters",))
    excel.add_table(wb, "SummaryByEnvironment", by_env, int_cols=("clusters",))

    path = excel.save(wb, out_path(args, "aks_tag_chargeback", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
