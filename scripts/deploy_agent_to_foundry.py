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
import urllib.error
import urllib.request
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


def maybe_load_json(repo_root: Path, path_str: str | None) -> dict:
    if not path_str:
        return {}
    return load_json(repo_root / path_str)


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{key}}}", value)
    return rendered


def endpoint_host(endpoint: str) -> str:
    return endpoint.replace("https://", "").replace("http://", "").strip().rstrip("/")


def ensure_knowledgebase_in_search(credential, search_endpoint: str, knowledgebase_cfg: dict) -> bool:
    """Create or update a Search knowledgebase in the target environment."""
    if not search_endpoint:
        print("[deploy] Warning: search endpoint missing; cannot sync knowledgebase.", file=sys.stderr)
        return False

    kb_definition = knowledgebase_cfg.get("definition") or {}
    kb_name = (kb_definition.get("name") or knowledgebase_cfg.get("name") or "").strip()
    if not kb_name:
        print("[deploy] Warning: knowledgebase config has no name; skipping kb sync.", file=sys.stderr)
        return False

    payload = {k: v for k, v in dict(kb_definition).items() if not str(k).startswith("@odata")}
    payload["name"] = kb_name

    try:
        token = credential.get_token("https://search.azure.com/.default")
    except Exception as exc:
        print(f"[deploy] Warning: could not acquire Search token for kb sync: {exc}", file=sys.stderr)
        return False

    url = (
        f"{search_endpoint.rstrip('/')}/knowledgebases/{kb_name}"
        "?api-version=2025-11-01-Preview"
    )
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Authorization", f"Bearer {token.token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
            print(
                f"[deploy] Knowledgebase '{kb_name}' synced to {search_endpoint}",
                file=sys.stderr,
            )
            return True
    except urllib.error.HTTPError as exc:
        details = exc.read()[:300].decode(errors="replace")
        print(
            f"[deploy] Warning: Search PUT knowledgebases/{kb_name} returned {exc.code}: {details}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"[deploy] Warning: could not sync knowledgebase '{kb_name}': {exc}", file=sys.stderr)
        return False


def ensure_knowledge_source_in_search(credential, search_endpoint: str, source_cfg: dict) -> bool:
    """Create or update a Search knowledge source in the target environment."""
    if not search_endpoint:
        print("[deploy] Warning: search endpoint missing; cannot sync knowledge source.", file=sys.stderr)
        return False

    src_definition = source_cfg.get("definition") or {}
    src_name = (src_definition.get("name") or source_cfg.get("name") or "").strip()
    if not src_name:
        print("[deploy] Warning: knowledge source config has no name; skipping source sync.", file=sys.stderr)
        return False

    payload = {k: v for k, v in dict(src_definition).items() if not str(k).startswith("@odata")}
    payload["name"] = src_name

    try:
        token = credential.get_token("https://search.azure.com/.default")
    except Exception as exc:
        print(f"[deploy] Warning: could not acquire Search token for source sync: {exc}", file=sys.stderr)
        return False

    url = (
        f"{search_endpoint.rstrip('/')}/knowledgeSources/{src_name}"
        "?api-version=2025-11-01-Preview"
    )
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Authorization", f"Bearer {token.token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
            print(
                f"[deploy] Knowledge source '{src_name}' synced to {search_endpoint}",
                file=sys.stderr,
            )
            return True
    except urllib.error.HTTPError as exc:
        details = exc.read()[:300].decode(errors="replace")
        print(
            f"[deploy] Warning: Search PUT knowledgeSources/{src_name} returned {exc.code}: {details}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"[deploy] Warning: could not sync knowledge source '{src_name}': {exc}", file=sys.stderr)
        return False


def resolve_index_config(repo_root: Path, agent_def: dict) -> tuple[dict, dict, dict]:
    """Resolve index metadata from legacy and new refs.

    Resolution order:
    1) knowledgeIndexRef (legacy, still supported)
    2) knowledgeRef -> indexRef
    3) foundryIqRef -> indexRef / knowledgeRef
    """
    index_cfg = maybe_load_json(repo_root, agent_def.get("knowledgeIndexRef"))
    knowledge_cfg = maybe_load_json(repo_root, agent_def.get("knowledgeRef"))
    foundry_iq_cfg = maybe_load_json(repo_root, agent_def.get("foundryIqRef"))

    if not knowledge_cfg and foundry_iq_cfg.get("knowledgeRef"):
        knowledge_cfg = maybe_load_json(repo_root, foundry_iq_cfg.get("knowledgeRef"))

    if not index_cfg:
        if knowledge_cfg.get("indexRef"):
            index_cfg = maybe_load_json(repo_root, knowledge_cfg.get("indexRef"))
        elif foundry_iq_cfg.get("indexRef"):
            index_cfg = maybe_load_json(repo_root, foundry_iq_cfg.get("indexRef"))

    if not index_cfg and knowledge_cfg.get("indexName"):
        index_cfg = {
            "name": knowledge_cfg["indexName"],
            "retrieval": knowledge_cfg.get("retrieval", {"mode": "hybrid"}),
            "connectionId": knowledge_cfg.get("connectionId"),
        }

    return index_cfg, knowledge_cfg, foundry_iq_cfg


def resolve_knowledgebase_config(repo_root: Path, index_cfg: dict, knowledge_cfg: dict, foundry_iq_cfg: dict) -> dict:
    kb_ref = (
        foundry_iq_cfg.get("knowledgeBaseRef")
        or knowledge_cfg.get("knowledgeBaseRef")
        or index_cfg.get("knowledgeBaseRef")
    )
    return maybe_load_json(repo_root, kb_ref)


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


def find_connection_by_name_or_prefix(connections_client, exact_name: str | None, prefix: str | None):
    """Resolve a project connection by exact name first, then by prefix."""
    try:
        matches = []
        for conn in connections_client.list():
            conn_name = getattr(conn, "name", "") or ""
            if exact_name and conn_name == exact_name:
                return conn
            if prefix and conn_name.startswith(prefix):
                matches.append(conn)
        if matches:
            return matches[0]
    except Exception as exc:
        print(f"[deploy] Warning: could not resolve connection by name/prefix: {exc}", file=sys.stderr)
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


def ensure_rai_policy_in_arm(credential, foundry_resource_id: str, guardrail_cfg: dict) -> bool:
    """Create or update the RAI content filter policy in ARM for the target environment.

    Reads contentFilters from the guardrail config (written by the exporter) and PUTs them
    to the target Foundry account so that the exact Dev content filter thresholds (severity
    levels, blocking flags, enabled/disabled states) are replicated before the agent version
    is created. Returns True if the policy was synced successfully.
    """
    import urllib.error
    import urllib.request

    policy_basename = guardrail_cfg.get("raiPolicyName")
    content_filters = guardrail_cfg.get("contentFilters")
    if not (policy_basename and content_filters and foundry_resource_id):
        return False

    try:
        token = credential.get_token("https://management.azure.com/.default")
    except Exception as exc:
        print(f"[deploy] Warning: could not acquire ARM token for RAI policy sync: {exc}", file=sys.stderr)
        return False

    body = json.dumps({
        "properties": {
            "basePolicyName": guardrail_cfg.get("basePolicyName", "Microsoft.DefaultV2"),
            "mode": guardrail_cfg.get("mode", "Deferred"),
            "contentFilters": content_filters,
        }
    }).encode()

    url = (
        f"https://management.azure.com{foundry_resource_id}"
        f"/raiPolicies/{policy_basename}?api-version=2024-10-01"
    )
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Authorization", f"Bearer {token.token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
            account_name = foundry_resource_id.rstrip("/").split("/")[-1]
            print(
                f"[deploy] RAI policy '{policy_basename}' content filters synced to {account_name}",
                file=sys.stderr,
            )
            return True
    except urllib.error.HTTPError as exc:
        err_body = exc.read()
        print(
            f"[deploy] Warning: ARM PUT raiPolicies/{policy_basename} returned {exc.code}: "
            f"{err_body[:200].decode(errors='replace')}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"[deploy] Warning: could not sync RAI policy to ARM: {exc}", file=sys.stderr)
        return False


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

    model_cfg = maybe_load_json(repo_root, agent_def.get("modelRef"))
    prompt_ref = agent_def.get("promptRef")
    system_prompt = (repo_root / prompt_ref).read_text().strip() if prompt_ref else ""
    guardrail_cfg = maybe_load_json(repo_root, agent_def.get("guardrailRef"))
    index_cfg, knowledge_cfg, foundry_iq_cfg = resolve_index_config(repo_root, agent_def)
    knowledgebase_cfg = resolve_knowledgebase_config(repo_root, index_cfg, knowledge_cfg, foundry_iq_cfg)
    memory_cfg = maybe_load_json(repo_root, agent_def.get("memoryRef"))

    # Build the environment-specific RAI policy resource path from the guardrail basename.
    # The guardrail JSON stores only the policy name (e.g. "Base-Guardrails-v1") so the
    # deployer can resolve it against whichever account it is deploying to.
    rai_policy_basename = guardrail_cfg.get("raiPolicyName") if guardrail_cfg else None
    foundry_resource_id = config["foundry"].get("resourceId", "")
    rai_policy_name: str | None = (
        f"{foundry_resource_id}/raiPolicies/{rai_policy_basename}"
        if rai_policy_basename and foundry_resource_id
        else None
    )

    print(
        f"[deploy] Assets loaded — model={model_cfg.get('name', model_deployment)}, "
        f"guardrail={rai_policy_basename or 'none'}, "
        f"index={index_cfg.get('name', 'none')}, "
        f"knowledge={knowledge_cfg.get('name', 'none')}, "
        f"foundryIq={foundry_iq_cfg.get('name', 'none')}, "
        f"knowledgebase={knowledgebase_cfg.get('name', 'none')}, "
        f"memory={memory_cfg.get('name', 'none')}",
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

    # --- Sync RAI content filter policy to target environment via ARM ---
    # If the guardrail config contains contentFilters (exported from Dev via ARM), PUT them
    # to the target account so the policy exists with the correct thresholds before the agent
    # version is created.
    if guardrail_cfg.get("contentFilters") and foundry_resource_id:
        print(
            f"[deploy] Syncing content filters for RAI policy '{rai_policy_basename}' to {environment}...",
            file=sys.stderr,
        )
        ensure_rai_policy_in_arm(credential, foundry_resource_id, guardrail_cfg)

    # --- Build tool list: Foundry IQ MCP tool when configured, else AI Search fallback ---
    tools: list = []
    tool_resources = None

    search_endpoint = ((config.get("knowledge") or {}).get("searchEndpoint") or "").rstrip("/")
    if knowledgebase_cfg and search_endpoint:
        for ref in knowledgebase_cfg.get("knowledgeSourceRefs") or []:
            src_cfg = maybe_load_json(repo_root, ref)
            if src_cfg:
                ensure_knowledge_source_in_search(credential, search_endpoint, src_cfg)
        ensure_knowledgebase_in_search(credential, search_endpoint, knowledgebase_cfg)

    knowledge_base_name = (foundry_iq_cfg.get("knowledgeBaseName") or knowledge_cfg.get("knowledgeBaseName") or "").strip()
    project_connection_id = (
        foundry_iq_cfg.get("projectConnectionId")
        or knowledge_cfg.get("projectConnectionId")
        or index_cfg.get("projectConnectionId")
        or ""
    ).strip()
    project_connection_template = (
        foundry_iq_cfg.get("projectConnectionNameTemplate")
        or knowledge_cfg.get("projectConnectionNameTemplate")
        or index_cfg.get("projectConnectionNameTemplate")
        or ""
    ).strip()
    project_connection_prefix = (
        foundry_iq_cfg.get("projectConnectionPrefix")
        or knowledge_cfg.get("projectConnectionPrefix")
        or index_cfg.get("projectConnectionPrefix")
        or ""
    ).strip()
    mcp_server_url_template = (
        foundry_iq_cfg.get("mcpServerUrlTemplate")
        or knowledge_cfg.get("mcpServerUrlTemplate")
        or index_cfg.get("mcpServerUrlTemplate")
        or ""
    ).strip()

    if knowledge_base_name:
        prefix = project_connection_prefix or f"kb-{knowledge_base_name}-"

        expected_connection_name = ""
        if project_connection_template:
            expected_connection_name = render_template(
                project_connection_template,
                {
                    "environment": environment,
                    "knowledgeBaseName": knowledge_base_name,
                },
            )

        kb_conn = None
        if expected_connection_name:
            kb_conn = find_connection_by_name_or_prefix(client.connections, expected_connection_name, None)
        if kb_conn is None:
            kb_conn = find_connection_by_name_or_prefix(client.connections, project_connection_id or None, prefix)

        if kb_conn and search_endpoint:
            kb_conn_name = getattr(kb_conn, "name", project_connection_id)
            if mcp_server_url_template:
                kb_server_url = render_template(
                    mcp_server_url_template,
                    {
                        "searchEndpoint": search_endpoint,
                        "searchEndpointHost": endpoint_host(search_endpoint),
                        "knowledgeBaseName": knowledge_base_name,
                        "environment": environment,
                    },
                )
            else:
                kb_server_url = (
                    f"{search_endpoint}/knowledgebases/{knowledge_base_name}/mcp"
                    "?api-version=2025-11-01-Preview"
                )
            tools = [
                {
                    "type": "mcp",
                    "server_label": f"kb_{knowledge_base_name}".replace("-", "_"),
                    "server_url": kb_server_url,
                    "project_connection_id": kb_conn_name,
                }
            ]
            print(
                f"[deploy] Using Foundry IQ MCP knowledgebase tool '{knowledge_base_name}' via connection '{kb_conn_name}'",
                file=sys.stderr,
            )
        else:
            print(
                "[deploy] Warning: Foundry IQ metadata found but matching connection/search endpoint was not resolved. "
                "Falling back to AzureAISearchTool wiring.",
                file=sys.stderr,
            )

    if not tools:
        search_conn = find_search_connection(client.connections)
        if search_conn and index_cfg.get("name"):
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
        elif search_conn and not index_cfg.get("name"):
            print(
                "[deploy] Warning: search connection exists, but agent has no attached knowledge index metadata.",
                file=sys.stderr,
            )
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
    if rai_policy_name:
        definition["rai_config"] = {"rai_policy_name": rai_policy_name}
        print(f"[deploy] Applying guardrail RAI policy: {rai_policy_name}", file=sys.stderr)
    else:
        print("[deploy] Warning: no guardrailRef found — agent will deploy without RAI policy.", file=sys.stderr)

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
        try:
            agent = create_version_fn(
                agent_name=agent_name,
                definition=definition,
                metadata=metadata,
                description=agent_def.get("description", ""),
            )
        except Exception as exc:
            exc_str = str(exc).lower()
            if rai_policy_name and ("rai policy" in exc_str or "raipolici" in exc_str or "bad_request" in exc_str):
                print(
                    f"[deploy] Warning: RAI policy '{rai_policy_name}' does not exist in this environment. "
                    "Deploying without guardrail — create the policy in this Foundry account to enforce it.",
                    file=sys.stderr,
                )
                definition_no_rai = {k: v for k, v in definition.items() if k != "rai_config"}
                agent = create_version_fn(
                    agent_name=agent_name,
                    definition=definition_no_rai,
                    metadata=metadata,
                    description=agent_def.get("description", ""),
                )
            else:
                raise
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
