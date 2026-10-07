"""Guard the bench harness's sys.path handling against a prepend regression.

harness.py resolves PIPELINE_REPO through `git rev-parse --git-common-dir`
(_resolve_pipeline_repo), so inside a pipeline worktree PIPELINE_REPO is the
MAIN checkout. An earlier shape of this file did
`sys.path.insert(0, str(PIPELINE_REPO))` at import time, which put the main
checkout AHEAD of the worktree's own scripts/, app/ and pipeline/ for every
later import in the process; unrelated tests then imported stale code and
story BPM-2 was parked (19 failures / 1 error in the merge-gate reverify)
while clean-clone CI stayed green, because in a clean clone the two paths
coincide. The current shape keeps the repo root importable but APPENDS it,
so it can never shadow the invoking checkout's own packages. The bench's
sibling scripts (compound_harness.py, run_real_repo_task.py) import harness
at module level and only later do `from app import ...`, so that append must
stay at module level. These tests make a one-word revert to insert(0) fail
deterministically everywhere -- including in a clean clone, where the
behavioural check is vacuous and CI is structurally blind to this hazard.
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

from tests.benchmark import harness

HARNESS_PY = Path(harness.__file__).resolve()
REPO_ROOT = HARNESS_PY.parents[2]
BENCH_DIR = HARNESS_PY.parent


def _src() -> str:
    return HARNESS_PY.read_text()


def _segment(node: ast.AST) -> str:
    return ast.get_source_segment(_src(), node) or ""


def test_no_insert_call_mentions_pipeline_repo():
    """No `.insert(...)` anywhere in harness.py may mention PIPELINE_REPO.

    `sys.path.insert(0, str(PIPELINE_REPO))` -- at import time or anywhere
    else, module level or inside a function -- is the BPM-2 hazard: in a
    worktree it shadows this checkout's own packages for the rest of the
    process. The current append passes; a one-word revert to insert(0) fails
    here deterministically, even in a clean clone where CI cannot see the
    difference between the two.
    """
    offenders = [
        _segment(node)
        for node in ast.walk(ast.parse(_src()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert"
        and "PIPELINE_REPO" in _segment(node)
    ]
    assert not offenders, (
        "harness.py must never insert PIPELINE_REPO into a path: prepending "
        "the main checkout recreates the BPM-2 worktree shadowing hazard. "
        f"Offenders:\n{''.join(offender + chr(10) for offender in offenders)}"
    )


def test_pipeline_repo_append_is_guarded_and_module_level():
    """The repo-root entry must be a guarded append at MODULE level.

    Module level is load-bearing: compound_harness.py and
    run_real_repo_task.py `import harness` at module level and only later do
    `from app import pipeline_mcp_server` / `from app import backend`, so the
    repo root must already be importable by the time harness finishes
    importing -- main() never runs for them. The `not in` guard keeps a
    second import idempotent.
    """
    matched = []
    for node in ast.parse(_src()).body:
        if not isinstance(node, ast.If):
            continue
        test_seg = _segment(node.test)
        body_seg = "\n".join(_segment(stmt) for stmt in node.body)
        if (
            "PIPELINE_REPO" in test_seg
            and "not in" in test_seg
            and "sys.path" in test_seg
            and "sys.path.append" in body_seg
            and "PIPELINE_REPO" in body_seg
            and "sys.path.insert" not in body_seg
        ):
            matched.append(node)
    assert len(matched) == 1, (
        "expected exactly one module-level "
        "`if str(PIPELINE_REPO) not in sys.path: sys.path.append(str(PIPELINE_REPO))` "
        f"guard, found {len(matched)}"
    )


def test_harness_own_directory_insert_survives():
    """Negative control: the own-directory insert must still be present.

    harness.py needs its own directory importable for `from models import
    MODELS` and `import cell_cost` (script mode puts only tests/benchmark on
    sys.path). Deleting that line while reworking PIPELINE_REPO handling
    would break every bench run with ModuleNotFoundError.
    """
    found = any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and "sys.path.insert" in _segment(node)
        and "Path(__file__)" in _segment(node)
        for node in ast.parse(_src()).body
    )
    assert found, (
        "module-level sys.path.insert(0, str(Path(__file__).resolve().parent)) "
        "is missing from harness.py"
    )


def test_import_may_add_pipeline_repo_only_at_the_end():
    """Importing the harness may add PIPELINE_REPO only at the END of sys.path.

    Never ahead of the invoking checkout's own root. NOTE: in a clean clone
    PIPELINE_REPO equals the repo root this probe puts on sys.path itself, so
    nothing is added and the ordering assertions below are vacuous -- which is
    why the AST tests above exist; in a pipeline worktree (where PIPELINE_REPO
    is the MAIN checkout) this is the live behavioural check.
    """
    probe = (
        "import json, os, sys\n"
        "root = os.getcwd()\n"
        "sys.path.insert(0, root)\n"
        "before = list(sys.path)\n"
        "import tests.benchmark.harness as h\n"
        "after = list(sys.path)\n"
        "added = [p for p in after if p not in before]\n"
        'print("RESULT:" + json.dumps({"root": root, "added": added,'
        ' "after": after, "pipeline_repo": str(h.PIPELINE_REPO)}))\n'
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr}"
    results = [line for line in proc.stdout.splitlines() if line.startswith("RESULT:")]
    assert len(results) == 1, f"unexpected probe output:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(results[0][len("RESULT:"):])
    after, root = data["after"], data["root"]
    pipeline_repo, added = data["pipeline_repo"], data["added"]
    assert set(added) <= {str(BENCH_DIR), pipeline_repo}, (
        f"importing the harness added unexpected sys.path entries: {added}"
    )
    assert str(BENCH_DIR) in added, f"own-directory insert did not run: {added}"
    if pipeline_repo in added:
        assert after[-1] == pipeline_repo, (
            f"PIPELINE_REPO must be appended at the END of sys.path; tail: {after[-3:]}"
        )
        assert after.index(root) < after.index(pipeline_repo), (
            "PIPELINE_REPO must never land ahead of the invoking checkout's own root"
        )


def test_bench_script_entry_still_resolves_imports(tmp_path):
    """Script mode still works: a bogus model exits 2 with "unknown model".

    Run as a script from an unrelated cwd, harness.py must reach the
    unknown-model exit without any ModuleNotFoundError. It exits before
    touching `app`, so this proves the script entry's own imports (models,
    cell_cost) still resolve via its own-directory insert.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(HARNESS_PY),
            "--task",
            "no-such-task",
            "--model",
            "definitely-not-a-model",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"exit {proc.returncode}; output:\n{combined}"
    assert "unknown model" in proc.stderr, combined
    assert "ModuleNotFoundError" not in combined, combined