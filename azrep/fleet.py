"""Fetch all AKS clusters across the selected subscriptions via Resource Graph
and flatten them into cluster-level and node-pool-level records."""
from . import arg
from .http_client import log
from .subs import (cluster_filter_label, cluster_in_scope, env_match,
                   prompt_cluster_filter, resolve_env_detail)


def _addon(addons, *names):
    """addonProfiles keys are case-insensitive and vary; returns True/False."""
    low = {str(k).lower(): v for k, v in (addons or {}).items()}
    for n in names:
        p = low.get(n.lower())
        if isinstance(p, dict):
            return bool(p.get("enabled"))
    return False


def _join(v):
    if isinstance(v, (list, tuple, set)):
        return ", ".join(str(x) for x in v if x is not None)
    if isinstance(v, dict):
        return ", ".join("%s=%s" % (k, v[k]) for k in sorted(v))
    return "" if v is None else str(v)


def _ap(auto, *names):
    for name in names:
        if name in auto:
            return auto.get(name)
    return ""


def _flatten_cluster(c, rg_tags, sub_name, env_keys):
    net = c.get("networkProfile") or {}
    api = c.get("apiServerAccessProfile") or {}
    aad = c.get("aadProfile") or {}
    sec = c.get("securityProfile") or {}
    addons = c.get("addonProfiles") or {}
    auto = c.get("autoUpgradeProfile") or {}
    autoscaler = c.get("autoScalerProfile") or {}
    pools = c.get("agentPoolProfiles") or []
    tags = c.get("tags") or {}
    rgt = rg_tags.get((c["subscriptionId"], str(c["resourceGroup"]).lower()), {})

    env, env_source = resolve_env_detail(
        tags, rgt, env_keys,
        names=(c.get("name"), c.get("resourceGroup"), c.get("nodeResourceGroup")),
    )
    spot_pools = [p for p in pools if str(p.get("scaleSetPriority", "")).lower() == "spot"]
    user_pools = [p for p in pools if str(p.get("mode", "User")).lower() == "user"]
    auth_ranges = api.get("authorizedIPRanges") or []
    private = bool(api.get("enablePrivateCluster"))

    return {
        "cluster": c["name"],
        "id": c["id"],
        "subscription_id": c["subscriptionId"],
        "subscription": sub_name or c["subscriptionId"],
        "resource_group": c["resourceGroup"],
        "node_resource_group": c.get("nodeResourceGroup") or "",
        "location": c.get("location"),
        "environment": env or "(unknown)",
        "environment_source": env_source or "",
        "power_state": c.get("powerState") or "",
        "provisioning_state": c.get("provisioningState") or "",
        "kubernetes_version": c.get("kubernetesVersion") or "",
        "current_kubernetes_version": c.get("currentKubernetesVersion") or "",
        "sku_tier": c.get("skuTier") or "Free",
        "support_plan": c.get("supportPlan") or "",
        "node_pools": len(pools),
        "user_pools": len(user_pools),
        "spot_pools": len(spot_pools),
        "total_nodes": sum(int(p.get("count") or 0) for p in pools),
        "spot_nodes": sum(int(p.get("count") or 0) for p in spot_pools),
        "vm_sizes": ", ".join(sorted({str(p.get("vmSize")) for p in pools if p.get("vmSize")})),
        "autoscaling_pools": sum(1 for p in pools if p.get("enableAutoScaling")),
        "network_plugin": net.get("networkPlugin") or "",
        "network_plugin_mode": net.get("networkPluginMode") or "",
        "network_policy": net.get("networkPolicy") or "",
        "network_dataplane": net.get("networkDataplane") or "",
        "outbound_type": net.get("outboundType") or "",
        "lb_sku": net.get("loadBalancerSku") or "",
        "service_cidrs": _join(net.get("serviceCidrs") or net.get("serviceCidr")),
        "pod_cidrs": _join(net.get("podCidrs") or net.get("podCidr")),
        "dns_service_ip": net.get("dnsServiceIP") or "",
        "ip_families": _join(net.get("ipFamilies")),
        "private_cluster": private,
        "authorized_ip_ranges": len(auth_ranges),
        "public_fqdn": c.get("fqdn") or "",
        "private_fqdn": c.get("privateFQDN") or "",
        "rbac_enabled": bool(c.get("enableRBAC")),
        "aad_managed": bool(aad.get("managed")),
        "azure_rbac": bool(aad.get("enableAzureRBAC")),
        "local_accounts_disabled": bool(c.get("disableLocalAccounts")),
        "identity_type": c.get("identityType") or ("ServicePrincipal" if c.get("servicePrincipalClientId") else ""),
        "oidc_issuer": bool((c.get("oidcIssuerProfile") or {}).get("enabled")),
        "workload_identity": bool((sec.get("workloadIdentity") or {}).get("enabled")),
        "defender": bool(((sec.get("defender") or {}).get("securityMonitoring") or {}).get("enabled")),
        "image_cleaner": bool((sec.get("imageCleaner") or {}).get("enabled")),
        "addon_monitoring": _addon(addons, "omsagent", "omsAgent"),
        "addon_azure_policy": _addon(addons, "azurepolicy", "azurePolicy"),
        "addon_keyvault_csi": _addon(addons, "azureKeyvaultSecretsProvider"),
        "addon_appgw_ingress": _addon(addons, "ingressApplicationGateway"),
        "addon_virtual_node": _addon(addons, "aciConnectorLinux"),
        "upgrade_channel": auto.get("upgradeChannel") or "",
        "node_os_upgrade_channel": auto.get("nodeOSUpgradeChannel") or "",
        "autoscaler_profile": autoscaler,
        "autoscaler_expander": _ap(autoscaler, "expander"),
        "autoscaler_balance_similar_node_groups": _ap(
            autoscaler, "balanceSimilarNodeGroups", "balance-similar-node-groups"),
        "autoscaler_scan_interval": _ap(autoscaler, "scanInterval", "scan-interval"),
        "autoscaler_scale_down_delay_after_add": _ap(
            autoscaler, "scaleDownDelayAfterAdd", "scale-down-delay-after-add"),
        "autoscaler_scale_down_unneeded_time": _ap(
            autoscaler, "scaleDownUnneededTime", "scale-down-unneeded-time"),
        "autoscaler_scale_down_utilization_threshold": _ap(
            autoscaler, "scaleDownUtilizationThreshold", "scale-down-utilization-threshold"),
        "autoscaler_skip_nodes_with_local_storage": _ap(
            autoscaler, "skipNodesWithLocalStorage", "skip-nodes-with-local-storage"),
        "autoscaler_skip_nodes_with_system_pods": _ap(
            autoscaler, "skipNodesWithSystemPods", "skip-nodes-with-system-pods"),
        "tags": tags,
        "resource_group_tags": rgt,
        "_pools_raw": pools,
    }


def _flatten_pool(cl, p):
    zones = p.get("availabilityZones") or []
    taints = p.get("nodeTaints") or []
    labels = p.get("nodeLabels") or {}
    return {
        "cluster": cl["cluster"],
        "cluster_id": cl["id"],
        "subscription_id": cl["subscription_id"],
        "subscription": cl["subscription"],
        "environment": cl["environment"],
        "environment_source": cl.get("environment_source", ""),
        "location": cl["location"],
        "resource_group": cl["resource_group"],
        "node_resource_group": cl["node_resource_group"],
        "pool": p.get("name"),
        "mode": p.get("mode") or "User",
        "vm_size": p.get("vmSize") or "",
        "priority": p.get("scaleSetPriority") or "Regular",
        "spot_max_price": p.get("spotMaxPrice"),
        "eviction_policy": p.get("scaleSetEvictionPolicy") or "",
        "count": int(p.get("count") or 0),
        "autoscaling": bool(p.get("enableAutoScaling")),
        "min_count": p.get("minCount"),
        "max_count": p.get("maxCount"),
        "os_type": p.get("osType") or "",
        "os_sku": p.get("osSKU") or "",
        "os_disk_type": p.get("osDiskType") or "",
        "os_disk_gb": p.get("osDiskSizeGB"),
        "max_pods": p.get("maxPods"),
        "zones": ",".join(str(z) for z in zones),
        "orchestrator_version": p.get("orchestratorVersion") or "",
        "current_orchestrator_version": p.get("currentOrchestratorVersion") or "",
        "node_image_version": p.get("nodeImageVersion") or "",
        "power_state": ((p.get("powerState") or {}).get("code")
                        if isinstance(p.get("powerState"), dict) else p.get("powerState")) or "",
        "taints": "; ".join(taints),
        "node_labels": _join(labels),
        "spot_taint_present": any("scalesetpriority=spot" in str(t).lower()
                                  for t in taints),
        "pool_type": p.get("type") or "",
        "encryption_at_host": bool(p.get("enableEncryptionAtHost")),
        "custom_subnet": bool(p.get("vnetSubnetID")),
        "vnet_subnet_id": p.get("vnetSubnetID") or "",
        "pod_subnet_id": p.get("podSubnetID") or "",
        "node_public_ip_enabled": bool(p.get("enableNodePublicIP")),
        "kubelet_disk_type": p.get("kubeletDiskType") or "",
        "ultra_ssd_enabled": bool(p.get("enableUltraSSD")),
        "fips_enabled": bool(p.get("enableFIPS")),
    }


def load_fleet(session, sel_subs, env_filter=None, include_unknown=False, env_keys=None):
    """Returns (clusters, pools) as lists of dicts, already filtered by environment."""
    sub_ids = [s["subscription_id"] for s in sel_subs]
    sub_name = {s["subscription_id"]: s["subscription_name"] for s in sel_subs}

    log("Resource Graph: fetching clusters, resource groups and subscription names...")
    raw = arg.query(session, arg.CLUSTERS_KQL, sub_ids)
    rg_rows = arg.query(session, arg.RG_TAGS_KQL, sub_ids)
    for s in arg.query(session, arg.SUB_NAMES_KQL, sub_ids):
        sub_name.setdefault(s["subscriptionId"], s.get("name"))
        if not sub_name.get(s["subscriptionId"]):
            sub_name[s["subscriptionId"]] = s.get("name")
    rg_tags = {(r["subscriptionId"], r["name"]): (r.get("tags") or {}) for r in rg_rows}

    clusters = [_flatten_cluster(c, rg_tags, sub_name.get(c["subscriptionId"]), env_keys)
                for c in raw]
    total = len(clusters)
    clusters = [c for c in clusters
                if env_match(c["environment"], env_filter, include_unknown)]
    env_total = len(clusters)
    prompt_cluster_filter(clusters)
    clusters = [c for c in clusters if cluster_in_scope(c)]
    unknown = sum(1 for c in clusters if c["environment"] == "(unknown)")
    log("Clusters: %d total, %d after env filter (%s), %d after cluster filter (%s), "
        "%d with unknown env"
        % (total, env_total, env_filter or "none", len(clusters),
           cluster_filter_label(), unknown))

    pools = [_flatten_pool(cl, p) for cl in clusters for p in cl["_pools_raw"]]
    return clusters, pools
