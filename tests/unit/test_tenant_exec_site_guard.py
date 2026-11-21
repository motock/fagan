"""Guard: every repo-facing subprocess launch goes through the command-wrap seam.

Each ``subprocess`` launch under ``pipeline/`` and ``app/`` must be one of:

- a call whose first argument is a literal argv (a fixed program name first),
- a call whose first argument is ``tenant_exec.wrap(...)``, or
- a site listed in ``ALLOWED`` with a reason that its command line is not
  repository-controlled.

``ALLOWED`` only shrinks: an entry whose call site no longer exists fails the
suite, so a removed or wrapped site must also leave the list. Entries are keyed
by path and enclosing function only, never by line number or count.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("pipeline", "app")
SUBPROCESS_NAMES = ("run", "Popen", "check_output", "check_call")

ALLOWED: dict[tuple[str, str], str] = {
    ("app/backend_claude.py", "complete"): "fixed claude CLI argv; wrapped only when a cwd is given",
    ("app/ollama_prompt_utils.py", "_bounded_bash_run"): "shell=True branch runs only when no wrap plugin is configured; with a plugin the command goes through tenant_exec.wrap",
    ("pipeline/advance_pool.py", "advance_plan_subprocess"): "sys.executable -m pipeline.advance_one; plan name is validated",
    ("pipeline/advance_workers.py", "_spawn"): "sys.executable -m pipeline.advance_worker with inherited pipe fds",
    ("pipeline/build_detect.py", "_lint_acceptance_fixtures"): "ruff from PATH run over a temporary copy of the story's fixtures; a linter executes no repository code",
    ("pipeline/ci_rollup.py", "_owner_and_name"): "fixed gh repo view argv; no repository-supplied command word",
    ("pipeline/ci_rollup.py", "_graphql_payload"): "fixed gh api argv; owner and name are data fields, query is a constant",
    ("pipeline/execution.py", "_spawn_fd_redirect"): "agent harness argv from build_agent_command, not repo content",
    ("pipeline/execution.py", "spawn_local"): "agent harness argv from build_agent_command, not repo content",
    ("pipeline/forge_github.py", "_run"): "runner seam; every caller passes a literal gh or git argv",
    ("pipeline/forge_gitlab.py", "_run"): "runner seam; every caller passes a literal glab argv",
    ("pipeline/merge_reverify.py", "start_merge_reverify"): "sys.executable -m pipeline.merge_reverify; plan, story and paths are data",
    ("pipeline/plan_conflict.py", "branch_file_sets"): "literal git ls-tree and git diff argvs; base ref is data",
    ("pipeline/plan_conflict_ruling.py", "_production_diff"): "literal git diff argv; paths follow the -- separator",
    ("pipeline/rebase.py", "_run"): "git runner seam; every caller passes a literal git argv",
    ("pipeline/repo_clone.py", "_run"): "git runner seam; argvs are built from a literal git head",
    ("pipeline/repo_clone.py", "_try_output"): "git runner seam; argvs are built from a literal git head",
    ("pipeline/setup/claude_setup.py", "_default_runner"): "operator setup wizard probing the operator's own host; never given a tenant repository command",
    ("pipeline/setup/detect.py", "_default_runner"): "operator setup wizard probing the operator's own host; never given a tenant repository command",
    ("pipeline/setup/services.py", "_default_runner"): "operator setup wizard probing the operator's own host; never given a tenant repository command",
    ("pipeline/story_worktree_status.py", "_git"): "literal git argv prefix with read-only subcommands",
    ("pipeline/supervise.py", "_spawn"): "child argv from operator CLI flags or the built-in child list",
    ("pipeline/triage_lint_evidence.py", "branch_changed_paths"): "module constant git diff argv",
    ("pipeline/worktree_diff.py", "_git"): "literal git argv prefix with read-only subcommands",
    ("pipeline/worktree_patch.py", "_run_git_apply"): "literal git apply argv; diff is fed on stdin, no shell",
}


def _is_subprocess_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return (
            isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in SUBPROCESS_NAMES
        )
    if isinstance(func, ast.Subscript):
        key = func.slice
        return (
            isinstance(func.value, ast.Call)
            and isinstance(func.value.func, ast.Name)
            and func.value.func.id == "globals"
            and isinstance(key, ast.Constant)
            and key.value in {f"subprocess.{name}" for name in SUBPROCESS_NAMES}
        )
    return False


def _is_wrap_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func) == "tenant_exec.wrap"


def _is_safe_argv(node: ast.expr) -> bool:
    if _is_wrap_call(node):
        return True
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    head = node.elts[0]
    if isinstance(head, ast.Constant) and isinstance(head.value, str):
        return True
    if isinstance(node, ast.List):
        if isinstance(head, ast.Attribute) and ast.unparse(head) == "sys.executable":
            return True
        if isinstance(head, ast.Call) and isinstance(head.func, ast.Name) and head.func.id == "str":
            return True
    return False


def scan_source(source: str, relpath: str) -> set[tuple[str, str]]:
    """Return ``(relpath, enclosing function)`` for each unsafe subprocess call."""
    hits: set[tuple[str, str]] = set()

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing
            if isinstance(child, ast.Call) and _is_subprocess_call(child):
                first = child.args[0] if child.args else None
                if first is None or not _is_safe_argv(first):
                    hits.add((relpath, enclosing))
            visit(child, inner)

    visit(ast.parse(source), "<module>")
    return hits


def scan_repo(root: Path = REPO_ROOT) -> set[tuple[str, str]]:
    hits: set[tuple[str, str]] = set()
    for directory in SCANNED_DIRS:
        for path in sorted((root / directory).rglob("*.py")):
            relpath = path.relative_to(root).as_posix()
            hits |= scan_source(path.read_text(encoding="utf-8"), relpath)
    return hits


def test_scanner_passes_call_wrapped_by_tenant_exec():
    source = (
        "import subprocess\n"
        "from pipeline import tenant_exec\n"
        "def run(path):\n"
        "    subprocess.run(tenant_exec.wrap(['make', 'test'], cwd=path, kind='test'))\n"
    )

    assert scan_source(source, "x.py") == set()


def test_scanner_passes_wrap_through_importlib_bound_local_names():
    source = (
        "import importlib\n"
        "def run(path):\n"
        "    subprocess = importlib.import_module('subprocess')\n"
        "    tenant_exec = importlib.import_module('pipeline.tenant_exec')\n"
        "    subprocess.run(tenant_exec.wrap(['pytest', '-q'], cwd=path, kind='test'))\n"
    )

    assert scan_source(source, "x.py") == set()


def test_scanner_flags_wrap_on_an_unrelated_object():
    source = (
        "import subprocess\n"
        "def run(helper, path):\n"
        "    subprocess.run(helper.wrap(['pytest', '-q'], cwd=path))\n"
    )

    assert scan_source(source, "x.py") == {("x.py", "run")}


def test_scanner_flags_bare_name_argv():
    source = "import subprocess\ndef run(cmd):\n    subprocess.run(cmd)\n"

    assert scan_source(source, "x.py") == {("x.py", "run")}


def test_scanner_passes_literal_git_argv():
    source = "import subprocess\ndef head():\n    subprocess.run(['git', 'rev-parse', 'HEAD'])\n"

    assert scan_source(source, "x.py") == set()


def test_scanner_passes_sys_executable_and_str_headed_argv():
    source = (
        "import subprocess, sys\n"
        "def spawn(n):\n"
        "    subprocess.Popen([sys.executable, '-m', 'pipeline.worker'])\n"
        "    subprocess.run([str(n), '--version'])\n"
    )

    assert scan_source(source, "x.py") == set()


def test_scanner_flags_argv_headed_by_a_name():
    source = "import subprocess\ndef probe(prog):\n    subprocess.run([prog, '--version'])\n"

    assert scan_source(source, "x.py") == {("x.py", "probe")}


def test_scanner_flags_argv_built_by_concatenation():
    source = "import subprocess\ndef run(extra):\n    subprocess.run(['git'] + extra)\n"

    assert scan_source(source, "x.py") == {("x.py", "run")}


def test_scanner_flags_shell_string_even_with_fixed_text():
    source = "import subprocess\ndef run(command):\n    subprocess.run(command, shell=True)\n"

    assert scan_source(source, "x.py") == {("x.py", "run")}


def test_scanner_flags_popen_and_check_output_with_bare_name():
    source = (
        "import subprocess\n"
        "def spawn(cmd):\n"
        "    subprocess.Popen(cmd)\n"
        "def read(cmd):\n"
        "    subprocess.check_output(cmd)\n"
    )

    assert scan_source(source, "x.py") == {("x.py", "spawn"), ("x.py", "read")}


def test_scanner_flags_call_without_positional_argument():
    source = "import subprocess\ndef run(cmd):\n    subprocess.run(args=cmd)\n"

    assert scan_source(source, "x.py") == {("x.py", "run")}


def test_scanner_checks_globals_subscript_form():
    unsafe = "def run(cmd):\n    globals()['subprocess.run'](cmd)\n"
    safe = "def run():\n    globals()['subprocess.run'](['git', 'status'])\n"

    assert scan_source(unsafe, "x.py") == {("x.py", "run")}
    assert scan_source(safe, "x.py") == set()


def test_scanner_ignores_unrelated_run_methods():
    source = "def run(runner, cmd):\n    runner.run(cmd)\n"

    assert scan_source(source, "x.py") == set()


def test_scanner_reports_innermost_enclosing_function():
    source = (
        "import subprocess\n"
        "def outer(cmd):\n"
        "    def _run():\n"
        "        subprocess.run(cmd)\n"
        "    return _run\n"
    )

    assert scan_source(source, "x.py") == {("x.py", "_run")}


def test_every_unsafe_subprocess_call_is_listed_in_allowed():
    unlisted = sorted(scan_repo() - set(ALLOWED))

    assert not unlisted, f"unwrapped, unlisted subprocess call sites: {unlisted}"


def test_allowed_entries_still_match_a_real_call_site():
    stale = sorted(set(ALLOWED) - scan_repo())

    assert not stale, f"stale ALLOWED entries (remove them): {stale}"


def test_allowed_reasons_are_single_nonempty_lines():
    for key, reason in ALLOWED.items():
        assert reason.strip() and "\n" not in reason, key
