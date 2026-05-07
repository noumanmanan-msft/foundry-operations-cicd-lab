---
mode: agent
description: >
  Create Bicep infrastructure templates for the Azure AI Foundry Operations CI/CD Lab.
  Provisions all required Azure resources for Dev, QA, and Prod environments under
  separate resource groups, using the new Azure AI Foundry resource model (not Hub-based).
---

# Infra Bicep Prompt — Foundry Operations CI/CD Lab

Use the following prompt to generate or regenerate the Bicep infrastructure templates for this lab.

## Prompt

I need Bicep infrastructure-as-code templates to provision all Azure resources required to run the Foundry Operations CI/CD Lab across three isolated environments: **Dev**, **QA**, and **Prod**.

### Deployment Model

- One Bicep template (`infra/bicep/main.bicep`) targeting **subscription scope**, which creates the resource group and then delegates resource creation to a module.
- One resource module (`infra/bicep/modules/resources.bicep`) targeting **resource group scope**, which declares all Azure resources for an environment.
- Three environment-specific parameter files (`infra/bicep/parameters/dev.bicepparam`, `qa.bicepparam`, `prod.bicepparam`) that set only **three user-facing parameters**: `environmentName`, `location`, and `foundryAdminObjectId`.
- All resource names are derived as **variables** inside the templates using `environmentName` and `location` (or a short `locationAbbr` alias). No resource names are hardcoded in the parameter files.

### Azure AI Foundry Resource Model

Use the **new Azure AI Foundry model**, not the old Hub-based model:

- Foundry resource: `Microsoft.CognitiveServices/accounts` with `kind: 'AIFoundry'`
- Foundry project: `Microsoft.MachineLearningServices/workspaces` with `kind: 'Project'` and `hubResourceId` pointing to the Foundry account
- Do **not** create a `Microsoft.MachineLearningServices/workspaces` with `kind: 'Hub'`

### Naming Conventions

All names are computed from `environmentName` and `location` (plus a short `locationAbbr` alias, e.g. `eastus2` → `eus2`).
Replace `<env>` with `dev`, `qa`, or `prod` and `<location>`/`<abbr>` as shown:

| Resource                       | Name Pattern                                          |
|-------------------------------|-------------------------------------------------------|
| Resource Group                | `rg-<env>-foundry-operation-lab-<location>`          |
| Azure AI Foundry               | `aif-<env>-foundry-operation-<location>`             |
| Foundry Project               | `default-<env>-project`                              |
| Storage Account               | `st<env>foundryoplab`                                |
| Key Vault                     | `kv-<env>-foundry-oplab`                             |
| Container Registry            | `acr<env>foundryoplab`                               |
| Log Analytics Workspace       | `law-<env>-foundry-oplab-<locationAbbr>`             |
| Application Insights          | `appi-<env>-foundry-oplab-<locationAbbr>`            |
| Container Apps Environment    | `cae-<env>-foundry-oplab-<locationAbbr>`             |
| Azure AI Search               | `srch-<env>-foundry-oplab-<locationAbbr>`            |
| User-Assigned Managed Identity| `id-<env>-foundry-oplab-<locationAbbr>`              |

### Required Services Per Environment

Provision the following for each environment:

1. **User-Assigned Managed Identity** — used by Foundry and Container Apps workloads
2. **Log Analytics Workspace** — central logging sink for all services
3. **Application Insights** — APM telemetry for agents, connected to Log Analytics
4. **Storage Account** — blob storage required by Foundry; also used by agents for state
5. **Key Vault** — secrets, certificates, API keys; RBAC-authorized (not access policy)
6. **Azure Container Registry** — container images for ACA self-hosted agents
7. **Azure AI Search** — vector index for Foundry IQ / RAG grounding
8. **Azure AI Foundry** (new model) — the core Foundry resource
9. **Foundry Project** — workspace inside the Foundry resource
10. **Azure Container Apps Environment** — hosting environment for self-hosted agents; connected to Log Analytics

### RBAC Assignments

Wire up the following role assignments as part of the Bicep deployment:

- Managed Identity → Foundry resource: `Azure AI Developer`
- Managed Identity → Storage Account: `Storage Blob Data Contributor`
- Managed Identity → Key Vault: `Key Vault Secrets User`
- Managed Identity → ACR: `AcrPull`
- Managed Identity → AI Search: `Search Index Data Contributor`
- Optional parameter `foundryAdminObjectId` → Foundry resource: `Azure AI Developer` (conditional, only if object ID is provided)

### Environment-Specific Differences

Apply these differences based on environment via inline conditionals (not extra parameters):

| Setting                         | Dev        | QA         | Prod     |
|--------------------------------|------------|------------|----------|
| Log Analytics retention (days) | 30         | 60         | 90       |
| ACR SKU                        | Basic      | Basic      | Standard |
| AI Search SKU                  | basic      | basic      | standard |
| Key Vault purge protection     | disabled   | disabled   | enabled  |

### Constraints

- All resources in `eastus2`
- All storage accounts must disable public blob access and enforce TLS 1.2
- Key Vault must use RBAC authorization (not access policies)
- ACR must disable admin user
- Tags applied to every resource: `{ environment, project: 'foundry-operations-cicd-lab', managedBy: 'bicep' }`
- `foundryAdminObjectId` defaults to empty string; skip the role assignment when empty

### Output Format

Produce the following files in order:

1. `infra/bicep/main.bicep`
2. `infra/bicep/modules/resources.bicep`
3. `infra/bicep/parameters/dev.bicepparam`
4. `infra/bicep/parameters/qa.bicepparam`
5. `infra/bicep/parameters/prod.bicepparam`

Each file should be well-commented for clarity. Group resource declarations by category (identity, monitoring, storage, secrets, containers, AI, RBAC) with section headers.
