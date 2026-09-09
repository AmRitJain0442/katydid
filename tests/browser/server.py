"""Launch an isolated real dashboard and worker for browser acceptance."""

import subprocess
import tempfile
from pathlib import Path

from katydid.cli import main
from katydid.demo import initialize


def run() -> int:
    with tempfile.TemporaryDirectory(prefix="katydid-browser-") as temporary:
        root = Path(temporary)
        fleet = initialize(root)
        catalog = root / "sources" / "catalog"
        verifier = catalog / "verify.py"
        verifier.write_text(
            "import time\n"
            "print('Starting catalog contract checks', flush=True)\n"
            "for progress in range(8):\n"
            "    print(f'Contract progress {progress + 1}/8', flush=True)\n"
            "    time.sleep(1)\n" + verifier.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "verify.py"],
            cwd=catalog,
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", "test: leave time for dashboard interruption"],
            cwd=catalog,
            check=True,
            capture_output=True,
            timeout=30,
        )
        return main(
            [
                "serve",
                "--fleet",
                str(fleet),
                "--host",
                "127.0.0.1",
                "--port",
                "4174",
            ]
        )


if __name__ == "__main__":
    raise SystemExit(run())
