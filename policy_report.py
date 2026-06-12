"""Azure Policy report for AKS: which policy assignments apply to your clusters
(including ones inherited from management groups), their compliance state per
cluster, and where Kubernetes policies CANNOT evaluate because the Azure Policy
addon is off ("blind spots").

Sources (all readable with Reader): policy assignments at subscription scope
($filter=atScope() includes inherited), PolicyInsights policy states filtered to
managed clusters, and definition metadata for display names/categories.

Tabs: ReadMe, Assignments, ClusterCompliance, NonCompliantDetail,
KubernetesBlindSpots, Summary.

Usage: python policy_report.py [--all|--nonprod|--env dev]
"""
import datetime as dt
from urllib.parse import quote

import pandas as pd

from azrep import excel
from azrep.fleet import load_fleet
from azrep.http_client import AzureApiError, connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

ASSIGN_API = "2022-06-01"
DEF_API = "2023-04-01"
STATES_API = "2019-10-01"

_def_cache = {}


def definition_info(session, def_id):
    """displayName + category for a policy or policy-set definition id (cached;
    built-ins repeat across subscriptions)."""
    key = (def_id or "").lower()
    if not key:
        return {"name": "", "category": ""}
    if key in _def_cache:
        return _def_cache[key]
    try:
        d = session.get(def_id, params={"api-version": DEF_API}, ok404=True) or {}
        props = d.get("properties") or {}
        info = {"name": props.get("displayName") or def_id.split("/")[-1],
                "category": (props.get("metadata") or {}).get("category") or ""}
    except AzureApiError:
        info = {"name": def_id.split("/")[-1], "category": "(no read access)"}
    _def_cache[key] = info
    return info


def main(argv=None):
    args = base_parser("Azure Policy compliance for AKS clusters").parse_args(argv)
    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    session = connect(min_interval=0.1)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, _pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    cluster_by_id = {c["id"].lower(): c for c in clusters}

    assign_rows, state_rows = [], []
    for i, s in enumerate(sel, 1):
        sid = s["subscription_id"]
        log("[%d/%d] %s: policy assignments + AKS compliance states..."
            % (i, len(sel), s["subscription_name"] or sid))
        assigns = session.get_paged(
            "/subscriptions/%s/providers/Microsoft.Authorization/policyAssignments" % sid,
            params={"api-version": ASSIGN_API, "$filter": "atScope()"})
        for a in assigns:
            props = a.get("properties") or {}
            def_id = props.get("policyDefinitionId") or ""
            info = definition_info(session, def_id)
            assign_rows.append({
                "subscription": s["subscription_name"] or sid,
                "assignment": props.get("displayName") or a.get("name"),
                "assignment_id": a.get("id"),
                "scope": props.get("scope") or "",
                "inherited": not (props.get("scope") or "").lower().startswith(
                    "/subscriptions/%s" % sid),
                "enforcement": props.get("enforcementMode") or "Default",
                "definition": info["name"],
                "category": info["category"],
                "is_initiative": "/policysetdefinitions/" in def_id.lower(),
            })
        flt = quote("resourceType eq 'microsoft.containerservice/managedclusters'")
        states = session.post_paged(
            "/subscriptions/%s/providers/Microsoft.PolicyInsights/policyStates/latest/"
            "queryResults?api-version=%s&$filter=%s" % (sid, STATES_API, flt),
            payload=None)
        for st in states:
            rid = (st.get("resourceId") or "").lower()
            cl = cluster_by_id.get(rid)
            if cl is None:
                continue  # cluster filtered out of scope (env filter) or deleted
            info = definition_info(session, st.get("policyDefinitionId") or "")
            state_rows.append({
                "cluster": cl["cluster"], "subscription": cl["subscription"],
                "environment": cl["environment"],
                "policy": info["name"], "category": info["category"],
                "assignment": (st.get("policyAssignmentId") or "").split("/")[-1],
                "compliance": st.get("complianceState") or "",
                "action": st.get("policyDefinitionAction") or "",
                "reference_id": st.get("policyDefinitionReferenceId") or "",
            })

    adf = pd.DataFrame(assign_rows) if assign_rows else pd.DataFrame(
        columns=["subscription", "assignment", "assignment_id", "scope", "inherited",
                 "enforcement", "definition", "category", "is_initiative"])
    sdf = pd.DataFrame(state_rows) if state_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "policy", "category",
                 "assignment", "compliance", "action", "reference_id"])

    if not sdf.empty:
        comp = sdf.pivot_table(index=["cluster", "subscription", "environment"],
                               columns="compliance", values="policy",
                               aggfunc="count").fillna(0).astype(int).reset_index()
        for col in ("Compliant", "NonCompliant"):
            if col not in comp.columns:
                comp[col] = 0
        # sort BEFORE adding formula columns - formulas are anchored to row numbers
        comp = comp.sort_values("NonCompliant", ascending=False).reset_index(drop=True)
        n = len(comp)
        cols = list(comp.columns)
        cl_c = excel.get_column_letter(cols.index("Compliant") + 1)
        cl_n = excel.get_column_letter(cols.index("NonCompliant") + 1)
        comp["NonCompliant %"] = ["=IF(%s%d+%s%d=0,\"\",%s%d/(%s%d+%s%d))"
                                  % (cl_c, r, cl_n, r, cl_n, r, cl_c, r, cl_n, r)
                                  for r in range(2, n + 2)]
    else:
        comp = pd.DataFrame(columns=["cluster", "subscription", "environment",
                                     "Compliant", "NonCompliant"])

    noncomp = sdf[sdf["compliance"] == "NonCompliant"].copy() if not sdf.empty else sdf

    # Kubernetes policies need the cluster-side addon (gatekeeper) to evaluate.
    k8s_subs = set(adf.loc[adf["category"].str.lower() == "kubernetes", "subscription"]) \
        if not adf.empty else set()
    blind = pd.DataFrame([{
        "cluster": c["cluster"], "subscription": c["subscription"],
        "environment": c["environment"],
        "policy_addon_enabled": c["addon_azure_policy"],
        "k8s_policies_assigned_in_sub": c["subscription"] in k8s_subs,
        "status": ("BLIND SPOT" if (c["subscription"] in k8s_subs
                                    and not c["addon_azure_policy"])
                   else ("OK" if c["addon_azure_policy"] else "NO ADDON")),
    } for c in clusters])

    summary = pd.DataFrame([
        ("Subscriptions scanned", len(sel)),
        ("Clusters in scope", len(clusters)),
        ("Policy assignments visible (incl. inherited)", len(adf)),
        ("  of which Kubernetes-category", int((adf["category"].str.lower() == "kubernetes").sum())
         if not adf.empty else 0),
        ("Compliance state rows for AKS clusters", len(sdf)),
        ("NonCompliant rows", len(noncomp)),
        ("Clusters with policy addon disabled", int(sum(1 for c in clusters
                                                        if not c["addon_azure_policy"]))),
        ("Kubernetes-policy blind spots", int((blind["status"] == "BLIND SPOT").sum())
         if not blind.empty else 0),
    ], columns=["Item", "Value"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "Azure Policy Report for AKS", [
        "Generated: %s   Scope: %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                       env_filter or "all"),
        "",
        "Assignments: every policy/initiative assignment visible at each subscription",
        "scope, including ones inherited from management groups ($filter=atScope()).",
        "ClusterCompliance/NonCompliantDetail: PolicyInsights latest states filtered to",
        "Microsoft.ContainerService/managedClusters.",
        "",
        "KubernetesBlindSpots: Kubernetes-category policies (deny privileged pods etc.)",
        "are evaluated BY the Azure Policy addon on the cluster. If such policies are",
        "assigned but the addon is off, the cluster silently reports nothing - those",
        "rows are marked BLIND SPOT.",
        "Note: pod-level (gatekeeper) compliance detail is only visible where the addon",
        "is enabled; this report is control-plane data only (no kubectl).",
    ])
    excel.add_table(wb, "Assignments", adf.drop(columns=["assignment_id"]) if not adf.empty else adf,
                    max_width=70)
    excel.add_table(wb, "ClusterCompliance", comp, pct_cols=("NonCompliant %",),
                    int_cols=("Compliant", "NonCompliant"),
                    colorscale_cols=("NonCompliant %",))
    excel.add_table(wb, "NonCompliantDetail", noncomp, fail_cols=("compliance",), max_width=70)
    excel.add_table(wb, "KubernetesBlindSpots", blind, fail_cols=("status",))
    excel.add_table(wb, "Summary", summary, max_width=60, section="summary")

    path = excel.save(wb, out_path(args, "aks_policy", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
