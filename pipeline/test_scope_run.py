import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .build_detect import (
    _acceptance_rel_paths,
    _is_pytest_cmd,
    _scope_test_cmd_to_acceptance,
    failed_node_ids,
)
from .config import _LOCAL_BACKEND_NAMES

NO_TESTS_CMD = [sys.executable, "-c", "print('test_scope=scoped: no tests in scope on the pipeline host; the full suite runs in remote CI')"]
MAX_CI_FAILURE_TARGETS = 200
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(::\S+)?$")


def is_scoped(story: dict) -> bool:
    resolution = story.get("test_scope_resolution")
    return isinstance(resolution, dict) and resolution.get("test_scope") == "scoped"


def _is_safe_node_id(node_id: str) -> bool:
    if not _NODE_ID_RE.match(node_id):
        return False
    path = node_id.split("::", 1)[0]
    return ".." not in path.split("/") and not path.startswith(("/", "-"))


def ci_failure_targets(review_feedback: str | None) -> list[str]:
    targets: list[str] = []
    for node_id in failed_node_ids(review_feedback or ""):
        if not _is_safe_node_id(node_id):
            continue
        targets.append(node_id)
        if len(targets) == MAX_CI_FAILURE_TARGETS:
            break
    return targets


def scoped_test_cmd(test_cmd: list[str], *, acceptance: list[str], own: list[str], ci_failures: list[str], test_dir) -> list[str]:
    if _is_pytest_cmd(test_cmd):
        targets = list(dict.fromkeys([*acceptance, *own, *ci_failures]))
        if not targets:
            return list(NO_TESTS_CMD)
        return [*test_cmd, *targets]
    if acceptance:
        scoped = _scope_test_cmd_to_acceptance(test_cmd, acceptance, test_dir)
        if scoped is not None:
            return list(scoped)
    return list(NO_TESTS_CMD)


def is_no_tests_cmd(cmd: list[str]) -> bool:
    return cmd == NO_TESTS_CMD


def _is_test_file_path(path: str) -> bool:
    p = Path(path)
    return (p.parts and p.parts[0] == "tests" and p.suffix == ".py"
            and (p.name.startswith("test_") or p.name.endswith("_test.py")))


def _git_lines(worktree, *args: str) -> list[str] | None:
    try:
        r = subprocess.run(
            ["git", *args], check=False, cwd=str(worktree), capture_output=True, text=True,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def changed_test_paths(worktree, base_branch: str) -> list[str]:
    """Repo-relative python test files the story changed, committed or not.

    Committed work is diffed against base_branch; staged, unstaged-new and
    untracked files are included so the agent-side gate sees uncommitted
    work. A failing base diff fails open to no own tests.
    """
    committed = _git_lines(worktree, "diff", "--name-only", "--diff-filter=ACM", f"{base_branch}...HEAD")
    if committed is None:
        return []
    uncommitted = _git_lines(worktree, "diff", "--name-only", "--diff-filter=ACM", "HEAD") or []
    untracked = _git_lines(worktree, "ls-files", "--others", "--exclude-standard") or []
    return sorted({p for p in [*committed, *uncommitted, *untracked] if _is_test_file_path(p)})


def _absolute(worktree, rel_paths: list[str]) -> list[str]:
    return [str(Path(worktree) / p) for p in rel_paths]


def grade_test_cmd(story: dict, story_key: str, worktree, test_dir, base_test_cmd: list[str], test_cmd: list[str]) -> list[str]:
    if not is_scoped(story):
        return test_cmd
    from .server import _default_branch

    acceptance = _acceptance_rel_paths(story)
    own = changed_test_paths(worktree, _default_branch())
    ci = ci_failure_targets(story.get("review_feedback")) if story.get("ci_rework") else []
    targets = [*acceptance, *own, *ci]
    cmd = scoped_test_cmd(
        base_test_cmd,
        acceptance=_absolute(worktree, acceptance),
        own=_absolute(worktree, own),
        ci_failures=_absolute(worktree, ci),
        test_dir=test_dir,
    )
    story["test_scope_grade"] = {
        "targets": list(dict.fromkeys(targets)),
        "no_tests": is_no_tests_cmd(cmd),
        "ts": datetime.now(UTC).isoformat(),
    }
    return cmd


def local_scope_kwargs(story: dict, dispatch_backend: str, worktree) -> dict:
    if dispatch_backend not in _LOCAL_BACKEND_NAMES or not is_scoped(story):
        return {}
    from .server import _default_branch

    return {
        "test_scope_spec": {
            "acceptance": _acceptance_rel_paths(story),
            "ci_failures": ci_failure_targets(story.get("review_feedback")) if story.get("ci_rework") else [],
            "base_branch": _default_branch(),
        }
    }


def _malformed_spec(spec) -> bool:
    if not isinstance(spec, dict):
        return True
    acceptance, ci_failures = spec.get("acceptance"), spec.get("ci_failures")
    if not isinstance(acceptance, list) or not all(isinstance(p, str) for p in acceptance):
        return True
    if not isinstance(ci_failures, list) or not all(isinstance(p, str) for p in ci_failures):
        return True
    return not isinstance(spec.get("base_branch"), str)


def agent_scoped_test_argv(cwd, test_dir, test_cmd: list[str], raw_spec: str | None) -> list[str] | None:
    if not raw_spec:
        return None
    try:
        spec = json.loads(raw_spec)
    except json.JSONDecodeError:
        spec = None
    if _malformed_spec(spec):
        print("test_scope: ignoring malformed LOCAL_AGENT_TEST_SCOPE", file=sys.stderr)
        return None
    acceptance = spec["acceptance"]
    ci = [node_id for node_id in spec["ci_failures"] if _is_safe_node_id(node_id)]
    own = changed_test_paths(cwd, spec["base_branch"])
    return scoped_test_cmd(
        test_cmd,
        acceptance=_absolute(cwd, acceptance),
        own=_absolute(cwd, own),
        ci_failures=_absolute(cwd, ci),
        test_dir=test_dir,
    )
