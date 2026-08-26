"""Tests for the pipeline MCP server: request_decision/list_decisions, plan role_config discoverability, persona/model-aware dispatch, and plan-schema carry-through.

Split out of test_pipeline_mcp_server.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._pipeline_mcp_server_test_helpers.
"""
import fcntl
import json
import os

import httpx
import pytest

from pipeline import server as p
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _explode_plane,
    _fake_plane,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _NullSetStateProvider,
    _plane_configured,
    _plane_disabled,
    _read_manifest,
    _story,
    _write_manifest,
    plan_dir,
)


# ---------- Plan schema carry-through ----------
def test_save_plan_preserves_persona_model_risk(plan_dir):
    plan = {
        "epics": [{
            "summary": "E1",
            "stories": [_story(persona="security-engineer", model="opus", risk="high")],
        }]
    }
    p.save_plan("carry", json.dumps(plan))
    saved = json.loads((plan_dir / "carry.json").read_text())
    story = saved["epics"][0]["stories"][0]
    assert story["persona"] == "security-engineer"
    assert story["model"] == "opus"
    assert story["risk"] == "high"




def test_ingest_plan_carries_persona_model_risk_into_manifest(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(persona="security-engineer", model="opus", risk="high")],
        }]
    }
    (plan_dir / "ing.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing.manifest.json").read_text())
    story = manifest["stories"]["issue-1"]
    assert story["persona"] == "security-engineer"
    assert story["model"] == "opus"
    assert story["risk"] == "high"


def test_ingest_plan_carries_backend_into_manifest(plan_dir, monkeypatch, tmp_path):
    """A plan can pin a story's dispatch provider upfront (e.g. "mlx"), not
    just via a runtime escalation flip - _LOCAL_BACKEND_NAMES already
    includes ollama/lmstudio/mlx, so this is purely a plan-authoring gap."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(backend="mlx")]}],
    }
    (plan_dir / "ing2.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing2")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing2.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] == "mlx"


def test_ingest_plan_backend_defaults_to_none_when_omitted(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story()]}],
    }
    (plan_dir / "ing3.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing3")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing3.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] is None


def test_ingest_plan_rejects_unknown_backend_value(plan_dir, monkeypatch, tmp_path):
    """Fail closed on a typo'd backend name at ingest time rather than
    letting it reach dispatch_story and raise NotImplementedError deep
    inside get_backend."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(backend="some-typo")]}],
    }
    (plan_dir / "ing4.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing4")
    assert result["ok"] is False
    assert "some-typo" in result["error"]
    assert not (plan_dir / "ing4.manifest.json").exists()


def test_ingest_plan_accepts_auto_backend_value(plan_dir, monkeypatch, tmp_path):
    """"auto" is a valid story["backend"] value (resolved by
    _route_dispatch_backend before reaching get_backend), even though it's
    not a registered driver in backend._DRIVERS."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(backend="auto")]}],
    }
    (plan_dir / "ing5.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing5")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing5.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] == "auto"


def test_ingest_plan_reingest_refreshes_backend_field(plan_dir, monkeypatch, tmp_path):
    """"backend" must be included in _INGEST_AUTHORED_STORY_FIELDS so a
    re-ingest updates it, like persona/model/risk already do."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(key="S1", backend="ollama")]}],
    }
    (plan_dir / "ing6.json").write_text(json.dumps(plan))
    p.ingest_plan("ing6")

    plan["epics"][0]["stories"] = [_story(key="S1", backend="mlx")]
    (plan_dir / "ing6.json").write_text(json.dumps(plan))
    result = p.ingest_plan("ing6")

    assert result["ok"] is True
    manifest = json.loads((plan_dir / "ing6.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["backend"] == "mlx"


def test_ingest_plan_remaps_local_keys_to_issue_ids_in_dependencies(plan_dir, monkeypatch, tmp_path):
    issue_ids = iter(["issue-1", "issue-2", "issue-3"])
    monkeypatch.setattr(pt, "plane_request",
        lambda method, path, **kw: (
            _fake_plane(method, path, **kw) if not path.endswith("/work-items/")
            else {"id": next(issue_ids)}
        ),
    )
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [
                _story(key="S1"),
                _story(key="S2", dependencies=["S1"]),
                _story(key="S3", dependencies=["S1", "S2"]),
            ],
        }]
    }
    (plan_dir / "deps.json").write_text(json.dumps(plan))
    result = p.ingest_plan("deps")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "deps.manifest.json").read_text())
    stories = manifest["stories"]
    assert stories["issue-1"]["dependencies"] == []
    assert stories["issue-2"]["dependencies"] == ["issue-1"]
    assert stories["issue-3"]["dependencies"] == ["issue-1", "issue-2"]


def test_ingest_plan_leaves_unresolvable_dependency_keys_unchanged(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(key="S1", dependencies=["no-such-key"])],
        }]
    }
    (plan_dir / "dangling.json").write_text(json.dumps(plan))
    result = p.ingest_plan("dangling")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "dangling.manifest.json").read_text())
    assert manifest["stories"]["issue-1"]["dependencies"] == ["no-such-key"]


def test_ingest_plan_warns_on_isolation_only_acceptance_fixture(plan_dir, monkeypatch, tmp_path):
    """Non-blocking authoring nudge: a story whose instructions require
    wiring at a call site but whose acceptance fixture only invokes the unit
    directly should notify the user, without failing ingest. Root-caused
    live 2026-07-28 on harness-targeted-done-nudge -- see
    pipeline.build_detect._isolation_only_acceptance_warning."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(
                summary="wire the nudge at the call site",
                agent_instructions="Update the call site to pass content.",
                acceptance=[{
                    "path": "tests/test_nudge.py",
                    "source": "def test_x():\n    assert _no_tool_nudge(0)\n",
                }],
            )],
        }]
    }
    (plan_dir / "isowarn.json").write_text(json.dumps(plan))
    result = p.ingest_plan("isowarn")
    assert result["ok"] is True
    notifications = (plan_dir / "isowarn.notifications.log").read_text()
    assert "isolation-only" in notifications
    assert "wire the nudge at the call site" in notifications


def test_ingest_plan_no_warning_when_fixture_exercises_integration(plan_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(
                summary="wire the nudge at the call site",
                agent_instructions="Update the call site to pass content.",
                acceptance=[{
                    "path": "tests/test_nudge.py",
                    "source": "def test_x(monkeypatch):\n    rc = main()\n    assert rc == 0\n",
                }],
            )],
        }]
    }
    (plan_dir / "isook.json").write_text(json.dumps(plan))
    result = p.ingest_plan("isook")
    assert result["ok"] is True
    assert not (plan_dir / "isook.notifications.log").exists()


# ---------- Logging hygiene ----------
def test_http_loggers_are_quieted():
    """Importing the server caps httpx/httpcore at WARNING so the per-tick
    HTTP probes don't flood the unattended launchd logs at INFO."""
    import logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


# ---------- Plane optional (unconfigured) ----------
def test_plane_enabled_reflects_config(monkeypatch):
    assert p._plane_enabled() is True  # set by _plane_configured fixture
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")
    assert p._plane_enabled() is False




def test_ingest_plan_without_plane_skips_calls_and_keys_by_story_key(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [
                _story(key="S1"),
                _story(key="S2", dependencies=["S1"]),
            ],
        }]
    }
    (plan_dir / "noplane.json").write_text(json.dumps(plan))
    result = p.ingest_plan("noplane")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "noplane.manifest.json").read_text())
    # Stories are keyed by their plan key (no Plane UUID to key on), and
    # dependencies still resolve to those keys.
    assert set(manifest["stories"]) == {"S1", "S2"}
    assert manifest["stories"]["S2"]["dependencies"] == ["S1"]


def test_ingest_plan_without_plane_synthesizes_keys_when_absent(
    _plane_disabled, plan_dir, monkeypatch, tmp_path,
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E1", "stories": [_story(), _story()]}],
    }
    (plan_dir / "nokeys.json").write_text(json.dumps(plan))
    result = p.ingest_plan("nokeys")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "nokeys.manifest.json").read_text())
    assert len(manifest["stories"]) == 2  # two distinct synthetic keys


def test_ingest_plan_survives_plane_configured_but_unreachable(
    plan_dir, monkeypatch, tmp_path, capsys,
):
    # Plane IS configured here (the autouse _plane_configured fixture), so
    # get_ticket_provider() resolves to PlaneTicketProvider - but every call
    # fails at the connection level (host down, timeout, ...). ingest_plan
    # must still succeed by falling back to synthesized/local story keys,
    # exactly like the "Plane unconfigured" path does.
    monkeypatch.delenv("PIPELINE_TICKET_PROVIDER", raising=False)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "E1",
            "stories": [_story(key="S1"), _story(key="S2", dependencies=["S1"])],
        }]
    }
    (plan_dir / "unreachable.json").write_text(json.dumps(plan))
    result = p.ingest_plan("unreachable")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "unreachable.manifest.json").read_text())
    assert set(manifest["stories"]) == {"S1", "S2"}
    assert manifest["stories"]["S2"]["dependencies"] == ["S1"]
    # The epic itself failed to create too, so it must not appear.
    assert manifest["epics"] == {}
    # The failure must be surfaced, not silently swallowed forever.
    assert "Warning" in capsys.readouterr().out


def test_plane_set_state_noop_when_plane_disabled(_plane_disabled, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    assert p._plane_set_state("S1", "started") is True


def test_mark_story_done_without_plane_skips_patch(_plane_disabled, plan_dir, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    (plan_dir / "md.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "pr_open"}}}))
    result = p.mark_story_done("md", "S1")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "md.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "done"


def test_mark_story_in_progress_without_plane_skips_patch(_plane_disabled, plan_dir, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    (plan_dir / "mip.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "todo"}}}))
    result = p.mark_story_in_progress("mip", "S1")
    assert result["ok"] is True
    manifest = json.loads((plan_dir / "mip.manifest.json").read_text())
    assert manifest["stories"]["S1"]["status"] == "in_progress"


# ---------- TicketProvider abstraction ----------
# Plane is already optional (see tests above); these cover the pluggable
# TicketProvider seam: selection via PIPELINE_TICKET_PROVIDER, the Null/Plane
# providers, and the documented Jira stub.
def test_get_ticket_provider_auto_resolves_to_plane_when_configured(monkeypatch):
    monkeypatch.delenv("PIPELINE_TICKET_PROVIDER", raising=False)
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.PlaneTicketProvider)
    assert provider.enabled is True


def test_get_ticket_provider_auto_resolves_to_null_when_unconfigured(
    _plane_disabled, monkeypatch,
):
    monkeypatch.delenv("PIPELINE_TICKET_PROVIDER", raising=False)
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.NullTicketProvider)
    assert provider.enabled is False


def test_get_ticket_provider_none_forces_null_even_when_plane_configured(monkeypatch):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "none")
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.NullTicketProvider)


def test_get_ticket_provider_plane_forced_without_config_raises(
    _plane_disabled, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "plane")
    with pytest.raises(ValueError, match="PLANE_"):
        p.get_ticket_provider()


def test_get_ticket_provider_jira_returns_stub(monkeypatch):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "jira")
    provider = p.get_ticket_provider()
    assert isinstance(provider, p.JiraTicketProvider)


def test_get_ticket_provider_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="bogus"):
        p.get_ticket_provider()


def test_null_ticket_provider_every_op_is_a_noop():
    provider = p.NullTicketProvider()
    assert provider.enabled is False
    assert provider.create_epic("E1") is None
    assert provider.create_story("S1", "desc", None, "agent-pipeline") is None
    assert provider.set_state("S1", p.LogicalState.DONE) is True
    assert provider.resolve_key("S1") == "S1"


def test_plane_ticket_provider_create_epic_and_story_delegate_to_plane_request(
    monkeypatch,
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    provider = p.PlaneTicketProvider()
    assert provider.enabled is True
    epic_id = provider.create_epic("E1")
    assert epic_id == "epic-1"
    issue_id = provider.create_story("S1", "desc", epic_id, "agent-pipeline")
    assert issue_id == "issue-1"


def test_plane_ticket_provider_create_epic_falls_back_to_none_on_api_error(
    monkeypatch,
):
    # Epics are an optional Plane module; some instances don't expose it.
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("404")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_epic("E1") is None


def test_plane_ticket_provider_create_epic_falls_back_to_none_on_connection_error(
    monkeypatch,
):
    # A connection failure (Plane host unreachable, timeout, ...) is not a
    # RuntimeError like a non-2xx response - it must be caught too, not just
    # the "epics module unsupported" 404 case.
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_epic("E1") is None


def test_plane_ticket_provider_create_story_falls_back_to_none_on_connection_error(
    monkeypatch,
):
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_story("S1", "desc", None, "agent-pipeline") is None


def test_plane_ticket_provider_create_story_falls_back_to_none_on_api_error(
    monkeypatch,
):
    # create_story must tolerate a non-2xx RuntimeError the same way
    # create_epic already does, not just connection-level failures.
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("500")),
    )
    provider = p.PlaneTicketProvider()
    assert provider.create_story("S1", "desc", None, "agent-pipeline") is None


def test_plane_ticket_provider_create_story_returns_issue_id_when_epic_link_fails(
    monkeypatch, capsys,
):
    # The issue itself was created successfully; only the (optional) epic-
    # link call failed afterwards. Discarding issue_id here would orphan a
    # real Plane ticket (never referenced by the manifest) and cause
    # ingest_plan to create a duplicate issue on a retried ingest - the link
    # failure must degrade the link only, matching create_epic's "epic
    # support is optional" contract, not discard a real created id.
    def flaky_plane(method, path, **kwargs):
        if "/epics/" in path and path.endswith("/issues/"):
            raise httpx.ConnectError("connection refused")
        return _fake_plane(method, path, **kwargs)

    monkeypatch.setattr(pt, "plane_request", flaky_plane)
    provider = p.PlaneTicketProvider()
    issue_id = provider.create_story("S1", "desc", "epic-1", "agent-pipeline")
    assert issue_id == "issue-1"
    assert "Warning" in capsys.readouterr().out


def test_plane_ticket_provider_set_state_delegates_to_plane_set_state(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pt, "_plane_set_state",
        lambda key, group, plan_name=None: calls.append((key, group, plan_name)) or True,
    )
    provider = p.PlaneTicketProvider()
    assert provider.set_state("S1", p.LogicalState.IN_PROGRESS, "myplan") is True
    assert calls == [("S1", "started", "myplan")]


def test_plane_ticket_provider_resolve_key_delegates_to_resolve_issue_uuid(monkeypatch):
    monkeypatch.setattr(pt, "_resolve_issue_uuid", lambda key: f"resolved-{key}")
    provider = p.PlaneTicketProvider()
    assert provider.resolve_key("PIPE-7") == "resolved-PIPE-7"


def test_jira_ticket_provider_every_op_raises_not_implemented():
    provider = p.JiraTicketProvider()
    with pytest.raises(NotImplementedError):
        provider.create_epic("E1")
    with pytest.raises(NotImplementedError):
        provider.create_story("S1", "desc", None, "agent-pipeline")
    with pytest.raises(NotImplementedError):
        provider.set_state("S1", p.LogicalState.DONE)
    with pytest.raises(NotImplementedError):
        provider.resolve_key("S1")


def test_mark_story_in_progress_routes_through_ticket_provider(plan_dir, monkeypatch):
    calls = []

    class _FakeProvider:
        def set_state(self, story_key, state, plan_name=None):
            calls.append((story_key, state, plan_name))
            return True

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _FakeProvider())
    (plan_dir / "tpmip.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "todo"}}}))
    result = p.mark_story_in_progress("tpmip", "S1")
    assert result["ok"] is True
    assert calls == [("S1", p.LogicalState.IN_PROGRESS, "tpmip")]


def test_mark_story_done_routes_through_ticket_provider(plan_dir, monkeypatch):
    calls = []

    class _FakeProvider:
        def set_state(self, story_key, state, plan_name=None):
            calls.append((story_key, state, plan_name))
            return True

    monkeypatch.setattr(p, "get_ticket_provider", lambda: _FakeProvider())
    (plan_dir / "tpmd.manifest.json").write_text(json.dumps(
        {"stories": {"S1": {"status": "pr_open"}}}))
    result = p.mark_story_done("tpmd", "S1")
    assert result["ok"] is True
    assert calls == [("S1", p.LogicalState.DONE, "tpmd")]


# ---------- mark_story_done plan-completion signal ----------
# When the last remaining non-done story is marked done, mark_story_done must
# signal that the whole plan is complete via a plan_completed key plus the full
# list of story keys. When any story is still not 'done' (including 'parked' or
# any other non-'done' terminal-looking state), the return dict must keep
# today's exact {'ok': True} shape - no plan_completed key at all - so existing
# equality assertions keep passing and callers can use .get('plan_completed')
# truthiness or `'plan_completed' in result` either way.

def test_mark_story_done_signals_plan_completed_when_all_done(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc1", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    })
    result = p.mark_story_done("pc1", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    assert set(result["stories"]) == {"S1", "S2"}
    manifest = _read_manifest(plan_dir, "pc1")
    assert manifest["stories"]["S2"]["status"] == "done"


def test_mark_story_done_omits_plan_completed_when_other_story_not_done(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc2", {
        "S1": {"status": "todo"},
        "S2": {"status": "in_progress"},
    })
    result = p.mark_story_done("pc2", "S1")
    assert result["ok"] is True
    # Exact today's shape: no plan_completed key at all.
    assert "plan_completed" not in result
    assert "stories" not in result
    assert result == {"ok": True}
    manifest = _read_manifest(plan_dir, "pc2")
    assert manifest["stories"]["S1"]["status"] == "done"
    assert manifest["stories"]["S2"]["status"] == "in_progress"


def test_mark_story_done_single_story_plan_completed(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc3", {
        "S1": {"status": "todo"},
    })
    result = p.mark_story_done("pc3", "S1")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    assert result["stories"] == ["S1"]
    manifest = _read_manifest(plan_dir, "pc3")
    assert manifest["stories"]["S1"]["status"] == "done"


def test_mark_story_done_parked_story_does_not_count_as_done(plan_dir, monkeypatch):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest(plan_dir, "pc4", {
        "S1": {"status": "todo"},
        "S2": {"status": "parked", "parked_reason": "blocked"},
    })
    result = p.mark_story_done("pc4", "S1")
    assert result["ok"] is True
    # A 'parked' story is not 'done', so the plan is not complete.
    assert "plan_completed" not in result
    assert "stories" not in result
    assert result == {"ok": True}
    manifest = _read_manifest(plan_dir, "pc4")
    assert manifest["stories"]["S1"]["status"] == "done"
    assert manifest["stories"]["S2"]["status"] == "parked"


# ---------- mark_story_done retro PENDING.md tracking ----------
# When the last story of a plan whose top-level manifest repo_root equals this
# pipeline's own repo (PIPELINE_SELF_REPO_ROOT) completes, mark_story_done must
# append a line to RETRO_PENDING_PATH so a retrospective gets queued. Plans
# rooted in some other repo (external game/app plans dispatched through this
# pipeline) must NOT get a PENDING.md entry. The write must be idempotent and
# must only happen on the FINAL story (plan_completed), not every story.

@pytest.fixture
def retro_pending_path(tmp_path, monkeypatch):
    self_root = tmp_path / "self_repo"
    pending = self_root / "retros" / "PENDING.md"
    # raising=False so the fixture itself doesn't error before the
    # implementation adds these names; the test bodies then fail with
    # AssertionError on the missing behavior instead.
    monkeypatch.setattr(p, "PIPELINE_SELF_REPO_ROOT", self_root, raising=False)
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    return pending


def _write_manifest_with_repo_root(plan_dir, plan_name, stories, repo_root):
    """Like _write_manifest but adds a top-level repo_root key, which the
    retro-pending scoping rule keys off of."""
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps(
            {"epics": {}, "stories": stories, "repo_root": repo_root}, indent=2
        )
    )


def test_mark_story_done_records_retro_pending_for_self_repo_plan(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    self_root = p.PIPELINE_SELF_REPO_ROOT
    _write_manifest_with_repo_root(plan_dir, "rp1", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    }, str(self_root))
    result = p.mark_story_done("rp1", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    # PENDING.md must now exist and contain a line starting with the plan name.
    assert retro_pending_path.exists()
    content = retro_pending_path.read_text()
    lines = content.splitlines()
    matching = [ln for ln in lines if ln.startswith("- rp1 ")]
    assert len(matching) == 1
    # The line must carry the story count.
    assert "2 stories" in matching[0]


def test_mark_story_done_skips_retro_pending_for_non_self_repo_plan(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    _write_manifest_with_repo_root(plan_dir, "rp2", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    }, "/some/other/repo")
    result = p.mark_story_done("rp2", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    # External-repo plan: no PENDING.md entry at all.
    assert not retro_pending_path.exists()


def test_mark_story_done_skips_retro_pending_when_repo_root_absent(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    # No repo_root key at all -> not a self-repo plan.
    _write_manifest(plan_dir, "rp2b", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    })
    result = p.mark_story_done("rp2b", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    assert not retro_pending_path.exists()


def test_mark_story_done_retro_pending_is_idempotent(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    self_root = p.PIPELINE_SELF_REPO_ROOT
    # Pre-create PENDING.md with an existing line for this plan.
    retro_pending_path.parent.mkdir(parents=True, exist_ok=True)
    retro_pending_path.write_text("- myplan \u2014 completed 2026-01-01, 1 stories\n")
    _write_manifest_with_repo_root(plan_dir, "myplan", {
        "S1": {"status": "done"},
        "S2": {"status": "todo"},
    }, str(self_root))
    result = p.mark_story_done("myplan", "S2")
    assert result["ok"] is True
    assert result.get("plan_completed") is True
    content = retro_pending_path.read_text()
    lines = content.splitlines()
    matching = [ln for ln in lines if ln.startswith("- myplan ")]
    # Exactly one line — no duplicate appended.
    assert len(matching) == 1
    assert matching[0] == "- myplan \u2014 completed 2026-01-01, 1 stories"


def test_mark_story_done_no_retro_pending_write_when_plan_not_yet_complete(
    plan_dir, monkeypatch, retro_pending_path
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    self_root = p.PIPELINE_SELF_REPO_ROOT
    _write_manifest_with_repo_root(plan_dir, "rp4", {
        "S1": {"status": "todo"},
        "S2": {"status": "todo"},
    }, str(self_root))
    # Complete only one of two stories — plan not yet complete.
    result = p.mark_story_done("rp4", "S1")
    assert result["ok"] is True
    assert "plan_completed" not in result
    assert result == {"ok": True}
    # Must NOT have written a PENDING.md entry on a non-final story.
    assert not retro_pending_path.exists()


def test_pipeline_self_repo_root_and_retro_pending_path_constants_exist():
    # The two new module-level constants must exist and be Path objects.
    assert hasattr(p, "PIPELINE_SELF_REPO_ROOT")
    assert hasattr(p, "RETRO_PENDING_PATH")
    from pathlib import Path as _Path
    assert isinstance(p.PIPELINE_SELF_REPO_ROOT, _Path)
    assert isinstance(p.RETRO_PENDING_PATH, _Path)
    # RETRO_PENDING_PATH must be PIPELINE_SELF_REPO_ROOT / "retros" / "PENDING.md".
    assert p.RETRO_PENDING_PATH == p.PIPELINE_SELF_REPO_ROOT / "retros" / "PENDING.md"
    # PIPELINE_SELF_REPO_ROOT must resolve to this repo's own root (server.py
    # lives at pipeline/server.py, so parent.parent is the repo root).
    assert p.PIPELINE_SELF_REPO_ROOT == _Path(p.__file__).resolve().parent.parent


def test_record_retro_pending_helper_writes_expected_line(tmp_path, monkeypatch):
    # The _record_retro_pending helper must exist and write a single line.
    self_root = tmp_path / "self_repo"
    pending = self_root / "retros" / "PENDING.md"
    monkeypatch.setattr(p, "PIPELINE_SELF_REPO_ROOT", self_root, raising=False)
    monkeypatch.setattr(p, "RETRO_PENDING_PATH", pending, raising=False)
    assert hasattr(p, "_record_retro_pending")
    p._record_retro_pending("helperplan", 3)
    assert pending.exists()
    line = pending.read_text()
    assert line.startswith("- helperplan ")
    assert "3 stories" in line
    assert "completed" in line


# ---------- patch_story / set_story_status (T2) ----------
# These give a caller a sanctioned, lock-serialized way to edit a story's
# authored fields or transition its status, so nobody needs to hand-edit the
# manifest JSON directly - which races the 60s scheduler tick with no lock
# protecting the edit (2026-07-07 web-client-epic retro, §6).

def test_patch_story_updates_allowlisted_field_preserves_others(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps1", {
        "S1": {"summary": "Old summary", "agent_instructions": "Old.",
               "status": "todo", "dependencies": [], "model": "sonnet"},
    })
    result = p.patch_story("ps1", "S1", {"agent_instructions": "New, clarified."})
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ps1")
    assert manifest["stories"]["S1"]["agent_instructions"] == "New, clarified."
    assert manifest["stories"]["S1"]["status"] == "todo"
    assert manifest["stories"]["S1"]["model"] == "sonnet"


def test_patch_story_can_update_tdd_split_opt_in(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "pstdd", {
        "S1": {"summary": "s", "status": "todo", "tdd_split": False},
    })
    result = p.patch_story("pstdd", "S1", {"tdd_split": True})
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "pstdd")
    assert manifest["stories"]["S1"]["tdd_split"] is True


def test_patch_story_can_update_pr_url(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps2", {
        "S1": {"summary": "s", "status": "done"},
    })
    result = p.patch_story("ps2", "S1", {"pr_url": "https://example.com/pr/9"})
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ps2")
    assert manifest["stories"]["S1"]["pr_url"] == "https://example.com/pr/9"


def test_patch_story_rejects_field_outside_allowlist(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps3", {
        "S1": {"summary": "s", "status": "todo"},
    })
    result = p.patch_story("ps3", "S1", {"status": "done"})
    assert result["ok"] is False
    assert "status" in result["error"]
    manifest = _read_manifest(plan_dir, "ps3")
    assert manifest["stories"]["S1"]["status"] == "todo"


def test_patch_story_rejects_missing_story(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps4", {})
    result = p.patch_story("ps4", "no-such-key", {"model": "opus"})
    assert result["ok"] is False
    assert "no-such-key" in result["error"]


def test_patch_story_skips_when_lock_held(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ps5", {
        "S1": {"summary": "s", "status": "todo", "model": "sonnet"},
    })
    lock_path = plan_dir / "ps5.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.patch_story("ps5", "S1", {"model": "opus"})
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "ps5")
    assert manifest["stories"]["S1"]["model"] == "sonnet"


def test_set_story_status_updates_to_valid_status(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss1", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ss1", "S1", "interrupted")
    assert result["ok"] is True
    manifest = _read_manifest(plan_dir, "ss1")
    assert manifest["stories"]["S1"]["status"] == "interrupted"


def test_set_story_status_rejects_invalid_status(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss2", {
        "S1": {"summary": "s", "status": "parked"},
    })
    result = p.set_story_status("ss2", "S1", "definitely-not-a-status")
    assert result["ok"] is False
    assert "definitely-not-a-status" in result["error"]
    manifest = _read_manifest(plan_dir, "ss2")
    assert manifest["stories"]["S1"]["status"] == "parked"


def test_set_story_status_rejects_missing_story(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss3", {})
    result = p.set_story_status("ss3", "no-such-key", "todo")
    assert result["ok"] is False
    assert "no-such-key" in result["error"]


def test_set_story_status_skips_when_lock_held(plan_dir, monkeypatch):
    _write_manifest(plan_dir, "ss4", {
        "S1": {"summary": "s", "status": "parked"},
    })
    lock_path = plan_dir / "ss4.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = p.set_story_status("ss4", "S1", "interrupted")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert result["ok"] is True
    assert result.get("skipped") == "locked"
    manifest = _read_manifest(plan_dir, "ss4")
    assert manifest["stories"]["S1"]["status"] == "parked"


def test_ingest_plan_rejects_missing_repo_root(plan_dir, monkeypatch):
    """Without repo_root, advance_all_plans() falls back to the global
    REPO_ROOT for this plan - almost certainly the wrong repo (or a
    deliberately-broken sentinel, if one's configured to fail loudly rather
    than silently operate on the wrong repo). Catch it at ingest, not three
    silent merge-attempt failures later."""
    called = []
    monkeypatch.setattr(pt, "plane_request", lambda *a, **kw: called.append(1) or _fake_plane(*a, **kw))
    plan = {"epics": [{"summary": "E1", "stories": [_story()]}]}
    (plan_dir / "norepo.json").write_text(json.dumps(plan))

    result = p.ingest_plan("norepo")

    assert result["ok"] is False
    assert "repo_root" in result["error"]
    assert not (plan_dir / "norepo.manifest.json").exists()
    assert not called  # must fail before any Plane side effects


def test_ingest_plan_rejects_nonexistent_repo_root_directory(plan_dir, monkeypatch):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": "/nonexistent-repo-root-set-per-plan-only",
        "epics": [{"summary": "E1", "stories": [_story()]}],
    }
    (plan_dir / "badrepo.json").write_text(json.dumps(plan))

    result = p.ingest_plan("badrepo")

    assert result["ok"] is False
    assert "repo_root" in result["error"]
    assert not (plan_dir / "badrepo.manifest.json").exists()


