import re
import sys

from .build_detect import _is_pytest_cmd, _scope_test_cmd_to_acceptance, failed_node_ids

NO_TESTS_CMD = [sys.executable, "-c", "print('test_scope=scoped: no tests in scope on the pipeline host; the full suite runs in remote CI')"]
MAX_CI_FAILURE_TARGETS = 200
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(::\S+)?$")


def is_scoped(story: dict) -> bool:
    resolution = story.get("test_scope_resolution")
    return isinstance(resolution, dict) and resolution.get("test_scope") == "scoped"


def ci_failure_targets(review_feedback: str | None) -> list[str]:
    targets: list[str] = []
    for node_id in failed_node_ids(review_feedback or ""):
        if not _NODE_ID_RE.match(node_id):
            continue
        path = node_id.split("::", 1)[0]
        if ".." in path.split("/") or path.startswith(("/", "-")):
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
