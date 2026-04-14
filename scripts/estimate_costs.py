#!/usr/bin/env python3
"""
estimate_costs.py – Parse Azure ARM what-if output and estimate monthly costs
using the Azure Retail Prices API (https://prices.azure.com).

This script:
  1. Reads the what-if JSON output from `az deployment group what-if`
  2. Queries the Azure Retail Prices API for live pricing per resource
  3. Produces a Markdown summary suitable for posting on a GitHub PR
  4. Exits with code 1 if total cost exceeds the configured threshold

Usage:
  python scripts/estimate_costs.py \
      --what-if-file what-if-output.json \
      --threshold 500 \
      --output cost-report.md
"""

import argparse
import json
import os
import sys
from typing import Any

import requests

# ─── Azure Retail Prices API ─────────────────────────────────────────────────

API_URL = "https://prices.azure.com/api/retail/prices"
_query_cache: dict[str, list[dict]] = {}

# ARM SKU → API skuName translation for Storage Accounts
_STORAGE_SKU_MAP: dict[str, str] = {
    "Standard_LRS": "Hot LRS",
    "Standard_GRS": "Hot GRS",
    "Standard_ZRS": "Hot ZRS",
    "Standard_RAGRS": "Hot RA-GRS",
    "Standard_RAGZRS": "Hot RA-GZRS",
    "Standard_GZRS": "Hot GZRS",
    "Premium_LRS": "Premium LRS",
    "Premium_ZRS": "Premium ZRS",
}


def _query_api(
    service_name: str,
    region: str,
    sku_name: str | None = None,
    product_name: str | None = None,
) -> list[dict]:
    """Query the Azure Retail Prices API with caching."""
    parts = [
        f"serviceName eq '{service_name}'",
        f"armRegionName eq '{region}'",
        "priceType eq 'Consumption'",
        "isPrimaryMeterRegion eq true",
    ]
    if sku_name:
        parts.append(f"skuName eq '{sku_name}'")
    if product_name:
        parts.append(f"productName eq '{product_name}'")

    filt = " and ".join(parts)
    if filt in _query_cache:
        return _query_cache[filt]

    try:
        resp = requests.get(API_URL, params={"$filter": filt, "$top": "100"}, timeout=15)
        resp.raise_for_status()
        items = resp.json().get("Items", [])
    except requests.RequestException as exc:
        print(f"⚠️  API error ({service_name}/{sku_name}): {exc}", file=sys.stderr)
        items = []

    _query_cache[filt] = items
    return items


def _pick_item(
    items: list[dict],
    meter_hint: str | None = None,
    unit_hint: str | None = None,
) -> dict | None:
    """Select the best-matching meter from API results."""
    if not items:
        return None
    candidates = items

    if meter_hint:
        flt = [i for i in candidates if meter_hint.lower() in i.get("meterName", "").lower()]
        if flt:
            candidates = flt

    if unit_hint:
        flt = [i for i in candidates if unit_hint.lower() in i.get("unitOfMeasure", "").lower()]
        if flt:
            candidates = flt

    # Prefer base-tier pricing (tierMinimumUnits == 0)
    base = [i for i in candidates if i.get("tierMinimumUnits", 0) == 0]
    if base:
        candidates = base

    # Prefer non-zero prices
    non_zero = [i for i in candidates if i.get("retailPrice", 0) > 0]
    if non_zero:
        candidates = non_zero

    return candidates[0] if candidates else None


# ─── Per-resource-type pricing functions ─────────────────────────────────────
# Each returns (monthly_cost, source_description).

def _price_app_service_plan(sku: str, region: str) -> tuple[float, str]:
    """App Service Plan – hourly rate × 730 hrs/month."""
    if sku in ("_default", ""):
        return 0.0, "No SKU detected"
    items = _query_api("Azure App Service", region, sku_name=sku)
    item = _pick_item(items, unit_hint="Hour")
    if item:
        monthly = item["retailPrice"] * 730
        return monthly, f"API: ${item['retailPrice']:.4f}/hr × 730 hrs"
    return 0.0, f"No API result for SKU '{sku}'"


def _price_web_site(_sku: str, _region: str) -> tuple[float, str]:
    """Web Apps are free – cost is on the App Service Plan."""
    return 0.0, "Included in App Service Plan"


def _price_storage(sku: str, region: str) -> tuple[float, str]:
    """Storage Account – per-GB data-stored rate × assumed 100 GB."""
    api_sku = _STORAGE_SKU_MAP.get(sku, "Hot LRS")
    items = _query_api("Storage", region, sku_name=api_sku, product_name="Blob Storage")
    item = _pick_item(items, meter_hint="Data Stored", unit_hint="GB/Month")
    assumed_gb = 100
    if item:
        monthly = item["retailPrice"] * assumed_gb
        return monthly, f"API: ${item['retailPrice']:.4f}/GB × {assumed_gb} GB"
    return 0.0, f"No API result for '{sku}' (→ '{api_sku}')"


def _price_app_config(sku: str, region: str) -> tuple[float, str]:
    """App Configuration – Free tier ($0) or daily instance rate × 30."""
    if sku.lower() in ("free", "_default", ""):
        return 0.0, "Free tier"
    api_sku = sku.capitalize()
    items = _query_api("App Configuration", region, sku_name=api_sku)
    item = _pick_item(items, meter_hint="Instance", unit_hint="Day")
    if item:
        monthly = item["retailPrice"] * 30
        return monthly, f"API: ${item['retailPrice']:.2f}/day × 30 days"
    return 0.0, f"No API result for tier '{sku}'"


def _price_app_insights(_sku: str, region: str) -> tuple[float, str]:
    """Application Insights – data-retention rate × assumed 5 GB."""
    items = _query_api("Application Insights", region, sku_name="Enterprise")
    item = _pick_item(items, meter_hint="Data Retention", unit_hint="GB/Month")
    assumed_gb = 5
    if item:
        monthly = item["retailPrice"] * assumed_gb
        return monthly, f"API: ${item['retailPrice']:.2f}/GB × {assumed_gb} GB"
    return 0.0, "No API result"


def _price_log_analytics(_sku: str, region: str) -> tuple[float, str]:
    """Log Analytics (Azure Monitor) – ingestion rate × assumed 5 GB."""
    items = _query_api("Azure Monitor", region, sku_name="Basic Logs")
    item = _pick_item(items, meter_hint="Data Ingestion", unit_hint="GB")
    assumed_gb = 5
    if item:
        monthly = item["retailPrice"] * assumed_gb
        return monthly, f"API: ${item['retailPrice']:.2f}/GB × {assumed_gb} GB"
    return 0.0, "No API result"


def _price_role_assignment(_sku: str, _region: str) -> tuple[float, str]:
    """Role assignments are free (management-plane RBAC)."""
    return 0.0, "Free (RBAC)"


# Registry: ARM resource type → pricing function
_ESTIMATORS: dict[str, Any] = {
    "Microsoft.Web/serverfarms": _price_app_service_plan,
    "Microsoft.Web/sites": _price_web_site,
    "Microsoft.Storage/storageAccounts": _price_storage,
    "Microsoft.AppConfiguration/configurationStores": _price_app_config,
    "Microsoft.Insights/components": _price_app_insights,
    "Microsoft.OperationalInsights/workspaces": _price_log_analytics,
    "Microsoft.Authorization/roleAssignments": _price_role_assignment,
}


# ─── Change-type labels ─────────────────────────────────────────────────────

CHANGE_LABELS = {
    "Create": "🆕 Create",
    "Delete": "🗑️ Delete",
    "Modify": "✏️ Modify",
    "NoChange": "✅ No Change",
    "Ignore": "⏭️ Ignore",
    "Deploy": "🚀 Deploy",
    "Unsupported": "❓ Unsupported",
}


def parse_what_if(filepath: str) -> list[dict[str, Any]]:
    """Read what-if JSON and return the list of resource changes."""
    with open(filepath, "r", encoding="utf-8") as fh:
        content = fh.read().strip()

    if not content:
        print("⚠️  What-if file is empty.", file=sys.stderr)
        return []

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"⚠️  Failed to parse what-if JSON: {e}", file=sys.stderr)
        return []

    # The what-if output structure can vary. Handle both CLI-captured
    # formats: wrapper with 'changes' key, or raw array.
    if isinstance(data, list):
        return data
    if "changes" in data:
        return data["changes"]
    if "properties" in data and "changes" in data["properties"]:
        return data["properties"]["changes"]
    return []


def resolve_sku(change: dict[str, Any]) -> str:
    """Best-effort extraction of SKU from a what-if change entry."""
    # Try after-state first, then before
    for state_key in ("after", "before"):
        state = change.get(state_key, {})
        if not state:
            continue
        # Direct sku object
        if "sku" in state:
            sku = state["sku"]
            if isinstance(sku, dict):
                return sku.get("name", sku.get("tier", "_default"))
            return str(sku)
        # properties.sku
        props = state.get("properties", {})
        if "sku" in props:
            sku = props["sku"]
            if isinstance(sku, dict):
                return sku.get("name", sku.get("tier", "_default"))
            return str(sku)
    return "_default"


def estimate_resource_cost(
    resource_type: str, sku: str, change_type: str, region: str = "eastus"
) -> tuple[float, str]:
    """Return (monthly_cost_delta, pricing_source) for a resource change.

    All prices come from the Azure Retail Prices API.
    """
    estimator = _ESTIMATORS.get(resource_type)
    if not estimator:
        return 0.0, "Unknown resource type"

    unit_cost, source = estimator(sku, region)

    if change_type == "Create":
        return unit_cost, source
    elif change_type == "Delete":
        return -unit_cost, source
    return 0.0, source


def build_report(changes: list[dict[str, Any]], threshold: float) -> tuple[str, float]:
    """Build a Markdown cost report and return (markdown, total_delta)."""

    rows: list[dict[str, Any]] = []
    total_delta = 0.0

    for change in changes:
        resource_id = change.get("resourceId", change.get("id", "unknown"))
        resource_type = change.get("resourceType", "")
        change_type = change.get("changeType", "Unsupported")
        name = resource_id.split("/")[-1] if "/" in resource_id else resource_id

        sku = resolve_sku(change)
        cost, source = estimate_resource_cost(resource_type, sku, change_type)
        total_delta += cost

        rows.append({
            "name": name,
            "type": resource_type,
            "change": change_type,
            "sku": sku,
            "cost": cost,
            "source": source,
        })

    # ── Build Markdown ───────────────────────────────────────────────────
    lines = [
        "## 💰 Infrastructure Cost Estimate",
        "",
        "| Resource | Type | Change | SKU | Est. Monthly Cost | Source |",
        "|----------|------|--------|-----|------------------:|--------|",
    ]

    for r in rows:
        label = CHANGE_LABELS.get(r["change"], r["change"])
        cost_str = f"${r['cost']:,.2f}" if r["cost"] >= 0 else f"-${abs(r['cost']):,.2f}"
        lines.append(
            f"| `{r['name']}` | `{r['type']}` | {label} | {r['sku']} "
            f"| {cost_str} | {r['source']} |"
        )

    lines.append("")
    lines.append(f"**Estimated total monthly delta: ${total_delta:,.2f}**")
    lines.append("")

    if threshold > 0:
        if total_delta > threshold:
            lines.append(
                f"> ⛔ **BLOCKED** – Estimated cost (${total_delta:,.2f}) "
                f"exceeds threshold (${threshold:,.2f}). "
                f"Please review and get approval before merging."
            )
        else:
            lines.append(
                f"> ✅ Estimated cost (${total_delta:,.2f}) is within the "
                f"threshold (${threshold:,.2f})."
            )
    else:
        lines.append(
            f"> ⛔ **BLOCKED** – Threshold is $0.00 (zero tolerance). "
            f"Estimated cost (${total_delta:,.2f}) exceeds threshold. "
            f"Set a threshold > 0 to allow deployment."
        )

    lines.append("")
    lines.append("---")
    lines.append("*Costs are live estimates from the "
                 "[Azure Retail Prices API](https://prices.azure.com). "
                 "Actual costs may vary based on usage.*")

    return "\n".join(lines), total_delta


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate Azure infra costs from what-if output")
    parser.add_argument("--what-if-file", required=True, help="Path to what-if JSON output")
    parser.add_argument("--threshold", type=float, default=0, help="Monthly cost threshold in USD (0 = no gate)")
    parser.add_argument("--output", default="cost-report.md", help="Output Markdown file path")
    args = parser.parse_args()

    if not os.path.isfile(args.what_if_file):
        print(f"❌ What-if file not found: {args.what_if_file}", file=sys.stderr)
        sys.exit(1)

    changes = parse_what_if(args.what_if_file)
    if not changes:
        print("⚠️  No resource changes detected in what-if output.")
        report = (
            "## 💰 Infrastructure Cost Estimate\n\n"
            "No resource changes detected – nothing to estimate.\n"
        )
        total = 0.0
    else:
        report, total = build_report(changes, args.threshold)

    # Write report file
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"📄 Cost report written to {args.output}")
    print(f"   Total estimated monthly delta: ${total:,.2f}")

    # Also write to GITHUB_STEP_SUMMARY if available
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    # Also export values for downstream steps
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"total_cost={total:.2f}\n")
            if args.threshold == 0 and total > 0:
                blocked = "true"
            elif args.threshold > 0 and total > args.threshold:
                blocked = "true"
            else:
                blocked = "false"
            fh.write(f"cost_exceeded={blocked}\n")

    # Exit with failure if over threshold or threshold is 0 (block all)
    if args.threshold == 0 and total > 0:
        print(f"⛔ Threshold is $0 (zero tolerance). Deployment blocked. Cost: ${total:,.2f}")
        sys.exit(1)
    if args.threshold > 0 and total > args.threshold:
        print(f"⛔ Cost threshold exceeded! ${total:,.2f} > ${args.threshold:,.2f}")
        sys.exit(1)

    print("✅ Cost check passed.")


if __name__ == "__main__":
    main()
