"""MERGEATTR-2: the merge gate's CI gather must probe the story's REAL branch.

``_populate_pr_checks_once`` resolved the story key as
``story.get("key") or story.get("story_key") or "?"``. No production story
carries either field (0 of 892 stories across 242 manifests): a story's identity
is its DICT KEY in ``manifest["stories"]``, and nothing stamps that onto the
story dict. So the key was ALWAYS the literal ``"?"``, the branch was always
``agent/?``, and every full-autonomy high-risk merge adjudication handed the
overlord fabricated CI evidence - a nonexistent-branch result or an unreadable
status - never the PR's real checks.

MERGEATTR-1 landed the mechanism: a module-level ContextVar
``_adjudication_story_key`` plus ``merge_adjudication_story(story_key)``, bound
around ``_merge_decision`` at both ``advance.py`` call sites. This suite pins the
consumption of that mechanism.

The tests drive the REAL path (``advance._adjudicate_merges``) rather than
calling ``_populate_pr_checks_once(story, key)`` directly: a direct call would
pass even if the context manager were bound nowhere at the call sites.

``pipeline/merge.py`` resolves ``_ci_status_once`` lazily inside the function
body through the module-documented monkeypatch seam (``from .server import
...``), so the spy is installed on the re-exported binding on ``pipeline.server``
rather than on a ``pipeline.merge`` name. Same for ``_resolve_story_branch``
(``from .pr import ...``) and ``_invoke_overlord`` (``from .overlord import
...``).
"""

# ruff: noqa: I001
# Import order below is deliberate, not disorganized: ``pipeline.server``
# transitively imports advance/ci/merge at module load, so importing it before
# ``pipeline.advance`` keeps that submodule import resolving against an
# already-initialized module (the ordering test_merge_overlord_adjudication.py
# documents). isort's alphabetical sort would put ``pipeline.advance`` first
# and reintroduce the circular import this ordering avoids.
import json

import pytest

from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import pr as pr_mod
from pipeline import server as server_mod

# The module that owns the manifest persistence and the real adjudication loop
# these tests drive end to end.
from pipeline import advance as advance_mod

PLAN = "PLAN-1"
PROCEED_REPLY = "RULING: proceed\nRATIONALE: ok"
PARK_REPLY = "RULING: park\nRATIONALE: ok"
PASS_STATE = {"state": "pass", "error": ""}
# Deliberately NOT the convention name for REAL-KEY-1: the gather must use the
# resolver's return value, so the two must be distinguishable.
RESOLVED_BRANCH = "agent/real-key-1-9f8e7d"


def _story(**overrides):
    """A production-shaped pr_open high-risk story.

    Deliberately carries NO ``key`` and NO ``story_key``: that is the production
    shape (0 of 892 stories carry either) and the whole point of this suite.
    """
    story = {
        "plan_name": PLAN,
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "worktree": "",
    }
    story.update(overrides)
    return story


def _write_manifest(tmp_path, stories):
    """Write a manifest whose story identity is the DICT KEY, as production does."""
    path = tmp_path / f"{PLAN}.manifest.json"
    path.write_text(json.dumps({"stories": stories}))
    return path


def _summary():
    """The exact summary keys ``advance._adjudicate_merges`` appends to."""
    return {"parked": [], "merged": [], "notify": [], "ci_pending": []}


def _install(monkeypatch, tmp_path, branches, resolve_calls=None, ruling=PROCEED_REPLY):
    """Patch the autonomy knobs, the overlord boundary and the CI gather.

    ``PLAN_DIR`` is patched on ``pipeline.server`` because ``advance.PLAN_DIR``
    is a ``_ServerRef`` that delegates there; leaving it unpatched would make the
    test contend on the real plan lock and fail non-deterministically as
    "locked".
    """
    monkeypatch.setattr(server_mod, "PLAN_DIR", tmp_path, raising=False)
    monkeypatch.setattr(server_mod, "WORKTREE_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)

    def fake_invoke(prompt, plan_role_config=None):
        return ruling

    monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
    monkeypatch.setattr(
        persistence_mod,
        "_plan_role_config",
        lambda plan_name: {"role": "overlord", "model": "opus"},
    )
    monkeypatch.setattr(
        persistence_mod, "_append_decision", lambda plan_name, record: None
    )

    def fake_once(branch, *, sha):
        branches.append(branch)
        return dict(PASS_STATE)

    monkeypatch.setattr(server_mod, "_ci_status_once", fake_once, raising=False)

    def fake_resolve(worktree, story_key):
        if resolve_calls is not None:
            resolve_calls.append((worktree, story_key))
        return RESOLVED_BRANCH

    monkeypatch.setattr(pr_mod, "_resolve_story_branch", fake_resolve)


def test_adjudicate_merges_probes_the_real_story_branch(tmp_path, monkeypatch):
    """The integration-grade test: the real loop must probe ``agent/<real key>``."""
    branches = []
    _install(monkeypatch, tmp_path, branches)
    _write_manifest(tmp_path, {"REAL-KEY-1": _story()})

    advance_mod._adjudicate_merges(PLAN, _summary())

    assert branches == ["agent/real-key-1"]
    assert branches != ["agent/?"]


def test_existing_worktree_resolves_with_the_real_key(tmp_path, monkeypatch):
    """A worktree path must be resolved with the real key, not the "?" sentinel."""
    branches = []
    resolve_calls = []
    # A park ruling keeps the merge path (which resolves the branch again for
    # its own reasons) out of the picture, so the gather's call is unambiguous.
    _install(
        monkeypatch,
        tmp_path,
        branches,
        resolve_calls=resolve_calls,
        ruling=PARK_REPLY,
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_manifest(tmp_path, {"REAL-KEY-1": _story(worktree=str(worktree))})

    advance_mod._adjudicate_merges(PLAN, _summary())

    assert resolve_calls == [(str(worktree), "REAL-KEY-1")]
    # The gather used the resolver's branch, not the convention name.
    assert branches == [RESOLVED_BRANCH]
    assert branches != ["agent/real-key-1"]


def test_two_stories_each_probe_their_own_branch(tmp_path, monkeypatch):
    """A key leaking across loop iterations (a ContextVar never reset) is caught."""
    branches = []
    _install(monkeypatch, tmp_path, branches)
    _write_manifest(
        tmp_path,
        {"REAL-KEY-1": _story(), "REAL-KEY-2": _story()},
    )

    advance_mod._adjudicate_merges(PLAN, _summary())

    assert branches == ["agent/real-key-1", "agent/real-key-2"]


def test_keyless_legacy_caller_still_probes_the_question_mark(tmp_path, monkeypatch):
    """Negative/compat: an unbound direct caller keeps today's behaviour exactly."""
    branches = []
    _install(monkeypatch, tmp_path, branches)
    story = _story()

    result = merge_mod._adjudicate_high_risk_merge(story)

    assert branches == ["agent/?"]
    assert result["action"] == "merge"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
