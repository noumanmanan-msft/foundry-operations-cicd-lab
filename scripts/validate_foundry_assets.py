import json
from pathlib import Path
import sys


REQUIRED_ENV_KEYS = [
    ("environment",),
    ("promotion", "qualityGateProfile"),
    ("foundry", "resourceId"),
    ("foundry", "projectId"),
    ("aca", "managedEnvironmentId"),
    ("security", "keyVaultUri"),
]


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def ensure_key(payload, path, file_path):
    current = payload
    for key in path:
        if key not in current:
            joined = ".".join(path)
            raise ValueError(f"Missing key '{joined}' in {file_path}")
        current = current[key]


def validate_environment_configs(repo_root: Path):
    env_root = repo_root / "environments"
    gate_profiles = load_json(repo_root / "foundry" / "evaluations" / "quality-gates.json")["profiles"]
    for config_path in sorted(env_root.glob("*/config.json")):
        payload = load_json(config_path)
        for path in REQUIRED_ENV_KEYS:
            ensure_key(payload, path, config_path)
        profile = payload["promotion"]["qualityGateProfile"]
        if profile not in gate_profiles:
            raise ValueError(f"Unknown quality gate profile '{profile}' in {config_path}")


def validate_refs(repo_root: Path):
    foundry_root = repo_root / "foundry"
    refs_to_check = []
    for path in sorted(foundry_root.rglob("*.json")):
        payload = load_json(path)
        for key, value in payload.items():
            if key.endswith("Ref") and isinstance(value, str):
                refs_to_check.append((path, repo_root / value))
    for source_path, ref_path in refs_to_check:
        if not ref_path.exists():
            raise ValueError(f"Missing referenced file {ref_path} from {source_path}")


def validate_prompt_files(repo_root: Path):
    for prompt_path in sorted((repo_root / "foundry" / "prompts").glob("*.txt")):
        content = prompt_path.read_text().strip()
        if not content:
            raise ValueError(f"Prompt file is empty: {prompt_path}")


def validate_portable_knowledge_metadata(repo_root: Path):
    """Ensure promoted knowledge metadata is environment-agnostic.

    Dev exports can include concrete ARM resource IDs and endpoint URLs. Those are
    not portable across QA/Prod and must be represented as templates or inferred
    from target environment config at deployment time.
    """
    check_roots = [
        repo_root / "foundry" / "indexes",
        repo_root / "foundry" / "knowledge",
        repo_root / "foundry" / "foundry-iq",
    ]

    for root in check_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            payload = load_json(path)

            connection_id = payload.get("connectionId")
            if isinstance(connection_id, str) and connection_id.strip().startswith("/"):
                raise ValueError(
                    f"Non-portable connectionId (ARM resource ID) found in {path}. "
                    "Use projectConnectionId/projectConnectionNameTemplate instead."
                )

            mcp_server_url = payload.get("mcpServerUrl")
            if isinstance(mcp_server_url, str) and ".search.windows.net" in mcp_server_url:
                raise ValueError(
                    f"Non-portable mcpServerUrl found in {path}. "
                    "Use mcpServerUrlTemplate with {searchEndpointHost}."
                )

            name_template = payload.get("projectConnectionNameTemplate")
            if name_template is not None and "{environment}" not in str(name_template):
                raise ValueError(
                    f"projectConnectionNameTemplate must contain '{{environment}}' in {path}."
                )


def main():
    repo_root = Path(__file__).resolve().parents[1]
    validate_environment_configs(repo_root)
    validate_refs(repo_root)
    validate_prompt_files(repo_root)
    validate_portable_knowledge_metadata(repo_root)
    print("Foundry asset validation passed")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)