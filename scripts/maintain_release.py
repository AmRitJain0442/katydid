"""Run one ownership-checked release recovery pass with bounded host logs."""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from katydid.deployment import DeploymentError, recover


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        args.logs / "release-recovery.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.getLogger("vultron-release-recovery")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        result = recover(str(args.state.resolve()), str(args.spec.resolve()))
        logger.info("%s", json.dumps(result))
        return 0
    except (DeploymentError, OSError, ValueError) as exc:
        logger.error("Recovery refused: %s", str(exc)[:8000])
        return 1
    finally:
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
