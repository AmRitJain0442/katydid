"""Cross-platform entry point using pinned uv without changing global installations."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = (ROOT / ".uv-version").read_text(encoding="utf-8").strip()
COMMANDS = {
    "sync": ["sync", "--locked"],
    "test": ["run", "--locked", "pytest"],
    "lint": ["run", "--locked", "ruff", "check", "."],
    "format": ["run", "--locked", "ruff", "format", "."],
    "format-check": ["run", "--locked", "ruff", "format", "--check", "."],
    "typecheck": ["run", "--locked", "mypy"],
    "cli": ["run", "--locked", "katydid"],
    "build": ["build", "--no-sources"],
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(
            "Usage: python scripts/dev.py {" + ",".join(COMMANDS) + "} [arguments]", file=sys.stderr
        )
        return 2
    try:
        return subprocess.call(
            ["uvx", "--from", f"uv=={UV_VERSION}", "uv", *COMMANDS[sys.argv[1]], *sys.argv[2:]],
            cwd=ROOT,
        )
    except FileNotFoundError:
        print(
            "uvx is required. Install uv: https://docs.astral.sh/uv/getting-started/installation/",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
