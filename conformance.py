"""Golden-config conformance / drift report for every AKS cluster in scope.

The golden baseline IS a sandbox-config-shaped YAML (subset allowed): every key you set
becomes a rule, keys you omit are not checked. Prove the baseline deploys (sandbox deploy
golden.yaml), then measure fleet drift against it here.

Usage: python conformance.py --golden golden.yaml [--all|--nonprod|--env dev]
"""
import datetime as dt

import pandas as pd

from azrep import excel
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope
from version_eol import minor

TIER_ORDER = {"free": 0, "standard": 1, "premium": 2}


def _ver_key(v):
    return tuple(int(x) for x in str(v or "").split(".") if x.isdigit())


def _user_pools(pools):
    return [q for q in pools if q["mode"].lower() == "user"]


def _eq_rule(rid, desc, want, actual_key, transform=lambda v: str(v or "").lower()):
    target = transform(want)

    def fn(c, ps):
        got = transform(c[actual_key])
        return ("PASS", "") if got == target else \
            ("FAIL", "%s is `%s`, golden wants `%s`" % (actual_key, c[actual_key], want))
    return (rid, desc, fn)


def _bool_rule(rid, desc, want, actual_key):
    def fn(c, ps):
        return ("PASS", "") if bool(c[actual_key]) == bool(want) else \
            ("FAIL", "%s is %s, golden wants %s" % (actual_key, bool(c[actual_key]), bool(want)))
    return (rid, desc, fn)


def build_rules(golden):
    """One rule per key PRESENT in the golden config; omitted keys are unconstrained."""
    gc = golden.get("cluster") or {}
    net = gc.get("network") or {}
    rules = []

    if gc.get("kubernetes_version"):
        want = minor(gc["kubernetes_version"])

        def version_fn(c, ps, want=want):
            got = minor(c["kubernetes_version"])
            if not got:
                return ("N-A", "no version reported")
            return ("PASS", "") if _ver_key(got) >= _ver_key(want) else \
                ("FAIL", "on %s, golden minimum is %s" % (c["kubernetes_version"], want))
        rules.append(("version_minimum", "Kubernetes minor >= %s" % want, version_fn))

    for key, actual, rid in (("plugin", "network_plugin", "net_plugin"),
                             ("plugin_mode", "network_plugin_mode", "net_plugin_mode"),
                             ("policy", "network_policy", "net_policy"),
                             ("dataplane", "network_dataplane", "net_dataplane"),
                             ("outbound_type", "outbound_type", "net_outbound"),
                             ("load_balancer_sku", "lb_sku", "net_lb_sku")):
        if net.get(key):
            rules.append(_eq_rule(rid, "Network %s = %s" % (key, net[key]), net[key], actual))

    for key, actual, rid, desc in (
            ("private_cluster", "private_cluster", "private_cluster", "Private API server"),
            ("disable_local_accounts", "local_accounts_disabled", "local_accounts",
             "Local accounts disabled"),
            ("enable_rbac", "rbac_enabled", "rbac", "Kubernetes RBAC"),
            ("azure_policy_addon", "addon_azure_policy", "policy_addon", "Azure Policy addon"),
            ("oidc_issuer", "oidc_issuer", "oidc_issuer", "OIDC issuer"),
            ("workload_identity", "workload_identity", "workload_identity",
             "Workload identity")):
        if key in gc:
            rules.append(_bool_rule(rid, "%s = %s" % (desc, bool(gc[key])), gc[key], actual))

    aad = gc.get("aad_profile")
    if aad:
        rules.append(_bool_rule("aad_managed", "Managed Entra ID integration", True,
                                "aad_managed"))
        if aad.get("enableAzureRBAC"):
            rules.append(_bool_rule("azure_rbac", "Azure RBAC for Kubernetes", True,
                                    "azure_rbac"))

    if gc.get("sku_tier"):
        floor = TIER_ORDER.get(str(gc["sku_tier"]).lower(), 0)

        def tier_fn(c, ps, floor=floor):
            got = TIER_ORDER.get(c["sku_tier"].lower(), 0)
            return ("PASS", "") if got >= floor else \
                ("FAIL", "tier %s below golden %s" % (c["sku_tier"], gc["sku_tier"]))
        rules.append(("sku_tier_min", "SKU tier >= %s" % gc["sku_tier"], tier_fn))

    auto = gc.get("auto_upgrade_profile") or {}
    if auto.get("upgradeChannel"):
        rules.append(_eq_rule("upgrade_channel", "Auto-upgrade channel = %s"
                              % auto["upgradeChannel"], auto["upgradeChannel"],
                              "upgrade_channel"))
    if auto.get("nodeOSUpgradeChannel"):
        rules.append(_eq_rule("node_os_channel", "Node OS channel = %s"
                              % auto["nodeOSUpgradeChannel"], auto["nodeOSUpgradeChannel"],
                              "node_os_upgrade_channel"))

    tag_keys = sorted(k for k in (golden.get("tags") or {}) if k != "cloned_from")
    if tag_keys:
        def tags_fn(c, ps, keys=tag_keys):
            have = {str(k).lower() for k in (c["tags"] or {})}
            missing = [k for k in keys if k.lower() not in have]
            return ("PASS", "") if not missing else \
                ("FAIL", "missing tag(s): %s" % ", ".join(missing))
        rules.append(("required_tags", "Cluster tags include: %s" % ", ".join(tag_keys),
                      tags_fn))

    gpools = gc.get("node_pools") or []
    if gpools:
        if any(p.get("autoscaling") for p in gpools if (p.get("mode") or "User") == "User"):
            def autoscale_fn(c, ps):
                user = _user_pools(ps)
                if not user:
                    return ("N-A", "no user pools")
                bad = [q["pool"] for q in user if not q["autoscaling"]]
                return ("PASS", "") if not bad else \
                    ("FAIL", "user pools without autoscaling: %s" % ", ".join(bad))
            rules.append(("pool_autoscaling", "Autoscaler on all user pools", autoscale_fn))

        if any(len(p.get("zones") or []) > 1 for p in gpools):
            def zones_fn(c, ps):
                if not ps:
                    return ("N-A", "no pools")
                bad = [q["pool"] for q in ps if not (q["zones"] and "," in q["zones"])]
                return ("PASS", "") if not bad else \
                    ("FAIL", "pools without multi-zone: %s" % ", ".join(bad))
            rules.append(("pool_multi_zone", "All pools span multiple zones", zones_fn))

        floors = [int(p["max_pods"]) for p in gpools if p.get("max_pods")]
        if floors:
            floor = min(floors)

            def maxpods_fn(c, ps, floor=floor):
                if not ps:
                    return ("N-A", "no pools")
                bad = ["%s=%s" % (q["pool"], q["max_pods"]) for q in ps
                       if int(q["max_pods"] or 0) < floor]
                return ("PASS", "") if not bad else \
                    ("FAIL", "pools below max_pods %d: %s" % (floor, ", ".join(bad)))
            rules.append(("pool_max_pods_min", "Pool max_pods >= %d" % floor, maxpods_fn))

        skus = {str(p["os_sku"]).lower() for p in gpools if p.get("os_sku")}
        if skus:
            def sku_fn(c, ps, skus=skus):
                linux = [q for q in ps if q["os_type"].lower() != "windows"]
                if not linux:
                    return ("N-A", "no linux pools")
                bad = ["%s=%s" % (q["pool"], q["os_sku"]) for q in linux
                       if q["os_sku"].lower() not in skus]
                return ("PASS", "") if not bad else \
                    ("FAIL", "pools off golden OS SKU (%s): %s"
                     % ("/".join(sorted(skus)), ", ".join(bad)))
            rules.append(("pool_os_sku", "Pool OS SKU in {%s}" % ", ".join(sorted(skus)),
                          sku_fn))
    return rules


def main(argv=None):
    parser = base_parser("Golden-config conformance / drift report")
    parser.add_argument("--golden", required=True,
                        help="Golden baseline YAML/JSON (sandbox config schema, subset ok)")
    args = parser.parse_args(argv)

    from azrep.sandbox import load_config
    golden = load_config(args.golden, validate=False)
    rules = build_rules(golden)
    if not rules:
        log("Golden config %s defines no checkable keys - nothing to do." % args.golden)
        return
    log("Golden baseline %s -> %d rule(s)." % (args.golden, len(rules)))

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
        pools_by_cluster.setdefault(q["cluster_id"].lower(), []).append(q)

    score_rows, detail_rows = [], []
    for c in clusters:
        ps = pools_by_cluster.get(c["id"].lower(), [])
        row = {"cluster": c["cluster"], "subscription": c["subscription"],
               "environment": c["environment"], "location": c["location"]}
        for rid, desc, fn in rules:
            try:
                status, detail = fn(c, ps)
            except Exception as e:
                status, detail = "N-A", "rule error: %s" % e
            row[rid] = status
            if status == "FAIL":
                detail_rows.append({"cluster": c["cluster"],
                                    "subscription": c["subscription"],
                                    "environment": c["environment"],
                                    "rule": rid, "description": desc, "detail": detail})
        score_rows.append(row)

    sc = pd.DataFrame(score_rows).sort_values(["environment", "cluster"]) \
        .reset_index(drop=True)
    first_chk = excel.get_column_letter(5)
    last_chk = excel.get_column_letter(4 + len(rules))
    sc["Score"] = ["=COUNTIF(%s%d:%s%d,\"PASS\")/(COUNTIF(%s%d:%s%d,\"PASS\")"
                   "+COUNTIF(%s%d:%s%d,\"FAIL\"))"
                   % (first_chk, r, last_chk, r, first_chk, r, last_chk, r,
                      first_chk, r, last_chk, r) for r in range(2, len(sc) + 2)]

    details = pd.DataFrame(detail_rows) if detail_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "rule", "description", "detail"])
    by_rule = details.groupby(["rule"]).agg(failing_clusters=("cluster", "count")) \
        .reset_index().sort_values("failing_clusters", ascending=False) \
        if not details.empty else pd.DataFrame(columns=["rule", "failing_clusters"])
    legend = pd.DataFrame([(rid, desc) for rid, desc, _ in rules],
                          columns=["rule", "description"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Golden-Config Conformance", [
        "Generated: %s   Scope: %s   Clusters: %d   Rules: %d   Golden: %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), env_filter or "all",
         len(sc), len(rules), args.golden),
        "",
        "Every key present in the golden config is a rule; omitted keys are unconstrained.",
        "Score = PASS / (PASS + FAIL) per cluster; N-A rules are excluded.",
        "Validate the baseline itself with: sandbox deploy <golden>.yaml --yes --wait,",
        "then this report filtered to that cluster must score 100%.",
    ])
    excel.add_table(wb, "Scorecard", sc, fail_cols=[rid for rid, _, _ in rules],
                    pct_cols=("Score",))
    excel.add_table(wb, "FailDetails", details, max_width=80)
    excel.add_table(wb, "FailuresByRule", by_rule, int_cols=("failing_clusters",),
                    section="summary")
    excel.add_table(wb, "RuleLegend", legend, max_width=90, section="reference")

    path = excel.save(wb, out_path(args, "aks_conformance", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
