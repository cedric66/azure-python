"""Governance / hygiene scorecard for every AKS cluster in scope.
Pure Resource Graph data - fast, no extra API calls.

Each check returns PASS / FAIL / N-A. The Scorecard tab has one column per check
plus a live =COUNTIF score; FailDetails is the same data in long form for filtering.

Usage: python governance.py [--all|--nonprod|--env dev]
"""
import datetime as dt

import pandas as pd

from azrep import excel
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, is_prod, load_subscriptions, out_path, pick_scope


def _user_pools(pools):
    return [q for q in pools if q["mode"].lower() == "user"]


CHECKS = [
    ("api_server_locked_down",
     "Private cluster or authorized IP ranges on the API server",
     lambda c, ps: ("PASS", "") if c["private_cluster"] or c["authorized_ip_ranges"] > 0
     else ("FAIL", "API server is reachable from any internet IP")),
    ("local_accounts_disabled",
     "Kubernetes local accounts disabled",
     lambda c, ps: ("PASS", "") if c["local_accounts_disabled"]
     else ("FAIL", "static admin kubeconfig can be issued")),
    ("aad_integration",
     "Entra ID (AAD) integration enabled",
     lambda c, ps: ("PASS", "") if c["aad_managed"]
     else ("FAIL", "no managed AAD integration")),
    ("rbac_enabled",
     "Kubernetes RBAC enabled",
     lambda c, ps: ("PASS", "") if c["rbac_enabled"] else ("FAIL", "RBAC disabled")),
    ("managed_identity",
     "Managed identity (not service principal)",
     lambda c, ps: ("PASS", "") if "msi" in (c["identity_type"] or "").lower()
     or "assigned" in (c["identity_type"] or "").lower()
     else ("FAIL", "identity: %s" % (c["identity_type"] or "service principal"))),
    ("paid_tier_for_prod",
     "Standard/Premium tier (uptime SLA) on prod clusters",
     lambda c, ps: ("N-A", "") if not is_prod(c["environment"])
     else (("PASS", "") if c["sku_tier"].lower() in ("standard", "premium")
           else ("FAIL", "prod cluster on Free tier - no SLA"))),
    ("no_spot_in_prod",
     "No spot node pools on prod clusters",
     lambda c, ps: ("N-A", "") if not is_prod(c["environment"])
     else (("PASS", "") if c["spot_pools"] == 0
           else ("FAIL", "%d spot pool(s) in prod" % c["spot_pools"]))),
    ("autoscaler_on_user_pools",
     "Cluster autoscaler on all user pools",
     lambda c, ps: ("N-A", "no user pools") if not _user_pools(ps)
     else (("PASS", "") if all(q["autoscaling"] for q in _user_pools(ps))
           else ("FAIL", "pools without autoscaling: %s" %
                 ", ".join(q["pool"] for q in _user_pools(ps) if not q["autoscaling"])))),
    ("multi_zone",
     "All node pools spread over multiple availability zones",
     lambda c, ps: ("PASS", "") if ps and all(q["zones"] and "," in q["zones"] for q in ps)
     else ("FAIL", "pools without multi-zone: %s" %
           ", ".join(q["pool"] for q in ps if not (q["zones"] and "," in q["zones"])))),
    ("network_policy_set",
     "Network policy (azure/calico/cilium) configured",
     lambda c, ps: ("PASS", "") if c["network_policy"]
     else ("FAIL", "no network policy - pods are unrestricted east-west")),
    ("not_kubenet",
     "Not using kubenet (retired plugin path)",
     lambda c, ps: ("PASS", "") if (c["network_plugin"] or "").lower() != "kubenet"
     else ("FAIL", "kubenet is deprecated/retiring - plan migration to Azure CNI")),
    ("monitoring_addon",
     "Monitoring (Container Insights) addon",
     lambda c, ps: ("PASS", "") if c["addon_monitoring"]
     else ("FAIL", "omsagent addon disabled")),
    ("azure_policy_addon",
     "Azure Policy addon",
     lambda c, ps: ("PASS", "") if c["addon_azure_policy"]
     else ("FAIL", "policy addon disabled - Kubernetes policies cannot evaluate")),
    ("auto_upgrade_channel",
     "Cluster auto-upgrade channel set",
     lambda c, ps: ("PASS", "") if c["upgrade_channel"] and c["upgrade_channel"].lower() != "none"
     else ("FAIL", "no auto-upgrade channel")),
    ("node_os_channel",
     "Node OS security update channel set",
     lambda c, ps: ("PASS", "") if c["node_os_upgrade_channel"]
     and c["node_os_upgrade_channel"].lower() not in ("none", "unmanaged")
     else ("FAIL", "node OS channel: %s" % (c["node_os_upgrade_channel"] or "none"))),
    ("env_tagged",
     "Environment resolvable from tags or names",
     lambda c, ps: ("PASS", "") if c["environment"] != "(unknown)"
     else ("FAIL", "no environment tag, resource-group tag, or name signal")),
    ("workload_identity",
     "Workload identity (or OIDC issuer) enabled",
     lambda c, ps: ("PASS", "") if c["workload_identity"] or c["oidc_issuer"]
     else ("FAIL", "pods need secrets/SP creds to reach Azure APIs")),
]


def main(argv=None):
    args = base_parser("AKS governance / hygiene scorecard").parse_args(argv)
    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope.")
        return

    pools_by_cluster = {}
    for q in pools:
        pools_by_cluster.setdefault(q["cluster"], []).append(q)

    score_rows, detail_rows = [], []
    for c in clusters:
        ps = pools_by_cluster.get(c["cluster"], [])
        row = {"cluster": c["cluster"], "subscription": c["subscription"],
               "environment": c["environment"], "location": c["location"]}
        for cid, desc, fn in CHECKS:
            try:
                status, detail = fn(c, ps)
            except Exception as e:
                status, detail = "N-A", "check error: %s" % e
            row[cid] = status
            if status == "FAIL":
                detail_rows.append({"cluster": c["cluster"],
                                    "subscription": c["subscription"],
                                    "environment": c["environment"],
                                    "check": cid, "description": desc,
                                    "detail": detail})
        score_rows.append(row)

    sc = pd.DataFrame(score_rows)
    first_chk = excel.get_column_letter(5)
    last_chk = excel.get_column_letter(4 + len(CHECKS))
    n = len(sc)
    sc["Score"] = ["=COUNTIF(%s%d:%s%d,\"PASS\")/(COUNTIF(%s%d:%s%d,\"PASS\")"
                   "+COUNTIF(%s%d:%s%d,\"FAIL\"))"
                   % (first_chk, r, last_chk, r, first_chk, r, last_chk, r,
                      first_chk, r, last_chk, r) for r in range(2, n + 2)]

    details = pd.DataFrame(detail_rows) if detail_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "check", "description", "detail"])
    by_check = details.groupby(["check"]).agg(failing_clusters=("cluster", "count")) \
        .reset_index().sort_values("failing_clusters", ascending=False) \
        if not details.empty else pd.DataFrame(columns=["check", "failing_clusters"])
    legend = pd.DataFrame([(cid, desc) for cid, desc, _ in CHECKS],
                          columns=["check", "description"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Governance Scorecard", [
        "Generated: %s   Scope: %s   Clusters: %d   Checks: %d" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all",
         len(sc), len(CHECKS)),
        "",
        "All checks use control-plane configuration from Resource Graph (read-only).",
        "Score = PASS / (PASS + FAIL) per cluster; N-A checks are excluded.",
        "FailDetails explains every FAIL. CheckLegend describes each check.",
        "These are hygiene signals - some FAILs are deliberate trade-offs (e.g. an",
        "internal sandbox without monitoring). Review before acting.",
    ])
    excel.add_table(wb, "Scorecard", sc, fail_cols=[cid for cid, _, _ in CHECKS],
                    pct_cols=("Score",), colorscale_cols=())
    excel.add_table(wb, "FailDetails", details, max_width=80)
    excel.add_table(wb, "FailuresByCheck", by_check, int_cols=("failing_clusters",))
    excel.add_table(wb, "CheckLegend", legend, max_width=90)

    path = excel.save(wb, out_path(args, "aks_governance", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
