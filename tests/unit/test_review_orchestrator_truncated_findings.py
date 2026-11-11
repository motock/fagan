"""A truncated REQUEST_CHANGES body must not be routed to a rework round.

b4 evidence (2026-11-09/10, standalone stack): at 09:26:33 the reviewer
returned 1,631 characters of feedback for 94efbfbc that ended mid-sentence at
"ONE blocking finding remains", with the finding itself cut off, and
``story["last_review_findings"]`` came back empty. The reviewer persona emits
the VERDICT line FIRST and the findings AFTER it, so a length-truncated
response still parses as a verdict. The old check only asked "is there any
non-VERDICT text?", so the cut-off body was accepted as a genuine rejection
and routed to a rework - burning the rework budget on feedback no agent could
act on.

A long body whose findings cannot be parsed (no file-scoped ``- Blocking:``
path and no ``- Blocking: (unscoped)`` finding) is "unfocused/truncated": it
must take the EXISTING review-inconclusive path - increment
``review_inconclusive_count``, notify, and park only at
``REVIEW_INCONCLUSIVE_MAX`` - and must never write ``review_feedback`` or
bump ``rework_attempts``.

The threshold is a named module constant (600 characters). Terse stubs like
``"still bad\\nVERDICT: REQUEST_CHANGES"`` stay below it and keep today's
behaviour: the rework path.
"""

import json

import pytest

import pipeline.server as p
from pipeline import parsers
from pipeline import persistence as ppers

# ---------------------------------------------------------------------------
# Fixtures (mirror test_final_rework_escalation.py - standalone, no cross-file
# imports of test code).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


def _write_manifest_with_story(plan_dir, plan_name, story_key, story):
    manifest = {"epics": {}, "stories": {story_key: story}}
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _make_story(plan_dir, *, status, **extra):
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt"),
        "risk": "low",
        "backend": "ollama",
        "model": "gpt-oss:20b",
    }
    story.update(extra)
    return story


def _force_reviewer_output(monkeypatch, output):
    """Mock _run_reviewer to return `output`, and make _open_pr explode if it
    is ever reached (a REQUEST_CHANGES must never open a PR)."""
    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                       acceptance=None, since_sha=None, risk=None):
        return output

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)

    def _boom_pr(*a, **k):
        raise AssertionError("PR must not be opened on REQUEST_CHANGES")

    monkeypatch.setattr(p, "_open_pr", _boom_pr)


def _disable_auto_escalation(monkeypatch):
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------

VERDICT_LINE = "VERDICT: REQUEST_CHANGES"

# The b4 shape: the reviewer persona emits VERDICT FIRST and the findings
# after it, so a length-truncated body still parses as a verdict. The prose
# ends mid-sentence with the actual finding cut off, and there is no
# ``- Blocking:`` line anywhere -> the findings are not parseable.
_FILLER = (
    "I went through the change in detail and there is a lot going on here. "
)


def _truncated_body(total_chars=700):
    """A REQUEST_CHANGES body of exactly `total_chars` characters with no
    parseable finding."""
    body = VERDICT_LINE + "\n" + _FILLER
    tail = "ONE blocking finding remains, and it concerns the way the cli"
    body += tail
    if len(body) < total_chars:
        body += "x" * (total_chars - len(body))
    assert len(body) == total_chars
    return body


# Long, and it DOES carry a file-scoped Blocking finding -> parseable.
LONG_WITH_FILE_FINDING = (
    VERDICT_LINE
    + "\n"
    + "The reviewer walked the diff and found two problems. " * 12
    + "\n- Blocking: pipeline/parsers.py: the parser drops the last line of "
    "the response when the body has no trailing newline, so a truncated "
    "review is indistinguishable from a complete one.\n"
    "The second problem is cosmetic and does not need to be fixed now.\n"
)

# The terse reviewer-stub convention: below the threshold, no Blocking
# finding. Today this is still a genuine rejection.
TERSE_STUB = "still bad\nVERDICT: REQUEST_CHANGES"


def test_the_bodies_are_the_shape_they_claim_to_be():
    """Guard the fixtures themselves: the truncated body is long and
    unparseable; the long one is long and parseable; the stub is short."""
    assert len(_truncated_body()) > 600
    assert parsers.findings_are_parseable(_truncated_body()) is False
    assert len(LONG_WITH_FILE_FINDING) > 600
    assert parsers.findings_are_parseable(LONG_WITH_FILE_FINDING) is True
    assert len(TERSE_STUB) <= 600
    assert parsers.findings_are_parseable(TERSE_STUB) is False


# ---------------------------------------------------------------------------
# (1) Long, unparseable REQUEST_CHANGES -> the inconclusive path.
# ---------------------------------------------------------------------------

def test_long_unparseable_request_changes_is_inconclusive(
    plan_dir, agents_dir, monkeypatch,
):
    """A long REQUEST_CHANGES with no parseable finding is NOT a rejection:
    no review_feedback, no rework attempt, status unchanged, counter
    incremented."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed", rework_attempts=0,
        review_inconclusive_count=0,
    )
    _write_manifest_with_story(plan_dir, "fre", "S1", story)
    _force_reviewer_output(monkeypatch, _truncated_body())

    result = p.review_story("fre", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "tests_passed", (
        "a truncated review must leave the status where it was so the next "
        f"tick retries; got {on_disk['status']!r}"
    )
    assert on_disk["review_inconclusive_count"] == 1
    assert "review_feedback" not in on_disk, (
        "a truncated review must not write review_feedback: the body has no "
        "actionable finding to hand a rework agent"
    )
    assert on_disk.get("rework_attempts", 0) == 0, (
        "a truncated review must not burn the rework budget"
    )
    assert on_disk.get("last_review_findings", []) == []


def test_long_unparseable_request_changes_does_not_park_before_the_max(
    plan_dir, agents_dir, monkeypatch,
):
    """Below REVIEW_INCONCLUSIVE_MAX the story stays parked-free: only the
    counter moves."""
    _disable_auto_escalation(monkeypatch)

    story = _make_story(
        plan_dir, status="tests_passed",
        review_inconclusive_count=0,
    )
    _write_manifest_with_story(plan_dir, "fre", "S1", story)
    _force_reviewer_output(monkeypatch, _truncated_body())

    p.review_story("fre", "S1")

    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "tests_passed"
    assert "parked_reason" not in on_disk
    assert on_disk["review_inconclusive_count"] == 1


def test_long_unparseable_request_changes_parks_at_the_inconclusive_max(
    plan_dir, agents_dir, monkeypatch,
):
    """At REVIEW_INCONCLUSIVE_MAX the existing inconclusive path parks the
    story for a human - the truncated review is escalated, not reworked."""
    _disable_auto_escalation(monkeypatch)

    max_attempts = p.REVIEW_INCONCLUSIVE_MAX
    story = _make_story(
        plan_dir, status="tests_passed",
        review_inconclusive_count=max_attempts - 1,
    )
    _write_manifest_with_story(plan_dir, "fre", "S1", story)
    _force_reviewer_output(monkeypatch, _truncated_body())

    p.review_story("fre", "S1")

    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "parked"
    assert on_disk["review_inconclusive_count"] == max_attempts
    assert "review_feedback" not in on_disk
    assert on_disk.get("rework_attempts", 0) == 0


# ---------------------------------------------------------------------------
# (2) Long REQUEST_CHANGES WITH a parseable finding -> today's rework path.
# ---------------------------------------------------------------------------

def test_long_parseable_request_changes_takes_the_rework_path(
    plan_dir, agents_dir, monkeypatch,
):
    """A long body that carries `- Blocking: pipeline/x.py: ...` keeps today's
    behaviour: review_feedback written, rework attempted, status
    changes_requested."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed", rework_attempts=0,
        review_inconclusive_count=0,
    )
    _write_manifest_with_story(plan_dir, "fre", "S1", story)
    _force_reviewer_output(monkeypatch, LONG_WITH_FILE_FINDING)

    result = p.review_story("fre", "S1")

    assert result["verdict"] == "REQUEST_CHANGES"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["rework_attempts"] == 1
    assert "review_feedback" in on_disk
    assert on_disk["last_review_findings"] == ["pipeline/parsers.py"]
    assert on_disk["review_inconclusive_count"] == 0, (
        "a parseable rejection is not inconclusive: the counter is reset"
    )


# ---------------------------------------------------------------------------
# (3) The terse stub keeps today's behaviour (threshold regression guard).
# ---------------------------------------------------------------------------

def test_terse_request_changes_stub_takes_the_rework_path(
    plan_dir, agents_dir, monkeypatch,
):
    """``still bad\\nVERDICT: REQUEST_CHANGES`` is below the threshold and
    stays a genuine rejection: the rework path, not the inconclusive path."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed", rework_attempts=0,
        review_inconclusive_count=0,
    )
    _write_manifest_with_story(plan_dir, "fre", "S1", story)
    _force_reviewer_output(monkeypatch, TERSE_STUB)

    result = p.review_story("fre", "S1")

    assert result["verdict"] == "REQUEST_CHANGES"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["rework_attempts"] == 1
    assert "review_feedback" in on_disk
    assert on_disk["review_inconclusive_count"] == 0


def test_body_at_the_threshold_still_takes_the_rework_path(
    plan_dir, agents_dir, monkeypatch,
):
    """Boundary: the threshold is EXCLUSIVE - a body of exactly 600
    characters is not "longer than 600", so it keeps today's rework path."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    body = _truncated_body(600)
    assert len(body) == 600

    story = _make_story(
        plan_dir, status="tests_passed", rework_attempts=0,
        review_inconclusive_count=0,
    )
    _write_manifest_with_story(plan_dir, "fre", "S1", story)
    _force_reviewer_output(monkeypatch, body)

    p.review_story("fre", "S1")

    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["rework_attempts"] == 1
    assert on_disk["review_inconclusive_count"] == 0


# ---------------------------------------------------------------------------
# (4) The threshold is a named module constant of 600.
# ---------------------------------------------------------------------------

def test_truncation_threshold_is_a_module_level_constant_of_600():
    """The 600-character threshold lives as a named module-level constant in
    ``pipeline.parsers`` (the name itself is not pinned here - only that the
    module exposes a 600 constant)."""
    constants = [
        name
        for name, value in vars(parsers).items()
        if isinstance(value, int) and not isinstance(value, bool) and value == 600
    ]
    assert constants, (
        "pipeline.parsers must expose the 600-character truncation threshold "
        "as a named module-level constant"
    )
