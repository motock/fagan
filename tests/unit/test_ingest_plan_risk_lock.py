"""Unit tests for the ingest_plan risk-lock fix (pipeline/server.py only).

A story that has already moved past ``status == "todo"`` (dispatched,
reviewed, parked at pr_open, or done) must NOT have its ``risk`` field
silently overwritten by a re-ingest of the same plan key with a lower
risk value. This is the root-cause closure of the risk-downgrade bypass
that ``chat-security-hardening`` closed for the chat ``patch_story``
tool: ``_ingest_plan_impl``'s re-ingest merge re-applied EVERY field in
``_INGEST_AUTHORED_STORY_FIELDS`` - including ``risk`` - onto an EXISTING
story on every re-ingest, even with ``overwrite=False``. ``_merge_decision``
reads ``risk`` live off the manifest at merge-decision time, so silently
downgrading it via ``save_plan`` + ``ingest_plan`` reopens the exact same
bypass for a story already parked at ``pr_open`` with an APPROVE verdict.

These tests follow the exact fixture pattern of
``test_ingest_plan_reingest_refreshes_authored_fields_preserves_runtime_status``
in ``tests/unit/test_pipeline_mcp_server.py``: import
``from pipeline import server as p`` and ``from pipeline import ticketing as pt``,
use a local ``plan_dir`` pytest fixture that points ``p.PLAN_DIR`` at a
``tmp_path`` subdirectory, and monkeypatch ``pt.plane_request`` to a
function that raises ``AssertionError`` if called (Plane must never be
invoked - PLANE_* env is unset in tests, so the NullTicketProvider path is
what's under test, and a story's own ``key`` field becomes its manifest
key directly).
"""
import json

import pytest

from pipeline import server as p
from pipeline import ticketing as pt


def _explode_plane(*a, **kw):
    raise AssertionError("Plane should not be called - PLANE_* env is unset in tests")


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _write_manifest(plan_dir, plan_name, manifest):
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


# ---------------------------------------------------------------------------
# 1. The headline fix: a dispatched story's risk is locked on re-ingest.
# ---------------------------------------------------------------------------
def test_reingest_does_not_overwrite_risk_on_a_dispatched_story(
    plan_dir, monkeypatch, tmp_path,
):
    """A story that has progressed past ``todo`` keeps its existing risk on
    re-ingest, and its genuine runtime state (status, review_verdict) is left
    untouched."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "high",
            }],
        }],
    }
    (plan_dir / "lock.json").write_text(json.dumps(plan))
    p.ingest_plan("lock")

    # Simulate real pipeline progress: dispatched, reviewed, approved, and
    # parked at pr_open awaiting human merge approval.
    manifest = _read_manifest(plan_dir, "lock")
    manifest["stories"]["S1"]["status"] = "pr_open"
    manifest["stories"]["S1"]["review_verdict"] = "APPROVE"
    _write_manifest(plan_dir, "lock", manifest)

    # A caller re-saves the SAME key with a downgraded risk and re-ingests.
    plan["epics"][0]["stories"][0]["risk"] = "low"
    (plan_dir / "lock.json").write_text(json.dumps(plan))
    result = p.ingest_plan("lock")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "lock")
    # The risk must NOT have been downgraded.
    assert merged["stories"]["S1"]["risk"] == "high"
    # The genuine runtime progress must be untouched.
    assert merged["stories"]["S1"]["status"] == "pr_open"
    assert merged["stories"]["S1"]["review_verdict"] == "APPROVE"


# ---------------------------------------------------------------------------
# 2. Negative/boundary: a story still at "todo" CAN still have its risk
#    edited on re-ingest - the lock only applies once a story has left "todo".
# ---------------------------------------------------------------------------
def test_reingest_still_allows_risk_edit_when_story_is_still_todo(
    plan_dir, monkeypatch, tmp_path,
):
    """A freshly ingested story that has never been touched (still ``todo``)
    can legitimately have its risk corrected by the plan author on re-ingest.
    This proves the fix only locks risk once a story has left ``todo``, not
    always."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "low",
            }],
        }],
    }
    (plan_dir / "todo.json").write_text(json.dumps(plan))
    p.ingest_plan("todo")

    # Story is still "todo" (never dispatched) - re-ingest with a higher risk.
    plan["epics"][0]["stories"][0]["risk"] = "high"
    (plan_dir / "todo.json").write_text(json.dumps(plan))
    result = p.ingest_plan("todo")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "todo")
    assert merged["stories"]["S1"]["risk"] == "high"
    # And the status is still the freshly-ingested "todo".
    assert merged["stories"]["S1"]["status"] == "todo"


# ---------------------------------------------------------------------------
# 3. Boundary: a BRAND NEW story key introduced in a re-ingest call (not
#    previously present in the manifest) gets its risk applied normally
#    regardless of any other story's status - the lock only applies to a key
#    that already exists in the prior manifest.
# ---------------------------------------------------------------------------
def test_reingest_new_story_key_gets_risk_applied_normally(
    plan_dir, monkeypatch, tmp_path,
):
    """A brand-new key introduced in a re-ingest (absent from the prior
    manifest) must get its risk applied normally, even if a sibling story in
    the same plan has already been dispatched. The lock only applies to a key
    that already exists in the prior manifest."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Old story",
                "key": "S1",
                "risk": "high",
            }],
        }],
    }
    (plan_dir / "mix.json").write_text(json.dumps(plan))
    p.ingest_plan("mix")

    # Progress the existing story past "todo" so it is dispatched.
    manifest = _read_manifest(plan_dir, "mix")
    manifest["stories"]["S1"]["status"] = "pr_open"
    manifest["stories"]["S1"]["review_verdict"] = "APPROVE"
    _write_manifest(plan_dir, "mix", manifest)

    # Re-ingest: keep S1 (with a downgraded risk, which must be locked) AND
    # introduce a brand-new S2 with its own risk, which must be applied.
    plan["epics"][0]["stories"] = [
        {"summary": "Old story", "key": "S1", "risk": "low"},
        {"summary": "Brand new story", "key": "S2", "risk": "medium"},
    ]
    (plan_dir / "mix.json").write_text(json.dumps(plan))
    result = p.ingest_plan("mix")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "mix")
    # Existing dispatched S1 keeps its locked risk.
    assert merged["stories"]["S1"]["risk"] == "high"
    assert merged["stories"]["S1"]["status"] == "pr_open"
    assert merged["stories"]["S1"]["review_verdict"] == "APPROVE"
    # Brand-new S2 gets its risk applied normally.
    assert merged["stories"]["S2"]["risk"] == "medium"
    assert merged["stories"]["S2"]["status"] == "todo"


# ---------------------------------------------------------------------------
# 4. Boundary: the lock applies to EVERY non-"todo" status, not just
#    "pr_open". A story at "in_progress" must also be locked.
# ---------------------------------------------------------------------------
def test_reingest_locks_risk_for_in_progress_story(
    plan_dir, monkeypatch, tmp_path,
):
    """The lock is keyed on ``status != "todo"``, so any non-"todo" status
    (here ``in_progress``) must lock risk, not just ``pr_open``."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "high",
            }],
        }],
    }
    (plan_dir / "prog.json").write_text(json.dumps(plan))
    p.ingest_plan("prog")

    manifest = _read_manifest(plan_dir, "prog")
    manifest["stories"]["S1"]["status"] = "in_progress"
    _write_manifest(plan_dir, "prog", manifest)

    plan["epics"][0]["stories"][0]["risk"] = "low"
    (plan_dir / "prog.json").write_text(json.dumps(plan))
    result = p.ingest_plan("prog")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "prog")
    assert merged["stories"]["S1"]["risk"] == "high"
    assert merged["stories"]["S1"]["status"] == "in_progress"


# ---------------------------------------------------------------------------
# 5. Boundary: the lock applies to "done" too (the last non-"todo" status).
# ---------------------------------------------------------------------------
def test_reingest_locks_risk_for_done_story(
    plan_dir, monkeypatch, tmp_path,
):
    """A story at ``done`` is also past ``todo`` and must have its risk
    locked on re-ingest."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "high",
            }],
        }],
    }
    (plan_dir / "done.json").write_text(json.dumps(plan))
    p.ingest_plan("done")

    manifest = _read_manifest(plan_dir, "done")
    manifest["stories"]["S1"]["status"] = "done"
    _write_manifest(plan_dir, "done", manifest)

    plan["epics"][0]["stories"][0]["risk"] = "low"
    (plan_dir / "done.json").write_text(json.dumps(plan))
    result = p.ingest_plan("done")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "done")
    assert merged["stories"]["S1"]["risk"] == "high"
    assert merged["stories"]["S1"]["status"] == "done"


# ---------------------------------------------------------------------------
# 6. The lock is risk-specific: other authored fields (e.g. agent_instructions,
#    persona, backend) are STILL refreshed on re-ingest for a dispatched
#    story. Only ``risk`` is locked. This guards against an over-broad fix
#    that locks every authored field.
# ---------------------------------------------------------------------------
def test_reingest_still_refreshes_other_authored_fields_on_dispatched_story(
    plan_dir, monkeypatch, tmp_path,
):
    """Only ``risk`` is locked for a dispatched story; other authored fields
    in ``_INGEST_AUTHORED_STORY_FIELDS`` (agent_instructions, persona,
    backend) must still be refreshed on re-ingest, exactly as before."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "high",
                "agent_instructions": "Build v1.",
                "persona": "software-engineer",
                "backend": "claude",
            }],
        }],
    }
    (plan_dir / "fields.json").write_text(json.dumps(plan))
    p.ingest_plan("fields")

    manifest = _read_manifest(plan_dir, "fields")
    manifest["stories"]["S1"]["status"] = "pr_open"
    manifest["stories"]["S1"]["review_verdict"] = "APPROVE"
    _write_manifest(plan_dir, "fields", manifest)

    plan["epics"][0]["stories"][0].update({
        "risk": "low",  # must be locked
        "agent_instructions": "Build v2, with edge cases.",  # must refresh
        "persona": "code-reviewer",  # must refresh
        "backend": "ollama",  # must refresh
    })
    (plan_dir / "fields.json").write_text(json.dumps(plan))
    result = p.ingest_plan("fields")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "fields")
    # risk locked.
    assert merged["stories"]["S1"]["risk"] == "high"
    # other authored fields refreshed.
    assert merged["stories"]["S1"]["agent_instructions"] == "Build v2, with edge cases."
    assert merged["stories"]["S1"]["persona"] == "code-reviewer"
    assert merged["stories"]["S1"]["backend"] == "ollama"
    # runtime state untouched.
    assert merged["stories"]["S1"]["status"] == "pr_open"
    assert merged["stories"]["S1"]["review_verdict"] == "APPROVE"


# ---------------------------------------------------------------------------
# 7. The lock is risk-specific the other way: a dispatched story whose
#    re-ingest plan does NOT change risk keeps the same risk value (no
#    accidental mutation), and the lock does not corrupt the field when the
#    incoming value equals the existing one.
# ---------------------------------------------------------------------------
def test_reingest_keeps_risk_unchanged_when_incoming_risk_equals_existing(
    plan_dir, monkeypatch, tmp_path,
):
    """When the incoming risk equals the existing risk on a dispatched story,
    the value is unchanged (the lock is a no-op, not a corruption)."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "high",
            }],
        }],
    }
    (plan_dir / "eq.json").write_text(json.dumps(plan))
    p.ingest_plan("eq")

    manifest = _read_manifest(plan_dir, "eq")
    manifest["stories"]["S1"]["status"] = "pr_open"
    _write_manifest(plan_dir, "eq", manifest)

    # Re-ingest with the SAME risk (no change). Must stay "high".
    (plan_dir / "eq.json").write_text(json.dumps(plan))
    result = p.ingest_plan("eq")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "eq")
    assert merged["stories"]["S1"]["risk"] == "high"
    assert merged["stories"]["S1"]["status"] == "pr_open"


# ---------------------------------------------------------------------------
# 8. The fix must not change ``_INGEST_AUTHORED_STORY_FIELDS`` itself: ``risk``
#    must still be a member (the lock is applied at merge time, not by
#    removing the field from the authored set). This guards against a
#    wrong fix that simply drops ``risk`` from the tuple.
# ---------------------------------------------------------------------------
def test_risk_remains_in_ingest_authored_story_fields():
    """The fix must NOT remove ``risk`` from ``_INGEST_AUTHORED_STORY_FIELDS``;
    it is applied at merge time by skipping the field for dispatched stories,
    not by dropping it from the authored set (which would break risk edits on
    todo stories)."""
    assert "risk" in p._INGEST_AUTHORED_STORY_FIELDS


# ---------------------------------------------------------------------------
# 9. The fix must not add ``risk`` to the set of runtime-preserved fields by
#    removing it from the authored set. Concretely: the authored set must
#    still contain every original member (no field was dropped as a side
#    effect of this fix).
# ---------------------------------------------------------------------------
def test_ingest_authored_story_fields_membership_unchanged():
    """No authored field was dropped from ``_INGEST_AUTHORED_STORY_FIELDS`` as
    a side effect of the risk-lock fix. Asserts membership of each original
    field (not exact tuple equality, so sibling stories can extend the set)."""
    for field in (
        "summary",
        "agent_instructions",
        "dependencies",
        "persona",
        "model",
        "acceptance",
        "risk",
        "backend",
        "tdd_split",
    ):
        assert field in p._INGEST_AUTHORED_STORY_FIELDS, (
            f"{field!r} must remain in _INGEST_AUTHORED_STORY_FIELDS"
        )


# ---------------------------------------------------------------------------
# 10. The merge logic must use a ``status != "todo"`` dispatch check (the
#     exact anchor described in the brief), not some other condition. We
#     assert the implementation reads the prior story's ``status`` to decide
#     whether to lock risk - by exercising a story whose status is missing
#     entirely. A missing status is not "todo", so risk must be locked.
# ---------------------------------------------------------------------------
def test_reingest_locks_risk_when_status_is_missing(
    plan_dir, monkeypatch, tmp_path,
):
    """If a prior story's ``status`` key is missing (falsy / not "todo"), risk
    must be locked - the dispatch check is ``status != "todo"``, and a missing
    status is not equal to "todo"."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [{
                "summary": "Do the thing",
                "key": "S1",
                "risk": "high",
            }],
        }],
    }
    (plan_dir / "miss.json").write_text(json.dumps(plan))
    p.ingest_plan("miss")

    # Corrupt the manifest: drop the status field entirely (simulate a
    # hand-edited / partially-progressed manifest). status is not "todo".
    manifest = _read_manifest(plan_dir, "miss")
    manifest["stories"]["S1"].pop("status", None)
    _write_manifest(plan_dir, "miss", manifest)

    plan["epics"][0]["stories"][0]["risk"] = "low"
    (plan_dir / "miss.json").write_text(json.dumps(plan))
    result = p.ingest_plan("miss")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "miss")
    # status missing is not "todo" -> risk locked.
    assert merged["stories"]["S1"]["risk"] == "high"