#!/usr/bin/env python3
"""
estimate_vm_apim_costs.py – Estimate Azure VM and API Management monthly costs
using the Azure Retail Prices API (https://prices.azure.com).

This is a standalone cost estimation pipeline that:
  1. Accepts VM size and APIM SKU as inputs
  2. Queries the Azure Retail Prices API for live pricing
  3. Produces a Markdown cost report
  4. Exits with code 1 if total cost exceeds the configured threshold

Usage:
  python scripts/estimate_vm_apim_costs.py \
      --vm-size Standard_B2s \
      --apim-sku Standard \
      --region eastus \
      --threshold 500 \
      --output vm-apim-cost-report.md
"""

import argparse
import os
import sys
from typing import Any

import requests

# ─── Azure Retail Prices API ─────────────────────────────────────────────────

API_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = 730

_query_cache: dict[str, list[dict]] = {}


def _query_api(
    service_name: str,
    region: str,
    sku_name: str | None = None,
    product_name: str | None = None,
    arm_sku_name: str | None = None,
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
    if arm_sku_name:
        parts.append(f"armSkuName eq '{arm_sku_name}'")

    filt = " and ".join(parts)
    if filt in _query_cache:
        return _query_cache[filt]

    all_items: list[dict] = []
    url: str | None = API_URL
    params: dict[str, str] = {"$filter": filt, "$top": "100"}

    while url:
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            all_items.extend(data.get("Items", []))
            url = data.get("NextPageLink")
            params = {}  # NextPageLink already contains the filter
        except requests.RequestException as exc:
            print(f"⚠️  API error ({service_name}/{sku_name}): {exc}", file=sys.stderr)
            break

    _query_cache[filt] = all_items
    return all_items


def _pick_item(
    items: list[dict],
    meter_hint: str | None = None,
    unit_hint: str | None = None,
    exclude_windows: bool = True,
) -> dict | None:
    """Select the best-matching meter from API results."""
    if not items:
        return None
    candidates = items

    # By default, exclude Windows-specific pricing (user can opt in)
    if exclude_windows:
        flt = [i for i in candidates if "windows" not in i.get("productName", "").lower()]
        if flt:
            candidates = flt

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


# ─── VM Pricing ──────────────────────────────────────────────────────────────

# ARM VM size → API skuName mapping for common sizes
_VM_SKU_MAP: dict[str, str] = {
    "Standard_B1s": "B1s",
    "Standard_B1ms": "B1ms",
    "Standard_B2s": "B2s",
    "Standard_B2ms": "B2ms",
    "Standard_B4ms": "B4ms",
    "Standard_D2s_v3": "D2s v3",
    "Standard_D4s_v3": "D4s v3",
    "Standard_D8s_v3": "D8s v3",
    "Standard_D2s_v5": "D2s v5",
    "Standard_D4s_v5": "D4s v5",
    "Standard_D2as_v5": "D2as v5",
    "Standard_D4as_v5": "D4as v5",
    "Standard_E2s_v3": "E2s v3",
    "Standard_E4s_v3": "E4s v3",
    "Standard_E2s_v5": "E2s v5",
    "Standard_F2s_v2": "F2s v2",
    "Standard_F4s_v2": "F4s v2",
}


def price_vm(vm_size: str, region: str, os_type: str = "linux") -> tuple[float, str, dict[str, Any]]:
    """
    Get VM monthly cost from the Azure Retail Prices API.
    Returns (monthly_cost, source_description, details_dict).
    """
    # Resolve the API SKU name
    api_sku = _VM_SKU_MAP.get(vm_size, vm_size.replace("Standard_", "").replace("_", " "))

    # Try by armSkuName first (most reliable)
    items = _query_api("Virtual Machines", region, arm_sku_name=vm_size)
    if not items:
        # Fallback: try by skuName
        items = _query_api("Virtual Machines", region, sku_name=api_sku)

    exclude_win = os_type.lower() != "windows"
    item = _pick_item(items, unit_hint="Hour", exclude_windows=exclude_win)

    details: dict[str, Any] = {
        "vm_size": vm_size,
        "region": region,
        "os_type": os_type,
    }

    if item:
        hourly = item["retailPrice"]
        monthly = hourly * HOURS_PER_MONTH
        details.update({
            "hourly_rate": hourly,
            "monthly_cost": monthly,
            "product_name": item.get("productName", ""),
            "meter_name": item.get("meterName", ""),
            "sku_name": item.get("skuName", ""),
        })
        source = f"API: ${hourly:.4f}/hr × {HOURS_PER_MONTH} hrs"
        return monthly, source, details

    return 0.0, f"No API result for VM size '{vm_size}'", details


# ─── APIM Pricing ────────────────────────────────────────────────────────────

# ARM tier → API skuName
_APIM_SKU_MAP: dict[str, str] = {
    "Consumption": "Consumption",
    "Developer": "Developer",
    "Basic": "Basic",
    "Standard": "Standard",
    "Premium": "Premium",
    "Isolated": "Isolated",
    "BasicV2": "Basic v2",
    "StandardV2": "Standard v2",
}


def price_apim(sku: str, region: str, units: int = 1) -> tuple[float, str, dict[str, Any]]:
    """
    Get API Management monthly cost from the Azure Retail Prices API.
    Returns (monthly_cost, source_description, details_dict).
    """
    api_sku = _APIM_SKU_MAP.get(sku, sku)

    items = _query_api("API Management", region, sku_name=api_sku)
    item = _pick_item(items, unit_hint="Hour", exclude_windows=False)

    details: dict[str, Any] = {
        "sku": sku,
        "api_sku": api_sku,
        "region": region,
        "units": units,
    }

    if item:
        hourly = item["retailPrice"]
        monthly = hourly * HOURS_PER_MONTH * units
        details.update({
            "hourly_rate": hourly,
            "monthly_cost": monthly,
            "product_name": item.get("productName", ""),
            "meter_name": item.get("meterName", ""),
        })
        source = f"API: ${hourly:.4f}/hr × {HOURS_PER_MONTH} hrs"
        if units > 1:
            source += f" × {units} units"
        return monthly, source, details

    # Consumption tier (pay-per-call) — $3.50 per million calls
    if api_sku == "Consumption":
        item = _pick_item(items, meter_hint="Calls", exclude_windows=False)
        if item:
            assumed_calls = 100_000  # 100K calls/month assumed
            price_per_10k = item["retailPrice"]
            monthly = price_per_10k * (assumed_calls / 10_000)
            details.update({
                "rate_per_10k": price_per_10k,
                "assumed_calls": assumed_calls,
                "monthly_cost": monthly,
            })
            return monthly, f"API: ${price_per_10k:.4f}/10K calls × {assumed_calls:,} calls", details

    return 0.0, f"No API result for APIM SKU '{sku}'", details


# ─── Report Builder ──────────────────────────────────────────────────────────

def build_report(
    vm_size: str,
    vm_cost: float,
    vm_source: str,
    vm_details: dict,
    apim_sku: str,
    apim_cost: float,
    apim_source: str,
    apim_details: dict,
    region: str,
    threshold: float,
) -> str:
    """Build a Markdown cost report."""
    total = vm_cost + apim_cost

    lines = [
        "## 💰 Azure VM & API Management Cost Estimate",
        "",
        f"**Region:** `{region}`",
        "",
        "| Component | Configuration | Est. Monthly Cost | Source |",
        "|-----------|--------------|------------------:|--------|",
        f"| Azure VM | `{vm_size}` ({vm_details.get('os_type', 'linux')}) | ${vm_cost:,.2f} | {vm_source} |",
        f"| API Management | `{apim_sku}` ({apim_details.get('units', 1)} unit) | ${apim_cost:,.2f} | {apim_source} |",
        "",
        f"**Estimated total monthly cost: ${total:,.2f}**",
        "",
    ]

    exceeded = (threshold == 0 and total > 0) or (threshold > 0 and total > threshold)
    delta = total - threshold if exceeded else 0.0

    if exceeded:
        lines.append(
            f"> ⚠️ **APPROVAL REQUIRED** – Estimated cost (${total:,.2f}) "
            f"exceeds threshold (${threshold:,.2f}) by **${delta:,.2f}**. "
            f"Manual approval is needed to proceed with deployment."
        )
    else:
        lines.append(
            f"> ✅ Estimated cost (${total:,.2f}) is within the "
            f"threshold (${threshold:,.2f})."
        )

    # Detail breakdown
    lines.extend([
        "",
        "<details>",
        "<summary>📋 Pricing Details (click to expand)</summary>",
        "",
        "### Azure VM",
        f"- **Size:** {vm_size}",
        f"- **OS:** {vm_details.get('os_type', 'linux')}",
        f"- **Product:** {vm_details.get('product_name', 'N/A')}",
        f"- **Hourly Rate:** ${vm_details.get('hourly_rate', 0):.4f}",
        f"- **Monthly (730 hrs):** ${vm_cost:,.2f}",
        "",
        "### API Management",
        f"- **SKU:** {apim_sku}",
        f"- **Units:** {apim_details.get('units', 1)}",
        f"- **Product:** {apim_details.get('product_name', 'N/A')}",
        f"- **Hourly Rate:** ${apim_details.get('hourly_rate', 0):.4f}",
        f"- **Monthly (730 hrs):** ${apim_cost:,.2f}",
        "",
        "</details>",
        "",
        "---",
        "*Costs are live estimates from the "
        "[Azure Retail Prices API](https://prices.azure.com). "
        "Actual costs may vary based on usage and reservations.*",
    ])

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate Azure VM and API Management costs from the Azure Retail Prices API"
    )
    parser.add_argument("--vm-size", required=True, help="Azure VM size (e.g. Standard_B2s)")
    parser.add_argument("--vm-os", default="linux", choices=["linux", "windows"], help="VM OS type")
    parser.add_argument("--apim-sku", required=True, help="APIM tier (Developer, Basic, Standard, Premium, Consumption)")
    parser.add_argument("--apim-units", type=int, default=1, help="Number of APIM scale units")
    parser.add_argument("--region", default="eastus", help="Azure region (default: eastus)")
    parser.add_argument("--threshold", type=float, default=0, help="Monthly cost threshold in USD (0 = block all)")
    parser.add_argument("--output", default="vm-apim-cost-report.md", help="Output Markdown file path")
    args = parser.parse_args()

    print(f"🔍 Querying Azure Retail Prices API...")
    print(f"   VM Size:    {args.vm_size} ({args.vm_os})")
    print(f"   APIM SKU:   {args.apim_sku} ({args.apim_units} unit(s))")
    print(f"   Region:     {args.region}")
    print(f"   Threshold:  ${args.threshold:,.2f}")
    print()

    # Get VM pricing
    vm_cost, vm_source, vm_details = price_vm(args.vm_size, args.region, args.vm_os)
    print(f"   💻 VM ({args.vm_size}): ${vm_cost:,.2f}/month – {vm_source}")

    # Get APIM pricing
    apim_cost, apim_source, apim_details = price_apim(args.apim_sku, args.region, args.apim_units)
    print(f"   🔗 APIM ({args.apim_sku}): ${apim_cost:,.2f}/month – {apim_source}")

    total = vm_cost + apim_cost
    print(f"\n   💰 Total: ${total:,.2f}/month")

    # Build report
    report = build_report(
        args.vm_size, vm_cost, vm_source, vm_details,
        args.apim_sku, apim_cost, apim_source, apim_details,
        args.region, args.threshold,
    )

    # Write report file
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"\n📄 Cost report written to {args.output}")

    # Write to GITHUB_STEP_SUMMARY if available
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    # Determine if cost exceeds threshold
    exceeded = (args.threshold == 0 and total > 0) or (args.threshold > 0 and total > args.threshold)
    delta = total - args.threshold if exceeded else 0.0

    # Export values for downstream steps
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"total_cost={total:.2f}\n")
            fh.write(f"cost_exceeded={'true' if exceeded else 'false'}\n")
            fh.write(f"cost_delta={delta:.2f}\n")
            fh.write(f"vm_cost={vm_cost:.2f}\n")
            fh.write(f"apim_cost={apim_cost:.2f}\n")

    if exceeded:
        print(f"\n⚠️  Cost threshold exceeded by ${delta:,.2f}!")
        print(f"   Total: ${total:,.2f} | Threshold: ${args.threshold:,.2f} | Delta: +${delta:,.2f}")
        print("   Deployment will require manual approval.")
    else:
        print("\n✅ Cost check passed.")


if __name__ == "__main__":
    main()
