"""render_foundry_templates.py — token substitution renderer for Foundry repo assets.

Core function:
    render(payload, env_config) -> dict

Walks a nested dict/list structure recursively and substitutes every {token}
pattern in string values using top-level keys from env_config.  Raises ValueError
with a JSON-pointer-style path if any token is unresolved.  Pure function: no
network, no file I/O, no Azure SDK imports.

CLI usage:
    python3 scripts/render_foundry_templates.py <input.json> <env-config.json>
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# Matches {token} where token starts with a letter or underscore.
# Does NOT match {{escaped}} — double braces are left as-is because Python's
# str.format_map would treat them specially, but this regex won't see them since
# {{ is two chars and our pattern requires exactly one {.
_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _render_value(value: Any, env_config: dict[str, str], path: str) -> Any:
    """Recursively render a single value, tracking its JSON-pointer path."""
    if isinstance(value, str):
        tokens = _TOKEN_RE.findall(value)
        for token in tokens:
            if token not in env_config:
                raise ValueError(
                    f"Unresolved token '{{{token}}}' at path '{path}' — "
                    f"add '{token}' to the environment config."
                )
        # Replace all tokens in one pass using re.sub so we never double-render.
        def _replace(match: re.Match) -> str:
            return str(env_config[match.group(1)])

        return _TOKEN_RE.sub(_replace, value)

    if isinstance(value, dict):
        return {
            k: _render_value(v, env_config, f"{path}.{k}" if path else k)
            for k, v in value.items()
        }

    if isinstance(value, list):
        return [
            _render_value(item, env_config, f"{path}[{i}]")
            for i, item in enumerate(value)
        ]

    # int, float, bool, None — pass through unchanged
    return value


def render(payload: dict, env_config: dict) -> dict:
    """Walk payload recursively and substitute {token} patterns in string values.

    Parameters
    ----------
    payload:
        The source dict (e.g. loaded from a foundry repo JSON file).  Not mutated.
    env_config:
        Top-level keys are valid token names.  Values are converted to str before
        substitution so int/bool config values work without special handling.

    Returns
    -------
    A new dict with every {token} in every string value replaced by its env_config
    counterpart.

    Raises
    ------
    ValueError
        If any {token} found in a string value has no corresponding key in
        env_config.  The message includes the full JSON-pointer-style path to the
        offending field and the token name.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"render() expects a dict payload, got {type(payload).__name__}")

    # Flatten env_config values to strings so callers don't have to pre-convert.
    str_env: dict[str, str] = {
        k: str(v) for k, v in env_config.items() if isinstance(k, str)
    }

    return _render_value(payload, str_env, "")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _main() -> None:
    if len(sys.argv) != 3:
        print(
            "Usage: python3 render_foundry_templates.py <input.json> <env-config.json>",
            file=sys.stderr,
        )
        sys.exit(1)

    input_path, config_path = sys.argv[1], sys.argv[2]

    try:
        with open(input_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error reading {input_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, encoding="utf-8") as fh:
            env_config = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error reading {config_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        result = render(payload, env_config)
    except (ValueError, TypeError) as exc:
        print(f"Render error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _main()
