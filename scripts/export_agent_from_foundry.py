#!/usr/bin/env python3
"""
Export a live Foundry agent definition and its connected assets to a structured JSON file.

This is a BOOTSTRAP utility for when an agent has been created or edited in the
Azure AI Foundry portal and needs to be synced back into the repo's foundry/ directory.
After exporting, review the output, update the corresponding foundry/ files, and open a PR.
The repo (foundry/ directory) is the authoritative source of truth for CI/CD.

Usage:
  python scripts/export_agent_from_foundry.py \
      --agent-name incident-triage-hosted \
      --environment dev \
      --output .dist/incident-triage-hosted-export.json

Prerequisites:
  pip install azure-ai-projects azure-identity
  az login   (or set AZURE_CLIENT_ID / AZURE_TENANT_ID for OIDC)
"""

import argparse
import json
import sys
from pathlib import Path


def build_project_endpoint(config: dict) -> str:
    """Build the Foundry project-scoped URL from config.json.

    Expected format:
      https://{accountName}.services.ai.azure.com/api/projects/{projectName}
    """
    account = config["foundry"]["accountName"]
    project_id = config["foundry"]["projectId"]
    # projectId format: .../accounts/{account}/projects/{project}
    project_name = project_id.split("/projects/")[-1]
    return f"https://{account}.services.ai.azure.com/api/projects/{project_name}"


def find_agent_by_name(agents_client, agent_name: str):
    """Return the first agent whose name matches, or raise."""
    list_fn = getattr(agents_client, "list", None) or getattr(agents_client, "list_agents", None)
    get_fn = getattr(agents_client, "get", None) or getattr(agents_client, "get_agent", None)

    if list_fn is None or get_fn is None:
        raise RuntimeError(
            "Unsupported azure-ai-projects SDK version: agents client does not expose "
            "expected list/get methods."
        )

    for agent in list_fn():
        if agent.name == agent_name:
            return get_fn(agent.id)

    available_names = []
    for agent in list_fn():
        if getattr(agent, "name", None):
            available_names.append(agent.name)

    details = ""
    if available_names:
        details = f" Available agents: {', '.join(sorted(available_names))}."

    raise ValueError(
        f"No agent named '{agent_name}' found. "
        "Check the name matches exactly what is shown in the Foundry portal."
        f"{details}"
    )


def safe_as_dict(obj):
    """Convert SDK model objects to plain dicts for JSON serialisation."""
    if obj is None:
        return None
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if isinstance(obj, list):
        return [safe_as_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: safe_as_dict(v) for k, v in obj.items()}
    return obj


def extract_agent_fields(agent) -> dict:
    """Normalize agent payload across azure-ai-projects SDK versions."""
    payload = safe_as_dict(agent) or {}

    # Newer SDK shape: AgentDetails with versions.latest.definition
    latest = ((payload.get("versions") or {}).get("latest") or {})
    definition = latest.get("definition") or {}

    if definition:
        metadata = latest.get("metadata") or {}
        return {
            "id": payload.get("id"),
            "name": payload.get("name"),
            "description": latest.get("description") or metadata.get("description") or "",
            "model": definition.get("model"),
            "instructions": definition.get("instructions") or "",
            "temperature": definition.get("temperature"),
            "topP": definition.get("top_p") or definition.get("topP"),
            "tools": definition.get("tools") or [],
            "toolResources": definition.get("tool_resources") or definition.get("toolResources") or {},
            "metadata": metadata,
            "version": latest.get("version"),
            "versionId": latest.get("id"),
        }

    # Older SDK shape: direct attributes on returned agent object
    return {
        "id": getattr(agent, "id", None),
        "name": getattr(agent, "name", None),
        "description": getattr(agent, "description", "") or "",
        "model": getattr(agent, "model", None),
        "instructions": getattr(agent, "instructions", "") or "",
        "temperature": getattr(agent, "temperature", None),
        "topP": getattr(agent, "top_p", None),
        "tools": safe_as_dict(getattr(agent, "tools", [])) or [],
        "toolResources": safe_as_dict(getattr(agent, "tool_resources", {})) or {},
        "metadata": dict(getattr(agent, "metadata", {}) or {}),
        "version": None,
        "versionId": None,
    }


def export_agent(agent_name: str, environment: str, output_path: Path | None) -> dict:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / "environments" / environment / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Environment config not found: {config_path}")

    config = json.loads(config_path.read_text())
    endpoint = build_project_endpoint(config)
    print(f"[export] Connecting to: {endpoint}", file=sys.stderr)

    credential = DefaultAzureCredential()
    client = AIProjectClient(endpoint=endpoint, credential=credential)

    print(f"[export] Searching for agent: {agent_name}", file=sys.stderr)
    agent = find_agent_by_name(client.agents, agent_name)
    print(f"[export] Found agent id={agent.id}", file=sys.stderr)

    # Capture the full agent state
    agent_fields = extract_agent_fields(agent)
    bundle = {
        "schemaVersion": "1.0",
        "exportedFrom": environment,
        "foundryAccountName": config["foundry"]["accountName"],
        "agent": agent_fields,
    }

    # Also capture active connections for reference
    try:
        connections = [
            {
                "id": c.id,
                "name": getattr(c, "name", ""),
                "type": getattr(c.properties, "category", "") if hasattr(c, "properties") else "",
            }
            for c in client.connections.list()
        ]
        bundle["connections"] = connections
        print(f"[export] Captured {len(connections)} connection(s)", file=sys.stderr)
    except Exception as exc:
        print(f"[export] Warning: could not list connections: {exc}", file=sys.stderr)
        bundle["connections"] = []

    result_json = json.dumps(bundle, indent=2) + "\n"

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result_json)
        print(f"[export] Written to {output_path}", file=sys.stderr)
    else:
        print(result_json)

    return bundle


def main():
    parser = argparse.ArgumentParser(
        description="Export a live Foundry agent definition to JSON (bootstrap utility)."
    )
    parser.add_argument(
        "--agent-name",
        required=True,
        help="The agent name as shown in the Foundry portal (e.g. incident-triage-hosted).",
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=["dev", "qa", "prod"],
        help="Source environment to export from.",
    )
    parser.add_argument(
        "--output",
        help="Optional output file path. Defaults to stdout.",
    )
    args = parser.parse_args()

    output = Path(args.output) if args.output else None
    try:
        export_agent(args.agent_name, args.environment, output)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
