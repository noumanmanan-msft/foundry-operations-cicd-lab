# Microsoft Foundry Operationalization Plan

## 1. Executive Summary

This repository operationalizes Microsoft Foundry across Dev, QA, and Prod by treating Foundry assets as versioned source artifacts. The operating model separates infrastructure provisioning from workload promotion. Environment-specific configuration lives in dedicated manifests, while shared Foundry assets such as prompts, agents, guardrails, evaluations, memory settings, and tool contracts are promoted through GitHub Actions.

The core demo narrative is simple: application teams make changes in Dev, platform controls validate those changes, and GitHub workflows promote the exact same asset bundle to QA and Prod with tighter approvals and stricter quality gates.

## 2. Demo Goals And Storyline

The demo shows an enterprise platform team standardizing how AI workloads move through environments without relying on manual portal changes.

Storyline:

1. A team updates an incident triage agent prompt and adds a new MCP-backed tool in Dev.
2. GitHub Actions validates the prompt, guardrails, evaluation thresholds, and environment overlays.
3. A Dev bundle is rendered from source control and published as the promotion artifact.
4. QA promotion consumes the same commit, applies QA-specific configuration, and requires approval.
5. Prod promotion repeats the same pattern with stricter approval and quality settings.

## 3. Operational Environment Topology: Dev, QA, Prod

### Dev

1. Fastest iteration path.
2. Lower-cost SKUs and relaxed deployment cadence.
3. Automatic deployment from the main branch after validation.

### QA

1. Mirrors production integration patterns.
2. Uses approval before deployment.
3. Enforces evaluation gates before promotion to Prod.

### Prod

1. Locked-down environment with manual promotion.
2. Higher data retention, stricter Key Vault settings, and stronger governance.
3. Release only from previously validated source.

## 4. Foundry-Hosted Vs ACA-Hosted Agent Operational Pattern

### Foundry-hosted agent

1. Managed as declarative agent metadata.
2. Promoted by updating agent definition, prompt reference, tool bindings, and guardrails.
3. Best for fast platform-managed orchestration and lower operational burden.

### ACA-hosted agent

1. Managed as a container deployment manifest plus runtime configuration.
2. Promoted by building a container image in Dev, then promoting the same image reference and configuration overlays across environments.
3. Best for custom runtime control, external dependencies, or non-native execution stacks.

## 5. CI/CD And Promotion Workflow

1. Pull request validation checks syntax, references, and renderability of all Foundry assets.
2. Push to main renders the Dev bundle and prepares the deployment payload.
3. QA workflow promotes the same source revision into QA with environment approval.
4. Prod workflow promotes the same source revision into Prod with stricter approval.
5. Every promotion uses environment-specific settings from versioned manifests instead of manual changes.

## 6. Configuration And Drift Management

1. Shared assets live under `foundry/`.
2. Environment drift is controlled by explicit config files under `environments/`.
3. The render script builds a resolved bundle per environment so differences are reviewable.
4. GitHub Actions validates that all referenced prompts, tools, indexes, guardrails, and evaluation files exist.

## 7. Security And Governance Model

1. Secrets are referenced by Key Vault URI and secret names, not embedded in source.
2. GitHub environment approvals gate QA and Prod promotion.
3. RBAC is assumed to be pre-provisioned; workflows only consume environment identities.
4. Guardrails and evaluation thresholds are source-controlled and promoted like code.

## 8. Observability And Evaluation Strategy

1. Application Insights and Log Analytics remain the operational telemetry sinks.
2. Evaluation datasets are versioned alongside prompts and agent definitions.
3. Quality gates define minimum pass thresholds for promotion.
4. Release bundles capture the source commit, asset set, and target environment.

## 9. Repository And Code Organization

Key folders:

1. `foundry/`: shared Foundry workload definitions.
2. `environments/`: Dev, QA, and Prod overlays.
3. `scripts/`: validation and render tooling used locally and in CI.
4. `.github/workflows/`: validation and promotion pipelines.
5. `docs/`: operational plan and demo narrative.

## 10. Step-By-Step Operationalization Plan

1. Define shared models, prompts, agents, tools, memory, guardrails, and evaluation datasets under `foundry/`.
2. Record environment-specific endpoints, resource identifiers, and policy flags under `environments/`.
3. Validate assets in pull requests.
4. Render an environment bundle from a single source revision.
5. Deploy Dev automatically after merge.
6. Promote the same revision to QA through environment approval.
7. Promote the same revision to Prod through a stronger approval boundary.

## 11. Demo Script For Presenting The Scenario

1. Open a prompt file and change the incident agent behavior.
2. Show the PR validation workflow catching any missing references or malformed config.
3. Merge to main and show the Dev workflow rendering a bundle.
4. Run the QA promotion workflow and show that the same bundle structure is rendered with QA values.
5. Repeat for Prod, highlighting the environment approval step and stricter release gate.

## 12. Risks And Next Decisions

1. The repo currently models the operational contract and release mechanics, not the final live Foundry deployment commands.
2. The next decision is whether to implement direct Foundry deployment calls inside the workflows or keep deployment execution in a separate secure runner.
3. Container-based agent image build and publish can be added once the ACA agent runtime code is introduced.