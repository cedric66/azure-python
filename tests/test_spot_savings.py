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


def main():
    test_counterfactual_saving_even_when_total_grows()
    test_price_missing_does_not_claim_savings()
    test_no_spot_cost()
    print("\nALL SPOT-SAVINGS TESTS PASSED")


if __name__ == "__main__":
    main()
