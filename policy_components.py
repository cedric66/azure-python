"""Drill a single Azure Policy compliance initiative down to the individual
NON-COMPLIANT COMPONENTS. You pick a Compliance name (an initiative/policy-set
assignment), optionally narrow to one or more groups (the initiative's
policyDefinitionGroups - the Regulatory-Compliance controls/compliance domains)
and member policies, and the report lists every component that is failing.

"Component" is Azure Policy's granular evaluation record: for resource-provider
mode policies (Kubernetes data-plane, Key Vault data-plane, etc.) it is the
in-resource object - e.g. the actual namespace/kind/name of a Kubernetes object
inside a cluster. For ordinary resource-manager policies there are no components,
so the report falls back to the non-compliant RESOURCE itself.

Sources (all readable with Reader): policy assignments at subscription scope
($filter=atScope() includes ones inherited from management groups), the policy
set definition (groups + member policies), PolicyInsights componentPolicyStates
(the components), and PolicyInsights policyStates (resource-level fallback).

Selection is interactive by default (you are prompted for the initiative, then
groups, then policies); pass --initiative/--group/--policy to run unattended, or
--list to just print what is available. --all implies no prompts.

Tabs: ReadMe, Summary, NonCompliantComponents, Selection.

Usage:
  python policy_components.py --list
  python policy_components.py --all --initiative "pod security baseline"
  python policy_components.py --env dev --initiative "NIST" --group "AC-6" --policy "privileged"
"""
import datetime as dt
import sys
from urllib.parse import quote

import pandas as pd

from azrep import excel
from azrep.fleet import load_fleet
from azrep.http_client import AzureApiError, connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

ASSIGN_API = "2022-06-01"
DEF_API = "2023-04-01"
STATES_API = "2019-10-01"
COMPONENT_API = "2022-04-01"  # componentPolicyStates data-plane (NOT 2019-10-01)

_def_cache = {}


def definition_info(session, def_id):
    """displayName + category for a policy or policy-set definition id (cached)."""
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


def add_initiative_args(parser):
    parser.add_argument("--initiative", help="compliance name to drill: substring of the "
                        "assignment or initiative display name (case-insensitive)")
    parser.add_argument("--group", help="comma-separated group names/display names to keep "
                        "(the initiative's policyDefinitionGroups); omitted means all")
    parser.add_argument("--policy", help="comma-separated member policy names/reference ids "
                        "to keep (substring, case-insensitive); omitted means all")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="list the available initiatives (and groups/policies for a "
                        "resolved --initiative) and exit")
    return parser


def _csv_terms(raw):
    return [t.strip().lower() for t in (raw or "").split(",") if t.strip()]


def collect_initiatives(session, sel):
    """Every initiative (policy-set) assignment visible across the selected subs,
    de-duplicated by assignment id (the same MG-inherited assignment shows up in
    each child subscription)."""
    out = {}
    for i, s in enumerate(sel, 1):
        sid = s["subscription_id"]
        log("[%d/%d] %s: policy assignments..." % (i, len(sel), s["subscription_name"] or sid))
        assigns = session.get_paged(
            "/subscriptions/%s/providers/Microsoft.Authorization/policyAssignments" % sid,
            params={"api-version": ASSIGN_API, "$filter": "atScope()"})
        for a in assigns:
            props = a.get("properties") or {}
            def_id = props.get("policyDefinitionId") or ""
            if "/policysetdefinitions/" not in def_id.lower():
                continue  # single-policy assignment, not an initiative
            aid = a.get("id") or ""
            rec = out.get(aid.lower())
            if rec is None:
                info = definition_info(session, def_id)
                rec = {"assignment_id": aid, "assignment_name": a.get("name"),
                       "display": props.get("displayName") or info["name"] or a.get("name"),
                       "initiative": info["name"], "category": info["category"],
                       "set_def_id": def_id, "scope": props.get("scope") or "",
                       "subs": []}
                out[aid.lower()] = rec
            rec["subs"].append(s)
    return list(out.values())


def _match_initiatives(inits, term):
    if not term:
        return list(inits)
    t = term.lower()
    return [i for i in inits if t in (i["display"] or "").lower()
            or t in (i["initiative"] or "").lower()]


def _prompt_pick_one(items, label_fn, what):
    print("\nSelect %s:" % what)
    for i, it in enumerate(items, 1):
        print("  %2d) %s" % (i, label_fn(it)))
    raw = input("%s [number]: " % what).strip()
    if not raw or not raw.isdigit() or not (1 <= int(raw) <= len(items)):
        sys.exit("No valid %s selected." % what)
    return items[int(raw) - 1]


def _prompt_pick_many(items, label_fn, what):
    print("\nSelect %s (comma-separated numbers, Enter for all):" % what)
    for i, it in enumerate(items, 1):
        print("  %2d) %s" % (i, label_fn(it)))
    raw = input("%s [all]: " % what).strip()
    if not raw:
        return list(items)
    idx = {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}
    picked = [it for i, it in enumerate(items, 1) if i in idx]
    return picked or list(items)


def resolve_initiative(inits, args, prompt_ok):
    matches = _match_initiatives(inits, args.initiative)
    if not matches:
        sys.exit("No initiative matches %r. Run with --list to see the %d available."
                 % (args.initiative, len(inits)))
    if len(matches) == 1:
        return matches[0]
    if prompt_ok:
        return _prompt_pick_one(matches, lambda i: "%s   [%s]" % (i["display"], i["initiative"]),
                                "compliance initiative")
    sys.exit("%d initiatives match; narrow --initiative or run --list (matches: %s)."
             % (len(matches), ", ".join(i["display"] for i in matches)))


def load_set_definition(session, set_def_id):
    """Return (groups, members). groups: [{name, display, category}];
    members: [{ref, def_id, name, groups}] for each included policy."""
    d = session.get(set_def_id, params={"api-version": DEF_API}, ok404=True) or {}
    props = d.get("properties") or {}
    groups = []
    for g in props.get("policyDefinitionGroups") or []:
        groups.append({"name": g.get("name") or "",
                       "display": g.get("displayName") or g.get("name") or "",
                       "category": g.get("category") or ""})
    members = []
    for m in props.get("policyDefinitions") or []:
        def_id = m.get("policyDefinitionId") or ""
        members.append({"ref": m.get("policyDefinitionReferenceId") or "",
                        "def_id": def_id, "name": definition_info(session, def_id)["name"],
                        "groups": [str(x) for x in (m.get("groupNames") or [])]})
    return groups, members


def select_groups(groups, args, prompt_ok):
    if not groups:
        return []  # initiative has no Regulatory-Compliance groups
    terms = _csv_terms(args.group)
    if terms:
        keep = [g for g in groups if any(t in g["name"].lower() or t in g["display"].lower()
                                         for t in terms)]
        if not keep:
            sys.exit("No group matches %r (have: %s)."
                     % (args.group, ", ".join(g["name"] for g in groups)))
        return keep
    if prompt_ok:
        return _prompt_pick_many(groups, lambda g: "%s   (%s)" % (g["display"], g["category"]),
                                 "groups")
    return list(groups)


def select_policies(members, args, prompt_ok):
    terms = _csv_terms(args.policy)
    if terms:
        keep = [m for m in members if any(t in m["name"].lower() or t in m["ref"].lower()
                                          for t in terms)]
        if not keep:
            sys.exit("No member policy matches %r." % args.policy)
        return keep
    if prompt_ok:
        return _prompt_pick_many(members, lambda m: m["name"], "policies")
    return list(members)


def fetch_components(session, sub_id, assignment_id):
    """Non-compliant component records for one assignment in one subscription."""
    flt = quote("PolicyAssignmentId eq '%s' and ComplianceState eq 'NonCompliant'"
                % assignment_id)
    url = ("/subscriptions/%s/providers/Microsoft.PolicyInsights/componentPolicyStates/"
           "latest/queryResults?api-version=%s&$filter=%s" % (sub_id, COMPONENT_API, flt))
    try:
        return session.post_paged(url, payload=None)
    except AzureApiError as e:
        if e.status in (400, 404):
            return []  # no component data in this sub / assignment not component-capable
        raise


def fetch_resource_states(session, sub_id, assignment_id):
    """Non-compliant resource-level states for one assignment (fallback source)."""
    flt = quote("PolicyAssignmentId eq '%s' and ComplianceState eq 'NonCompliant'"
                % assignment_id)
    url = ("/subscriptions/%s/providers/Microsoft.PolicyInsights/policyStates/latest/"
           "queryResults?api-version=%s&$filter=%s" % (sub_id, STATES_API, flt))
    try:
        return session.post_paged(url, payload=None)
    except AzureApiError as e:
        if e.status in (400, 404):
            return []
        raise


def short_id(rid):
    return (rid or "").split("/")[-1] or rid


def main(argv=None):
    parser = add_initiative_args(base_parser(
        "Drill an Azure Policy initiative to its non-compliant components"))
    args = parser.parse_args(argv)
    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    prompt_ok = sys.stdin.isatty() and not getattr(args, "all", False)

    session = connect(min_interval=0.1)
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, _pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    cluster_by_id = {c["id"].lower(): c for c in clusters}

    inits = collect_initiatives(session, sel)
    if not inits:
        sys.exit("No policy INITIATIVE (policy-set) assignments are visible in scope. "
                 "Single-policy assignments are not drillable; use the `policy` report.")

    if getattr(args, "list_only", False) and not args.initiative:
        print("\nAvailable compliance initiatives (%d):" % len(inits))
        for i in inits:
            print("  - %s   [%s]  in %d sub(s)"
                  % (i["display"], i["initiative"], len(i["subs"])))
        return

    init = resolve_initiative(inits, args, prompt_ok)
    log("Initiative: %s  (%s)" % (init["display"], init["initiative"]))
    groups, members = load_set_definition(session, init["set_def_id"])

    sel_groups = select_groups(groups, args, prompt_ok)
    sel_group_names = {g["name"] for g in sel_groups}
    group_label = {g["name"]: g["display"] for g in groups}
    # if the initiative has groups, keep only members in the selected groups
    if groups:
        members = [m for m in members
                   if (not m["groups"] and not sel_group_names)
                   or (set(m["groups"]) & sel_group_names)]
    sel_policies = select_policies(members, args, prompt_ok)
    sel_refs = {m["ref"] for m in sel_policies}
    member_by_ref = {m["ref"]: m for m in sel_policies}

    if getattr(args, "list_only", False):
        print("\n%s" % init["display"])
        print("Groups (%d):" % len(groups))
        for g in groups:
            print("  - %s   (%s)" % (g["display"], g["category"]))
        print("Member policies in selected groups (%d):" % len(members))
        for m in members:
            print("  - %s   [ref=%s; groups=%s]"
                  % (m["name"], m["ref"], ",".join(m["groups"]) or "-"))
        return

    aid = init["assignment_id"]
    rows = []
    refs_with_components = set()
    for i, s in enumerate(init["subs"], 1):
        sid = s["subscription_id"]
        log("[%d/%d] %s: components for %s..."
            % (i, len(init["subs"]), s["subscription_name"] or sid, init["display"]))
        for comp in fetch_components(session, sid, aid):
            ref = comp.get("policyDefinitionReferenceId") or ""
            if sel_refs and ref not in sel_refs:
                continue
            refs_with_components.add(ref)
            rid = (comp.get("resourceId") or "")
            cl = cluster_by_id.get(rid.lower())
            m = member_by_ref.get(ref) or {}
            rows.append({
                "subscription": s["subscription_name"] or sid,
                "environment": cl["environment"] if cl else "(n/a)",
                "resource": cl["cluster"] if cl else short_id(rid),
                "resource_type": "/".join(rid.lower().split("/providers/")[-1].split("/")[:2])
                                 if "/providers/" in rid else "",
                "component_type": comp.get("componentType") or "",
                "component_name": comp.get("componentName") or short_id(comp.get("componentId")),
                "policy": m.get("name") or definition_info(
                    session, comp.get("policyDefinitionId") or "")["name"],
                "group": next((group_label.get(g, g) for g in (m.get("groups") or [])
                               if g in sel_group_names or not sel_group_names), "(ungrouped)"),
                "reference_id": ref,
                "compliance": comp.get("complianceState") or "NonCompliant",
                "timestamp": comp.get("timestamp") or "",
            })

    # resource-level fallback: only for selected policies that produced NO components
    for i, s in enumerate(init["subs"], 1):
        sid = s["subscription_id"]
        for st in fetch_resource_states(session, sid, aid):
            ref = st.get("policyDefinitionReferenceId") or ""
            if (st.get("policyAssignmentId") or "").lower() != aid.lower():
                continue
            if sel_refs and ref not in sel_refs:
                continue
            if ref in refs_with_components:
                continue  # already covered at component granularity
            rid = st.get("resourceId") or ""
            cl = cluster_by_id.get(rid.lower())
            m = member_by_ref.get(ref) or {}
            rows.append({
                "subscription": s["subscription_name"] or sid,
                "environment": cl["environment"] if cl else "(n/a)",
                "resource": cl["cluster"] if cl else short_id(rid),
                "resource_type": "/".join(rid.lower().split("/providers/")[-1].split("/")[:2])
                                 if "/providers/" in rid else "",
                "component_type": "(resource)",
                "component_name": short_id(rid),
                "policy": m.get("name") or definition_info(
                    session, st.get("policyDefinitionId") or "")["name"],
                "group": next((group_label.get(g, g) for g in (m.get("groups") or [])
                               if g in sel_group_names or not sel_group_names), "(ungrouped)"),
                "reference_id": ref,
                "compliance": st.get("complianceState") or "NonCompliant",
                "timestamp": st.get("timestamp") or "",
            })

    cols = ["subscription", "environment", "resource", "resource_type", "component_type",
            "component_name", "policy", "group", "reference_id", "compliance", "timestamp"]
    detail = pd.DataFrame(rows, columns=cols)
    if not detail.empty:
        detail = detail.sort_values(["subscription", "resource", "policy", "component_name"]) \
            .reset_index(drop=True)

    if detail.empty:
        by_group = pd.DataFrame(columns=["group", "components"])
        by_policy = pd.DataFrame(columns=["policy", "components"])
    else:
        by_group = detail.groupby("group").size().reset_index(name="components") \
            .sort_values("components", ascending=False)
        by_policy = detail.groupby("policy").size().reset_index(name="components") \
            .sort_values("components", ascending=False)

    n_resource = int((detail["component_type"] == "(resource)").sum()) if not detail.empty else 0
    summary = pd.DataFrame([
        ("Compliance initiative", init["display"]),
        ("Initiative definition", init["initiative"]),
        ("Subscriptions in scope", len(sel)),
        ("Subscriptions with this assignment", len(init["subs"])),
        ("Groups selected", "all" if (not groups or len(sel_groups) == len(groups))
         else len(sel_groups)),
        ("Member policies selected", len(sel_policies)),
        ("Non-compliant components", len(detail) - n_resource),
        ("  resource-level fallback rows", n_resource),
        ("Distinct resources affected",
         int(detail["resource"].nunique()) if not detail.empty else 0),
    ], columns=["Item", "Value"])

    selection = pd.DataFrame(
        [("Initiative", init["display"]),
         ("Scope", env_filter or "all")]
        + [("Group", "%s (%s)" % (g["display"], g["category"])) for g in sel_groups]
        + [("Policy", "%s [ref=%s]" % (m["name"], m["ref"])) for m in sel_policies]
        + [("Generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))],
        columns=["Item", "Value"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "Non-Compliant Components: %s" % init["display"], [
        "Generated: %s   Scope: %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                       env_filter or "all"),
        "",
        "Compliance name = the chosen initiative (policy-set) assignment: %s" % init["display"],
        "Groups = the initiative's policyDefinitionGroups (Regulatory-Compliance controls/",
        "compliance domains). Policies = the member definitions in those groups.",
        "",
        "NonCompliantComponents lists every failing COMPONENT - Azure Policy's granular",
        "evaluation record. For resource-provider policies (Kubernetes, Key Vault data",
        "plane, ...) a component is the in-resource object (e.g. a Kubernetes",
        "namespace/kind/name). Policies with no components fall back to the non-compliant",
        "resource itself (component_type = (resource)).",
        "",
        "Assignments are read at subscription scope including ones inherited from",
        "management groups. The subscription set you picked drives the query; the",
        "environment filter only annotates AKS resources (cluster name + environment) and",
        "does NOT drop non-AKS components. Control-plane PolicyInsights data only (no",
        "kubectl); component detail exists only where the Azure Policy add-on reports it.",
    ])
    excel.add_table(wb, "Summary", summary, max_width=70, section="summary")
    excel.add_table(wb, "SummaryByGroup", by_group, int_cols=("components",),
                    colorscale_cols=("components",), section="summary")
    excel.add_table(wb, "SummaryByPolicy", by_policy, int_cols=("components",),
                    colorscale_cols=("components",), section="summary")
    excel.add_table(wb, "NonCompliantComponents", detail, fail_cols=("compliance",),
                    max_width=70)
    excel.add_table(wb, "Selection", selection, max_width=80, section="reference")

    path = excel.save(wb, out_path(args, "aks_policy_components", env_filter))
    log("Report written: %s  (%d component rows)" % (path, len(detail)))


if __name__ == "__main__":
    main()
