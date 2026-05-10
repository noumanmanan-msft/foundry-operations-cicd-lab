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
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
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


def normalize_kb_metadata_for_repo(payload: dict | None) -> dict | None:
    """Strip environment-specific values and persist portable metadata templates.

    Export payloads can contain absolute connection IDs and concrete endpoint URLs
    from the source environment (typically Dev). Those values are not portable
    across QA/Prod, so we normalize them before writing foundry/ JSON files.
    """
    if not payload:
        return payload

    normalized = {k: v for k, v in payload.items() if v is not None}

    connection_name = basename_from_resource_id(normalized.pop("connectionId", None))

    project_connection_id = normalized.get("projectConnectionId") or connection_name
    if project_connection_id:
        normalized["projectConnectionId"] = project_connection_id

    knowledge_base_name = (
        normalized.get("knowledgeBaseName")
        or normalized.get("indexName")
        or normalized.get("name")
        or ""
    ).strip()

    normalized.pop("mcpServerUrl", None)

    if knowledge_base_name:
        normalized.setdefault("knowledgeBaseName", knowledge_base_name)
        normalized.setdefault("projectConnectionPrefix", f"kb-{knowledge_base_name}-")
        normalized.setdefault("projectConnectionNameTemplate", f"kb-{knowledge_base_name}-{{environment}}")
        normalized.setdefault(
            "mcpServerUrlTemplate",
            f"https://{{searchEndpointHost}}/knowledgebases/{knowledge_base_name}/mcp?api-version=2025-11-01-Preview",
        )
        normalized.setdefault("mcpServerLabel", f"kb_{knowledge_base_name}".replace("-", "_"))

    return normalized


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


def parse_kb_name_from_mcp_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"/knowledgebases/([^/]+)/mcp", value)
    if not match:
        return None
    return match.group(1)


def get_search_endpoint_from_mcp_url(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None
    return None


def fetch_search_knowledgebase(credential, search_endpoint: str, knowledge_base_name: str) -> dict | None:
    """Fetch a knowledgebase definition from Azure AI Search data-plane."""
    if not (search_endpoint and knowledge_base_name):
        return None

    try:
        token = credential.get_token("https://search.azure.com/.default")
    except Exception as exc:
        print(f"[export] Warning: could not acquire Search token: {exc}", file=sys.stderr)
        return None

    url = (
        f"{search_endpoint.rstrip('/')}/knowledgebases/{knowledge_base_name}"
        "?api-version=2025-11-01-Preview"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token.token}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
            print(
                f"[export] Captured knowledgebase '{knowledge_base_name}' from Search endpoint {search_endpoint}",
                file=sys.stderr,
            )
            return payload
    except urllib.error.HTTPError as exc:
        print(
            f"[export] Warning: Search GET knowledgebases/{knowledge_base_name} returned {exc.code}",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"[export] Warning: could not fetch knowledgebase '{knowledge_base_name}': {exc}", file=sys.stderr)
        return None


def fetch_search_knowledge_source(credential, search_endpoint: str, source_name: str) -> dict | None:
    """Fetch a knowledge source definition from Azure AI Search data-plane."""
    if not (search_endpoint and source_name):
        return None

    try:
        token = credential.get_token("https://search.azure.com/.default")
    except Exception as exc:
        print(f"[export] Warning: could not acquire Search token: {exc}", file=sys.stderr)
        return None

    url = (
        f"{search_endpoint.rstrip('/')}/knowledgeSources/{source_name}"
        "?api-version=2025-11-01-Preview"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token.token}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
            print(
                f"[export] Captured knowledge source '{source_name}' from Search endpoint {search_endpoint}",
                file=sys.stderr,
            )
            return payload
    except urllib.error.HTTPError as exc:
        print(
            f"[export] Warning: Search GET knowledgeSources/{source_name} returned {exc.code}",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"[export] Warning: could not fetch knowledge source '{source_name}': {exc}", file=sys.stderr)
        return None


def collect_knowledgebase_snapshots(credential, bundle: dict, config: dict) -> list[dict]:
    """Capture all knowledgebase objects referenced by the agent and kb MCP connections."""
    snapshots: list[dict] = []
    knowledge_sources: dict[str, dict] = {}

    tools = ((bundle.get("agent") or {}).get("tools") or [])
    connections = bundle.get("connections") or []
    default_search_endpoint = ((config.get("knowledge") or {}).get("searchEndpoint") or "").rstrip("/")

    candidates: dict[str, dict[str, str]] = {}

    for tool in tools:
        payload = safe_as_dict(tool) or {}
        if (payload.get("type") or "").lower() != "mcp":
            continue
        server_url = payload.get("server_url")
        kb_name = parse_kb_name_from_mcp_url(server_url)
        if not kb_name:
            continue
        candidates[kb_name] = {
            "searchEndpoint": get_search_endpoint_from_mcp_url(server_url) or default_search_endpoint,
            "connectionName": (payload.get("project_connection_id") or "").strip(),
            "serverUrl": server_url,
        }

    for conn in connections:
        target = conn.get("target")
        kb_name = parse_kb_name_from_mcp_url(target)
        if not kb_name:
            continue
        meta = conn.get("metadata") or {}
        if isinstance(meta.get("knowledgeBaseName"), str) and meta.get("knowledgeBaseName").strip():
            kb_name = meta["knowledgeBaseName"].strip()

        candidates.setdefault(
            kb_name,
            {
                "searchEndpoint": get_search_endpoint_from_mcp_url(target) or default_search_endpoint,
                "connectionName": (conn.get("name") or "").strip(),
                "serverUrl": target,
            },
        )

    for kb_name, details in sorted(candidates.items()):
        search_endpoint = (details.get("searchEndpoint") or default_search_endpoint or "").rstrip("/")
        kb_payload = fetch_search_knowledgebase(credential, search_endpoint, kb_name)
        if not kb_payload:
            continue

        source_refs = []
        for src in kb_payload.get("knowledgeSources") or []:
            src_name = (src.get("name") or "").strip() if isinstance(src, dict) else ""
            if not src_name:
                continue
            source_refs.append(src_name)
            if src_name not in knowledge_sources:
                src_payload = fetch_search_knowledge_source(credential, search_endpoint, src_name)
                if src_payload:
                    knowledge_sources[src_name] = src_payload

        snapshots.append(
            {
                "name": kb_name,
                "searchEndpoint": search_endpoint,
                "projectConnectionId": details.get("connectionName") or "",
                "mcpServerUrl": details.get("serverUrl") or "",
                "knowledgeSourceNames": source_refs,
                "definition": kb_payload,
            }
        )

    return [
        {
            "knowledgeBases": snapshots,
            "knowledgeSources": [
                {"name": name, "definition": definition}
                for name, definition in sorted(knowledge_sources.items())
            ],
        }
    ][0]


def extract_search_attachment(tools: list[dict[str, Any]], tool_resources: dict[str, Any], connections: list[dict[str, str]]) -> dict | None:
    search_tool = None
    for tool in tools:
        payload = safe_as_dict(tool) or {}
        serialized = json.dumps(payload).lower()
        if "search" in serialized:
            search_tool = payload
            break

    # Foundry IQ knowledgebase tools are surfaced as MCP endpoints and may not
    # include explicit index_name fields in the tool payload.
    if search_tool is None:
        for tool in tools:
            payload = safe_as_dict(tool) or {}
            if (payload.get("type") or "").lower() == "mcp" and payload.get("server_url"):
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
            "project_connection_id",
            "projectConnectionId",
            "name",
        },
    )

    mcp_server_url = search_tool.get("server_url") if isinstance(search_tool, dict) else None
    mcp_kb_name = None
    if isinstance(mcp_server_url, str):
        match = re.search(r"/knowledgebases/([^/]+)/mcp", mcp_server_url)
        if match:
            mcp_kb_name = match.group(1)

    index_name = first_string(values["index_name"]) or first_string(values["indexName"]) or mcp_kb_name or "default"
    connection_id = (
        first_string(values["index_connection_id"])
        or first_string(values["indexConnectionId"])
        or first_string(values["connection_id"])
        or first_string(values["connectionId"])
        or first_string(values["project_connection_id"])
        or first_string(values["projectConnectionId"])
    )

    # Resolve short project connection names to full ARM IDs when possible.
    if connection_id and not connection_id.startswith("/"):
        for conn in connections:
            conn_id = conn.get("id") or ""
            conn_name = conn.get("name") or ""
            if conn_name == connection_id or conn_id.endswith(f"/connections/{connection_id}"):
                connection_id = conn_id
                break

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
    if mcp_kb_name:
        payload["knowledgeBaseName"] = mcp_kb_name
    if isinstance(mcp_server_url, str) and mcp_server_url:
        payload["mcpServerUrl"] = mcp_server_url
    if isinstance(search_tool, dict):
        if search_tool.get("server_label"):
            payload["mcpServerLabel"] = search_tool.get("server_label")
        if search_tool.get("project_connection_id"):
            payload["projectConnectionId"] = search_tool.get("project_connection_id")
    return payload


def fetch_rai_policy_from_arm(credential, foundry_resource_id: str, policy_basename: str) -> dict | None:
    """Fetch full RAI policy settings (content filters, mode, base policy) via the ARM API.

    The Foundry SDK only surfaces the policy *name*; the ARM raiPolicies resource is the
    only place that holds severity thresholds, blocking flags, and enabled/disabled states.
    Calling this from the exporter captures those settings so they can be stored in the
    repo and promoted through Dev -> QA -> Prod.
    """
    import urllib.error
    import urllib.request

    try:
        token = credential.get_token("https://management.azure.com/.default")
    except Exception as exc:
        print(f"[export] Warning: could not acquire ARM token: {exc}", file=sys.stderr)
        return None

    url = (
        f"https://management.azure.com{foundry_resource_id}"
        f"/raiPolicies/{policy_basename}?api-version=2024-10-01"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token.token}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            print(f"[export] Fetched RAI policy '{policy_basename}' content filters from ARM", file=sys.stderr)
            return data
    except urllib.error.HTTPError as exc:
        print(
            f"[export] Warning: ARM GET raiPolicies/{policy_basename} returned {exc.code} — "
            "content filters will not be captured.",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"[export] Warning: could not fetch RAI policy from ARM: {exc}", file=sys.stderr)
        return None


def derive_attached_resources(bundle: dict, arm_rai_policy: dict | None = None) -> dict:
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
        "knowledge": None,
        "foundryIq": None,
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
            # Store only the basename so the deployer can resolve it against any
            # target environment's Foundry account (dev, qa, prod) at deployment time.
            guardrail_payload["raiPolicyName"] = basename_from_resource_id(rai_policy_name) or rai_policy_name
        # Merge full content filter settings fetched from ARM (severity thresholds, etc.)
        if arm_rai_policy:
            props = arm_rai_policy.get("properties") or {}
            if props.get("contentFilters"):
                guardrail_payload["contentFilters"] = props["contentFilters"]
            if props.get("mode"):
                guardrail_payload["mode"] = props["mode"]
            if props.get("basePolicyName"):
                guardrail_payload["basePolicyName"] = props["basePolicyName"]
        attached["guardrail"] = guardrail_payload

    if tools:
        normalized = []
        for tool in tools:
            t = safe_as_dict(tool) or {}
            normalized.append(
                {
                    "name": (
                        t.get("name")
                        or t.get("id")
                        or t.get("server_label")
                        or t.get("function", {}).get("name")
                        or "unnamed-tool"
                    ),
                    "kind": t.get("type") or t.get("kind") or "unknown",
                    "purpose": t.get("description") or t.get("purpose") or t.get("server_url") or "",
                }
            )
        attached["toolset"] = {
            "tools": normalized,
        }

    knowledge_payload = extract_search_attachment(tools, tool_resources, connections)
    if knowledge_payload:
        attached["knowledgeIndex"] = knowledge_payload
        attached["knowledge"] = {
            "name": f"{knowledge_payload['name']}-knowledge",
            "description": "Knowledge asset exported from live Foundry attachment.",
            "indexName": knowledge_payload["name"],
            "retrieval": knowledge_payload.get("retrieval", {"mode": "hybrid"}),
            "connectionId": knowledge_payload.get("connectionId"),
            "knowledgeBaseName": knowledge_payload.get("knowledgeBaseName"),
            "indexRef": "",
        }
        attached["foundryIq"] = {
            "name": f"{knowledge_payload['name']}-foundry-iq",
            "description": "Foundry IQ configuration exported from live Foundry attachment.",
            "provider": "azure-ai-search",
            "indexName": knowledge_payload["name"],
            "connectionId": knowledge_payload.get("connectionId"),
            "knowledgeBaseName": knowledge_payload.get("knowledgeBaseName"),
            "projectConnectionId": knowledge_payload.get("projectConnectionId"),
            "mcpServerLabel": knowledge_payload.get("mcpServerLabel"),
            "mcpServerUrl": knowledge_payload.get("mcpServerUrl"),
            "knowledgeRef": "",
            "indexRef": "",
        }

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
    knowledge_sources = bundle.get("knowledgeSources") or []

    knowledge_source_refs: dict[str, str] = {}
    for source in knowledge_sources:
        src_name = (source.get("name") or "").strip()
        src_definition = source.get("definition") or {}
        if not src_name or not isinstance(src_definition, dict) or not src_definition:
            continue
        src_ref = f"foundry/knowledge-sources/{src_name}.json"
        src_file = repo_root / src_ref
        src_payload = {
            "name": src_name,
            "description": "Knowledge source definition exported from Dev Azure AI Search.",
            "definition": src_definition,
        }
        write_json(src_file, src_payload)
        knowledge_source_refs[src_name] = src_ref

    agent_file = find_repo_agent_file(repo_root, agent_name)
    if agent_file is None:
        agent_file = repo_root / "foundry" / "agents" / "hosted" / f"{agent_name}.json"
        existing_agent = {}
    else:
        existing_agent = load_json(agent_file)

    default_slug = agent_name.replace("_", "-")
    synced_files: dict[str, str] = {}

    for src_name, src_ref in sorted(knowledge_source_refs.items()):
        synced_files[f"knowledgeSource:{src_name}"] = src_ref

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
        existing_toolset = load_json(toolset_file) if toolset_file.exists() else {}
        exported_tools = attached["toolset"].get("tools", [])
        if exported_tools and all(
            t.get("name") == "unnamed-tool" and not (t.get("purpose") or "").strip()
            for t in exported_tools
        ):
            # Keep existing tool contracts if the exporter could not infer tool details.
            exported_tools = existing_toolset.get("tools", exported_tools)

        toolset_payload = dict(existing_toolset)
        toolset_payload["name"] = (
            toolset_payload.get("name")
            or basename_from_resource_id(toolset_ref)[:-5]
            if toolset_ref.endswith('.json')
            else f"{agent_name}-toolset"
        )
        toolset_payload["tools"] = exported_tools
        write_json(toolset_file, toolset_payload)
        synced_agent["toolsetRef"] = toolset_ref
        synced_files["toolset"] = str(toolset_file.relative_to(repo_root))
    else:
        synced_agent.pop("toolsetRef", None)

    if attached["knowledgeIndex"]:
        knowledge_ref = choose_ref(existing_agent, "knowledgeIndexRef", f"foundry/indexes/{default_slug}-knowledge-index.json")
        knowledge_file = repo_root / knowledge_ref
        existing_knowledge_index = load_json(knowledge_file) if knowledge_file.exists() else {}
        normalized_knowledge_index = normalize_kb_metadata_for_repo(attached["knowledgeIndex"]) or {}
        merged_knowledge_index = dict(existing_knowledge_index)
        merged_knowledge_index.update({k: v for k, v in normalized_knowledge_index.items() if v is not None})
        if isinstance(existing_knowledge_index.get("retrieval"), dict) or isinstance(normalized_knowledge_index.get("retrieval"), dict):
            merged_retrieval = dict(existing_knowledge_index.get("retrieval", {}))
            merged_retrieval.update(normalized_knowledge_index.get("retrieval", {}))
            merged_knowledge_index["retrieval"] = merged_retrieval
        write_json(knowledge_file, merged_knowledge_index)
        synced_agent["knowledgeIndexRef"] = knowledge_ref
        synced_files["knowledgeIndex"] = str(knowledge_file.relative_to(repo_root))
    else:
        synced_agent.pop("knowledgeIndexRef", None)

    if attached["knowledge"]:
        knowledge_ref = choose_ref(existing_agent, "knowledgeRef", f"foundry/knowledge/{default_slug}-knowledge.json")
        knowledge_file = repo_root / knowledge_ref
        knowledge_payload = normalize_kb_metadata_for_repo(attached["knowledge"]) or {}
        knowledge_payload["indexRef"] = synced_agent.get("knowledgeIndexRef", "")
        write_json(knowledge_file, knowledge_payload)
        synced_agent["knowledgeRef"] = knowledge_ref
        synced_files["knowledge"] = str(knowledge_file.relative_to(repo_root))
    else:
        synced_agent.pop("knowledgeRef", None)

    if attached["foundryIq"]:
        foundry_iq_ref = choose_ref(existing_agent, "foundryIqRef", f"foundry/foundry-iq/{default_slug}-foundry-iq.json")
        foundry_iq_file = repo_root / foundry_iq_ref
        foundry_iq_payload = normalize_kb_metadata_for_repo(attached["foundryIq"]) or {}
        foundry_iq_payload["indexRef"] = synced_agent.get("knowledgeIndexRef", "")
        foundry_iq_payload["knowledgeRef"] = synced_agent.get("knowledgeRef", "")

        kb_snaps = bundle.get("knowledgeBases") or []
        kb_name = (foundry_iq_payload.get("knowledgeBaseName") or "").strip()
        if kb_name:
            kb_match = next((k for k in kb_snaps if (k.get("name") or "").strip() == kb_name), None)
            if kb_match:
                kb_ref = f"foundry/knowledgebases/{kb_name}.json"
                kb_file = repo_root / kb_ref
                kb_repo_payload = {
                    "name": kb_name,
                    "description": "Knowledgebase definition exported from Dev Azure AI Search.",
                    "projectConnectionId": kb_match.get("projectConnectionId") or foundry_iq_payload.get("projectConnectionId"),
                    "projectConnectionPrefix": f"kb-{kb_name}-",
                    "projectConnectionNameTemplate": f"kb-{kb_name}-{{environment}}",
                    "mcpServerUrlTemplate": (
                        f"https://{{searchEndpointHost}}/knowledgebases/{kb_name}/mcp?api-version=2025-11-01-Preview"
                    ),
                    "knowledgeSourceRefs": [
                        knowledge_source_refs[name]
                        for name in kb_match.get("knowledgeSourceNames") or []
                        if name in knowledge_source_refs
                    ],
                    "definition": kb_match.get("definition") or {},
                }
                write_json(kb_file, kb_repo_payload)
                synced_files["knowledgebase"] = str(kb_file.relative_to(repo_root))
                foundry_iq_payload["knowledgeBaseRef"] = kb_ref

        write_json(foundry_iq_file, foundry_iq_payload)
        synced_agent["foundryIqRef"] = foundry_iq_ref
        synced_files["foundryIq"] = str(foundry_iq_file.relative_to(repo_root))
    else:
        synced_agent.pop("foundryIqRef", None)

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
                "group": getattr(c.properties, "group", "") if hasattr(c, "properties") else "",
                "target": getattr(c.properties, "target", "") if hasattr(c, "properties") else "",
                "metadata": safe_as_dict(getattr(c.properties, "metadata", {})) if hasattr(c, "properties") else {},
            }
            for c in client.connections.list()
        ]
        bundle["connections"] = connections
        print(f"[export] Captured {len(connections)} connection(s)", file=sys.stderr)
    except Exception as exc:
        print(f"[export] Warning: could not list connections: {exc}", file=sys.stderr)
        bundle["connections"] = []

    # Fetch full RAI policy settings from ARM — the Foundry SDK only surfaces the policy
    # name, not the content filter thresholds configured in the portal.
    arm_rai_policy: dict | None = None
    rai_policy_basename = basename_from_resource_id(
        (agent_fields.get("raiConfig") or {}).get("raiPolicyName")
    )
    foundry_resource_id = config["foundry"].get("resourceId", "")
    if rai_policy_basename and foundry_resource_id:
        arm_rai_policy = fetch_rai_policy_from_arm(credential, foundry_resource_id, rai_policy_basename)

    bundle["attachedResources"] = derive_attached_resources(bundle, arm_rai_policy=arm_rai_policy)

    kb_capture = collect_knowledgebase_snapshots(credential, bundle, config)
    knowledge_bases = kb_capture.get("knowledgeBases") or []
    knowledge_sources = kb_capture.get("knowledgeSources") or []
    if knowledge_bases:
        bundle["knowledgeBases"] = knowledge_bases
        print(f"[export] Captured {len(knowledge_bases)} knowledgebase definition(s)", file=sys.stderr)
    if knowledge_sources:
        bundle["knowledgeSources"] = knowledge_sources
        print(f"[export] Captured {len(knowledge_sources)} knowledge source definition(s)", file=sys.stderr)

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
