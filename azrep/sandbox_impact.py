"""Fleet-wide what-if for a candidate Azure Policy, evaluated from the sandbox.

Stages the candidate as a DoNotEnforce assignment at the sandbox resource group, then runs
every fleet cluster's verbatim ARM body through checkPolicyRestrictions at that scope —
prod is never touched (fleet reads stay Resource Graph only). Output: XLSX evidence pack
of which clusters would be flagged, by environment and subscription.

Caveat: bodies are evaluated as if they lived in the sandbox RG, so policies keyed on the
source resource group name/tags or subscription context will not simulate correctly.
"""
import json
import sys

import pandas as pd

from azrep import arg, excel, fleet
from azrep.http_client import log
from azrep.sandbox import (apply_policies, delete_policy_artifacts, load_policy_definition,
                           rg_scope)
from azrep.subs import env_match, load_subscriptions, out_path

CHECK_API = "2024-10-01"
AKS_BODY_API = "2024-05-01"


def parse_params(pairs):
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            sys.exit("--params expects k=v, got: %s" % pair)
        k, v = pair.split("=", 1)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def override_effect(payload):
    """Force the staged copy's effect to Deny so deny-path evaluation (fieldRestrictions)
    is guaranteed; the assignment stays DoNotEnforce so nothing can actually block."""
    props = payload.get("properties") or {}
    effect = (props.get("parameters") or {}).get("effect")
    if effect is not None:
        values = effect.get("allowedValues")
        deny = "Deny"
        if values:
            deny = next((v for v in values if str(v).lower() == "deny"), None)
            if deny is None:
                deny = "Deny"
                effect["allowedValues"] = list(values) + [deny]
        effect["defaultValue"] = deny
    else:
        (props.get("policyRule") or {}).setdefault("then", {})["effect"] = "Deny"
    return payload


def staged_policies(cfg, args):
    payload = load_policy_definition(cfg, {"file": args.policy})
    if args.effect_override:
        payload = override_effect(payload)
    return {
        "definitions": [{"name": args.name, "definition": payload["properties"]}],
        "assignments": [{
            "name": args.name,
            "display_name": "Impact candidate: %s" % args.name,
            "definition_id": "custom:%s" % args.name,
            "scope": "resource_group",
            "enforcement_mode": "DoNotEnforce",
            "parameters": parse_params(args.params),
            "metadata": {"source": "aks-reporting-sandbox-impact"},
        }],
    }


def select_subs(args):
    sel = load_subscriptions(args.csv)
    if args.subs:
        wanted = [w.strip().lower() for w in args.subs.split(",") if w.strip()]
        sel = [s for s in sel if s["subscription_id"].lower() in wanted
               or any(w in s["subscription_name"].lower() for w in wanted)]
        if not sel:
            sys.exit("--subs matched no subscriptions in %s" % args.csv)
    elif not (args.all or args.env or args.nonprod):
        sys.exit("Pick a fleet scope: --all, --subs, --env or --nonprod.")
    return sel


def fleet_bodies(session, args):
    """(raw_rows, labels) - raw ARM bodies plus environment/subscription labels by id."""
    sel = select_subs(args)
    env_filter = args.env or ("nonprod" if args.nonprod else None)
    clusters, _pools = fleet.load_fleet(session, sel, env_filter)
    labels = {c["id"].lower(): c for c in clusters}
    raw = arg.query(session, arg.CLUSTERS_RAW_KQL, [s["subscription_id"] for s in sel])
    raw = [r for r in raw if r["id"].lower() in labels]
    return raw, labels, env_filter


def check_one(session, cfg, row):
    content = {k: v for k, v in (
        ("type", row.get("type") or "Microsoft.ContainerService/managedClusters"),
        ("name", row.get("name")),
        ("location", row.get("location")),
        ("tags", row.get("tags")),
        ("identity", row.get("identity")),
        ("sku", row.get("sku")),
        ("properties", row.get("properties")),
    ) if v}
    payload = {
        "resourceDetails": {"resourceContent": content, "apiVersion": AKS_BODY_API},
        "includeAuditEffect": True,
    }
    url = "%s/providers/Microsoft.PolicyInsights/checkPolicyRestrictions" % rg_scope(cfg)
    return session.post(url, params={"api-version": CHECK_API}, payload=payload)


def candidate_hits(resp, assignment_id):
    """Evaluation entries for OUR assignment only (the sandbox may have other policies)."""
    want = assignment_id.lower()
    hits = []
    evals = ((resp or {}).get("contentEvaluationResult") or {}).get("policyEvaluations") or []
    for e in evals:
        info = e.get("policyInfo") or {}
        if (info.get("policyAssignmentId") or "").lower() == want:
            hits.append(e)
    for fr in (resp or {}).get("fieldRestrictions") or []:
        for r in fr.get("restrictions") or []:
            info = (r.get("policy") or {}).get("policyInfo") or r.get("policy") or {}
            if (info.get("policyAssignmentId") or "").lower() == want:
                hits.append({"policyInfo": info, "evaluationResult": "FieldRestriction",
                             "field": fr.get("field"), "result": r.get("result")})
    return hits


def hit_detail(hits):
    parts = []
    for h in hits:
        if h.get("field"):
            parts.append("%s: %s" % (h.get("field"), h.get("result")))
            continue
        exprs = ((h.get("evaluationDetails") or {}).get("evaluatedExpressions")) or []
        for ex in exprs[:4]:
            parts.append("%s %s (value: %s)" % (ex.get("path") or ex.get("expression") or "?",
                                                ex.get("operator") or "",
                                                json.dumps(ex.get("expressionValue"))[:80]))
    return "; ".join(parts)[:500]


def summarize(df, by):
    g = df.groupby(by, dropna=False)
    out = g.agg(clusters=("result", "size"),
                non_compliant=("result", lambda s: int((s == "NonCompliant").sum())))
    out = out.reset_index()
    out["pct_non_compliant"] = out["non_compliant"] / out["clusters"]
    return out.sort_values("non_compliant", ascending=False)


def write_report(rows, raw_rows, args, env_filter, assignment_id):
    df = pd.DataFrame(rows)
    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS policy impact simulation", [
        "Candidate policy: %s (staged DoNotEnforce as %s)" % (args.policy, assignment_id),
        "Every fleet cluster's ARM body was evaluated via checkPolicyRestrictions at the "
        "sandbox resource group scope; production was not modified.",
        "NonCompliant = the candidate would flag/deny the cluster as currently configured.",
        "Caveat: evaluation happens as if the cluster lived in the sandbox resource group, "
        "so rules on resource group name/tags or subscription context do not simulate.",
        "Scope: %s | clusters evaluated: %d" % (env_filter or "all", len(df)),
    ])
    total = len(df)
    bad = int((df["result"] == "NonCompliant").sum())
    summary = pd.DataFrame([
        ("clusters evaluated", total),
        ("would be non-compliant", bad),
        ("% non-compliant", (bad / total) if total else 0),
    ], columns=["metric", "value"])
    excel.add_table(wb, "Summary", summary, section="summary", pct_cols=())
    excel.add_table(wb, "SummaryByEnv", summarize(df, "environment"), section="summary",
                    int_cols=("clusters", "non_compliant"), pct_cols=("pct_non_compliant",))
    excel.add_table(wb, "SummaryBySubscription", summarize(df, "subscription"),
                    section="summary", int_cols=("clusters", "non_compliant"),
                    pct_cols=("pct_non_compliant",))
    detail = df.sort_values(["result", "environment", "cluster"],
                            ascending=[True, True, True])
    excel.add_table(wb, "PerCluster", detail, fail_cols=("result",))
    raw_eval = [{"id": r["id"], "evaluations": json.dumps(r.get("_resp"))[:2000]}
                for r in raw_rows if r.get("_resp")]
    if raw_eval:
        excel.add_table(wb, "RawEvaluations", pd.DataFrame(raw_eval), section="reference")
    path = out_path(args, "aks_policy_impact", env_filter)
    excel.save(wb, path)
    log("Wrote %s" % path)
    return path


def run(session, cfg, args):
    staged = staged_policies(cfg, args)
    cfg_staged = dict(cfg)
    cfg_staged["policies"] = staged
    assignment_id = "%s/providers/Microsoft.Authorization/policyAssignments/%s" % (
        rg_scope(cfg), args.name)

    raw, labels, env_filter = fleet_bodies(session, args)
    if not raw:
        sys.exit("No fleet clusters in scope.")
    log("Evaluating candidate policy against %d cluster(s)..." % len(raw))

    apply_policies(session, cfg_staged)
    try:
        rows = []
        for i, r in enumerate(raw, 1):
            resp = check_one(session, cfg, r)
            hits = candidate_hits(resp, assignment_id)
            label = labels[r["id"].lower()]
            rows.append({
                "cluster": label["cluster"],
                "subscription": label["subscription"],
                "environment": label["environment"],
                "location": label["location"],
                "kubernetes_version": label["kubernetes_version"],
                "result": "NonCompliant" if hits else "Compliant",
                "matched": hit_detail(hits),
            })
            if hits:
                r["_resp"] = candidate_hits(resp, assignment_id)
            if i % 50 == 0 or i == len(raw):
                log("  %d/%d evaluated (%d non-compliant so far)"
                    % (i, len(raw), sum(1 for x in rows if x["result"] == "NonCompliant")))
    finally:
        if args.keep_assignment:
            log("Keeping staged definition/assignment `%s` (--keep-assignment)." % args.name)
        else:
            delete_policy_artifacts(session, cfg_staged)

    bad = sum(1 for x in rows if x["result"] == "NonCompliant")
    log("Impact: %d/%d clusters would be non-compliant." % (bad, len(rows)))
    if bad == 0 and not args.effect_override:
        log("NOTE: zero hits can also mean the audit-effect candidate did not register in "
            "checkPolicyRestrictions for this tenant; retry with --effect-override to be sure.")
    return write_report(rows, raw, args, env_filter, assignment_id)
