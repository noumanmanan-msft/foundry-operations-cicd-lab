#!/usr/bin/env python3
"""
Deploy (create or update) a Foundry agent and all connected assets to a target environment.

Reads agent definitions from the foundry/ directory (the source of truth in this repo)
and calls the Azure AI Foundry API to create or update the agent in the target project.

This is what the CI/CD pipeline calls — not the portal. Definitions live in code.

Usage:
  python scripts/deploy_agent_to_foundry.py \
      --agent-name incident-triage-hosted \
      --environment qa \
      --version v1.2.0

Prerequisites:
  pip install azure-ai-projects azure-identity
  az login   (or OIDC: set AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_SUBSCRIPTION_ID)
"""

import argparse
import json
import sys
from pathlib import Path


def build_project_endpoint(config: dict) -> str:
    """Build the Foundry project-scoped URL from config.json."""
    account = config["foundry"]["accountName"]
    project_name = config["foundry"]["projectId"].split("/projects/")[-1]
    return f"https://{account}.services.ai.azure.com/api/projects/{project_name}"


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def resolve_agent_file(repo_root: Path, agent_name: str) -> Path:
    """Find the agent JSON file whose 'name' field matches agent_name."""
    for candidate in sorted((repo_root / "foundry" / "agents").rglob("*.json")):
        try:
            data = load_json(candidate)
            if data.get("name") == agent_name:
                return candidate
        except Exception:
            continue
    raise FileNotFoundError(
        f"No agent definition with name='{agent_name}' found under foundry/agents/. "
        "Check the 'name' field in your agent JSON files."
    )


def find_search_connection(connections_client):
    """Return the first Azure AI Search / Cognitive Search connection, or None."""
    try:
        for conn in connections_client.list():
            props = getattr(conn, "properties", None)
            category = getattr(props, "category", "") if props else ""
            if "search" in category.lower() or "cognitive" in category.lower():
                return conn
    except Exception as exc:
        print(f"[deploy] Warning: could not list connections: {exc}", file=sys.stderr)
    return None


def find_existing_agent(agents_client, agent_name: str):
    """Return the agent object if it already exists in the project, else None."""
    list_fn = getattr(agents_client, "list", None) or getattr(agents_client, "list_agents", None)
    if list_fn is None:
        print(
            "[deploy] Warning: agents client does not expose list/list_agents.",
            file=sys.stderr,
        )
        return None

    try:
        for agent in list_fn():
            if agent.name == agent_name:
                return agent
    except Exception as exc:
        print(f"[deploy] Warning: could not list agents: {exc}", file=sys.stderr)
    return None


def deploy_agent(agent_name: str, environment: str, version: str | None) -> dict:
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import AzureAISearchTool
    from azure.identity import DefaultAzureCredential

    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "environments" / environment / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Environment config not found: {config_path}")

    config = load_json(config_path)
    endpoint = build_project_endpoint(config)
    model_deployment = config["foundry"]["defaultModelDeployment"]

    # --- Load agent definition and all referenced assets ---
    agent_file = resolve_agent_file(repo_root, agent_name)
    agent_def = load_json(agent_file)
    print(f"[deploy] Loaded agent definition: {agent_file}", file=sys.stderr)

    model_cfg = load_json(repo_root / agent_def["modelRef"])
    system_prompt = (repo_root / agent_def["promptRef"]).read_text().strip()
    guardrail_cfg = load_json(repo_root / agent_def["guardrailRef"])
    index_cfg = load_json(repo_root / agent_def["knowledgeIndexRef"])
    memory_cfg = load_json(repo_root / agent_def["memoryRef"])

    print(
        f"[deploy] Assets loaded — model={model_cfg['name']}, "
        f"index={index_cfg['name']}, memory={memory_cfg['name']}",
        file=sys.stderr,
    )

    # --- Metadata: version + guardrail policy summary ---
    metadata: dict = {
        "sourceEnvironment": "dev",
        "modelFamily": model_cfg.get("modelFamily", ""),
        "memoryMode": memory_cfg.get("mode", ""),
        "guardrailInputPolicies": ",".join(guardrail_cfg.get("inputPolicies", [])),
        "guardrailOutputPolicies": ",".join(guardrail_cfg.get("outputPolicies", [])),
    }
    if version:
        metadata["version"] = version

    # Foundry metadata values must be strings ≤ 256 chars
    metadata = {k: str(v)[:256] for k, v in metadata.items()}

    # --- Connect to target environment ---
    print(f"[deploy] Target endpoint: {endpoint}", file=sys.stderr)
    credential = DefaultAzureCredential()
    client = AIProjectClient(endpoint=endpoint, credential=credential)

    # --- Build tool list: AI Search for knowledge grounding ---
    tools: list = []
    tool_resources = None

    search_conn = find_search_connection(client.connections)
    if search_conn:
        print(
            f"[deploy] Found search connection: {getattr(search_conn, 'name', search_conn.id)}",
            file=sys.stderr,
        )
        ai_search = AzureAISearchTool(
            index_connection_id=search_conn.id,
            index_name=index_cfg["name"],
        )
        tools = ai_search.definitions
        tool_resources = ai_search.resources
    else:
        print(
            "[deploy] Warning: no Azure AI Search connection found — "
            "agent will deploy without knowledge grounding.",
            file=sys.stderr,
        )

    # --- Create or update the agent ---
    agent_kwargs = dict(
        model=model_deployment,
        name=agent_name,
        description=agent_def.get("description", ""),
        instructions=system_prompt,
        tools=tools,
        tool_resources=tool_resources,
        metadata=metadata,
    )

    definition = {
        "kind": "prompt",
        "model": model_deployment,
        "instructions": system_prompt,
        "tools": tools,
    }
    if tool_resources:
        definition["tool_resources"] = tool_resources

    create_version_fn = getattr(client.agents, "create_version", None)

    create_fn = (
        getattr(client.agents, "create_agent", None)
        or getattr(client.agents, "create", None)
        or getattr(client.agents, "_create_agent", None)
    )
    update_fn = (
        getattr(client.agents, "update_agent", None)
        or getattr(client.agents, "update", None)
        or getattr(client.agents, "_update_agent", None)
    )

    if create_fn is None and create_version_fn is None:
        raise RuntimeError(
            "Unsupported azure-ai-projects SDK version: no create or create_version method found on agents client."
        )

    if create_version_fn is not None:
        print(
            f"[deploy] Creating/updating version for agent '{agent_name}' via create_version",
            file=sys.stderr,
        )
        agent = create_version_fn(
            agent_name=agent_name,
            definition=definition,
            metadata=metadata,
            description=agent_def.get("description", ""),
        )
        summary = {
            "agentId": getattr(agent, "id", None),
            "name": getattr(agent, "name", agent_name),
            "model": model_deployment,
            "environment": environment,
            "version": version,
            "endpoint": endpoint,
        }
        print(json.dumps(summary, indent=2))
        return summary

    existing = find_existing_agent(client.agents, agent_name)
    if existing:
        if update_fn is None:
            raise RuntimeError(
                "Unsupported azure-ai-projects SDK version: no update method found on agents client."
            )
        print(
            f"[deploy] Updating existing agent '{agent_name}' (id={existing.id})",
            file=sys.stderr,
        )
        agent = update_fn(existing.id, **agent_kwargs)
    else:
        print(f"[deploy] Creating new agent '{agent_name}'", file=sys.stderr)
        agent = create_fn(**agent_kwargs)

    print(
        f"[deploy] Done — id={agent.id}  name={agent.name}  model={agent.model}",
        file=sys.stderr,
    )

    summary = {
        "agentId": agent.id,
        "name": agent.name,
        "model": agent.model,
        "environment": environment,
        "version": version,
        "endpoint": endpoint,
    }
    print(json.dumps(summary, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Deploy a Foundry agent from foundry/ code to a target environment."
    )
    parser.add_argument(
        "--agent-name",
        required=True,
        help=(
            "Agent name matching the 'name' field in foundry/agents/ JSON "
            "(e.g. incident-triage-hosted)."
        ),
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=["dev", "qa", "prod"],
        help="Target environment.",
    )
    parser.add_argument(
        "--version",
        help="Version tag stored in agent metadata (e.g. v1.2.0 or git SHA).",
    )
    args = parser.parse_args()

    try:
        deploy_agent(args.agent_name, args.environment, args.version)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
