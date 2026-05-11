# Archived Foundry assets

This directory holds files that were removed from active use during the dev → QA → prod multi-KB schema migration in May 2026. They are kept for historical reference and possible future resurrection, not for deployment.

## Contents

### agent-1
Sample/demo agent that was never wired into deployment workflows. Used legacy singular knowledgeRef/knowledgeIndexRef/foundryIqRef schema. Archived because no workflow, script, or doc references it.

### incident-triage-aca
ACA-hosted variant of the incident-triage agent. The README mentions ACA-hosted as an intended pattern, but no workflow ever deployed this manifest. Archived because no workflow, script, or doc references it. Resurrect if/when ACA agent deployment is actually implemented.

### operations-foundry-iq.json (and related v1-singular files)
Legacy single-KB-era files that the agent-level knowledgeRef/knowledgeIndexRef pointed to. Replaced by per-KB foundry-iq files (kb-iq-v1-foundry-iq.json, kb-iq-v2-foundry-iq.json) under foundry/foundry-iq/.
