"""The repository access hook consulted before a dispatch and before a merge.

With ``PIPELINE_REPO_ACCESS_PLUGIN`` unset the hook is absent and both entry
points change nothing. With a plugin set, a refusal parks the story with
``access refused: <reason token>`` and appends one host audit record, and any
failure to consult the hook refuses.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.plugin_loader import ALLOWLIST_ENV
from pipeline import access_hooks
from pipeline.access_hooks import (
    ACCESS_HOOK_PLUGIN_ENV,
    REFUSAL_PREFIX,
    merge_decision,
    refuse_dispatch,
)
from pipeline.actor import bind_namespace

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = "plan-x"
STORY = "s1"

_TEMPLATE = """\
from pipeline.access_hooks import AccessDecision

CALLS = []


class StubHook:
    def check(self, namespace, repo, action, risk):
        CALLS.append((namespace, repo, action, risk))
@@BODY@@


def build():
    return StubHook()
"""

REFUSE_MERGE_ONLY = """
        if action == "merge":
            return AccessDecision(allowed=False, reason="policy_block")
        return AccessDecision(allowed=True, reason="ok")"""

REFUSE_ALL = """
        return AccessDecision(allowed=False, reason="policy_block")"""

REFUSE_WITH_FREE_TEXT = """
        return AccessDecision(allowed=False, reason="Carol may not touch acme")"""

ALLOW_ALL = """
        return AccessDecision(allowed=True, reason="ok")"""

RAISE = """
        raise RuntimeError("boom")"""

NON_BOOLEAN_ALLOWED = """
        return AccessDecision(allowed="yes", reason="ok")"""

NOT_A_DECISION = """
        return {"allowed": True}"""


class _Records:
    def __init__(self) -> None:
        self.puts: list[tuple[str, str, dict[str, Any]]] = []

    def put_manifest(self, namespace: str, plan: str, manifest: dict[str, Any]) -> None:
        self.puts.append((namespace, plan, manifest))


class _Store:
    def __init__(self) -> None:
        self.records_ = _Records()

    def records(self) -> _Records:
        return self.records_


@pytest.fixture(autouse=True)
def _no_plugin_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ACCESS_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)


@pytest.fixture
def repo_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the repo identity so the hook's repo argument is deterministic."""
    monkeypatch.setattr(access_hooks, "_plan_repo", lambda plan_name: "host/owner/repo")
    monkeypatch.setattr(
        access_hooks, "_manifest_repo", lambda manifest: "host/owner/repo"
    )


def _manifest(risk: str = "high") -> dict[str, Any]:
    return {"stories": {STORY: {"status": "pr_open", "risk": risk}}}


def _install_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    *,
    allowlist: str | None = None,
) -> str:
    """Write ``source`` as a fresh module, select it, and return its name."""
    name = "repo_access_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(ACCESS_HOOK_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name if allowlist is None else allowlist)
    return name


def _stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    return _install_plugin(tmp_path, monkeypatch, _TEMPLATE.replace("@@BODY@@", body))


class _Decider:
    """Stands in for the merge gate's decision; records each call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, story: dict[str, Any]) -> dict[str, str]:
        self.calls.append(story)
        return {"action": "merge", "reason": "autonomy=full"}


class TestDefaultPathUnchanged:
    def test_dispatch_is_not_refused_with_no_plugin(self, repo_identity) -> None:
        assert refuse_dispatch(_Store(), PLAN, STORY, _manifest()) is None

    def test_dispatch_leaves_the_manifest_untouched_with_no_plugin(
        self, repo_identity
    ) -> None:
        manifest = _manifest()
        before = json.dumps(manifest, sort_keys=True)
        store = _Store()
        refuse_dispatch(store, PLAN, STORY, manifest)
        assert json.dumps(manifest, sort_keys=True) == before
        assert store.records_.puts == []

    def test_merge_runs_the_decision_unchanged_with_no_plugin(
        self, repo_identity
    ) -> None:
        decider = _Decider()
        story = _manifest()["stories"][STORY]
        assert merge_decision(decider, PLAN, story) == {
            "action": "merge",
            "reason": "autonomy=full",
        }
        assert decider.calls == [story]

    def test_no_plugin_does_not_resolve_the_repo(self, monkeypatch) -> None:
        def _fail(*_args: Any) -> str:
            raise AssertionError("repo resolved with no plugin configured")

        monkeypatch.setattr(access_hooks, "_plan_repo", _fail)
        monkeypatch.setattr(access_hooks, "_manifest_repo", _fail)
        story = _manifest()["stories"][STORY]
        assert merge_decision(_Decider(), PLAN, story)["action"] == "merge"
        assert refuse_dispatch(_Store(), PLAN, STORY, _manifest()) is None


class TestRefusingMergeOnly:
    def test_refuses_merge_and_parks_with_the_reason_token(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, REFUSE_MERGE_ONLY)
        decider = _Decider()
        story = _manifest()["stories"][STORY]
        assert merge_decision(decider, PLAN, story) == {
            "action": "park",
            "reason": f"{REFUSAL_PREFIX}policy_block",
        }
        assert decider.calls == []

    def test_allows_dispatch_when_only_merge_is_refused(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, REFUSE_MERGE_ONLY)
        store = _Store()
        assert refuse_dispatch(store, PLAN, STORY, _manifest()) is None
        assert store.records_.puts == []

    def test_hook_receives_namespace_action_and_risk(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        name = _stub(tmp_path, monkeypatch, REFUSE_MERGE_ONLY)
        story = _manifest(risk="high")["stories"][STORY]
        with bind_namespace("a/b"):
            merge_decision(_Decider(), PLAN, story)
        assert sys.modules[name].CALLS == [("a/b", "host/owner/repo", "merge", "high")]

    def test_unknown_risk_is_passed_as_unknown_not_low(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        name = _stub(tmp_path, monkeypatch, ALLOW_ALL)
        merge_decision(_Decider(), PLAN, {"status": "pr_open"})
        assert sys.modules[name].CALLS[-1][3] == "unknown"


class TestDispatchRefusal:
    def test_parks_the_story_with_the_reason_token(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, REFUSE_ALL)
        manifest = _manifest()
        store = _Store()
        result = refuse_dispatch(store, PLAN, STORY, manifest)
        assert result == {
            "ok": False,
            "error": f"{REFUSAL_PREFIX}policy_block",
            "story_key": STORY,
        }
        assert manifest["stories"][STORY]["status"] == "parked"
        assert manifest["stories"][STORY]["parked_reason"] == (
            f"{REFUSAL_PREFIX}policy_block"
        )
        assert store.records_.puts == [("default", PLAN, manifest)]

    def test_non_token_reason_is_recorded_as_unspecified(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, REFUSE_WITH_FREE_TEXT)
        manifest = _manifest()
        refuse_dispatch(_Store(), PLAN, STORY, manifest)
        reason = manifest["stories"][STORY]["parked_reason"]
        assert reason == f"{REFUSAL_PREFIX}unspecified"
        assert "Carol" not in reason and "acme" not in reason

    def test_refusal_appends_one_audit_record_without_identifiers(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, REFUSE_ALL)
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            access_hooks, "append_host_audit", lambda **kwargs: recorded.append(kwargs)
        )
        refuse_dispatch(_Store(), PLAN, STORY, _manifest())
        assert len(recorded) == 1
        audit = repr(recorded[0])
        assert "repo_access_refused" in audit
        assert "policy_block" in audit
        assert PLAN not in audit
        assert STORY not in audit
        assert "host/owner/repo" not in audit

    def test_allowed_dispatch_appends_no_audit_record(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, ALLOW_ALL)
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            access_hooks, "append_host_audit", lambda **kwargs: recorded.append(kwargs)
        )
        assert refuse_dispatch(_Store(), PLAN, STORY, _manifest()) is None
        assert recorded == []

    def test_failing_audit_append_does_not_change_the_refusal(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, REFUSE_ALL)

        def _broken(**_kwargs: Any) -> None:
            raise OSError("audit disk full")

        monkeypatch.setattr(access_hooks, "append_host_audit", _broken)
        manifest = _manifest()
        result = refuse_dispatch(_Store(), PLAN, STORY, manifest)
        assert result is not None and result["ok"] is False
        assert manifest["stories"][STORY]["status"] == "parked"


class TestFailClosed:
    def test_hook_raising_refuses_dispatch(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, RAISE)
        manifest = _manifest()
        result = refuse_dispatch(_Store(), PLAN, STORY, manifest)
        assert result is not None and result["ok"] is False
        assert (
            manifest["stories"][STORY]["parked_reason"] == f"{REFUSAL_PREFIX}hook_error"
        )

    def test_hook_raising_refuses_merge_without_calling_the_decision(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, RAISE)
        decider = _Decider()
        result = merge_decision(decider, PLAN, _manifest()["stories"][STORY])
        assert result == {"action": "park", "reason": f"{REFUSAL_PREFIX}hook_error"}
        assert decider.calls == []

    def test_non_boolean_allowed_refuses(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, NON_BOOLEAN_ALLOWED)
        decider = _Decider()
        result = merge_decision(decider, PLAN, _manifest()["stories"][STORY])
        assert result == {"action": "park", "reason": f"{REFUSAL_PREFIX}hook_error"}
        assert decider.calls == []

    def test_a_result_that_is_not_an_access_decision_refuses(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _stub(tmp_path, monkeypatch, NOT_A_DECISION)
        assert refuse_dispatch(_Store(), PLAN, STORY, _manifest()) is not None

    def test_plugin_without_a_check_method_refuses(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        _install_plugin(
            tmp_path,
            monkeypatch,
            "def build():\n    return object()\n",
        )
        assert refuse_dispatch(_Store(), PLAN, STORY, _manifest()) is not None

    def test_spec_without_the_module_allowlist_refuses_before_import(
        self, tmp_path, monkeypatch, repo_identity
    ) -> None:
        name = _install_plugin(
            tmp_path,
            monkeypatch,
            _TEMPLATE.replace("@@BODY@@", ALLOW_ALL),
            allowlist="",
        )
        monkeypatch.delenv(ALLOWLIST_ENV)
        assert refuse_dispatch(_Store(), PLAN, STORY, _manifest()) is not None
        assert name not in sys.modules

    def test_malformed_spec_refuses(self, monkeypatch, repo_identity) -> None:
        monkeypatch.setenv(ACCESS_HOOK_PLUGIN_ENV, "no_colon_here")
        monkeypatch.setenv(ALLOWLIST_ENV, "no_colon_here")
        decider = _Decider()
        result = merge_decision(decider, PLAN, _manifest()["stories"][STORY])
        assert result["action"] == "park"
        assert decider.calls == []


class TestLineBudget:
    @pytest.mark.parametrize(
        "relative",
        [
            "pipeline/access_hooks.py",
            "pipeline/dispatch.py",
            "pipeline/advance_merge.py",
        ],
    )
    def test_source_file_is_within_the_cap(self, relative: str) -> None:
        lines = (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 1000
