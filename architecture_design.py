"""Architecture design snapshot from actual Azure state.

Creates a multi-tab XLSX plus an optional Markdown design document with Mermaid
diagrams. Works with subscription Reader data from ARM/Resource Graph only; no
kubectl access is required.

Tabs: ReadMe, DesignSummary, Clusters, NodePools, Network, Subnets, Resources,
ResourceCounts, Components, Diagrams.

Usage:
  python architecture_design.py --cluster aks-dev-01 --all
  python architecture_design.py --subs contoso-platform --resource-group rg-apps-dev
  python architecture_design.py --subs contoso-platform --all
"""
import datetime as dt
import os
import re

import pandas as pd

from azrep import arg, excel
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import (base_parser, cluster_filter_empty, load_subscriptions,
                        out_path, pick_scope)

# Azure Resource Graph uses a stricter KQL subset than Kusto/Log Analytics:
#  - it rejects line comments (// ...), so the query must start with the table;
#  - `kind` is a reserved keyword, so it must be projected bare (not `kind = ...`),
#    or the parser fails at the `=`. `=` aliasing itself is fine (see type below).
RESOURCE_INVENTORY_KQL = """
Resources
%s
| project id, name, type = tolower(type), subscriptionId, resourceGroup, location,
    kind,
    sku_name = tostring(sku.name),
    sku_tier = tostring(sku.tier),
    provisioning_state = tostring(properties.provisioningState),
    tags
"""


RESOURCE_TYPES = {
    "microsoft.containerservice/managedclusters": "AKS control plane",
    "microsoft.compute/virtualmachinescalesets": "AKS node VMSS",
    "microsoft.compute/disks": "Managed disk",
    "microsoft.network/loadbalancers": "Load balancer",
    "microsoft.network/publicipaddresses": "Public IP",
    "microsoft.network/networksecuritygroups": "Network security group",
    "microsoft.network/routetables": "Route table",
    "microsoft.network/natgateways": "NAT gateway",
    "microsoft.network/virtualnetworks": "Virtual network",
    "microsoft.operationalinsights/workspaces": "Log Analytics workspace",
    "microsoft.insights/components": "Application Insights",
    "microsoft.managedidentity/userassignedidentities": "Managed identity",
    "microsoft.keyvault/vaults": "Key Vault",
    "microsoft.containerregistry/registries": "Container Registry",
}


def _split_csv(raw):
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _kql_string(value):
    return "'" + str(value).lower().replace("'", "''") + "'"


def _where_resource_groups(resource_groups):
    rgs = sorted({str(rg).lower() for rg in (resource_groups or []) if str(rg).strip()})
    if not rgs:
        return ""
    return "| where tolower(resourceGroup) in (%s)" % ", ".join(_kql_string(rg) for rg in rgs)


def _join(v, sep=", "):
    if isinstance(v, (list, tuple, set)):
        return sep.join(str(x) for x in v if x is not None)
    return "" if v is None else str(v)


def _resource_class(resource_type):
    return RESOURCE_TYPES.get(str(resource_type or "").lower(), "Azure resource")


def _safe_id(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "x")).strip("_") or "x"


def _friendly_bool(value):
    return "Yes" if bool(value) else "No"


def _tags_summary(tags):
    if not isinstance(tags, dict) or not tags:
        return ""
    return "; ".join("%s=%s" % (k, v) for k, v in sorted(tags.items()))


def load_resources(session, subs, resource_groups=None):
    sub_ids = [s["subscription_id"] for s in subs]
    sub_names = {s["subscription_id"]: s["subscription_name"] or s["subscription_id"] for s in subs}
    rows = arg.query(session, RESOURCE_INVENTORY_KQL % _where_resource_groups(resource_groups), sub_ids)
    out = []
    for r in rows:
        sid = (r.get("subscriptionId") or "").lower()
        r = dict(r)
        r["subscription"] = sub_names.get(sid, sid)
        r["component_class"] = _resource_class(r.get("type"))
        r["tags_summary"] = _tags_summary(r.get("tags"))
        out.append(r)
    return out


def load_subnets(session, subs, referenced_ids=None, resource_groups=None, subscription_scope=False):
    sub_ids = [s["subscription_id"] for s in subs]
    sub_names = {s["subscription_id"]: s["subscription_name"] or s["subscription_id"] for s in subs}
    refs = {str(x).lower() for x in (referenced_ids or []) if x}
    rgs = {str(x).lower() for x in (resource_groups or []) if x}
    rows = arg.query(session, arg.SUBNETS_KQL, sub_ids)
    out = []
    for s in rows:
        sid = (s.get("subscriptionId") or "").lower()
        subnet_id = (s.get("id") or "").lower()
        if not subscription_scope and subnet_id not in refs and str(s.get("resourceGroup") or "").lower() not in rgs:
            continue
        endpoints = s.get("serviceEndpoints") or []
        delegations = s.get("delegations") or []
        row = {
            "subnet_id": s.get("id"),
            "subscription": sub_names.get(sid, sid),
            "resource_group": s.get("resourceGroup") or "",
            "vnet": s.get("vnet") or "",
            "subnet": s.get("name") or "",
            "location": s.get("location") or "",
            "prefixes": _join(s.get("addressPrefixes") or s.get("addressPrefix")),
            "referenced_by_aks": subnet_id in refs,
            "nsg_id": s.get("nsgId") or "",
            "route_table_id": s.get("routeTableId") or "",
            "nat_gateway_id": s.get("natGatewayId") or "",
            "service_endpoints": _join([e.get("service") for e in endpoints if isinstance(e, dict)]),
            "delegations": _join([d.get("name") for d in delegations if isinstance(d, dict)]),
        }
        out.append(row)
    return out


def resource_scope(args, clusters, env_filter):
    explicit_rgs = _split_csv(getattr(args, "resource_group", None))
    if explicit_rgs:
        return "resource_group", explicit_rgs
    if env_filter is not None or not cluster_filter_empty():
        rgs = set()
        for c in clusters:
            if c.get("resource_group"):
                rgs.add(c["resource_group"])
            if c.get("node_resource_group"):
                rgs.add(c["node_resource_group"])
        return "cluster_set", sorted(rgs)
    return "subscription", []


def network_rows(clusters):
    rows = []
    for c in clusters:
        rows.append({
            "cluster": c["cluster"],
            "subscription": c["subscription"],
            "environment": c["environment"],
            "location": c["location"],
            "network_plugin": c["network_plugin"],
            "network_plugin_mode": c["network_plugin_mode"],
            "network_policy": c["network_policy"],
            "network_dataplane": c["network_dataplane"],
            "outbound_type": c["outbound_type"],
            "load_balancer_sku": c["lb_sku"],
            "private_cluster": c["private_cluster"],
            "authorized_ip_ranges": c["authorized_ip_ranges"],
            "public_fqdn": c["public_fqdn"],
            "private_fqdn": c["private_fqdn"],
            "service_cidrs": c["service_cidrs"],
            "pod_cidrs": c["pod_cidrs"],
            "dns_service_ip": c["dns_service_ip"],
            "ip_families": c["ip_families"],
        })
    return rows


def component_rows(clusters, pools, resources):
    res_by_rg = {}
    for r in resources:
        key = ((r.get("subscriptionId") or "").lower(), str(r.get("resourceGroup") or "").lower())
        res_by_rg.setdefault(key, []).append(r)
    pools_by_cluster = {}
    for p in pools:
        pools_by_cluster.setdefault((p["cluster_id"] or "").lower(), []).append(p)

    rows = []
    for c in clusters:
        cid = (c["id"] or "").lower()
        rows.extend([
            {
                "cluster": c["cluster"],
                "component": "AKS control plane",
                "name": c["cluster"],
                "resource_group": c["resource_group"],
                "type": "Microsoft.ContainerService/managedClusters",
                "sku_or_size": c["sku_tier"],
                "state": c["provisioning_state"],
                "details": "version=%s; power=%s; support_plan=%s" % (
                    c["kubernetes_version"], c["power_state"], c["support_plan"]),
            },
            {
                "cluster": c["cluster"],
                "component": "API server",
                "name": c["public_fqdn"] or c["private_fqdn"] or "(no fqdn)",
                "resource_group": c["resource_group"],
                "type": "control-plane endpoint",
                "sku_or_size": "private=%s" % _friendly_bool(c["private_cluster"]),
                "state": "authorized ranges=%s" % c["authorized_ip_ranges"],
                "details": "public=%s; private=%s" % (c["public_fqdn"], c["private_fqdn"]),
            },
            {
                "cluster": c["cluster"],
                "component": "Identity and addons",
                "name": c["identity_type"],
                "resource_group": c["resource_group"],
                "type": "configuration",
                "sku_or_size": "",
                "state": "",
                "details": "AAD=%s; Azure RBAC=%s; policy addon=%s; monitoring=%s; workload identity=%s" % (
                    _friendly_bool(c["aad_managed"]), _friendly_bool(c["azure_rbac"]),
                    _friendly_bool(c["addon_azure_policy"]), _friendly_bool(c["addon_monitoring"]),
                    _friendly_bool(c["workload_identity"])),
            },
        ])
        for p in pools_by_cluster.get(cid, []):
            rows.append({
                "cluster": c["cluster"],
                "component": "Node pool",
                "name": p["pool"],
                "resource_group": c["node_resource_group"],
                "type": p["mode"],
                "sku_or_size": p["vm_size"],
                "state": p["power_state"],
                "details": "nodes=%s; autoscaling=%s; min=%s; max=%s; priority=%s; zones=%s" % (
                    p["count"], p["autoscaling"], p["min_count"], p["max_count"],
                    p["priority"], p["zones"]),
            })
        for rg in (c.get("resource_group"), c.get("node_resource_group")):
            for r in res_by_rg.get((c["subscription_id"].lower(), str(rg or "").lower()), []):
                if (r.get("id") or "").lower() == cid:
                    continue
                rows.append({
                    "cluster": c["cluster"],
                    "component": r["component_class"],
                    "name": r.get("name") or "",
                    "resource_group": r.get("resourceGroup") or "",
                    "type": r.get("type") or "",
                    "sku_or_size": r.get("sku_name") or r.get("sku_tier") or "",
                    "state": r.get("provisioning_state") or "",
                    "details": r.get("id") or "",
                })
    return rows


def resource_counts(resources):
    if not resources:
        return []
    df = pd.DataFrame(resources)
    grp = (df.groupby(["subscription", "resourceGroup", "component_class", "type"])
             .size().reset_index(name="count")
             .sort_values(["subscription", "resourceGroup", "component_class", "type"]))
    return grp.to_dict("records")


def diagram_for_cluster(cluster, pools, resources, subnets):
    cid = (cluster["id"] or "").lower()
    pool_rows = [p for p in pools if (p.get("cluster_id") or "").lower() == cid]
    rg_resources = [r for r in resources
                    if str(r.get("resourceGroup") or "").lower() in {
                        str(cluster.get("resource_group") or "").lower(),
                        str(cluster.get("node_resource_group") or "").lower(),
                    }]
    type_counts = {}
    for r in rg_resources:
        typ = r.get("type") or ""
        type_counts[typ] = type_counts.get(typ, 0) + 1
    subnet_ids = {p.get("vnet_subnet_id") for p in pool_rows if p.get("vnet_subnet_id")}
    subnet_ids.update(p.get("pod_subnet_id") for p in pool_rows if p.get("pod_subnet_id"))
    subnet_labels = []
    for s in subnets:
        if str(s.get("subnet_id") or "").lower() in {str(x).lower() for x in subnet_ids if x}:
            subnet_labels.append("%s/%s" % (s.get("vnet"), s.get("subnet")))

    root = _safe_id(cluster["cluster"])
    lines = [
        "flowchart LR",
        '  subgraph sub_%s["Subscription: %s"]' % (root, cluster["subscription"]),
        '    rg_%s["Cluster RG: %s"]' % (root, cluster["resource_group"]),
        '    aks_%s["AKS: %s\\nKubernetes %s\\nTier %s"]' % (
            root, cluster["cluster"], cluster["kubernetes_version"], cluster["sku_tier"]),
        '    api_%s["API server\\nprivate=%s\\nauthorized ranges=%s"]' % (
            root, _friendly_bool(cluster["private_cluster"]), cluster["authorized_ip_ranges"]),
        '    nrg_%s["Node RG: %s"]' % (root, cluster["node_resource_group"] or "(unknown)"),
        "  end",
        "  rg_%s --> aks_%s" % (root, root),
        "  aks_%s --> api_%s" % (root, root),
        "  aks_%s --> nrg_%s" % (root, root),
    ]
    for p in pool_rows:
        pid = "%s_%s" % (root, _safe_id(p["pool"]))
        lines.append('  pool_%s["Pool: %s\\n%s %s\\n%s nodes"]' % (
            pid, p["pool"], p["mode"], p["vm_size"], p["count"]))
        lines.append("  nrg_%s --> pool_%s" % (root, pid))
    for typ, label in (
        ("microsoft.compute/virtualmachinescalesets", "VM scale sets"),
        ("microsoft.network/loadbalancers", "Load balancers"),
        ("microsoft.network/publicipaddresses", "Public IPs"),
        ("microsoft.compute/disks", "Managed disks"),
    ):
        count = type_counts.get(typ, 0)
        if count:
            nid = "%s_%s" % (root, _safe_id(label))
            lines.append('  res_%s["%s\\ncount=%s"]' % (nid, label, count))
            lines.append("  nrg_%s --> res_%s" % (root, nid))
    if subnet_labels:
        lines.append('  net_%s["Referenced subnet(s)\\n%s"]' % (
            root, "\\n".join(sorted(set(subnet_labels))[:6])))
        for p in pool_rows:
            lines.append("  pool_%s_%s --> net_%s" % (root, _safe_id(p["pool"]), root))
    return "\n".join(lines)


def build_diagrams(clusters, pools, resources, subnets):
    return [{"cluster": c["cluster"], "diagram": diagram_for_cluster(c, pools, resources, subnets)}
            for c in clusters]


def write_design_doc(path, clusters, resources, components, diagrams, scope_label):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# AKS Architecture Design Snapshot\n\n")
        f.write("Generated: %s\n\n" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        f.write("Scope: %s\n\n" % scope_label)
        f.write("This document is generated from Azure ARM/Resource Graph state only. ")
        f.write("It does not require kubectl and does not inspect in-cluster Kubernetes objects.\n\n")
        f.write("## Summary\n\n")
        f.write("| Item | Value |\n|---|---:|\n")
        f.write("| Clusters | %d |\n" % len(clusters))
        f.write("| Azure resources in design scope | %d |\n" % len(resources))
        f.write("| Design components | %d |\n\n" % len(components))
        for c in clusters:
            f.write("## Cluster: %s\n\n" % c["cluster"])
            f.write("| Field | Value |\n|---|---|\n")
            for key in ("subscription", "environment", "location", "resource_group",
                        "node_resource_group", "kubernetes_version", "sku_tier",
                        "network_plugin", "network_policy", "outbound_type"):
                f.write("| `%s` | %s |\n" % (key, c.get(key) or ""))
            diag = next((d["diagram"] for d in diagrams if d["cluster"] == c["cluster"]), "")
            if diag:
                f.write("\n```mermaid\n%s\n```\n\n" % diag)
        f.write("## Notes\n\n")
        f.write("- Load balancers, public IPs, VMSS and disks are inferred from the cluster and node resource groups.\n")
        f.write("- Shared networking may live outside the cluster RG; referenced subnets are included when node-pool subnet IDs are visible.\n")
        f.write("- Application workloads, Services, Ingresses, namespaces and pod-level objects require kubectl access and are outside this report.\n")
    return path


def build_parser():
    p = base_parser("Architecture design snapshot from actual Azure state")
    p.add_argument("--resource-group", "--rg", dest="resource_group",
                   help="comma-separated resource group names to design directly")
    p.add_argument("--no-doc", action="store_true",
                   help="write only the XLSX workbook; skip the Markdown design document")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.1)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)

    scope_kind, resource_groups = resource_scope(args, clusters, env_filter)
    subscription_scope = scope_kind == "subscription"
    log("Design scope: %s%s" % (
        scope_kind, " (%s)" % ", ".join(resource_groups[:8]) if resource_groups else ""))

    resources = load_resources(session, sel, resource_groups if not subscription_scope else None)
    referenced_subnets = set()
    for p in pools:
        if p.get("vnet_subnet_id"):
            referenced_subnets.add(p["vnet_subnet_id"])
        if p.get("pod_subnet_id"):
            referenced_subnets.add(p["pod_subnet_id"])
    subnets = load_subnets(session, sel, referenced_subnets, resource_groups,
                           subscription_scope=subscription_scope)

    comps = component_rows(clusters, pools, resources)
    diagrams = build_diagrams(clusters, pools, resources, subnets)

    cdf = pd.DataFrame(clusters)
    pdf = pd.DataFrame(pools)
    ndf = pd.DataFrame(network_rows(clusters), columns=[
        "cluster", "subscription", "environment", "location", "network_plugin",
        "network_plugin_mode", "network_policy", "network_dataplane", "outbound_type",
        "load_balancer_sku", "private_cluster", "authorized_ip_ranges", "public_fqdn",
        "private_fqdn", "service_cidrs", "pod_cidrs", "dns_service_ip", "ip_families"])
    sdf = pd.DataFrame(subnets, columns=[
        "subnet_id", "subscription", "resource_group", "vnet", "subnet", "location",
        "prefixes", "referenced_by_aks", "nsg_id", "route_table_id", "nat_gateway_id",
        "service_endpoints", "delegations"])
    rdf = pd.DataFrame(resources, columns=[
        "subscription", "subscriptionId", "resourceGroup", "name", "type",
        "component_class", "location", "kind", "sku_name", "sku_tier",
        "provisioning_state", "tags_summary", "id"])
    rcdf = pd.DataFrame(resource_counts(resources), columns=[
        "subscription", "resourceGroup", "component_class", "type", "count"])
    compdf = pd.DataFrame(comps, columns=[
        "cluster", "component", "name", "resource_group", "type", "sku_or_size",
        "state", "details"])
    ddf = pd.DataFrame(diagrams, columns=["cluster", "diagram"])

    summary = pd.DataFrame([
        ("Design scope", scope_kind),
        ("Subscriptions", len(sel)),
        ("Clusters in scope", len(clusters)),
        ("Node pools in scope", len(pools)),
        ("Resource groups queried", len(resource_groups) if resource_groups else "all in subscription scope"),
        ("Azure resources in design scope", len(resources)),
        ("Referenced subnets", len(subnets)),
        ("Markdown document", "disabled" if args.no_doc else "enabled"),
    ], columns=["Item", "Value"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Architecture Design Snapshot", [
        "Generated: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Scope: %s; environment filter: %s" % (scope_kind, env_filter or "none"),
        "Subscriptions: %d   |   Clusters: %d   |   Resources: %d" % (
            len(sel), len(clusters), len(resources)),
        "",
        "Source: Azure Resource Graph and AKS ARM properties only.",
        "No kubectl access is required. In-cluster Services, Ingresses, namespaces,",
        "pods and app topology are not visible from subscription-level Reader.",
        "",
        "The Markdown companion file contains Mermaid diagrams for cluster-level views.",
    ])
    excel.add_table(wb, "DesignSummary", summary)
    cluster_cols = [
        "cluster", "subscription", "environment", "environment_source", "location",
        "resource_group", "node_resource_group", "power_state", "provisioning_state",
        "kubernetes_version", "current_kubernetes_version", "sku_tier", "support_plan",
        "node_pools", "user_pools", "spot_pools", "total_nodes", "spot_nodes",
        "vm_sizes", "identity_type", "addon_monitoring", "addon_azure_policy",
        "addon_keyvault_csi", "private_cluster", "id",
    ]
    excel.add_table(wb, "Clusters", cdf[cluster_cols] if not cdf.empty else pd.DataFrame(columns=cluster_cols),
                    int_cols=("node_pools", "user_pools", "spot_pools", "total_nodes", "spot_nodes"))
    excel.add_table(wb, "NodePools", pdf, int_cols=("count", "min_count", "max_count", "max_pods"))
    excel.add_table(wb, "Network", ndf)
    excel.add_table(wb, "Subnets", sdf)
    excel.add_table(wb, "Resources", rdf, max_width=80)
    excel.add_table(wb, "ResourceCounts", rcdf, int_cols=("count",))
    excel.add_table(wb, "Components", compdf, max_width=90)
    excel.add_table(wb, "Diagrams", ddf, max_width=120)

    xlsx_path = excel.save(wb, out_path(args, "aks_design", env_filter))
    log("Report written: %s" % xlsx_path)
    if not args.no_doc:
        md_path = os.path.splitext(xlsx_path)[0] + ".md"
        write_design_doc(md_path, clusters, resources, comps, diagrams, scope_kind)
        log("Design document written: %s" % md_path)


if __name__ == "__main__":
    main()
