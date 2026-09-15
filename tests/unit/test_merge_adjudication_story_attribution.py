"""MERGEATTR-1: a high-risk merge adjudication record names the story it ruled on.

``merge._adjudicate_high_risk_merge`` builds its decisions-log record with
``"story_key": story.get("key")``. No story in any manifest carries a ``key``
or ``story_key`` field - a story's identity is its dict key in
``manifest["stories"]``, and nothing stamps that onto the story dict - so the
expression is ALWAYS ``None`` and every high-risk merge adjudication record is
unattributed. Live case: two records written 2026-09-15T14:08:42Z and
14:08:55Z ruled ``proceed`` on plan ``chat-worktree-apply`` (merging PRs #760
and #761) and cannot be told apart except by reading their rationale prose.

The story key IS known at every production call site; it is simply never
threaded through. It is threaded with the SAME mechanism the file already uses
for the sibling problem (``plan_name``): a module-level ``ContextVar`` bound by
a context manager, because the gate's one-argument call shape is load-bearing
(several long-lived one-argument test doubles call or patch
``_merge_decision``, so a second positional argument is not an option).

The contract these tests pin down:

* ``merge.merge_adjudication_story(story_key)`` binds the key for the gate call
  and resets it on exit - including when the body raises - so a stale key from
  a previous loop iteration can never attribute the next story's ruling.
* ``advance._readjudicate_parked_merge_hold`` and ``advance._adjudicate_merges``
  bind the manifest dict key around their gate calls, so BOTH records the gate
  can build (the parsed-ruling record and the unparseable-reply fail-closed
  record) carry it.
* Both warning lines name the story rather than ``None``.
* An unbound legacy caller keeps today's behaviour exactly: the fallback chain
  ends at ``None``, never a ``"?"`` sentinel.

The integration-grade tests drive the REAL ``advance`` entry points (a test
that calls ``_adjudicate_high_risk_merge`` directly would still pass if the
context manager were added but never bound at the call sites), with the real
``persistence._append_decision`` writer running against a tmp ``PLAN_DIR`` so
the assertion is about the record that actually lands on disk.
"""

# ruff: noqa: I001
# Import order below is deliberate, not disorganized: ``pipeline.server``
# transitively imports advance/ci/merge at module load, so importing it before
# ``pipeline.advance`` keeps that submodule import resolving against an
# already-initialized module (the ordering test_merge_overlord_adjudication.py
# documents). isort's alphabetical sort would put ``pipeline.advance`` first
# and reintroduce the circular import this ordering avoids.
import json
import logging

import pytest

from pipeline import server as server_mod
from pipeline import ci as ci_mod
from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod

# The module that owns the production context bindings guarded by the
# integration-grade tests below.
from pipeline import advance as advance_mod

HOLD_REASON = "high risk held for human review"
PLAN = "MERGEATTR-PLAN"
PROCEED_REPLY = "RULING: proceed\nRATIONALE: ok"
PARK_REPLY = "RULING: park\nRATIONALE: not yet"
UNPARSEABLE_REPLY = "I would probably merge this one, but it is your call."


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _plan_decisions(plan_dir, plan_name):
    """The records in ``plan_name``'s own decisions log (``[]`` if absent)."""
    path = plan_dir / f"{plan_name}.decisions.json"
    return json.loads(path.read_text()) if path.exists() else []


def _records(plan_dir):
    return _plan_decisions(plan_dir, PLAN)


def _full_autonomy(monkeypatch):
    """Full autonomy + a low threshold, so a high-risk story is adjudicated."""
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setattr(
        persistence_mod,
        "_plan_role_config",
        lambda plan_name: {"role": "overlord", "model": "opus"},
    )


def _stub_overlord(monkeypatch, reply=None, exc=None):
    def fake_invoke(prompt, plan_role_config=None):
        if exc is not None:
            raise exc
        return reply

    monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)


def _stub_ci_gather(monkeypatch, value):
    """Stub the NON-polling gather ``_readjudicate_parked_merge_hold`` uses.

    It lazy-imports ``_ci_status_once`` from ``pipeline/ci.py``, so the stub
    must land there (never on ``pipeline.server``).
    """
    monkeypatch.setattr(ci_mod, "_ci_status_once", lambda branch, sha="": value)


def _parked_story(**overrides):
    """A production-shaped parked high-risk hold: no ``key``/``story_key``."""
    story = {
        "status": "parked",
        "parked_reason": HOLD_REASON,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "pr_url": "https://github.com/example/repo/pull/1",
        "merge_park_evidence": {"pr_checks": None},
        "worktree": "",
    }
    story.update(overrides)
    return story


# --------------------------------------------------------------------------
# 1. the integration-grade guard: the REAL re-adjudication path
# --------------------------------------------------------------------------


def test_readjudication_record_names_the_story(plan_dir, monkeypatch):
    """``_readjudicate_parked_merge_hold`` binds the key it was handed.

    The CI gather returns a state that DIFFERS from the recorded snapshot
    (``{"pr_checks": "success"}`` vs ``{"pr_checks": None}``), so the
    re-adjudication actually proceeds and a record is appended. Stubbing the
    gather to the snapshot value would short-circuit on the flap guard and
    append nothing, failing this test for the wrong reason.
    """
    _full_autonomy(monkeypatch)
    _stub_overlord(monkeypatch, PROCEED_REPLY)
    _stub_ci_gather(monkeypatch, {"pr_checks": "success"})
    story = _parked_story()

    decision = advance_mod._readjudicate_parked_merge_hold(PLAN, "SOME-KEY", story)

    assert decision == {"action": "merge", "reason": "overlord ruled proceed"}
    records = _records(plan_dir)
    assert len(records) == 1, "the re-adjudication must append exactly one record"
    assert records[0]["story_key"] == "SOME-KEY"


# --------------------------------------------------------------------------
# 2. the ordinary pr_open path through ``_adjudicate_merges``
# --------------------------------------------------------------------------


def test_ordinary_pr_open_path_record_names_the_manifest_key(plan_dir, monkeypatch):
    """The pr_open gate call binds the story's manifest dict key.

    ``_readjudicate_parked_merge_hold`` returns ``None`` for a pr_open story,
    so the only binding that can attribute this record is the one around the
    ``if decision is None:`` gate call.
    """
    _full_autonomy(monkeypatch)
    _stub_overlord(monkeypatch, PARK_REPLY)
    monkeypatch.setattr(server_mod, "_notify_user", lambda *a, **k: None, raising=False)
    key = "MERGEATTR-ORDINARY"
    story = {
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        # Truthy, so ``_populate_pr_checks_once`` returns early instead of
        # probing a branch for a story that has no worktree.
        "pr_checks": {"ci": "pass"},
        "worktree": "",
    }
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {key: story}}, indent=2)
    )
    summary = {"parked": [], "notify": [], "failed": [], "merged": [], "ci_pending": []}

    advance_mod._adjudicate_merges(PLAN, summary)

    records = _records(plan_dir)
    assert len(records) == 1, "the gate must append exactly one record"
    assert records[0]["story_key"] == key
    assert summary["parked"] == [key]


# --------------------------------------------------------------------------
# 3. compat: an unbound legacy caller keeps today's behaviour exactly
# --------------------------------------------------------------------------


def test_direct_call_without_binding_keeps_story_key_none(plan_dir, monkeypatch):
    """No binding and no ``key``/``story_key`` field -> ``None``, not ``"?"``."""
    _full_autonomy(monkeypatch)
    _stub_overlord(monkeypatch, PROCEED_REPLY)
    monkeypatch.setattr(
        server_mod, "_ci_status_once", lambda branch, sha="": None, raising=False
    )
    captured = []
    monkeypatch.setattr(
        persistence_mod,
        "_append_decision",
        lambda plan_name, record: captured.append((plan_name, record)),
    )
    story = _parked_story()
    story.pop("merge_park_evidence")

    assert merge_mod._adjudication_story_key.get() is None
    merge_mod._merge_decision(story)

    assert len(captured) == 1
    assert captured[0][1]["story_key"] is None


# --------------------------------------------------------------------------
# 4. the fail-closed branch builds its OWN record and needs its own assertion
# --------------------------------------------------------------------------


def test_unparseable_reply_records_fail_closed_with_the_story_key(
    plan_dir, monkeypatch
):
    _full_autonomy(monkeypatch)
    _stub_overlord(monkeypatch, UNPARSEABLE_REPLY)
    _stub_ci_gather(monkeypatch, {"pr_checks": "success"})
    story = _parked_story()

    decision = advance_mod._readjudicate_parked_merge_hold(PLAN, "SOME-KEY", story)

    assert decision == {"action": "park", "reason": HOLD_REASON}
    records = _records(plan_dir)
    assert len(records) == 1
    assert records[0]["ruling"] == ""
    assert "failed closed to the hold" in records[0]["rationale"]
    assert records[0]["story_key"] == "SOME-KEY"


# --------------------------------------------------------------------------
# 5. boundary: the binding must not leak
# --------------------------------------------------------------------------


def test_story_binding_restores_the_previous_value():
    """Nested bindings restore the OUTER value, not ``None``.

    A ``finally`` that sets ``None`` instead of ``reset(token)`` would lose the
    outer story's key for the rest of its gate call.
    """
    assert merge_mod._adjudication_story_key.get() is None

    with merge_mod.merge_adjudication_story("A-1"):
        assert merge_mod._adjudication_story_key.get() == "A-1"
        with merge_mod.merge_adjudication_story("B-2"):
            assert merge_mod._adjudication_story_key.get() == "B-2"
        assert merge_mod._adjudication_story_key.get() == "A-1", (
            "the inner binding must restore the outer value, not None"
        )
    assert merge_mod._adjudication_story_key.get() is None


def test_story_binding_restores_the_previous_value_on_exception():
    """A raising body must not leave the key bound for the next story."""
    with pytest.raises(RuntimeError), merge_mod.merge_adjudication_story("C-3"):
        assert merge_mod._adjudication_story_key.get() == "C-3"
        raise RuntimeError("boom")

    assert merge_mod._adjudication_story_key.get() is None


# --------------------------------------------------------------------------
# 6. both warning lines name the story
# --------------------------------------------------------------------------


def test_overlord_failure_warning_names_the_story(plan_dir, monkeypatch, caplog):
    _full_autonomy(monkeypatch)
    _stub_overlord(monkeypatch, exc=RuntimeError("overlord down"))
    _stub_ci_gather(monkeypatch, {"pr_checks": "success"})
    story = _parked_story()

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        advance_mod._readjudicate_parked_merge_hold(PLAN, "SOME-KEY", story)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "overlord merge adjudication failed for SOME-KEY" in message
        for message in messages
    ), messages


def test_append_failure_warning_names_the_story(plan_dir, monkeypatch, caplog):
    _full_autonomy(monkeypatch)
    _stub_overlord(monkeypatch, PROCEED_REPLY)
    _stub_ci_gather(monkeypatch, {"pr_checks": "success"})

    def boom(plan_name, record):
        raise OSError("disk full")

    monkeypatch.setattr(persistence_mod, "_append_decision", boom)
    story = _parked_story()

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        advance_mod._readjudicate_parked_merge_hold(PLAN, "SOME-KEY", story)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "failed to append merge adjudication record for SOME-KEY" in message
        for message in messages
    ), messages
