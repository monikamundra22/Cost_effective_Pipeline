# Cost-Effective Infrastructure Pipelines

Two automated GitHub Actions pipelines that provision Azure infrastructure with **built-in cost estimation and gating** using live pricing from the [Azure Retail Prices API](https://prices.azure.com).

| Pipeline | Workflow | Trigger | Resources |
|----------|----------|---------|-----------|
| **Infrastructure – What-If & Cost Gate** | `infra-cost-gate.yml` | PR on `infra/` files or manual | Web App, Function App, Storage, App Config |
| **VM & APIM – Cost Estimate** | `vm-apim-cost-gate.yml` | Manual (`workflow_dispatch`) | Virtual Machine, API Management |

Both pipelines query the Azure Retail Prices API for live pricing, enforce a configurable cost threshold, and **block deployment** when the threshold is exceeded.

---

## Project Structure

```
Cost_Effective_Pipeline/
├── .github/
│   └── workflows/
│       ├── infra-cost-gate.yml           # Pipeline 1 – Infra What-If & Cost Gate
│       └── vm-apim-cost-gate.yml         # Pipeline 2 – VM & APIM Cost Gate
├── infra/
│   ├── main.bicep                        # Bicep: Web App, Function, Storage, App Config
│   ├── vm-apim.bicep                     # Bicep: VM, VNet, NSG, Public IP, APIM
│   └── parameters/
│       ├── dev.bicepparam
│       ├── staging.bicepparam
│       └── prod.bicepparam
├── scripts/
│   ├── estimate_costs.py                 # Cost script for Pipeline 1
│   ├── estimate_vm_apim_costs.py         # Cost script for Pipeline 2
│   └── sample-what-if-output.json        # Sample what-if data for local testing
└── README.md
```

---

## Pipeline 1 – Infrastructure What-If & Cost Gate

**Workflow:** `.github/workflows/infra-cost-gate.yml`
**Script:** `scripts/estimate_costs.py`
**Template:** `infra/main.bicep`

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              PR on infra/ files  OR  Manual Trigger          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Job 1: What-If & Cost Estimate                             │
│  ├─ az deployment group what-if → JSON diff                 │
│  ├─ estimate_costs.py → query Azure Retail Prices API       │
│  ├─ Generate Markdown cost report                           │
│  ├─ Post report as PR comment                               │
│  └─ EXIT 1 if cost > threshold  ← blocks pipeline          │
└────────────────────────┬────────────────────────────────────┘
                         │ (only if cost check passed)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Job 2: Deploy Infrastructure                               │
│  ├─ az deployment group create                              │
│  └─ Deploys Bicep template to target environment            │
└─────────────────────────────────────────────────────────────┘
```

### Triggers

- **Automatic:** PRs that touch `infra/**`, `scripts/estimate_costs.py`, or the workflow file
- **Manual:** `workflow_dispatch` with `environment` (dev/staging/prod) and `cost_threshold` inputs

### Azure Resources Provisioned

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

### How the Cost Gate Works

1. `az deployment group what-if` produces a JSON diff of resource changes
2. `estimate_costs.py` parses the JSON, queries the Azure Retail Prices API for each resource type + SKU
3. Generates a Markdown cost report (posted as a PR comment on pull requests)
4. If total estimated cost > threshold → script exits with code 1 → **pipeline fails** → deploy job is skipped
5. If cost is within threshold → script exits with code 0 → deploy job proceeds

### Environment Parameter Comparison

| Parameter | Dev | Staging | Prod |
|-----------|-----|---------|------|
| App Service Plan SKU | B1 | S1 | P1v3 |
| Storage SKU | Standard_LRS | Standard_GRS | Standard_ZRS |
| App Config SKU | Free | Standard | Standard |
| Function Runtime | .NET 8 | .NET 8 | .NET 8 |

### Local Testing

```bash
python scripts/estimate_costs.py \
  --what-if-file scripts/sample-what-if-output.json \
  --threshold 500 \
  --output cost-report.md
```

---

## Pipeline 2 – VM & APIM Cost Estimate

**Workflow:** `.github/workflows/vm-apim-cost-gate.yml`
**Script:** `scripts/estimate_vm_apim_costs.py`
**Template:** `infra/vm-apim.bicep`

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Manual Trigger (workflow_dispatch)               │
│  Inputs: vm_size, vm_os, apim_sku, apim_units, region,      │
│          cost_threshold                                      │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Job 1: Estimate VM & APIM Costs                            │
│  ├─ Query Azure Retail Prices API for VM pricing            │
│  ├─ Query Azure Retail Prices API for APIM pricing          │
│  ├─ Generate Markdown cost report                           │
│  └─ EXIT 1 if cost > threshold  ← blocks pipeline          │
└────────────────────────┬────────────────────────────────────┘
                         │ (only if cost check passed)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Job 2: Deploy VM & APIM                                    │
│  ├─ az deployment group create                              │
│  └─ Deploys VM, VNet, NSG, Public IP, NIC, and APIM        │
└─────────────────────────────────────────────────────────────┘
```

### Inputs

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `vm_size` | choice | `Standard_B2s` | Azure VM size (B1s, B2s, D2s_v3, etc.) |
| `vm_os` | choice | `linux` | Operating system (linux / windows) |
| `apim_sku` | choice | `Developer` | APIM tier (Consumption, Developer, Basic, Standard, Premium, BasicV2, StandardV2) |
| `apim_units` | string | `1` | Number of APIM scale units |
| `region` | choice | `eastus` | Azure region |
| `cost_threshold` | string | `500` | Monthly cost threshold in USD. `0` = block all. |

### Azure Resources Provisioned

| Resource | Bicep Type | Purpose |
|----------|-----------|---------|
| Network Security Group | `Microsoft.Network/networkSecurityGroups` | NSG with SSH/RDP rules |
| Virtual Network + Subnet | `Microsoft.Network/virtualNetworks` | Isolated networking |
| Public IP Address | `Microsoft.Network/publicIPAddresses` | VM public access |
| Network Interface | `Microsoft.Network/networkInterfaces` | VM NIC |
| Virtual Machine | `Microsoft.Compute/virtualMachines` | Linux (Ubuntu 22.04) or Windows Server 2022 |
| API Management | `Microsoft.ApiManagement/service` | API gateway |

### How the Cost Gate Works

1. `estimate_vm_apim_costs.py` queries the Azure Retail Prices API for VM and APIM pricing based on the selected size/SKU/region
2. Generates a Markdown cost report with per-component breakdown
3. If total estimated cost > threshold → script exits with code 1 → **pipeline fails** → deploy job is skipped
4. If cost is within threshold → script exits with code 0 → deploy job proceeds

### Local Testing

```bash
# Cost within threshold (passes)
python scripts/estimate_vm_apim_costs.py \
  --vm-size Standard_B2s \
  --vm-os linux \
  --apim-sku Developer \
  --apim-units 1 \
  --region eastus \
  --threshold 500 \
  --output vm-apim-cost-report.md

# Cost exceeds threshold (fails with exit code 1)
python scripts/estimate_vm_apim_costs.py \
  --vm-size Standard_D4s_v5 \
  --vm-os windows \
  --apim-sku Standard \
  --apim-units 2 \
  --region eastus \
  --threshold 50 \
  --output vm-apim-cost-report.md
```

---

## Setup Instructions

### 1. Azure Prerequisites

```bash
# Create resource groups
az group create --name rg-costpipe-dev --location eastus

# Create an Azure AD App Registration for OIDC
az ad app create --display-name "github-infra-pipeline"

# Configure federated credentials for GitHub Actions
# See: https://learn.microsoft.com/en-us/azure/developer/github/connect-from-azure
```

### 2. GitHub Repository Secrets

Configure these secrets in **Settings → Secrets and variables → Actions**:

| Secret | Required By | Description |
|--------|------------|-------------|
| `AZURE_CLIENT_ID` | Both pipelines | App Registration (service principal) client ID |
| `AZURE_TENANT_ID` | Both pipelines | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Both pipelines | Target Azure subscription ID |
| `VM_ADMIN_PASSWORD_OR_KEY` | Pipeline 2 only | VM admin password (must meet Azure complexity: 12+ chars, uppercase, lowercase, number, special char) |
| `APIM_PUBLISHER_EMAIL` | Pipeline 2 only | Publisher email for API Management |

### 3. Branch Protection (Optional – Pipeline 1)

To enforce the cost gate on PRs:
1. Go to **Settings → Branches → Add rule**
2. Enable **Require status checks to pass before merging**
3. Search for and select **"What-If & Cost Estimate"**

### 4. Configure Cost Threshold

The default threshold for both pipelines is **$500/month**. Override it by:
- Using the `workflow_dispatch` trigger with a custom `cost_threshold` value
- Setting it to `0` to block all deployments (zero tolerance)
