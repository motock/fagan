"""TRIFID-4: an unreadable or in-flight CI status is never rendered as ``none``.

``_populate_pr_checks_once`` used to swallow every gather failure with a bare
``except Exception: return`` and no logging, and the prompt rendered
``f"PR CHECKS: {story.get('pr_checks') or '(none)'}"``. Two very different
situations therefore collapsed into the same three tokens ``(none)``:

* CI genuinely reported no checks, and
* the gather could not determine CI state at all - an exception was swallowed,
  or ``gh pr checks`` exited nonzero (``{"state": "none", "error": ...}``),
  which is also what an in-flight / not-yet-registered run looks like.

The overlord then asserted the first as fact while the second was true. Live
case: plan ``chat-worktree-apply`` stories WAP-7 and WAP-9 were parked with
rationales citing "no PR checks" / "the missing PR checks are a residual
verification gap" while the manifest held ``pr_checks: None``.

The contract these tests pin down:

* A gather failure is logged as a WARNING on the ``"pipeline"`` logger naming
  the story key and the exception type, and recorded on the story as
  ``story["pr_checks_error"] = f"{type(exc).__name__}: {exc}"[:200]``.
* The ``key`` lookup is hoisted ABOVE the ``try``: the first statement inside
  the try is an import, so a key bound inside the try would be unbound exactly
  when the failure handler needs it (``NameError`` from inside the swallow
  path).
* The prompt renders ``PR CHECKS: (unreadable - <error>)`` for a recorded
  failure, the gathered state verbatim (including ``{"state": "none", ...}``
  with its ``error`` field) when there is one, and ``PR CHECKS: (none)`` only
  when the story was never gathered at all.
* The gather still never propagates and the adjudication still fails closed to
  the standing high-risk hold.
* ``merge_park_evidence`` keeps its exact ``{"pr_checks"}`` key set - the new
  error key lives on the story, never inside that snapshot.
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
from pathlib import Path

import pytest

from pipeline import merge as merge_mod
from pipeline import overlord as overlord_mod
from pipeline import persistence as persistence_mod
from pipeline import server as server_mod
from pipeline import pr as pr_mod

# The module that owns the manifest persistence guarded by the end-to-end test
# at the bottom of this file.
from pipeline import advance as advance_mod

HOLD_REASON = "high risk held for human review"
PARK_REPLY = "RULING: park\nRATIONALE: security review is too thin"
PROCEED_REPLY = "RULING: proceed\nRATIONALE: checks are green and contained"

PLAN = "PLAN-1"
E2E_KEY = "TRIFID-4-E2E"

PASS_STATE = {"state": "pass", "error": ""}


def _boom_blocking(*args, **kwargs):
    """Stand-in for the blocking poller: using it is a hard failure."""
    raise AssertionError(
        "the blocking _ci_status poller must never be used by the merge "
        "adjudication - it time.sleep(10)-loops up to the merge timeout inside "
        "the scheduler's plan-locked tick"
    )


def _story(**overrides):
    """A production-shaped pr_open high-risk story with no gathered evidence."""
    story = {
        "key": "TRIFID-4-STORY",
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


class _Harness:
    """Patches the autonomy knobs, the overlord boundary and the CI gather.

    ``_ci_status_once`` is stubbed on ``pipeline.server`` only: that is the
    module-documented monkeypatch seam merge.py resolves the lazy import
    through. ``gather=None`` means the stub returns ``None`` (no state
    gathered); pass ``gather_exc`` to make it raise.
    """

    def __init__(
        self,
        monkeypatch,
        ruling=None,
        gather=None,
        gather_exc=None,
        autonomy="full",
        threshold="low",
        break_first_import=False,
    ):
        self.invocations = []
        self.decisions = []
        self.gather_calls = []
        self.ruling = ruling
        monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", autonomy, raising=False)
        monkeypatch.setattr(
            server_mod, "PIPELINE_RISK_THRESHOLD", threshold, raising=False
        )

        def fake_invoke(prompt, plan_role_config=None):
            self.invocations.append(
                {"prompt": prompt, "plan_role_config": plan_role_config}
            )
            return self.ruling

        monkeypatch.setattr(overlord_mod, "_invoke_overlord", fake_invoke)
        monkeypatch.setattr(
            persistence_mod,
            "_plan_role_config",
            lambda plan_name: {"role": "overlord", "model": "opus"},
        )
        monkeypatch.setattr(
            persistence_mod,
            "_append_decision",
            lambda plan_name, record: self.decisions.append((plan_name, record)),
        )

        def fake_once(branch, *, sha):
            self.gather_calls.append({"branch": branch, "sha": sha})
            if gather_exc is not None:
                raise gather_exc
            return dict(gather) if gather is not None else None

        monkeypatch.setattr(server_mod, "_ci_status_once", fake_once, raising=False)
        monkeypatch.setattr(server_mod, "_ci_status", _boom_blocking, raising=False)

        monkeypatch.setattr(
            pr_mod,
            "_resolve_story_branch",
            lambda worktree, story_key: "agent/trifid-4",
        )
        if break_first_import:
            # The FIRST statement inside the gather's try block is
            # ``from .pr import _convention_branch, _resolve_story_branch``.
            # Removing the name makes that import raise ImportError before the
            # gather ever reaches the branch resolution - the exact shape that
            # left ``key`` unbound under the old ordering.
            monkeypatch.delattr(pr_mod, "_convention_branch")

    def decide(self, story):
        # Bind the production adjudication context exactly as
        # ``advance._adjudicate_merges`` does around its gate call.
        with merge_mod.merge_adjudication_plan(PLAN):
            return merge_mod._merge_decision(story)

    @property
    def prompt(self):
        assert self.invocations, "the overlord was never invoked"
        return self.invocations[0]["prompt"]

    def pr_checks_line(self):
        lines = [
            line for line in self.prompt.splitlines() if line.startswith("PR CHECKS:")
        ]
        assert len(lines) == 1, f"expected exactly one PR CHECKS line, got {lines!r}"
        return lines[0]


def _pipeline_warnings(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "pipeline" and record.levelno == logging.WARNING
    ]


# --------------------------------------------------------------------------
# a failed gather is recorded, logged and rendered as unreadable - never none
# --------------------------------------------------------------------------


def test_gather_failure_renders_unreadable_and_fails_closed(monkeypatch):
    h = _Harness(monkeypatch, ruling=None, gather_exc=RuntimeError("gh exploded"))
    story = _story()

    decision = h.decide(story)  # must not raise

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert not story.get("pr_checks"), "a failed gather must leave pr_checks unset"
    assert story["pr_checks_error"].startswith("RuntimeError")
    assert "gh exploded" in story["pr_checks_error"]
    assert "PR CHECKS: (unreadable" in h.prompt
    assert "PR CHECKS: (none)" not in h.prompt, (
        "an unreadable CI status is not evidence of absence and must never be "
        "rendered as (none)"
    )
    assert "RuntimeError" in h.pr_checks_line()


def test_gather_failure_logs_a_warning_naming_the_story_key(monkeypatch, caplog):
    h = _Harness(monkeypatch, ruling=None, gather_exc=RuntimeError("gh exploded"))
    story = _story()

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        h.decide(story)

    messages = _pipeline_warnings(caplog)
    assert messages, "the swallowed gather failure must be logged, not silent"
    assert any(story["key"] in message for message in messages), (
        f"the warning must name the story key; got {messages!r}"
    )
    assert any("RuntimeError" in message for message in messages), (
        f"the warning must name the exception type; got {messages!r}"
    )


def test_import_failure_still_logs_and_records_without_raising(monkeypatch, caplog):
    """The key lookup is hoisted above the try, so the handler can always log.

    With the key bound inside the try (the old ordering) the first import
    raising would leave ``key`` unbound and the handler would raise NameError
    from inside the swallow path.
    """
    h = _Harness(monkeypatch, ruling=None, break_first_import=True)
    story = _story()

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        decision = h.decide(story)  # must not raise NameError or ImportError

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert story["pr_checks_error"].startswith("ImportError")
    assert "PR CHECKS: (unreadable" in h.prompt
    messages = _pipeline_warnings(caplog)
    assert any(story["key"] in message for message in messages), (
        f"the handler must log the story key even when the import raised; "
        f"got {messages!r}"
    )


def test_pr_checks_error_is_truncated_to_200_characters(monkeypatch):
    h = _Harness(monkeypatch, ruling=None, gather_exc=RuntimeError("x" * 500))
    story = _story()

    h.decide(story)

    recorded = story["pr_checks_error"]
    assert recorded.startswith("RuntimeError")
    assert len(recorded) == 200, "the recorded error must be capped at 200 chars"


def test_handler_catches_plain_exception_subclasses(monkeypatch):
    """The catch clause stays ``except Exception`` - a plain error is caught."""
    h = _Harness(monkeypatch, ruling=None, gather_exc=ValueError("bad json"))
    story = _story()

    assert h.decide(story) == {"action": "park", "reason": HOLD_REASON}
    assert story["pr_checks_error"].startswith("ValueError")


def test_base_exception_is_not_swallowed(monkeypatch):
    """Pins that the handler is ``except Exception``, not a bare ``except:``."""
    h = _Harness(monkeypatch, ruling=None, gather_exc=KeyboardInterrupt())
    story = _story()

    with pytest.raises(KeyboardInterrupt):
        h.decide(story)

    assert "pr_checks_error" not in story


# --------------------------------------------------------------------------
# a gathered state is rendered verbatim - including a nonzero-rc "none"
# --------------------------------------------------------------------------


def test_state_none_with_error_is_rendered_verbatim(monkeypatch):
    gathered = {"state": "none", "error": "gh pr checks exited 1"}
    h = _Harness(monkeypatch, ruling=PARK_REPLY, gather=gathered)
    story = _story()

    h.decide(story)

    assert story["pr_checks"] == gathered
    assert "pr_checks_error" not in story
    line = h.pr_checks_line()
    assert "'state': 'none'" in line
    assert "gh pr checks exited 1" in line, (
        "the error field must stay visible so the reader can tell a nonzero gh "
        "exit apart from a real empty result"
    )
    assert "(unreadable" not in line
    assert "PR CHECKS: (none)" not in h.prompt


def test_pending_state_is_rendered_verbatim(monkeypatch):
    gathered = {"state": "pending", "error": ""}
    h = _Harness(monkeypatch, ruling=PARK_REPLY, gather=gathered)
    story = _story()

    h.decide(story)

    assert story["pr_checks"] == gathered
    line = h.pr_checks_line()
    assert "'state': 'pending'" in line
    assert "(unreadable" not in line
    assert "PR CHECKS: (none)" not in h.prompt


# --------------------------------------------------------------------------
# the never-gathered path still says (none) - the new branch must not over-fire
# --------------------------------------------------------------------------


def test_never_gathered_story_still_renders_none(monkeypatch):
    """The gate did not run the gather: no state, no error -> ``(none)``."""
    h = _Harness(monkeypatch, ruling=PARK_REPLY)
    monkeypatch.setattr(merge_mod, "_populate_pr_checks_once", lambda story: None)
    story = _story()

    with merge_mod.merge_adjudication_plan(PLAN):
        decision = merge_mod._adjudicate_high_risk_merge(story, PLAN)

    assert decision == {"action": "park", "reason": HOLD_REASON}
    assert h.pr_checks_line() == "PR CHECKS: (none)"
    assert "pr_checks" not in story
    assert "pr_checks_error" not in story


def test_gather_returning_none_renders_none_without_error(monkeypatch):
    h = _Harness(monkeypatch, ruling=PARK_REPLY, gather=None)
    story = _story()

    h.decide(story)

    assert "pr_checks" not in story
    assert "pr_checks_error" not in story
    assert h.pr_checks_line() == "PR CHECKS: (none)"


def test_no_gather_outside_full_autonomy_records_no_error(monkeypatch):
    h = _Harness(monkeypatch, autonomy="gated", ruling=PROCEED_REPLY, gather=PASS_STATE)
    story = _story()

    assert h.decide(story) == {"action": "park", "reason": HOLD_REASON}
    assert h.gather_calls == []
    assert "pr_checks_error" not in story


# --------------------------------------------------------------------------
# end-to-end: the new error key never leaks into merge_park_evidence
# --------------------------------------------------------------------------


def _e2e_story(**overrides):
    """A production-shaped pr_open story: NO ``plan`` key, as manifests have."""
    story = {
        "key": E2E_KEY,
        "status": "pr_open",
        "parked_reason": None,
        "review_verdict": "APPROVE",
        "security_review_verdict": "APPROVE",
        "risk": "high",
        "summary": "Rewrite the auth token cache",
        "worktree": "",
        "dependencies": [],
    }
    story.update(overrides)
    return story


def _summary():
    """A summary dict carrying every key ``_adjudicate_merges`` appends to."""
    return {"parked": [], "notify": [], "failed": [], "merged": [], "ci_pending": []}


def test_merge_park_evidence_key_set_is_unchanged_by_a_gather_failure(
    plan_dir, monkeypatch
):
    monkeypatch.setattr(server_mod, "PIPELINE_AUTONOMY", "full", raising=False)
    monkeypatch.setattr(server_mod, "PIPELINE_RISK_THRESHOLD", "low", raising=False)
    monkeypatch.setattr(advance_mod, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        advance_mod,
        "_atomic_write_json",
        lambda path, data: Path(path).write_text(json.dumps(data, indent=2)),
    )
    monkeypatch.setattr(
        overlord_mod,
        "_invoke_overlord",
        lambda prompt, plan_role_config=None: PARK_REPLY,
    )

    def fake_once(branch, *, sha):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(server_mod, "_ci_status_once", fake_once, raising=False)
    monkeypatch.setattr(server_mod, "_ci_status", _boom_blocking, raising=False)
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": {E2E_KEY: _e2e_story()}}, indent=2)
    )

    summary = _summary()
    advance_mod._adjudicate_merges(PLAN, summary)

    assert summary["parked"] == [E2E_KEY]
    manifest = json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())
    parked = manifest["stories"][E2E_KEY]
    assert parked["pr_checks_error"].startswith("RuntimeError")
    evidence = parked.get("merge_park_evidence")
    assert evidence is not None, "the park path records the evidence snapshot"
    assert set(evidence) == {"pr_checks"}, (
        "the new pr_checks_error key must live on the story, never inside the "
        "merge_park_evidence snapshot"
    )
    assert evidence["pr_checks"] is None
