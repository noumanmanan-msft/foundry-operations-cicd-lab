from pathlib import Path
import re
import sys


PATTERNS = [
    re.compile(r"InstrumentationKey=[^\s\"']+"),
    re.compile(r"AccountKey=[^\s\"']+"),
    re.compile(r"SharedAccessKey=[^\s\"']+"),
    re.compile(r"[\"']client[_-]?secret[\"']\s*[:=]\s*[\"'][^\"']+[\"']", re.IGNORECASE),
    re.compile(r"[\"']api[_-]?key[\"']\s*[:=]\s*[\"'][^\"']+[\"']", re.IGNORECASE),
    re.compile(r"[\"']password[\"']\s*[:=]\s*[\"'][^\"']+[\"']", re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY-----"),
]

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".dist",
}

SKIP_FILES = {
    ".gitignore",
    "scan_for_secrets.py",
}


def scan_file(path: Path):
    text = path.read_text(errors="ignore")
    matches = []
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            matches.append((pattern.pattern, match.group(0)[:120]))
    return matches


def main():
    repo_root = Path(__file__).resolve().parents[1]
    findings = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        for pattern, sample in scan_file(path):
            findings.append((path.relative_to(repo_root), pattern, sample))

    if findings:
        for rel_path, pattern, sample in findings:
            print(f"{rel_path}: matched {pattern}: {sample}", file=sys.stderr)
        sys.exit(1)

    print("No secret-like patterns detected")


if __name__ == "__main__":
    main()