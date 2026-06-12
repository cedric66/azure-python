"""kubectl/kubeconfig helpers for the sandbox cluster (subprocess-based, no kubernetes SDK).

Kubeconfigs are written next to the sandbox config file as .kubeconfig-<cluster> (gitignored)
so they never touch ~/.kube/config. Preferred fetch path is `az aks get-credentials` +
`kubelogin convert-kubeconfig -l azurecli`; the ARM listClusterUserCredential fallback keeps
this usable without the az CLI.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from azrep.http_client import log

AKS_API = "2024-05-01"
KUBECTL_TIMEOUT = 120

# test seam: tests replace _run with a scripted fake instead of spawning processes
_run = subprocess.run


def have(binary):
    return shutil.which(binary) is not None


def kubeconfig_path(cfg):
    return Path(cfg["_config_dir"]) / (".kubeconfig-%s" % cfg["cluster"]["name"])


def _run_tool(argv, timeout=KUBECTL_TIMEOUT, env=None):
    try:
        r = _run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds: %s" % (timeout, " ".join(argv))
    return r.returncode, r.stdout, r.stderr


def _convert_kubeconfig(path):
    if not have("kubelogin"):
        log("WARNING: kubelogin not found; AAD clusters may prompt interactively or fail.")
        return
    rc, _, err = _run_tool(["kubelogin", "convert-kubeconfig", "-l", "azurecli",
                            "--kubeconfig", str(path)])
    if rc != 0:
        log("WARNING: kubelogin convert-kubeconfig failed: %s" % err.strip())


def fetch_kubeconfig(cfg, session=None, force=False):
    """Fetch a dedicated kubeconfig for the sandbox cluster and return its path."""
    path = kubeconfig_path(cfg)
    if path.exists() and not force:
        return path
    cluster = cfg["cluster"]["name"]
    if have("az"):
        log("Fetching kubeconfig for %s via az aks get-credentials..." % cluster)
        rc, _, err = _run_tool([
            "az", "aks", "get-credentials",
            "--resource-group", cfg["resource_group"], "--name", cluster,
            "--subscription", cfg["subscription_id"],
            "--file", str(path), "--overwrite-existing",
        ])
        if rc != 0:
            sys.exit("az aks get-credentials failed: %s" % err.strip())
    elif session is not None:
        log("az CLI not found; fetching kubeconfig for %s via ARM..." % cluster)
        url = "/subscriptions/%s/resourceGroups/%s/providers/Microsoft.ContainerService" \
              "/managedClusters/%s/listClusterUserCredential" % (
                  cfg["subscription_id"], cfg["resource_group"], cluster)
        data = session.post(url, params={"api-version": AKS_API, "format": "exec"})
        kubeconfigs = (data or {}).get("kubeconfigs") or []
        if not kubeconfigs:
            sys.exit("listClusterUserCredential returned no kubeconfigs for %s" % cluster)
        path.write_bytes(base64.b64decode(kubeconfigs[0]["value"]))
        path.chmod(0o600)
    else:
        sys.exit("Need the az CLI or an Azure session to fetch a kubeconfig.")
    _convert_kubeconfig(path)
    return path


def run_kubectl(kubeconfig, args, timeout=KUBECTL_TIMEOUT):
    """Run kubectl against the given kubeconfig; returns (rc, stdout, stderr), never raises."""
    if not have("kubectl"):
        sys.exit("kubectl not found on PATH.")
    env = dict(os.environ)
    env["KUBECONFIG"] = str(kubeconfig)
    argv = ["kubectl"] + list(args)
    log("  $ %s" % " ".join(argv))
    return _run_tool(argv, timeout=timeout, env=env)


def kubectl_json(kubeconfig, args):
    """kubectl ... -o json -> parsed dict, or None when the call or parse fails."""
    rc, out, _ = run_kubectl(kubeconfig, list(args) + ["-o", "json"])
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def apply_manifest(kubeconfig, path, namespace=None):
    args = ["apply", "-f", str(path)]
    if namespace:
        args += ["-n", namespace]
    return run_kubectl(kubeconfig, args)


def delete_manifest(kubeconfig, path, namespace=None):
    args = ["delete", "-f", str(path), "--ignore-not-found", "--wait=false"]
    if namespace:
        args += ["-n", namespace]
    return run_kubectl(kubeconfig, args)


def ensure_namespace(kubeconfig, namespace):
    rc, _, _ = run_kubectl(kubeconfig, ["get", "namespace", namespace])
    if rc != 0:
        rc, _, err = run_kubectl(kubeconfig, ["create", "namespace", namespace])
        if rc != 0:
            sys.exit("Could not create namespace %s: %s" % (namespace, err.strip()))


def delete_namespace(kubeconfig, namespace):
    return run_kubectl(kubeconfig, ["delete", "namespace", namespace,
                                    "--ignore-not-found", "--wait=false"])
