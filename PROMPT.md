# Microsoft Foundry Operations CI/CD Demo Prompt

> **Scope:** This prompt covers the **operationalization** of Microsoft Foundry across Dev, QA, and Prod environments. It assumes infrastructure (resource groups, Foundry resources, projects, and supporting services) is already provisioned. For infrastructure-as-code templates, see [prompts/infra-bicep.prompt.md](prompts/infra-bicep.prompt.md).

Use the following prompt as the baseline brief for designing and building this demo:

## Prompt

I want to create a realistic demo that showcases how to operationalize Microsoft Foundry across the full application and platform lifecycle.

The demo models an enterprise setup with three isolated, pre-provisioned environments:

- Dev
- QA
- Prod

Each environment has its own Microsoft Foundry resource and its own Foundry project. The infrastructure is already in place. The focus is on how teams manage, configure, validate, evaluate, and promote Foundry workloads across those environments through repeatable, automated operations.

The solution should demonstrate how teams manage and promote the following Foundry capabilities across environments:

- Models and model deployments
- Agents (Foundry-hosted and ACA self-hosted)
- Foundry IQ (knowledge indexes, grounding data)
- Memory (agent state and session storage)
- Tools (function calling, MCP-connected tools)
- Guardrails (content safety, input/output policies)
- Evaluations (batch eval, metrics, quality gates)
- Configuration and environment-specific settings

The demo must cover both hosting patterns for agents:

- Foundry-hosted agents
- Self-hosted agents running in Azure Container Apps

Design this as an operations-focused demo, not just a feature demo. Show how Foundry workloads are configured, validated, secured, monitored, and promoted through CI/CD from Dev to QA to Prod, with clear governance and control at each stage.

## What I Need

Produce a complete operationalization plan and reference workflows for this demo, including:

1. An operational environment topology showing how Dev, QA, and Prod differ in configuration, access control, and promotion gates.
2. A CI/CD strategy for promoting Foundry assets across environments, including model deployments, agent definitions, prompts, guardrail policies, knowledge indexes, and evaluation datasets.
3. A clear distinction between what is managed directly in Foundry and what is self-hosted in Azure Container Apps, and how each is promoted.
4. Guidance for handling configuration drift, promotion approvals, secret rotation, identity federation, RBAC, and environment isolation.
5. Observability and operational governance, including logging, tracing, monitoring, evaluation trending, and rollback strategy.
6. A suggested repository structure for application code, agent definitions, prompts, evaluations, guardrail configs, and deployment pipelines.
7. Example end-to-end promotion flow for:
   - Model deployment configuration changes
   - Agent definition and system prompt updates
   - Guardrail policy changes
   - Evaluation dataset updates and quality gate checks
   - Promotion approval from QA to Prod with validation checkpoints
8. Recommendations for demo data, sample use cases, and storyline so the operational value is obvious to an enterprise audience.
9. Risks, tradeoffs, and assumptions.

## Constraints And Expectations

- Infrastructure is already provisioned. Do not include steps to create or tear down resource groups, Foundry resources, projects, or any Azure services.
- Assume this is a demo lab, but keep the design credible for real enterprise operating practices.
- Prefer repeatable, automation-first workflows over manual portal operations.
- Use environment separation and least-privilege access throughout.
- Make it easy to explain to both platform engineers and application teams.
- Include opinionated recommendations when multiple valid choices exist.

## Desired Output Format

Structure the response with the following sections:

1. Executive summary
2. Demo goals and storyline
3. Operational environment topology: Dev, QA, Prod
4. Foundry-hosted vs ACA-hosted agent operational pattern
5. CI/CD and promotion workflow
6. Configuration and drift management
7. Security and governance model
8. Observability and evaluation strategy
9. Repository and code organization
10. Step-by-step operationalization plan
11. Demo script for presenting the scenario
12. Risks and next decisions
