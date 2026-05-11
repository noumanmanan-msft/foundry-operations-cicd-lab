#!/usr/bin/env python3
"""Verify deployed Foundry agent wiring matches committed multi-KB definitions.

Checks:
- Agent manifest foundryIqRefs is authoritative.
- Deployed MCP tools match expected one-per-KB exactly (no missing/extras).
- Each tool is environment-scoped (connection suffix + search host + KB path).
- Referenced knowledge sources exist on Search in target environment.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from azure.identity import DefaultAzureCredential


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{key}}}", value)
    return rendered


def resolve_agent_file(repo_root: Path, agent_name: str) -> Path:
    agents_root = repo_root / "foundry" / "agents"
    for candidate in sorted(agents_root.rglob("*.json")):
        try:
            payload = load_json(candidate)
        except Exception:
            continue
        if payload.get("name") == agent_name:
            return candidate
    raise FileNotFoundError(
        f"No agent definition with name='{agent_name}' found under foundry/agents/."
    )


def fetch_export_bundle(repo_root: Path, agent_name: str, environment: str) -> dict:
    with tempfile.NamedTemporaryFile(prefix="verify-deployed-", suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)

    try:
        cmd = [
            sys.executable,
            str(repo_root / "scripts" / "export_agent_from_foundry.py"),
            "--agent-name",
            agent_name,
            "--environment",
            environment,
            "--output",
            str(out_path),
        ]
        result = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                "Failed to export deployed agent state:\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return load_json(out_path)
    finally:
        if out_path.exists():
            out_path.unlink()


def extract_knowledge_source_names(repo_root: Path, fiq_payload: dict) -> list[str]:
    names: list[str] = []
    kb_ref = (fiq_payload.get("knowledgeBaseRef") or "").strip()
    if not kb_ref:
        return names

    kb_payload = load_json(repo_root / kb_ref)
    for ks_ref in kb_payload.get("knowledgeSourceRefs") or []:
        ks_payload = load_json(repo_root / ks_ref)
        ks_name = (ks_payload.get("name") or (ks_payload.get("definition") or {}).get("name") or "").strip()
        if ks_name:
            names.append(ks_name)
    return names


def http_get_status(url: str, token: str) -> int:
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        resp.read()
        return resp.status


def verify_knowledge_sources_exist(search_host: str, source_names: set[str]) -> list[str]:
    errors: list[str] = []
    if not source_names:
        return errors

    cred = DefaultAzureCredential()
    try:
        token = cred.get_token("https://search.azure.com/.default").token
    except Exception as exc:
        return [
            f"Could not acquire Search token for knowledge source verification: {exc}"
        ]

    for ks_name in sorted(source_names):
        url = f"https://{search_host}/knowledgeSources/{ks_name}?api-version=2025-11-01-Preview"
        try:
            status = http_get_status(url, token)
            if status != 200:
                errors.append(
                    f"Knowledge source '{ks_name}' returned unexpected HTTP {status} on '{search_host}'"
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                errors.append(
                    f"Knowledge source '{ks_name}' not found on '{search_host}'"
                )
            else:
                body = exc.read()[:250].decode(errors="replace")
                errors.append(
                    f"Could not verify knowledge source '{ks_name}' on '{search_host}': HTTP {exc.code} {body}"
                )
        except Exception as exc:
            errors.append(
                f"Could not verify knowledge source '{ks_name}' on '{search_host}': {exc}"
            )

    return errors


def infer_kb_from_url(server_url: str) -> str:
    m = re.search(r"/knowledgebases/([^/]+)/mcp", server_url or "")
    return m.group(1) if m else ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify deployed Foundry agent MCP wiring and knowledge source presence."
    )
    parser.add_argument("--agent-name", required=True, help="Agent name in foundry/agents/.")
    parser.add_argument("--environment", required=True, choices=["dev", "qa", "prod"], help="Target environment.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    env_cfg = load_json(repo_root / "environments" / args.environment / "config.json")
    search_host = env_cfg.get("searchEndpointHost", "")
    connection_suffix = (env_cfg.get("connectionNameSuffix") or args.environment).strip()

    agent_file = resolve_agent_file(repo_root, args.agent_name)
    agent_payload = load_json(agent_file)
    fiq_refs = agent_payload.get("foundryIqRefs") or []
    if not isinstance(fiq_refs, list) or not fiq_refs:
        raise SystemExit(
            f"Agent '{args.agent_name}' has no foundryIqRefs in {agent_file}; cannot run multi-KB verification."
        )

    expected: list[dict] = []
    expected_source_names: set[str] = set()

    for fiq_ref in fiq_refs:
        fiq_payload = load_json(repo_root / fiq_ref)
        kb_name = (fiq_payload.get("knowledgeBaseName") or "").strip()
        if not kb_name:
            raise SystemExit(f"foundry-iq '{fiq_ref}' missing knowledgeBaseName")

        vals = {
            "environment": args.environment,
            "connectionNameSuffix": connection_suffix,
            "knowledgeBaseName": kb_name,
            "searchEndpointHost": search_host,
            "searchEndpoint": f"https://{search_host}",
        }

        conn_template = (fiq_payload.get("projectConnectionNameTemplate") or fiq_payload.get("projectConnectionId") or "").strip()
        if not conn_template:
            raise SystemExit(f"foundry-iq '{fiq_ref}' missing projectConnectionNameTemplate/projectConnectionId")
        expected_conn = render_template(conn_template, vals)

        server_tmpl = (fiq_payload.get("mcpServerUrlTemplate") or "").strip()
        if not server_tmpl:
            raise SystemExit(f"foundry-iq '{fiq_ref}' missing mcpServerUrlTemplate")
        expected_url = render_template(server_tmpl, vals)

        ks_names = extract_knowledge_source_names(repo_root, fiq_payload)
        expected_source_names.update(ks_names)

        expected.append(
            {
                "kb": kb_name,
                "project_connection_id": expected_conn,
                "server_url": expected_url,
            }
        )

    deployed_bundle = fetch_export_bundle(repo_root, args.agent_name, args.environment)
    deployed_tools = ((deployed_bundle.get("agent") or {}).get("tools") or [])
    deployed_mcp = [
        {
            "kb": infer_kb_from_url((tool or {}).get("server_url") or ""),
            "project_connection_id": ((tool or {}).get("project_connection_id") or "").strip(),
            "server_url": ((tool or {}).get("server_url") or "").strip(),
        }
        for tool in deployed_tools
        if ((tool or {}).get("type") or "").lower() == "mcp"
    ]

    errors: list[str] = []

    if len(deployed_mcp) != len(expected):
        errors.append(
            f"Deployed MCP tool count mismatch: expected {len(expected)}, got {len(deployed_mcp)}"
        )

    expected_set = {
        (e["kb"], e["project_connection_id"], e["server_url"]) for e in expected
    }
    deployed_set = {
        (d["kb"], d["project_connection_id"], d["server_url"]) for d in deployed_mcp
    }

    missing = sorted(expected_set - deployed_set)
    extras = sorted(deployed_set - expected_set)

    for kb, conn, url in missing:
        errors.append(
            f"Missing deployed MCP tool for KB '{kb}': connection='{conn}', server_url='{url}'"
        )
    for kb, conn, url in extras:
        errors.append(
            f"Unexpected deployed MCP tool for KB '{kb}': connection='{conn}', server_url='{url}'"
        )

    for item in deployed_mcp:
        kb = item["kb"]
        conn = item["project_connection_id"]
        url = item["server_url"]

        if not conn.endswith(f"-{connection_suffix}"):
            errors.append(
                f"MCP connection '{conn}' for KB '{kb}' is not environment-scoped (expected suffix '-{connection_suffix}')"
            )

        if f"srch-{args.environment}-" not in url:
            errors.append(
                f"MCP server_url for KB '{kb}' does not target {args.environment} search host pattern: '{url}'"
            )

        if search_host not in url:
            errors.append(
                f"MCP server_url for KB '{kb}' does not include configured host '{search_host}': '{url}'"
            )

        if f"/knowledgebases/{kb}/" not in url:
            errors.append(
                f"MCP server_url for KB '{kb}' does not include KB path '/knowledgebases/{kb}/': '{url}'"
            )

    errors.extend(verify_knowledge_sources_exist(search_host, expected_source_names))

    summary_lines = [
        f"### {args.environment.upper()} Deployed Agent Verification",
        f"- Agent: {args.agent_name}",
        f"- Expected KB count: {len(expected)}",
        f"- Deployed MCP tool count: {len(deployed_mcp)}",
        f"- Knowledge sources checked: {', '.join(sorted(expected_source_names)) or 'none'}",
    ]

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fp:
            fp.write("\n".join(summary_lines) + "\n")
            if errors:
                fp.write("- Result: FAILED\n")
            else:
                fp.write("- Result: PASSED\n")

    if errors:
        print("Verification failed:")
        for msg in errors:
            print(f"  - {msg}")
        raise SystemExit(1)

    print(
        f"Verification passed: agent='{args.agent_name}' env='{args.environment}' "
        f"kb_count={len(expected)} mcp_count={len(deployed_mcp)}"
    )


if __name__ == "__main__":
    main()
