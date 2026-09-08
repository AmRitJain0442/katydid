"""Refresh the installed scanner database and retain bounded operational logs."""

import argparse
import logging
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()
    manifest = args.manifest.resolve()
    if manifest.name != "setup.json" or not manifest.is_file():
        parser.error("An installed security setup.json manifest is required")
    args.logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        args.logs / "security-refresh.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.getLogger("vultron-security-refresh")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == "pythonw.exe":
        interpreter = interpreter.with_name("python.exe")
    try:
        result = subprocess.run(
            [
                str(interpreter),
                str(Path(__file__).parent / "security" / "setup.py"),
                "refresh-db",
                "--directory",
                str(manifest.parent),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            check=False,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
        )
        logger.info("Database refresh exited with code %s", result.returncode)
        if result.returncode:
            logger.error("%s", result.stderr[-8000:])
        return result.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Database refresh unavailable: %s", type(exc).__name__)
        return 1
    finally:
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
