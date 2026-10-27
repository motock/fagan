"""AUTH-08: the last three unguarded service-layer mutations.

``patch_story`` rewrites a dispatched story's brief, ``interrupt_story`` stops
a running agent and ``set_plan_role_config`` changes a plan's role
configuration. All three are ``PipelineService`` methods, so each one now asks
``pipeline.authorization.authorize`` for a decision before it mutates anything
(or even acquires the plan lock), and returns the refusal dict unchanged when
the policy says no.

The contract these tests pin down:

* ``local-operator`` -- the identity the unattended scheduler and daemon run
  as -- is still allowed all three, so nothing about the automated paths
  changes;
* a principal bound to ``ANONYMOUS`` (the dashboard chat-stream pass-through)
  is refused on all three, and the refusal is a no-op: the manifest bytes on
  disk are identical before and after;
* ``set_plan_role_config`` is gated because role config decides who may act
  later -- an operator who can write it could grant themselves any role the
  policy grants -- so a refusal must leave an existing ``role_config``
  untouched;
* ``PIPELINE_POLICY_ENFORCE=0`` is the operator's kill switch and works on all
  three as well;
* each refusal appends exactly one ``authorization_denied`` record to the
  plan's notification sidecar, naming its own action. ``authorize`` writes
  that record, so the service must not log a second one of its own.

``PIPELINE_POLICY_ENFORCE`` is read on every call (``enforcement_enabled`` ->
``os.environ``), not cached at import time, so ``monkeypatch.setenv`` is
observed at call time.

``plan_dir`` is the shared fixture from ``tests/unit/conftest.py``, which
points the ``pipeline.server``, ``pipeline.persistence`` and
``pipeline.concurrency`` PLAN_DIR bindings at one tmp directory -- the same
bindings the store and the denial sidecar read through.
"""

from __future__ import annotations

import json

import pytest

from app.identity import LOCAL_OPERATOR
from pipeline import server as service_mod
from pipeline.actor import ANONYMOUS, bind_principal

PLAN = "acme"
STORY = "S-1"
DENIAL_EVENT = "authorization_denied"
ACTIONS = ("story:patch", "story:interrupt", "config:write")

#: The provider/model pair the registry accepts for the ``review`` role; the
#: same pair ``tests/unit/test_config_write_overrides.py`` uses.
ROLE = "review"
PROVIDER = "claude"
MODEL = "sonnet"


def _manifest(**overrides):
    """A minimal manifest carrying one running, dispatched story."""
    manifest = {
        "repo_root": "/tmp/repo",
        "version": 3,
        "stories": {
            STORY: {
                "summary": "do the thing",
                "status": "running",
                "pid": 999999,
                "dispatched_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }
    manifest.update(overrides)
    return manifest


def _write_plan(plan_dir, **overrides):
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(_manifest(**overrides)))


def _manifest_path(plan_dir):
    return plan_dir / f"{PLAN}.manifest.json"


def _manifest_bytes(plan_dir):
    """The manifest exactly as it sits on disk -- the no-op assertion."""
    return _manifest_path(plan_dir).read_bytes()


def _read_manifest(plan_dir):
    return json.loads(_manifest_bytes(plan_dir))


def _denials(plan_dir):
    """Every ``authorization_denied`` record in the plan's JSONL sidecar."""
    path = plan_dir / f"{PLAN}.notifications.jsonl"
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    return [record for record in records if record.get("event") == DENIAL_EVENT]


def _new_denial_actions(plan_dir, before):
    """Which actions the denial records appended since *before* name."""
    named = []
    for record in _denials(plan_dir)[len(before) :]:
        rendered = json.dumps(record)
        named.extend(action for action in ACTIONS if action in rendered)
    return named


@pytest.fixture
def no_terminate(monkeypatch):
    """Stub the terminator so ``interrupt_story`` never signals a real pid."""
    monkeypatch.setattr(
        service_mod,
        "_terminate_and_checkpoint",
        lambda *args, **kwargs: "deadbeef",
    )


# ---------------------------------------------------------------------------
# The no-regression guarantee: local-operator still does all three
# ---------------------------------------------------------------------------
def test_local_operator_may_patch_a_story(plan_dir):
    _write_plan(plan_dir)

    with bind_principal(LOCAL_OPERATOR):
        result = service_mod.PipelineService().patch_story(
            PLAN, STORY, {"summary": "new summary"}
        )

    assert result.get("ok") is True


def test_local_operator_may_interrupt_a_story(plan_dir, no_terminate):
    _write_plan(plan_dir)

    with bind_principal(LOCAL_OPERATOR):
        result = service_mod.PipelineService().interrupt_story(PLAN, STORY)

    assert result.get("ok") is True


def test_local_operator_may_write_plan_role_config(plan_dir):
    _write_plan(plan_dir)

    with bind_principal(LOCAL_OPERATOR):
        result = service_mod.PipelineService().set_plan_role_config(
            PLAN, ROLE, provider=PROVIDER, model=MODEL
        )

    assert result.get("ok") is True


# ---------------------------------------------------------------------------
# A denied principal is refused
# ---------------------------------------------------------------------------
def test_anonymous_patch_is_refused(plan_dir):
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        result = service_mod.PipelineService().patch_story(
            PLAN, STORY, {"summary": "pwned"}
        )

    assert result.get("ok") is False


def test_anonymous_interrupt_is_refused(plan_dir, no_terminate):
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        result = service_mod.PipelineService().interrupt_story(PLAN, STORY)

    assert result.get("ok") is False


def test_anonymous_role_config_write_is_refused(plan_dir):
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        result = service_mod.PipelineService().set_plan_role_config(
            PLAN, ROLE, provider=PROVIDER, model=MODEL
        )

    assert result.get("ok") is False


# ---------------------------------------------------------------------------
# ... and the refusal is a no-op on disk
# ---------------------------------------------------------------------------
def test_refused_patch_leaves_the_manifest_bytes_unchanged(plan_dir):
    _write_plan(plan_dir)
    before = _manifest_bytes(plan_dir)

    with bind_principal(ANONYMOUS):
        service_mod.PipelineService().patch_story(PLAN, STORY, {"summary": "pwned"})

    assert _manifest_bytes(plan_dir) == before


def test_refused_interrupt_leaves_the_manifest_bytes_unchanged(plan_dir, no_terminate):
    _write_plan(plan_dir)
    before = _manifest_bytes(plan_dir)

    with bind_principal(ANONYMOUS):
        service_mod.PipelineService().interrupt_story(PLAN, STORY)

    assert _manifest_bytes(plan_dir) == before


def test_refused_role_config_write_leaves_the_manifest_bytes_unchanged(plan_dir):
    _write_plan(plan_dir)
    before = _manifest_bytes(plan_dir)

    with bind_principal(ANONYMOUS):
        service_mod.PipelineService().set_plan_role_config(
            PLAN, ROLE, provider=PROVIDER, model=MODEL
        )

    assert _manifest_bytes(plan_dir) == before


def test_refused_role_config_write_leaves_an_existing_role_config_alone(plan_dir):
    """Role config decides who may act later, so a refusal must not touch it."""
    existing = {ROLE: {"provider": PROVIDER, "model": "haiku"}}
    _write_plan(plan_dir, role_config=existing)

    with bind_principal(ANONYMOUS):
        service_mod.PipelineService().set_plan_role_config(
            PLAN, ROLE, provider=PROVIDER, model=MODEL
        )

    assert _read_manifest(plan_dir)["role_config"] == existing


# ---------------------------------------------------------------------------
# PIPELINE_POLICY_ENFORCE=0 is the operator's kill switch
# ---------------------------------------------------------------------------
def test_enforcement_disabled_allows_an_anonymous_patch(plan_dir, monkeypatch):
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        result = service_mod.PipelineService().patch_story(
            PLAN, STORY, {"summary": "new summary"}
        )

    assert result.get("ok") is True


def test_enforcement_disabled_allows_an_anonymous_interrupt(
    plan_dir, monkeypatch, no_terminate
):
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        result = service_mod.PipelineService().interrupt_story(PLAN, STORY)

    assert result.get("ok") is True


def test_enforcement_disabled_allows_an_anonymous_role_config_write(
    plan_dir, monkeypatch
):
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        result = service_mod.PipelineService().set_plan_role_config(
            PLAN, ROLE, provider=PROVIDER, model=MODEL
        )

    assert result.get("ok") is True


# ---------------------------------------------------------------------------
# Exactly one denial record per refusal, naming its own action
# ---------------------------------------------------------------------------
def test_refused_patch_records_one_story_patch_denial(plan_dir):
    _write_plan(plan_dir)
    before = _denials(plan_dir)

    with bind_principal(ANONYMOUS):
        service_mod.PipelineService().patch_story(PLAN, STORY, {"summary": "pwned"})

    assert _new_denial_actions(plan_dir, before) == ["story:patch"]


def test_refused_interrupt_records_one_story_interrupt_denial(plan_dir, no_terminate):
    _write_plan(plan_dir)
    before = _denials(plan_dir)

    with bind_principal(ANONYMOUS):
        service_mod.PipelineService().interrupt_story(PLAN, STORY)

    assert _new_denial_actions(plan_dir, before) == ["story:interrupt"]


def test_refused_role_config_write_records_one_config_write_denial(plan_dir):
    _write_plan(plan_dir)
    before = _denials(plan_dir)

    with bind_principal(ANONYMOUS):
        service_mod.PipelineService().set_plan_role_config(
            PLAN, ROLE, provider=PROVIDER, model=MODEL
        )

    assert _new_denial_actions(plan_dir, before) == ["config:write"]
