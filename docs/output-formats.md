# Output formats

_[← Back to the README](../README.md)_

How workbooks are laid out, the charts they carry, Markdown->DOCX/PDF conversion, and the architecture-design companion files.

## Subscription Resource CSV

`resources` is the one report that emits CSV instead of XLSX:

```bash
uv run python aks_report.py resources --subs contoso-platform
uv run python aks_report.py resources --subs contoso-platform --output exports/resources.csv
```

It writes one row per resource visible in Azure Resource Graph. Stable scalar
columns identify the subscription, resource group, resource, type, location,
and ARM ID. Heterogeneous fields are losslessly serialized within
`sku_json`, `plan_json`, `identity_json`, `zones_json`,
`extended_location_json`, `tags_json`, and `properties_json` cells. The file is
UTF-8 with a BOM for Excel compatibility and is written only after all Resource
Graph pages have been fetched successfully.

This is a Resource Graph snapshot, not a recursive provider-specific ARM GET.
It reflects Resource Graph/RBAC coverage and eventual consistency, omits
secrets, and may not contain live runtime or instance-view state. Very large
JSON property cells remain valid CSV even if Excel truncates their display.

## Workbook Layout

Every workbook follows the same four-section tab layout, enforced at save time:

1. **ReadMe** (blue tab): what the report is, scope, caveats, and a "Tab
   sections" index of the workbook.
2. **Summary** (green tabs): a `Summary` tab first, then optional
   `SummaryBy<Dimension>` breakdowns.
3. **Detail** (plain tabs): findings and per-entity tables.
4. **Reference** (gray tabs): `Raw*` extracts and lookup/legend tabs
   (`PriceReference`, `SupportedVersions`, `CheckLegend`, ...), always last.

## XLSX Visualizations

Reports are still multi-sheet XLSX workbooks, and several sheets now include
native Excel charts:

- `deepdive`: daily cost and utilization trends.
- `cost`: top movers and subscription cost charts.
- `optimization`: estimated savings and cluster cost charts.

The charts are generated from workbook data, so they remain editable in Excel.

## Markdown to DOCX/PDF

The launcher can also convert Markdown documentation to DOCX and PDF:

```bash
uv run python aks_report.py convert README.md --to docx
uv run python aks_report.py convert README.md --to pdf
uv run python aks_report.py convert README.md --to all --config examples/report_style.example.yaml
```

The style is configurable through a YAML file. Start with:

```bash
cp examples/report_style.example.yaml my_report_style.yaml
uv run python aks_report.py convert README.md --to all --config my_report_style.yaml
```

Configurable items include page size, margins, body/heading/code fonts, heading
sizes, colors, paragraph spacing, and table styling. This is intentionally like
a Terraform example file: copy it, edit values, and run the same command.

## Architecture Design Output

The design report creates a workbook and, unless `--no-doc` is supplied, three
companion files next to it:

- a Markdown document with Mermaid diagrams: one per-cluster architecture view
  plus a fleet-wide relationship chart (subscriptions, clusters, subnets, vnets
  and subnet attachments such as NSGs, route tables and NAT gateways);
- a `.drawio` file (open in <https://app.diagrams.net> or the draw.io desktop /
  VS Code app) with a "Fleet relationships" page and one editable architecture
  page per cluster;
- a self-contained `.html` design view (pure HTML/CSS, no JavaScript or CDN, so
  it opens in any browser even offline): the fleet overview shows subscription
  boxes with cluster cards next to VNet boxes whose subnet cards carry
  attachment chips (NSG/route table/NAT gateway) and "used by" chips
  (cluster/pool, nodes vs pods); each cluster then gets a section with nested
  subscription -> resource group boxes holding AKS/API-server/identity cards,
  node-pool cards (spot pools highlighted) and resource-count chips.

The workbook also has a `Relationships` tab listing every relationship as a
`source -> relation -> target` row (containment, node pools, subnet usage,
vnet membership, subnet attachments, co-located resources).

```bash
uv run python aks_report.py design --cluster aks-dev-01 --all
uv run python aks_report.py design --env dev
uv run python aks_report.py design --subs contoso-platform --rg rg-apps-dev
uv run python aks_report.py design --subs contoso-platform --all --no-doc
```

Scope behavior:

- `--cluster` / `--cluster-prefix` / `--cluster-contains`: designs the selected
  cluster or cluster set, including the AKS resource group, node resource group,
  node pools, network profile, referenced subnets, and nearby Azure resources.
- `--rg` / `--resource-group`: designs all Azure resources in the named resource
  group(s), and includes AKS clusters found in the selected subscription scope.
- `--subs` with no cluster/RG filter: creates a subscription-level resource
  inventory and resource-type summary.

Because this uses subscription-level ARM/Resource Graph data, it can describe
AKS control-plane state, node pools, VMSS, load balancers, public IPs, disks,
subnets, SKUs, identities, addons, and tags. It cannot see in-cluster Services,
Ingresses, namespaces, pods, or application-to-application traffic without
kubectl access.
