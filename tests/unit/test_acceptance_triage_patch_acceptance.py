"""Acceptance oracle: the ``patch_acceptance`` triage action (OPSA-7).

A born-broken acceptance fixture (one that cannot pass no matter what an
implementer writes) is the most expensive single failure mode in the pipeline:
it burns an implementer's whole step budget and produces no usable signal. The
fixture is a read-only oracle, so the dispatched agent can never repair it.
OPSA-7 makes the repair a first-class executable triage action:

* ``pipeline/parsers.py`` recognises ``patch_acceptance`` (fail-closed
  normalization unchanged - an unknown ACTION still parks).
* ``pipeline/triage.py`` gains ``_execute_patch_acceptance``, which applies
  ONLY when the story's fixture is demonstrably broken at a clean baseline
  (reusing ``pipeline.oracle_gate``'s existing classification/validation), asks
  the overlord for a corrected fixture, and VALIDATES BEFORE WRITING: the
  rewrite must pass OPSA-8's lint/collection helper AND must still FAIL at a
  clean baseline (a "fixed" fixture that passes with no implementation is
  isolation-only and is rejected). On success the manifest's authoritative
  acceptance source is rewritten, the PREVIOUS digests are snapshotted into the
  OPSA-3 execution record so the change is undoable, and the story returns to
  ``todo`` for a fresh dispatch.
* ``overlord-policy.md`` publishes ``patch_acceptance`` in the ACTION contract
  line and defines it, plus the ``===FIXTURE-START===`` / ``===FIXTURE-END===``
  + ``DIAGNOSIS:`` output format, in the Failure triage section.

The overlord is stubbed at the true boundary (``pipeline.triage._invoke_overlord``)
- never real network. The policy assertions are membership/prefix based: the
ACTION contract line is a shared artifact later sibling stories extend again.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import build_detect, oracle_gate, overlord, parsers, persistence, triage

_POLICY = Path(__file__).resolve().parents[2] / "overlord-policy.md"

# The base contract every sibling story must keep as the line's prefix.
_BASE_CONTRACT_PREFIX = (
    "ACTION: escalate_model | split_story | repo_issue | park_for_human"
)

_FIXTURE_PATH = "tests/unit/test_x.py"
_ORIGINAL_SOURCE = "def test_x():\n    assert False\n"
_REWRITTEN_SOURCE = "def test_x():\n    assert True\n"

_START = "===FIXTURE-START==="
_END = "===FIXTURE-END==="

_EXECUTOR_PARAMS = (
    "plan_name",
    "story_key",
    "story",
    "ruling",
    "manifest",
    "manifest_path",
)

# OPSA-2's parked-story matrix + autonomy ladder and OPSA-6's mark_done
# definition: sibling content this story must not reword.
_SIBLING_ANCHORS = (
    "### Parked-story resolution",
    "Autonomy ladder:",
    "| stale bookkeeping",
    "| rework exhaustion with mechanical leftovers",
    "| repeated step-caps on oversized scope",
    "| acceptance fixture demonstrably broken at a clean baseline",
    "`mark_done`",
    "`split_story`",
    "`escalate_model`",
    "`repo_issue`",
    "`park_for_human`",
)


def _overlord_reply(source: str = _REWRITTEN_SOURCE, diagnosis: str = "the fixture crashed at import") -> str:
    return f"DIAGNOSIS: {diagnosis}\n{_START}\n{source}{_END}\n"


# ---------------------------------------------------------------------------
# policy helpers
# ---------------------------------------------------------------------------


def _policy_text() -> str:
    return _POLICY.read_text() if _POLICY.is_file() else ""


def _triage_section() -> str:
    return _policy_text().partition("## Failure triage")[2]


def _output_contract_section() -> str:
    return _policy_text().partition("## Output contract")[2]


def _action_contract_line() -> str:
    for line in _output_contract_section().splitlines():
        if line.strip().startswith("ACTION:"):
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# executor harness
# ---------------------------------------------------------------------------


def _source_of(story) -> str | None:
    """The acceptance source carried by whatever the executor hands the
    validators: a story dict, or the source string itself."""
    if isinstance(story, str):
        return story
    if isinstance(story, dict):
        for entry in story.get("acceptance") or []:
            if isinstance(entry, dict) and entry.get("source") is not None:
                return entry["source"]
    return None


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Isolate the executor: stub the overlord, the oracle validators, the
    OPSA-8 lint/collection helpers, and capture notify + decisions."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    calls = {
        "notify": [],
        "decisions": [],
        "overlord": [],
        "validate": [],
        "lint": [],
        "collect": [],
    }
    state = {
        # classification of the ORIGINAL fixture at a clean baseline
        "baseline_state": "errors",
        "baseline_detail": "baseline failure output: SyntaxError in fixture",
        # classification of the REWRITTEN fixture at a clean baseline
        "rewritten_state": "fails_correctly",
        "rewritten_detail": "rewritten fixture fails as expected",
        "lint_kind": "clean",
        "lint_msg": None,
        "overlord_reply": _overlord_reply(),
        "overlord_raises": None,
        "role_config": {"overlord": {"model": "sentinel-model"}},
        "evidence": "EVIDENCE-SENTINEL-42",
    }

    def fake_validate(story, checkout=None, **kwargs):
        src = _source_of(story)
        calls["validate"].append({"story": story, "checkout": checkout, "source": src})
        if isinstance(story, dict) and not (story.get("acceptance") or []):
            # Mirrors the real validate_acceptance_fixtures: no fixtures -> "none".
            return {"state": "none", "detail": "story has no acceptance fixtures", "paths": []}
        if src == _REWRITTEN_SOURCE:
            return {
                "state": state["rewritten_state"],
                "detail": state["rewritten_detail"],
                "paths": [_FIXTURE_PATH],
            }
        return {
            "state": state["baseline_state"],
            "detail": state["baseline_detail"],
            "paths": [_FIXTURE_PATH],
        }

    def fake_lint(story, repo_root=None, **kwargs):
        calls["lint"].append({"story": story, "source": _source_of(story)})
        return (state["lint_kind"], state["lint_msg"])

    def fake_pytest_collect(story, repo_root=None, **kwargs):
        calls["lint"].append({"story": story, "source": _source_of(story)})
        return (state["lint_kind"], state["lint_msg"])

    def fake_overlord(prompt, plan_role_config=None, **kwargs):
        calls["overlord"].append({"prompt": prompt, "plan_role_config": plan_role_config})
        if state["overlord_raises"] is not None:
            raise state["overlord_raises"]
        return state["overlord_reply"]

    def fake_collect_evidence(worktree=None, story=None, findings=None, **kwargs):
        calls["collect"].append(worktree)
        return state["evidence"]

    # The overlord boundary: never real network.
    monkeypatch.setattr(triage, "_invoke_overlord", fake_overlord)
    monkeypatch.setattr(overlord, "_invoke_overlord", fake_overlord)
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan_name: state["role_config"])
    # Also patch the lazy-import source, in case the executor resolves the
    # plan's role config from pipeline.persistence directly.
    monkeypatch.setattr(
        persistence, "_plan_role_config", lambda plan_name: state["role_config"]
    )
    monkeypatch.setattr(
        triage, "_notify_user", lambda *a, **k: calls["notify"].append((a, k))
    )
    monkeypatch.setattr(
        triage, "_append_decision", lambda plan, rec: calls["decisions"].append(rec)
    )
    monkeypatch.setattr(triage, "collect_triage_evidence", fake_collect_evidence, raising=False)

    # oracle_gate's existing classification/validation must be REUSED, not
    # re-derived. Patch both the module attribute and any name imported into
    # triage, so either import style is covered.
    monkeypatch.setattr(oracle_gate, "validate_acceptance_fixtures", fake_validate)
    monkeypatch.setattr(triage, "validate_acceptance_fixtures", fake_validate, raising=False)

    # OPSA-8's lint/collection helpers (one implementation, two callers).
    monkeypatch.setattr(build_detect, "_lint_acceptance_fixtures", fake_lint)
    monkeypatch.setattr(triage, "_lint_acceptance_fixtures", fake_lint, raising=False)
    monkeypatch.setattr(build_detect, "_pytest_acceptance_fixtures", fake_pytest_collect)
    monkeypatch.setattr(triage, "_pytest_acceptance_fixtures", fake_pytest_collect, raising=False)

    return SimpleNamespace(worktree=str(worktree), calls=calls, state=state)


def _story(harness, **over):
    story = {
        "key": "OPSA-7",
        "story_key": "OPSA-7",
        "summary": "patch_acceptance executor",
        "status": "parked",
        "parked_reason": "acceptance fixture demonstrably broken at a clean baseline",
        "worktree": harness.worktree,
        "acceptance": [{"path": _FIXTURE_PATH, "source": _ORIGINAL_SOURCE}],
    }
    story.update(over)
    return story


def _manifest(story):
    return {"stories": {story["key"]: story}}


def _run(harness, story, ruling=None, manifest=None, manifest_path=None):
    return triage._execute_patch_acceptance(
        "plan-a",
        story["key"],
        story,
        ruling
        if ruling is not None
        else {"action": "patch_acceptance", "rationale": "born-broken oracle"},
        manifest if manifest is not None else _manifest(story),
        manifest_path
        if manifest_path is not None
        else Path("/tmp/plan-a.manifest.json"),
    )


def _manifest_source(manifest, key="OPSA-7"):
    return manifest["stories"][key]["acceptance"][0]["source"]


def _park_text(story, harness):
    """Everything the park path said: the recorded reason plus the notification."""
    parts = [str(story.get("parked_reason") or "")]
    for args, kwargs in harness.calls["notify"]:
        parts.extend(str(a) for a in args)
        parts.extend(str(v) for v in kwargs.values())
    return "\n".join(parts)


def _digest_field(record):
    for key, value in record.items():
        lowered = str(key).lower()
        if "digest" in lowered or "hash" in lowered:
            return key, value
    return None, None


def _digests_of(source):
    return oracle_gate.acceptance_digests(
        {"acceptance": [{"path": _FIXTURE_PATH, "source": source}]}
    )


# ---------------------------------------------------------------------------
# parsers: patch_acceptance is a recognized action
# ---------------------------------------------------------------------------


def test_patch_acceptance_is_a_recognized_triage_action():
    assert "patch_acceptance" in parsers.TRIAGE_ACTIONS


def test_triage_actions_keep_the_existing_actions():
    # Membership, not equality: later sibling stories extend this set again.
    assert parsers.TRIAGE_ACTIONS >= {
        "escalate_model",
        "split_story",
        "repo_issue",
        "park_for_human",
        "mark_done",
        "patch_acceptance",
    }


def test_normalize_action_accepts_patch_acceptance():
    assert parsers._normalize_action("patch_acceptance") == "patch_acceptance"
    assert parsers._normalize_action("  PATCH_ACCEPTANCE  ") == "patch_acceptance"


def test_parse_ruling_reads_a_patch_acceptance_action():
    ruling = parsers._parse_ruling(
        "ACTION: patch_acceptance\nRATIONALE: born-broken oracle"
    )
    assert ruling["action"] == "patch_acceptance"


def test_unknown_action_still_fails_closed_to_park_for_human():
    assert parsers._normalize_action("nonsense") == "park_for_human"
    assert parsers._normalize_action(None) == "park_for_human"
    assert parsers._parse_ruling("ACTION: nonsense")["action"] == "park_for_human"


# ---------------------------------------------------------------------------
# executor shape + dispatch
# ---------------------------------------------------------------------------


def test_execute_patch_acceptance_exists_with_the_executor_signature():
    assert hasattr(triage, "_execute_patch_acceptance"), (
        "pipeline.triage must implement _execute_patch_acceptance"
    )
    params = tuple(inspect.signature(triage._execute_patch_acceptance).parameters)
    assert params == _EXECUTOR_PARAMS


def test_patch_acceptance_is_not_a_deferred_action():
    assert "patch_acceptance" not in triage.DEFERRED_ACTIONS


def test_execute_ruling_dispatches_patch_acceptance(harness, monkeypatch):
    story = _story(harness)
    manifest = _manifest(story)
    manifest_path = Path("/tmp/plan-a.manifest.json")
    ruling = {"action": "patch_acceptance", "rationale": "born-broken oracle"}
    seen = []

    def fake_exec(*args, **kwargs):
        seen.append((args, kwargs))
        return "patch_acceptance"

    monkeypatch.setattr(triage, "_execute_patch_acceptance", fake_exec)

    result = triage.execute_ruling(
        "plan-a", story["key"], story, ruling, manifest, manifest_path
    )

    assert result == "patch_acceptance"
    assert len(seen) == 1, "execute_ruling must route patch_acceptance to its executor"
    args, kwargs = seen[0]
    passed = list(args) + list(kwargs.values())
    assert "plan-a" in passed
    assert "OPSA-7" in passed
    assert story in passed
    assert manifest in passed
    assert manifest_path in passed
    assert ruling in passed


# ---------------------------------------------------------------------------
# born-broken + valid rewrite -> manifest updated, digests snapshotted, todo
# ---------------------------------------------------------------------------


def test_born_broken_story_with_a_valid_rewrite_updates_the_manifest_source(harness):
    story = _story(harness)
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "patch_acceptance"
    assert _manifest_source(manifest) == _REWRITTEN_SOURCE
    assert story["acceptance"][0]["source"] == _REWRITTEN_SOURCE


def test_success_returns_the_story_to_todo_for_a_fresh_dispatch(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    assert story["status"] == "todo"


def test_success_records_the_prior_acceptance_digests_in_the_execution_record(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    records = [r for r in harness.calls["decisions"] if r.get("action") == "patch_acceptance"]
    assert records, "the executed action must be recorded in the decisions log"
    key, value = _digest_field(records[-1])
    assert key is not None, "the execution record must carry the prior acceptance digests"
    assert value == _digests_of(_ORIGINAL_SOURCE)


def test_recorded_digests_are_the_PREVIOUS_source_not_the_rewrite(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    record = [r for r in harness.calls["decisions"] if r.get("action") == "patch_acceptance"][-1]
    _, value = _digest_field(record)
    assert value != _digests_of(_REWRITTEN_SOURCE)
    assert value == _digests_of(_ORIGINAL_SOURCE)


def test_success_records_the_execution_via_the_opsa3_helper(harness):
    story = _story(harness)
    manifest = _manifest(story)
    prior_reason = story["parked_reason"]

    _run(harness, story, manifest=manifest)

    record = [r for r in harness.calls["decisions"] if r.get("action") == "patch_acceptance"][-1]
    assert record.get("story_key") == "OPSA-7"
    assert record.get("prior_status") == "parked"
    assert record.get("prior_parked_reason") == prior_reason


def test_success_does_not_park_the_story(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    assert story["status"] != "parked"


# ---------------------------------------------------------------------------
# the overlord prompt
# ---------------------------------------------------------------------------


def test_overlord_is_invoked_with_the_plan_role_config(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    assert harness.calls["overlord"], "the overlord must be re-invoked for a rewrite"
    assert harness.calls["overlord"][0]["plan_role_config"] == harness.state["role_config"]


def test_overlord_prompt_carries_the_fixture_source_failure_output_and_evidence(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    prompt = harness.calls["overlord"][0]["prompt"]
    assert _ORIGINAL_SOURCE in prompt, "the prompt must carry the broken fixture source"
    assert harness.state["baseline_detail"] in prompt, "the prompt must carry the failure output"
    assert (
        harness.state["evidence"] in prompt or "born-broken oracle" in prompt
    ), "the prompt must carry the triage evidence"


def test_overlord_prompt_instructs_the_fixture_marker_format(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    prompt = harness.calls["overlord"][0]["prompt"]
    assert _START in prompt
    assert _END in prompt
    assert "DIAGNOSIS:" in prompt


# ---------------------------------------------------------------------------
# not born-broken -> park loudly, overlord never consulted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["fails_correctly", "passes", "empty", "none"])
def test_not_born_broken_parks_without_invoking_the_overlord(harness, state):
    harness.state["baseline_state"] = state
    story = _story(harness)
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story.get("parked_reason")
    assert harness.calls["notify"], "the park must be loud"
    assert harness.calls["overlord"] == [], "a healthy fixture must not reach the overlord"
    assert _manifest_source(manifest) == _ORIGINAL_SOURCE


def test_baseline_is_classified_before_the_overlord_is_consulted(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    assert harness.calls["validate"], "the baseline classification must be consulted"
    assert harness.calls["validate"][0]["source"] == _ORIGINAL_SOURCE
    assert harness.calls["validate"][0]["checkout"] is not None


def test_story_with_no_acceptance_fixtures_parks(harness):
    story = _story(harness, acceptance=[])
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert harness.calls["notify"], "the park must be loud"
    assert harness.calls["overlord"] == [], "nothing to rewrite without a fixture"


def test_story_with_a_missing_acceptance_key_parks(harness):
    story = _story(harness)
    del story["acceptance"]
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert harness.calls["overlord"] == []


# ---------------------------------------------------------------------------
# unparseable overlord response -> park loudly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("", id="empty"),
        pytest.param("garbage", id="no-markers"),
        pytest.param(f"DIAGNOSIS: x\n{_START}\n{_REWRITTEN_SOURCE}", id="missing-end"),
        pytest.param(f"{_START}\n{_REWRITTEN_SOURCE}{_END}\n", id="missing-diagnosis"),
        pytest.param(f"DIAGNOSIS: x\n{_START}\n{_END}\n", id="empty-body"),
        pytest.param(f"DIAGNOSIS: x\n{_START}\n{_END}", id="empty-body-no-newline"),
    ],
)
def test_unparseable_overlord_response_parks_loudly(harness, reply):
    harness.state["overlord_reply"] = reply
    story = _story(harness)
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story.get("parked_reason")
    assert harness.calls["notify"], "the park must be loud"
    assert _manifest_source(manifest) == _ORIGINAL_SOURCE


def test_overlord_invocation_failure_parks_loudly(harness):
    harness.state["overlord_raises"] = RuntimeError("backend down")
    story = _story(harness)
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert story.get("parked_reason")
    assert harness.calls["notify"], "the park must be loud"
    assert _manifest_source(manifest) == _ORIGINAL_SOURCE


# ---------------------------------------------------------------------------
# validate BEFORE writing
# ---------------------------------------------------------------------------


def test_rewritten_source_failing_lint_parks_naming_the_violations(harness):
    harness.state["lint_kind"] = "finding"
    harness.state["lint_msg"] = "tests/unit/test_x.py:1:8: F401 `os` imported but unused"
    story = _story(harness)
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert "F401" in _park_text(story, harness), "the park must name the violations"
    assert harness.calls["notify"], "the park must be loud"
    assert _manifest_source(manifest) == _ORIGINAL_SOURCE


def test_rewritten_source_passing_at_a_clean_baseline_parks_as_isolation_only(harness):
    harness.state["rewritten_state"] = "passes"
    story = _story(harness)
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    text = _park_text(story, harness).lower()
    assert "isolation" in text or "pass" in text or "baseline" in text, (
        "a rewrite that passes with no implementation is isolation-only and must be named"
    )
    assert harness.calls["notify"], "the park must be loud"
    assert _manifest_source(manifest) == _ORIGINAL_SOURCE


@pytest.mark.parametrize("state", ["empty", "none", "errors"])
def test_rewritten_source_not_failing_correctly_parks(harness, state):
    harness.state["rewritten_state"] = state
    story = _story(harness)
    manifest = _manifest(story)

    result = _run(harness, story, manifest=manifest)

    assert result == "park_for_human"
    assert story["status"] == "parked"
    assert harness.calls["notify"], "the park must be loud"
    assert _manifest_source(manifest) == _ORIGINAL_SOURCE


def test_rewritten_source_is_validated_and_linted_before_it_is_written(harness):
    story = _story(harness)
    manifest = _manifest(story)

    _run(harness, story, manifest=manifest)

    assert any(
        call["source"] == _REWRITTEN_SOURCE for call in harness.calls["validate"]
    ), "the rewrite must be re-validated at a clean baseline"
    assert any(
        call["source"] == _REWRITTEN_SOURCE for call in harness.calls["lint"]
    ), "the rewrite must pass OPSA-8's lint/collection helper"


def test_opsa8_lint_helper_is_reused_not_reimplemented():
    source = Path(triage.__file__).read_text()
    assert (
        "_lint_acceptance_fixtures" in source or "_pytest_acceptance_fixtures" in source
    ), "triage must import OPSA-8's helper rather than re-deriving the check"
    # One implementation, two callers: if triage binds the name, it must be
    # OPSA-8's function object, never a local re-implementation.
    for name in ("_lint_acceptance_fixtures", "_pytest_acceptance_fixtures"):
        if hasattr(triage, name):
            assert getattr(triage, name) is getattr(build_detect, name), (
                f"triage.{name} must be OPSA-8's helper, not a re-implementation"
            )


def test_oracle_gate_validation_is_reused_not_reimplemented():
    source = Path(triage.__file__).read_text()
    assert "validate_acceptance_fixtures" in source, (
        "triage must reuse pipeline.oracle_gate's existing validation"
    )
    if hasattr(triage, "validate_acceptance_fixtures"):
        assert triage.validate_acceptance_fixtures is oracle_gate.validate_acceptance_fixtures, (
            "triage must reuse pipeline.oracle_gate's validation, not re-derive it"
        )


# ---------------------------------------------------------------------------
# policy: the ACTION contract + the triage definition + the output format
# ---------------------------------------------------------------------------


def test_policy_file_is_present():
    assert _POLICY.is_file()


def test_action_contract_line_gains_patch_acceptance():
    line = _action_contract_line()
    assert line.startswith(_BASE_CONTRACT_PREFIX), (
        "the contract line must keep the base actions as its prefix"
    )
    assert "patch_acceptance" in line


def test_failure_triage_section_defines_patch_acceptance():
    section = _triage_section()
    assert "patch_acceptance" in section
    # A definition, in the same bullet style as the sibling actions, not just
    # the matrix row OPSA-2 added.
    definition_lines = [
        line.strip()
        for line in section.splitlines()
        if re.match(r"^[-*]\s*\**`patch_acceptance`", line.strip())
    ]
    assert definition_lines, (
        "the Failure triage section must define patch_acceptance in the same "
        "bullet style as the sibling actions"
    )


def test_failure_triage_section_keeps_the_sibling_action_definitions():
    section = _triage_section()
    missing = [
        name
        for name in ("escalate_model", "split_story", "repo_issue", "park_for_human", "mark_done")
        if f"- `{name}`" not in section
    ]
    assert missing == []


def test_failure_triage_section_documents_the_fixture_marker_format():
    section = _triage_section()
    assert _START in section
    assert _END in section
    assert "DIAGNOSIS:" in section


def test_failure_triage_section_states_the_fail_closed_default():
    assert "fail closed" in _triage_section().lower()


def test_policy_retains_the_sibling_anchors():
    text = _policy_text()
    missing = [anchor for anchor in _SIBLING_ANCHORS if anchor not in text]
    assert missing == []


def test_policy_retains_the_existing_output_contract_fields():
    text = _policy_text()
    absent = [
        field
        for field in ("RULING:", "TIER:", "RISK:", "RATIONALE:", "NOTIFY_USER:", "SPLIT:")
        if field not in text
    ]
    assert absent == []
