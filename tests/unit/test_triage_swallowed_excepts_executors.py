"""Swallowed-exception handlers in the triage executor modules.

Each test drives the function holding the handler into the handled failure and
asserts the unchanged return value; L sites also assert one constant log record
(class name only), N sites assert an exception outside the narrowed set propagates.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import types

import pytest

from pipeline import triage, triage_park_classes
from pipeline import triage_patch_acceptance as pa
from pipeline import triage_rebase as rb
from pipeline import triage_rebrief as rr
from pipeline import triage_story_actions as sa
from pipeline import triage_test_edit as te
from pipeline.authority_matrix import HUMAN_TIER
from pipeline.branch_guard import BranchGuardError

LOGGER = "pipeline"
SECRET = "secret-token-in-message"


class Boom(Exception):
    """A fault outside every narrowed exception set."""


def _boom(*_a, **_k):
    raise RuntimeError(SECRET)


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def _assert_one_class_only_warning(caplog):
    records = _warnings(caplog)
    assert len(records) == 1
    assert "RuntimeError" in records[0].getMessage()
    assert SECRET not in records[0].getMessage()
    assert records[0].exc_info is None


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)


@pytest.fixture
def parked(monkeypatch):
    """Replace the park helper with a marker so only the handler under test logs."""
    monkeypatch.setattr(triage, "_park", lambda plan, key, story, reason: "parked:" + reason)


# --- triage_park_classes ----------------------------------------------------


def test_classify_park_returns_unknown_and_logs_when_classification_fails(monkeypatch, caplog):
    monkeypatch.setattr(triage_park_classes, "_classify", _boom)

    park = triage_park_classes.classify_park({}, {})

    assert (park.name, park.facts) == ("unknown", {})
    _assert_one_class_only_warning(caplog)


# --- triage_patch_acceptance ------------------------------------------------


def _ctx():
    return pa._PatchContext("p", "k", {"status": "parked"})


def test_rewrite_prompt_is_built_and_logs_when_evidence_fails(monkeypatch, caplog):
    monkeypatch.setattr(triage, "collect_triage_evidence", _boom)
    monkeypatch.setattr(triage, "_load_policy", lambda: "POLICY")

    prompt = pa._build_rewrite_prompt(_ctx(), [], ".", "why", "detail")

    assert prompt.startswith("POLICY") and "TRIAGE EVIDENCE:\n\n" in prompt
    _assert_one_class_only_warning(caplog)


def test_rewrite_prompt_is_built_and_logs_when_policy_fails(monkeypatch, caplog):
    monkeypatch.setattr(triage, "collect_triage_evidence", lambda *a, **k: "EVID")
    monkeypatch.setattr(triage, "_load_policy", _boom)

    prompt = pa._build_rewrite_prompt(_ctx(), [], ".", "why", "detail")

    assert prompt.startswith("\n\nTRIAGE QUESTION") and "EVID" in prompt
    _assert_one_class_only_warning(caplog)


def test_request_rewrite_parks_with_exception_class_when_overlord_fails(monkeypatch, parked):
    monkeypatch.setattr(triage, "_invoke_overlord", _boom)
    monkeypatch.setattr(triage, "_plan_role_config", lambda plan: {})

    parsed, result = pa._request_rewrite(_ctx(), "prompt")

    assert parsed is None
    assert result == (
        "parked:patch_acceptance ruled but the overlord could not be "
        "re-invoked: RuntimeError; parked for a human"
    )


def _apply(monkeypatch, server_module):
    modes = []
    monkeypatch.setitem(sys.modules, "pipeline.server", server_module)
    monkeypatch.setattr(triage, "acceptance_digests", lambda story: {})
    monkeypatch.setattr(triage, "_record_execution", lambda *a, **k: modes.append(a[4]))
    story = {"status": "parked", "acceptance": [{"path": "a.py", "source": "old"}]}
    result = pa._apply_rewrite(pa._PatchContext("p", "k", story), {}, "patch_acceptance", "new", "diag")
    return result, modes


def test_apply_rewrite_falls_back_to_gated_when_autonomy_cannot_be_imported(monkeypatch):
    result, modes = _apply(monkeypatch, types.ModuleType("pipeline.server"))

    assert modes == ["gated"]
    assert result == "patch_acceptance"


def test_apply_rewrite_propagates_unexpected_error_reading_autonomy(monkeypatch):
    server = types.ModuleType("pipeline.server")
    server.__getattr__ = _boom

    with pytest.raises(RuntimeError):
        _apply(monkeypatch, server)


# --- triage_rebase ----------------------------------------------------------


def test_pr_blocks_unpark_fails_closed_and_logs_when_state_unreadable(monkeypatch, caplog):
    monkeypatch.setattr(rr, "_pr_state", _boom)

    reason = rb._pr_blocks_unpark({"pr_url": "https://example/pr/1"}, "/r")

    assert reason == "the PR state could not be read"
    _assert_one_class_only_warning(caplog)


_REBASE_STORY = {
    "status": "parked",
    "branch": "agent/x",
    "parked_reason": "rebase conflict: CONFLICT (content): Merge conflict in pipeline/x.py",
}


def _resolve(monkeypatch, error):
    monkeypatch.setattr(triage, "_triage_mode", lambda manifest=None: "execute")
    monkeypatch.setattr(triage, "_park", lambda plan, key, story, reason: "parked:" + reason)
    monkeypatch.setattr(rb, "plan_repo", lambda manifest: rr.PlanRepo("/r", "main"))
    monkeypatch.setattr(rb, "_observe", lambda *a: (_ for _ in ()).throw(error))
    return rb.execute_resolve_rebase("p", "k", dict(_REBASE_STORY), {"action": "resolve_rebase"}, {})


@pytest.mark.parametrize(
    "error", [RuntimeError("fetch failed"), OSError("no git"), subprocess.TimeoutExpired("git", 1), ValueError("n")]
)
def test_resolve_rebase_parks_when_live_evidence_cannot_be_read(monkeypatch, error):
    result = _resolve(monkeypatch, error)

    assert result == "parked:resolve_rebase declined: live git evidence could not be read; parked for a human"


def test_resolve_rebase_propagates_error_outside_the_git_failure_set(monkeypatch):
    with pytest.raises(Boom):
        _resolve(monkeypatch, Boom())


# --- triage_rebrief ---------------------------------------------------------


_LIVE_STORY = {"pr_url": "https://example/pr/1", "branch": "agent/x"}


def test_live_evidence_refusal_fails_closed_and_logs_when_pr_state_fails(monkeypatch, caplog):
    monkeypatch.setattr(rr, "_pr_state", _boom)

    assert rr._live_evidence_refusal(_LIVE_STORY, "/r") == "the PR state could not be read"
    _assert_one_class_only_warning(caplog)


@pytest.mark.parametrize("error", [BranchGuardError("q"), OSError("git"), subprocess.TimeoutExpired("git", 1)])
def test_live_evidence_refusal_fails_closed_when_origin_cannot_be_queried(monkeypatch, error):
    monkeypatch.setattr(rr, "_pr_state", lambda root, url: "OPEN")
    monkeypatch.setattr(rr, "_branch_on_origin", lambda *a: (_ for _ in ()).throw(error))

    assert rr._live_evidence_refusal(_LIVE_STORY, "/r") == "origin could not be queried for the branch"


def test_live_evidence_refusal_propagates_error_outside_the_git_failure_set(monkeypatch):
    monkeypatch.setattr(rr, "_pr_state", lambda root, url: "OPEN")
    monkeypatch.setattr(rr, "_branch_on_origin", lambda *a: (_ for _ in ()).throw(Boom()))

    with pytest.raises(Boom):
        rr._live_evidence_refusal(_LIVE_STORY, "/r")


# --- triage_story_actions ---------------------------------------------------


def _notify_boom(*_a, **_k):
    raise RuntimeError(SECRET)


def test_split_story_with_invalid_payload_parks_and_logs_when_notice_fails(monkeypatch, parked, caplog):
    monkeypatch.setattr(triage, "_notify_user", _notify_boom)

    result = sa._execute_split_story("p", "k", {}, {"action": "split_story", "split": ["only one"]}, {})

    assert result.startswith("parked:split_story ruled but SPLIT payload invalid")
    _assert_one_class_only_warning(caplog)


def test_split_story_over_budget_parks_and_logs_when_notice_fails(monkeypatch, parked, caplog):
    monkeypatch.setattr(triage, "_notify_user", _notify_boom)
    monkeypatch.setattr(triage, "plan_triage_budget_exhausted", lambda manifest: True)

    result = sa._execute_split_story("p", "k", {}, {"action": "split_story", "split": ["a", "b"]}, {})

    assert result == "parked:plan triage budget exhausted"
    _assert_one_class_only_warning(caplog)


def test_mark_done_uncorroborated_parks_and_logs_when_notice_fails(monkeypatch, parked, caplog):
    monkeypatch.setattr(triage, "_notify_user", _notify_boom)

    result = sa._execute_mark_done("p", "k", {}, {"action": "mark_done"}, {})

    assert result == "parked:" + sa._MARK_DONE_UNCORROBORATED_REASON
    _assert_one_class_only_warning(caplog)


# --- triage_test_edit -------------------------------------------------------


def test_resolve_tier_fails_closed_to_human_and_logs_when_matrix_unreadable(monkeypatch, caplog):
    monkeypatch.setattr(te, "FileLayerSource", _boom)

    assert te.resolve_tier() == HUMAN_TIER
    _assert_one_class_only_warning(caplog)


def _approve(monkeypatch, error):
    monkeypatch.setattr(triage, "_triage_mode", lambda manifest=None: "execute")
    monkeypatch.setattr(triage, "_park", lambda plan, key, story, reason: "parked:" + reason)
    monkeypatch.setattr(te, "plan_repo", lambda manifest: rr.PlanRepo("/r", "main"))
    monkeypatch.setattr(te, "_live_evidence", lambda *a: (_ for _ in ()).throw(error))
    story = {"branch": "agent/x"}
    return te.execute_approve_test_edit("p", "k", story, {"action": "approve_test_edit"}, {})


@pytest.mark.parametrize(
    "error", [RuntimeError("fetch failed"), OSError("no git"), subprocess.TimeoutExpired("git", 1)]
)
def test_approve_test_edit_parks_when_live_evidence_cannot_be_read(monkeypatch, error):
    result = _approve(monkeypatch, error)

    assert result == "parked:approve_test_edit declined: live git evidence could not be read; parked for a human"


def test_approve_test_edit_propagates_error_outside_the_git_failure_set(monkeypatch):
    with pytest.raises(Boom):
        _approve(monkeypatch, Boom())
