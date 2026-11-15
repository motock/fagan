"""External dependency references: ``ref:<namespace>:<plan>:<story_key>``.

Ingest refuses every external reference unless a reference hook is selected
through ``PIPELINE_REFERENCE_PLUGIN`` and allows it. Readiness reads only the
referenced story's status string, and only through a read of the store. Plain
``<plan>:<story_key>`` entries must ingest exactly as they did before.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.plugin_loader import ALLOWLIST_ENV
from pipeline import access_hooks, external_refs, ingest
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline.access_hooks import REFERENCE_HOOK_PLUGIN_ENV
from pipeline.actor import current_namespace
from pipeline.store_records import UnknownNamespaceError

REFERENCED_NAMESPACE = "alpha/beta"
REFERENCED_PLAN = "plan-one"
REFERENCED_KEY = "S9"
EXTERNAL_REF = f"ref:{REFERENCED_NAMESPACE}:{REFERENCED_PLAN}:{REFERENCED_KEY}"

_ALLOW_TEMPLATE = """\
CALLS = []


class StubHook:
    def allow(self, from_namespace, to_namespace):
        CALLS.append((from_namespace, to_namespace))
        return @@RESULT@@


def build():
    return StubHook()
"""

_RAISE_SOURCE = """\
class StubHook:
    def allow(self, from_namespace, to_namespace):
        raise RuntimeError("boom")


def build():
    return StubHook()
"""

_NO_ALLOW_SOURCE = """\
class StubHook:
    def check(self, namespace, repo, action, risk):
        return None


def build():
    return StubHook()
"""


@pytest.fixture(autouse=True)
def _no_reference_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REFERENCE_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)


@pytest.fixture(autouse=True)
def _audit_captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Keep refusal audit appends in memory; they must never reach a real log."""
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        access_hooks, "append_host_audit", lambda **kwargs: recorded.append(kwargs)
    )
    return recorded


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **kw: pytest.fail("Plane must not be called")
    )
    return d


def _install_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> str:
    name = "reference_hook_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(REFERENCE_HOOK_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    return name


def _allow_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str) -> str:
    return _install_hook(tmp_path, monkeypatch, _ALLOW_TEMPLATE.replace("@@RESULT@@", result))


def _ingest(plan_dir, tmp_path, **story_extra):
    story = {"summary": "Do it", "key": "S1", **story_extra}
    plan = {"repo_root": str(tmp_path), "epics": [{"summary": "E", "stories": [story]}]}
    (plan_dir / "bb.json").write_text(json.dumps(plan))
    return p.ingest_plan("bb")


def _manifest(plan_dir):
    return json.loads((plan_dir / "bb.manifest.json").read_text())


class _Records:
    """Read-only stand-in for the namespaced record view of a store."""

    def __init__(self, manifests: dict[tuple[str, str], dict[str, Any]]) -> None:
        self._manifests = manifests
        self.reads: list[tuple[str, str]] = []

    def get_manifest(self, namespace: str, plan_name: str) -> dict[str, Any] | None:
        self.reads.append((namespace, plan_name))
        return self._manifests.get((namespace, plan_name))


class _Store:
    def __init__(self, records: _Records) -> None:
        self._records = records

    def records(self) -> _Records:
        return self._records


def _referenced_store(status: str) -> _Store:
    manifest = {
        "stories": {
            REFERENCED_KEY: {
                "status": status,
                "agent_instructions": "SECRET BODY TEXT",
                "summary": "Private title",
            }
        }
    }
    return _Store(_Records({(REFERENCED_NAMESPACE, REFERENCED_PLAN): manifest}))


class TestDefaultRefusesAtIngest:
    def test_should_refuse_external_reference_with_no_plugin(self, plan_dir, tmp_path):
        result = _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])

        assert result["ok"] is False
        assert "reference" in result["error"]
        assert not (plan_dir / "bb.manifest.json").exists()

    def test_should_audit_the_no_plugin_refusal_without_identifiers(
        self, plan_dir, tmp_path, _audit_captured
    ):
        _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])

        assert len(_audit_captured) == 1
        record = _audit_captured[0]
        assert record["outcome"] == "refused"
        assert record["detail"] == {
            "access_action": "reference",
            "reason": "no_reference_plugin",
        }
        serialized = json.dumps(record)
        for identifier in (REFERENCED_NAMESPACE, REFERENCED_PLAN, REFERENCED_KEY):
            assert identifier not in serialized


class TestUnchangedPathWithoutPlugin:
    """With no plugin and no reference entries, the check is today's check."""

    @pytest.mark.parametrize(
        "blocked_by",
        [
            None,
            [],
            ["other:K1"],
            ["x.y-z:K_2", "a:b:c"],
            "plan:KEY",
            ["plan:"],
            ["nocolon"],
            [5],
            ["a b:c"],
        ],
    )
    def test_should_return_the_plain_check_result_unchanged(self, blocked_by):
        story = {"summary": "Do it", "blocked_by": blocked_by}

        assert external_refs.blocked_by_error(story, ingest._blocked_by_error) == (
            ingest._blocked_by_error(story)
        )

    def test_should_ingest_plain_blocked_by_as_before(self, plan_dir, tmp_path):
        result = _ingest(plan_dir, tmp_path, blocked_by=["other:K1", "x.y-z:K_2"])

        assert result["ok"] is True
        assert _manifest(plan_dir)["stories"]["S1"]["blocked_by"] == ["other:K1", "x.y-z:K_2"]


class TestStubAllowingHook:
    def test_should_accept_external_reference_when_hook_allows(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _allow_hook(tmp_path, monkeypatch, "True")

        result = _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])

        assert result["ok"] is True
        assert _manifest(plan_dir)["stories"]["S1"]["blocked_by"] == [EXTERNAL_REF]

    def test_should_pass_current_and_referenced_namespace_to_the_hook(
        self, tmp_path, monkeypatch
    ):
        name = _allow_hook(tmp_path, monkeypatch, "True")

        external_refs.blocked_by_error({"blocked_by": [EXTERNAL_REF]}, lambda _s: None)

        assert sys.modules[name].CALLS == [(current_namespace(), REFERENCED_NAMESPACE)]

    def test_should_refuse_external_reference_when_hook_denies(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _allow_hook(tmp_path, monkeypatch, "False")

        result = _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])

        assert result["ok"] is False
        assert not (plan_dir / "bb.manifest.json").exists()

    @pytest.mark.parametrize("result", ["'yes'", "1", "None"])
    def test_should_refuse_a_non_boolean_decision(
        self, plan_dir, tmp_path, monkeypatch, result
    ):
        _allow_hook(tmp_path, monkeypatch, result)

        assert _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])["ok"] is False

    def test_should_refuse_when_the_hook_raises(self, plan_dir, tmp_path, monkeypatch):
        _install_hook(tmp_path, monkeypatch, _RAISE_SOURCE)

        assert _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])["ok"] is False

    def test_should_refuse_when_the_plugin_has_no_allow_method(
        self, plan_dir, tmp_path, monkeypatch
    ):
        _install_hook(tmp_path, monkeypatch, _NO_ALLOW_SOURCE)

        assert _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])["ok"] is False

    def test_should_refuse_without_the_allowlist_before_import(
        self, plan_dir, tmp_path, monkeypatch
    ):
        name = "reference_hook_" + uuid.uuid4().hex
        (tmp_path / f"{name}.py").write_text(
            _ALLOW_TEMPLATE.replace("@@RESULT@@", "True"), encoding="utf-8"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv(REFERENCE_HOOK_PLUGIN_ENV, f"{name}:build")

        assert _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])["ok"] is False

    def test_should_refuse_with_a_malformed_plugin_spec(
        self, plan_dir, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(REFERENCE_HOOK_PLUGIN_ENV, "no_colon_here")
        monkeypatch.setenv(ALLOWLIST_ENV, "no_colon_here")

        assert _ingest(plan_dir, tmp_path, blocked_by=[EXTERNAL_REF])["ok"] is False


class TestReadinessBlocksUntilDone:
    @pytest.mark.parametrize("status", ["todo", "in_progress", "pr_open", "parked"])
    def test_should_hold_the_story_while_the_referenced_story_is_not_done(
        self, tmp_path, monkeypatch, status
    ):
        _allow_hook(tmp_path, monkeypatch, "True")

        gaps = external_refs.external_gaps(_referenced_store(status), {"blocked_by": [EXTERNAL_REF]})

        assert gaps == [EXTERNAL_REF]

    @pytest.mark.parametrize("status", ["done", "merged"])
    def test_should_release_the_story_once_the_referenced_story_is_done(
        self, tmp_path, monkeypatch, status
    ):
        _allow_hook(tmp_path, monkeypatch, "True")

        gaps = external_refs.external_gaps(_referenced_store(status), {"blocked_by": [EXTERNAL_REF]})

        assert gaps == []

    def test_should_hold_the_story_when_the_hook_no_longer_allows(self, tmp_path, monkeypatch):
        _allow_hook(tmp_path, monkeypatch, "False")

        gaps = external_refs.external_gaps(_referenced_store("done"), {"blocked_by": [EXTERNAL_REF]})

        assert gaps == [EXTERNAL_REF]

    def test_should_hold_the_story_with_no_plugin_selected(self):
        gaps = external_refs.external_gaps(_referenced_store("done"), {"blocked_by": [EXTERNAL_REF]})

        assert gaps == [EXTERNAL_REF]

    def test_should_hold_the_story_on_a_malformed_reference(self, tmp_path, monkeypatch):
        _allow_hook(tmp_path, monkeypatch, "True")

        gaps = external_refs.external_gaps(_referenced_store("done"), {"blocked_by": ["ref:alpha:plan"]})

        assert gaps == ["ref:alpha:plan"]

    def test_should_not_treat_plain_entries_as_external_gaps(self):
        gaps = external_refs.external_gaps(_referenced_store("todo"), {"blocked_by": ["p:K"]})

        assert gaps == []


class TestMalformedReferencesRefused:
    @pytest.fixture(autouse=True)
    def _allow_everything(self, tmp_path, monkeypatch):
        _allow_hook(tmp_path, monkeypatch, "True")

    @pytest.mark.parametrize(
        "entry",
        [
            "ref:",
            "ref:alpha",
            "ref:alpha:plan",
            "ref:alpha:plan:",
            "ref::plan:S9",
            "ref:alpha::S9",
            "ref:Alpha:plan:S9",
            "ref:alpha/beta/gamma:plan:S9",
            "ref:alpha/:plan:S9",
            "ref:alpha:pl an:S9",
            "ref:alpha:plan:S/9",
            "ref:alpha:plan:S9:extra",
        ],
    )
    def test_should_refuse_malformed_reference(self, plan_dir, tmp_path, entry):
        result = _ingest(plan_dir, tmp_path, blocked_by=[entry])

        assert result["ok"] is False
        assert not (plan_dir / "bb.manifest.json").exists()

    def test_should_not_echo_a_malformed_reference_in_the_error(self):
        error = external_refs.blocked_by_error({"blocked_by": ["ref:alpha:plan"]}, lambda _s: None)

        assert error is not None
        assert "ref:alpha:plan" not in error

    def test_should_report_a_plain_error_before_a_reference_error(self):
        error = external_refs.blocked_by_error(
            {"blocked_by": [EXTERNAL_REF, "nocolon"]},
            lambda story: "plain error" if "nocolon" in story["blocked_by"] else None,
        )

        assert error == "plain error"


class TestAccessorReturnsStatusOnly:
    def test_should_return_only_the_status_string(self):
        status = external_refs.status_of(
            _referenced_store("done"), REFERENCED_NAMESPACE, REFERENCED_PLAN, REFERENCED_KEY
        )

        assert status == "done"

    def test_should_return_unknown_for_a_missing_plan(self):
        store = _Store(_Records({}))

        status = external_refs.status_of(store, REFERENCED_NAMESPACE, REFERENCED_PLAN, REFERENCED_KEY)

        assert status == "unknown"

    def test_should_return_unknown_for_a_missing_story(self):
        status = external_refs.status_of(_referenced_store("done"), REFERENCED_NAMESPACE, REFERENCED_PLAN, "NOPE")

        assert status == "unknown"

    def test_should_return_unknown_for_a_non_string_status(self):
        manifest = {"stories": {REFERENCED_KEY: {"status": 7}}}
        store = _Store(_Records({(REFERENCED_NAMESPACE, REFERENCED_PLAN): manifest}))

        status = external_refs.status_of(store, REFERENCED_NAMESPACE, REFERENCED_PLAN, REFERENCED_KEY)

        assert status == "unknown"

    def test_should_return_unknown_when_the_namespace_is_not_served(self):
        class _Refusing(_Records):
            def get_manifest(self, namespace, plan_name):
                raise UnknownNamespaceError("unknown namespace")

        store = _Store(_Refusing({}))

        status = external_refs.status_of(store, REFERENCED_NAMESPACE, REFERENCED_PLAN, REFERENCED_KEY)

        assert status == "unknown"

    def test_should_only_read_never_write(self):
        records = _Records({(REFERENCED_NAMESPACE, REFERENCED_PLAN): {"stories": {REFERENCED_KEY: {"status": "done"}}}})

        external_refs.status_of(_Store(records), REFERENCED_NAMESPACE, REFERENCED_PLAN, REFERENCED_KEY)

        assert records.reads == [(REFERENCED_NAMESPACE, REFERENCED_PLAN)]
