"""Tests for the ingest-time local-dispatch preflight gate (W1a).

``.claude/rules/local-dispatch-preflight.md`` requires a non-Claude dispatch
tier to carry a real ``Preflight:`` line in its brief: a conflict/impact run
against the base commit, so the executor does not discover a stale base or a
conflicting sibling change mid-dispatch. Nothing enforced it -- a plan author
had to remember, and a story dispatched to a local model without one burned a
dispatch attempt (and sometimes an escalation) before anyone noticed.

``_preflight_status`` / ``_preflight_dispatch_provider`` plus the gate inside
``_ingest_plan_impl``'s existing up-front validation loop close that gap:
a story whose resolved dispatch provider is not ``claude`` and whose brief has
no real ``Preflight:`` line is rejected before any ticket-provider call or
manifest write, unless the plan carries a ``preflight_override`` reason (an
emergency escape hatch, notified as ``event=preflight_override``).

These tests follow the fixture pattern of
``tests/unit/test_ingest_story_sizing_warning.py``: a local ``plan_dir``
fixture pointing ``p.PLAN_DIR`` at a ``tmp_path`` subdirectory, the shared
``_story``/``_fake_plane`` helpers, and end-to-end drives through
``pipeline.server.ingest_plan``.
"""

import json
import logging
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest: pipeline.ingest's module
# body imports pipeline.build_detect, which imports pipeline.server, which
# imports _ingest_plan_impl back from pipeline.ingest -- so importing
# pipeline.ingest first re-enters a partially-initialized pipeline.server.
import pipeline.server as p
from app import role_registry
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline import ticketing as pt
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _fake_plane,
    _isolate_usage_state,
    _plane_configured,
    _story,
)

# The rule file the rejection error must point the plan author at.
PREFLIGHT_RULE = ".claude/rules/local-dispatch-preflight.md"
OVERRIDE_EVENT = "preflight_override"
# A real preflight line, as the rule file prescribes.
GOOD_PREFLIGHT = "Preflight: impact run 2026-09-22 on abc123 — 0 conflicts"

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def preflight_plan_dir(tmp_path, monkeypatch):
    """Local copy of the shared ``plan_dir`` fixture.

    Importing the shared fixture and using it as a same-named test parameter
    is the standard pytest pattern, but ruff's F811 false positive is only
    ignored for the ``test_pipeline_mcp_server_*.py`` file pattern, so this
    file keeps its own copy under a distinct name.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches
    # must land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _explode_plane(*a, **kw):
    """Fail loudly if the ticket provider is reached: the gate must reject
    before any ticket-provider call."""
    raise AssertionError("plane_request must not be called for a rejected ingest")


def _write_plan(plan_dir, name, plan):
    path = plan_dir / f"{name}.json"
    path.write_text(json.dumps(plan))
    return path


def _manifest_path(plan_dir, name):
    return plan_dir / f"{name}.manifest.json"


def _plan(repo_root, stories, **plan_extra):
    plan = {
        "repo_root": str(repo_root),
        "epics": [{"summary": "E1", "stories": stories}],
    }
    plan.update(plan_extra)
    return plan


def _point_registry_at(monkeypatch, tmp_path, registry):
    """Point ``PIPELINE_MODEL_REGISTRY_PATH`` at a stub registry file.

    Never asserts against the live ``model_registry.json``: the operator's
    registry is config, not a fixture (see
    .claude/rules/testing-config-gates.md).
    """
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(path))
    role_registry.reset_registry_cache()
    return path


# ---------------------------------------------------------------------------
# _preflight_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "instructions",
    [None, "", "   ", "Do the thing with no preflight line at all."],
)
def test_preflight_status_is_missing_without_a_preflight_line(instructions):
    assert ingest_mod._preflight_status(instructions) == "missing"


def test_preflight_status_is_ok_for_a_real_preflight_line():
    assert ingest_mod._preflight_status(GOOD_PREFLIGHT) == "ok"


def test_preflight_status_is_ok_for_a_line_with_no_space_after_the_colon():
    assert ingest_mod._preflight_status("Preflight:done") == "ok"


def test_preflight_status_tolerates_leading_whitespace():
    """The prefix is matched against ``lstrip()``, so an indented line counts."""
    assert ingest_mod._preflight_status(f"    {GOOD_PREFLIGHT}") == "ok"


@pytest.mark.parametrize("remainder", ["", "   ", "\t"])
def test_preflight_status_is_missing_for_an_empty_remainder(remainder):
    assert ingest_mod._preflight_status(f"Preflight:{remainder}") == "missing"


@pytest.mark.parametrize(
    "line",
    [
        "Preflight: NOT RUN",
        "Preflight: not run",
        "Preflight: Not Run — no impact run against the base commit yet",
    ],
)
def test_preflight_status_is_not_run_for_a_not_run_remainder(line):
    """``NOT RUN`` is matched case-insensitively."""
    assert ingest_mod._preflight_status(line) == "not_run"


def test_preflight_status_finds_a_line_that_is_not_the_first_line():
    text = f"Some brief prose.\n\n{GOOD_PREFLIGHT}\n\nThen the task.\n"
    assert ingest_mod._preflight_status(text) == "ok"


def test_preflight_status_first_preflight_line_wins():
    text = f"Preflight: NOT RUN\n{GOOD_PREFLIGHT}\n"
    assert ingest_mod._preflight_status(text) == "not_run"


def test_preflight_status_prefix_is_case_sensitive():
    """A lowercase ``preflight:`` is not the documented prefix."""
    assert ingest_mod._preflight_status("preflight: done") == "missing"


def test_preflight_status_ignores_a_lowercase_line_before_a_real_one():
    text = f"preflight: done\n{GOOD_PREFLIGHT}\n"
    assert ingest_mod._preflight_status(text) == "ok"


# ---------------------------------------------------------------------------
# _preflight_dispatch_provider
# ---------------------------------------------------------------------------


def test_provider_prefers_the_story_backend():
    assert ingest_mod._preflight_dispatch_provider({"backend": "ollama"}, None) == (
        "ollama"
    )


def test_provider_lowercases_the_story_backend():
    assert ingest_mod._preflight_dispatch_provider({"backend": "OLLAMA"}, None) == (
        "ollama"
    )


def test_provider_returns_auto_verbatim():
    """``auto`` is gated: it may run locally."""
    assert ingest_mod._preflight_dispatch_provider({"backend": "auto"}, None) == "auto"


def test_provider_returns_claude_verbatim():
    assert ingest_mod._preflight_dispatch_provider({"backend": "claude"}, None) == (
        "claude"
    )


def test_provider_falls_through_an_empty_story_backend_to_the_plan_role_config():
    story = {"backend": ""}
    cfg = {"dispatch": {"provider": "ollama"}}
    assert ingest_mod._preflight_dispatch_provider(story, cfg) == "ollama"


def test_provider_uses_the_plan_role_config_when_the_story_is_silent():
    cfg = {"dispatch": {"provider": "MLX"}}
    assert ingest_mod._preflight_dispatch_provider({}, cfg) == "mlx"


def test_provider_uses_the_env_var_when_story_and_plan_are_silent(
    monkeypatch, tmp_path
):
    _point_registry_at(monkeypatch, tmp_path, {})
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    assert ingest_mod._preflight_dispatch_provider({}, None) == "ollama"


def test_provider_uses_the_registry_when_everything_else_is_silent(
    monkeypatch, tmp_path
):
    _point_registry_at(
        monkeypatch,
        tmp_path,
        {
            "providers": {"ollama": {"models": {}}},
            "roles": {"dispatch": {"provider": "ollama"}},
        },
    )
    assert ingest_mod._preflight_dispatch_provider({}, None) == "ollama"


def test_provider_defaults_to_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    _point_registry_at(monkeypatch, tmp_path, {})
    assert ingest_mod._preflight_dispatch_provider({}, None) == "claude"


def test_provider_tolerates_a_none_plan_role_config(monkeypatch, tmp_path):
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    _point_registry_at(monkeypatch, tmp_path, {})
    assert ingest_mod._preflight_dispatch_provider({}, None) == "claude"


@pytest.mark.parametrize(
    "cfg",
    [{"dispatch": None}, {"dispatch": {}}, {"dispatch": {"provider": ""}}, {}],
)
def test_provider_tolerates_a_missing_or_empty_dispatch_entry(
    monkeypatch, tmp_path, cfg
):
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    _point_registry_at(monkeypatch, tmp_path, {})
    assert ingest_mod._preflight_dispatch_provider({}, cfg) == "claude"


@pytest.mark.parametrize("registry", [{"roles": None}, {"roles": {"dispatch": None}}])
def test_provider_tolerates_a_none_registry_entry(monkeypatch, registry):
    """A registry whose ``roles``/``roles.dispatch`` entry is ``None`` must
    degrade to the default rather than raising."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **kw: registry)
    assert ingest_mod._preflight_dispatch_provider({}, None) == "claude"


# ---------------------------------------------------------------------------
# Positive: the gate admits these ingests
# ---------------------------------------------------------------------------


def test_ollama_story_with_a_preflight_line_is_accepted(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = _plan(
        tmp_path,
        [
            _story(
                backend="ollama",
                agent_instructions=f"{GOOD_PREFLIGHT}\n\nDo the thing.",
            )
        ],
    )
    _write_plan(preflight_plan_dir, "ok", plan)

    result = p.ingest_plan("ok")

    assert result["ok"] is True
    assert _manifest_path(preflight_plan_dir, "ok").exists()


def test_ollama_story_with_a_preflight_line_below_the_first_line_is_accepted(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = _plan(
        tmp_path,
        [
            _story(
                backend="ollama",
                agent_instructions=f"Brief prose first.\n\n{GOOD_PREFLIGHT}\n",
            )
        ],
    )
    _write_plan(preflight_plan_dir, "ok2", plan)

    result = p.ingest_plan("ok2")

    assert result["ok"] is True
    assert _manifest_path(preflight_plan_dir, "ok2").exists()


def test_claude_story_without_a_preflight_line_is_accepted(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = _plan(
        tmp_path,
        [_story(backend="claude", agent_instructions="No preflight line here.")],
    )
    _write_plan(preflight_plan_dir, "claude", plan)

    result = p.ingest_plan("claude")

    assert result["ok"] is True
    assert _manifest_path(preflight_plan_dir, "claude").exists()


def test_claude_story_saying_not_run_is_still_accepted(
    preflight_plan_dir, monkeypatch, tmp_path
):
    """Claude-provider stories are never gated, whatever their brief says."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = _plan(
        tmp_path,
        [_story(backend="claude", agent_instructions="Preflight: NOT RUN\nDo it.")],
    )
    _write_plan(preflight_plan_dir, "claude2", plan)

    result = p.ingest_plan("claude2")

    assert result["ok"] is True
    assert _manifest_path(preflight_plan_dir, "claude2").exists()


def test_story_with_no_backend_defaults_to_claude_and_is_accepted(
    preflight_plan_dir, monkeypatch, tmp_path
):
    """With no story backend, no plan role_config, no env var and no registry
    entry the provider resolves to ``claude``, so nothing is gated."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    _point_registry_at(monkeypatch, tmp_path, {})
    plan = _plan(tmp_path, [_story(agent_instructions="No preflight line here.")])
    _write_plan(preflight_plan_dir, "default", plan)

    result = p.ingest_plan("default")

    assert result["ok"] is True
    assert _manifest_path(preflight_plan_dir, "default").exists()


def test_preflight_override_admits_a_not_run_story_and_notifies_once(
    preflight_plan_dir, monkeypatch, tmp_path, caplog
):
    # Force the no-op ticket provider and give the story an explicit ``key``,
    # so the plan's local key and the manifest key are the same string: the
    # notice's key prefix is then unambiguous.
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "none")
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    notices = []
    manifest_written_at_notify = []

    def _capture(plan, msg, **kw):
        notices.append((plan, msg, kw))
        # The override notice is emitted next to the existing advisory
        # notifications, i.e. after the manifest write.
        manifest_written_at_notify.append(
            _manifest_path(preflight_plan_dir, "ovr").exists()
        )

    monkeypatch.setattr(ingest_mod, "_notify_user", _capture)
    plan = _plan(
        tmp_path,
        [
            _story(
                key="S1",
                backend="ollama",
                agent_instructions="Preflight: NOT RUN\nDo it.",
            )
        ],
        preflight_override="emergency: hotfix for the release",
    )
    _write_plan(preflight_plan_dir, "ovr", plan)

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        result = p.ingest_plan("ovr")

    assert result["ok"] is True
    assert _manifest_path(preflight_plan_dir, "ovr").exists()

    assert len(notices) == 1
    plan_name, msg, kw = notices[0]
    assert plan_name == "ovr"
    assert kw.get("event") == OVERRIDE_EVENT
    assert "preflight gate overridden" in msg
    assert "not_run" in msg
    assert "emergency: hotfix for the release" in msg
    assert msg.startswith("S1:")
    assert manifest_written_at_notify == [True]

    assert any(
        "preflight gate overridden" in record.getMessage()
        and "emergency: hotfix for the release" in record.getMessage()
        for record in caplog.records
    )


def test_preflight_override_notifies_once_per_admitted_story(
    preflight_plan_dir, monkeypatch, tmp_path
):
    # Force the no-op ticket provider so each story's own ``key`` becomes its
    # manifest key (the fake Plane provider returns one shared id for every
    # story, which would collapse them into a single manifest entry).
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "none")
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    notices = []
    monkeypatch.setattr(
        ingest_mod,
        "_notify_user",
        lambda plan, msg, **kw: notices.append((plan, msg, kw)),
    )
    plan = _plan(
        tmp_path,
        [
            _story(
                summary="One",
                key="S1",
                backend="ollama",
                agent_instructions="Preflight: NOT RUN\nDo it.",
            ),
            _story(
                summary="Two",
                key="S2",
                backend="ollama",
                agent_instructions="No preflight line here.",
            ),
        ],
        preflight_override="emergency: two stories",
    )
    _write_plan(preflight_plan_dir, "ovr4", plan)

    result = p.ingest_plan("ovr4")

    assert result["ok"] is True
    overrides = [n for n in notices if n[2].get("event") == OVERRIDE_EVENT]
    assert len(overrides) == 2
    assert {msg.split(":")[0] for _, msg, _ in overrides} == {"S1", "S2"}
    assert all("emergency: two stories" in msg for _, msg, _ in overrides)


def test_preflight_override_is_silent_for_a_story_that_is_not_gated(
    preflight_plan_dir, monkeypatch, tmp_path
):
    """An override only records the stories it actually admitted."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    notices = []
    monkeypatch.setattr(
        ingest_mod,
        "_notify_user",
        lambda plan, msg, **kw: notices.append((plan, msg, kw)),
    )
    plan = _plan(
        tmp_path,
        [
            _story(
                backend="ollama",
                agent_instructions=f"{GOOD_PREFLIGHT}\nDo it.",
            )
        ],
        preflight_override="not needed here",
    )
    _write_plan(preflight_plan_dir, "ovr2", plan)

    result = p.ingest_plan("ovr2")

    assert result["ok"] is True
    assert [n for n in notices if n[2].get("event") == OVERRIDE_EVENT] == []


def test_preflight_override_is_silent_for_a_claude_story(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    notices = []
    monkeypatch.setattr(
        ingest_mod,
        "_notify_user",
        lambda plan, msg, **kw: notices.append((plan, msg, kw)),
    )
    plan = _plan(
        tmp_path,
        [_story(backend="claude", agent_instructions="No preflight line here.")],
        preflight_override="not needed here",
    )
    _write_plan(preflight_plan_dir, "ovr3", plan)

    result = p.ingest_plan("ovr3")

    assert result["ok"] is True
    assert [n for n in notices if n[2].get("event") == OVERRIDE_EVENT] == []


# ---------------------------------------------------------------------------
# Negative: the gate rejects these ingests before any side effect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "instructions, phrase",
    [
        ("Do the thing with no preflight line.", "has no `Preflight:` line"),
        ("Preflight: NOT RUN\n\nDo the thing.", "says `Preflight: NOT RUN`"),
        ("Preflight:\n\nDo the thing.", "has no `Preflight:` line"),
        ("Preflight:    \n\nDo the thing.", "has no `Preflight:` line"),
        ("preflight: done\n\nDo the thing.", "has no `Preflight:` line"),
    ],
)
def test_gated_story_without_a_real_preflight_line_is_rejected(
    preflight_plan_dir, monkeypatch, tmp_path, instructions, phrase
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(
        tmp_path,
        [
            _story(
                summary="Do the thing",
                backend="ollama",
                agent_instructions=instructions,
            )
        ],
    )
    _write_plan(preflight_plan_dir, "gate", plan)

    result = p.ingest_plan("gate")

    assert result["ok"] is False
    error = result["error"]
    assert PREFLIGHT_RULE in error
    assert repr("Do the thing") in error
    assert "ollama" in error
    assert phrase in error
    assert "preflight_override" in error
    assert not _manifest_path(preflight_plan_dir, "gate").exists()


def test_auto_backend_is_gated(preflight_plan_dir, monkeypatch, tmp_path):
    """``auto`` may run locally, so it needs a preflight line too."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(
        tmp_path,
        [_story(backend="auto", agent_instructions="No preflight line here.")],
    )
    _write_plan(preflight_plan_dir, "auto", plan)

    result = p.ingest_plan("auto")

    assert result["ok"] is False
    assert "auto" in result["error"]
    assert PREFLIGHT_RULE in result["error"]
    assert not _manifest_path(preflight_plan_dir, "auto").exists()


def test_plan_role_config_dispatch_provider_gates_a_story_with_no_backend(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(
        tmp_path,
        [_story(agent_instructions="No preflight line here.")],
        role_config={"dispatch": {"provider": "ollama"}},
    )
    _write_plan(preflight_plan_dir, "rolecfg", plan)

    result = p.ingest_plan("rolecfg")

    assert result["ok"] is False
    assert "ollama" in result["error"]
    assert PREFLIGHT_RULE in result["error"]
    assert not _manifest_path(preflight_plan_dir, "rolecfg").exists()


def test_env_dispatch_provider_gates_a_story_with_no_backend(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    plan = _plan(tmp_path, [_story(agent_instructions="No preflight line here.")])
    _write_plan(preflight_plan_dir, "env", plan)

    result = p.ingest_plan("env")

    assert result["ok"] is False
    assert "ollama" in result["error"]
    assert PREFLIGHT_RULE in result["error"]
    assert not _manifest_path(preflight_plan_dir, "env").exists()


def test_registry_dispatch_provider_gates_a_story_with_no_backend(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)
    _point_registry_at(
        monkeypatch,
        tmp_path,
        {
            "providers": {"ollama": {"models": {}}},
            "roles": {"dispatch": {"provider": "ollama"}},
        },
    )
    plan = _plan(tmp_path, [_story(agent_instructions="No preflight line here.")])
    _write_plan(preflight_plan_dir, "registry", plan)

    result = p.ingest_plan("registry")

    assert result["ok"] is False
    assert "ollama" in result["error"]
    assert PREFLIGHT_RULE in result["error"]
    assert not _manifest_path(preflight_plan_dir, "registry").exists()


def test_one_gated_story_rejects_the_whole_plan(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(
        tmp_path,
        [
            _story(
                summary="Fine",
                backend="ollama",
                agent_instructions=f"{GOOD_PREFLIGHT}\nDo it.",
            ),
            _story(
                summary="Gated",
                backend="ollama",
                agent_instructions="No preflight line here.",
            ),
        ],
    )
    _write_plan(preflight_plan_dir, "mixed", plan)

    result = p.ingest_plan("mixed")

    assert result["ok"] is False
    assert repr("Gated") in result["error"]
    assert not _manifest_path(preflight_plan_dir, "mixed").exists()


def test_gate_respects_only_epics(preflight_plan_dir, monkeypatch, tmp_path):
    """The gate lives in the existing validation loop, which skips epics the
    caller did not select."""
    monkeypatch.setattr(pt, "plane_request", _fake_plane)
    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    _story(
                        summary="Gated",
                        backend="ollama",
                        agent_instructions="No preflight line here.",
                    )
                ],
            },
            {
                "summary": "E2",
                "stories": [
                    _story(
                        summary="Fine",
                        backend="claude",
                        agent_instructions="No preflight line here.",
                    )
                ],
            },
        ],
    }
    _write_plan(preflight_plan_dir, "only", plan)

    result = p.ingest_plan("only", only_epics=["E2"])

    assert result["ok"] is True
    assert _manifest_path(preflight_plan_dir, "only").exists()


# ---------------------------------------------------------------------------
# Negative: a malformed preflight_override is rejected before any side effect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", True, 1])
def test_malformed_preflight_override_is_rejected(
    preflight_plan_dir, monkeypatch, tmp_path, bad
):
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(
        tmp_path,
        [_story(backend="ollama", agent_instructions="Preflight: NOT RUN\nDo it.")],
        preflight_override=bad,
    )
    _write_plan(preflight_plan_dir, "badovr", plan)

    result = p.ingest_plan("badovr")

    assert result["ok"] is False
    assert "non-empty reason string" in result["error"]
    assert not _manifest_path(preflight_plan_dir, "badovr").exists()


def test_malformed_preflight_override_is_rejected_even_for_a_claude_story(
    preflight_plan_dir, monkeypatch, tmp_path
):
    """The override is validated up front, before the per-story gate."""
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    plan = _plan(
        tmp_path,
        [_story(backend="claude", agent_instructions="No preflight line here.")],
        preflight_override="",
    )
    _write_plan(preflight_plan_dir, "badovr2", plan)

    result = p.ingest_plan("badovr2")

    assert result["ok"] is False
    assert "non-empty reason string" in result["error"]
    assert not _manifest_path(preflight_plan_dir, "badovr2").exists()


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


def test_reference_documents_the_preflight_ingest_gate():
    lines = (_REPO_ROOT / "REFERENCE.md").read_text().splitlines()
    assert "## Preflight ingest gate" in lines
    gate_idx = lines.index("## Preflight ingest gate")
    accept_idx = lines.index("## Acceptance fixture grading")
    assert gate_idx < accept_idx
    section = "\n".join(lines[gate_idx:accept_idx])
    assert "preflight_override" in section
    assert "Preflight:" in section


def test_story_schema_rule_documents_preflight_override():
    lines = (
        (_REPO_ROOT / ".claude/rules/pipeline-story-schema.md").read_text().splitlines()
    )
    role_idx = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("- `role_config` (optional, plan-level")
    )
    ovr_idx = next(i for i, line in enumerate(lines) if "preflight_override" in line)
    # Ordering relative to the fixed role_config anchor, not the section's
    # total contents: later stories extend this list too.
    assert ovr_idx > role_idx
    # The bullet may wrap, so grade a small window starting at its first line.
    bullet = "\n".join(lines[ovr_idx : ovr_idx + 6])
    assert "non-empty" in bullet
    assert "reject" in bullet.lower()


# ---------------------------------------------------------------------------
# RPT-4: the override notice must carry the admitted story's key
# ---------------------------------------------------------------------------


def test_preflight_override_notice_carries_the_story_key(
    preflight_plan_dir, monkeypatch, tmp_path
):
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "none")
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    notices = []

    def _capture(plan, msg, **kw):
        notices.append((plan, msg, kw))

    monkeypatch.setattr(ingest_mod, "_notify_user", _capture)
    plan = _plan(
        tmp_path,
        [
            _story(
                key="S1",
                backend="ollama",
                agent_instructions="Preflight: NOT RUN\nDo it.",
            )
        ],
        preflight_override="emergency: hotfix for the release",
    )
    _write_plan(preflight_plan_dir, "ovrkey", plan)

    result = p.ingest_plan("ovrkey")

    assert result["ok"] is True
    overrides = [n for n in notices if n[2].get("event") == OVERRIDE_EVENT]
    assert len(overrides) == 1
    plan_name, msg, kw = overrides[0]
    assert plan_name == "ovrkey"
    # The record names the admitted story instead of landing in the synthetic
    # "<uncorrelated>" bucket.
    assert kw.get("story_key") == "S1"
    assert msg.startswith("S1:")


def test_preflight_override_notice_carries_the_story_key_without_a_correlation_id(
    preflight_plan_dir, monkeypatch, tmp_path
):
    """The boundary the fix is for: no correlation_id, still a story_key."""
    monkeypatch.setenv("PIPELINE_TICKET_PROVIDER", "none")
    monkeypatch.setattr(pt, "plane_request", _explode_plane)
    notices = []

    def _capture(plan, msg, **kw):
        notices.append((plan, msg, kw))

    monkeypatch.setattr(ingest_mod, "_notify_user", _capture)
    story = _story(
        key="S2",
        backend="ollama",
        agent_instructions="Preflight: NOT RUN\nDo it.",
    )
    assert "correlation_id" not in story
    plan = _plan(
        tmp_path,
        [story],
        preflight_override="emergency: hotfix for the release",
    )
    _write_plan(preflight_plan_dir, "ovrkey2", plan)

    result = p.ingest_plan("ovrkey2")

    assert result["ok"] is True
    overrides = [n for n in notices if n[2].get("event") == OVERRIDE_EVENT]
    assert len(overrides) == 1
    _plan_name, _msg, kw = overrides[0]
    assert kw.get("story_key") == "S2"
    assert kw.get("correlation_id") is None


def test_preflight_override_notify_call_passes_the_loop_key_as_story_key():
    """The ``preflight_override`` call site passes ``story_key=key``."""
    source = Path(ingest_mod.__file__).read_text()
    idx = source.index(f'event="{OVERRIDE_EVENT}"')
    start = source.rindex("_notify_user(", 0, idx)
    end = source.index(")", idx)
    call = source[start : end + 1]
    assert "story_key=key" in call.replace(" ", ""), call
