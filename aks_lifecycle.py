"""AKS lifecycle & release radar (Microsoft pages, no Azure auth).

Scrapes the public Microsoft sources that announce AKS lifecycle changes and
writes one workbook covering version GA/EOL dates, managed add-ons,
retirements/deprecations, new GA features, preview features and behavior
changes:

  - learn.microsoft.com .../aks/supported-kubernetes-versions
      AKS release calendar, LTS calendar, per-version component breaking changes
  - learn.microsoft.com .../aks/integrations
      managed add-ons and open-source/third-party integrations
  - github.com/Azure/AKS releases (the official weekly AKS release notes)
      announcements, GA features, preview features, behavioral changes,
      component updates

Tabs: ReadMe, Summary, ReleaseCalendar, Announcements, GAFeatures,
PreviewFeatures, BehaviorChanges, Addons, OpenSourceIntegrations,
BreakingChanges, ComponentUpdates, RawReleaseNotes.

Usage: python aks_lifecycle.py [--out reports] [--releases 30]
"""
import argparse
import calendar
import datetime as dt
import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import pandas as pd
import requests

from azrep import excel
from azrep.http_client import log

VERSIONS_URL = "https://learn.microsoft.com/en-us/azure/aks/supported-kubernetes-versions"
INTEGRATIONS_URL = "https://learn.microsoft.com/en-us/azure/aks/integrations"
RELEASES_URL = "https://api.github.com/repos/Azure/AKS/releases"
HEADERS = {"User-Agent": "aks-reporting-toolkit (aks_lifecycle)"}

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)")
MONTH_RE = re.compile(r"([A-Z][a-z]{2,8})\.?\s+(?:(\d{1,2}),?\s+)?(\d{4})")
K8S_HEADING_RE = re.compile(r"Kubernetes\s+(\d+\.\d+)")


def fetch_html(url, timeout=30):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def fetch_releases(count=30, timeout=30):
    r = requests.get(RELEASES_URL, headers={**HEADERS, "Accept": "application/vnd.github+json"},
                     params={"per_page": min(count, 100)}, timeout=timeout)
    r.raise_for_status()
    return r.json()[:count]


class _DocParser(HTMLParser):
    """Collects headings and tables in document order. Each table cell keeps
    its text and the first link inside it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []  # ("h", level, text) | ("table", headers, rows)
        self._h = self._table = self._row = self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4"):
            self._h = [int(tag[1]), ""]
        elif tag == "table":
            self._table = {"headers": [], "rows": []}
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = [tag, "", None]
        elif tag == "a" and self._cell is not None and self._cell[2] is None:
            self._cell[2] = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4") and self._h:
            self.items.append(("h", self._h[0], " ".join(self._h[1].split())))
            self._h = None
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append((self._cell[0], " ".join(self._cell[1].split()), self._cell[2]))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and all(k == "th" for k, _, _ in self._row):
                self._table["headers"] = [t for _, t, _ in self._row]
            else:
                self._table["rows"].append([(t, u) for _, t, u in self._row])
            self._row = None
        elif tag == "table" and self._table is not None:
            self.items.append(("table", self._table["headers"], self._table["rows"]))
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell[1] += data
        elif self._h is not None:
            self._h[1] += data


def parse_doc(html):
    p = _DocParser()
    p.feed(html)
    return p.items


def month_date(text):
    """'Mar 2026' / 'Aug 22, 2025' -> date (end of month when day missing)."""
    m = MONTH_RE.search(text or "")
    if not m:
        return None
    try:
        month = dt.datetime.strptime(m.group(1)[:3], "%b").month
    except ValueError:
        return None
    year, day = int(m.group(3)), m.group(2)
    return dt.date(year, month, int(day) if day else calendar.monthrange(year, month)[1])


def calendar_rows(items, today):
    rows = []
    for kind, headers, trs in (i for i in items if i[0] == "table"):
        if not headers or headers[0] != "Kubernetes version" or "AKS GA" not in headers:
            continue
        track = "LTS" if "LTS End of life" in headers else "Community"
        for tr in trs:
            cells = dict(zip(headers, (t for t, _ in tr)))
            ga = month_date(cells.get("AKS GA", ""))
            eol = month_date(cells.get("End of life", ""))
            ext = month_date(cells.get("LTS End of life") or cells.get("Platform support") or "")
            final = ext if (track == "LTS" and ext) else eol
            if final and final < today:
                status = "EOL"
            elif track == "LTS" and eol and eol < today:
                status = "LTS ONLY"
            elif final and (final - today).days <= 90:
                status = "EOL <90 DAYS"
            elif ga and ga <= today:
                status = "GA"
            else:
                status = "PREVIEW/UPCOMING"
            rows.append({"kubernetes_version": cells.get("Kubernetes version", ""),
                         "support_track": track,
                         "upstream_release": cells.get("Upstream release", ""),
                         "aks_preview": cells.get("AKS preview", ""),
                         "aks_ga": cells.get("AKS GA", ""),
                         "end_of_life": cells.get("End of life", ""),
                         "lts_or_platform_support_until":
                             cells.get("LTS End of life") or cells.get("Platform support") or "",
                         "days_to_final_eol": (final - today).days if final else None,
                         "status": status})
    return rows


def breaking_change_rows(items):
    rows, version = [], ""
    for it in items:
        if it[0] == "h":
            m = K8S_HEADING_RE.search(it[2])
            if m:
                version = m.group(1)
            continue
        headers, trs = it[1], it[2]
        if version and headers and any(h.startswith("Breaking changes") for h in headers):
            for tr in trs:
                texts = [t for t, _ in tr]
                links = [u for _, u in tr]
                cells = dict(zip(headers, texts))
                bc_col = next(h for h in headers if h.startswith("Breaking changes"))
                rows.append({"kubernetes_version": version,
                             "managed_addons": cells.get("AKS managed add-ons (addon)", ""),
                             "aks_components_ccp": cells.get("AKS components (ccp)", ""),
                             "os_components": cells.get("OS components", ""),
                             "breaking_changes": cells.get(bc_col, ""),
                             "link": next((u for u in links if u), "") or ""})
    return rows


def integration_rows(items, base_url):
    addons, oss = [], []
    for kind, headers, trs in (i for i in items if i[0] == "table"):
        if headers[:4] == ["Name", "Description", "Articles", "GitHub"]:
            for tr in (t for t in trs if len(t) >= 4):
                addons.append({"addon": tr[0][0], "description": tr[1][0],
                               "docs": tr[2][0],
                               "docs_url": urljoin(base_url, tr[2][1] or ""),
                               "github_url": urljoin(base_url, tr[3][1] or "")})
        elif headers[:3] == ["Name", "Description", "More details"]:
            for tr in (t for t in trs if len(t) >= 3):
                oss.append({"integration": tr[0][0],
                            "homepage": tr[0][1] or "",
                            "description": tr[1][0], "docs": tr[2][0],
                            "docs_url": urljoin(base_url, tr[2][1] or "")})
    return addons, oss


def strip_md(text):
    """Markdown bullet -> (plain text, first link)."""
    link = MD_LINK_RE.search(text)
    text = MD_LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("`", "")
    return " ".join(text.split()), (link.group(2) if link else "")


def release_bullets(body):
    """Yield (section_heading, bullet_text) from a release-notes markdown body."""
    section, cur = "", None
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            if cur:
                yield section, cur
            section, cur = s.lstrip("#").strip(), None
        elif s.startswith(("* ", "- ")) and not line[:1].isspace():
            if cur:
                yield section, cur
            cur = s[2:].strip()
        elif cur and s and not s.startswith(("|", "```")):
            cur += " " + s
        elif cur and not s:
            yield section, cur
            cur = None
    if cur:
        yield section, cur


def classify_section(heading):
    h = heading.lower()
    for key, cat in (("announce", "announcement"), ("retire", "announcement"),
                     ("preview feature", "preview"), ("feature", "feature"),
                     ("behavior", "behavior"), ("bug", "bugfix"),
                     ("component", "component"), ("kubernetes version", "k8s")):
        if key in h:
            return cat
    return "other"


def announcement_kind(text):
    t = text.lower()
    if "retir" in t:
        return "RETIREMENT"
    if any(k in t for k in ("deprecat", "no longer support", "end of life",
                            "end of support", "removed")):
        return "DEPRECATION"
    if "generally available" in t or "now available" in t:
        return "GA"
    if "preview" in t:
        return "PREVIEW"
    return "NOTICE"


def main(argv=None):
    p = argparse.ArgumentParser(
        description="AKS lifecycle & release radar from Microsoft pages",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--out", default="reports", help="output directory")
    p.add_argument("--releases", type=int, default=30,
                   help="how many AKS release notes to scan (AKS releases ship roughly weekly)")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    args = p.parse_args(argv)
    today = dt.date.today()

    log("Fetching AKS supported-versions page...")
    version_items = parse_doc(fetch_html(VERSIONS_URL, args.timeout))
    log("Fetching AKS integrations (add-ons) page...")
    integ_items = parse_doc(fetch_html(INTEGRATIONS_URL, args.timeout))
    log("Fetching last %d AKS release notes from GitHub..." % args.releases)
    releases = fetch_releases(args.releases, args.timeout)

    cal = calendar_rows(version_items, today)
    breaking = breaking_change_rows(version_items)
    addons, oss = integration_rows(integ_items, INTEGRATIONS_URL)

    ann, ga_feats, prev_feats, behavior, comps, raw = [], [], [], [], [], []
    for r in releases:
        rel, pub = r.get("tag_name") or r.get("name") or "", (r.get("published_at") or "")[:10]
        for section, bullet in release_bullets(r.get("body")):
            text, link = strip_md(bullet)
            cat = classify_section(section)
            raw.append({"release": rel, "published": pub, "section": section,
                        "category": cat, "item": text, "link": link})
            base = {"release": rel, "published": pub, "item": text, "link": link}
            if cat == "announcement":
                ann.append({"release": rel, "published": pub,
                            "kind": announcement_kind(text), "item": text, "link": link})
            elif cat == "feature":
                ga_feats.append(base)
            elif cat == "preview":
                prev_feats.append(base)
            elif cat == "behavior":
                behavior.append(base)
            elif cat == "component":
                comps.append(base)

    rel_window = "%s .. %s" % (releases[-1]["tag_name"], releases[0]["tag_name"]) \
        if releases else "(none)"
    kinds = pd.Series([a["kind"] for a in ann]).value_counts().to_dict() if ann else {}
    summary = pd.DataFrame([
        ("Release notes scanned", "%d (%s)" % (len(releases), rel_window)),
        ("Retirements announced", kinds.get("RETIREMENT", 0)),
        ("Deprecations announced", kinds.get("DEPRECATION", 0)),
        ("GA announcements / features", "%d / %d" % (kinds.get("GA", 0), len(ga_feats))),
        ("Preview features in window", len(prev_feats)),
        ("Behavior changes in window", len(behavior)),
        ("K8s versions on calendar (community / LTS)",
         "%d / %d" % (sum(c["support_track"] == "Community" for c in cal),
                      sum(c["support_track"] == "LTS" for c in cal))),
        ("Versions GA today", sum(c["status"] == "GA" for c in cal)),
        ("Versions within 90 days of EOL", sum(c["status"] == "EOL <90 DAYS" for c in cal)),
        ("Managed add-ons documented", len(addons)),
        ("Open-source integrations documented", len(oss)),
    ], columns=["Item", "Value"])

    def df_or_note(rows, note_cols):
        return pd.DataFrame(rows) if rows else pd.DataFrame(
            [{c: ("(none found)" if i == 0 else "") for i, c in enumerate(note_cols)}])

    wb = excel.new_workbook()
    excel.add_readme(wb, "AKS Lifecycle & Release Radar", [
        "Generated: %s   Release notes window: %s" %
        (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), rel_window),
        "",
        "Scraped from public Microsoft sources (no Azure subscription access used):",
        "  %s" % VERSIONS_URL,
        "  %s" % INTEGRATIONS_URL,
        "  https://github.com/Azure/AKS/releases",
        "",
        "How to read the workbook:",
        "  ReleaseCalendar   - AKS GA/EOL dates per Kubernetes minor, community and LTS tracks.",
        "  Announcements     - retirements, deprecations and GA notices from the weekly",
        "                      AKS release notes; act on RETIREMENT/DEPRECATION rows first.",
        "  GAFeatures        - features that went generally available in the window.",
        "  PreviewFeatures   - preview features (no production SLA).",
        "  BehaviorChanges   - defaults/behavior that changed without an API version bump.",
        "  Addons            - documented managed add-ons (lifecycle handled by AKS).",
        "  BreakingChanges   - per-version component versions and breaking changes (reference).",
        "  ComponentUpdates  - component bumps shipped by each release (reference).",
        "",
        "Status meanings on ReleaseCalendar:",
        "  GA / PREVIEW/UPCOMING / EOL <90 DAYS / LTS ONLY (community support over) / EOL.",
    ])
    excel.add_table(wb, "Summary", summary, section="summary", max_width=70)
    excel.add_table(wb, "ReleaseCalendar", df_or_note(cal, ("kubernetes_version",)),
                    fail_cols=("status",), fail_values=("EOL",),
                    warn_values=("EOL <90 DAYS", "LTS ONLY", "PREVIEW/UPCOMING"),
                    int_cols=("days_to_final_eol",), max_width=40)
    excel.add_table(wb, "Announcements",
                    df_or_note(ann, ("release", "published", "kind", "item", "link")),
                    fail_cols=("kind",), fail_values=("RETIREMENT",),
                    warn_values=("DEPRECATION",), max_width=120)
    excel.add_table(wb, "GAFeatures",
                    df_or_note(ga_feats, ("release", "published", "item", "link")),
                    max_width=120)
    excel.add_table(wb, "PreviewFeatures",
                    df_or_note(prev_feats, ("release", "published", "item", "link")),
                    max_width=120)
    excel.add_table(wb, "BehaviorChanges",
                    df_or_note(behavior, ("release", "published", "item", "link")),
                    max_width=120)
    excel.add_table(wb, "Addons",
                    df_or_note(addons, ("addon", "description", "docs", "docs_url",
                                        "github_url")), max_width=80)
    excel.add_table(wb, "OpenSourceIntegrations",
                    df_or_note(oss, ("integration", "homepage", "description", "docs",
                                     "docs_url")), section="reference", max_width=80)
    excel.add_table(wb, "BreakingChanges",
                    df_or_note(breaking, ("kubernetes_version",)),
                    section="reference", max_width=90)
    excel.add_table(wb, "ComponentUpdates",
                    df_or_note(comps, ("release", "published", "item", "link")),
                    section="reference", max_width=120)
    excel.add_table(wb, "RawReleaseNotes",
                    df_or_note(raw, ("release", "published", "section", "category",
                                     "item", "link")), section="reference", max_width=120)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "aks_lifecycle_%s.xlsx"
                        % dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    excel.save(wb, path)
    log("Report written: %s" % path)


if __name__ == "__main__":
    main()
