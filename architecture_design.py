"""Architecture design snapshot from actual Azure state.

Creates a multi-tab XLSX plus, unless --no-doc is given, two companion files:
a Markdown design document with Mermaid diagrams and a .drawio diagram file
(openable in draw.io / diagrams.net) with a fleet relationship page and one
architecture page per cluster. Works with subscription Reader data from
ARM/Resource Graph only; no kubectl access is required.

Tabs: ReadMe, Summary, Clusters, NodePools, Network, Subnets, Resources,
ResourceCounts, Components, Relationships, Diagrams.

Usage:
  python architecture_design.py --cluster aks-dev-01 --all
  python architecture_design.py --subs contoso-platform --resource-group rg-apps-dev
  python architecture_design.py --subs contoso-platform --all
"""
import datetime as dt
import os
import re

import pandas as pd

from azrep import arg, drawio, excel
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


def _name_from_id(resource_id):
    return str(resource_id or "").rstrip("/").rsplit("/", 1)[-1]


def cluster_view(cluster, pools, resources, subnets):
    """Per-cluster slice shared by the Mermaid and draw.io builders: own node
    pools, resource-type counts in the cluster/node RGs, referenced subnets."""
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
    subnet_ids = {str(p.get(k) or "").lower()
                  for p in pool_rows for k in ("vnet_subnet_id", "pod_subnet_id")}
    subnet_ids.discard("")
    subnet_rows = [s for s in subnets
                   if str(s.get("subnet_id") or "").lower() in subnet_ids]
    return pool_rows, type_counts, subnet_rows


def diagram_for_cluster(cluster, pools, resources, subnets):
    pool_rows, type_counts, subnet_rows = cluster_view(cluster, pools, resources, subnets)
    subnet_labels = ["%s/%s" % (s.get("vnet"), s.get("subnet")) for s in subnet_rows]

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


def relationship_rows(clusters, pools, resources, subnets):
    """Flat edge list of every relationship the design can see: containment
    (subscription/RG/cluster), node pools, subnet usage, vnet membership,
    subnet attachments (NSG/route table/NAT) and co-located Azure resources."""
    rows = []

    def add(stype, source, relation, ttype, target, details=""):
        if source and target:
            rows.append({"source_type": stype, "source": source, "relation": relation,
                         "target_type": ttype, "target": target, "details": details})

    subnet_by_id = {str(s["subnet_id"]).lower(): s for s in subnets if s.get("subnet_id")}
    for c in clusters:
        add("subscription", c["subscription"], "contains", "resource group",
            c["resource_group"])
        add("resource group", c["resource_group"], "contains", "AKS cluster",
            c["cluster"], "Kubernetes %s; tier %s; %s" % (
                c["kubernetes_version"], c["sku_tier"], c["location"]))
        add("AKS cluster", c["cluster"], "manages", "resource group",
            c["node_resource_group"], "node resource group")
        add("AKS cluster", c["cluster"], "exposes", "API server",
            c["public_fqdn"] or c["private_fqdn"] or "(no fqdn)",
            "private=%s; authorized ranges=%s" % (
                _friendly_bool(c["private_cluster"]), c["authorized_ip_ranges"]))
    for p in pools:
        add("AKS cluster", p["cluster"], "runs", "node pool",
            "%s/%s" % (p["cluster"], p["pool"]),
            "%s %s; %s nodes; priority=%s" % (p["mode"], p["vm_size"], p["count"],
                                              p["priority"]))
        for key, relation in (("vnet_subnet_id", "nodes in"), ("pod_subnet_id", "pods in")):
            sid = str(p.get(key) or "").lower()
            if not sid:
                continue
            s = subnet_by_id.get(sid)
            label = ("%s/%s" % (s["vnet"], s["subnet"])) if s else _name_from_id(sid)
            add("node pool", "%s/%s" % (p["cluster"], p["pool"]), relation,
                "subnet", label, s.get("prefixes", "") if s else "")
    for s in subnets:
        label = "%s/%s" % (s["vnet"], s["subnet"])
        add("subnet", label, "part of", "virtual network", s["vnet"], s["prefixes"])
        for key, relation, ttype in (("nsg_id", "filtered by", "network security group"),
                                     ("route_table_id", "routes via", "route table"),
                                     ("nat_gateway_id", "egress via", "NAT gateway")):
            if s.get(key):
                add("subnet", label, relation, ttype, _name_from_id(s[key]))
    for c in clusters:
        _, type_counts, _ = cluster_view(c, [], resources, subnets)
        for typ, count in sorted(type_counts.items()):
            if typ == "microsoft.containerservice/managedclusters":
                continue
            add("AKS cluster", c["cluster"], "uses", _resource_class(typ), typ,
                "count=%d (in cluster/node RG)" % count)
    return rows


def relationship_diagram(clusters, pools, subnets):
    """Fleet-wide Mermaid relationship chart: subscriptions, clusters, subnets,
    vnets and subnet attachments in one graph."""
    lines = ["graph LR"]
    by_sub = {}
    for c in clusters:
        by_sub.setdefault(c["subscription"], []).append(c)
    for i, (sub, cls) in enumerate(sorted(by_sub.items())):
        lines.append('  subgraph sub%d["Subscription: %s"]' % (i, sub))
        for c in cls:
            lines.append('    cl_%s["AKS: %s\\nK8s %s | %s pools | %s nodes"]' % (
                _safe_id(c["cluster"]), c["cluster"], c["kubernetes_version"],
                c["node_pools"], c["total_nodes"]))
        lines.append("  end")

    subnet_by_id = {str(s["subnet_id"]).lower(): s for s in subnets if s.get("subnet_id")}
    seen_edges, seen_nodes = set(), set()

    def subnet_node(s):
        nid = "sn_%s" % _safe_id("%s_%s" % (s["vnet"], s["subnet"]))
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            lines.append('  %s["Subnet: %s\\n%s"]' % (nid, s["subnet"], s["prefixes"]))
            vid = "vn_%s" % _safe_id(s["vnet"])
            if vid not in seen_nodes:
                seen_nodes.add(vid)
                lines.append('  %s["VNet: %s"]' % (vid, s["vnet"]))
            lines.append("  %s --> %s" % (nid, vid))
            for key, label in (("nsg_id", "NSG"), ("route_table_id", "Route table"),
                               ("nat_gateway_id", "NAT gateway")):
                if s.get(key):
                    aid = "at_%s" % _safe_id(_name_from_id(s[key]))
                    if aid not in seen_nodes:
                        seen_nodes.add(aid)
                        lines.append('  %s["%s: %s"]' % (aid, label, _name_from_id(s[key])))
                    lines.append("  %s -.-> %s" % (nid, aid))
        return nid

    for p in pools:
        for key, label in (("vnet_subnet_id", "nodes"), ("pod_subnet_id", "pods")):
            sid = str(p.get(key) or "").lower()
            s = subnet_by_id.get(sid)
            if not s:
                continue
            edge = (p["cluster"], sid, label)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            lines.append("  cl_%s -- %s --> %s" % (
                _safe_id(p["cluster"]), label, subnet_node(s)))
    return "\n".join(lines)


def _drawio_cluster_page(cluster, pools, resources, subnets):
    pool_rows, type_counts, subnet_rows = cluster_view(cluster, pools, resources, subnets)
    page = drawio.Page(cluster["cluster"])

    node_w, node_h, gap, per_row = 240, 70, 20, 2
    inner_w = per_row * node_w + (per_row + 1) * gap

    def grid(items, start_y):
        """Yields (item, x, y) positions in a 2-column grid."""
        for i, item in enumerate(items):
            col, row = i % per_row, i // per_row
            yield item, gap + col * (node_w + gap), start_y + row * (node_h + gap)

    pool_rows_n = (len(pool_rows) + per_row - 1) // per_row
    counted = [(t, c) for t, c in sorted(type_counts.items())
               if t != "microsoft.containerservice/managedclusters"]
    res_rows_n = (len(counted) + per_row - 1) // per_row
    nrg_h = 40 + max(pool_rows_n + res_rows_n, 1) * (node_h + gap) + gap
    rg_h = 40 + node_h + 2 * gap
    sub_h = 40 + rg_h + gap + nrg_h + gap

    sub = page.container("Subscription: %s" % cluster["subscription"],
                         40, 40, inner_w + 40, sub_h, drawio.SUBSCRIPTION)
    rg = page.container("Resource group: %s" % cluster["resource_group"],
                        20, 40, inner_w, rg_h, drawio.RESOURCE_GROUP, parent=sub)
    aks = page.node("AKS: %s\nKubernetes %s | Tier %s" % (
        cluster["cluster"], cluster["kubernetes_version"], cluster["sku_tier"]),
        gap, 40, node_w, node_h, drawio.CLUSTER, parent=rg)
    api = page.node("API server\nprivate=%s | authorized ranges=%s" % (
        _friendly_bool(cluster["private_cluster"]), cluster["authorized_ip_ranges"]),
        2 * gap + node_w, 40, node_w, node_h, drawio.API_SERVER, parent=rg)
    nrg = page.container("Node resource group: %s" % (cluster["node_resource_group"] or "(unknown)"),
                         20, 40 + rg_h + gap, inner_w, nrg_h, drawio.RESOURCE_GROUP,
                         parent=sub)
    page.edge(aks, api)
    page.edge(aks, nrg, "manages")

    pool_ids = {}
    for p, x, y in grid(pool_rows, 40):
        pid = page.node("Pool: %s\n%s %s | %s nodes\npriority=%s" % (
            p["pool"], p["mode"], p["vm_size"], p["count"], p["priority"]),
            x, y, node_w, node_h, drawio.POOL, parent=nrg)
        pool_ids[p["pool"]] = pid
    res_y = 40 + pool_rows_n * (node_h + gap)
    for (typ, count), x, y in grid(counted, res_y):
        page.node("%s\ncount=%d" % (_resource_class(typ), count),
                  x, y, node_w, node_h, drawio.RESOURCE, parent=nrg)

    net_x = 40 + inner_w + 40 + 80
    subnet_ids = {}
    for i, s in enumerate(subnet_rows):
        sid = page.node("Subnet: %s/%s\n%s" % (s["vnet"], s["subnet"], s["prefixes"]),
                        net_x, 40 + i * (node_h + gap), node_w, node_h, drawio.SUBNET)
        subnet_ids[str(s["subnet_id"]).lower()] = sid
        for key, label in (("nsg_id", "NSG"), ("route_table_id", "Route table"),
                           ("nat_gateway_id", "NAT gateway")):
            if s.get(key):
                att = page.node("%s: %s" % (label, _name_from_id(s[key])),
                                net_x + node_w + 60, 40 + i * (node_h + gap),
                                node_w, node_h - 20, drawio.NET_ATTACH)
                page.edge(sid, att, style=drawio.EDGE_DASHED)
    for p in pool_rows:
        for key, label in (("vnet_subnet_id", "nodes"), ("pod_subnet_id", "pods")):
            target = subnet_ids.get(str(p.get(key) or "").lower())
            if target and p["pool"] in pool_ids:
                page.edge(pool_ids[p["pool"]], target, label)
    return page


def _drawio_overview_page(clusters, pools, subnets):
    page = drawio.Page("Fleet relationships")
    node_w, node_h, gap = 260, 70, 20

    by_sub = {}
    for c in clusters:
        by_sub.setdefault(c["subscription"], []).append(c)
    cluster_ids = {}
    y = 40
    for sub, cls in sorted(by_sub.items()):
        sub_h = 40 + len(cls) * (node_h + gap) + gap
        box = page.container("Subscription: %s" % sub, 40, y, node_w + 2 * gap,
                             sub_h, drawio.SUBSCRIPTION)
        for i, c in enumerate(cls):
            cluster_ids[c["cluster"]] = page.node(
                "AKS: %s\nK8s %s | %s pools | %s nodes" % (
                    c["cluster"], c["kubernetes_version"], c["node_pools"],
                    c["total_nodes"]),
                gap, 40 + i * (node_h + gap), node_w, node_h, drawio.CLUSTER,
                parent=box)
        y += sub_h + gap

    sn_x = 40 + node_w + 2 * gap + 140
    vn_x = sn_x + node_w + 120
    at_x = vn_x + node_w + 120
    subnet_ids, vnet_ids, attach_ids = {}, {}, {}
    for i, s in enumerate(subnets):
        sid = page.node("Subnet: %s/%s\n%s" % (s["vnet"], s["subnet"], s["prefixes"]),
                        sn_x, 40 + i * (node_h + gap), node_w, node_h, drawio.SUBNET)
        subnet_ids[str(s["subnet_id"]).lower()] = sid
        if s["vnet"] not in vnet_ids:
            vnet_ids[s["vnet"]] = page.node(
                "VNet: %s" % s["vnet"], vn_x, 40 + len(vnet_ids) * (node_h + gap),
                node_w, node_h, drawio.VNET)
        page.edge(sid, vnet_ids[s["vnet"]])
        for key, label in (("nsg_id", "NSG"), ("route_table_id", "Route table"),
                           ("nat_gateway_id", "NAT gateway")):
            if s.get(key):
                name = "%s: %s" % (label, _name_from_id(s[key]))
                if name not in attach_ids:
                    attach_ids[name] = page.node(
                        name, at_x, 40 + len(attach_ids) * (node_h + gap),
                        node_w, node_h - 20, drawio.NET_ATTACH)
                page.edge(sid, attach_ids[name], style=drawio.EDGE_DASHED)

    seen = set()
    for p in pools:
        for key, label in (("vnet_subnet_id", "nodes"), ("pod_subnet_id", "pods")):
            target = subnet_ids.get(str(p.get(key) or "").lower())
            source = cluster_ids.get(p["cluster"])
            if not target or not source or (source, target, label) in seen:
                continue
            seen.add((source, target, label))
            page.edge(source, target, label)
    return page


def build_drawio_pages(clusters, pools, resources, subnets):
    pages = [_drawio_overview_page(clusters, pools, subnets)]
    pages.extend(_drawio_cluster_page(c, pools, resources, subnets) for c in clusters)
    return pages


def write_design_doc(path, clusters, resources, components, diagrams, scope_label,
                     rel_diagram=""):
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
        if rel_diagram:
            f.write("## Relationship overview\n\n")
            f.write("Subscriptions, clusters, subnets, virtual networks and subnet ")
            f.write("attachments (NSG, route table, NAT gateway) in one chart. ")
            f.write("The same topology is in the companion .drawio file for editing ")
            f.write("in draw.io / diagrams.net.\n\n")
            f.write("```mermaid\n%s\n```\n\n" % rel_diagram)
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
                   help="write only the XLSX workbook; skip the Markdown design "
                        "document and the .drawio diagram file")
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
    relationships = relationship_rows(clusters, pools, resources, subnets)

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
    reldf = pd.DataFrame(relationships, columns=[
        "source_type", "source", "relation", "target_type", "target", "details"])
    ddf = pd.DataFrame(diagrams, columns=["cluster", "diagram"])

    summary = pd.DataFrame([
        ("Design scope", scope_kind),
        ("Subscriptions", len(sel)),
        ("Clusters in scope", len(clusters)),
        ("Node pools in scope", len(pools)),
        ("Resource groups queried", len(resource_groups) if resource_groups else "all in subscription scope"),
        ("Azure resources in design scope", len(resources)),
        ("Referenced subnets", len(subnets)),
        ("Relationships mapped", len(relationships)),
        ("Markdown + draw.io companions", "disabled" if args.no_doc else "enabled"),
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
        "Companion files: a Markdown document with Mermaid diagrams (per-cluster",
        "views plus a fleet relationship chart) and a .drawio file with the same",
        "topology, editable in draw.io / diagrams.net.",
    ])
    excel.add_table(wb, "Summary", summary, section="summary")
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
    excel.add_table(wb, "Relationships", reldf, max_width=70)
    excel.add_table(wb, "Diagrams", ddf, max_width=120, section="reference")

    xlsx_path = excel.save(wb, out_path(args, "aks_design", env_filter))
    log("Report written: %s" % xlsx_path)
    if not args.no_doc:
        rel_diagram = relationship_diagram(clusters, pools, subnets)
        md_path = os.path.splitext(xlsx_path)[0] + ".md"
        write_design_doc(md_path, clusters, resources, comps, diagrams, scope_kind,
                         rel_diagram)
        log("Design document written: %s" % md_path)
        drawio_path = os.path.splitext(xlsx_path)[0] + ".drawio"
        drawio.save(build_drawio_pages(clusters, pools, resources, subnets), drawio_path)
        log("draw.io diagram written: %s (open in draw.io / diagrams.net)" % drawio_path)


if __name__ == "__main__":
    main()
