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

    # Export + sync attached resources into foundry/ files for PR promotion
    python scripts/export_agent_from_foundry.py \
            --agent-name incident-triage-hosted \
            --environment dev \
            --output .dist/incident-triage-hosted-export.json \
            --sync-repo

Prerequisites:
  pip install azure-ai-projects azure-identity
  az login   (or set AZURE_CLIENT_ID / AZURE_TENANT_ID for OIDC)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


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


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def choose_ref(existing: dict, key: str, default_ref: str) -> str:
    ref = existing.get(key)
    if isinstance(ref, str) and ref.strip():
        return ref
    return default_ref


def basename_from_resource_id(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    return resource_id.rstrip("/").split("/")[-1]


def collect_candidate_values(obj: Any, candidate_keys: set[str]) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {key: [] for key in candidate_keys}

    def walk(value: Any):
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in candidate_keys:
                    found[key].append(inner)
                walk(inner)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return found


def first_string(values: list[Any]) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_search_attachment(tools: list[dict[str, Any]], tool_resources: dict[str, Any], connections: list[dict[str, str]]) -> dict | None:
    search_tool = None
    for tool in tools:
        payload = safe_as_dict(tool) or {}
        serialized = json.dumps(payload).lower()
        if "search" in serialized:
            search_tool = payload
            break

    if search_tool is None:
        return None

    values = collect_candidate_values(
        {"tool": search_tool, "resources": tool_resources},
        {
            "index_name",
            "indexName",
            "index_connection_id",
            "indexConnectionId",
            "connection_id",
            "connectionId",
            "name",
        },
    )

    index_name = first_string(values["index_name"]) or first_string(values["indexName"]) or "default"
    connection_id = (
        first_string(values["index_connection_id"])
        or first_string(values["indexConnectionId"])
        or first_string(values["connection_id"])
        or first_string(values["connectionId"])
    )

    if connection_id is None and len(connections) == 1:
        connection_id = connections[0].get("id") or None

    payload = {
        "name": index_name,
        "description": "Knowledge index exported from live agent attachment.",
        "retrieval": {
            "mode": "hybrid",
        },
    }
    if connection_id:
        payload["connectionId"] = connection_id
    return payload


def derive_attached_resources(bundle: dict) -> dict:
    agent = bundle["agent"]
    metadata = agent.get("metadata") or {}
    tools = agent.get("tools") or []
    tool_resources = agent.get("toolResources") or {}
    connections = bundle.get("connections") or []
    rai_policy_name = ((agent.get("raiConfig") or {}).get("raiPolicyName"))

    attached = {
        "prompt": None,
        "model": None,
        "guardrail": None,
        "toolset": None,
        "knowledgeIndex": None,
        "memory": None,
    }

    instructions = (agent.get("instructions") or "").strip()
    if instructions:
        attached["prompt"] = {
            "content": instructions,
        }

    if agent.get("model"):
        model_payload = {
            "type": "modelDeployment",
            "deploymentName": agent["model"],
        }
        if metadata.get("modelFamily"):
            model_payload["modelFamily"] = metadata["modelFamily"]
        attached["model"] = model_payload

    input_policies = split_csv(metadata.get("guardrailInputPolicies"))
    output_policies = split_csv(metadata.get("guardrailOutputPolicies"))
    if rai_policy_name or input_policies or output_policies:
        guardrail_payload = {
            "name": basename_from_resource_id(rai_policy_name) or "attached-guardrail",
            "inputPolicies": input_policies,
            "outputPolicies": output_policies,
        }
        if rai_policy_name:
            guardrail_payload["raiPolicyName"] = rai_policy_name
        attached["guardrail"] = guardrail_payload

    if tools:
        normalized = []
        for tool in tools:
            t = safe_as_dict(tool) or {}
            normalized.append(
                {
                    "name": t.get("name") or t.get("id") or t.get("function", {}).get("name") or "unnamed-tool",
                    "kind": t.get("type") or t.get("kind") or "unknown",
                    "purpose": t.get("description") or t.get("purpose") or "",
                }
            )
        attached["toolset"] = {
            "tools": normalized,
        }

    knowledge_payload = extract_search_attachment(tools, tool_resources, connections)
    if knowledge_payload:
        attached["knowledgeIndex"] = knowledge_payload

    if metadata.get("memoryMode"):
        attached["memory"] = {
            "name": "attached-memory",
            "mode": metadata["memoryMode"],
        }

    return attached


def find_repo_agent_file(repo_root: Path, agent_name: str) -> Path | None:
    agents_root = repo_root / "foundry" / "agents"
    if not agents_root.exists():
        return None
    for candidate in sorted(agents_root.rglob("*.json")):
        try:
            data = load_json(candidate)
        except Exception:
            continue
        if data.get("name") == agent_name:
            return candidate
    return None


def sync_agent_bundle_to_repo(bundle: dict, repo_root: Path) -> dict:
    agent_fields = bundle["agent"]
    agent_name = agent_fields["name"]
    attached = bundle["attachedResources"]

    agent_file = find_repo_agent_file(repo_root, agent_name)
    if agent_file is None:
        agent_file = repo_root / "foundry" / "agents" / "hosted" / f"{agent_name}.json"
        existing_agent = {}
    else:
        existing_agent = load_json(agent_file)

    default_slug = agent_name.replace("_", "-")
    synced_files: dict[str, str] = {}

    synced_agent = dict(existing_agent) if existing_agent else {}
    synced_agent["name"] = agent_name
    synced_agent["kind"] = synced_agent.get("kind", "foundryHosted")
    synced_agent["description"] = agent_fields.get("description", synced_agent.get("description", ""))
    synced_agent["evaluationProfile"] = synced_agent.get("evaluationProfile", "incident-resolution")

    if attached["prompt"]:
        prompt_ref = choose_ref(existing_agent, "promptRef", f"foundry/prompts/{default_slug}.system.txt")
        prompt_file = repo_root / prompt_ref
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(attached["prompt"]["content"] + "\n")
        synced_agent["promptRef"] = prompt_ref
        synced_files["prompt"] = str(prompt_file.relative_to(repo_root))
    else:
        synced_agent.pop("promptRef", None)

    if attached["model"]:
        model_ref = choose_ref(existing_agent, "modelRef", f"foundry/models/{default_slug}-model.json")
        model_file = repo_root / model_ref
        model_payload = {
            "name": basename_from_resource_id(model_ref)[:-5] if model_ref.endswith('.json') else f"{agent_name}-model",
            **attached["model"],
        }
        write_json(model_file, model_payload)
        synced_agent["modelRef"] = model_ref
        synced_files["model"] = str(model_file.relative_to(repo_root))
    else:
        synced_agent.pop("modelRef", None)

    if attached["guardrail"]:
        guardrail_ref = choose_ref(existing_agent, "guardrailRef", f"foundry/guardrails/{default_slug}-guardrails.json")
        guardrail_file = repo_root / guardrail_ref
        write_json(guardrail_file, attached["guardrail"])
        synced_agent["guardrailRef"] = guardrail_ref
        synced_files["guardrail"] = str(guardrail_file.relative_to(repo_root))
    else:
        synced_agent.pop("guardrailRef", None)

    if attached["toolset"]:
        toolset_ref = choose_ref(existing_agent, "toolsetRef", f"foundry/tools/{default_slug}-toolset.json")
        toolset_file = repo_root / toolset_ref
        toolset_payload = {
            "name": basename_from_resource_id(toolset_ref)[:-5] if toolset_ref.endswith('.json') else f"{agent_name}-toolset",
            **attached["toolset"],
        }
        write_json(toolset_file, toolset_payload)
        synced_agent["toolsetRef"] = toolset_ref
        synced_files["toolset"] = str(toolset_file.relative_to(repo_root))
    else:
        synced_agent.pop("toolsetRef", None)

    if attached["knowledgeIndex"]:
        knowledge_ref = choose_ref(existing_agent, "knowledgeIndexRef", f"foundry/indexes/{default_slug}-knowledge-index.json")
        knowledge_file = repo_root / knowledge_ref
        write_json(knowledge_file, attached["knowledgeIndex"])
        synced_agent["knowledgeIndexRef"] = knowledge_ref
        synced_files["knowledgeIndex"] = str(knowledge_file.relative_to(repo_root))
    else:
        synced_agent.pop("knowledgeIndexRef", None)

    if attached["memory"]:
        memory_ref = choose_ref(existing_agent, "memoryRef", f"foundry/memory/{default_slug}-memory.json")
        memory_file = repo_root / memory_ref
        write_json(memory_file, attached["memory"])
        synced_agent["memoryRef"] = memory_ref
        synced_files["memory"] = str(memory_file.relative_to(repo_root))
    else:
        synced_agent.pop("memoryRef", None)

    write_json(agent_file, synced_agent)
    synced_files["agent"] = str(agent_file.relative_to(repo_root))

    return synced_files


def extract_agent_fields(agent) -> dict:
    """Normalize agent payload across azure-ai-projects SDK versions."""
    payload = safe_as_dict(agent) or {}

    # Newer SDK shape: AgentDetails with versions.latest.definition
    latest = ((payload.get("versions") or {}).get("latest") or {})
    definition = latest.get("definition") or {}

    if definition:
        metadata = latest.get("metadata") or {}
        rai_config = definition.get("rai_config") or definition.get("raiConfig") or {}
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
            "raiConfig": {
                "raiPolicyName": rai_config.get("rai_policy_name") or rai_config.get("raiPolicyName")
            }
            if rai_config
            else {},
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
        "raiConfig": safe_as_dict(getattr(agent, "rai_config", {})) or {},
        "metadata": dict(getattr(agent, "metadata", {}) or {}),
        "version": None,
        "versionId": None,
    }


def export_agent(
    agent_name: str,
    environment: str,
    output_path: Path | None,
    sync_repo: bool,
) -> dict:
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

    bundle["attachedResources"] = derive_attached_resources(bundle)

    result_json = json.dumps(bundle, indent=2) + "\n"

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result_json)
        print(f"[export] Written to {output_path}", file=sys.stderr)
    else:
        print(result_json)

    if sync_repo:
        synced_files = sync_agent_bundle_to_repo(bundle, repo_root)
        bundle["repoSync"] = synced_files
        print("[export] Synced attached resources into foundry/ files:", file=sys.stderr)
        for _, path in synced_files.items():
            print(f"  - {path}", file=sys.stderr)

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
    parser.add_argument(
        "--sync-repo",
        action="store_true",
        help=(
            "Also sync agent + attached resources into foundry/ files so the change can be committed "
            "and promoted via PR (Dev->QA->Prod)."
        ),
    )
    args = parser.parse_args()

    output = Path(args.output) if args.output else None
    try:
        export_agent(args.agent_name, args.environment, output, args.sync_repo)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
