#!/usr/bin/env python3
"""
Validate committed foundry/ assets for portability, schema correctness, and template rules.

Usage:
  python scripts/validate_foundry_assets.py            # warn mode (exit 0)
  python scripts/validate_foundry_assets.py --strict   # CI mode (exit 1 on any failure)

Checks are grouped into three categories:
  Cat 1 -- Sanitization leaks (hardcoded env values, ARM paths, revision metadata)
  Cat 2 -- Schema consistency  (legacy fields, broken refs, missing required structure)
  Cat 3 -- Template/portability rules (placeholder correctness in templates)
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Finding(NamedTuple):
    category: int       # 1, 2, or 3
    file: Path
    message: str        # one-liner, no file prefix


# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

REQUIRED_ENV_KEYS = [
    ("environment",),
    ("promotion", "qualityGateProfile"),
    ("foundry", "resourceId"),
    ("foundry", "projectId"),
    ("aca", "managedEnvironmentId"),
    ("security", "keyVaultUri"),
]

_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_REVISION_FIELDS = {"@odata.etag", "createdAt", "modifiedAt", "systemData", "eTag"}
_ENV_HOSTNAME_PREFIXES = [
    "aif-dev-", "aif-qa-", "aif-prod-",
    "srch-dev-", "srch-qa-", "srch-prod-",
]
_SKIP_DIRS = {"_archive", "_orphaned"}
_LEGACY_AGENT_FIELDS = {"foundryIqRef", "knowledgeRef", "knowledgeIndexRef"}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def _collect_env_configs(repo_root: Path) -> list:
    return [_load_json(p) for p in sorted((repo_root / "environments").glob("*/config.json"))]


def _collect_all_config_keys(configs: list) -> set:
    """Return every key name (at any nesting level) across all env configs."""
    keys = set()

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not k.startswith("_"):
                    keys.add(k)
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    for cfg in configs:
        _walk(cfg)
    return keys


def _collect_suffix_values(configs: list) -> set:
    return {
        str(cfg["connectionNameSuffix"])
        for cfg in configs
        if cfg.get("connectionNameSuffix")
    }


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _foundry_files(repo_root: Path) -> list:
    results = []
    for p in sorted((repo_root / "foundry").rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        results.append(p)
    return results


def _json_files(files: list) -> list:
    return [f for f in files if f.suffix == ".json"]


def _text_files(files: list) -> list:
    return [f for f in files if f.suffix != ".json"]


# ---------------------------------------------------------------------------
# Shared helper: extract all string leaves from a JSON structure
# ---------------------------------------------------------------------------

def _all_strings_in_json(obj, path="") -> list:
    """Return list of (string_value, json_path_hint) for every string leaf."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}" if path else k
            results.extend(_all_strings_in_json(v, child))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            results.extend(_all_strings_in_json(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        results.append((obj, path))
    return results


# ---------------------------------------------------------------------------
# Cat 1 -- Sanitization leaks
# ---------------------------------------------------------------------------

def _cat1_json(file: Path, payload: dict, suffix_values: set) -> list:
    findings = []
    strings = _all_strings_in_json(payload)

    for val, jpath in strings:
        # 1a -- env-pinned hostnames
        for prefix in _ENV_HOSTNAME_PREFIXES:
            if prefix in val:
                findings.append(Finding(1, file,
                    f"env-pinned hostname '{prefix}' in field '{jpath}': {val[:120]}"))
                break

        # 1b -- ARM paths
        if "/subscriptions/" in val:
            findings.append(Finding(1, file,
                f"ARM path '/subscriptions/' in field '{jpath}': {val[:120]}"))

        # 1d -- literal connection suffix values
        # Alpha-only boundary: catches '-qa'/`_119cl` in connection names but not 'production'.
        # Evaluations/ are test datasets where "qa"/"prod" are legitimate content words.
        if "foundry/evaluations/" not in str(file):
            for suffix in suffix_values:
                pat = r"(?<![a-zA-Z])" + re.escape(suffix) + r"(?![a-zA-Z])"
                if re.search(pat, val):
                    findings.append(Finding(1, file,
                        f"literal connection suffix '{suffix}' in field '{jpath}': {val[:120]}"))
                    break

    # 1c -- revision/system metadata keys anywhere in the tree
    def _scan_keys(obj, jpath=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                child = f"{jpath}.{k}" if jpath else k
                if k in _REVISION_FIELDS:
                    findings.append(Finding(1, file,
                        f"revision/system metadata field '{k}' at '{child}' should be removed"))
                _scan_keys(v, child)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _scan_keys(item, f"{jpath}[{i}]")

    _scan_keys(payload)
    return findings


def _cat1_text(file: Path, text: str, suffix_values: set) -> list:
    """Cat 1 for non-JSON files (prompts, etc.) -- line-by-line."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for prefix in _ENV_HOSTNAME_PREFIXES:
            if prefix in line:
                findings.append(Finding(1, file,
                    f"line {lineno}: env-pinned hostname '{prefix}' in: {line.strip()[:120]}"))
        if "/subscriptions/" in line:
            findings.append(Finding(1, file,
                f"line {lineno}: ARM path '/subscriptions/' in: {line.strip()[:120]}"))
        if "foundry/evaluations/" not in str(file):
            for suffix in suffix_values:
                pat = r"(?<![a-zA-Z])" + re.escape(suffix) + r"(?![a-zA-Z])"
                if re.search(pat, line):
                    findings.append(Finding(1, file,
                        f"line {lineno}: literal connection suffix '{suffix}' in: {line.strip()[:120]}"))
                    break
    return findings


def _cat1e_unknown_tokens(file: Path, text: str, known_keys: set) -> list:
    """Cat 1e -- report {tokenName} not present in any env config."""
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for token in _TOKEN_RE.findall(line):
            if token not in known_keys:
                findings.append(Finding(1, file,
                    f"line {lineno}: unrecognized token '{{{token}}}' -- "
                    f"add '{token}' to an environments/*/config.json"))
    return findings


# ---------------------------------------------------------------------------
# Cat 2 -- Schema consistency
# ---------------------------------------------------------------------------

def _cat2_agent(file: Path, payload: dict, repo_root: Path) -> list:
    findings = []

    for legacy in _LEGACY_AGENT_FIELDS:
        if legacy in payload:
            findings.append(Finding(2, file,
                f"legacy field '{legacy}' present; use 'foundryIqRefs' (array)"))

    fiq_refs = payload.get("foundryIqRefs")
    if fiq_refs is not None and not isinstance(fiq_refs, list):
        findings.append(Finding(2, file, "'foundryIqRefs' must be an array"))

    for ref in (fiq_refs if isinstance(fiq_refs, list) else []):
        if not isinstance(ref, str):
            continue
        ref_path = repo_root / ref
        if not ref_path.exists():
            findings.append(Finding(2, file,
                f"foundryIqRefs entry '{ref}' does not exist on disk"))
        else:
            try:
                _load_json(ref_path)
            except ValueError as exc:
                findings.append(Finding(2, file,
                    f"foundryIqRefs entry '{ref}' is not valid JSON: {exc}"))

    for field in [k for k in payload if k.endswith("Ref") and k != "foundryIqRefs"]:
        val = payload[field]
        if isinstance(val, str) and val:
            if not (repo_root / val).exists():
                findings.append(Finding(2, file,
                    f"'{field}' points to missing file '{val}'"))

    return findings


def _cat2_foundry_iq(file: Path, payload: dict, repo_root: Path) -> list:
    findings = []
    kb_ref = payload.get("knowledgeBaseRef")
    if kb_ref:
        kb_path = repo_root / kb_ref
        if not kb_path.exists():
            findings.append(Finding(2, file,
                f"'knowledgeBaseRef' points to missing file '{kb_ref}'"))
        else:
            try:
                kb_payload = _load_json(kb_path)
                for ks_ref in (kb_payload.get("knowledgeSourceRefs") or []):
                    if not (repo_root / ks_ref).exists():
                        findings.append(Finding(2, file,
                            f"knowledgeSourceRefs entry '{ks_ref}' (via '{kb_ref}') does not exist"))
            except ValueError as exc:
                findings.append(Finding(2, file,
                    f"'knowledgeBaseRef' target '{kb_ref}' is not valid JSON: {exc}"))
    return findings


def _cat2_knowledgebase(file: Path, payload: dict, repo_root: Path) -> list:
    findings = []
    kb_name = (payload.get("name") or "").strip()
    if not kb_name:
        findings.append(Finding(2, file, "knowledgebase missing 'name' field"))
    definition = payload.get("definition") or {}
    if not isinstance(definition, dict) or not definition:
        findings.append(Finding(2, file, "knowledgebase missing 'definition' block"))
    else:
        def_name = (definition.get("name") or "").strip()
        if def_name and def_name != kb_name:
            findings.append(Finding(2, file,
                f"name/definition.name mismatch: '{kb_name}' != '{def_name}'"))
    for ks_ref in (payload.get("knowledgeSourceRefs") or []):
        if isinstance(ks_ref, str) and ks_ref and not (repo_root / ks_ref).exists():
            findings.append(Finding(2, file,
                f"knowledgeSourceRefs entry '{ks_ref}' does not exist on disk"))
    return findings


def _cat2_knowledge_source(file: Path, payload: dict) -> list:
    findings = []
    src_name = (payload.get("name") or "").strip()
    if not src_name:
        findings.append(Finding(2, file, "knowledge source missing 'name' field"))
    definition = payload.get("definition") or {}
    if not isinstance(definition, dict) or not definition:
        findings.append(Finding(2, file, "knowledge source missing 'definition' block"))
    else:
        def_name = (definition.get("name") or "").strip()
        if def_name and def_name != src_name:
            findings.append(Finding(2, file,
                f"name/definition.name mismatch: '{src_name}' != '{def_name}'"))
    return findings


def _validate_env_configs(repo_root: Path) -> list:
    findings = []
    gate_profiles_path = repo_root / "foundry" / "evaluations" / "quality-gates.json"
    try:
        gate_profiles = _load_json(gate_profiles_path).get("profiles", {})
    except (ValueError, FileNotFoundError):
        gate_profiles = {}

    for config_path in sorted((repo_root / "environments").glob("*/config.json")):
        try:
            payload = _load_json(config_path)
        except ValueError as exc:
            findings.append(Finding(2, config_path, str(exc)))
            continue
        for key_path in REQUIRED_ENV_KEYS:
            current = payload
            ok = True
            for k in key_path:
                if not isinstance(current, dict) or k not in current:
                    findings.append(Finding(2, config_path,
                        f"missing required key '{'.'.join(str(x) for x in key_path)}'"))
                    ok = False
                    break
                current = current[k]
        if gate_profiles:
            profile = (payload.get("promotion") or {}).get("qualityGateProfile", "")
            if profile and profile not in gate_profiles:
                findings.append(Finding(2, config_path,
                    f"unknown quality gate profile '{profile}'"))
    return findings


def _validate_prompt_files(repo_root: Path) -> list:
    findings = []
    prompts_dir = repo_root / "foundry" / "prompts"
    if prompts_dir.exists():
        for p in sorted(prompts_dir.glob("*.txt")):
            if not p.read_text().strip():
                findings.append(Finding(2, p, "prompt file is empty"))
    return findings


# ---------------------------------------------------------------------------
# Cat 3 -- Template/portability rules
# ---------------------------------------------------------------------------

def _cat3_foundry_iq(file: Path, payload: dict, suffix_values: set) -> list:
    findings = []

    # 3a -- projectConnectionNameTemplate must use {connectionNameSuffix}
    name_tmpl = payload.get("projectConnectionNameTemplate")
    if name_tmpl is not None:
        s = str(name_tmpl)
        if "{connectionNameSuffix}" not in s:
            findings.append(Finding(3, file,
                f"'projectConnectionNameTemplate' must contain '{{connectionNameSuffix}}'; got: '{s}'"))
        if "{environment}" in s:
            findings.append(Finding(3, file,
                f"'projectConnectionNameTemplate' uses legacy '{{environment}}' placeholder; "
                f"replace with '{{connectionNameSuffix}}'"))

    # 3b -- mcpServerUrlTemplate must include both required placeholders
    url_tmpl = payload.get("mcpServerUrlTemplate")
    if url_tmpl is not None:
        s = str(url_tmpl)
        if "{searchEndpointHost}" not in s:
            findings.append(Finding(3, file,
                f"'mcpServerUrlTemplate' must contain '{{searchEndpointHost}}'; got: '{s[:100]}'"))
        if "{knowledgeBaseName}" not in s:
            findings.append(Finding(3, file,
                f"'mcpServerUrlTemplate' must contain '{{knowledgeBaseName}}'; got: '{s[:100]}'"))

    # 3d -- mcpServerLabel must not contain a literal suffix value
    label = payload.get("mcpServerLabel")
    if label is not None:
        label_s = str(label)
        for suffix in suffix_values:
            pat = r"(?<![a-zA-Z0-9_-])" + re.escape(suffix) + r"(?![a-zA-Z0-9_-])"
            if re.search(pat, label_s):
                findings.append(Finding(3, file,
                    f"'mcpServerLabel' contains literal connection suffix '{suffix}'; "
                    f"label must be env-agnostic (got: '{label_s}')"))
                break

    return findings


def _cat3_knowledgebase(file: Path, payload: dict) -> list:
    findings = []
    # 3c -- resourceUri in azureOpenAIParameters must use {azureOpenAIAccountHost}
    for val, jpath in _all_strings_in_json(payload):
        if "resourceUri" in jpath and "azureOpenAIParameters" in jpath:
            if val and "{azureOpenAIAccountHost}" not in val:
                findings.append(Finding(3, file,
                    f"'resourceUri' in azureOpenAIParameters must use "
                    f"'{{azureOpenAIAccountHost}}'; got '{val[:120]}' at '{jpath}'"))
    return findings


# ---------------------------------------------------------------------------
# Main validation runner
# ---------------------------------------------------------------------------

def run_validation(repo_root: Path):
    configs = _collect_env_configs(repo_root)
    suffix_values = _collect_suffix_values(configs)
    known_keys = _collect_all_config_keys(configs)

    all_files = _foundry_files(repo_root)
    json_files_list = _json_files(all_files)
    text_files_list = _text_files(all_files)

    cat1 = []
    cat2 = []
    cat3 = []

    cat2.extend(_validate_env_configs(repo_root))
    cat2.extend(_validate_prompt_files(repo_root))

    for file in json_files_list:
        try:
            payload = _load_json(file)
        except ValueError as exc:
            cat2.append(Finding(2, file, f"invalid JSON: {exc}"))
            continue

        rel = str(file.relative_to(repo_root))
        in_agents = "foundry/agents/" in rel
        in_foundry_iq = "foundry/foundry-iq/" in rel
        in_knowledgebases = "foundry/knowledgebases/" in rel
        in_knowledge_sources = "foundry/knowledge-sources/" in rel

        # Cat 1 -- sanitization leaks
        cat1.extend(_cat1_json(file, payload, suffix_values))
        # Augment known_keys with the file's own top-level keys — a template may
        # reference sibling fields (e.g. {knowledgeBaseName} in mcpServerUrlTemplate).
        file_own_keys = known_keys | {k for k in payload if isinstance(k, str)}
        cat1.extend(_cat1e_unknown_tokens(file, file.read_text(), file_own_keys))

        # Cat 2 -- schema
        if in_agents:
            cat2.extend(_cat2_agent(file, payload, repo_root))
        if in_foundry_iq:
            cat2.extend(_cat2_foundry_iq(file, payload, repo_root))
        if in_knowledgebases:
            cat2.extend(_cat2_knowledgebase(file, payload, repo_root))
        if in_knowledge_sources:
            cat2.extend(_cat2_knowledge_source(file, payload))

        # Cat 3 -- template rules
        if in_foundry_iq:
            cat3.extend(_cat3_foundry_iq(file, payload, suffix_values))
        if in_knowledgebases:
            cat3.extend(_cat3_knowledgebase(file, payload))

    for file in text_files_list:
        text = file.read_text()
        cat1.extend(_cat1_text(file, text, suffix_values))
        cat1.extend(_cat1e_unknown_tokens(file, text, known_keys))

    return cat1, cat2, cat3


# ---------------------------------------------------------------------------
# Output formatter
# ---------------------------------------------------------------------------

def _format_category(findings: list, cat_num: int, cat_name: str, repo_root: Path) -> list:
    count = len(findings)
    lines = [f"CAT {cat_num} -- {cat_name} ({count} failure{'s' if count != 1 else ''}):"]
    if not findings:
        return lines

    by_file = {}
    for f in findings:
        rel = f.file.relative_to(repo_root) if f.file.is_absolute() else f.file
        by_file.setdefault(rel, []).append(f.message)

    for file_path in sorted(by_file):
        lines.append(f"  {file_path}:")
        for msg in by_file[file_path]:
            lines.append(f"    - {msg}")
    return lines


def print_report(cat1: list, cat2: list, cat3: list, repo_root: Path) -> None:
    print("=== validate_foundry_assets.py ===")
    print()
    for lines in [
        _format_category(cat1, 1, "Sanitization leaks", repo_root),
        _format_category(cat2, 2, "Schema consistency", repo_root),
        _format_category(cat3, 3, "Template/portability rules", repo_root),
    ]:
        for line in lines:
            print(line)
        print()

    total = len(cat1) + len(cat2) + len(cat3)
    affected = len({f.file for f in cat1 + cat2 + cat3})
    print(f"TOTAL: {total} failure{'s' if total != 1 else ''} across {affected} file{'s' if affected != 1 else ''}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate committed foundry/ assets for portability, schema correctness, "
            "and template rules. Without --strict, exits 0 and prints warnings. "
            "With --strict, exits non-zero if any failures are found."
        )
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on any failure (use in CI pipelines).",
    )
    parser.add_argument(
        "--foundry-root",
        help="Override repo root (default: two levels above this script).",
    )
    args = parser.parse_args()

    repo_root = (
        Path(args.foundry_root).resolve()
        if args.foundry_root
        else Path(__file__).resolve().parents[1]
    )
    cat1, cat2, cat3 = run_validation(repo_root)
    print_report(cat1, cat2, cat3, repo_root)

    total = len(cat1) + len(cat2) + len(cat3)
    if total == 0:
        print()
        print("All checks passed.")
    elif args.strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
