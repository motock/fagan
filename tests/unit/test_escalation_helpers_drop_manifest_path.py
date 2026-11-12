"""The escalation helpers persist through the record store, not a manifest path.

``_escalate_to_claude`` and ``_escalate_to_local_fallback_model`` only use the
manifest dict they are handed and write it back via ``records()``. The
``manifest_path`` parameter they used to take was vestigial; ADR 0012 stage C
removes ``manifest_path`` from the Store Protocol, so these signatures must not
keep a path argument alive.
"""

import inspect
from pathlib import Path

import pytest

from pipeline import escalation


def test_escalate_to_claude_has_no_manifest_path_parameter():
    params = inspect.signature(escalation._escalate_to_claude).parameters

    assert "manifest_path" not in params
    assert list(params) == ["manifest", "plan_name", "story_key"]


def test_escalate_to_local_fallback_model_has_no_manifest_path_parameter():
    params = inspect.signature(escalation._escalate_to_local_fallback_model).parameters

    assert "manifest_path" not in params
    assert list(params) == ["manifest", "plan_name", "story_key", "fallback_model"]


def test_escalate_to_claude_rejects_legacy_manifest_path_argument():
    with pytest.raises(TypeError):
        escalation._escalate_to_claude({}, "plan", "S1", Path("plan.manifest.json"))


def test_escalate_to_local_fallback_model_rejects_legacy_manifest_path_argument():
    with pytest.raises(TypeError):
        escalation._escalate_to_local_fallback_model(
            {}, "plan", "S1", Path("plan.manifest.json"), "gpt-oss-20b-high:latest"
        )
