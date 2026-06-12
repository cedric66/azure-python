"""Gatekeeper (Azure Policy for AKS) admission/audit test harness for the sandbox cluster.

Cases come from the sandbox config's k8s_tests block; each applies a manifest and asserts
the admission result: expect deny (webhook must reject), allow (must admit), or audit
(must admit, then show up in constraint status.violations once the ~60s audit loop runs).
"""
import sys
import time
from pathlib import Path

from azrep import kubectl as kctl
from azrep.http_client import log
from azrep.sandbox import resolve_path

DENY_MARKERS = ("denied the request", "admission webhook", "validation.gatekeeper.sh")
POLL_S = 15


def _pods_ready(kubeconfig, namespace, selector=None):
    args = ["get", "pods", "-n", namespace]
    if selector:
        args += ["-l", selector]
    data = kctl.kubectl_json(kubeconfig, args)
    items = (data or {}).get("items") or []
    ready = 0
    for pod in items:
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        if statuses and all(s.get("ready") for s in statuses):
            ready += 1
    return ready, len(items)


def wait_for_gatekeeper(kubeconfig, timeout):
    """The azure-policy addon needs its kube-system pods plus gatekeeper-system up."""
    deadline = time.time() + timeout
    while True:
        ap_ready, ap_total = _pods_ready(kubeconfig, "kube-system", "app=azure-policy")
        gk_ready, gk_total = _pods_ready(kubeconfig, "gatekeeper-system")
        crd_rc, _, _ = kctl.run_kubectl(
            kubeconfig, ["get", "crd", "constrainttemplates.templates.gatekeeper.sh"])
        log("Gatekeeper readiness: azure-policy %d/%d, gatekeeper-system %d/%d, CRD %s"
            % (ap_ready, ap_total, gk_ready, gk_total, "ok" if crd_rc == 0 else "missing"))
        if ap_total and ap_ready == ap_total and gk_total and gk_ready == gk_total and crd_rc == 0:
            return
        if time.time() > deadline:
            sys.exit("Gatekeeper/azure-policy addon not ready after %ds. Is "
                     "cluster.azure_policy_addon: true and the cluster running?" % timeout)
        time.sleep(POLL_S)


def get_constraints(kubeconfig):
    data = kctl.kubectl_json(kubeconfig, ["get", "constraints", "-A"])
    return (data or {}).get("items") or []


def wait_for_constraints(kubeconfig, timeout, want_contains=()):
    """Azure Policy assignments replicate into constraints asynchronously (up to ~15 min)."""
    deadline = time.time() + timeout
    want = [w.lower() for w in want_contains if w]
    while True:
        items = get_constraints(kubeconfig)
        names = ["%s/%s" % (i.get("kind", ""), (i.get("metadata") or {}).get("name", ""))
                 for i in items]
        missing = [w for w in want if not any(w in n.lower() for n in names)]
        log("Constraints synced: %d (%s)" % (len(items), ", ".join(names) or "none"))
        if items and not missing:
            return items
        if time.time() > deadline:
            sys.exit("Constraints not synced after %ds (missing: %s). Azure Policy can take "
                     "up to ~15 min to replicate; raise k8s_tests.constraint_wait_seconds "
                     "or rerun." % (timeout, ", ".join(missing) or "any"))
        time.sleep(POLL_S)


def manifest_resources(path):
    """[(kind, name), ...] from a (multi-doc) manifest, for audit violation matching."""
    import yaml
    out = []
    for doc in yaml.safe_load_all(Path(path).read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind"):
            out.append((doc["kind"], ((doc.get("metadata") or {}).get("name")) or ""))
    return out


def find_violations(constraints, namespace, resources, constraint_contains=None):
    hits = []
    frag = (constraint_contains or "").lower()
    for item in constraints:
        cname = "%s/%s" % (item.get("kind", ""), (item.get("metadata") or {}).get("name", ""))
        if frag and frag not in cname.lower():
            continue
        for v in ((item.get("status") or {}).get("violations") or []):
            for kind, name in resources:
                if v.get("name") == name and v.get("kind") == kind \
                        and v.get("namespace") in (namespace, None, ""):
                    hits.append((cname, v.get("message", "")))
    return hits


def run_case(kubeconfig, cfg, case, namespace, constraint_wait):
    name = case.get("name") or case.get("manifest", "?")
    expect = (case.get("expect") or "allow").lower()
    path = resolve_path(cfg, case["manifest"])
    if not path.exists():
        return (name, expect, "FAIL", "manifest not found: %s" % path)
    rc, out, err = kctl.apply_manifest(kubeconfig, path, namespace=namespace)
    blob = (out + "\n" + err).lower()

    if expect == "deny":
        if rc != 0 and any(m in blob for m in DENY_MARKERS):
            return (name, expect, "PASS", "admission denied as expected")
        kctl.delete_manifest(kubeconfig, path, namespace=namespace)
        if rc == 0:
            return (name, expect, "FAIL", "resource was admitted but expected a deny")
        return (name, expect, "FAIL", "apply failed for another reason: %s" % err.strip()[:300])

    if rc != 0:
        return (name, expect, "FAIL", "apply failed: %s" % err.strip()[:300])

    if expect == "allow":
        kctl.delete_manifest(kubeconfig, path, namespace=namespace)
        return (name, expect, "PASS", "admitted as expected")

    if expect == "audit":
        resources = manifest_resources(path)
        deadline = time.time() + constraint_wait
        while True:
            hits = find_violations(get_constraints(kubeconfig), namespace, resources,
                                   case.get("constraint_contains"))
            if hits:
                kctl.delete_manifest(kubeconfig, path, namespace=namespace)
                return (name, expect, "PASS", "audit violation: %s" % hits[0][1][:200])
            if time.time() > deadline:
                kctl.delete_manifest(kubeconfig, path, namespace=namespace)
                return (name, expect, "FAIL",
                        "no audit violation within %ds (audit runs ~every 60s)" % constraint_wait)
            log("  waiting for audit violation on %s..." % name)
            time.sleep(POLL_S)

    kctl.delete_manifest(kubeconfig, path, namespace=namespace)
    return (name, expect, "FAIL", "unknown expect `%s` (use deny/allow/audit)" % expect)


def print_results(rows):
    width = max(len(r[0]) for r in rows) + 2
    print("\n%-*s %-7s %-6s %s" % (width, "case", "expect", "status", "detail"))
    for name, expect, status, detail in rows:
        print("%-*s %-7s %-6s %s" % (width, name, expect, status, detail))


def write_xlsx(rows, violations, cfg, out_dir):
    import pandas as pd
    from azrep import excel
    wb = excel.new_workbook()
    excel.add_readme(wb, "Sandbox Gatekeeper test results", [
        "Cluster: %s" % cfg["cluster"]["name"],
        "Cases come from the sandbox config k8s_tests block; deny cases assert the "
        "admission webhook rejects the manifest, allow cases assert admission, audit "
        "cases assert a constraint status violation appears.",
    ])
    excel.add_table(wb, "Results",
                    pd.DataFrame(rows, columns=["case", "expect", "status", "detail"]),
                    fail_cols=("status",), section="summary")
    if violations:
        excel.add_table(wb, "AuditViolations",
                        pd.DataFrame(violations, columns=["constraint", "enforcement", "kind",
                                                          "namespace", "name", "message"]))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = "%s/k8s_test_%s_%s.xlsx" % (out_dir, cfg["cluster"]["name"],
                                       time.strftime("%Y%m%d_%H%M%S"))
    excel.save(wb, path)
    log("Wrote %s" % path)


def all_violations(constraints):
    rows = []
    for item in constraints:
        cname = "%s/%s" % (item.get("kind", ""), (item.get("metadata") or {}).get("name", ""))
        action = (item.get("spec") or {}).get("enforcementAction", "")
        for v in ((item.get("status") or {}).get("violations") or []):
            rows.append((cname, action, v.get("kind", ""), v.get("namespace", ""),
                         v.get("name", ""), v.get("message", "")))
    return rows


def run(session, cfg, args):
    tests = cfg.get("k8s_tests") or {}
    cases = tests.get("cases") or []
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.get("name") in wanted]
    if not cases:
        sys.exit("No k8s_tests.cases in the config%s. See sandbox.example.yaml."
                 % (" matching --case" if args.case else ""))

    kubeconfig = kctl.fetch_kubeconfig(cfg, session=session)
    namespace = tests.get("namespace") or "policy-test"
    constraint_wait = int(tests.get("constraint_wait_seconds") or 300)

    kctl.ensure_namespace(kubeconfig, namespace)
    wait_for_gatekeeper(kubeconfig, constraint_wait)
    constraints = wait_for_constraints(
        kubeconfig, constraint_wait,
        want_contains=[c.get("constraint_contains") for c in cases])

    rows = [run_case(kubeconfig, cfg, case, namespace, constraint_wait) for case in cases]
    violations = all_violations(get_constraints(kubeconfig) or constraints)

    if not args.keep:
        kctl.delete_namespace(kubeconfig, namespace)

    print_results(rows)
    if args.xlsx:
        write_xlsx(rows, violations, cfg, args.out)
    failed = [r for r in rows if r[2] == "FAIL"]
    log("k8s-test: %d/%d cases passed." % (len(rows) - len(failed), len(rows)))
    if failed:
        sys.exit(1)
