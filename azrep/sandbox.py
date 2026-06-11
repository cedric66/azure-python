"""Sandbox AKS deployment and Azure Policy test workflow.

This module is intentionally behind the `aks_report.py sandbox ...` command.
Normal report commands stay read-only; every write/delete sandbox operation
requires `--yes`.
"""
import argparse
import csv
import json
import os
import sys
import tempfile
import time
from pathlib import Path

RG_API = "2021-04-01"
DEPLOY_API = "2022-09-01"
AKS_API = "2024-05-01"
DEF_API = "2023-04-01"
ASSIGN_API = "2022-06-01"
POLICY_STATES_API = "2019-10-01"


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def load_config(path):
    p = Path(path)
    if not p.exists():
        sys.exit("Sandbox config not found: %s" % path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        try:
            import yaml
        except ModuleNotFoundError:
            sys.exit("PyYAML is required for YAML sandbox configs. Run `pip install -r requirements.txt`.")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        sys.exit("Sandbox config must be a mapping/object: %s" % path)
    data["_config_path"] = str(p.resolve())
    data["_config_dir"] = str(p.resolve().parent)
    validate_config(data)
    return data


def validate_config(cfg):
    required = ("subscription_id", "resource_group", "location")
    for key in required:
        if not cfg.get(key):
            sys.exit("Sandbox config missing required key: %s" % key)
    cluster = cfg.get("cluster") or {}
    if not cluster.get("name"):
        sys.exit("Sandbox config missing required key: cluster.name")
    pools = cluster.get("node_pools") or []
    if not pools:
        sys.exit("Sandbox config must define at least one cluster.node_pools entry")
    if not any((p.get("mode") or "User").lower() == "system" for p in pools):
        sys.exit("Sandbox config needs one node pool with mode: System")


def require_yes(args, action):
    if not getattr(args, "yes", False):
        sys.exit("Refusing to %s without --yes. Run `sandbox plan` first." % action)


def ensure_sandbox_safe(cfg, action):
    safety = cfg.get("safety") or {}
    if safety.get("allow_non_sandbox_names"):
        return
    names = [cfg.get("resource_group", ""), (cfg.get("cluster") or {}).get("name", "")]
    haystack = " ".join(names).lower()
    if not any(token in haystack for token in ("sandbox", "sbx", "test", "lab")):
        sys.exit(
            "Refusing to %s because resource_group/cluster name does not look like a sandbox. "
            "Use names containing sandbox/sbx/test/lab or set safety.allow_non_sandbox_names: true."
            % action
        )


def sub_id(cfg):
    return cfg["subscription_id"]


def rg_scope(cfg):
    return "/subscriptions/%s/resourceGroups/%s" % (sub_id(cfg), cfg["resource_group"])


def cluster_id(cfg):
    return "%s/providers/Microsoft.ContainerService/managedClusters/%s" % (
        rg_scope(cfg), cfg["cluster"]["name"]
    )


def resolve_scope(cfg, scope_name):
    scope = (scope_name or (cfg.get("policies") or {}).get("assignment_scope") or "resource_group").lower()
    if scope in ("subscription", "sub"):
        return "/subscriptions/%s" % sub_id(cfg)
    if scope in ("resource_group", "resourcegroup", "rg"):
        return rg_scope(cfg)
    if scope in ("cluster", "aks"):
        return cluster_id(cfg)
    if scope.startswith("/"):
        return scope
    sys.exit("Unknown policy scope `%s`; use subscription, resource_group, cluster, or a full Azure resource ID." % scope)


def resolve_path(cfg, maybe_path):
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return Path(cfg["_config_dir"]) / p


def clean_dict(obj):
    if isinstance(obj, dict):
        return {k: clean_dict(v) for k, v in obj.items()
                if v is not None and v != "" and clean_dict(v) != {}}
    if isinstance(obj, list):
        return [clean_dict(v) for v in obj if v is not None and clean_dict(v) != {}]
    return obj


def plan_summary(cfg):
    policies = cfg.get("policies") or {}
    definitions = policies.get("definitions") or []
    assignments = policies.get("assignments") or []
    pools = (cfg.get("cluster") or {}).get("node_pools") or []
    return [
        ("subscription", cfg.get("subscription_name") or sub_id(cfg)),
        ("subscription_id", sub_id(cfg)),
        ("resource_group", cfg.get("resource_group")),
        ("location", cfg.get("location")),
        ("cluster", cfg["cluster"]["name"]),
        ("node_pools", ", ".join("%s:%s:%s" % (
            p.get("name"), p.get("mode", "User"), p.get("vm_size")) for p in pools)),
        ("policy_definitions", len(definitions)),
        ("policy_assignments", len(assignments)),
        ("default_policy_scope", policies.get("assignment_scope") or "resource_group"),
        ("policy_report_scope", cfg.get("environment") or "sandbox"),
    ]


def print_plan(cfg):
    print("\nSandbox plan\n")
    for key, value in plan_summary(cfg):
        print("  %-22s %s" % (key + ":", value))
    print("\nCommands:")
    print("  python aks_report.py sandbox deploy %s --yes --wait" % cfg["_config_path"])
    print("  python aks_report.py sandbox policy-apply %s --yes" % cfg["_config_path"])
    print("  python aks_report.py sandbox scan %s --yes" % cfg["_config_path"])
    print("  python aks_report.py sandbox report %s" % cfg["_config_path"])
    print("  python aks_report.py sandbox cleanup %s --yes" % cfg["_config_path"])


def build_node_pool(pool):
    out = {
        "name": pool["name"],
        "count": int(pool.get("count", 1)),
        "vmSize": pool["vm_size"],
        "mode": pool.get("mode", "User"),
        "osType": pool.get("os_type", "Linux"),
        "osSKU": pool.get("os_sku", "Ubuntu"),
        "type": pool.get("type", "VirtualMachineScaleSets"),
        "maxPods": int(pool.get("max_pods", 30)),
        "enableAutoScaling": bool(pool.get("autoscaling", False)),
    }
    if pool.get("orchestrator_version"):
        out["orchestratorVersion"] = pool["orchestrator_version"]
    if pool.get("min_count") is not None:
        out["minCount"] = int(pool["min_count"])
    if pool.get("max_count") is not None:
        out["maxCount"] = int(pool["max_count"])
    if pool.get("zones"):
        out["availabilityZones"] = [str(z) for z in pool["zones"]]
    if pool.get("vnet_subnet_id"):
        out["vnetSubnetID"] = pool["vnet_subnet_id"]
    if pool.get("pod_subnet_id"):
        out["podSubnetID"] = pool["pod_subnet_id"]
    if (pool.get("priority") or "").lower() == "spot":
        out["scaleSetPriority"] = "Spot"
        out["scaleSetEvictionPolicy"] = pool.get("eviction_policy", "Delete")
        out["spotMaxPrice"] = float(pool.get("spot_max_price", -1))
    return clean_dict(out)


def build_aks_template(cfg):
    cluster = cfg["cluster"]
    network = cluster.get("network") or {}
    properties = {
        "dnsPrefix": cluster.get("dns_prefix") or cluster["name"],
        "kubernetesVersion": cluster.get("kubernetes_version"),
        "enableRBAC": bool(cluster.get("enable_rbac", True)),
        "disableLocalAccounts": bool(cluster.get("disable_local_accounts", True)),
        "agentPoolProfiles": [build_node_pool(p) for p in cluster["node_pools"]],
        "networkProfile": {
            "networkPlugin": network.get("plugin", "azure"),
            "networkPluginMode": network.get("plugin_mode"),
            "networkPolicy": network.get("policy", "azure"),
            "networkDataplane": network.get("dataplane"),
            "loadBalancerSku": network.get("load_balancer_sku", "standard"),
            "outboundType": network.get("outbound_type", "loadBalancer"),
            "serviceCidr": network.get("service_cidr"),
            "dnsServiceIP": network.get("dns_service_ip"),
            "podCidr": network.get("pod_cidr"),
        },
        "addonProfiles": {
            "azurepolicy": {"enabled": bool(cluster.get("azure_policy_addon", True))}
        },
        "oidcIssuerProfile": {"enabled": bool(cluster.get("oidc_issuer", True))},
        "securityProfile": {
            "workloadIdentity": {"enabled": bool(cluster.get("workload_identity", True))}
        },
        "apiServerAccessProfile": {
            "enablePrivateCluster": bool(cluster.get("private_cluster", False)),
            "authorizedIPRanges": cluster.get("authorized_ip_ranges") or None,
        },
    }
    aad = cluster.get("aad_profile")
    if aad:
        properties["aadProfile"] = aad
    auto_upgrade = cluster.get("auto_upgrade_profile")
    if auto_upgrade:
        properties["autoUpgradeProfile"] = auto_upgrade
    identity = cluster.get("identity") or {"type": "SystemAssigned"}
    sku = {
        "name": cluster.get("sku_name", "Base"),
        "tier": cluster.get("sku_tier", "Free"),
    }
    resource = {
        "type": "Microsoft.ContainerService/managedClusters",
        "apiVersion": AKS_API,
        "name": cluster["name"],
        "location": cfg["location"],
        "tags": cfg.get("tags") or {},
        "identity": identity,
        "sku": sku,
        "properties": clean_dict(properties),
    }
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "resources": [clean_dict(resource)],
    }


def create_or_update_resource_group(session, cfg):
    log("Creating/updating resource group %s..." % cfg["resource_group"])
    return session.put(
        "/subscriptions/%s/resourcegroups/%s" % (sub_id(cfg), cfg["resource_group"]),
        params={"api-version": RG_API},
        payload={"location": cfg["location"], "tags": cfg.get("tags") or {}},
    )


def deploy_cluster(session, cfg, wait=False):
    create_or_update_resource_group(session, cfg)
    deployment = cfg.get("deployment_name") or ("aks-sandbox-%s" % cfg["cluster"]["name"])
    payload = {
        "properties": {
            "mode": "Incremental",
            "template": build_aks_template(cfg),
            "parameters": {},
        }
    }
    url = "%s/providers/Microsoft.Resources/deployments/%s" % (rg_scope(cfg), deployment)
    log("Starting ARM deployment %s..." % deployment)
    result = session.put(url, params={"api-version": DEPLOY_API}, payload=payload)
    if wait:
        wait_for_deployment(session, cfg, deployment)
    return result


def wait_for_deployment(session, cfg, deployment):
    url = "%s/providers/Microsoft.Resources/deployments/%s" % (rg_scope(cfg), deployment)
    terminal = {"succeeded", "failed", "canceled"}
    while True:
        data = session.get(url, params={"api-version": DEPLOY_API})
        state = (((data.get("properties") or {}).get("provisioningState")) or "").lower()
        log("Deployment state: %s" % (state or "unknown"))
        if state in terminal:
            if state != "succeeded":
                sys.exit("Deployment did not succeed: %s" % state)
            return data
        time.sleep(30)


def load_policy_definition(cfg, item):
    if item.get("file"):
        path = resolve_path(cfg, item["file"])
        data = json.loads(path.read_text(encoding="utf-8"))
    elif item.get("definition"):
        data = item["definition"]
    else:
        sys.exit("Policy definition %s needs `file` or `definition`." % item.get("name", ""))
    if "properties" not in data:
        data = {"properties": data}
    props = data.setdefault("properties", {})
    props.setdefault("policyType", "Custom")
    return clean_dict(data)


def custom_definition_id(cfg, name):
    return "/subscriptions/%s/providers/Microsoft.Authorization/policyDefinitions/%s" % (sub_id(cfg), name)


def resolve_definition_id(cfg, raw):
    if not raw:
        sys.exit("Policy assignment missing definition_id")
    if raw.startswith("custom:"):
        return custom_definition_id(cfg, raw.split(":", 1)[1])
    return raw


def normalize_parameters(params):
    out = {}
    for key, value in (params or {}).items():
        if isinstance(value, dict) and "value" in value:
            out[key] = value
        else:
            out[key] = {"value": value}
    return out


def apply_policy_definitions(session, cfg):
    definitions = (cfg.get("policies") or {}).get("definitions") or []
    for item in definitions:
        name = item.get("name")
        if not name:
            sys.exit("Every custom policy definition needs a name")
        payload = load_policy_definition(cfg, item)
        log("Creating/updating policy definition %s..." % name)
        session.put(
            "/subscriptions/%s/providers/Microsoft.Authorization/policyDefinitions/%s" % (sub_id(cfg), name),
            params={"api-version": DEF_API},
            payload=payload,
        )


def apply_policy_assignments(session, cfg):
    assignments = (cfg.get("policies") or {}).get("assignments") or []
    for item in assignments:
        name = item.get("name")
        if not name:
            sys.exit("Every policy assignment needs a name")
        scope = resolve_scope(cfg, item.get("scope"))
        payload = {
            "identity": item.get("identity"),
            "location": item.get("location") or cfg.get("location"),
            "properties": {
                "displayName": item.get("display_name") or name,
                "description": item.get("description"),
                "policyDefinitionId": resolve_definition_id(cfg, item.get("definition_id")),
                "parameters": normalize_parameters(item.get("parameters")),
                "enforcementMode": item.get("enforcement_mode", "DoNotEnforce"),
                "nonComplianceMessages": item.get("non_compliance_messages"),
                "metadata": item.get("metadata") or {"source": "aks-reporting-sandbox"},
            },
        }
        log("Creating/updating policy assignment %s at %s..." % (name, scope))
        session.put(
            "%s/providers/Microsoft.Authorization/policyAssignments/%s" % (scope, name),
            params={"api-version": ASSIGN_API},
            payload=clean_dict(payload),
        )


def apply_policies(session, cfg):
    apply_policy_definitions(session, cfg)
    apply_policy_assignments(session, cfg)


def delete_policy_artifacts(session, cfg):
    policies = cfg.get("policies") or {}
    for item in reversed(policies.get("assignments") or []):
        if not item.get("name"):
            continue
        scope = resolve_scope(cfg, item.get("scope"))
        log("Deleting policy assignment %s..." % item["name"])
        session.delete(
            "%s/providers/Microsoft.Authorization/policyAssignments/%s" % (scope, item["name"]),
            params={"api-version": ASSIGN_API},
            ok404=True,
        )
    for item in reversed(policies.get("definitions") or []):
        if not item.get("name"):
            continue
        log("Deleting policy definition %s..." % item["name"])
        session.delete(
            "/subscriptions/%s/providers/Microsoft.Authorization/policyDefinitions/%s" % (sub_id(cfg), item["name"]),
            params={"api-version": DEF_API},
            ok404=True,
        )


def trigger_policy_scan(session, cfg):
    raw_scope = (cfg.get("policies") or {}).get("scan_scope") or "subscription"
    if str(raw_scope).lower() in ("cluster", "aks"):
        sys.exit("Azure Policy triggerEvaluation supports subscription or resource_group scope; use resource_group for an AKS test cluster.")
    scope = resolve_scope(cfg, raw_scope)
    if "/providers/Microsoft.ContainerService/managedClusters/" in scope.lower():
        sys.exit("Azure Policy triggerEvaluation supports subscription or resource_group scope, not a cluster resource ID.")
    if "/resourcegroups/" in scope.lower():
        url = "%s/providers/Microsoft.PolicyInsights/policyStates/latest/triggerEvaluation" % scope
    else:
        url = "/subscriptions/%s/providers/Microsoft.PolicyInsights/policyStates/latest/triggerEvaluation" % sub_id(cfg)
    log("Triggering Azure Policy evaluation at %s..." % scope)
    return session.post(url, params={"api-version": POLICY_STATES_API}, payload=None)


def delete_resource_group(session, cfg):
    if not (cfg.get("cleanup") or {}).get("delete_resource_group"):
        log("cleanup.delete_resource_group is false; leaving resource group in place.")
        return
    log("Deleting resource group %s..." % cfg["resource_group"])
    session.delete(
        "/subscriptions/%s/resourcegroups/%s" % (sub_id(cfg), cfg["resource_group"]),
        params={"api-version": RG_API},
        ok404=True,
    )


def write_temp_sub_csv(cfg):
    tmp = tempfile.NamedTemporaryFile("w", newline="", suffix=".csv", delete=False, encoding="utf-8")
    with tmp:
        writer = csv.DictWriter(tmp, fieldnames=["subscription_id", "subscription_name", "environment", "include"])
        writer.writeheader()
        writer.writerow({
            "subscription_id": sub_id(cfg),
            "subscription_name": cfg.get("subscription_name") or "sandbox",
            "environment": cfg.get("environment") or "sandbox",
            "include": "Y",
        })
    return tmp.name


def run_policy_report(cfg, out_dir):
    import policy_report
    csv_path = write_temp_sub_csv(cfg)
    try:
        argv = ["--csv", csv_path, "--out", out_dir, "--all"]
        log("Running sandbox policy report...")
        return policy_report.main(argv)
    finally:
        try:
            os.unlink(csv_path)
        except OSError:
            pass


def build_parser():
    p = argparse.ArgumentParser(
        prog="python aks_report.py sandbox",
        description="Admin-only sandbox AKS deployment and Azure Policy test workflow.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Show what the sandbox config will do; no Azure writes.")
    plan.add_argument("config", help="YAML or JSON sandbox config file")

    deploy = sub.add_parser("deploy", help="Create/update resource group and deploy the AKS cluster.")
    deploy.add_argument("config")
    deploy.add_argument("--yes", action="store_true", help="Required for Azure write operations")
    deploy.add_argument("--wait", action="store_true", help="Poll ARM deployment until complete")
    deploy.add_argument("--with-policies", action="store_true", help="Apply configured policies after starting deployment")

    pol = sub.add_parser("policy-apply", help="Create/update custom policy definitions and assignments.")
    pol.add_argument("config")
    pol.add_argument("--yes", action="store_true", help="Required for Azure write operations")

    scan = sub.add_parser("scan", help="Trigger Azure Policy compliance evaluation.")
    scan.add_argument("config")
    scan.add_argument("--yes", action="store_true", help="Required for Azure write operations")

    rep = sub.add_parser("report", help="Run the existing Azure Policy XLSX report for the sandbox subscription.")
    rep.add_argument("config")
    rep.add_argument("--out", default="reports", help="Output directory for XLSX report")

    cleanup = sub.add_parser("cleanup", help="Delete configured policy artifacts and optionally the resource group.")
    cleanup.add_argument("config")
    cleanup.add_argument("--yes", action="store_true", help="Required for Azure delete operations")
    cleanup.add_argument("--keep-policies", action="store_true", help="Do not delete configured policy assignments/definitions")
    cleanup.add_argument("--keep-resource-group", action="store_true", help="Do not delete the sandbox resource group")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "plan":
        print_plan(cfg)
        return

    if args.command == "report":
        return run_policy_report(cfg, args.out)

    require_yes(args, args.command)
    ensure_sandbox_safe(cfg, args.command)
    from azrep.http_client import connect
    session = connect(min_interval=0.1)

    if args.command == "deploy":
        result = deploy_cluster(session, cfg, wait=args.wait)
        if args.with_policies:
            apply_policies(session, cfg)
        log("Sandbox deploy submitted for cluster %s." % cfg["cluster"]["name"])
        return result
    if args.command == "policy-apply":
        return apply_policies(session, cfg)
    if args.command == "scan":
        return trigger_policy_scan(session, cfg)
    if args.command == "cleanup":
        if not args.keep_policies:
            delete_policy_artifacts(session, cfg)
        if not args.keep_resource_group:
            delete_resource_group(session, cfg)
        log("Sandbox cleanup submitted.")
        return


if __name__ == "__main__":
    main()
