#!/usr/bin/env python3
"""
estimate_costs.py – Parse Azure ARM what-if output and estimate monthly costs.

This script:
  1. Reads the what-if JSON output from `az deployment group what-if`
  2. Maps each resource type + SKU to an approximate monthly cost (USD)
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
# Maps ARM resource types to the (serviceName, skuName) used by the API.
# When a SKU isn't found via the API, the static fallback is used.

RESOURCE_TYPE_TO_SERVICE: dict[str, str] = {
    "Microsoft.Web/serverfarms": "Azure App Service",
    "Microsoft.Web/sites": "Azure App Service",
    "Microsoft.Storage/storageAccounts": "Storage",
    "Microsoft.AppConfiguration/configurationStores": "Azure App Configuration",
    "Microsoft.Insights/components": "Application Insights",
    "Microsoft.OperationalInsights/workspaces": "Log Analytics",
}

# Static fallback prices (USD / month) used when the API returns no results
FALLBACK_PRICING: dict[str, dict[str, float]] = {
    "Microsoft.Web/serverfarms": {
        "F1": 0.00, "B1": 13.14, "B2": 26.28, "S1": 69.35,
        "S2": 138.70, "P1v3": 138.70, "P2v3": 277.40, "_default": 69.35,
    },
    "Microsoft.Web/sites": {"_default": 0.00},
    "Microsoft.Storage/storageAccounts": {
        "Standard_LRS": 21.84, "Standard_GRS": 43.69,
        "Standard_ZRS": 27.30, "_default": 21.84,
    },
    "Microsoft.AppConfiguration/configurationStores": {
        "free": 0.00, "standard": 36.50, "_default": 0.00,
    },
    "Microsoft.Insights/components": {"_default": 14.27},
    "Microsoft.OperationalInsights/workspaces": {"_default": 12.41},
    "Microsoft.Authorization/roleAssignments": {"_default": 0.00},
}

# Cache to avoid repeated API calls for the same (service, sku, region)
_price_cache: dict[tuple[str, str, str], float] = {}


def get_azure_price(service_name: str, sku_name: str, region: str = "eastus") -> float | None:
    """Fetch the monthly retail price from the Azure Retail Prices API."""
    cache_key = (service_name, sku_name, region)
    if cache_key in _price_cache:
        return _price_cache[cache_key]

    url = "https://prices.azure.com/api/retail/prices"
    params = {
        "$filter": (
            f"serviceName eq '{service_name}' and skuName eq '{sku_name}' "
            f"and armRegionName eq '{region}' and priceType eq 'Consumption'"
        )
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        if items:
            # Use retailPrice and convert hourly → monthly (730 hrs)
            hourly = items[0]["retailPrice"]
            unit = items[0].get("unitOfMeasure", "")
            if "Hour" in unit:
                price = hourly * 730
            elif "Month" in unit:
                price = hourly
            elif "GB" in unit:
                price = hourly * 5  # assume 5 GB/month for log/insights
            else:
                price = hourly * 730
            _price_cache[cache_key] = price
            return price
    except requests.RequestException as e:
        print(f"⚠️  API call failed for {service_name}/{sku_name}: {e}", file=sys.stderr)
    return None


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
        data = json.load(fh)

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


def estimate_resource_cost(resource_type: str, sku: str, change_type: str, region: str = "eastus") -> float:
    """Return estimated monthly cost delta for a single resource change.

    Tries the Azure Retail Prices API first, falls back to static pricing.
    """
    # Try live API lookup
    service_name = RESOURCE_TYPE_TO_SERVICE.get(resource_type)
    unit_cost = None
    if service_name and sku != "_default":
        unit_cost = get_azure_price(service_name, sku, region)

    # Fall back to static pricing
    if unit_cost is None:
        fallback = FALLBACK_PRICING.get(resource_type, {})
        unit_cost = fallback.get(sku, fallback.get("_default", 0.0))

    if change_type == "Create":
        return unit_cost
    elif change_type == "Delete":
        return -unit_cost
    else:
        return 0.0


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
        cost = estimate_resource_cost(resource_type, sku, change_type)
        total_delta += cost

        rows.append({
            "name": name,
            "type": resource_type,
            "change": change_type,
            "sku": sku,
            "cost": cost,
        })

    # ── Build Markdown ───────────────────────────────────────────────────
    lines = [
        "## 💰 Infrastructure Cost Estimate",
        "",
        "| Resource | Type | Change | SKU | Est. Monthly Cost |",
        "|----------|------|--------|-----|------------------:|",
    ]

    for r in rows:
        label = CHANGE_LABELS.get(r["change"], r["change"])
        cost_str = f"${r['cost']:,.2f}" if r["cost"] >= 0 else f"-${abs(r['cost']):,.2f}"
        lines.append(
            f"| `{r['name']}` | `{r['type']}` | {label} | {r['sku']} | {cost_str} |"
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
    lines.append("*Costs are estimates based on Azure retail pricing as of 2026-04. "
                 "Actual costs may vary.*")

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
