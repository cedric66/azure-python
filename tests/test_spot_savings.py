"""Unit tests for spot_savings.py verdict and window math.

  uv run python tests/test_spot_savings.py
"""
import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spot_savings


CLUSTER = {
    "id": "cluster-1",
    "cluster": "aks-dev-01",
    "subscription": "contoso-platform",
    "environment": "dev",
    "location": "eastus",
}
POOLS = [{
    "cluster_id": "cluster-1",
    "pool": "spt",
    "priority": "Spot",
    "count": 2,
}]
FULL_POOLS = [
    {
        "cluster_id": "cluster-1",
        "cluster": "aks-dev-01",
        "subscription": "contoso-platform",
        "environment": "dev",
        "location": "eastus",
        "pool": "wrk",
        "mode": "User",
        "priority": "Regular",
        "count": 4,
        "vm_size": "Standard_D4s_v5",
        "os_type": "Linux",
    },
    {
        "cluster_id": "cluster-1",
        "cluster": "aks-dev-01",
        "subscription": "contoso-platform",
        "environment": "dev",
        "location": "eastus",
        "pool": "spt",
        "mode": "User",
        "priority": "Spot",
        "count": 2,
        "vm_size": "Standard_D4s_v5",
        "os_type": "Linux",
    },
]
PRICES = {("eastus", "Standard_D4s_v5"): {"od_hr": 0.40, "spot_hr": 0.10}}


def expect(cond, msg):
    assert cond, msg


def daily_rows(start, days, spot_start, total_growth=False):
    rows = []
    start_d = dt.date.fromisoformat(start)
    for i in range(days):
        day = start_d + dt.timedelta(days=i)
        has_spot = i >= spot_start
        ondemand = 100.0 if not has_spot else (82.0 if not total_growth else 120.0)
        spot = 0.0 if not has_spot else 10.0
        fee = 5.0
        total = ondemand + spot + fee
        rows.append({
            "cluster_id": CLUSTER["id"],
            "cluster": CLUSTER["cluster"],
            "subscription": CLUSTER["subscription"],
            "environment": CLUSTER["environment"],
            "location": CLUSTER["location"],
            "Date": day.isoformat(),
            "OnDemand": ondemand,
            "Spot": spot,
            "Reservation": 0.0,
            "SavingsPlan": 0.0,
            "Cluster fee": fee,
            "Compute total (USD)": ondemand + spot,
            "Total (USD)": total,
            "Spot %": spot / (ondemand + spot) if (ondemand + spot) else 0.0,
        })
    return pd.DataFrame(rows)


def estimate_rows(start, days, spot_start):
    rows = []
    start_d = dt.date.fromisoformat(start)
    for i in range(spot_start, days):
        day = start_d + dt.timedelta(days=i)
        rows.append({
            "cluster_id": CLUSTER["id"],
            "cluster": CLUSTER["cluster"],
            "subscription": CLUSTER["subscription"],
            "environment": CLUSTER["environment"],
            "location": CLUSTER["location"],
            "Date": day.isoformat(),
            "pool": "spt",
            "vm_size": "Standard_D4s_v5",
            "ResourceId": "vmss",
            "spot_hr": 0.10,
            "od_hr": 0.40,
            "actual_spot_cost": 10.0,
            "estimated_spot_node_hours": 100.0,
            "od_counterfactual": 40.0,
            "estimated_spot_saving": 30.0,
            "price_status": "priced",
        })
    return pd.DataFrame(rows)


def raw_resource_rows(start, days, spot_start):
    rows = []
    start_d = dt.date.fromisoformat(start)
    for i in range(days):
        day = (start_d + dt.timedelta(days=i)).isoformat()
        rows.append({
            "cluster_id": CLUSTER["id"],
            "cluster": CLUSTER["cluster"],
            "subscription": CLUSTER["subscription"],
            "environment": CLUSTER["environment"],
            "location": CLUSTER["location"],
            "Date": day,
            "ResourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachineScaleSets/aks-wrk-11111111-vmss",
            "PricingModel": "OnDemand",
            "CostUSD": 100.0 if i < spot_start else 120.0,
        })
        if i >= spot_start:
            rows.append({
                "cluster_id": CLUSTER["id"],
                "cluster": CLUSTER["cluster"],
                "subscription": CLUSTER["subscription"],
                "environment": CLUSTER["environment"],
                "location": CLUSTER["location"],
                "Date": day,
                "ResourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachineScaleSets/aks-spt-11111111-vmss",
                "PricingModel": "Spot",
                "CostUSD": 10.0,
            })
    return pd.DataFrame(rows)


def test_counterfactual_saving_even_when_total_grows():
    daily = daily_rows("2026-05-01", 60, 30, total_growth=True)
    estimates = estimate_rows("2026-05-01", 60, 30)
    annotated, summary = spot_savings.annotate_daily_and_summary(
        daily, estimates, [CLUSTER], POOLS, trend_days=30, baseline_days=30)

    row = summary.iloc[0]
    expect(row["verdict"] == "SAVING",
           "counterfactual should drive verdict even when total cost grows: %s" %
           row.to_dict())
    expect(row["last_30_estimated_spot_saving"] == 900.0,
           "30 days * $30/day expected")
    expect(row["total_delta_vs_pre_per_day"] > 0,
           "total cost context should still show growth")
    post = annotated[annotated["phase"] == "after_spot"]
    expect(post["cumulative_estimated_spot_saving"].iloc[-1] == 900.0,
           "daily cumulative saving should reach $900")


def test_before_after_projection_tables_and_chart():
    daily = daily_rows("2026-05-01", 60, 30, total_growth=True)
    estimates = estimate_rows("2026-05-01", 60, 30)
    annotated, summary = spot_savings.annotate_daily_and_summary(
        daily, estimates, [CLUSTER], FULL_POOLS, trend_days=30, baseline_days=30)
    raw = raw_resource_rows("2026-05-01", 60, 30)
    vmss = spot_savings.enrich_vmss_cost(raw, FULL_POOLS, PRICES)
    windows = spot_savings.spot_windows(annotated, 30, 30, spot_savings.SPOT_THRESHOLD_USD)
    before = spot_savings.period_cost_table(
        annotated, vmss, [CLUSTER], FULL_POOLS, windows, "before")
    after = spot_savings.period_cost_table(
        annotated, vmss, [CLUSTER], FULL_POOLS, windows, "after")
    projection = spot_savings.savings_projection_table(
        summary, annotated, [CLUSTER], FULL_POOLS, PRICES, windows, 30, 30, 30, None)

    expect("OnDemand" in set(before["billing_type"]),
           "BeforeSpot table should show on-demand billing rows")
    expect("Spot" in set(after["billing_type"]),
           "AfterSpot table should show spot billing rows")
    expect(float(projection.iloc[0]["projected_monthly_saving_usd"]) > 0,
           "projection should model a positive retail saving for OD->spot move")
    expect(projection.iloc[0]["project_move_to_spot_nodes"] == 4,
           "default projection should model all priced regular user OD nodes")

    chart_data = spot_savings.projection_chart_rows(annotated, projection, 30)
    from azrep import excel
    wb = excel.new_workbook()
    ws = excel.add_table(wb, "ActualVsProjection", chart_data)
    ch = spot_savings.add_actual_projection_chart(ws, chart_data)
    expect(ch.series[1].graphicalProperties.line.dashStyle == "dash",
           "counterfactual/projection chart series should be dashed")


def test_price_missing_does_not_claim_savings():
    daily = daily_rows("2026-05-01", 60, 30)
    estimates = estimate_rows("2026-05-01", 60, 30)
    estimates["od_counterfactual"] = None
    estimates["estimated_spot_saving"] = None
    estimates["price_status"] = "price_missing"
    _annotated, summary = spot_savings.annotate_daily_and_summary(
        daily, estimates, [CLUSTER], POOLS, trend_days=30, baseline_days=30)
    expect(summary.iloc[0]["verdict"] == "PRICE_MISSING",
           "missing retail prices must not produce a savings verdict")


def test_no_spot_cost():
    daily = daily_rows("2026-05-01", 30, 99)
    estimates = pd.DataFrame()
    _annotated, summary = spot_savings.annotate_daily_and_summary(
        daily, estimates, [CLUSTER], POOLS, trend_days=30, baseline_days=30)
    expect(summary.iloc[0]["verdict"] == "NO_SPOT_COST",
           "clusters with no spot cost should be explicit")


def test_coverage_risk_recommendations_and_realized():
    daily = daily_rows("2026-05-01", 60, 30)
    estimates = estimate_rows("2026-05-01", 60, 30)
    _annotated, summary = spot_savings.annotate_daily_and_summary(
        daily, estimates, [CLUSTER], FULL_POOLS, trend_days=30, baseline_days=30)

    churn = {CLUSTER["id"]: {"count": 3, "last_event_ts": "2026-06-10T03:00:00Z",
                             "rows": []}}
    cov = spot_savings.coverage_risk_rows([CLUSTER], FULL_POOLS, daily, churn, 30)
    crow = cov.iloc[0]
    expect(crow["spot_nodes"] == 2 and crow["total_nodes"] == 6,
           "coverage risk should count spot vs total nodes: %s" % crow.to_dict())
    expect(crow["vmss_churn_approx"] == 3,
           "coverage risk should carry the eviction-proxy churn count")
    expect(crow["risk_band"] in ("LOW", "MED", "HIGH"),
           "a cluster with spot exposure should get a risk band: %s" % crow["risk_band"])

    risk_by = dict(zip(cov["cluster_id"], cov["risk_band"]))
    rec = spot_savings.recommendation_rows([CLUSTER], FULL_POOLS, PRICES, risk_by, 30)
    expect(not rec.empty, "should recommend moving the regular user OD pool to spot")
    top = rec.iloc[0]
    expect(top["rank"] == 1 and top["current_od_nodes"] == 4,
           "top recommendation should size the 4-node OD pool: %s" % top.to_dict())
    want = 4 * (0.40 - 0.10) * 24 * spot_savings.DAYS_PER_MONTH
    expect(abs(float(top["est_monthly_saving_usd"]) - want) < 1e-6,
           "monthly saving math should be nodes*(od-spot)*24*30.4375: %s" %
           top["est_monthly_saving_usd"])
    expect(str(top["verify_before_move"]).strip() != "",
           "every recommendation must carry the workload-suitability caveat")

    realized = spot_savings.realized_savings_rows(summary, 30)
    rrow = realized.iloc[0]
    expect(rrow["invoiced_spot_fact_usd"] == summary.iloc[0]["last_30_actual_spot_cost"],
           "realized savings fact column should equal invoiced (billed) spot cost")
    expect(rrow["status"] == "Verified saving",
           "a priced net saving should map to the Verified saving badge: %s" % rrow["status"])


def test_vmss_churn_events_keeps_compute_drops_containerservice():
    events = [
        {"eventTimestamp": "2026-06-10T03:00:00Z",
         "operationName": {"value": "Microsoft.Compute/virtualMachineScaleSets/delete"},
         "status": {"value": "Succeeded"},
         "resourceId": "/subscriptions/s/resourceGroups/MC_x/providers/"
                       "Microsoft.Compute/virtualMachineScaleSets/aks-spt-1-vmss"},
        {"eventTimestamp": "2026-06-09T03:00:00Z",
         "operationName": {"value": "Microsoft.Compute/virtualMachineScaleSets/write"},
         "status": {"value": "Succeeded"}, "resourceId": "x"},
        {"eventTimestamp": "2026-06-08T03:00:00Z",
         "operationName": {"value": "Microsoft.ContainerService/managedClusters/write"},
         "status": {"value": "Succeeded"}, "resourceId": "y"},
    ]

    class FakeSession:
        def get_paged(self, url, params=None):
            return events

    from azrep import armextras
    out = armextras.vmss_churn_events(FakeSession(), "sub", "MC_x", days=30)
    expect(out["count"] == 1,
           "only VMSS delete/deallocate count as churn (write/ContainerService dropped): %s" % out)
    expect(out["last_event_ts"] == "2026-06-10T03:00:00Z",
           "last_event_ts should be the most recent kept event")


def test_monthly_savings_three_month_rollup_and_flag():
    # April carries no spot; adoption starts 2026-05-01, so April -> "No", May/June -> "Yes".
    daily = daily_rows("2026-04-01", 88, 30)            # 2026-04-01 .. 2026-06-27
    estimates = estimate_rows("2026-04-01", 88, 30)
    annotated, _summary = spot_savings.annotate_daily_and_summary(
        daily, estimates, [CLUSTER], FULL_POOLS, trend_days=30, baseline_days=30)

    monthly = spot_savings.monthly_savings_rows(annotated, dt.date(2026, 6, 27))
    expect(set(monthly["month"]) == {"2026-04", "2026-05", "2026-06"},
           "monthly roll-up should cover the last 3 calendar months: %s" %
           sorted(set(monthly["month"])))
    expect("(all clusters)" in set(monthly["cluster"]),
           "monthly roll-up should include a fleet-total row")

    per = monthly[monthly["cluster"] == "aks-dev-01"].set_index("month")
    apr, may, jun = per.loc["2026-04"], per.loc["2026-05"], per.loc["2026-06"]
    expect(apr["savings_from_spot_pool"] == "No (no spot spend)",
           "a month with no spot spend must flag the saving as not spot-attributable")
    expect(float(apr["estimated_saving_usd"]) == 0.0 and pd.isna(apr["savings_rate_pct"]),
           "a no-spot month should show zero saving and a blank savings rate")
    expect(may["savings_from_spot_pool"] == "Yes" and may["month_status"] == "full",
           "a completed month with spot spend should be Yes + full")
    expect(abs(float(may["spot_cost_usd"]) - 310.0) < 1e-6,
           "May spot fact = 31 days * $10: %s" % may["spot_cost_usd"])
    expect(abs(float(may["estimated_saving_usd"]) - 930.0) < 1e-6,
           "May saving = 31 days * $30: %s" % may["estimated_saving_usd"])
    expect(jun["month_status"] == "MTD (partial)",
           "the current calendar month should be marked month-to-date (partial)")


RID_BASE = ("/subscriptions/s/resourceGroups/MC_rg/providers/"
            "Microsoft.Compute/virtualMachineScaleSets/")


def _res_row(vmss, pm, cost, qty, day):
    return {
        "cluster_id": CLUSTER["id"], "cluster": CLUSTER["cluster"],
        "subscription": CLUSTER["subscription"], "environment": CLUSTER["environment"],
        "location": CLUSTER["location"], "Date": day, "ResourceId": RID_BASE + vmss,
        "PricingModel": pm, "CostUSD": float(cost), "UsageQuantity": float(qty),
    }


def actual_res_rows(start, days, spot_start, od_hr=0.40, spot_hr=0.10,
                    od_nodes=4, spot_nodes=2, with_od=True):
    """res-shaped daily rows with billed node-hours: an OnDemand wrk pool (when
    with_od) every day and a Spot spt pool from spot_start, at the given rates."""
    rows, start_d = [], dt.date.fromisoformat(start)
    for i in range(days):
        day = (start_d + dt.timedelta(days=i)).isoformat()
        if with_od:
            oh = od_nodes * 24
            rows.append(_res_row("aks-wrk-11111111-vmss", "OnDemand", oh * od_hr, oh, day))
        if i >= spot_start:
            sh = spot_nodes * 24
            rows.append(_res_row("aks-spt-11111111-vmss", "Spot", sh * spot_hr, sh, day))
    return pd.DataFrame(rows)


def test_actual_amortized_rate_ladder():
    max_date = dt.date(2026, 5, 1) + dt.timedelta(days=59)
    # 1. pre-spot history: 30 OD days before the first spot day -> actual baseline rate
    res = actual_res_rows("2026-05-01", 60, 30)
    fs = {CLUSTER["id"]: dt.date(2026, 5, 31)}
    est = spot_savings.build_spot_estimates_actual(
        res, FULL_POOLS, {}, fs, baseline_days=30, trend_days=30, max_date=max_date)
    expect(not est.empty, "actual estimator should emit priced spot rows")
    expect((est["od_hr_source"] == "pre_spot_history").all(),
           "pre-spot OD history should drive the rate: %s" % set(est["od_hr_source"]))
    expect(abs(float(est.iloc[0]["od_hr"]) - 0.40) < 1e-9,
           "actual OD rate should be cost/node-hours = 0.40: %s" % est.iloc[0]["od_hr"])
    expect(abs(float(est.iloc[0]["estimated_spot_saving"]) - (48 * (0.40 - 0.10))) < 1e-6,
           "saving = spot node-hours * (od_hr - spot_hr): %s" % est.iloc[0]["estimated_spot_saving"])

    # 2. no pre-spot window -> fall back to the concurrent OD/RI pool's actual rate
    res2 = actual_res_rows("2026-05-01", 60, 0)
    fs2 = {CLUSTER["id"]: dt.date(2026, 5, 1)}
    est2 = spot_savings.build_spot_estimates_actual(
        res2, FULL_POOLS, {}, fs2, baseline_days=30, trend_days=30, max_date=max_date)
    expect((est2["od_hr_source"] == "concurrent_od_pool").all(),
           "with no pre-spot history, the current OD/RI pool rate should be used: %s"
           % set(est2["od_hr_source"]))

    # 3. no OD/RI data at all -> retail list price is the last-resort fallback
    res3 = actual_res_rows("2026-05-01", 60, 30, with_od=False)
    est3 = spot_savings.build_spot_estimates_actual(
        res3, FULL_POOLS, PRICES, fs, baseline_days=30, trend_days=30, max_date=max_date)
    expect((est3["od_hr_source"] == "retail_fallback").all(),
           "with no actual OD data, retail price should be the fallback: %s"
           % set(est3["od_hr_source"]))
    # and without retail prices either, it must not claim a saving
    est4 = spot_savings.build_spot_estimates_actual(
        res3, FULL_POOLS, {}, fs, baseline_days=30, trend_days=30, max_date=max_date)
    expect((est4["price_status"] == "price_missing").all(),
           "no actual rate and no retail price must be price_missing: %s"
           % set(est4["price_status"]))


def test_before_after_by_env_rows():
    daily = daily_rows("2026-05-01", 60, 30, total_growth=True)
    estimates = estimate_rows("2026-05-01", 60, 30)
    annotated, _summary = spot_savings.annotate_daily_and_summary(
        daily, estimates, [CLUSTER], FULL_POOLS, trend_days=30, baseline_days=30)
    ba = spot_savings.before_after_rows(annotated, [CLUSTER], FULL_POOLS, 30)
    expect(len(ba) == 1, "one environment row expected: %s" % ba.to_dict("records"))
    row = ba.iloc[0]
    expect(row["environment"] == "dev", "row should roll up to the dev environment")
    expect(row["spot_clusters"] == 1 and row["clusters"] == 1,
           "dev should count one cluster, one with a spot pool")
    expect(row["monthly_on_demand_cost_usd"] > row["monthly_actual_cost_usd"],
           "before (all-on-demand) should exceed after (with spot)")
    f = spot_savings.DAYS_PER_MONTH / 30.0
    expect(abs(float(row["monthly_saving_usd"]) - 900.0 * f) < 1e-6,
           "monthly saving should be the counterfactual delta run-rated: %s" %
           row["monthly_saving_usd"])
    expect(abs(float(row["saving_pct"]) - 900.0 / 4950.0) < 1e-9,
           "saving %% should be saving/before: %s" % row["saving_pct"])
    expect(row["status"] == "Verified saving",
           "a priced positive saving should read Verified saving: %s" % row["status"])


def main():
    test_counterfactual_saving_even_when_total_grows()
    test_before_after_projection_tables_and_chart()
    test_price_missing_does_not_claim_savings()
    test_no_spot_cost()
    test_coverage_risk_recommendations_and_realized()
    test_vmss_churn_events_keeps_compute_drops_containerservice()
    test_monthly_savings_three_month_rollup_and_flag()
    test_actual_amortized_rate_ladder()
    test_before_after_by_env_rows()
    print("\nALL SPOT-SAVINGS TESTS PASSED")


if __name__ == "__main__":
    main()
