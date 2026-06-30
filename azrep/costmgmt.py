"""Cost Management Query API client.

Rate limiting: this API is QPU-throttled per tenant (12 QPU/10s, 60/min, 600/hr;
roughly 1 QPU per month of data per query). The client paces calls client-side,
watches the qpu-remaining header, and AzureSession already honors the
x-ms-ratelimit-microsoft.costmanagement-*-retry-after headers on 429.
"""
import datetime as dt
import time

import pandas as pd

from .http_client import AzureApiError, log

API_VERSION = "2023-03-01"


def dim_in(name, values):
    return {"dimensions": {"name": name, "operator": "In", "values": list(values)}}


def f_and(*filters):
    fs = [f for f in filters if f]
    if not fs:
        return None
    return fs[0] if len(fs) == 1 else {"and": fs}


def default_window(months=3):
    """First day of the month `months` full months back, through today (MTD included)."""
    today = dt.date.today()
    y, m = today.year, today.month - months
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, 1), today


class CostClient:
    def __init__(self, session, min_interval=1.5):
        self.s = session
        self.min_interval = min_interval
        self._last = 0.0
        self.calls = 0

    def _pace(self):
        wait = self._last + self.min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        # If the tenant QPU budget is nearly exhausted, slow down before we hit 429.
        rem = self.s.last_headers.get("x-ms-ratelimit-microsoft.costmanagement-qpu-remaining", "")
        try:
            if rem and min(float(x) for x in rem.split(",")) < 6:
                log("  Cost Management QPU nearly exhausted, pausing 15s...")
                time.sleep(15)
        except ValueError:
            pass
        self._last = time.time()

    def query(self, scope, metric="AmortizedCost", granularity="Monthly",
              group_by=(), filt=None, date_from=None, date_to=None, _with_usd=True,
              with_quantity=False):
        """Returns a DataFrame with Cost, CostUSD, Period and one column per grouping.
        with_quantity adds a UsageQuantity column (billed usage units, e.g. node-hours
        for VM/VMSS compute meters) so callers can derive an actual effective rate.
        Because the API caps aggregation at 2 items, with_quantity swaps out the
        separate CostUSD aggregation; CostUSD is then mirrored from Cost (billing
        currency), which matches the existing no-USD fallback behavior."""
        self._pace()
        self.calls += 1
        ds = {"granularity": granularity,
              "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}}
        # Cost Management allows at most 2 aggregation items. Normally that's
        # Cost + CostUSD; when the caller also needs UsageQuantity we drop the
        # separate CostUSD aggregation (it's backfilled from Cost below) so the
        # request stays within the cap instead of 400-ing with "Invalid dataset
        # aggregation, the maximum allowed number of items is 2".
        if _with_usd and not with_quantity:
            ds["aggregation"]["totalCostUSD"] = {"name": "CostUSD", "function": "Sum"}
        if with_quantity:
            ds["aggregation"]["usageQuantity"] = {"name": "UsageQuantity", "function": "Sum"}
        if group_by:
            ds["grouping"] = [{"type": "Dimension", "name": g} for g in group_by]
        if filt:
            ds["filter"] = filt
        payload = {
            "type": metric, "timeframe": "Custom",
            "timePeriod": {"from": date_from.strftime("%Y-%m-%dT00:00:00+00:00"),
                           "to": date_to.strftime("%Y-%m-%dT23:59:59+00:00")},
            "dataset": ds,
        }
        url = "%s/providers/Microsoft.CostManagement/query?api-version=%s" % (scope, API_VERSION)
        try:
            data = self.s.post(url, payload=payload)
        except AzureApiError as e:
            if _with_usd and e.status == 400 and "costusd" in e.body.lower():
                return self.query(scope, metric, granularity, group_by, filt,
                                  date_from, date_to, _with_usd=False,
                                  with_quantity=with_quantity)
            raise
        frames = []
        while True:
            props = data.get("properties") or {}
            cols = [c["name"] for c in props.get("columns") or []]
            rows = props.get("rows") or []
            if cols:
                frames.append(pd.DataFrame(rows, columns=cols))
            nxt = props.get("nextLink")
            if not nxt:
                break
            self._pace()
            data = self.s.post(nxt, payload=payload)
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if df.empty:
            extra = ["UsageQuantity"] if with_quantity else []
            return pd.DataFrame(columns=list(group_by) + ["Cost", "CostUSD",
                                                          "Period", "Currency"] + extra)
        if "UsageDate" in df.columns:
            df["Period"] = pd.to_datetime(df["UsageDate"].astype(int).astype(str),
                                          format="%Y%m%d").dt.strftime("%Y-%m-%d")
        elif "BillingMonth" in df.columns:
            df["Period"] = pd.to_datetime(df["BillingMonth"]).dt.strftime("%Y-%m")
        if "CostUSD" not in df.columns and "Cost" in df.columns:
            df["CostUSD"] = df["Cost"]
        return df
