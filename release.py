import json
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
        previous.write_text("def total(prices): return sum(prices)\n", encoding="utf-8")
    shutil.copyfile(Path(__file__).parent / "app.py", current)
    if fail_health == "yes":
        current.write_text("def total(prices): return -999\n", encoding="utf-8")
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
