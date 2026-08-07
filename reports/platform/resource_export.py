"""Azure Resource Graph snapshot for one subscription, exported as CSV.

The export contains one row per resource visible in Resource Graph. Generic
fields stay as scalar CSV columns; heterogeneous ARM fields such as properties,
tags, identity, SKU, and zones are retained as canonical JSON cells.

Usage:
  uv run python aks_report.py resources --subs contoso-platform
  uv run python aks_report.py resources --subs <subscription-guid> --output resources.csv
"""
import csv
import datetime as dt
import json
import os
import sys
import tempfile

from azrep import arg
from azrep.http_client import connect, log
from azrep.subs import (base_parser, load_subscriptions, pick_scope,
                        sanitize_scope_part)


RESOURCE_DETAILS_KQL = """
resources
| project id,
    name,
    type = tolower(type),
    tenantId,
    subscriptionId,
    resourceGroup,
    location,
    kind,
    managedBy,
    sku,
    plan,
    identity,
    zones,
    extendedLocation,
    tags,
    properties
| order by id asc
"""

CSV_COLUMNS = [
    "subscription_name",
    "subscription_id",
    "tenant_id",
    "resource_group",
    "name",
    "type",
    "location",
    "id",
    "kind",
    "managed_by",
    "sku_json",
    "plan_json",
    "identity_json",
    "zones_json",
    "extended_location_json",
    "tags_json",
    "properties_json",
]


def _json_cell(value):
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def resource_row(resource, subscription_name):
    return {
        "subscription_name": subscription_name,
        "subscription_id": resource.get("subscriptionId") or "",
        "tenant_id": resource.get("tenantId") or "",
        "resource_group": resource.get("resourceGroup") or "",
        "name": resource.get("name") or "",
        "type": resource.get("type") or "",
        "location": resource.get("location") or "",
        "id": resource.get("id") or "",
        "kind": resource.get("kind") or "",
        "managed_by": resource.get("managedBy") or "",
        "sku_json": _json_cell(resource.get("sku")),
        "plan_json": _json_cell(resource.get("plan")),
        "identity_json": _json_cell(resource.get("identity")),
        "zones_json": _json_cell(resource.get("zones")),
        "extended_location_json": _json_cell(resource.get("extendedLocation")),
        "tags_json": _json_cell(resource.get("tags")),
        "properties_json": _json_cell(resource.get("properties")),
    }


def _output_path(args, subscription):
    if args.output:
        path = os.path.abspath(args.output)
        if not path.lower().endswith(".csv"):
            path += ".csv"
    else:
        os.makedirs(args.out, exist_ok=True)
        label = subscription["subscription_name"] or subscription["subscription_id"]
        name = "azure_resources_%s_%s.csv" % (
            sanitize_scope_part(label), dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        path = os.path.abspath(os.path.join(args.out, name))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def write_csv(path, resources, subscription_name):
    """Atomically write a complete inventory, returning the number of rows."""
    ordered = sorted(resources, key=lambda r: (
        str(r.get("type") or "").lower(),
        str(r.get("resourceGroup") or "").lower(),
        str(r.get("name") or "").lower(),
        str(r.get("id") or "").lower(),
    ))
    fd, tmp_path = tempfile.mkstemp(prefix=".resource_export_", suffix=".csv",
                                    dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for resource in ordered:
                writer.writerow(resource_row(resource, subscription_name))
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return len(ordered)


def build_parser():
    p = base_parser("Export all Resource Graph resource details for one subscription to CSV")
    p.add_argument("--output", help="exact CSV output path (otherwise generated under --out)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    subscriptions = load_subscriptions(args.csv)
    selected, _ = pick_scope(subscriptions, args)
    if len(selected) != 1:
        print("This export requires exactly ONE subscription; %d are in scope.\n"
              "Narrow with --subs <id-or-name>." % len(selected), file=sys.stderr)
        sys.exit(2)

    subscription = selected[0]
    sub_id = subscription["subscription_id"]
    sub_name = subscription["subscription_name"] or sub_id
    session = connect(min_interval=0.1)

    # Resource Graph otherwise returns an indistinguishable empty result when a
    # listed subscription is in the wrong tenant or is not readable.
    visible = arg.query(session, arg.SUB_NAMES_KQL, [sub_id])
    if not any(str(r.get("subscriptionId") or "").lower() == sub_id for r in visible):
        print("Subscription %s is not visible in Azure Resource Graph. Check the active "
              "tenant and Reader access." % sub_id, file=sys.stderr)
        sys.exit(2)

    log("Inventorying Resource Graph details for %s (%s)..." % (sub_name, sub_id))
    resources = arg.query(session, RESOURCE_DETAILS_KQL, [sub_id])
    path = _output_path(args, subscription)
    count = write_csv(path, resources, sub_name)
    types = len({str(r.get("type") or "").lower() for r in resources})
    log("Exported %d resources across %d resource types: %s" % (count, types, path))
    return path


if __name__ == "__main__":
    main()
