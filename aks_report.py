"""Single entry point for the AKS reporting toolkit.

This script is the only command users need to remember. The report-specific
scripts remain importable modules so the code stays small and maintainable.

Examples:
  uv run python aks_report.py
  uv run python aks_report.py inventory --all
  uv run python aks_report.py cost --subs contoso-platform --env dev
  uv run python aks_report.py deepdive --env dev --cluster aks-dev-01
  uv run python aks_report.py design --cluster aks-dev-01 --all
  uv run python aks_report.py network --nonprod
  uv run python aks_report.py spot-design --cluster aks-dev-01
  uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules vulnerability_classification.example.json
  uv run python aks_report.py convert README.md --to all --config report_style.example.yaml
  uv run python aks_report.py sandbox plan sandbox.example.yaml
  uv run python aks_report.py list
"""
import importlib
import sys


REPORTS = [
    {
        "key": "inventory",
        "aliases": ("inv", "fleet", "clusters"),
        "module": "fleet_inventory",
        "title": "Fleet inventory",
        "description": "All cluster, node-pool, network, addon, tag and summary details.",
    },
    {
        "key": "360",
        "aliases": ("cluster-360", "estate", "all-in-one"),
        "module": "cluster_360",
        "title": "Cluster 360",
        "description": "All clusters from all subscriptions categorized in one workbook: "
                       "version EOL, governance, cost trend, utilization, health score.",
    },
    {
        "key": "cost",
        "aliases": ("fleet-cost", "costs"),
        "module": "fleet_cost",
        "title": "Fleet cost",
        "description": "3-month amortized cost trend, spot/RI/SP split and SKU change signals.",
    },
    {
        "key": "deepdive",
        "aliases": ("cluster", "cluster-cost"),
        "module": "cluster_deepdive",
        "title": "Cluster deep dive",
        "description": "One cluster: daily cost, actual vs amortized, node-pool cost, SKU changes.",
    },
    {
        "key": "design",
        "aliases": ("architecture", "topology", "diagram"),
        "module": "architecture_design",
        "title": "Architecture design",
        "description": "Actual-state design workbook plus Mermaid doc and draw.io relationship diagrams.",
    },
    {
        "key": "version",
        "aliases": ("versions", "eol", "version-eol"),
        "module": "version_eol",
        "title": "Version and EOL",
        "description": "AKS supported-version status and node image staleness.",
    },
    {
        "key": "spot",
        "aliases": ("spot-opportunity", "spot-detail", "spot-config", "spot-clusters",
                    "spot-cost"),
        "module": "spot_cluster_report",
        "title": "Spot clusters and opportunity",
        "description": "Spot pool config, autoscaler, assessment, cost breakup and "
                       "candidate pools with retail-price savings.",
    },
    {
        "key": "spot-design",
        "aliases": ("spot-split", "split-design"),
        "module": "spot_split_design",
        "title": "Spot split design",
        "description": "Present vs future node-pool split for team-dedicated clusters: "
                       "az commands, workload YAML, rollout plan, Mermaid design doc.",
    },
    {
        "key": "utilization",
        "aliases": ("util", "idle"),
        "module": "utilization_idle",
        "title": "Utilization and idle",
        "description": "CPU/memory platform metrics, idle and stopped-but-billing clusters.",
    },
    {
        "key": "governance",
        "aliases": ("gov", "hygiene"),
        "module": "governance",
        "title": "Governance scorecard",
        "description": "Control-plane hygiene checks: API access, AAD, policy, autoscaler, zones.",
    },
    {
        "key": "policy",
        "aliases": ("policies", "policy-compliance"),
        "module": "policy_report",
        "title": "Azure Policy",
        "description": "Assignments, compliance, non-compliance and Kubernetes policy blind spots.",
    },
    {
        "key": "network",
        "aliases": ("ip", "ip-capacity", "network-ip"),
        "module": "network_ip_capacity",
        "title": "Network and IP capacity",
        "description": "Network model, API exposure, subnet metadata and Azure CNI IP pressure.",
    },
    {
        "key": "tags",
        "aliases": ("tag", "chargeback"),
        "module": "tag_chargeback",
        "title": "Tags and chargeback",
        "description": "Required tag coverage, missing owner/cost-center tags and value cleanup.",
    },
    {
        "key": "optimization",
        "aliases": ("optimize", "savings"),
        "module": "optimization_report",
        "title": "Optimization priorities",
        "description": "Cost, utilization, spot/RI/SP and stopped-billing candidates in one workbook.",
    },
    {
        "key": "container-eol",
        "aliases": ("image-eol", "os-eol", "runtime-eol", "eol-radar"),
        "module": "container_os_eol",
        "title": "Container & OS EOL radar",
        "description": "endoflife.date lifecycle for Alpine/Debian/UBI base images and "
                       "Java/Python/Node.js runtimes.",
    },
    {
        "key": "aks-lifecycle",
        "aliases": ("lifecycle", "aks-releases", "release-notes", "addons"),
        "module": "aks_lifecycle",
        "title": "AKS lifecycle & release radar",
        "description": "AKS release calendar, add-ons, retirements/deprecations, GA and "
                       "preview features from Microsoft pages.",
    },
    {
        "key": "conformance",
        "aliases": ("golden", "drift", "baseline"),
        "module": "conformance",
        "title": "Golden-config conformance",
        "description": "Fleet drift against a golden baseline YAML (sandbox config schema); "
                       "requires --golden <file>.",
    },
    {
        "key": "rearch",
        "aliases": ("rearchitect", "subres"),
        "module": "subscription_rearch",
        "title": "Subscription re-architecture (cost savings)",
        "description": "One subscription, ALL resources: orphan/idle/redundancy "
                       "findings, Advisor cost recs, actual-cost evidence and a "
                       "re-architecture narrative (.md) with savings estimates.",
    },
    {
        "key": "vulnerabilities",
        "aliases": ("vuln", "cve", "prisma", "prisma-vuln"),
        "module": "vulnerability_report",
        "title": "CVE vulnerability classification",
        "description": "CVE/Prisma XLSX enrichment and base-image/application/platform classification.",
    },
]

ALIASES = {r["key"]: r for r in REPORTS}
for _report in REPORTS:
    for _alias in _report["aliases"]:
        ALIASES[_alias] = _report


def print_help():
    print(__doc__.strip())
    print("\nReports:")
    for r in REPORTS:
        names = ", ".join((r["key"],) + r["aliases"])
        print("  %-34s %s" % (names, r["title"]))
    print("\nScope flags are passed through to the selected report, for example:")
    print("  --subs <id-or-name>   --env dev   --nonprod   --cluster-prefix aks-d")
    print("  --cluster <name>      --cluster-contains payments")
    print("\nUse a report-specific help page like:")
    print("  uv run python aks_report.py cost --help")
    print("\nDocument conversion:")
    print("  uv run python aks_report.py convert README.md --to docx")
    print("  uv run python aks_report.py convert README.md --to pdf --config report_style.example.yaml")
    print("\nSandbox admin workflow:")
    print("  uv run python aks_report.py sandbox plan sandbox.example.yaml")
    print("  uv run python aks_report.py sandbox deploy sandbox.example.yaml --yes --wait")
    print("  uv run python aks_report.py sandbox policy-apply sandbox.example.yaml --yes")
    print("  uv run python aks_report.py sandbox scan sandbox.example.yaml --yes")
    print("  uv run python aks_report.py sandbox clone --cluster-id <ARM-id> --base sandbox.yaml")
    print("  uv run python aks_report.py sandbox k8s-test sandbox.yaml --yes")
    print("  uv run python aks_report.py sandbox impact sandbox.yaml --policy policies/candidate.json --all --yes")
    print("  uv run python aks_report.py sandbox spot-sim sandbox.yaml --yes")
    print("  uv run python aks_report.py sandbox upgrade-rehearsal sandbox.yaml --to next --yes")
    print("\nVulnerability workflow:")
    print("  uv run python aks_report.py vulnerabilities --cves cves.txt")
    print("  uv run python aks_report.py vulnerabilities --prisma prisma.xlsx --classification-rules vulnerability_classification.example.json")


def print_list():
    for i, r in enumerate(REPORTS, 1):
        print("%2d) %-24s %s" % (i, r["key"], r["description"]))


def choose_report():
    print("\nAKS Reporting Toolkit")
    print("Choose a report. Press Enter for inventory.\n")
    print_list()
    raw = input("\nReport [inventory]: ").strip().lower()
    if not raw:
        return ALIASES["inventory"]
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(REPORTS):
            return REPORTS[idx - 1]
    if raw in ALIASES:
        return ALIASES[raw]
    sys.exit("Unknown report: %s. Run `uv run python aks_report.py list`." % raw)


def run_report(report, args):
    mod = importlib.import_module(report["module"])
    if not hasattr(mod, "main"):
        sys.exit("Report module has no main(): %s" % report["module"])
    print("\nRunning: %s\n" % report["title"])
    return mod.main(args)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print_help()
        return
    if argv and argv[0].lower() in ("list", "reports"):
        print_list()
        return
    if argv and argv[0].lower() in ("convert", "export-doc", "docs"):
        from azrep import doc_export
        return doc_export.main(argv[1:])
    if argv and argv[0].lower() in ("sandbox", "lab"):
        from azrep import sandbox
        return sandbox.main(argv[1:])

    if argv and not argv[0].startswith("-"):
        token = argv.pop(0).lower()
        report = ALIASES.get(token)
        if not report:
            sys.exit("Unknown report: %s. Run `uv run python aks_report.py list`." % token)
    else:
        report = choose_report()

    return run_report(report, argv)


if __name__ == "__main__":
    main()
