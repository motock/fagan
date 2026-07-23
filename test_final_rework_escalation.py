"""Tests for the plan-scoped ``final_rework_escalation`` manifest key.

P1 from the 2026-07-21 harness retro (``HARNESS_RETRO_TDDSPLIT_2026_07_21.md``):
"route polish-only rework to a stronger model". A local model's core fix can be
correct several review cycles in while the only remaining findings are small
and mechanical, yet the same (weaker) model keeps getting redispatched and the
story parks at the rework cap anyway. Rather than trying to classify
"polish vs capability" findings (hard, deferred), this ships the simpler,
general version: on request, ALWAYS give the story's LAST rework attempt to a
stronger, explicitly configured model instead of the model it has been running
on.

The feature is a new plan-level manifest key, a sibling of the existing
``local_model_fallback`` and ``role_config`` keys:

    "final_rework_escalation": {
        "enabled": true,
        "provider": "claude",
        "model": "opus"
    }

``enabled`` defaults OFF. Missing key, missing ``enabled``, or a falsy
``enabled`` all mean the feature does nothing (byte-for-byte unchanged existing
behavior - the most important regression bar). ``provider`` must be one of
``{"claude", "local", "ollama", "lmstudio", "mlx"}`` (the same value space
``story["backend"]`` already accepts). ``model`` is a raw string assigned
directly to ``story["model"]`` (no role_registry resolution).

In ``review_story``'s non-APPROVE branch, the ``else: story["status"] =
"changes_requested"`` arm is where a story gets redispatched for another rework
round. When ``attempts == rework_cap - 1`` at that point, THIS redispatch is
the story's last chance: if it also comes back REQUEST_CHANGES, ``attempts``
will equal ``rework_cap`` next cycle and the story parks (or auto-escalates).
This story routes that last-chance redispatch to the configured stronger
provider/model.

These tests are written to fail until the implementation exists. They are
fully standalone (own fixtures/helpers, no cross-file imports of test code) and
mirror the established one-file-per-guard convention. The helper/fixture shape
is replicated from ``test_review_story_stale_guard.py`` /
``test_review_story_lock_guard.py`` (read-only references); the
``_run_reviewer``-mock-to-force-a-verdict pattern comes from
``test_review_story_lock_guard.py``. ``p.review_story`` is called directly (it
delegates through the thin lock-guard wrapper to the real implementation), so
these tests exercise the real registered entry point end-to-end.
"""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
import pipeline.ticketing as pt


# ---------------------------------------------------------------------------
# Fixtures (mirror test_review_story_stale_guard.py / test_review_story_lock_guard.py
# so this file is fully standalone).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


# ---------------------------------------------------------------------------
# Helpers (replicated from the sibling guard test files - do not import
# across test files).
# ---------------------------------------------------------------------------

def _write_manifest_with_story(plan_dir, plan_name, story_key, story, *,
                               top_level=None):
    """Write a manifest containing a single story, plus optional top-level
    keys (e.g. final_rework_escalation, local_model_fallback, role_config)."""
    manifest = {"epics": {}, "stories": {story_key: story}}
    if top_level:
        manifest.update(top_level)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _read_manifest(plan_dir, plan_name):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )


def _make_story(plan_dir, *, status, worktree=None, **extra):
    """Build a minimal story dict in the given state."""
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt") if worktree is None else worktree,
        "risk": "low",
    }
    story.update(extra)
    return story


# A REQUEST_CHANGES verdict that carries genuine findings text (so it is NOT
# routed through the empty-findings inconclusive path - see _has_review_findings
# in pipeline/parsers.py). The body text survives the VERDICT-line strip, so
# this counts as a real rejection that consumes the rework budget.
_REQUEST_CHANGES_WITH_FINDINGS = (
    "The error path is untested and the SQL is injectable; add coverage and "
    "parameterize the query.\nVERDICT: REQUEST_CHANGES"
)


def _force_request_changes(monkeypatch, output=None):
    """Mock _run_reviewer to return a REQUEST_CHANGES verdict with real
    findings text, and _open_pr to fail loudly if ever called. Returns the
    list of reviewer calls so callers can assert dispatch happened."""
    reviewer_calls = []
    out = output if output is not None else _REQUEST_CHANGES_WITH_FINDINGS

    def _fake_reviewer(wt, br, backend_name=None, plan_role_config=None,
                       acceptance=None):
        reviewer_calls.append({"backend_name": backend_name})
        return out

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)

    def _boom_pr(*a, **k):
        raise AssertionError("PR must not be opened on REQUEST_CHANGES")

    monkeypatch.setattr(p, "_open_pr", _boom_pr)
    return reviewer_calls


def _disable_auto_escalation(monkeypatch):
    """Ensure _auto_escalation_enabled() returns False so the rework-cap
    branch parks (rather than escalating via _escalate_review_to_claude),
    keeping these tests deterministic and focused on final_rework_escalation."""
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)


# ---------------------------------------------------------------------------
# (1) Disabled by default: no final_rework_escalation key at all.
# ---------------------------------------------------------------------------

def test_final_rework_escalation_disabled_by_default(
    plan_dir, agents_dir, monkeypatch,
):
    """Regression bar: a manifest with NO ``final_rework_escalation`` key must
    behave byte-for-byte like today. A story one attempt below its rework cap,
    reviewer mocked to REQUEST_CHANGES: backend/model are unchanged from their
    pre-call values, status is changes_requested."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend="ollama", model="gpt-oss:20b",
        rework_attempts=1,  # this cycle's attempts -> 2 == rework_cap - 1
    )
    _write_manifest_with_story(plan_dir, "fre", "S1", story)

    _force_request_changes(monkeypatch)

    result = p.review_story("fre", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["rework_attempts"] == 2
    # Regression bar: backend/model untouched.
    assert on_disk["backend"] == "ollama", (
        f"backend must be unchanged when final_rework_escalation is absent; "
        f"got {on_disk.get('backend')!r}"
    )
    assert on_disk["model"] == "gpt-oss:20b", (
        f"model must be unchanged when final_rework_escalation is absent; "
        f"got {on_disk.get('model')!r}"
    )


# ---------------------------------------------------------------------------
# (2) Disabled when enabled is explicitly false.
# ---------------------------------------------------------------------------

def test_final_rework_escalation_disabled_when_enabled_false(
    plan_dir, agents_dir, monkeypatch,
):
    """Same as (1) but with ``final_rework_escalation`` present and
    ``enabled: false`` - still a no-op. Proves a present-but-disabled block
    does not change behavior."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend="ollama", model="gpt-oss:20b",
        rework_attempts=1,  # this cycle's attempts -> 2 == rework_cap - 1
    )
    _write_manifest_with_story(
        plan_dir, "fre", "S1", story,
        top_level={"final_rework_escalation": {
            "enabled": False, "provider": "claude", "model": "opus",
        }},
    )

    _force_request_changes(monkeypatch)

    result = p.review_story("fre", "S1")

    assert result["status"] == "changes_requested"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["backend"] == "ollama", (
        f"backend must be unchanged when enabled is false; "
        f"got {on_disk.get('backend')!r}"
    )
    assert on_disk["model"] == "gpt-oss:20b", (
        f"model must be unchanged when enabled is false; "
        f"got {on_disk.get('model')!r}"
    )


# ---------------------------------------------------------------------------
# (3) Fires on the last attempt.
# ---------------------------------------------------------------------------

def test_final_rework_escalation_fires_on_last_attempt(
    plan_dir, agents_dir, monkeypatch,
):
    """``final_rework_escalation`` enabled, ``REWORK_MAX_ATTEMPTS`` 3, story's
    ``rework_attempts`` at 1 so this cycle's ``attempts`` becomes 2 ==
    ``rework_cap - 1`` (the last-chance redispatch). REQUEST_CHANGES verdict.
    Assert backend/model are switched to the configured provider/model and
    status is changes_requested."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend="ollama", model="gpt-oss:20b",
        rework_attempts=1,  # this cycle's attempts -> 2 == rework_cap - 1
    )
    _write_manifest_with_story(
        plan_dir, "fre", "S1", story,
        top_level={"final_rework_escalation": {
            "enabled": True, "provider": "claude", "model": "opus",
        }},
    )

    notify_calls = []
    monkeypatch.setattr(
        p, "_notify_user",
        lambda plan_name, msg: notify_calls.append((plan_name, msg)),
    )

    _force_request_changes(monkeypatch)

    result = p.review_story("fre", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    assert result["status"] == "changes_requested"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["status"] == "changes_requested"
    assert on_disk["rework_attempts"] == 2
    assert on_disk["backend"] == "claude", (
        f"backend must be switched to the configured provider on the last "
        f"rework attempt; got {on_disk.get('backend')!r}"
    )
    assert on_disk["model"] == "opus", (
        f"model must be switched to the configured model on the last rework "
        f"attempt; got {on_disk.get('model')!r}"
    )
    # A user notification describing the escalation is emitted.
    assert any(
        "fre" in pn and "escalating" in msg.lower() and "claude" in msg.lower()
        and "opus" in msg.lower()
        for pn, msg in notify_calls
    ), f"expected an escalation notification, got {notify_calls!r}"


# ---------------------------------------------------------------------------
# (4) Does NOT fire before the last attempt.
# ---------------------------------------------------------------------------

def test_final_rework_escalation_does_not_fire_before_last_attempt(
    plan_dir, agents_dir, monkeypatch,
):
    """Same config as (3), but ``rework_attempts`` at 0 so this cycle's
    ``attempts`` becomes 1, which is NOT ``rework_cap - 1 = 2``. The escalation
    must NOT fire - backend/model unchanged. This is an ordinary mid-budget
    rework round, not the last chance."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend="ollama", model="gpt-oss:20b",
        rework_attempts=0,  # this cycle's attempts -> 1, not rework_cap - 1
    )
    _write_manifest_with_story(
        plan_dir, "fre", "S1", story,
        top_level={"final_rework_escalation": {
            "enabled": True, "provider": "claude", "model": "opus",
        }},
    )

    _force_request_changes(monkeypatch)

    result = p.review_story("fre", "S1")

    assert result["status"] == "changes_requested"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["rework_attempts"] == 1
    assert on_disk["backend"] == "ollama", (
        f"backend must be unchanged before the last attempt; "
        f"got {on_disk.get('backend')!r}"
    )
    assert on_disk["model"] == "gpt-oss:20b", (
        f"model must be unchanged before the last attempt; "
        f"got {on_disk.get('model')!r}"
    )


# ---------------------------------------------------------------------------
# (5) No effect on an oracle cap of one (rework_cap <= 1).
# ---------------------------------------------------------------------------

def test_final_rework_escalation_noop_on_oracle_cap_of_one(
    plan_dir, agents_dir, monkeypatch,
):
    """EDGE CASE: an acceptance-bearing story's ``rework_cap`` resolves to
    ``REWORK_MAX_ATTEMPTS_ORACLE`` (default 1). The very first REQUEST_CHANGES
    already has ``attempts (1) >= rework_cap (1)``, so it takes the
    park/auto-escalate branch on cycle 1 and NEVER reaches the ``else`` branch
    this feature modifies. ``final_rework_escalation`` therefore has no effect
    on 1-attempt-budget stories - confirm with a test rather than special-casing
    in code.

    With auto-escalation disabled, the story parks exactly as it would with
    ``final_rework_escalation`` entirely absent."""
    _disable_auto_escalation(monkeypatch)
    # Default REWORK_MAX_ATTEMPTS_ORACLE is 1; leave it at the default by not
    # patching it (but assert the assumption so the test is self-documenting).
    assert p.REWORK_MAX_ATTEMPTS_ORACLE == 1, (
        "this test assumes the default oracle cap of 1; if the default changed, "
        "patch it explicitly here"
    )

    story = _make_story(
        plan_dir, status="tests_passed",
        backend="ollama", model="gpt-oss:20b",
        acceptance=[{"path": "test_acceptance.py"}],
        # No rework_attempts -> this cycle's attempts becomes 1 == rework_cap.
    )
    _write_manifest_with_story(
        plan_dir, "fre", "S1", story,
        top_level={"final_rework_escalation": {
            "enabled": True, "provider": "claude", "model": "opus",
        }},
    )
    # The acceptance-oracle re-verification path runs on REQUEST_CHANGES for
    # acceptance-bearing stories; stub it so no real subprocess is needed.
    monkeypatch.setattr(p, "_reverify_acceptance",
                        lambda s, wt: {"state": "fail", "error": ""})

    _force_request_changes(monkeypatch)

    result = p.review_story("fre", "S1")

    assert result["verdict"] == "REQUEST_CHANGES"
    on_disk = _read_story(plan_dir, "fre", "S1")
    # The story parks (rework budget exhausted on the first cycle), exactly as
    # it would with final_rework_escalation absent - the feature never reached
    # the else branch.
    assert on_disk["status"] == "parked", (
        f"oracle-cap-of-one story must park on the first REQUEST_CHANGES; "
        f"got status={on_disk.get('status')!r}"
    )
    assert on_disk["rework_attempts"] == 1
    # backend/model untouched: the escalation branch was never entered.
    assert on_disk["backend"] == "ollama", (
        f"backend must be unchanged when the budget never reaches the else "
        f"branch; got {on_disk.get('backend')!r}"
    )
    assert on_disk["model"] == "gpt-oss:20b", (
        f"model must be unchanged when the budget never reaches the else "
        f"branch; got {on_disk.get('model')!r}"
    )


# ---------------------------------------------------------------------------
# (6) Unknown provider is silently ignored (fail closed, never raise).
# ---------------------------------------------------------------------------

def test_final_rework_escalation_unknown_provider_is_ignored(
    plan_dir, agents_dir, monkeypatch,
):
    """``final_rework_escalation`` enabled but with an unrecognized
    ``provider`` string. At the last-attempt boundary, the escalation must be
    silently skipped (backend left unchanged) and ``review_story`` must NOT
    raise - a plan-authoring typo must never break a live pipeline tick."""
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend="ollama", model="gpt-oss:20b",
        rework_attempts=1,  # this cycle's attempts -> 2 == rework_cap - 1
    )
    _write_manifest_with_story(
        plan_dir, "fre", "S1", story,
        top_level={"final_rework_escalation": {
            "enabled": True, "provider": "not-a-real-provider", "model": "opus",
        }},
    )

    _force_request_changes(monkeypatch)

    # Must not raise.
    result = p.review_story("fre", "S1")

    assert result["ok"] is True
    assert result["status"] == "changes_requested"
    on_disk = _read_story(plan_dir, "fre", "S1")
    assert on_disk["backend"] != "not-a-real-provider", (
        f"an unrecognized provider must never be written to story['backend']; "
        f"got {on_disk.get('backend')!r}"
    )
    assert on_disk["backend"] == "ollama", (
        f"backend must be left unchanged on an unrecognized provider; "
        f"got {on_disk.get('backend')!r}"
    )
    assert on_disk["model"] == "gpt-oss:20b", (
        f"model must be left unchanged on an unrecognized provider; "
        f"got {on_disk.get('model')!r}"
    )


# ---------------------------------------------------------------------------
# (7) Survives ingest_plan re-ingest (top-level key carried forward).
# ---------------------------------------------------------------------------

def _fake_plane(method, path, **kwargs):
    """Minimal Plane stub mirroring test_pipeline_mcp_server.py's helper so
    ingest_plan runs without a real ticketing backend."""
    if path.endswith("/states/"):
        return {"results": [
            {"group": "backlog", "id": "st-backlog"},
            {"group": "started", "id": "st-started"},
            {"group": "completed", "id": "st-done"},
        ]}
    if path.endswith("/labels/") and method == "GET":
        return {"results": []}
    if path.endswith("/labels/") and method == "POST":
        return {"id": "label-1"}
    if path.endswith("/epics/") and method == "POST":
        return {"id": "epic-1"}
    if path.endswith("/work-items/") and method == "POST":
        return {"id": "issue-1"}
    return {}


def _authored_story(**over):
    base = {"summary": "Do the thing", "agent_instructions": "Build it with tests."}
    base.update(over)
    return base


def test_final_rework_escalation_survives_ingest_replan(
    plan_dir, agents_dir, monkeypatch, tmp_path,
):
    """``ingest_plan``'s merge-preserves-top-level-keys behavior must carry
    ``final_rework_escalation`` forward untouched across a re-ingest (mirroring
    the existing ``local_model_fallback`` coverage in
    ``test_pipeline_mcp_server.py``). A plan re-ingested after adding a new
    story must still carry the key, byte-for-byte unchanged in value."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)

    fre_block = {"enabled": True, "provider": "claude", "model": "opus"}
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [
            _authored_story(key="S1"),
        ]}],
    }
    (plan_dir / "freplan.json").write_text(json.dumps(plan))
    p.ingest_plan("freplan")

    # Inject the top-level key into the manifest, simulating an operator
    # enabling the feature after the initial ingest.
    manifest = _read_manifest(plan_dir, "freplan")
    manifest["final_rework_escalation"] = dict(fre_block)
    (plan_dir / "freplan.manifest.json").write_text(json.dumps(manifest))

    # Re-ingest (e.g. a new story was added to the plan). The key must survive.
    result = p.ingest_plan("freplan")

    assert result["ok"] is True
    merged = _read_manifest(plan_dir, "freplan")
    assert "final_rework_escalation" in merged, (
        "final_rework_escalation must be carried forward across a re-ingest"
    )
    assert merged["final_rework_escalation"] == fre_block, (
        f"final_rework_escalation must be preserved untouched across "
        f"re-ingest; got {merged.get('final_rework_escalation')!r}"
    )


def test_final_rework_escalation_null_value(monkeypatch, plan_dir, agents_dir):
    _disable_auto_escalation(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend="ollama", model="gpt-oss:20b",
        rework_attempts=1,
    )
    _write_manifest_with_story(
        plan_dir, "fre", "S1", story,
        top_level={"final_rework_escalation": None},
    )

    _force_request_changes(monkeypatch)

    result = p.review_story("fre", "S1")

    assert result["ok"] is True
    on_disk = _read_story(plan_dir, "fre", "S1")
    # backend/model unchanged
    assert on_disk["backend"] == "ollama"
    assert on_disk["model"] == "gpt-oss:20b"
