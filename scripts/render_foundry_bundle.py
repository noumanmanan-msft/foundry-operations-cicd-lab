import argparse
import json
from pathlib import Path


def load_json(path: Path):
    return json.loads(path.read_text())


def collect_assets(foundry_root: Path):
    bundle = {}
    for category in ["models", "agents", "guardrails", "indexes", "tools", "memory", "evaluations"]:
        category_root = foundry_root / category
        items = []
        for path in sorted(category_root.rglob("*.json")):
            items.append({
                "path": str(path.relative_to(foundry_root.parent)),
                "content": load_json(path),
            })
        bundle[category] = items
    bundle["prompts"] = []
    for path in sorted((foundry_root / "prompts").glob("*.txt")):
        bundle["prompts"].append({
            "path": str(path.relative_to(foundry_root.parent)),
            "content": path.read_text().strip(),
        })
    return bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    env_config = load_json(repo_root / "environments" / args.environment / "config.json")
    bundle = {
        "environment": env_config,
        "assets": collect_assets(repo_root / "foundry"),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(bundle, indent=2) + "\n")
    print(f"Rendered {args.environment} bundle to {output_path}")


if __name__ == "__main__":
    main()