"""Clone a fleet AKS cluster's shape into a sandbox config YAML.

Reads ONE cluster from Resource Graph (fleet stays read-only) and writes a sandbox config
that mirrors its version/CNI/security/addon/pool shape, downsized for sandbox cost: pool
counts 1, autoscaler 0/1..2, Free tier, subnet IDs and authorized IP ranges stripped.
Not cloned: windowsProfile, maintenance windows, diagnostic settings, AAD admin group IDs.
"""
import sys
from pathlib import Path

from azrep import arg, fleet
from azrep.http_client import log

CLONE_PREFIX = "sbx-"
# deliberately free of sandbox/sbx/test/lab tokens so placeholders never satisfy
# the sandbox-name safety check
TODO_SUB = "TODO-fill-subscription-id"
TODO_RG = "TODO-fill-resource-group"


def fetch_cluster(session, cluster_id):
    rid = cluster_id.strip().rstrip("/")
    parts = rid.split("/")
    if len(parts) < 3 or parts[1].lower() != "subscriptions":
        sys.exit("--cluster-id must be a full ARM resource ID (/subscriptions/.../managedClusters/...)")
    kql = arg.CLUSTERS_KQL + "| where tolower(id) == '%s'" % rid.lower()
    rows = arg.query(session, kql, [parts[2]])
    if not rows:
        sys.exit("Cluster not found in Resource Graph: %s" % rid)
    return fleet._flatten_cluster(rows[0], {}, None, None)


def first_csv(value):
    return value.split(",")[0].strip() if value else ""


def clone_pool(p, args):
    if str(p.get("osType") or "Linux").lower() == "windows":
        log("WARNING: skipping Windows pool %s (windowsProfile is not cloned)." % p.get("name"))
        return None
    mode = p.get("mode") or "User"
    out = {
        "name": p.get("name"),
        "mode": mode,
        "vm_size": p.get("vmSize"),
        "os_type": p.get("osType") or "Linux",
        "os_sku": p.get("osSKU") or "Ubuntu",
        "count": int(p.get("count") or 1) if args.keep_counts else 1,
        "autoscaling": bool(p.get("enableAutoScaling")),
    }
    if p.get("maxPods"):
        out["max_pods"] = int(p["maxPods"])
    if out["autoscaling"]:
        if args.keep_counts:
            out["min_count"] = p.get("minCount")
            out["max_count"] = p.get("maxCount")
        else:
            out["min_count"] = 1 if mode.lower() == "system" else 0
            out["max_count"] = 2
    if p.get("availabilityZones"):
        out["zones"] = [str(z) for z in p["availabilityZones"]]
    if p.get("orchestratorVersion"):
        out["orchestrator_version"] = p["orchestratorVersion"]
    if str(p.get("scaleSetPriority") or "").lower() == "spot":
        out["priority"] = "Spot"
        out["eviction_policy"] = p.get("scaleSetEvictionPolicy") or "Delete"
        out["spot_max_price"] = p.get("spotMaxPrice", -1)
    taints = [t for t in (p.get("nodeTaints") or [])
              if "scalesetpriority=spot" not in str(t).lower()]
    if taints:
        out["node_taints"] = taints
    if p.get("nodeLabels"):
        out["node_labels"] = dict(p["nodeLabels"])
    if args.keep_subnets:
        if p.get("vnetSubnetID"):
            out["vnet_subnet_id"] = p["vnetSubnetID"]
        if p.get("podSubnetID"):
            out["pod_subnet_id"] = p["podSubnetID"]
    return out


def build_clone(cl, base, args):
    name = args.name or (CLONE_PREFIX + cl["cluster"])[:40]
    outbound = cl["outbound_type"]
    if outbound.lower() == "userdefinedrouting" and not args.keep_subnets:
        log("WARNING: source uses userDefinedRouting; clone uses loadBalancer "
            "(the prod route table is not available in the sandbox).")
        outbound = "loadBalancer"
    if cl["network_dataplane"].lower() == "cilium":
        log("WARNING: source uses the Cilium dataplane, which the org cannot use yet; "
            "keeping it in the clone so you can review, but consider removing network.dataplane.")

    network = {
        "plugin": cl["network_plugin"] or "azure",
        "plugin_mode": cl["network_plugin_mode"],
        "policy": cl["network_policy"],
        "dataplane": cl["network_dataplane"],
        "load_balancer_sku": cl["lb_sku"] or "standard",
        "outbound_type": outbound or "loadBalancer",
        "service_cidr": first_csv(cl["service_cidrs"]),
        "dns_service_ip": cl["dns_service_ip"],
    }
    if (cl["network_plugin"].lower() == "kubenet"
            or cl["network_plugin_mode"].lower() == "overlay"):
        network["pod_cidr"] = first_csv(cl["pod_cidrs"])
    network = {k: v for k, v in network.items() if v}

    pools = [cp for cp in (clone_pool(p, args) for p in cl["_pools_raw"]) if cp]
    if not pools:
        sys.exit("No clonable (Linux) node pools on the source cluster.")
    if not any(p["mode"].lower() == "system" for p in pools):
        sys.exit("Source cluster has no clonable System pool; cannot generate a deployable config.")

    cluster = {
        "name": name,
        "dns_prefix": name,
        "kubernetes_version": cl["kubernetes_version"],
        "sku_name": "Base",
        "sku_tier": cl["sku_tier"] if args.keep_sku_tier else "Free",
        "enable_rbac": cl["rbac_enabled"],
        "disable_local_accounts": cl["local_accounts_disabled"],
        "private_cluster": cl["private_cluster"],
        "authorized_ip_ranges": [],
        "azure_policy_addon": cl["addon_azure_policy"],
        "oidc_issuer": cl["oidc_issuer"],
        "workload_identity": cl["workload_identity"],
        "network": network,
        "node_pools": pools,
    }
    if cl["aad_managed"]:
        cluster["aad_profile"] = {"managed": True, "enableAzureRBAC": cl["azure_rbac"]}
    auto = {}
    if cl["upgrade_channel"]:
        auto["upgradeChannel"] = cl["upgrade_channel"]
    if cl["node_os_upgrade_channel"]:
        auto["nodeOSUpgradeChannel"] = cl["node_os_upgrade_channel"]
    if auto:
        cluster["auto_upgrade_profile"] = auto
    if cl["private_cluster"]:
        log("WARNING: source is a private cluster; kubectl/k8s-test against the clone "
            "needs network line of sight to the private API server.")
    if not cl["addon_azure_policy"]:
        log("WARNING: source has the Azure Policy addon disabled; k8s-test needs "
            "cluster.azure_policy_addon: true.")

    tags = dict(cl["tags"] or {})
    tags["environment"] = "sandbox"
    tags["cloned_from"] = cl["id"]

    cfg = {
        "subscription_id": base.get("subscription_id") or TODO_SUB,
        "subscription_name": base.get("subscription_name") or "sandbox",
        "environment": "sandbox",
        "resource_group": base.get("resource_group") or TODO_RG,
        "location": base.get("location") or cl["location"],
        "tags": tags,
        "safety": base.get("safety") or {"allow_non_sandbox_names": False},
        "cluster": cluster,
    }
    if base.get("policies"):
        cfg["policies"] = base["policies"]
    if base.get("k8s_tests"):
        cfg["k8s_tests"] = base["k8s_tests"]
    if base.get("cleanup"):
        cfg["cleanup"] = base["cleanup"]
    return cfg


def looks_sandbox(name, cfg):
    if (cfg.get("safety") or {}).get("allow_non_sandbox_names"):
        return True
    hay = ("%s %s" % (name, cfg.get("resource_group", ""))).lower()
    return any(token in hay for token in ("sandbox", "sbx", "test", "lab"))


HEADER = """\
# Sandbox clone of %s
# Generated by `aks_report.py sandbox clone`; review before deploying.
# Downsized for sandbox cost: node counts 1, autoscaler %s, sku %s.
# NOT cloned: windowsProfile, maintenance windows, diagnostic settings,
# AAD admin group IDs, subnet IDs%s, authorized IP ranges.
"""


def run(args):
    import yaml
    from azrep import sandbox
    from azrep.http_client import connect

    base = sandbox.load_config(args.base, validate=False) if args.base else {}
    session = connect()
    cl = fetch_cluster(session, args.cluster_id)
    cfg = build_clone(cl, base, args)
    name = cfg["cluster"]["name"]
    if not looks_sandbox(name, cfg):
        sys.exit("Clone name `%s` does not look like a sandbox name; pass --name with "
                 "sandbox/sbx/test/lab in it or use a --base config with "
                 "safety.allow_non_sandbox_names: true." % name)

    out = Path(args.out) if args.out else Path("%s.clone.yaml" % cl["cluster"])
    header = HEADER % (
        cl["id"],
        "kept" if args.keep_counts else "0/1..2",
        "kept" if args.keep_sku_tier else "Base/Free",
        " (kept)" if args.keep_subnets else "",
    )
    out.write_text(header + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
                   encoding="utf-8")
    log("Wrote %s (cluster %s, %d pool(s), k8s %s, CNI %s%s)."
        % (out, name, len(cfg["cluster"]["node_pools"]), cl["kubernetes_version"],
           cl["network_plugin"],
           "/" + cl["network_plugin_mode"] if cl["network_plugin_mode"] else ""))
    if cfg["subscription_id"] == TODO_SUB:
        log("NOTE: fill in subscription_id/resource_group (or rerun with --base sandbox.yaml).")
    print("\nNext: uv run python aks_report.py sandbox plan %s" % out)
    return str(out)
