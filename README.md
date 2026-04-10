# Cost-Effective Infrastructure Pipeline

An automated GitHub Actions pipeline that provisions Azure infrastructure (Web App, Function App, Storage Account, App Configuration) with **built-in cost estimation and gating** on pull requests.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Pull Request                       │
│          (changes to infra/ files trigger pipeline)         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  1. Azure What-If Analysis                                  │
│     az deployment group what-if                             │
│     → Outputs JSON describing resource changes              │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  2. Cost Estimation Script                                  │
│     scripts/estimate_costs.py                               │
│     → Parses what-if JSON, maps SKUs to prices              │
│     → Generates Markdown cost report                        │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  3. PR Comment & Gate                                       │
│     → Posts cost report as PR comment                       │
│     → Blocks PR if cost exceeds threshold (configurable)    │
└─────────────────────────────────────────────────────────────┘
```

## Azure Resources Provisioned

| Resource | Bicep Type | Purpose |
|----------|-----------|---------|
| Log Analytics Workspace | `Microsoft.OperationalInsights/workspaces` | Centralized logging |
| Application Insights | `Microsoft.Insights/components` | Application telemetry |
| Storage Account (primary) | `Microsoft.Storage/storageAccounts` | Application data |
| Storage Account (functions) | `Microsoft.Storage/storageAccounts` | Function App runtime storage |
| App Service Plan | `Microsoft.Web/serverfarms` | Hosting plan (Linux) |
| Web App | `Microsoft.Web/sites` | Main web application |
| Function App | `Microsoft.Web/sites` | Serverless functions |
| App Configuration | `Microsoft.AppConfiguration/configurationStores` | Centralized configuration |

## Project Structure

```
Cost_Effective_Pipeline/
├── .github/
│   └── workflows/
│       └── infra-cost-gate.yml        # GitHub Actions pipeline
├── infra/
│   ├── main.bicep                     # Bicep template (all resources)
│   └── parameters/
│       ├── dev.bicepparam             # Dev environment parameters
│       ├── staging.bicepparam         # Staging environment parameters
│       └── prod.bicepparam            # Prod environment parameters
├── scripts/
│   ├── estimate_costs.py             # Cost estimation script
│   └── sample-what-if-output.json    # Sample what-if output for testing
└── README.md
```

## Pipeline Flow

### Step 1 – Automatic Trigger
The pipeline runs automatically when a PR is opened (or updated) that modifies any file under `infra/`, the cost script, or the workflow itself.

### Step 2 – Azure What-If
Runs `az deployment group what-if` against the target environment. This produces a JSON diff showing which resources will be **Created**, **Modified**, **Deleted**, or left **Unchanged**.

### Step 3 – Cost Estimation
The Python script (`scripts/estimate_costs.py`) parses the what-if JSON, maps each resource type + SKU to approximate Azure retail prices, and calculates the estimated monthly cost delta.

### Step 4 – PR Comment
The pipeline posts (or updates) a comment on the PR containing:
- The full what-if output (collapsed)
- A cost table with per-resource breakdown
- Total monthly cost delta
- Pass/fail status against the threshold

### Step 5 – Cost Gate
If the estimated cost exceeds the configured threshold, the pipeline **fails**, blocking the PR from merging (when branch protection rules require this check to pass).

## Setup Instructions

### 1. Azure Prerequisites

```bash
# Create a resource group
az group create --name rg-costpipe-dev --location eastus

# Create an Azure AD App Registration for OIDC (no secret needed)
az ad app create --display-name "github-infra-pipeline"

# Configure federated credentials for GitHub Actions
# See: https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure
```

### 2. GitHub Repository Secrets

Configure these secrets in **Settings → Secrets and variables → Actions**:

| Secret | Description |
|--------|-------------|
| `AZURE_CLIENT_ID` | App Registration (service principal) client ID |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Target Azure subscription ID |

### 3. Branch Protection (Optional)

To enforce the cost gate, add a branch protection rule on `main`:
1. Go to **Settings → Branches → Add rule**
2. Enable **Require status checks to pass before merging**
3. Search for and select **"What-If & Cost Estimate"**

### 4. Configure Cost Threshold

The default threshold is **$500/month**. Override it by:
- Editing the `COST_THRESHOLD` env variable in the workflow
- Using the manual `workflow_dispatch` trigger with a custom value
- Setting it to `0` to disable the gate (report-only mode)

## Environment Parameter Comparison

| Parameter | Dev | Staging | Prod |
|-----------|-----|---------|------|
| App Service Plan SKU | B1 ($13/mo) | S1 ($69/mo) | P1v3 ($139/mo) |
| Storage SKU | Standard_LRS | Standard_GRS | Standard_ZRS |
| App Config SKU | Free | Standard | Standard |
| Function Runtime | .NET 8 | .NET 8 | .NET 8 |

## Local Testing

Test the cost estimation script locally with the sample what-if output:

```bash
python scripts/estimate_costs.py \
  --what-if-file scripts/sample-what-if-output.json \
  --threshold 500 \
  --output cost-report.md

cat cost-report.md
```

## Customization

### Adding More Resources
1. Add the resource to `infra/main.bicep`
2. Add pricing for the resource type in `scripts/estimate_costs.py` → `PRICING` dict
3. Update parameter files as needed

### Using Azure Retail Prices API
For production accuracy, replace the static `PRICING` lookup with calls to the [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices):

```python
import requests

def get_azure_price(service_name: str, sku_name: str, region: str = "eastus") -> float:
    url = "https://prices.azure.com/api/retail/prices"
    params = {
        "$filter": f"serviceName eq '{service_name}' and skuName eq '{sku_name}' and armRegionName eq '{region}' and priceType eq 'Consumption'"
    }
    resp = requests.get(url, params=params)
    items = resp.json().get("Items", [])
    return items[0]["retailPrice"] if items else 0.0
```
