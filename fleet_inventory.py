"""Fleet inventory: every AKS cluster detail readable with subscription Reader
access, via Azure Resource Graph (a handful of API calls for the whole fleet).

Tabs: ReadMe, Clusters, NodePools, NetworkSecurity, Addons, Tags, Summary.

Usage:
  python fleet_inventory.py                 # interactive scope prompt
  python fleet_inventory.py --env dev
  python fleet_inventory.py --nonprod --out reports
"""
import datetime as dt

import pandas as pd

from azrep import excel
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

CLUSTER_COLS = [
    "cluster", "subscription", "environment", "environment_source", "location", "resource_group",
    "node_resource_group", "power_state", "provisioning_state",
    "kubernetes_version", "current_kubernetes_version", "sku_tier", "support_plan",
    "node_pools", "user_pools", "spot_pools", "total_nodes", "spot_nodes",
    "autoscaling_pools", "vm_sizes", "upgrade_channel", "node_os_upgrade_channel",
    "identity_type", "private_cluster", "subscription_id", "id",
]
NETSEC_COLS = [
    "cluster", "subscription", "environment", "environment_source", "network_plugin", "network_plugin_mode",
    "network_dataplane", "network_policy", "outbound_type", "lb_sku",
    "service_cidrs", "pod_cidrs", "dns_service_ip", "ip_families",
    "private_cluster", "authorized_ip_ranges", "public_fqdn", "private_fqdn",
    "rbac_enabled", "aad_managed", "azure_rbac", "local_accounts_disabled",
    "oidc_issuer", "workload_identity", "defender", "image_cleaner",
]
ADDON_COLS = [
    "cluster", "subscription", "environment", "environment_source", "addon_monitoring", "addon_azure_policy",
    "addon_keyvault_csi", "addon_appgw_ingress", "addon_virtual_node",
]


def main(argv=None):
    args = base_parser(__doc__.splitlines()[0]).parse_args(argv)
    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not clusters:
        log("No clusters in scope - nothing to report.")
        return

    cdf = pd.DataFrame(clusters)
    pdf = pd.DataFrame(pools)
    tags_rows = [{"cluster": c["cluster"], "subscription": c["subscription"],
                  "tag": k, "value": str(v)}
                 for c in clusters for k, v in (c["tags"] or {}).items()]
    tdf = pd.DataFrame(tags_rows) if tags_rows else pd.DataFrame(
        columns=["cluster", "subscription", "tag", "value"])

    def pivot(col, label):
        g = cdf.groupby(col).agg(clusters=("cluster", "count"),
                                 nodes=("total_nodes", "sum"),
                                 spot_nodes=("spot_nodes", "sum")).reset_index()
        g.insert(0, "group_by", label)
        g.columns = ["group_by", "value", "clusters", "nodes", "spot_nodes"]
        return g

    cdf["k8s_minor"] = cdf["kubernetes_version"].str.extract(r"^(\d+\.\d+)")[0].fillna(cdf["kubernetes_version"])
    summary = pd.concat([pivot("subscription", "subscription"),
                         pivot("environment", "environment"),
                         pivot("location", "region"),
                         pivot("k8s_minor", "k8s minor version"),
                         pivot("sku_tier", "sku tier"),
                         pivot("power_state", "power state")], ignore_index=True)

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Fleet Inventory", [
        "Generated: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Scope: %d subscription(s); environment filter: %s" % (len(sel), env_filter or "none"),
        "Clusters in scope: %d   |   Node pools: %d" % (len(cdf), len(pdf)),
        "",
        "Source: Azure Resource Graph (control-plane data only, no kubectl needed).",
        "Environment is resolved from cluster tags, then resource group tags (keys: %s)," % args.env_tag_keys,
        "then cluster/resource-group/name inference. '(unknown)' means none matched.",
        "",
        "Tabs: Clusters (one row per cluster), NodePools (one row per pool),",
        "NetworkSecurity, Addons, Tags (exploded key/value), Summary (counts).",
    ])
    excel.add_table(wb, "Clusters", cdf[CLUSTER_COLS],
                    int_cols=("node_pools", "user_pools", "spot_pools", "total_nodes",
                              "spot_nodes", "autoscaling_pools"))
    excel.add_table(wb, "NodePools", pdf, int_cols=("count", "min_count", "max_count",
                                                    "os_disk_gb", "max_pods"))
    excel.add_table(wb, "NetworkSecurity", cdf[NETSEC_COLS])
    excel.add_table(wb, "Addons", cdf[ADDON_COLS])
    excel.add_table(wb, "Tags", tdf)
    excel.add_table(wb, "Summary", summary,
                    int_cols=("clusters", "nodes", "spot_nodes"))

    path = excel.save(wb, out_path(args, "aks_inventory", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
