"""``get_story_worktree`` obeys the plan-scoping every remote read obeys (pe-02).

The middleware is driven directly with a minimal context object and a recording
``call_next``, exactly as ``tests/unit/test_remote_read_scoping.py`` does: no
server, no SDK session, and the tool itself is never re-implemented here. The
cases live in their own file so the established read-scoping suite is untouched.
"""

from __future__ import annotations

# ruff: noqa: I001
# ``pipeline.server`` is imported before the other pipeline modules on purpose:
# a pre-existing import cycle in the server module means a test that reaches
# ``pipeline.*`` has to load it first (see tests/unit/test_triage_cap_silent_skip.py).
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import pipeline.server as p
import pipeline.persistence as ppers
import app.policy as policy_module
from app.identity import Principal
from pipeline.actor import bind_principal
from pipeline.remote_tool_policy import remote_tool_policy

ALPHA_OWNER = Principal(subject="https://idp#alice", auth_method="oidc")
BETA_OWNER = Principal(subject="https://idp#bob", auth_method="oidc")
ALPHA_DELEGATE = Principal(subject="https://idp#dee", auth_method="oidc")
STRANGER = Principal(subject="https://idp#eve", auth_method="oidc")

#: Distinguishes "the handler ran" from any dict the middleware might return.
SENTINEL = object()

TOOL = "get_story_worktree"
ARGUMENTS = {"plan_name": "alpha", "story_key": "S-1"}


class _Ctx:
    """The two attributes the middleware is allowed to read."""

    def __init__(self, params: object, method: str = "tools/call") -> None:
        self.method = method
        self.params = params


class _Recorder:
    """An async ``call_next`` that counts invocations and returns a fixed result."""

    def __init__(self, result: object = SENTINEL) -> None:
        self.calls = 0
        self._result = result

    async def __call__(self, ctx: Any) -> object:
        self.calls += 1
        return self._result


def _can_view() -> Any:
    """Return ``app.policy.can_view``, failing as an assertion while it is missing."""
    can_view = getattr(policy_module, "can_view", None)
    assert callable(can_view), "app.policy.can_view is missing"
    return can_view


@pytest.fixture(autouse=True)
def _plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point every PLAN_DIR binding at a throwaway directory."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    return directory


def _write_plan(plan: str, *, owner: str = "", delegates: tuple[str, ...] = ()) -> None:
    """Write the manifest a plan's visibility is read from."""
    manifest: dict[str, Any] = {"plan": plan}
    if owner:
        manifest["owner"] = owner
    if delegates:
        manifest["delegates"] = list(delegates)
    (ppers.PLAN_DIR / f"{plan}.manifest.json").write_text(json.dumps(manifest))


def _call(params: object, principal: Principal) -> tuple[Any, _Recorder]:
    """Drive the middleware once, returning ``(result, recorder)``."""
    recorder = _Recorder(SENTINEL)

    async def _run() -> object:
        with bind_principal(principal):
            return await remote_tool_policy(_Ctx(params), recorder)

    return asyncio.run(_run()), recorder


def _worktree_call(plan_name: str = "alpha", story_key: str = "S-1") -> dict[str, Any]:
    return {"name": TOOL, "arguments": {"plan_name": plan_name, "story_key": story_key}}


# --------------------------------------------------------------------------- #
# Allowed: the plan's owner and a delegate reach the tool
# --------------------------------------------------------------------------- #
def test_owner_of_the_plan_reaches_the_tool() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)

    result, recorder = _call(_worktree_call(), ALPHA_OWNER)

    assert result is SENTINEL
    assert recorder.calls == 1


def test_delegate_of_the_plan_reaches_the_tool() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject, delegates=(ALPHA_DELEGATE.subject,))

    result, recorder = _call(_worktree_call(), ALPHA_DELEGATE)

    assert result is SENTINEL
    assert recorder.calls == 1


# --------------------------------------------------------------------------- #
# Refused: a stranger never reaches the tool, and cannot tell known from unknown
# --------------------------------------------------------------------------- #
def test_stranger_is_refused_before_the_tool_runs() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)

    result, recorder = _call(_worktree_call(), BETA_OWNER)

    assert result["isError"] is True
    assert recorder.calls == 0
    assert "alpha" not in json.dumps(result)


def test_stranger_gets_one_identical_answer_for_known_and_unknown_plans() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)

    known, known_recorder = _call(_worktree_call("alpha"), STRANGER)
    unknown, unknown_recorder = _call(_worktree_call("ghost"), STRANGER)

    assert known["isError"] is True
    assert unknown["isError"] is True
    # Byte-equal: a distinct answer for "exists but hidden" would be an oracle.
    assert known == unknown
    assert (known_recorder.calls, unknown_recorder.calls) == (0, 0)


def test_traversal_in_plan_name_is_refused_before_the_tool_runs() -> None:
    _can_view()
    _write_plan("alpha", owner=ALPHA_OWNER.subject)

    result, recorder = _call(_worktree_call("../../etc/passwd"), ALPHA_OWNER)

    assert result["isError"] is True
    assert recorder.calls == 0
