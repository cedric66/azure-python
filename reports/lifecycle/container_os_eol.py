"""Container & OS end-of-life radar (endoflife.date).

Scrapes https://endoflife.date for the lifecycle of the container base images
and language runtimes this estate builds on - Alpine, Debian, Red Hat UBI
(RHEL lifecycle), Java (Eclipse Temurin), Python and Node.js - and writes one
workbook that answers, per product line: which versions are still safe to
build on, which are security-only, what is about to fall off support, and
what to move to next.

Tabs: ReadMe, Summary, EolRadar, OsBaseImages, LanguageRuntimes, RawLifecycle.

No Azure access needed - this report only calls endoflife.date.

Usage: python container_os_eol.py [--out reports] [--products golang,ubuntu]
"""
import argparse
import datetime as dt
import os

import pandas as pd
import requests

from azrep import excel
from azrep.http_client import log

EOL_API = "https://endoflife.date/api/%s.json"
HEADERS = {"User-Agent": "aks-reporting-toolkit (container_os_eol)",
           "Accept": "application/json"}

# slug = endoflife.date product. `lts_based` products should only ship LTS
# cycles to prod, so the recommended target prefers the newest active LTS.
PRODUCTS = [
    {"slug": "alpine-linux", "product": "Alpine Linux", "group": "OS base image",
     "image": "alpine", "lts_based": False,
     "note": "each 3.x branch gets ~2 years of fixes; rebuild images on branch EOL"},
    {"slug": "debian", "product": "Debian", "group": "OS base image",
     "image": "debian / debian-slim", "lts_based": False,
     "note": "eol = end of standard support; extended = community Debian LTS"},
    {"slug": "rhel", "product": "Red Hat UBI (RHEL)", "group": "OS base image",
     "image": "ubi8 / ubi9 / ubi10", "lts_based": False,
     "note": "UBI images follow the RHEL lifecycle of the same major version"},
    {"slug": "eclipse-temurin", "product": "Java (Eclipse Temurin)",
     "group": "Language runtime", "image": "eclipse-temurin", "lts_based": True,
     "note": "build on LTS feature releases (8/11/17/21/25); interim releases die in 6 months"},
    {"slug": "python", "product": "Python", "group": "Language runtime",
     "image": "python / python-slim", "lts_based": False,
     "note": "~5 years per minor; security-only once active support ends"},
    {"slug": "nodejs", "product": "Node.js", "group": "Language runtime",
     "image": "node / node-slim", "lts_based": True,
     "note": "even majors get LTS; never ship odd (non-LTS) majors to prod"},
]


def fetch_product(slug, timeout=30):
    r = requests.get(EOL_API % slug, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _date(v):
    """endoflife.date mixes ISO dates with booleans in the same fields."""
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _show(v):
    if v is None or v is False:
        return ""
    if v is True:
        return "yes"
    return str(v)


def cycle_status(raw, today, warn_days=90, soon_days=180):
    """EOL / EOL <90 DAYS / EOL <180 DAYS / SECURITY ONLY / SUPPORTED."""
    eol_raw = raw.get("eol")
    eol_d = _date(eol_raw)
    if eol_raw is True or (eol_d and eol_d <= today):
        return "EOL"
    if eol_d:
        days = (eol_d - today).days
        if days <= warn_days:
            return "EOL <90 DAYS"
        if days <= soon_days:
            return "EOL <180 DAYS"
    sup_raw = raw.get("support")
    sup_d = _date(sup_raw)
    if sup_raw is False or (sup_d and sup_d <= today):
        return "SECURITY ONLY"
    return "SUPPORTED"


def is_active_lts(raw, today):
    lts = raw.get("lts")
    d = _date(lts)
    return lts is True or (d is not None and d <= today)


def recommended_target(cycles, today, lts_based):
    """Newest non-EOL cycle; for LTS-based products the newest active LTS."""
    alive = [c for c in cycles if cycle_status(c, today) != "EOL"]
    if lts_based:
        lts = [c for c in alive if is_active_lts(c, today)]
        if lts:
            alive = lts
    if not alive:
        return ""
    best = alive[0]  # the API lists newest cycles first
    return "%s (latest %s)" % (best.get("cycle"), best.get("latest") or "?")


def build_rows(meta, cycles, today):
    target = recommended_target(cycles, today, meta["lts_based"])
    rows = []
    for c in cycles:
        eol_d = _date(c.get("eol"))
        rows.append({
            "product": meta["product"], "group": meta["group"],
            "cycle": str(c.get("cycle") or ""), "codename": c.get("codename") or "",
            "latest_patch": c.get("latest") or "",
            "released": _show(c.get("releaseDate")),
            "lts": _show(c.get("lts")),
            "active_support_until": _show(c.get("support")),
            "security_support_until": _show(c.get("eol")),
            "extended_support": _show(c.get("extendedSupport")),
            "days_to_eol": (eol_d - today).days if eol_d else None,
            "status": cycle_status(c, today),
            "recommended_target": target,
            "container_image": meta["image"],
        })
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Container & OS EOL radar from endoflife.date",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--out", default="reports", help="output directory")
    p.add_argument("--products", default="",
                   help="extra endoflife.date product slugs, e.g. ubuntu,golang,dotnet")
    p.add_argument("--radar-lookback-days", type=int, default=180,
                   help="keep versions on the EolRadar tab this long after their EOL date")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    args = p.parse_args(argv)
    today = dt.date.today()

    products = list(PRODUCTS)
    for slug in (s.strip().lower() for s in args.products.split(",")):
        if slug and slug not in {q["slug"] for q in products}:
            products.append({"slug": slug, "product": slug.replace("-", " ").title(),
                             "group": "Custom", "image": slug, "lts_based": False,
                             "note": "added via --products"})

    detail, raw_rows, summary = [], [], []
    for meta in products:
        log("Fetching %s lifecycle (%s)..." % (meta["product"], meta["slug"]))
        try:
            cycles = fetch_product(meta["slug"], args.timeout)
        except requests.RequestException as e:
            log("  WARNING: skipping %s: %s" % (meta["slug"], e))
            continue
        rows = build_rows(meta, cycles, today)
        detail.extend(rows)
        for c in cycles:
            raw_rows.append({"product": meta["product"], "slug": meta["slug"],
                             **{k: _show(v) for k, v in c.items()}})

        alive = [r for r in rows if r["status"] != "EOL"]
        nxt = min((r for r in alive if r["days_to_eol"] is not None),
                  key=lambda r: r["days_to_eol"], default=None)
        summary.append({
            "product": meta["product"], "group": meta["group"],
            "container_image": meta["image"],
            "recommended_target": rows[0]["recommended_target"] if rows else "",
            "supported": sum(r["status"].startswith(("SUPPORTED", "EOL <")) for r in rows),
            "security_only": sum(r["status"] == "SECURITY ONLY" for r in rows),
            "eol": sum(r["status"] == "EOL" for r in rows),
            "cycles_tracked": len(rows),
            "next_eol_cycle": nxt["cycle"] if nxt else "",
            "next_eol_date": nxt["security_support_until"] if nxt else "",
            "next_eol_days": nxt["days_to_eol"] if nxt else None,
            "lifecycle_note": meta["note"],
        })
    if not detail:
        raise SystemExit("endoflife.date returned nothing - check connectivity.")

    df = pd.DataFrame(detail)
    radar = df[df["days_to_eol"].isna()
               | (df["days_to_eol"] > -args.radar_lookback_days)].copy()
    radar = radar.sort_values("days_to_eol", na_position="last")[
        ["product", "group", "cycle", "latest_patch", "status", "security_support_until",
         "days_to_eol", "active_support_until", "recommended_target", "container_image"]]

    cols = ["product", "group", "cycle", "codename", "latest_patch", "released", "lts",
            "active_support_until", "security_support_until", "extended_support",
            "days_to_eol", "status", "recommended_target", "container_image"]
    os_df = df[df["group"] == "OS base image"][cols]
    rt_df = df[df["group"] != "OS base image"][cols]

    wb = excel.new_workbook()
    excel.add_readme(wb, "Container & OS End-of-Life Radar", [
        "Generated: %s   Source: endoflife.date   Products: %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
         ", ".join(m["product"] for m in products)),
        "",
        "Lifecycle truth for the base images and runtimes the estate builds on.",
        "Red Hat UBI follows the RHEL lifecycle of the same major; Java is tracked via",
        "Eclipse Temurin, the most common OpenJDK container distribution.",
        "",
        "Status meanings:",
        "  EOL            - security support has ended; rebase/upgrade now.",
        "  EOL <90 DAYS   - falls out of support within 90 days; schedule the move.",
        "  EOL <180 DAYS  - within 180 days; put it on the roadmap.",
        "  SECURITY ONLY  - still patched, but no bug fixes; avoid for new builds.",
        "  SUPPORTED      - in active support.",
        "",
        "How to read the workbook:",
        "  Summary           - one row per product: what to build on next and the next EOL hit.",
        "  EolRadar          - every live version across all products, soonest EOL first.",
        "  OsBaseImages      - full lifecycle tables for Alpine / Debian / UBI.",
        "  LanguageRuntimes  - full lifecycle tables for Java / Python / Node.js.",
        "  RawLifecycle      - unmodified endoflife.date fields per version cycle.",
    ])
    excel.add_table(wb, "Summary", pd.DataFrame(summary), section="summary",
                    int_cols=("supported", "security_only", "eol", "cycles_tracked",
                              "next_eol_days"), max_width=60)
    excel.add_table(wb, "EolRadar", radar, fail_cols=("status",),
                    fail_values=("EOL",),
                    warn_values=("EOL <90 DAYS", "EOL <180 DAYS", "SECURITY ONLY"),
                    int_cols=("days_to_eol",), colorscale_cols=("days_to_eol",),
                    max_width=60)
    for name, part in (("OsBaseImages", os_df), ("LanguageRuntimes", rt_df)):
        excel.add_table(wb, name, part, fail_cols=("status",), fail_values=("EOL",),
                        warn_values=("EOL <90 DAYS", "EOL <180 DAYS", "SECURITY ONLY"),
                        int_cols=("days_to_eol",), max_width=60)
    excel.add_table(wb, "RawLifecycle", pd.DataFrame(raw_rows), section="reference",
                    max_width=70)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "container_os_eol_%s.xlsx"
                        % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    excel.save(wb, path)
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
