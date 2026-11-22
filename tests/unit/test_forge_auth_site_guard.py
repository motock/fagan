"""Guard: every team-namespace git or gh network command carries the team's forge env.

A ``subprocess`` launch under ``pipeline/`` and ``app/`` whose argv is a literal
network command must splat ``**forge_auth.env_kw(...)``, so a team namespace's
push, fetch, pull, ls-remote, clone or gh call uses the team's token and never
the process token. The network verbs checked are ``push``, ``fetch``, ``pull``,
``ls-remote`` and ``clone`` for ``git``, and any ``gh`` command.

Two kinds of site are checked:

- literal argv sites: the first argument is a list literal whose first element
  is ``git`` or ``gh``; every unauthenticated one fails unless it is in
  ``EXEMPT``;
- helper sites: the argv is a variable, so the literal rule cannot see the
  command. ``HELPER_SITES`` names each (path, function) that runs a network
  command through a helper; every subprocess call inside that function must
  carry the splat.

``EXEMPT`` only shrinks: an entry that matches no real network site fails the
suite. Entries are keyed by path and enclosing function, never by line number.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("pipeline", "app")
SUBPROCESS_NAMES = ("run", "Popen", "check_output", "check_call")
NETWORK_GIT_VERBS = frozenset({"push", "fetch", "pull", "ls-remote", "clone"})

# Each reason says why the team's token must not reach the command. The file
# list matches the story's "not in scope" set; only the sites that the literal
# rule can see need an entry.
EXEMPT: dict[tuple[str, str], str] = {
    ("pipeline/remote_sync.py", "ensure_remote_worktree"): (
        "pushes to an explicit remote_url that is not the team's connected "
        "repository; the team's token must not be sent to another host"
    ),
    ("pipeline/remote_sync.py", "sync_back_commits"): (
        "fetches an explicit remote_url that is not the team's connected "
        "repository; the team's token must not be sent to another host"
    ),
}

# (path, function) whose subprocess call takes a variable argv. Every subprocess
# call nested inside the named function must carry the splat.
HELPER_SITES: tuple[tuple[str, str], ...] = (
    ("pipeline/rebase.py", "_rebase_onto_master"),  # inner _run runs fetch and the rebase
    ("pipeline/review_rework_scope.py", "_run_git"),
    ("pipeline/triage_rebase.py", "_git"),
    ("pipeline/review_merge_binding.py", "_git"),
    ("pipeline/ci_rollup.py", "_owner_and_name"),
    ("pipeline/ci_rollup.py", "_graphql_payload"),
)


def _is_subprocess_call(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
        and func.attr in SUBPROCESS_NAMES
    )


def _is_network_argv(node: ast.expr) -> bool:
    if not isinstance(node, ast.List) or not node.elts:
        return False
    head = node.elts[0]
    if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
        return False
    if head.value == "gh":
        return True
    if head.value != "git":
        return False
    return any(
        isinstance(elt, ast.Constant) and elt.value in NETWORK_GIT_VERBS
        for elt in node.elts[1:]
    )


def _has_forge_env_splat(call: ast.Call) -> bool:
    return any(
        keyword.arg is None
        and isinstance(keyword.value, ast.Call)
        and ast.unparse(keyword.value.func) == "forge_auth.env_kw"
        for keyword in call.keywords
    )


def _subprocess_sites(source: str, relpath: str) -> list[dict]:
    """Every subprocess call in ``source``, with its enclosing function chain."""
    sites: list[dict] = []

    def visit(node: ast.AST, chain: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = chain
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = (*chain, child.name)
            if isinstance(child, ast.Call) and _is_subprocess_call(child):
                first = child.args[0] if child.args else None
                sites.append({
                    "path": relpath,
                    "function": chain[-1] if chain else "<module>",
                    "chain": chain,
                    "network": first is not None and _is_network_argv(first),
                    "splat": _has_forge_env_splat(child),
                })
            visit(child, inner)

    visit(ast.parse(source), ())
    return sites


def unauthenticated_network_sites(source: str, relpath: str) -> set[tuple[str, str]]:
    """``(path, function)`` for each literal network call without the splat."""
    return {
        (site["path"], site["function"])
        for site in _subprocess_sites(source, relpath)
        if site["network"] and not site["splat"]
    }


def scan_repo(root: Path = REPO_ROOT) -> list[dict]:
    sites: list[dict] = []
    for directory in SCANNED_DIRS:
        for path in sorted((root / directory).rglob("*.py")):
            relpath = path.relative_to(root).as_posix()
            sites += _subprocess_sites(path.read_text(encoding="utf-8"), relpath)
    return sites


def test_every_literal_team_network_site_carries_the_forge_env():
    sites = scan_repo()
    network_keys = {(s["path"], s["function"]) for s in sites if s["network"]}
    unauthenticated = {
        (s["path"], s["function"])
        for s in sites
        if s["network"] and not s["splat"]
    } - set(EXEMPT)

    assert unauthenticated == set(), (
        "network git/gh call without **forge_auth.env_kw(): "
        f"{sorted(unauthenticated)}"
    )
    stale = set(EXEMPT) - network_keys
    assert stale == set(), f"EXEMPT entries that match no network site: {sorted(stale)}"


def test_every_helper_network_site_carries_the_forge_env():
    sites = scan_repo()
    for path, function in HELPER_SITES:
        covered = [s for s in sites if s["path"] == path and function in s["chain"]]
        assert covered, f"HELPER_SITES entry matches no subprocess call: {(path, function)}"
        bare = [s for s in covered if not s["splat"]]
        assert bare == [], (
            f"{path}::{function} runs a subprocess call without **forge_auth.env_kw(): "
            f"{[s['chain'] for s in bare]}"
        )


def test_guard_flags_an_unauthenticated_git_push():
    source = (
        "import subprocess\n"
        "def publish(path):\n"
        "    subprocess.run(['git', 'push', 'origin', 'main'], cwd=path)\n"
    )

    assert unauthenticated_network_sites(source, "x.py") == {("x.py", "publish")}


def test_guard_passes_a_splatted_git_push():
    source = (
        "import subprocess\n"
        "from pipeline import forge_auth\n"
        "def publish(path):\n"
        "    subprocess.run(['git', 'push', 'origin', 'main'], cwd=path,\n"
        "                   **forge_auth.env_kw())\n"
    )

    assert unauthenticated_network_sites(source, "x.py") == set()
