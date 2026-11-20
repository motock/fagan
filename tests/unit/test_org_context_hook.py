"""The org context provider seam (``PIPELINE_ORG_CONTEXT_PLUGIN``).

With the variable unset no provider is consulted and ingest is unchanged. With a
provider selected, its sources are merged after the ``PIPELINE_CONTEXT_CONFIG``
file's at the org level, and any failure refuses the ingest.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (circular import order).
import pipeline.server as p
from app.plugin_loader import ALLOWLIST_ENV
from pipeline import ingest as ingest_mod
from pipeline import org_context_hook
from pipeline import persistence as ppers
from pipeline.org_context_hook import (
    ORG_CONTEXT_PLUGIN_ENV,
    PROVIDER_ERROR,
    plugin_org_sources,
)

NAMESPACE = "team-a"

_PROVIDER = """\
RECEIVED = []


class Provider:
    def org_sources(self, namespace):
        RECEIVED.append(namespace)
@@BODY@@


def build():
    return Provider()
"""

RETURN_ONE = """
        return [{"name": "org-ux", "provider": "dir", "uri": "/mnt/org/ux", "phases": ["dispatch"]}]"""

RETURN_EMPTY = """
        return []"""

RAISE = """
        raise RuntimeError("secret token sk-live-123")"""

NON_LIST = """
        return {"name": "org-ux"}"""

INVALID_SOURCE = """
        return [{"name": "org-ux", "provider": "git", "uri": "/mnt/org/ux", "phases": ["dispatch"]}]"""


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    *,
    allowlist: str | None = None,
) -> str:
    """Write a fresh provider module, select it, and return its module name."""
    name = "org_context_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(_PROVIDER.replace("@@BODY@@", body), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(ORG_CONTEXT_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name if allowlist is None else allowlist)
    return name


@pytest.fixture(autouse=True)
def _no_plugin_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ORG_CONTEXT_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    records: list[dict] = []

    def _record(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(org_context_hook, "append_host_audit", _record)
    return records


class TestNoPlugin:
    def test_should_return_no_sources_and_import_nothing_without_a_plugin(
        self, monkeypatch, audits
    ) -> None:
        def _fail(seam, *args, **kwargs):
            raise AssertionError("no plugin may be loaded")

        monkeypatch.setattr(org_context_hook, "load_plugin", _fail)

        assert plugin_org_sources(NAMESPACE) == ([], None)
        assert audits == []


class TestAllowlist:
    def test_should_refuse_a_plugin_outside_the_allowlist_before_importing_it(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        name = _install(tmp_path, monkeypatch, RETURN_ONE, allowlist="some_other_module")

        assert plugin_org_sources(NAMESPACE) == ([], PROVIDER_ERROR)
        assert name not in sys.modules
        assert audits[0]["detail"]["reason"] == "hook_error"


class TestProviderCall:
    def test_should_pass_the_bound_namespace_to_the_provider(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        name = _install(tmp_path, monkeypatch, RETURN_ONE)

        plugin_org_sources(NAMESPACE)

        assert sys.modules[name].RECEIVED == [NAMESPACE]

    def test_should_return_the_providers_valid_sources(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install(tmp_path, monkeypatch, RETURN_ONE)

        sources, error = plugin_org_sources(NAMESPACE)

        assert error is None
        assert [s["name"] for s in sources] == ["org-ux"]
        assert audits == []

    def test_should_accept_an_empty_list_as_no_sources(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install(tmp_path, monkeypatch, RETURN_EMPTY)

        assert plugin_org_sources(NAMESPACE) == ([], None)
        assert audits == []


class TestFailClosed:
    def test_should_fail_closed_when_the_provider_raises(
        self, tmp_path, monkeypatch, audits, caplog
    ) -> None:
        _install(tmp_path, monkeypatch, RAISE)

        sources, error = plugin_org_sources(NAMESPACE)

        assert (sources, error) == ([], PROVIDER_ERROR)
        assert "sk-live-123" not in error
        assert "sk-live-123" not in caplog.text
        assert audits[0]["action"] == "org_context_refused"
        assert audits[0]["outcome"] == "refused"
        assert audits[0]["detail"] == {"reason": "hook_error"}

    def test_should_fail_closed_on_a_non_list_result(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install(tmp_path, monkeypatch, NON_LIST)

        assert plugin_org_sources(NAMESPACE) == ([], PROVIDER_ERROR)
        assert audits[0]["detail"] == {"reason": "invalid_sources"}

    def test_should_fail_closed_on_an_invalid_source(
        self, tmp_path, monkeypatch, audits
    ) -> None:
        _install(tmp_path, monkeypatch, INVALID_SOURCE)

        assert plugin_org_sources(NAMESPACE) == ([], PROVIDER_ERROR)
        assert audits[0]["detail"] == {"reason": "invalid_sources"}

    def test_should_keep_the_decision_when_the_audit_append_fails(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        _install(tmp_path, monkeypatch, RAISE)

        def _broken(**kwargs):
            raise OSError("audit disk full")

        monkeypatch.setattr(org_context_hook, "append_host_audit", _broken)

        assert plugin_org_sources(NAMESPACE) == ([], PROVIDER_ERROR)
        assert any(r.levelname == "WARNING" for r in caplog.records)


# ---------- ingest ----------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch, audits):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    monkeypatch.delenv("PIPELINE_CONTEXT_CONFIG", raising=False)
    wt = tmp_path / "worktrees"
    wt.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", wt)

    class _Provider:
        enabled = False

        def create_epic(self, summary):
            return None

        def create_story(self, summary, description, epic_id, agent):
            return None

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _Provider())
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    return p.PLAN_DIR


def _source(**overrides) -> dict:
    source = {
        "name": "org-ux",
        "provider": "dir",
        "uri": "/mnt/org/ux",
        "phases": ["dispatch"],
    }
    source.update(overrides)
    return source


def _repo(tmp_path: Path, fagan: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".fagan.json").write_text(json.dumps({"test_cmd": ["true"], **(fagan or {})}))
    return repo


def _org_file(tmp_path: Path, monkeypatch, sources: list[dict]) -> None:
    path = tmp_path / "org-context.json"
    path.write_text(json.dumps({"sources": sources}))
    monkeypatch.setenv("PIPELINE_CONTEXT_CONFIG", str(path))


def _write_plan(plan_dir: Path, repo_root: Path, context=None) -> None:
    story = {"summary": "s1", "agent_instructions": "Do it.", "key": "OC-1"}
    plan = {
        "repo_root": str(repo_root),
        "epics": [{"summary": "E1", "stories": [story]}],
    }
    if context is not None:
        plan["context"] = context
    (plan_dir / "ocplan.json").write_text(json.dumps(plan))


def _ingest() -> dict:
    return ingest_mod._ingest_plan_impl("ocplan", None, False)


def _sources(plan_dir: Path) -> dict:
    manifest = json.loads((plan_dir / "ocplan.manifest.json").read_text())
    return {s["name"]: s for s in manifest["context"]["sources"]}


def _provider_returning(tmp_path, monkeypatch, sources: list[dict]) -> None:
    body = f"""
        return {json.dumps(sources)}"""
    _install(tmp_path, monkeypatch, body)


class TestIngest:
    def test_should_ingest_unchanged_without_a_provider(
        self, plan_dir, tmp_path, monkeypatch
    ) -> None:
        _org_file(tmp_path, monkeypatch, [_source(name="from-file")])
        _write_plan(plan_dir, _repo(tmp_path))

        assert _ingest()["ok"] is True

        assert list(_sources(plan_dir)) == ["from-file"]

    def test_should_merge_provider_sources_after_file_sources_at_org_level(
        self, plan_dir, tmp_path, monkeypatch
    ) -> None:
        _org_file(tmp_path, monkeypatch, [_source(name="from-file")])
        _provider_returning(tmp_path, monkeypatch, [_source(name="from-provider")])
        _write_plan(plan_dir, _repo(tmp_path))

        assert _ingest()["ok"] is True

        sources = _sources(plan_dir)
        assert list(sources) == ["from-file", "from-provider"]
        assert sources["from-file"]["origin"] == "org"
        assert sources["from-provider"]["origin"] == "org"

    def test_should_refuse_ingest_when_the_provider_fails(
        self, plan_dir, tmp_path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch, RAISE)
        _write_plan(plan_dir, _repo(tmp_path))

        result = _ingest()

        assert result["ok"] is False
        assert PROVIDER_ERROR in result["error"]
        assert not (plan_dir / "ocplan.manifest.json").exists()

    def test_should_refuse_a_name_declared_by_both(
        self, plan_dir, tmp_path, monkeypatch
    ) -> None:
        _org_file(tmp_path, monkeypatch, [_source(name="shared")])
        _provider_returning(tmp_path, monkeypatch, [_source(name="shared")])
        _write_plan(plan_dir, _repo(tmp_path))

        result = _ingest()

        assert result["ok"] is False
        assert (
            "org context source name 'shared' is declared by both "
            "PIPELINE_CONTEXT_CONFIG and the org context provider" in result["error"]
        )
        assert not (plan_dir / "ocplan.manifest.json").exists()

    def test_should_let_a_plan_source_override_a_provider_source_of_the_same_name(
        self, plan_dir, tmp_path, monkeypatch
    ) -> None:
        _provider_returning(tmp_path, monkeypatch, [_source(uri="/mnt/org")])
        _write_plan(plan_dir, _repo(tmp_path), {"sources": [_source(uri="/mnt/plan")]})

        assert _ingest()["ok"] is True

        source = _sources(plan_dir)["org-ux"]
        assert source["uri"] == "/mnt/plan"
        assert source["origin"] == "plan"
