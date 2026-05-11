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
from datetime import datetime, timezone
import json
import re
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


def extract_foundry_iq_attachments(tools: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract per-knowledgebase MCP attachments from agent tools."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    for tool in tools:
        t = safe_as_dict(tool) or {}
        if (t.get("type") or "").lower() != "mcp":
            continue

        server_url = (t.get("server_url") or "").strip()
        match = re.search(r"/knowledgebases/([^/]+)/mcp", server_url)
        if not match:
            continue

        kb_name = match.group(1).strip()
        if not kb_name or kb_name in seen:
            continue

        seen.add(kb_name)
        found.append(
            {
                "name": kb_name,
                "projectConnectionId": (t.get("project_connection_id") or "").strip(),
                "mcpServerLabel": (t.get("server_label") or "").strip(),
            }
        )

    return found


def normalize_kb_metadata_for_repo(payload: dict | None) -> dict | None:
    """Return a repo-friendly knowledge payload with null/empty pruning."""
    if not isinstance(payload, dict):
        return payload

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        normalized[key] = value
    return normalized


def build_knowledge_source_ref_map(repo_root: Path) -> dict[str, str]:
    """Map knowledge source names to their file refs under foundry/knowledge-sources."""
    refs: dict[str, str] = {}
    sources_root = repo_root / "foundry" / "knowledge-sources"
    if not sources_root.exists():
        return refs

    for candidate in sorted(sources_root.glob("*.json")):
        try:
            data = load_json(candidate)
        except Exception:
            continue

        name = (data.get("name") or "").strip()
        if not name:
            continue
        refs[name] = str(candidate.relative_to(repo_root))

    return refs


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
        "knowledge": None,
        "knowledgeIndex": None,
        "foundryIq": [],
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

    foundry_iq_payload = extract_foundry_iq_attachments(tools)
    if foundry_iq_payload:
        attached["foundryIq"] = foundry_iq_payload

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


def collect_referenced_foundry_iq_refs(repo_root: Path) -> set[str]:
    """Collect all foundryIqRefs values from every agent JSON in foundry/agents/."""
    refs: set[str] = set()
    agents_root = repo_root / "foundry" / "agents"
    if not agents_root.exists():
        return refs

    for candidate in sorted(agents_root.rglob("*.json")):
        try:
            data = load_json(candidate)
        except Exception:
            continue

        value = data.get("foundryIqRefs")
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item.strip():
                refs.add(item.strip())

    return refs


def move_orphaned_foundry_iq_files(repo_root: Path, max_orphans: int = 5) -> int:
    """Move unreferenced foundry-iq JSON files into foundry/foundry-iq/_orphaned/."""
    foundry_iq_root = repo_root / "foundry" / "foundry-iq"
    orphaned_root = foundry_iq_root / "_orphaned"
    orphaned_root.mkdir(parents=True, exist_ok=True)

    referenced_refs = collect_referenced_foundry_iq_refs(repo_root)
    orphan_candidates: list[Path] = []

    for candidate in sorted(foundry_iq_root.glob("*.json")):
        rel = str(candidate.relative_to(repo_root))
        if rel not in referenced_refs:
            orphan_candidates.append(candidate)

    if len(orphan_candidates) > max_orphans:
        listed = "\n".join(f"  - {str(path.relative_to(repo_root))}" for path in orphan_candidates)
        raise RuntimeError(
            "Orphan cleanup aborted: more than 5 unreferenced foundry-iq files detected.\n"
            "Candidate orphans:\n"
            f"{listed}\n"
            "Fix references or re-run with --no-orphan-cleanup to skip moving files."
        )

    moved_count = 0
    for source in orphan_candidates:
        destination = orphaned_root / source.name
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
            destination = orphaned_root / f"{source.stem}.{stamp}{source.suffix}"
            if destination.exists():
                idx = 1
                while True:
                    alt = orphaned_root / f"{source.stem}.{stamp}.{idx}{source.suffix}"
                    if not alt.exists():
                        destination = alt
                        break
                    idx += 1

        source.rename(destination)
        moved_count += 1
        print(
            "[export] orphaned foundry-iq file: "
            f"{source.name} -> {str(destination.relative_to(repo_root))} "
            "(no agent in foundry/agents/ references this file)",
            file=sys.stderr,
        )

    print(f"[export] orphans moved: {moved_count}.", file=sys.stderr)
    return moved_count


def resolve_connection_suffix(config: dict) -> str:
    configured = (config.get("connectionNameSuffix") or "").strip()
    if configured:
        return configured
    return (config.get("environment") or "").strip()


def discover_connection_suffixes(repo_root: Path) -> set[str]:
    suffixes: set[str] = set()
    env_root = repo_root / "environments"
    for env in ["dev", "qa", "prod"]:
        cfg_path = env_root / env / "config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = load_json(cfg_path)
        except Exception:
            continue
        value = (cfg.get("connectionNameSuffix") or "").strip()
        if value:
            suffixes.add(value)
    return suffixes


def discover_local_connection_suffixes(payload: Any, known_suffixes: set[str]) -> set[str]:
    """Find concrete connection suffix values present in a single file payload."""
    found: set[str] = set()

    def walk(value: Any):
        if isinstance(value, dict):
            for key, inner in value.items():
                if key == "projectConnectionId" and isinstance(inner, str):
                    match = re.search(r"-([a-zA-Z0-9]+)$", inner)
                    if match:
                        candidate = match.group(1)
                        if candidate in known_suffixes:
                            found.add(candidate)
                walk(inner)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


def replace_connection_suffix(value: str, suffixes: set[str]) -> str:
    updated = value
    for suffix in sorted(suffixes, key=len, reverse=True):
        if not suffix:
            continue
        updated = re.sub(
            rf"-{re.escape(suffix)}(?=($|[^a-zA-Z0-9]))",
            "-{connectionNameSuffix}",
            updated,
        )
    return updated


def tokenize_search_host(value: str) -> str:
    return re.sub(
        r"\b(?:srch-(?:dev|qa|prod)[^/\s\"']*\.search\.windows\.net)\b",
        "{searchEndpointHost}",
        value,
    )


def tokenize_openai_host(value: str) -> str:
    return re.sub(
        r"\b(?:aif-(?:dev|qa|prod)[^/\s\"']*\.openai\.azure\.com)\b",
        "{azureOpenAIAccountHost}",
        value,
    )


def tokenize_arm_paths(value: str) -> str:
    project_match = re.match(
        r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.CognitiveServices/accounts/[^/]+/projects/[^/]+/connections/([^/]+)$",
        value,
    )
    if project_match:
        return f"{{projectArmPrefix}}/connections/{project_match.group(1)}"

    policy_match = re.match(
        r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.CognitiveServices/accounts/[^/]+/raiPolicies/([^/]+)$",
        value,
    )
    if policy_match:
        return f"{{accountArmPrefix}}/raiPolicies/{policy_match.group(1)}"

    return value


def sanitize_string_value(
    value: str,
    key: str,
    suffixes: set[str],
    local_suffixes: set[str],
) -> str:
    updated = value

    if key == "mcpServerLabel":
        for suffix in sorted(local_suffixes, key=len, reverse=True):
            marker = f"_{suffix}"
            if updated.endswith(marker):
                updated = updated[: -len(marker)]
                break

    if key in {"projectConnectionId", "projectConnectionPrefix", "projectConnectionNameTemplate"}:
        updated = replace_connection_suffix(updated, suffixes)
        if key == "projectConnectionNameTemplate":
            updated = updated.replace("{environment}", "{connectionNameSuffix}")

    if key in {"connectionId", "id", "raiPolicyName"} or "/subscriptions/" in updated:
        updated = tokenize_arm_paths(updated)

    updated = tokenize_search_host(updated)
    updated = tokenize_openai_host(updated)
    return updated


def normalize_agent_connections_section(payload: dict):
    connections = payload.get("connections")
    if not isinstance(connections, dict):
        return

    refs = connections.get("referencedConnections")
    if not isinstance(refs, list):
        return

    simplified = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        connection_name = ref.get("name") or basename_from_resource_id(ref.get("id")) or ""
        auth_type = ""
        credentials = ref.get("credentials")
        if isinstance(credentials, dict):
            auth_type = credentials.get("type") or ""
        if not auth_type:
            auth_type = ref.get("authType") or ""
        target_template = ref.get("target") or ""
        simplified.append(
            {
                "connectionName": connection_name,
                "authType": auth_type,
                "targetTemplate": target_template,
            }
        )

    payload["connections"] = {"referencedConnections": simplified}


def sanitize_value(
    value: Any,
    suffixes: set[str],
    local_suffixes: set[str],
    parent_key: str = "",
) -> Any:
    keys_to_strip = {
        "createdAt",
        "modifiedAt",
        "created_at",
        "modified_at",
        "systemData",
        "etag",
        "eTag",
        "@odata.etag",
    }

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, inner in value.items():
            if key in keys_to_strip:
                continue
            cleaned[key] = sanitize_value(inner, suffixes, local_suffixes, key)
        return cleaned

    if isinstance(value, list):
        return [sanitize_value(item, suffixes, local_suffixes, parent_key) for item in value]

    if isinstance(value, str):
        return sanitize_string_value(value, parent_key, suffixes, local_suffixes)

    return value


def enforce_sanitization_invariants(path: Path, payload: dict, suffixes: set[str]):
    serialized = json.dumps(payload, indent=2)
    forbidden = [
        "aif-dev-",
        "aif-qa-",
        "aif-prod-",
        "srch-dev-",
        "srch-qa-",
        "srch-prod-",
        "/subscriptions/",
        "@odata.etag",
        "createdAt",
        "modifiedAt",
        "systemData",
    ]

    for suffix in sorted(suffixes):
        if suffix:
            forbidden.extend([suffix, f"_{suffix}", f"-{suffix}"])

    for needle in forbidden:
        if needle in serialized:
            raise RuntimeError(
                f"Sanitization invariant failed for {path}: found forbidden string '{needle}'"
            )


def write_sanitized_json(path: Path, payload: dict, suffixes: set[str]):
    enforce_sanitization_invariants(path, payload, suffixes)
    write_json(path, payload)


def sanitize_repo_assets(repo_root: Path) -> dict[str, int]:
    suffixes = discover_connection_suffixes(repo_root)

    targets = [
        repo_root / "foundry" / "foundry-iq",
        repo_root / "foundry" / "knowledgebases",
        repo_root / "foundry" / "knowledge",
        repo_root / "foundry" / "indexes",
    ]

    files_sanitized = 0
    for target in targets:
        if not target.exists():
            continue
        for path in sorted(target.rglob("*.json")):
            if "_orphaned" in path.parts:
                continue
            payload = load_json(path)
            local_suffixes = discover_local_connection_suffixes(payload, suffixes)
            sanitized = sanitize_value(payload, suffixes, local_suffixes)
            write_sanitized_json(path, sanitized, suffixes)
            files_sanitized += 1

    print(f"[export] Sanitized repo assets: {files_sanitized} file(s)", file=sys.stderr)
    return {"filesSanitized": files_sanitized}


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
    knowledge_source_refs = build_knowledge_source_ref_map(repo_root)

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

    # Only write legacy singular knowledge/index files when the agent has no foundryIq (multi-KB) data.
    # When foundryIq is present, the per-KB knowledgebases/ files already capture all this information
    # and the deployer reads from foundryIqRefs — writing these files would produce unreferenced stale copies.
    if attached["knowledgeIndex"] and not attached["foundryIq"]:
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
        synced_files["knowledgeIndex"] = str(knowledge_file.relative_to(repo_root))
    synced_agent.pop("knowledgeIndexRef", None)

    if attached["knowledge"] and not attached["foundryIq"]:
        knowledge_ref = choose_ref(existing_agent, "knowledgeRef", f"foundry/knowledge/{default_slug}-knowledge.json")
        knowledge_file = repo_root / knowledge_ref
        knowledge_payload = normalize_kb_metadata_for_repo(attached["knowledge"]) or {}
        write_json(knowledge_file, knowledge_payload)
        synced_files["knowledge"] = str(knowledge_file.relative_to(repo_root))
    synced_agent.pop("knowledgeRef", None)

    if attached["foundryIq"]:
        kb_snaps = bundle.get("knowledgeBases") or []
        foundry_iq_refs: list[str] = []
        agent_tools = agent_fields.get("tools") or []

        # Iterate over ALL captured knowledgebases; create separate foundry-iq JSON per KB
        for kb_snap in kb_snaps:
            kb_name = (kb_snap.get("name") or "").strip()
            if not kb_name:
                continue

            matching_tool = next(
                (
                    tool
                    for tool in agent_tools
                    if isinstance(tool, dict)
                    and f"/knowledgebases/{kb_name}/mcp" in (tool.get("server_url") or "")
                ),
                {},
            )

            project_connection_id = (
                (kb_snap.get("projectConnectionId") or "").strip()
                or (matching_tool.get("project_connection_id") or "").strip()
            )
            mcp_server_label = (
                (matching_tool.get("server_label") or "").strip()
                or f"kb_{kb_name}".replace("-", "_")
            )

            # Create foundry-iq JSON for this specific KB
            foundry_iq_ref = f"foundry/foundry-iq/{kb_name}-foundry-iq.json"
            foundry_iq_file = repo_root / foundry_iq_ref
            foundry_iq_payload = {
                "name": f"{kb_name}-foundry-iq",
                "description": "Foundry IQ configuration exported from live Foundry attachment.",
                "provider": "azure-ai-search",
                "indexName": kb_name,
                "knowledgeBaseName": kb_name,
                "projectConnectionId": project_connection_id,
                "mcpServerLabel": mcp_server_label,
                "projectConnectionPrefix": f"kb-{kb_name}-",
                "projectConnectionNameTemplate": f"kb-{kb_name}-{{environment}}",
                "mcpServerUrlTemplate": (
                    f"https://{{searchEndpointHost}}/knowledgebases/{kb_name}/mcp?api-version=2025-11-01-Preview"
                ),
            }

            # Write the KB definition file
            kb_ref = f"foundry/knowledgebases/{kb_name}.json"
            kb_file = repo_root / kb_ref
            kb_repo_payload = {
                "name": kb_name,
                "description": "Knowledgebase definition exported from Dev Azure AI Search.",
                "projectConnectionId": project_connection_id,
                "projectConnectionPrefix": f"kb-{kb_name}-",
                "projectConnectionNameTemplate": f"kb-{kb_name}-{{environment}}",
                "mcpServerUrlTemplate": (
                    f"https://{{searchEndpointHost}}/knowledgebases/{kb_name}/mcp?api-version=2025-11-01-Preview"
                ),
                "knowledgeSourceRefs": [
                    knowledge_source_refs[name]
                    for name in kb_snap.get("knowledgeSourceNames") or []
                    if name in knowledge_source_refs
                ],
                "definition": kb_snap.get("definition") or {},
            }
            write_json(kb_file, kb_repo_payload)
            foundry_iq_payload["knowledgeBaseRef"] = kb_ref

            write_json(foundry_iq_file, foundry_iq_payload)
            foundry_iq_refs.append(foundry_iq_ref)
            synced_files[f"foundryIq:{kb_name}"] = str(foundry_iq_file.relative_to(repo_root))

        synced_agent["foundryIqRefs"] = foundry_iq_refs
        synced_agent.pop("foundryIqRef", None)
    else:
        synced_agent.pop("foundryIqRefs", None)
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
    no_orphan_cleanup: bool,
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
    if not bundle.get("knowledgeBases"):
        fiq = bundle["attachedResources"].get("foundryIq") or []
        bundle["knowledgeBases"] = [
            {
                "name": item.get("name"),
                "projectConnectionId": item.get("projectConnectionId"),
                "knowledgeSourceNames": [],
                "definition": {},
            }
            for item in fiq
            if isinstance(item, dict) and item.get("name")
        ]

    result_json = json.dumps(bundle, indent=2) + "\n"

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result_json)
        print(f"[export] Written to {output_path}", file=sys.stderr)
    else:
        print(result_json)

    if sync_repo:
        synced_files = sync_agent_bundle_to_repo(bundle, repo_root)
        sanitize_repo_assets(repo_root)
        bundle["repoSync"] = synced_files
        print("[export] Synced attached resources into foundry/ files:", file=sys.stderr)
        for _, path in synced_files.items():
            print(f"  - {path}", file=sys.stderr)

        if no_orphan_cleanup:
            print("[export] Orphan cleanup skipped (--no-orphan-cleanup).", file=sys.stderr)
        else:
            move_orphaned_foundry_iq_files(repo_root)

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
    parser.add_argument(
        "--no-orphan-cleanup",
        action="store_true",
        help="Skip moving unreferenced foundry-iq files to _orphaned/.",
    )
    args = parser.parse_args()

    output = Path(args.output) if args.output else None
    try:
        export_agent(
            args.agent_name,
            args.environment,
            output,
            args.sync_repo,
            args.no_orphan_cleanup,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
