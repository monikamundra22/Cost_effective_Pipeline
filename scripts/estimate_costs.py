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

# ─── Approximate Azure retail prices (USD / month) ──────────────────────────
# These are *rough estimates* – the real pipeline can call the Azure Retail
# Prices API for exact numbers. This lookup keeps the script dependency-free.

PRICING: dict[str, dict[str, float]] = {
    # App Service Plans  (Linux, monthly estimate)
    "Microsoft.Web/serverfarms": {
        "F1": 0.00,
        "B1": 13.14,
        "B2": 26.28,
        "S1": 69.35,
        "S2": 138.70,
        "P1v3": 138.70,
        "P2v3": 277.40,
        "_default": 69.35,
    },
    # Web Apps & Function Apps – cost is driven by the plan; the site
    # resource itself is free, but we tag a fixed ops overhead.
    "Microsoft.Web/sites": {
        "_default": 0.00,
    },
    # Storage Accounts
    "Microsoft.Storage/storageAccounts": {
        "Standard_LRS": 21.84,
        "Standard_GRS": 43.69,
        "Standard_ZRS": 27.30,
        "_default": 21.84,
    },
    # App Configuration
    "Microsoft.AppConfiguration/configurationStores": {
        "free": 0.00,
        "standard": 36.50,
        "_default": 0.00,
    },
    # Application Insights (per-GB ingestion – assume 5 GB / month)
    "Microsoft.Insights/components": {
        "_default": 14.27,
    },
    # Log Analytics Workspace (per-GB – assume 5 GB / month)
    "Microsoft.OperationalInsights/workspaces": {
        "_default": 12.41,
    },
    # Role Assignments – no cost
    "Microsoft.Authorization/roleAssignments": {
        "_default": 0.00,
    },
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


def estimate_resource_cost(resource_type: str, sku: str, change_type: str) -> float:
    """Return estimated monthly cost delta for a single resource change."""
    prices = PRICING.get(resource_type, {})
    unit_cost = prices.get(sku, prices.get("_default", 0.0))

    if change_type == "Create":
        return unit_cost
    elif change_type == "Delete":
        return -unit_cost
    else:
        # Modify, NoChange, etc. – no *new* cost delta
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
        lines.append("> ℹ️ No cost threshold configured – report is informational only.")

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
            blocked = "true" if (args.threshold > 0 and total > args.threshold) else "false"
            fh.write(f"cost_exceeded={blocked}\n")

    # Exit with failure if over threshold (to block the PR)
    if args.threshold > 0 and total > args.threshold:
        print(f"⛔ Cost threshold exceeded! ${total:,.2f} > ${args.threshold:,.2f}")
        sys.exit(1)

    print("✅ Cost check passed.")


if __name__ == "__main__":
    main()
