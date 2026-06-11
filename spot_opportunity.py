"""Spot opportunity report: where is spot already used, and which user node pools
are candidates to move to spot - with an estimated saving from the public Azure
Retail Prices API (no auth required).

Tabs: ReadMe, SpotToday, Candidates, PriceReference, Summary.

Run it with --nonprod or --env dev: spot is normally only sensible outside prod.

Usage: python spot_opportunity.py --nonprod
"""
import datetime as dt

import pandas as pd

from azrep import excel
from azrep.armextras import retail_vm_prices
from azrep.fleet import load_fleet
from azrep.http_client import connect, log
from azrep.subs import base_parser, load_subscriptions, out_path, pick_scope

HOURS_PER_MONTH = 730


def main(argv=None):
    p = base_parser("AKS spot usage & opportunity")
    p.add_argument("--currency", default="USD")
    args = p.parse_args(argv)

    subs = load_subscriptions(args.csv)
    sel, env_filter = pick_scope(subs, args)
    if env_filter is None:
        print("NOTE: no environment filter chosen - prod pools will appear as candidates.")
        print("      Spot nodes can be evicted at any time; usually only move non-prod.")
    session = connect()
    env_keys = [k.strip() for k in args.env_tag_keys.split(",") if k.strip()]
    clusters, pools = load_fleet(session, sel, env_filter, args.include_unknown_env, env_keys)
    if not pools:
        log("No clusters in scope.")
        return

    spot_now = [q for q in pools if q["priority"].lower() == "spot"]
    candidates = [q for q in pools
                  if q["priority"].lower() != "spot"
                  and q["mode"].lower() == "user"
                  and q["os_type"].lower() != "windows"
                  and q["count"] > 0
                  and q["power_state"].lower() != "stopped"]

    need = sorted({(q["location"], q["vm_size"]) for q in candidates + spot_now if q["vm_size"]})
    log("Looking up retail prices for %d (region, size) combos..." % len(need))
    prices = {}
    for i, (region, size) in enumerate(need, 1):
        if i % 20 == 0:
            log("  %d/%d..." % (i, len(need)))
        prices[(region, size)] = retail_vm_prices(region, size, args.currency)

    cand_rows = []
    for q in candidates:
        pr = prices.get((q["location"], q["vm_size"])) or {}
        cand_rows.append({
            "cluster": q["cluster"], "subscription": q["subscription"],
            "environment": q["environment"], "location": q["location"],
            "pool": q["pool"], "vm_size": q["vm_size"], "nodes": q["count"],
            "autoscaling": q["autoscaling"], "taints": q["taints"],
            "od_hr": pr.get("od_hr"), "spot_hr": pr.get("spot_hr"),
        })
    cand = pd.DataFrame(cand_rows)
    if not cand.empty:
        n = len(cand)
        # nodes=G, od_hr=J, spot_hr=K -> formulas keep the sheet dynamic
        cand["Spot discount %"] = ["=IF(OR(J%d=\"\",K%d=\"\",J%d=0),\"\",1-K%d/J%d)"
                                   % (r, r, r, r, r) for r in range(2, n + 2)]
        cand["Est monthly OD cost"] = ["=IF(J%d=\"\",\"\",G%d*%d*J%d)"
                                       % (r, r, HOURS_PER_MONTH, r) for r in range(2, n + 2)]
        cand["Est monthly saving"] = ["=IF(OR(J%d=\"\",K%d=\"\"),\"\",G%d*%d*(J%d-K%d))"
                                      % (r, r, r, HOURS_PER_MONTH, r, r) for r in range(2, n + 2)]

    spot_rows = [{
        "cluster": q["cluster"], "subscription": q["subscription"],
        "environment": q["environment"], "location": q["location"], "pool": q["pool"],
        "vm_size": q["vm_size"], "nodes": q["count"],
        "spot_max_price": ("-1 (pay up to on-demand)" if q["spot_max_price"] in (-1, "-1")
                           else q["spot_max_price"]),
        "eviction_policy": q["eviction_policy"],
        "autoscaling": q["autoscaling"],
        "min_count": q["min_count"], "max_count": q["max_count"],
    } for q in spot_now]
    spotdf = pd.DataFrame(spot_rows) if spot_rows else pd.DataFrame(
        columns=["cluster", "subscription", "environment", "location", "pool",
                 "vm_size", "nodes", "spot_max_price", "eviction_policy",
                 "autoscaling", "min_count", "max_count"])

    pr_rows = [{"region": r, "vm_size": s,
                "od_hr": (prices.get((r, s)) or {}).get("od_hr"),
                "spot_hr": (prices.get((r, s)) or {}).get("spot_hr")}
               for (r, s) in need]
    prdf = pd.DataFrame(pr_rows)
    if not prdf.empty:
        n = len(prdf)
        prdf["discount %"] = ["=IF(OR(C%d=\"\",D%d=\"\",C%d=0),\"\",1-D%d/C%d)"
                              % (r, r, r, r, r) for r in range(2, n + 2)]

    cl_total = len({q["cluster"] for q in pools})
    cl_with_spot = len({q["cluster"] for q in spot_now})
    summary = pd.DataFrame([
        ("Clusters in scope", cl_total),
        ("Clusters already using spot", cl_with_spot),
        ("Clusters with zero spot pools", cl_total - cl_with_spot),
        ("Existing spot pools / nodes", "%d / %d" % (len(spot_now),
                                                     sum(q["count"] for q in spot_now))),
        ("Candidate user pools (non-spot)", len(cand)),
        ("Candidate nodes", int(cand["nodes"].sum()) if not cand.empty else 0),
    ], columns=["Item", "Value"])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Spot Opportunity", [
        "Generated: %s   Scope: %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                       env_filter or "all (careful: includes prod)"),
        "",
        "Candidates = user-mode, Linux, non-spot pools with running nodes. System pools",
        "cannot be spot. Estimated savings use PUBLIC retail prices (prices.azure.com):",
        "  - your EA/MCA negotiated rates and RI/savings-plan coverage are NOT reflected;",
        "  - spot prices float with capacity and vary by region/size;",
        "  - spot nodes can be evicted at any time - workloads must tolerate disruption.",
        "Treat 'Est monthly saving' as an upper-bound screening number, then validate the",
        "top candidates against actual amortized cost (fleet_cost.py / cluster_deepdive.py).",
    ])
    excel.add_table(wb, "SpotToday", spotdf, int_cols=("nodes", "min_count", "max_count"))
    excel.add_table(wb, "Candidates", cand,
                    money_cols=("od_hr", "spot_hr", "Est monthly OD cost", "Est monthly saving"),
                    formats={"od_hr": "#,##0.0000", "spot_hr": "#,##0.0000"},
                    pct_cols=("Spot discount %",), int_cols=("nodes",),
                    colorscale_cols=("Spot discount %",))
    excel.add_table(wb, "PriceReference", prdf,
                    formats={"od_hr": "#,##0.0000", "spot_hr": "#,##0.0000"},
                    pct_cols=("discount %",))
    excel.add_table(wb, "Summary", summary, max_width=60)

    path = excel.save(wb, out_path(args, "aks_spot_opportunity", env_filter))
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
