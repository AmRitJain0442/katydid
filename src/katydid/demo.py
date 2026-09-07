"""Reproducible local acceptance fixture; live runs never substitute a fake AI."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from katydid.controller import Controller
from katydid.runner import _write_json

GOOD_APP = (
    'def total(prices):\n    """Sum all prices; empty input returns zero."""\n'
    "    return sum(prices)\n"
)
BAD_APP = GOOD_APP.replace("return sum(prices)", "return sum(prices) + 1")
VERIFY = """import sys
import xml.etree.ElementTree as ET
from app import total

cases = [([], 0), ([1, 2, 3], 6), ([-4, 4], 0), ([7], 7), ([0, 0], 0)]
suite = ET.Element("testsuite", name="pricing", tests=str(len(cases)))
failed = 0
for index, (prices, expected) in enumerate(cases):
    case = ET.SubElement(suite, "testcase", name=f"total_{index}", classname="pricing")
    try:
        actual = total(prices)
        assert actual == expected, f"total({prices}) expected {expected}, got {actual}"
    except Exception as error:
        failed += 1
        ET.SubElement(case, "failure", message=str(error)).text = str(error)
        print(error)
suite.set("failures", str(failed))
ET.ElementTree(suite).write(sys.argv[1], encoding="utf-8", xml_declaration=True)
raise SystemExit(1 if failed else 0)
"""
RELEASE = """import json
import runpy
import shutil
import sys
from pathlib import Path

action, destination, commit, fail_health = sys.argv[1:]
root = Path(destination)
root.mkdir(parents=True, exist_ok=True)
current, previous = root / "app.py", root / "previous.py"
if action == "deploy":
    if current.exists():
        shutil.copyfile(current, previous)
    else:
        previous.write_text("def total(prices): return sum(prices)\\n", encoding="utf-8")
    shutil.copyfile(Path(__file__).parent / "app.py", current)
    if fail_health == "yes":
        current.write_text("def total(prices): return -999\\n", encoding="utf-8")
    (root / "deployment.json").write_text(json.dumps({"commit": commit}), encoding="utf-8")
elif action == "rollback":
    shutil.copyfile(previous, current)
    (root / "deployment.json").write_text(json.dumps({"restored_previous": True}), encoding="utf-8")
elif action == "health":
    total = runpy.run_path(str(current))["total"]
    assert total([]) == 0 and total([2, 3, -1]) == 4, "deployed application failed health check"
    print("deployed application healthy")
else:
    raise SystemExit("unknown action")
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=60)


def initialize(directory: Path) -> Path:
    directory = directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(
            "Demo directory must be new or empty; existing evidence is never overwritten"
        )
    directory.mkdir(parents=True, exist_ok=True)
    repositories = []
    for name, broken, rollback in (
        ("pricing", True, False),
        ("catalog", False, False),
        ("recovery", True, True),
    ):
        source = directory / "sources" / name
        source.mkdir(parents=True)
        for path, text in {
            "app.py": BAD_APP if broken else GOOD_APP,
            "verify.py": VERIFY,
            "release.py": RELEASE,
            ".gitignore": "__pycache__/\n",
        }.items():
            (source / path).write_text(text, encoding="utf-8", newline="\n")
        profile = {
            "schema_version": 1,
            "repository": name,
            "owner": "demo",
            "checks": [
                {
                    "id": "unit",
                    "kind": "test",
                    "argv": ["{python}", "verify.py", "{report}"],
                    "timeout_seconds": 30,
                }
            ],
        }
        (source / "quality.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
        _git(source, "init", "--initial-branch=main")
        _git(source, "config", "user.name", "Katydid Demo")
        _git(source, "config", "user.email", "katydid@users.noreply.github.com")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "test: seed reproducible pricing acceptance fixture")
        hooks = {
            action: {
                "id": action,
                "kind": "command",
                "timeout_seconds": 30,
                "argv": [
                    "{python}",
                    "release.py",
                    action,
                    "{release_dir}",
                    "{commit}",
                    "yes" if rollback else "no",
                ],
            }
            for action in ("deploy", "health", "rollback")
        }
        repositories.append(
            {
                "id": name,
                "source": f"sources/{name}",
                "profile": "quality.yaml",
                "base_branch": "main",
                "context_paths": ["app.py", "verify.py"],
                "editable_paths": ["app.py"],
                "requirements": (
                    "total(prices) returns the sum of every price; empty input returns 0. "
                    "Preserve the function API and all protected tests. No additional dependencies."
                ),
                "required_checks": {"unit": "test"},
                "delivery": {"mode": "local", "auto_merge": True},
                "release": hooks,
            }
        )
    fleet = directory / "fleet.yaml"
    fleet.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "state_directory": "control",
                "ai": {"provider": "codex", "model": "gpt-5.6-sol", "reasoning": "high"},
                "repositories": repositories,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return fleet


def run_demo(directory: Path) -> dict[str, Any]:
    fleet = directory.resolve() / "fleet.yaml"
    if not fleet.exists():
        fleet = initialize(directory)
    controller = Controller(fleet)
    protected = {
        repo.id: hashlib.sha256((Path(repo.source) / "verify.py").read_bytes()).hexdigest()
        for repo in controller.config.repositories
    }
    tasks = [
        controller.enqueue(name, f"live-demo:{name}") for name in ("pricing", "catalog", "recovery")
    ]
    while any(controller.store.get_task(task["id"])["state"] == "queued" for task in tasks):
        controller.work_once()
    outcomes = {task["repository"]: controller.store.get_task(task["id"]) for task in tasks}
    unchanged = all(
        hashlib.sha256((Path(repo.source) / "verify.py").read_bytes()).hexdigest()
        == protected[repo.id]
        for repo in controller.config.repositories
    )
    pricing, catalog, recovery = (outcomes[name] for name in ("pricing", "catalog", "recovery"))
    receipts = list(controller.directory.glob("tasks/*/*/ai/*/receipt.json"))
    real_calls = [json.loads(path.read_text(encoding="utf-8")) for path in receipts]
    recovery_result = recovery.get("result") or {}
    recovery_health = recovery_result.get("rollback_health") or {}
    passed = (
        pricing["state"] == "completed"
        and pricing["result"]["outcome"] == "repaired"
        and catalog["state"] == "completed"
        and catalog["result"]["outcome"] == "healthy"
        and recovery["state"] == "failed"
        and recovery_health.get("gate", {}).get("passed") is True
        and unchanged
        and len(real_calls) >= 6
        and all(call["provider"] == "codex" for call in real_calls)
    )
    summary = {
        "passed": passed,
        "fleet": str(fleet),
        "protected_tests_unchanged": unchanged,
        "real_ai_calls": len(real_calls),
        "tasks": outcomes,
    }
    _write_json(directory.resolve() / "acceptance.json", summary)
    return summary
