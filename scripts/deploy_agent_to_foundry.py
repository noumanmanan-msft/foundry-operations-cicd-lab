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
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Renderer is a pure dict→dict module; import at module level (no network/SDK).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_foundry_templates import render as _render_dict


# ---------------------------------------------------------------------------
# Renderer helpers
# ---------------------------------------------------------------------------

import re as _re
_TOKEN_RE = _re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _render_str(text: str, env_config: dict, label: str) -> str:
    """Render {token} patterns in a plain string (e.g. prompt text)."""
    tokens = _TOKEN_RE.findall(text)
    for token in tokens:
        if token not in env_config:
            raise ValueError(
                f"Unresolved token '{{{token}}}' in {label} — "
                f"add '{token}' to the environment config."
            )
    def _replace(m: _re.Match) -> str:
        return str(env_config[m.group(1)])
    return _TOKEN_RE.sub(_replace, text)


def _require_file(repo_root: Path, ref: str, referenced_by: str) -> Path:
    """Return repo_root/ref, raising clearly if the file is missing."""
    path = repo_root / ref
    if not path.exists():
        raise FileNotFoundError(
            f"Referenced file not found: '{ref}' (referenced by {referenced_by})"
        )
    return path


def render_agent_assets(repo_root: Path, agent_def: dict, env_config: dict) -> dict:
    """Render the agent JSON and every ref'd asset file against env_config.

    Returns a structured dict:
    {
        "agent":    <rendered agent JSON>,
        "model":    <rendered model JSON or None>,
        "prompt":   <rendered prompt text or ''>,
        "guardrail": <rendered guardrail JSON or None>,
        "toolset":  <rendered toolset JSON or None>,
        "memory":   <rendered memory JSON or None>,
        "foundryIq": [
            {
                "foundryIq":       <rendered foundry-iq JSON>,
                "knowledgeBase":   <rendered KB JSON or None>,
                "knowledgeSources": [<rendered KS JSON>, ...]
            },
            ...
        ]
    }

    Raises FileNotFoundError for any missing ref'd file.
    Raises ValueError (from renderer) for any unresolved token.
    """
    agent_name = agent_def.get("name", "<unknown>")
    rendered_agent = _render_dict(agent_def, env_config)

    # --- model ---
    model_cfg = None
    if ref := agent_def.get("modelRef"):
        model_cfg = _render_dict(
            load_json(_require_file(repo_root, ref, f"agent '{agent_name}' modelRef")),
            env_config,
        )

    # --- prompt (plain text) ---
    prompt_text = ""
    if ref := agent_def.get("promptRef"):
        raw = _require_file(repo_root, ref, f"agent '{agent_name}' promptRef").read_text()
        prompt_text = _render_str(raw.strip(), env_config, f"promptRef '{ref}'")

    # --- guardrail ---
    guardrail_cfg = None
    if ref := agent_def.get("guardrailRef"):
        guardrail_cfg = _render_dict(
            load_json(_require_file(repo_root, ref, f"agent '{agent_name}' guardrailRef")),
            env_config,
        )

    # --- toolset ---
    toolset_cfg = None
    if ref := agent_def.get("toolsetRef"):
        toolset_cfg = _render_dict(
            load_json(_require_file(repo_root, ref, f"agent '{agent_name}' toolsetRef")),
            env_config,
        )

    # --- memory ---
    memory_cfg = None
    if ref := agent_def.get("memoryRef"):
        memory_cfg = _render_dict(
            load_json(_require_file(repo_root, ref, f"agent '{agent_name}' memoryRef")),
            env_config,
        )

    # --- foundryIqRefs (one entry per KB) ---
    foundry_iq_entries: list[dict] = []
    for fiq_ref in agent_def.get("foundryIqRefs") or []:
        fiq_raw = load_json(_require_file(repo_root, fiq_ref, f"agent '{agent_name}' foundryIqRefs"))
        # Augment env_config with knowledgeBaseName so {knowledgeBaseName} resolves
        # in mcpServerUrlTemplate (and any other template using it).
        kb_name_for_render = (fiq_raw.get("knowledgeBaseName") or "").strip()
        fiq_env = {**env_config, "knowledgeBaseName": kb_name_for_render} if kb_name_for_render else env_config
        fiq_rendered = _render_dict(fiq_raw, fiq_env)

        kb_cfg = None
        ks_cfgs: list[dict] = []
        if kb_ref := fiq_raw.get("knowledgeBaseRef"):
            kb_raw = load_json(_require_file(repo_root, kb_ref, f"foundry-iq '{fiq_ref}' knowledgeBaseRef"))
            kb_cfg = _render_dict(kb_raw, env_config)
            for ks_ref in kb_raw.get("knowledgeSourceRefs") or []:
                ks_raw = load_json(_require_file(repo_root, ks_ref, f"knowledgeBase '{kb_ref}' knowledgeSourceRefs"))
                ks_cfgs.append(_render_dict(ks_raw, env_config))

        foundry_iq_entries.append({
            "foundryIq": fiq_rendered,
            "knowledgeBase": kb_cfg,
            "knowledgeSources": ks_cfgs,
        })

    return {
        "agent": rendered_agent,
        "model": model_cfg,
        "prompt": prompt_text,
        "guardrail": guardrail_cfg,
        "toolset": toolset_cfg,
        "memory": memory_cfg,
        "foundryIq": foundry_iq_entries,
    }


def _print_dry_run(rendered: dict, environment: str, preflight_progress: list, preflight_failures: list) -> None:
    """Print full dry-run report to stdout."""
    print(f"=== DRY RUN: rendered payloads for environment={environment} ===\n")

    print("--- agent ---")
    print(json.dumps(rendered["agent"], indent=2))
    print()

    for i, entry in enumerate(rendered["foundryIq"]):
        fiq = entry["foundryIq"]
        kb_name = fiq.get("knowledgeBaseName", f"index-{i}")
        print(f"--- foundry-iq[{i}]: {kb_name} ---")
        print(json.dumps(fiq, indent=2))
        print()

        if entry["knowledgeBase"]:
            print(f"--- knowledgebase: {kb_name} ---")
            print(json.dumps(entry["knowledgeBase"], indent=2))
            print()

        for ks in entry["knowledgeSources"]:
            ks_name = ks.get("name", "<unknown>")
            print(f"--- knowledge-source: {ks_name} ---")
            print(json.dumps(ks, indent=2))
            print()

    if rendered["model"]:
        print("--- model ---")
        print(json.dumps(rendered["model"], indent=2))
        print()

    if rendered["guardrail"]:
        print("--- guardrail ---")
        print(json.dumps(rendered["guardrail"], indent=2))
        print()

    if rendered["toolset"]:
        print("--- toolset ---")
        print(json.dumps(rendered["toolset"], indent=2))
        print()

    if rendered["memory"]:
        print("--- memory ---")
        print(json.dumps(rendered["memory"], indent=2))
        print()

    if rendered["prompt"]:
        print("--- prompt ---")
        print(rendered["prompt"])
        print()

    print("=== PRE-FLIGHT VALIDATION ===")
    for line in preflight_progress:
        print(line)
    print()

    if preflight_failures:
        print(f"PRE-FLIGHT FAILURES ({len(preflight_failures)}):")
        for msg in preflight_failures:
            print(f"  - {msg}")
        print()
        print("=== PRE-FLIGHT FAILED ===")
    else:
        print("PRE-FLIGHT PASSED (no failures).")
        print()
        print("=== DRY RUN COMPLETE — no changes made ===")


# ---------------------------------------------------------------------------
# Pre-flight validation helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, token: str) -> int:
    """HTTP GET url with Bearer auth. Returns HTTP status code. Raises on network failures."""
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def preflight_validate(
    rendered: dict,
    env_config: dict,
    environment: str,
    repo_root: "Path",
    project_client,
    credential,
) -> "tuple[list[str], list[str]]":
    """Validate all resources referenced in rendered exist in the target environment.

    Returns (progress_messages, failure_messages). Empty failure_messages = all checks passed.
    Collects all failures before returning — never fast-fails.
    """
    progress: list[str] = []
    failures: list[str] = []

    search_host = env_config.get("searchEndpointHost", "")
    oai_host = env_config.get("azureOpenAIAccountHost", "")
    account_arm_prefix = env_config.get("accountArmPrefix", "")
    project_id = (env_config.get("foundry") or {}).get("projectId", "")
    project_basename = (
        project_id.rstrip("/").split("/projects/")[-1]
        if "/projects/" in project_id
        else project_id.rstrip("/").split("/")[-1]
    )

    # ------------------------------------------------------------------
    # 1. Connection existence
    # ------------------------------------------------------------------
    connection_ids: list[str] = []
    for entry in rendered.get("foundryIq") or []:
        cid = ((entry.get("foundryIq") or {}).get("projectConnectionId") or "").strip()
        if cid and cid not in connection_ids:
            connection_ids.append(cid)

    progress.append(f"[preflight] Checking {len(connection_ids)} project connection(s)...")
    existing_conn_names: set[str] = set()
    conn_check_failed = False
    if connection_ids:
        try:
            for conn in project_client.connections.list():
                existing_conn_names.add(getattr(conn, "name", "") or "")
        except Exception as exc:
            conn_check_failed = True
            for cid in connection_ids:
                failures.append(
                    f"Pre-flight check 'connection existence' could not complete: {exc}. "
                    f"Cannot verify '{cid}'."
                )
    if not conn_check_failed:
        for cid in connection_ids:
            if cid not in existing_conn_names:
                failures.append(
                    f"Project connection '{cid}' not found in environments/{environment} "
                    f"(project: {project_basename})"
                )

    # ------------------------------------------------------------------
    # 2. Azure OpenAI deployment existence (ARM management plane)
    # ------------------------------------------------------------------
    # Only check deploymentId values from KB azureOpenAIParameters blocks —
    # these are real ARM-level Azure OpenAI deployments needed by knowledgebases.
    # The agent model JSON deploymentName is a project-level name not visible at
    # ARM account scope, so we don't validate it here.
    oai_deployments: list[str] = []
    for entry in rendered.get("foundryIq") or []:
        kb = entry.get("knowledgeBase") or {}
        for m in (kb.get("definition") or {}).get("models") or []:
            dep = ((m.get("azureOpenAIParameters") or {}).get("deploymentId") or "").strip()
            if dep and dep not in oai_deployments:
                oai_deployments.append(dep)

    progress.append(f"[preflight] Checking {len(oai_deployments)} Azure OpenAI deployment(s)...")

    # Acquire one ARM token reused by both Check 2 and Check 5.
    arm_token: "str | None" = None
    arm_token_err: str = ""
    if (oai_deployments and account_arm_prefix) or (True):  # always try; needed for RAI too
        try:
            arm_token = credential.get_token("https://management.azure.com/.default").token
        except Exception as exc:
            arm_token_err = str(exc)

    if oai_deployments and account_arm_prefix:
        if arm_token is None:
            for dep in oai_deployments:
                failures.append(
                    f"Pre-flight check 'Azure OpenAI deployment existence' could not complete: "
                    f"{arm_token_err}. Cannot verify '{dep}'."
                )
        else:
            for dep in oai_deployments:
                url = (
                    f"https://management.azure.com{account_arm_prefix}"
                    f"/deployments/{dep}?api-version=2024-10-01"
                )
                try:
                    status = _http_get(url, arm_token)
                    if status == 404:
                        failures.append(
                            f"Azure OpenAI deployment '{dep}' not found on account "
                            f"'{oai_host}' (env: {environment})"
                        )
                    elif status >= 400:
                        failures.append(
                            f"Pre-flight check 'Azure OpenAI deployment existence' could not complete: "
                            f"HTTP {status}. Cannot verify '{dep}'."
                        )
                except Exception as exc:
                    failures.append(
                        f"Pre-flight check 'Azure OpenAI deployment existence' could not complete: {exc}. "
                        f"Cannot verify '{dep}'."
                    )

    # ------------------------------------------------------------------
    # 3. Search KB existence
    # ------------------------------------------------------------------
    kb_names: list[str] = []
    for entry in rendered.get("foundryIq") or []:
        kb_name = ((entry.get("foundryIq") or {}).get("knowledgeBaseName") or "").strip()
        if kb_name and kb_name not in kb_names:
            kb_names.append(kb_name)

    # ------------------------------------------------------------------
    # 4. Knowledge source existence
    # ------------------------------------------------------------------
    ks_names: list[str] = []
    for entry in rendered.get("foundryIq") or []:
        kb = entry.get("knowledgeBase") or {}
        for ks in (kb.get("definition") or {}).get("knowledgeSources") or []:
            ks_name = (ks.get("name") or "").strip()
            if ks_name and ks_name not in ks_names:
                ks_names.append(ks_name)

    progress.append(f"[preflight] Checking {len(kb_names)} search knowledge base(s)...")
    progress.append(f"[preflight] Checking {len(ks_names)} search knowledge source(s)...")

    search_token: "str | None" = None
    if (kb_names or ks_names) and search_host:
        try:
            search_token = credential.get_token("https://search.azure.com/.default").token
        except Exception as exc:
            for kb in kb_names:
                failures.append(
                    f"Pre-flight check 'search KB existence' could not complete: {exc}. "
                    f"Cannot verify '{kb}'."
                )
            for ks in ks_names:
                failures.append(
                    f"Pre-flight check 'search knowledge source existence' could not complete: {exc}. "
                    f"Cannot verify '{ks}'."
                )

    if search_token:
        for kb in kb_names:
            url = f"https://{search_host}/knowledgebases/{kb}?api-version=2025-11-01-Preview"
            try:
                status = _http_get(url, search_token)
                if status == 404:
                    failures.append(
                        f"Search knowledge base '{kb}' not found on '{search_host}' (env: {environment})"
                    )
                elif status >= 400:
                    failures.append(
                        f"Pre-flight check 'search KB existence' could not complete: "
                        f"HTTP {status}. Cannot verify '{kb}'."
                    )
            except Exception as exc:
                failures.append(
                    f"Pre-flight check 'search KB existence' could not complete: {exc}. "
                    f"Cannot verify '{kb}'."
                )

        for ks in ks_names:
            url = f"https://{search_host}/knowledgeSources/{ks}?api-version=2025-11-01-Preview"
            try:
                status = _http_get(url, search_token)
                if status == 404:
                    failures.append(
                        f"Search knowledge source '{ks}' not found on '{search_host}' (env: {environment})"
                    )
                elif status >= 400:
                    failures.append(
                        f"Pre-flight check 'search knowledge source existence' could not complete: "
                        f"HTTP {status}. Cannot verify '{ks}'."
                    )
            except Exception as exc:
                failures.append(
                    f"Pre-flight check 'search knowledge source existence' could not complete: {exc}. "
                    f"Cannot verify '{ks}'."
                )

    # ------------------------------------------------------------------
    # 5. RAI policy existence
    # ------------------------------------------------------------------
    rai_policy_name = ((rendered.get("guardrail") or {}).get("raiPolicyName") or "").strip()
    progress.append(f"[preflight] Checking {1 if rai_policy_name else 0} RAI policy...")

    if rai_policy_name and account_arm_prefix:
        if arm_token is None:
            failures.append(
                f"Pre-flight check 'RAI policy existence' could not complete: "
                f"{arm_token_err}. Cannot verify '{rai_policy_name}'."
            )
        else:
            rai_arm_path = f"{account_arm_prefix}/raiPolicies/{rai_policy_name}"
            url = f"https://management.azure.com{rai_arm_path}?api-version=2024-10-01"
            try:
                status = _http_get(url, arm_token)
                if status == 404:
                    failures.append(
                        f"RAI policy '{rai_policy_name}' not found on Foundry account "
                        f"(env: {environment}, ARM path: {rai_arm_path})"
                    )
                elif status >= 400:
                    failures.append(
                        f"Pre-flight check 'RAI policy existence' could not complete: "
                        f"HTTP {status}. Cannot verify '{rai_policy_name}'."
                    )
            except Exception as exc:
                failures.append(
                    f"Pre-flight check 'RAI policy existence' could not complete: {exc}. "
                    f"Cannot verify '{rai_policy_name}'."
                )

    # ------------------------------------------------------------------
    # 6. Ref file existence (disk sanity)
    # ------------------------------------------------------------------
    agent_json = rendered.get("agent") or {}
    agent_name_val = agent_json.get("name", "<unknown>")
    file_refs: "list[tuple[str, str]]" = []
    for field, val in agent_json.items():
        if field.endswith("Ref"):
            if isinstance(val, str) and val:
                file_refs.append((field, val))
        elif field == "foundryIqRefs":
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item:
                        file_refs.append(("foundryIqRefs", item))

    progress.append(f"[preflight] Checking {len(file_refs)} referenced file(s) on disk...")
    for field_name, ref_path in file_refs:
        if not (repo_root / ref_path).exists():
            failures.append(
                f"Referenced file '{ref_path}' from agent '{agent_name_val}' "
                f"field '{field_name}' does not exist on disk"
            )

    return progress, failures


# ---------------------------------------------------------------------------
# Deployment summary helpers
# ---------------------------------------------------------------------------

def _get_git_sha(repo_root: "Path") -> str:
    """Return HEAD commit SHA, or 'unknown' if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _print_deploy_summary(
    rendered: dict,
    environment: str,
    git_sha: str,
    agent_result: "dict | None",
    warnings: list,
    dry_run: bool,
    *,
    file=None,
) -> None:
    """Print a structured deployment summary (stdout in dry-run, stderr in live mode)."""
    if file is None:
        file = sys.stdout if dry_run else sys.stderr

    if dry_run:
        print(f"=== DEPLOYMENT SUMMARY (DRY RUN \u2014 would have set metadata.version={git_sha}) ===", file=file)
    else:
        print("=== DEPLOYMENT SUMMARY ===", file=file)
    print(file=file)

    if dry_run:
        agent_name = (rendered.get("agent") or {}).get("name", "<unknown>")
        print(f"  agent:       {agent_name} (not deployed \u2014 dry run)", file=file)
    else:
        print(f"  agentId:     {(agent_result or {}).get('agentId', '?')}", file=file)
        print(f"  name:        {(agent_result or {}).get('name', '?')}", file=file)
    print(f"  environment: {environment}", file=file)
    print(f"  git SHA:     {git_sha}", file=file)
    if not dry_run and agent_result:
        print(f"  endpoint:    {agent_result.get('endpoint', '?')}", file=file)
    print(file=file)

    deployed: list = ["agent"]
    skipped: list = []

    fiq_entries = rendered.get("foundryIq") or []
    if fiq_entries:
        deployed.append(f"{len(fiq_entries)} foundry-iq config(s)")
        kb_count = sum(1 for e in fiq_entries if e.get("knowledgeBase"))
        if kb_count:
            deployed.append(f"{kb_count} knowledgebase definition(s)")
        ks_count = sum(len(e.get("knowledgeSources") or []) for e in fiq_entries)
        if ks_count:
            deployed.append(f"{ks_count} knowledge source(s)")
    else:
        skipped.append("foundryIq (no foundryIqRefs in agent)")

    for sec in ("model", "guardrail", "toolset", "memory"):
        if rendered.get(sec):
            deployed.append(sec)
        else:
            skipped.append(f"{sec} (no {sec}Ref in agent JSON)")
    if rendered.get("prompt"):
        deployed.append("prompt")
    else:
        skipped.append("prompt (no promptRef in agent JSON)")

    print("  sections deployed:", file=file)
    for s in deployed:
        print(f"    + {s}", file=file)
    if skipped:
        print("  sections skipped:", file=file)
        for s in skipped:
            print(f"    - {s}", file=file)
    print(file=file)

    if warnings:
        print(f"  warnings ({len(warnings)}):", file=file)
        for w in warnings:
            print(f"    ! {w}", file=file)
    else:
        print("  warnings: none", file=file)
    print(file=file)


# ---------------------------------------------------------------------------
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


def resolve_foundry_iq_configs(repo_root: Path, agent_def: dict) -> list[dict]:
    """Return a list of per-KB foundry-iq config dicts sourced from foundryIqRefs.

    Each entry in foundryIqRefs is a path to a JSON file that contains all fields
    needed to wire an MCP tool for one KB: knowledgeBaseName, projectConnectionNameTemplate,
    mcpServerUrlTemplate, knowledgeBaseRef, etc.
    Returns an empty list when foundryIqRefs is absent (triggers legacy fallback in caller).
    """
    refs = agent_def.get("foundryIqRefs") or []
    configs = []
    for ref in refs:
        cfg = maybe_load_json(repo_root, ref)
        if cfg:
            configs.append(cfg)
    return configs


def resolve_knowledgebase_config(repo_root: Path, foundry_iq_cfg: dict) -> dict:
    kb_ref = foundry_iq_cfg.get("knowledgeBaseRef")
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


def find_connection_by_name_or_prefix(connections_client, expected_name: str | None, prefix: str | None):
    """Find a project connection by exact name/id first, then by name prefix."""
    try:
        connections = list(connections_client.list())
    except Exception as exc:
        print(f"[deploy] Warning: could not list connections: {exc}", file=sys.stderr)
        return None

    if expected_name:
        expected = expected_name.strip().lower()
        for conn in connections:
            conn_name = str(getattr(conn, "name", "") or "").strip().lower()
            conn_id = str(getattr(conn, "id", "") or "").strip().lower()
            if conn_name == expected or conn_id == expected:
                return conn

    if prefix:
        starts_with = prefix.strip().lower()
        for conn in connections:
            conn_name = str(getattr(conn, "name", "") or "").strip().lower()
            if conn_name.startswith(starts_with):
                return conn

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


def deploy_agent(agent_name: str, environment: str, version: str | None, dry_run: bool = False) -> dict:
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import AzureAISearchTool
    from azure.identity import DefaultAzureCredential

    repo_root = Path(__file__).resolve().parents[1]
    git_sha = _get_git_sha(repo_root)
    config_path = repo_root / "environments" / environment / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Environment config not found: {config_path}")

    config = load_json(config_path)
    endpoint = build_project_endpoint(config)
    model_deployment = config["foundry"]["defaultModelDeployment"]

    # --- Load and render agent definition and all referenced assets ---
    agent_file = resolve_agent_file(repo_root, agent_name)
    agent_def_raw = load_json(agent_file)
    print(f"[deploy] Loaded agent definition: {agent_file}", file=sys.stderr)

    print(f"[deploy] Rendering assets for environment={environment}...", file=sys.stderr)
    rendered = render_agent_assets(repo_root, agent_def_raw, config)

    # Connect to target project — needed for pre-flight validation even in dry-run
    # (pre-flight makes read-only GET calls; dry-run never mutates Azure state).
    print(f"[deploy] Connecting to project for pre-flight validation...", file=sys.stderr)
    credential = DefaultAzureCredential()
    client = AIProjectClient(endpoint=endpoint, credential=credential)

    # Run pre-flight: collect all failures before touching any Azure state.
    preflight_progress, preflight_failures = preflight_validate(
        rendered, config, environment, repo_root, client, credential
    )

    if dry_run:
        _print_dry_run(rendered, environment, preflight_progress, preflight_failures)
        _print_deploy_summary(rendered, environment, git_sha, None, [], dry_run=True)
        sys.exit(1 if preflight_failures else 0)

    # Live deploy: refuse to proceed if pre-flight failed.
    if preflight_failures:
        print("\nPRE-FLIGHT FAILURES:", file=sys.stderr)
        for msg in preflight_failures:
            print(f"  - {msg}", file=sys.stderr)
        print("=== PRE-FLIGHT FAILED — no changes made ===", file=sys.stderr)
        sys.exit(1)

    # Live deploy path — use rendered versions of all assets.
    agent_def = rendered["agent"]
    model_cfg = rendered["model"] or {}
    system_prompt = rendered["prompt"]
    guardrail_cfg = rendered["guardrail"] or {}
    foundry_iq_configs = [entry["foundryIq"] for entry in rendered["foundryIq"]]
    memory_cfg = rendered["memory"] or {}

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

    iq_names = [c.get("name", "?") for c in foundry_iq_configs]
    print(
        f"[deploy] Assets rendered — model={model_cfg.get('name', model_deployment)}, "
        f"guardrail={rai_policy_basename or 'none'}, "
        f"foundryIqConfigs={iq_names}, "
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
    metadata["version"] = f"{version}+{git_sha}" if version else git_sha

    # Foundry metadata values must be strings ≤ 256 chars
    metadata = {k: str(v)[:256] for k, v in metadata.items()}
    deploy_warnings: list = []

    # --- Connect to target environment (credential + client already created above) ---
    print(f"[deploy] Target endpoint: {endpoint}", file=sys.stderr)

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

    # --- Build tool list: AI Search for knowledge grounding ---
    tools: list = []
    tool_resources = None

    connection_name_suffix = config.get("connectionNameSuffix", environment)
    search_endpoint = ((config.get("knowledge") or {}).get("searchEndpoint") or "").rstrip("/")

    # --- Build MCP tool list: one tool per KB in foundryIqRefs ---
    tools: list = []
    tool_resources = None

    for foundry_iq_cfg in foundry_iq_configs:
        knowledgebase_cfg = resolve_knowledgebase_config(repo_root, foundry_iq_cfg)
        if knowledgebase_cfg and search_endpoint:
            for ref in knowledgebase_cfg.get("knowledgeSourceRefs") or []:
                src_cfg = maybe_load_json(repo_root, ref)
                if src_cfg:
                    ensure_knowledge_source_in_search(credential, search_endpoint, src_cfg)
            ensure_knowledgebase_in_search(credential, search_endpoint, knowledgebase_cfg)

        knowledge_base_name = (foundry_iq_cfg.get("knowledgeBaseName") or "").strip()
        if not knowledge_base_name:
            continue

        project_connection_id = (foundry_iq_cfg.get("projectConnectionId") or "").strip()
        project_connection_template = (foundry_iq_cfg.get("projectConnectionNameTemplate") or "").strip()
        project_connection_prefix = (foundry_iq_cfg.get("projectConnectionPrefix") or "").strip()
        mcp_server_url_template = (foundry_iq_cfg.get("mcpServerUrlTemplate") or "").strip()

        prefix = project_connection_prefix or f"kb-{knowledge_base_name}-"
        expected_connection_name = ""
        if project_connection_template:
            expected_connection_name = render_template(
                project_connection_template,
                {
                    "environment": environment,
                    "connectionNameSuffix": connection_name_suffix,
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
                        "connectionNameSuffix": connection_name_suffix,
                    },
                )
            else:
                kb_server_url = (
                    f"{search_endpoint}/knowledgebases/{knowledge_base_name}/mcp"
                    "?api-version=2025-11-01-Preview"
                )
            tools.append(
                {
                    "type": "mcp",
                    "server_label": f"kb_{knowledge_base_name}".replace("-", "_"),
                    "server_url": kb_server_url,
                    "project_connection_id": kb_conn_name,
                }
            )
            print(
                f"[deploy] KB '{knowledge_base_name}': MCP tool wired via connection '{kb_conn_name}'",
                file=sys.stderr,
            )
        else:
            _w = f"KB '{knowledge_base_name}': no matching connection or search endpoint resolved; MCP tool skipped."
            print(f"[deploy] Warning: {_w}", file=sys.stderr)
            deploy_warnings.append(_w)

    if not tools:
        search_conn = find_search_connection(client.connections)
        if search_conn:
            print(
                f"[deploy] No Foundry IQ tools wired; falling back to AzureAISearchTool via connection "
                f"'{getattr(search_conn, 'name', search_conn.id)}'.",
                file=sys.stderr,
            )
            fallback_index = next(
                (c.get("indexName") for c in foundry_iq_configs if c.get("indexName")), None
            )
            if fallback_index:
                ai_search = AzureAISearchTool(
                    index_connection_id=search_conn.id,
                    index_name=fallback_index,
                )
                tools = ai_search.definitions
                tool_resources = ai_search.resources
            else:
                print(
                    "[deploy] Warning: search connection exists but no index name found — "
                    "agent will deploy without knowledge grounding.",
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
                _w = (
                    f"RAI policy '{rai_policy_name}' does not exist in {environment} "
                    "\u2014 deployed without guardrail. Create the policy in this Foundry account to enforce it."
                )
                print(f"[deploy] Warning: {_w}", file=sys.stderr)
                deploy_warnings.append(_w)
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
            "version": metadata.get("version"),
            "endpoint": endpoint,
        }
        _print_deploy_summary(rendered, environment, git_sha, summary, deploy_warnings, dry_run=False)
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
        "version": metadata.get("version"),
        "endpoint": endpoint,
    }
    _print_deploy_summary(rendered, environment, git_sha, summary, deploy_warnings, dry_run=False)
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Render all assets against the target environment config and print "
            "the substituted payloads to stdout. No Azure SDK calls are made and "
            "no state is mutated. Use this to validate token substitution and "
            "inspect exactly what would be deployed before committing to a live run."
        ),
    )
    args = parser.parse_args()

    try:
        deploy_agent(args.agent_name, args.environment, args.version, dry_run=args.dry_run)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
