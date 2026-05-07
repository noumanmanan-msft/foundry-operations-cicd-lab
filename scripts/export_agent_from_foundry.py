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
    for agent in agents_client.list_agents():
        if agent.name == agent_name:
            return agents_client.get_agent(agent.id)
    raise ValueError(
        f"No agent named '{agent_name}' found. "
        "Check the name matches exactly what is shown in the Foundry portal."
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
    bundle = {
        "schemaVersion": "1.0",
        "exportedFrom": environment,
        "foundryAccountName": config["foundry"]["accountName"],
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "description": getattr(agent, "description", "") or "",
            "model": agent.model,
            "instructions": agent.instructions or "",
            "temperature": getattr(agent, "temperature", None),
            "topP": getattr(agent, "top_p", None),
            "tools": safe_as_dict(agent.tools) or [],
            "toolResources": safe_as_dict(agent.tool_resources) or {},
            "metadata": dict(agent.metadata or {}),
        },
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
