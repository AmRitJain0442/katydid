"""Run a Katydid profile with the repository-local pinned security tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--directory", type=Path, default=ROOT / ".katydid" / "security-tools")
    args = parser.parse_args(argv)
    metadata_path = args.directory.resolve() / "setup.json"
    try:
        metadata = json.loads(metadata_path.read_bytes())
        executables = metadata["executables"]
        checksums = metadata["executable_sha256"]
        if set(executables) != {"semgrep", "trivy", "gitleaks"} or any(
            _sha256(Path(value)) != checksums[name] for name, value in executables.items()
        ):
            raise ValueError("security tool checksum mismatch")
        environment = os.environ.copy()
        environment["KATYDID_SECURITY_MANIFEST"] = str(metadata_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("security run: pinned tools are not prepared", file=sys.stderr)
        return 2
    command = [sys.executable, "-m", "katydid", "run", str(args.profile.resolve())]
    if args.root is not None:
        command.extend(["--root", str(args.root.resolve())])
    try:
        return subprocess.call(command, cwd=ROOT, env=environment)
    except OSError:
        print("security run: Katydid could not start", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
