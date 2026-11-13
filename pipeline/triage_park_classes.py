"""Park classes: the vocabulary failure triage uses to name a terminal park.

``classify_park`` reads the strings a caller already collected (the parked
reason, review feedback, test output, the branch diff) and names the park
after the live signal it carries. It is pure: no I/O, and it never raises --
input it cannot read classifies as ``unknown``.

``ALLOWED_ACTIONS`` says which ruling actions the overlord may return for each
class. A ruling outside that set is replaced with ``park_for_human`` by the
caller (see ``pipeline.triage.rule_on_story``).
"""

import re
from dataclasses import dataclass, field

# Actions the triage vocabulary added with the park classes. Each has only a
# shadow-only executor stub until its own story replaces it.
NEW_ACTIONS = frozenset({"resolve_rebase", "retry_transient", "rebrief", "approve_test_edit"})

# The actions every triage ruling could carry before the park classes landed.
EXISTING_ACTIONS = frozenset({
    "escalate_model", "split_story", "repo_issue", "park_for_human",
    "mark_done", "patch_acceptance", "amend_scope",
})


@dataclass(frozen=True)
class ParkClass:
    """A named park and the live facts that identified it."""

    name: str
    facts: dict = field(default_factory=dict)


ALLOWED_ACTIONS: dict[str, frozenset[str]] = {
    "rebase_conflict": frozenset({"resolve_rebase", "park_for_human"}),
    "scope_gate": frozenset({"amend_scope", "park_for_human"}),
    "guard_demands_path": frozenset({"amend_scope", "rebrief", "park_for_human"}),
    "pin_removed_test_failure": frozenset({"approve_test_edit", "park_for_human"}),
    "no_commit_after_rework": frozenset({"rebrief", "amend_scope", "escalate_model", "park_for_human"}),
    "ci_red_after_rework": frozenset({"rebrief", "escalate_model", "park_for_human"}),
    "transient_bookkeeping": frozenset({"retry_transient", "park_for_human"}),
    "high_risk_hold": frozenset({"park_for_human"}),
    "unknown": EXISTING_ACTIONS,
}

_CONFLICT_RE = re.compile(r"CONFLICT \(([^)]*)\): (?:Merge conflict in )?(\S+)")
_CONFLICT_KINDS = {"content": "content", "modify/delete": "modify_delete", "add/add": "add_add"}
_SCOPE_RE = re.compile(r"- Blocking: (\S+?): outside this story's `files` scope")
_GUARD_PHRASES = ("remove it", "add it", "must be listed")
_PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+)")
_ATTR_RE = re.compile(r"AttributeError[^\n]*? has no attribute '(\w+)'")
_IMPORT_RE = re.compile(r"ImportError[^\n]*?cannot import name '(\w+)'")
_DIFF_HEADER_RE = re.compile(r"diff --git a/\S+ b/(\S+)")
_FAILED_RE = re.compile(r"^\s*FAILED\s+(\S+)", re.MULTILINE)
_NO_COMMIT_RE = re.compile(r"no new commit after (\d+) rework redispatches")
_TRANSIENT_MARKERS = ("branch head unavailable", "plan lock contended", "unchanged_since_last_review")
_HIGH_RISK_MARKER = "high risk held for human review"


def classify_park(story: dict, evidence: dict) -> ParkClass:
    """Name the park by the live signal it carries; ``unknown`` if none match."""
    try:
        return _classify(_merged_view(story, evidence))
    except Exception:  # noqa: BLE001 - classification must never raise
        return ParkClass(name="unknown", facts={})


def check_ruling_allowed(park_class: ParkClass, action: str) -> bool:
    """True iff ``action`` is an allowed ruling for ``park_class``.

    An unrecognised class name allows nothing (fail closed).
    """
    return action in ALLOWED_ACTIONS.get(getattr(park_class, "name", None), frozenset())


def _merged_view(story, evidence) -> dict:
    view: dict = {}
    if isinstance(story, dict):
        view.update(story)
    if isinstance(evidence, dict):
        view.update(evidence)
    return view


def _text(view: dict, key: str) -> str:
    value = view.get(key)
    return value if isinstance(value, str) else ""


def _files(view: dict) -> list:
    value = view.get("files")
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _classify(view: dict) -> ParkClass:
    reason = _text(view, "parked_reason")
    review = _text(view, "review_feedback")
    test_output = _text(view, "test_output")
    guard = _guard_facts(test_output, _files(view))
    if guard is not None:
        return ParkClass("guard_demands_path", guard)
    pin = _pin_facts(test_output, _text(view, "branch_diff"))
    if pin is not None:
        return ParkClass("pin_removed_test_failure", pin)
    scope_paths = _unique(_SCOPE_RE.findall(review))
    if scope_paths:
        return ParkClass("scope_gate", {"paths": scope_paths})
    if reason.startswith("rebase conflict:"):
        return ParkClass("rebase_conflict", _rebase_facts(reason))
    if _ci_is_red(view):
        return ParkClass("ci_red_after_rework", {"failing_ids": _ci_failing_ids(view)})
    no_commit = _NO_COMMIT_RE.search(reason)
    if no_commit:
        return ParkClass("no_commit_after_rework", {"n": int(no_commit.group(1))})
    if any(marker in reason for marker in _TRANSIENT_MARKERS):
        return ParkClass("transient_bookkeeping", {})
    if _HIGH_RISK_MARKER in reason:
        return ParkClass("high_risk_hold", {})
    return ParkClass("unknown", {})


def _rebase_facts(reason: str) -> dict:
    matches = _CONFLICT_RE.findall(reason)
    kind = _CONFLICT_KINDS.get(matches[0][0], "other") if matches else "other"
    return {"paths": _unique(path for _, path in matches), "kind": kind}


def _guard_facts(test_output: str, files: list) -> dict | None:
    declared = set(files)
    paths: list = []
    for line in test_output.splitlines():
        if not any(phrase in line for phrase in _GUARD_PHRASES):
            continue
        paths.extend(
            path for path in _PATH_RE.findall(line)
            if path not in declared and not _is_test_path(path)
        )
    paths = _unique(paths)
    if not paths:
        return None
    return {"paths": paths, "test_ids": _failed_ids(test_output)}


def _pin_facts(test_output: str, branch_diff: str) -> dict | None:
    names = [m.group(1) for m in _ATTR_RE.finditer(test_output)]
    names += [m.group(1) for m in _IMPORT_RE.finditer(test_output)]
    if not names or not branch_diff:
        return None
    removed = list(_removed_lines(branch_diff))
    for name in names:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if any(pattern.search(text) for text in removed):
            return {"name": name, "test_files": _unique(p for p in _PATH_RE.findall(test_output) if _is_test_path(p))}
    return None


def _ci_is_red(view: dict) -> bool:
    if view.get("ci_rework"):
        return True
    return _pr_checks(view).get("state") == "fail"


def _ci_failing_ids(view: dict) -> list:
    pr_checks = _pr_checks(view)
    texts = [_text(view, "review_feedback"), _text(view, "test_output")]
    texts += [value for value in pr_checks.values() if isinstance(value, str)]
    return _failed_ids("\n".join(texts))


def _pr_checks(view: dict) -> dict:
    evidence = view.get("merge_park_evidence")
    if not isinstance(evidence, dict):
        return {}
    checks = evidence.get("pr_checks")
    return checks if isinstance(checks, dict) else {}


def _removed_lines(diff: str):
    current = None
    for line in diff.splitlines():
        header = _DIFF_HEADER_RE.match(line)
        if header:
            current = header.group(1)
            continue
        if line.startswith("-") and not line.startswith("---") and current and not _is_test_path(current):
            yield line[1:]


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        any(part in ("tests", "test") for part in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _failed_ids(text: str) -> list:
    return _unique(m.group(1) for m in _FAILED_RE.finditer(text))


def _unique(items) -> list:
    seen: list = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
