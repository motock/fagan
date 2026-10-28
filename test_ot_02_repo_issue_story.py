"""Guard for the ``OT-02-repo-issue`` follow-up story artifact.

OT-02 was parked with a *stale* ``parked_reason``: live git state shows the work
committed at ``f8453e5`` on ``agent/ot-02`` (PR #1347 open), while the pipeline
recorded "no new commit". Two pipeline defects caused that churn:

(a) the new-commits/base-branch measurement returned ``INCONCLUSIVE`` because
    ``main`` is unresolved in that worktree, and the pipeline converted the
    inconclusive result into a false "no new commit" park (after two wasted
    redispatches), instead of escalating;
(b) dispatched sessions expose no ``request_decision`` tool, so the agent could
    not satisfy the scope gate and had to self-report ``SCOPE-CONFLICT``.

This test fails until ``stories/OT-02-repo-issue.md`` exists and carries the
adjudication the humans and the next agent need. It also pins idempotency: the
store must hold exactly one record for the id, on every run.

Scope note: this file is the ONLY test added by this task. It never edits
OT-02's telemetry tests, and it does not touch ``pipeline/service.py`` or
``pipeline/advance_merge.py`` - the recording sites there are IN SCOPE.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "stories"
STORY_ID = "OT-02-repo-issue"
STORY_PATH = STORE / f"{STORY_ID}.md"

# The store record is a single ```json block at the top of the markdown file.
FENCE = re.compile(r"^```json\s*$(.*?)^```\s*$", re.DOTALL | re.MULTILINE)

# Schema fields (see .claude/rules/pipeline-story-schema.md and the canonical
# repo-issue child shape in pipeline/triage_repo_issue.py::_execute_repo_issue).
REQUIRED_FIELDS = {
    "id",
    "key",
    "type",
    "parent",
    "status",
    "severity",
    "summary",
    "agent_instructions",
    "persona",
    "risk",
    "backend",
    "defects",
    "scope_adjudication",
    "hold_conditions",
    "pre_merge_requirement",
    "acceptance_criteria",
}


def _flat(text: str) -> str:
    """Collapse whitespace so assertions do not depend on line wrapping."""
    return re.sub(r"\s+", " ", text)


def _records():
    """Every parseable story record in the store, with its path."""
    out = []
    for path in sorted(STORE.glob("*.md")):
        match = FENCE.search(path.read_text(encoding="utf-8"))
        if match is None:
            continue
        out.append((path, json.loads(match.group(1))))
    return out


def _story():
    assert STORY_PATH.is_file(), f"OT-02-repo-issue story missing: {STORY_PATH}"
    text = STORY_PATH.read_text(encoding="utf-8")
    match = FENCE.search(text)
    assert match is not None, f"{STORY_PATH} has no ```json story record"
    return text, json.loads(match.group(1))


def test_store_has_exactly_one_record_for_the_id():
    """Re-running the write must not add a second, suffixed record."""
    matches = [(p, r) for p, r in _records() if r.get("id") == STORY_ID]
    assert len(matches) == 1, (
        f"expected exactly 1 {STORY_ID} record, found {[p.name for p, _ in matches]}"
    )
    assert matches[0][0].name == STORY_PATH.name
    # A timestamp/unique suffix would make the backlog grow on every re-run.
    siblings = sorted(p.name for p in STORE.glob(f"{STORY_ID}-*.md"))
    assert siblings == [], f"timestamp-suffixed duplicate stories: {siblings}"


def test_record_satisfies_store_schema():
    _, record = _story()
    missing = REQUIRED_FIELDS - record.keys()
    assert missing == set(), f"story record missing schema fields: {sorted(missing)}"
    assert record["id"] == STORY_ID
    assert record["key"] == STORY_ID
    assert record["type"] == "repo-issue"
    assert record["parent"] == "OT-02"
    assert record["status"] in {"open", "todo"}, record["status"]
    assert isinstance(record["agent_instructions"], str) and record["agent_instructions"]
    assert isinstance(record["acceptance_criteria"], list) and record["acceptance_criteria"]
    assert isinstance(record["hold_conditions"], list) and record["hold_conditions"]


def test_record_references_the_live_evidence():
    _, record = _story()
    blob = json.dumps(record)
    assert "f8453e5" in blob, "story must cite the committed sha f8453e5"
    assert "agent/ot-02" in blob, "story must cite branch agent/ot-02"
    assert record["parent_pr"]["number"] == 1347
    assert record["parent_pr"]["state"] == "OPEN"
    assert record["parent_pr"]["merged"] is False


def test_names_both_pipeline_defects():
    text, record = _story()
    blob = _flat(text)
    defects = record["defects"]
    assert set(defects) >= {"a", "b"}, sorted(defects)

    # (a) inconclusive measurement -> false "no new commit" park
    assert "INCONCLUSIVE" in blob
    assert "main" in blob and "unresolved" in blob.lower()
    assert "no new commit" in blob.lower()
    assert "two wasted redispatches" in blob.lower() or "two redispatches" in blob.lower()
    assert "escalate" in blob.lower()

    # (b) no request_decision tool -> agent self-reports SCOPE-CONFLICT
    assert "request_decision" in blob
    assert "SCOPE-CONFLICT" in blob


def test_scope_adjudication_is_in_scope():
    text, record = _story()
    blob = _flat(text)
    assert record["scope_adjudication"]["ruling"] == "IN SCOPE"
    assert "PipelineService.dispatch_story" in blob
    assert "pipeline/service.py" in blob
    assert "_adjudicate_merges" in blob
    assert "pipeline/advance_merge.py" in blob
    # The two forbidden "fixes" must be named as forbidden.
    lowered = blob.lower()
    assert "revert" in lowered
    assert "deleting the tests" in lowered or "delete the tests" in lowered


def test_pre_merge_requirement_names_the_unreliable_files():
    text, record = _story()
    blob = _flat(text)
    assert "re-derive the diff against a clean `main`" in blob
    for path in ("app/oidc_login.py", "pipeline/authorization.py", "deploy/"):
        assert path in blob, f"unreliable changed-file {path} must be named"
    assert record["pre_merge_requirement"], "pre_merge_requirement must not be empty"


def test_two_call_trace_is_recorded():
    text, _ = _story()
    blob = _flat(text)
    # Call 1: unresolved main -> INCONCLUSIVE, high-water stays aaaa111
    assert "aaaa111" in blob
    assert "1a2b3c4" in blob
    assert "must not be advanced" in blob.lower()
    assert "needs-human" in blob.lower() or "needs human" in blob.lower()
    # Call 2: resolvable base -> one new commit
    assert "aaaa111..f8453e5" in blob
    assert "NEW_COMMITS=1" in blob
    # The specific mistake: advancing high-water on the inconclusive call.
    assert "f8453e5..f8453e5" in blob
    assert "NO_NEW_COMMITS" in blob
    assert "high_water=f8453e5" in blob or "high_water = f8453e5" in blob


def test_hold_conditions_are_pinned():
    text, record = _story()
    blob = _flat(text).lower()
    assert record["status"] != "done"
    assert "do not mark ot-02 done" in blob or "do not mark it done" in blob
    assert "do not merge pr #1347" in blob
    assert "do not redispatch" in blob
    assert any("human" in cond.lower() for cond in record["hold_conditions"])


def test_negative_no_claim_of_reverted_recording_sites():
    """The story must never record the forbidden revert as done."""
    text, record = _story()
    blob = _flat(text).lower()
    assert record["scope_adjudication"]["reverted"] is False
    assert record["scope_adjudication"]["tests_deleted"] is False
    assert "did not revert" in blob or "reverting them is forbidden" in blob


def test_write_is_idempotent_across_runs():
    """Reading the store twice must yield one record both times."""
    first = len([r for _, r in _records() if r.get("id") == STORY_ID])
    second = len([r for _, r in _records() if r.get("id") == STORY_ID])
    assert first == 1, f"run 1 saw {first} records for {STORY_ID}"
    assert second == 1, f"run 2 saw {second} records for {STORY_ID}"
