"""One-shot: run the escalation teardown in the plan's own repo root."""
from pathlib import Path

path = Path("pipeline/escalation.py")
src = path.read_text(encoding="utf-8")

# 1. every teardown git call must run in the plan's repo root, not the global.
assert src.count("cwd=REPO_ROOT") == 6, src.count("cwd=REPO_ROOT")
src = src.replace("cwd=REPO_ROOT", "cwd=repo_root")

# 2. bind repo_root in BOTH escalation functions, right after the story lookup.
lookup = '    from .server import PLAN_DIR\n    story = manifest["stories"][story_key]\n'
assert src.count(lookup) == 2, src.count(lookup)
src = src.replace(lookup, lookup + "    repo_root = _escalation_repo_root(manifest)\n")

# 3. the module docstring must stop naming the process-global.
assert src.count("All read REPO_ROOT / PLAN_DIR") == 1
src = src.replace("All read REPO_ROOT / PLAN_DIR", "All read PLAN_DIR")

path.write_text(src, encoding="utf-8")
print("ok: 6 cwd=repo_root, 2 repo_root bindings, docstring reworded")
