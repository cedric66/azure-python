"""Upgrade rehearsal for the sandbox cluster: hop minor-by-minor with health gates.

Computes the supported hop path, blocks on an offline deprecated-API scan of your
manifests, then per hop: control-plane upgrade (PUT WITHOUT agentPoolProfiles =
control-plane-only), kubectl health gate, sequential pool upgrades (spot pools last),
gate again. Produces a timing/result workbook you can hand to app teams before
recommending the same path in prod.
"""
import glob
import sys
import time
from pathlib import Path

from azrep import kubectl as kctl
from azrep.armextras import aks_supported_versions
from azrep.http_client import log
from azrep.sandbox import AKS_API, cluster_id, resolve_path, wait_for_provisioning
from version_eol import minor

# K8s APIs REMOVED at each minor (the upgrade crossing that minor breaks these).
# Source: kubernetes.io/docs/reference/using-api/deprecation-guide/ - review at each new minor.
REMOVED_APIS = {
    "1.25": [("policy/v1beta1", "PodSecurityPolicy"), ("policy/v1beta1", "PodDisruptionBudget"),
             ("batch/v1beta1", "CronJob"), ("discovery.k8s.io/v1beta1", "EndpointSlice"),
             ("events.k8s.io/v1beta1", "Event"),
             ("autoscaling/v2beta1", "HorizontalPodAutoscaler"),
             ("node.k8s.io/v1beta1", "RuntimeClass")],
    "1.26": [("autoscaling/v2beta2", "HorizontalPodAutoscaler"),
             ("flowcontrol.apiserver.k8s.io/v1beta1", "*")],
    "1.27": [("storage.k8s.io/v1beta1", "CSIStorageCapacity")],
    "1.29": [("flowcontrol.apiserver.k8s.io/v1beta2", "*")],
    "1.32": [("flowcontrol.apiserver.k8s.io/v1beta3", "*")],
}

CLUSTER_READONLY = ("provisioningState", "powerState", "currentKubernetesVersion",
                    "maxAgentPools", "fqdn", "privateFQDN", "azurePortalFQDN")
POOL_READONLY = ("provisioningState", "powerState", "currentOrchestratorVersion",
                 "nodeImageVersion")


def _ver_key(v):
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


def hop_path(current, target, supported):
    """List of full versions to step through, one minor per hop, highest patch each."""
    nonpreview = sorted((m for m, info in supported.items() if not info["is_preview"]),
                        key=_ver_key)
    if not nonpreview:
        sys.exit("No supported (non-preview) AKS versions returned for this region.")
    cur = minor(current)
    if target == "latest":
        tgt_minor, tgt_patch = nonpreview[-1], None
    elif target == "next":
        nxt = [m for m in nonpreview if _ver_key(m) > _ver_key(cur)]
        if not nxt:
            sys.exit("Cluster is already on the newest supported minor (%s)." % cur)
        tgt_minor, tgt_patch = nxt[0], None
    else:
        tgt_minor = minor(target)
        tgt_patch = target if target.count(".") >= 2 else None
    if _ver_key(tgt_minor) <= _ver_key(cur):
        sys.exit("Target %s is not newer than the current version %s." % (target, current))

    hops = []
    for m in sorted(supported, key=_ver_key):
        if _ver_key(cur) < _ver_key(m) <= _ver_key(tgt_minor):
            hops.append(m)
    expect = list(range(_ver_key(cur)[1] + 1, _ver_key(tgt_minor)[1] + 1))
    if [_ver_key(m)[1] for m in hops] != expect:
        sys.exit("Hop path %s -> %s needs intermediate minors not supported in this region "
                 "(found: %s)." % (cur, tgt_minor, ", ".join(hops) or "none"))
    out = []
    for m in hops:
        patches = sorted(supported[m]["patches"], key=_ver_key)
        if not patches:
            sys.exit("No patch versions listed for %s in this region." % m)
        if m == tgt_minor and tgt_patch:
            if tgt_patch not in patches:
                sys.exit("Patch %s not supported in this region (available: %s)."
                         % (tgt_patch, ", ".join(patches)))
            out.append(tgt_patch)
        else:
            out.append(patches[-1])
    return out


def gather_manifests(cfg, args):
    paths = []
    for case in ((cfg.get("k8s_tests") or {}).get("cases") or []):
        if case.get("manifest"):
            paths.append(resolve_path(cfg, case["manifest"]))
    for pattern in args.manifests:
        hits = glob.glob(pattern, recursive=True)
        if not hits:
            log("WARNING: --manifests %s matched nothing." % pattern)
        paths.extend(Path(h) for h in hits)
    return [p for p in dict.fromkeys(paths) if Path(p).is_file()]


def deprecated_api_check(paths, hops):
    import yaml
    crossing = {minor(h) for h in hops}
    findings = []
    for path in paths:
        try:
            docs = list(yaml.safe_load_all(Path(path).read_text(encoding="utf-8")))
        except yaml.YAMLError as e:
            findings.append((str(path), "?", "?", "unparseable: %s" % str(e)[:100]))
            continue
        for doc in docs:
            if not isinstance(doc, dict) or not doc.get("apiVersion"):
                continue
            api, kind = doc["apiVersion"], doc.get("kind", "")
            for m in sorted(crossing, key=_ver_key):
                for bad_api, bad_kind in REMOVED_APIS.get(m, []):
                    if api == bad_api and bad_kind in ("*", kind):
                        findings.append((str(path), api, kind, "removed in %s" % m))
    return findings


def upgrade_control_plane(session, cfg, version):
    url = cluster_id(cfg)
    body = session.get(url, params={"api-version": AKS_API}) or {}
    props = dict(body.get("properties") or {})
    for key in CLUSTER_READONLY:
        props.pop(key, None)
    # no agentPoolProfiles in the PUT = control-plane-only upgrade
    props.pop("agentPoolProfiles", None)
    props["kubernetesVersion"] = version
    payload = {"location": body.get("location"), "sku": body.get("sku"),
               "identity": body.get("identity"), "tags": body.get("tags"),
               "properties": props}
    log("Upgrading control plane to %s..." % version)
    session.put(url, params={"api-version": AKS_API}, payload=payload)
    wait_for_provisioning(session, url, AKS_API, what="control plane %s" % version)


def upgrade_pool(session, cfg, pool_name, version):
    url = "%s/agentPools/%s" % (cluster_id(cfg), pool_name)
    props = dict((session.get(url, params={"api-version": AKS_API}) or {})
                 .get("properties") or {})
    for key in POOL_READONLY:
        props.pop(key, None)
    props["orchestratorVersion"] = version
    log("Upgrading pool %s to %s..." % (pool_name, version))
    session.put(url, params={"api-version": AKS_API}, payload={"properties": props})
    wait_for_provisioning(session, url, AKS_API, what="pool %s" % pool_name)


def list_pools(session, cfg):
    pools = session.get_paged("%s/agentPools" % cluster_id(cfg),
                              params={"api-version": AKS_API})
    # spot pools upgrade last: evictions there must not block the regular pools
    return sorted(pools, key=lambda p: str((p.get("properties") or {})
                                           .get("scaleSetPriority") or "").lower() == "spot")


def health_gate(kubeconfig, target_minor):
    checks = []
    nodes = (kctl.kubectl_json(kubeconfig, ["get", "nodes"]) or {}).get("items") or []
    if not nodes:
        return [("nodes", "FAIL", "could not list nodes")]
    not_ready, wrong_ver = [], []
    for n in nodes:
        name = n["metadata"]["name"]
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in ((n.get("status") or {}).get("conditions") or []))
        if not ready:
            not_ready.append(name)
        kubelet = ((n.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion", "")
        if target_minor and minor(kubelet.lstrip("v")) not in ("", target_minor):
            wrong_ver.append("%s=%s" % (name, kubelet))
    checks.append(("nodes_ready", "PASS" if not not_ready else "FAIL",
                   ", ".join(not_ready) or "%d nodes Ready" % len(nodes)))
    if target_minor:
        checks.append(("kubelet_version", "PASS" if not wrong_ver else "FAIL",
                       ", ".join(wrong_ver[:5]) or "all kubelets on %s" % target_minor))
    pods = (kctl.kubectl_json(kubeconfig, ["get", "pods", "-A"]) or {}).get("items") or []
    broken = []
    for p in pods:
        ns = p["metadata"].get("namespace", "")
        if ns in ("kube-system", "gatekeeper-system"):
            continue
        for cs in ((p.get("status") or {}).get("containerStatuses") or []):
            reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason", "")
            if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                broken.append("%s/%s (%s)" % (ns, p["metadata"]["name"], reason))
    checks.append(("workloads", "PASS" if not broken else "FAIL",
                   ", ".join(broken[:5]) or "no crash-looping pods outside kube-system"))
    return checks


def write_report(args, cfg, hops, hop_rows, gate_rows, findings):
    import pandas as pd
    from azrep import excel
    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS upgrade rehearsal", [
        "Cluster: %s | path: %s" % (cfg["cluster"]["name"], " -> ".join(hops)),
        "Per hop: control-plane upgrade (PUT without agentPoolProfiles), kubectl health "
        "gate, sequential pool upgrades (spot last), gate again.",
        "Use the hop timings to size prod maintenance windows.",
    ])
    excel.add_table(wb, "Hops",
                    pd.DataFrame(hop_rows, columns=["hop", "phase", "result", "minutes",
                                                    "detail"]),
                    fail_cols=("result",), section="summary")
    if gate_rows:
        excel.add_table(wb, "GateResults",
                        pd.DataFrame(gate_rows, columns=["hop", "check", "status", "detail"]),
                        fail_cols=("status",))
    if findings:
        excel.add_table(wb, "DeprecatedAPIs",
                        pd.DataFrame(findings, columns=["file", "apiVersion", "kind",
                                                        "finding"]),
                        section="reference")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    path = "%s/upgrade_rehearsal_%s_%s.xlsx" % (args.out, cfg["cluster"]["name"],
                                                time.strftime("%Y%m%d_%H%M%S"))
    excel.save(wb, path)
    log("Wrote %s" % path)


def run(session, cfg, args):
    url = cluster_id(cfg)
    body = session.get(url, params={"api-version": AKS_API}, ok404=True)
    if not body:
        sys.exit("Sandbox cluster %s not found; deploy it first." % cfg["cluster"]["name"])
    current = ((body.get("properties") or {}).get("currentKubernetesVersion")
               or (body.get("properties") or {}).get("kubernetesVersion") or "")
    location = body.get("location") or cfg["location"]
    supported = aks_supported_versions(session, cfg["subscription_id"], location)
    hops = hop_path(current, args.to.strip().lower(), supported)
    log("Upgrade path: %s -> %s" % (current, " -> ".join(hops)))

    manifests = gather_manifests(cfg, args)
    findings = deprecated_api_check(manifests, hops)
    if findings:
        for f in findings:
            log("  DEPRECATED: %s uses %s/%s (%s)" % f)
        if not args.force:
            sys.exit("Deprecated-API findings block the rehearsal; fix the manifests or "
                     "rerun with --force.")

    kubeconfig = None
    if not args.no_kubectl:
        kubeconfig = kctl.fetch_kubeconfig(cfg, session=session)

    hop_rows, gate_rows = [], []

    def gate(hop, label, target_minor):
        if not kubeconfig:
            hop_rows.append((hop, label, "SKIP", 0, "--no-kubectl"))
            return True
        checks = health_gate(kubeconfig, target_minor)
        gate_rows.extend((hop, c, s, d) for c, s, d in checks)
        ok = all(s == "PASS" for _c, s, _d in checks)
        hop_rows.append((hop, label, "PASS" if ok else "FAIL", 0,
                         "; ".join("%s=%s" % (c, s) for c, s, _d in checks)))
        return ok

    failed = False
    for hop in hops:
        started = time.time()
        try:
            upgrade_control_plane(session, cfg, hop)
        except SystemExit as e:
            hop_rows.append((hop, "control_plane", "FAIL",
                             round((time.time() - started) / 60, 1), str(e)))
            failed = True
            break
        hop_rows.append((hop, "control_plane", "PASS",
                         round((time.time() - started) / 60, 1), ""))
        if not gate(hop, "gate_after_control_plane", None):
            failed = True
            break
        if args.control_plane_only:
            continue
        for pool in list_pools(session, cfg):
            started = time.time()
            try:
                upgrade_pool(session, cfg, pool["name"], hop)
            except SystemExit as e:
                hop_rows.append((hop, "pool_%s" % pool["name"], "FAIL",
                                 round((time.time() - started) / 60, 1), str(e)))
                failed = True
                break
            hop_rows.append((hop, "pool_%s" % pool["name"], "PASS",
                             round((time.time() - started) / 60, 1), ""))
        if failed or not gate(hop, "gate_after_pools",
                              minor(hop) if not args.control_plane_only else None):
            failed = True
            break

    write_report(args, cfg, hops, hop_rows, gate_rows, findings)
    log("Upgrade rehearsal %s." % ("FAILED - see report" if failed else "complete"))
    if failed:
        sys.exit(1)
