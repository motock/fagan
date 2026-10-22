"""Unit tests for the guided-setup orchestration (``pipeline.setup.wizard``).

Scope: ``build_plan``, ``run_plan`` and ``render_summary`` only. Every host
effect is injected, so nothing here touches the network, a real service, or
the operator's ``~/.claude``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.setup import wizard
from pipeline.setup.policy import describe_autonomy

SECTION_ORDER = (
    "core",
    "routing",
    "policy",
    "notifications",
    "integrations",
    "claude_assets",
    "services",
)

SECRET_SENTINEL = "s3cr3t-sentinel-value"

REGISTRY = {
    "providers": {
        "claude": {
            "models": {
                "sonnet": {"tag": "sonnet"},
                "opus": {"tag": "opus"},
            }
        },
        "ollama": {
            "models": {
                "gpt-oss": {"tag": "gpt-oss:20b"},
                "glm": {"tag": "glm-5.3-flash:cloud"},
            }
        },
    }
}

REGISTRY_WITHOUT_CLAUDE = {
    "providers": {"ollama": {"models": {"gpt-oss": {"tag": "gpt-oss:20b"}}}}
}


def make_args(**overrides):
    """A namespace shaped exactly like the CLI hands ``build_plan``."""
    base = {
        "yes": False,
        "dry_run": False,
        "set": [],
        "skip": set(),
        "repo_root": None,
        "data_dir": None,
        "port": None,
        "autonomy": None,
        "role": None,
        "provider": None,
        "model": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_state(env=None, roles=None, data=None):
    return {
        "env": {} if env is None else dict(env),
        "registry": {
            "data": REGISTRY if data is None else data,
            "roles": {} if roles is None else dict(roles),
        },
    }


def section(plan, name):
    for entry in plan["sections"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"section {name!r} missing from the plan")


class RecordingWriter:
    """Stand-in for ``envfile.write_env_file`` that records every call."""

    def __init__(self, changed=True):
        self.calls = []
        self.changed = changed

    def __call__(self, path, values, *, secret_keys=(), now=None):
        self.calls.append(
            {"path": Path(path), "values": dict(values), "secret_keys": tuple(secret_keys)}
        )
        return {
            "changed": self.changed,
            "backup": None,
            "mode": "0644",
            "path": str(path),
        }


class RecordingApplier:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


# ---------------------------------------------------------------------------
# Positive: fresh non-interactive install
# ---------------------------------------------------------------------------


def test_fresh_non_interactive_plan_has_secure_defaults():
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    assert plan["errors"] == []
    assert plan["interactive"] is False
    assert plan["dry_run"] is False
    assert [entry["name"] for entry in plan["sections"]] == list(SECTION_ORDER)
    assert section(plan, "services")["enabled"] is False
    assert section(plan, "services")["reason"]
    assert section(plan, "core")["enabled"] is True
    assert plan["changes"]["PIPELINE_AUTONOMY"] == "dry-run"
    assert plan["changes"]["PIPELINE_NOTIFY_OUTBOX_ENABLED"] == "0"
    assert plan["changes"]["PIPELINE_NOTIFY_EMAIL_ENABLED"] == "0"
    assert plan["changes"]["PIPELINE_TICKET_PROVIDER"] == "none"
    assert "PLANE_API_KEY" in plan["changes"]
    assert "REPO_ROOT" not in plan["changes"]
    assert Path(plan["changes"]["PLAN_DIR"]).is_absolute()
    assert Path(plan["changes"]["WORKTREE_ROOT"]).is_absolute()


def test_fresh_non_interactive_plan_applies_claude_preset_for_every_role():
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    assert plan["routing"]["preset"] == "claude"
    choices = plan["routing"]["choices"]
    assert len(choices) == 9
    assert {choice["role"] for choice in choices} == {
        "overlord",
        "planner",
        "dispatch",
        "review",
        "decompose",
        "test_author",
        "diagnosis",
        "security",
        "chat",
    }
    assert {(choice["provider"], choice["model"]) for choice in choices} == {
        ("claude", "sonnet")
    }


def test_fresh_plan_leaves_escalation_keys_unset():
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    assert "PIPELINE_ESCALATION_BACKEND" not in plan["changes"]
    assert "PIPELINE_ESCALATION_MODEL" not in plan["changes"]


def test_fresh_plan_names_repo_root_as_a_next_step():
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    assert any("--repo-root" in step for step in plan["next_steps"])


def test_port_defaults_to_8001_not_8000():
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    assert plan["flags"]["port"] == 8001


def test_explicit_port_is_kept():
    plan = wizard.build_plan(
        make_args(yes=True, port=9123), state=make_state(), interactive=False
    )

    assert plan["flags"]["port"] == 9123
    assert plan["errors"] == []


# ---------------------------------------------------------------------------
# Positive: interactive flow
# ---------------------------------------------------------------------------


def test_interactive_ask_is_called_once_per_enabled_section():
    calls = []

    def ask(name, current):
        calls.append((name, dict(current)))
        if name == "routing":
            return {"preset": "claude"}
        if name == "policy":
            return {"PIPELINE_AUTONOMY": "gated", "PIPELINE_RISK_THRESHOLD": "medium"}
        if name == "notifications":
            return {"PIPELINE_NOTIFY_OUTBOX_ENABLED": "1"}
        if name == "services":
            return {"enabled": False}
        return {}

    plan = wizard.build_plan(
        make_args(), state=make_state(), interactive=True, ask=ask
    )

    assert [name for name, _current in calls] == list(SECTION_ORDER)
    assert plan["interactive"] is True
    assert plan["errors"] == []
    assert plan["changes"]["PIPELINE_AUTONOMY"] == "gated"
    assert plan["changes"]["PIPELINE_RISK_THRESHOLD"] == "medium"
    assert plan["changes"]["PIPELINE_NOTIFY_OUTBOX_ENABLED"] == "1"
    assert plan["routing"]["preset"] == "claude"
    assert len(plan["routing"]["choices"]) == 9
    assert section(plan, "services")["enabled"] is False


def test_interactive_ask_skips_a_skipped_section():
    calls = []

    def ask(name, current):
        calls.append(name)
        return {}

    plan = wizard.build_plan(
        make_args(skip={"services"}),
        state=make_state(),
        interactive=True,
        ask=ask,
    )

    assert "services" not in calls
    assert len(calls) == len(SECTION_ORDER) - 1
    assert section(plan, "services")["enabled"] is False


def test_yes_flag_never_calls_ask_even_on_a_tty(monkeypatch):
    calls = []

    def ask(name, current):
        calls.append(name)
        return {}

    monkeypatch.setattr(wizard.ui, "is_interactive", lambda *a, **k: True)

    plan = wizard.build_plan(make_args(yes=True), state=make_state(), ask=ask)

    assert calls == []
    assert plan["interactive"] is False


def test_non_interactive_run_never_calls_ask(monkeypatch):
    calls = []

    def ask(name, current):
        calls.append(name)
        return {}

    monkeypatch.setattr(wizard.ui, "is_interactive", lambda *a, **k: False)

    plan = wizard.build_plan(make_args(), state=make_state(), ask=ask)

    assert calls == []
    assert plan["interactive"] is False


def test_interactive_answers_are_validated_and_normalized():
    def ask(name, current):
        if name == "notifications":
            return {"PIPELINE_NOTIFY_EMAIL_ENABLED": "yes"}
        return {}

    plan = wizard.build_plan(
        make_args(), state=make_state(), interactive=True, ask=ask
    )

    assert plan["changes"]["PIPELINE_NOTIFY_EMAIL_ENABLED"] == "1"


# ---------------------------------------------------------------------------
# Positive: existing env is preserved
# ---------------------------------------------------------------------------


def test_existing_env_values_are_preserved_and_no_defaults_injected():
    existing = {"PIPELINE_AUTONOMY": "gated", "DASHBOARD_PORT": "9000"}
    plan = wizard.build_plan(
        make_args(yes=True), state=make_state(env=existing), interactive=False
    )

    assert plan["changes"] == {}
    assert plan["preserve"]["PIPELINE_AUTONOMY"] == "gated"
    assert plan["preserve"]["DASHBOARD_PORT"] == "9000"
    assert plan["routing"]["preset"] is None
    assert plan["routing"]["choices"] == []


def test_existing_registry_roles_block_means_no_preset_is_planned():
    roles = {"overlord": {"provider": "ollama", "model": "gpt-oss"}}
    plan = wizard.build_plan(
        make_args(yes=True), state=make_state(roles=roles), interactive=False
    )

    assert plan["routing"]["preset"] is None
    assert plan["routing"]["choices"] == []


def test_set_overrides_an_existing_value():
    existing = {"PIPELINE_AUTONOMY": "gated"}
    plan = wizard.build_plan(
        make_args(yes=True, set=["PIPELINE_AUTONOMY=full"]),
        state=make_state(env=existing),
        interactive=False,
    )

    assert plan["changes"]["PIPELINE_AUTONOMY"] == "full"
    assert "PIPELINE_AUTONOMY" not in plan["preserve"]
    assert plan["errors"] == []


def test_autonomy_flag_overrides_the_fresh_default():
    plan = wizard.build_plan(
        make_args(yes=True, autonomy="gated"), state=make_state(), interactive=False
    )

    assert plan["changes"]["PIPELINE_AUTONOMY"] == "gated"
    assert plan["flags"]["autonomy"] == "gated"


def test_data_dir_derives_plan_and_worktree_roots(tmp_path):
    plan = wizard.build_plan(
        make_args(yes=True, data_dir=str(tmp_path / "data")),
        state=make_state(),
        interactive=False,
    )

    assert plan["changes"]["PLAN_DIR"] == str(tmp_path / "data" / "plans")
    assert plan["changes"]["WORKTREE_ROOT"] == str(tmp_path / "data" / "worktrees")
    assert plan["flags"]["data_dir"] == str(tmp_path / "data")


def test_repo_root_is_only_set_when_explicitly_supplied(tmp_path):
    plan = wizard.build_plan(
        make_args(yes=True, repo_root=str(tmp_path)),
        state=make_state(),
        interactive=False,
    )

    assert plan["changes"]["REPO_ROOT"] == str(tmp_path)
    assert plan["flags"]["repo_root"] == str(tmp_path)


def test_provider_and_model_produce_one_routing_choice():
    plan = wizard.build_plan(
        make_args(yes=True, role="dispatch", provider="ollama", model="gpt-oss"),
        state=make_state(),
        interactive=False,
    )

    assert plan["errors"] == []
    assert plan["routing"]["choices"] == [
        {"role": "dispatch", "provider": "ollama", "model": "gpt-oss"}
    ]


def test_skip_disables_a_section_with_a_reason():
    plan = wizard.build_plan(
        make_args(yes=True, skip={"claude_assets"}),
        state=make_state(),
        interactive=False,
    )

    assert section(plan, "claude_assets")["enabled"] is False
    assert section(plan, "claude_assets")["reason"]


# ---------------------------------------------------------------------------
# Negative: bad input becomes an error, never an exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "PIPELINE_AUTONOMY=sideways",
        "NOT_A_REAL_KEY=1",
        "PIPELINE_MAX_CONCURRENT_AGENTS=zero",
    ],
)
def test_bad_set_value_is_an_error(token):
    plan = wizard.build_plan(
        make_args(yes=True, set=[token]), state=make_state(), interactive=False
    )

    assert plan["errors"]
    assert any(token.split("=")[0] in error for error in plan["errors"])


def test_set_without_equals_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, set=["PIPELINE_AUTONOMY"]),
        state=make_state(),
        interactive=False,
    )

    assert plan["errors"]


def test_bad_set_never_echoes_a_secret_value():
    plan = wizard.build_plan(
        make_args(yes=True, set=[f"PLANE_API_KEY={SECRET_SENTINEL}"]),
        state=make_state(),
        interactive=False,
    )

    # A str key accepts anything, so this is a change, not an error - but the
    # value must never leak into an error string.
    assert all(SECRET_SENTINEL not in error for error in plan["errors"])


def test_bad_set_key_never_echoes_the_value():
    plan = wizard.build_plan(
        make_args(yes=True, set=[f"NOT_A_KEY={SECRET_SENTINEL}"]),
        state=make_state(),
        interactive=False,
    )

    assert plan["errors"]
    assert all(SECRET_SENTINEL not in error for error in plan["errors"])


def test_provider_without_role_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, provider="claude", model="sonnet"),
        state=make_state(),
        interactive=False,
    )

    assert any("--role" in error for error in plan["errors"])


def test_role_without_provider_and_model_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, role="dispatch"), state=make_state(), interactive=False
    )

    assert any("--provider" in error for error in plan["errors"])


def test_provider_without_model_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, role="dispatch", provider="claude"),
        state=make_state(),
        interactive=False,
    )

    assert plan["errors"]


def test_unknown_role_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, role="nope", provider="claude", model="sonnet"),
        state=make_state(),
        interactive=False,
    )

    assert any("nope" in error for error in plan["errors"])


def test_unknown_skip_section_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, skip={"nonsense"}), state=make_state(), interactive=False
    )

    assert any("nonsense" in error for error in plan["errors"])


def test_bad_autonomy_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, autonomy="sideways"), state=make_state(), interactive=False
    )

    assert any("PIPELINE_AUTONOMY" in error for error in plan["errors"])


def test_relative_data_dir_is_an_error():
    plan = wizard.build_plan(
        make_args(yes=True, data_dir="relative/path"),
        state=make_state(),
        interactive=False,
    )

    assert any("--data-dir" in error for error in plan["errors"])


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_out_of_range_port_is_an_error(port):
    plan = wizard.build_plan(
        make_args(yes=True, port=port), state=make_state(), interactive=False
    )

    assert any("port" in error.lower() for error in plan["errors"])


def test_registry_without_claude_sonnet_leaves_routing_unset():
    plan = wizard.build_plan(
        make_args(yes=True),
        state=make_state(data=REGISTRY_WITHOUT_CLAUDE),
        interactive=False,
    )

    assert plan["routing"]["preset"] is None
    assert plan["routing"]["choices"] == []
    assert section(plan, "routing")["enabled"] is False
    assert section(plan, "routing")["reason"]
    assert any("--provider" in step and "--model" in step for step in plan["next_steps"])


# ---------------------------------------------------------------------------
# run_plan: errors short-circuit
# ---------------------------------------------------------------------------


def test_run_plan_with_errors_calls_no_writer():
    writer = RecordingWriter()
    plan = wizard.build_plan(
        make_args(yes=True, set=["PIPELINE_AUTONOMY=sideways"]),
        state=make_state(),
        interactive=False,
    )

    result = wizard.run_plan(plan, output=lambda _line: None, env_writer=writer)

    assert result["applied"] is False
    assert result["errors"] == plan["errors"]
    assert writer.calls == []


# ---------------------------------------------------------------------------
# run_plan: dry run
# ---------------------------------------------------------------------------


def test_dry_run_makes_zero_applier_calls_and_zero_file_writes(tmp_path):
    writer = RecordingWriter()
    applier = RecordingApplier(result=[])
    assets = RecordingApplier(result={})
    services = RecordingApplier(result={})
    lines = []

    plan = wizard.build_plan(
        make_args(
            yes=True,
            dry_run=True,
            repo_root=str(tmp_path),
            data_dir=str(tmp_path / "data"),
        ),
        state=make_state(),
        interactive=False,
    )
    result = wizard.run_plan(
        plan,
        output=lines.append,
        env_writer=writer,
        provider_applier=applier,
        asset_installer=assets,
        service_runner=services,
    )

    assert result["applied"] is False
    assert result["dry_run"] is True
    assert writer.calls == []
    assert applier.calls == []
    assert assets.calls == []
    assert services.calls == []
    assert not (tmp_path / ".pipeline.env").exists()
    assert not (tmp_path / "data").exists()


def test_dry_run_prints_every_planned_change_and_masks_secrets():
    lines = []
    plan = wizard.build_plan(
        make_args(yes=True, dry_run=True, set=[f"PLANE_API_KEY={SECRET_SENTINEL}"]),
        state=make_state(),
        interactive=False,
    )

    result = wizard.run_plan(plan, output=lines.append)

    printed = "\n".join(lines)
    for key in plan["changes"]:
        assert key in printed
    assert SECRET_SENTINEL not in printed
    assert "PLANE_API_KEY=***" in printed
    assert result["planned"]["PLANE_API_KEY"] == "***"
    assert SECRET_SENTINEL not in str(result)


def test_dry_run_prints_one_line_per_section():
    lines = []
    plan = wizard.build_plan(
        make_args(yes=True, dry_run=True), state=make_state(), interactive=False
    )

    wizard.run_plan(plan, output=lines.append)

    printed = "\n".join(lines)
    for name in SECTION_ORDER:
        assert name in printed


# ---------------------------------------------------------------------------
# run_plan: apply
# ---------------------------------------------------------------------------


def test_run_plan_calls_env_writer_once_with_preserve_and_changes_merged():
    writer = RecordingWriter()
    existing = {"DASHBOARD_PORT": "9000"}
    plan = wizard.build_plan(
        make_args(yes=True, set=["PIPELINE_AUTONOMY=gated"]),
        state=make_state(env=existing),
        interactive=False,
    )

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=writer,
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=RecordingApplier(result={}),
    )

    assert len(writer.calls) == 1
    assert writer.calls[0]["values"] == {
        **plan["preserve"],
        **plan["changes"],
    }
    assert writer.calls[0]["values"]["DASHBOARD_PORT"] == "9000"
    assert writer.calls[0]["values"]["PIPELINE_AUTONOMY"] == "gated"
    assert result["applied"] is True
    assert result["env"]["changed"] is True


def test_run_plan_passes_secret_keys_to_the_writer():
    writer = RecordingWriter()
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=writer,
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=RecordingApplier(result={}),
    )

    assert "PLANE_API_KEY" in writer.calls[0]["secret_keys"]
    assert "PIPELINE_NOTIFY_EMAIL_PASSWORD" in writer.calls[0]["secret_keys"]


def test_run_plan_passes_routing_choices_to_the_provider_applier():
    applier = RecordingApplier(result=[{"role": "overlord", "ok": True}])
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=applier,
        asset_installer=RecordingApplier(result={}),
        service_runner=RecordingApplier(result={}),
    )

    assert applier.calls[0]["choices"] == plan["routing"]["choices"]
    assert result["registry"] == [{"role": "overlord", "ok": True}]


def test_run_plan_runs_asset_installer_when_claude_assets_enabled():
    assets = RecordingApplier(result={"installed": ["a.md"], "kept": []})
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=RecordingApplier(result=[]),
        asset_installer=assets,
        service_runner=RecordingApplier(result={}),
    )

    assert len(assets.calls) == 1
    assert result["assets"] == {"installed": ["a.md"], "kept": []}


def test_run_plan_skips_service_runner_when_services_disabled():
    services = RecordingApplier(result={})
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=services,
    )

    assert services.calls == []
    assert result["services"] is None
    assert "services" in result["skipped"]


def test_run_plan_runs_service_runner_when_services_enabled():
    services = RecordingApplier(result={"render": {"status": "rendered"}})
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)
    for entry in plan["sections"]:
        if entry["name"] == "services":
            entry["enabled"] = True
            entry["reason"] = None

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=services,
    )

    assert len(services.calls) == 1
    assert result["services"] == {"render": {"status": "rendered"}}


def test_a_raising_applier_is_recorded_and_later_appliers_still_run():
    assets = RecordingApplier(result={"installed": [], "kept": []})
    services = RecordingApplier(result={"render": {"status": "rendered"}})
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)
    for entry in plan["sections"]:
        if entry["name"] == "services":
            entry["enabled"] = True
            entry["reason"] = None

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=RecordingApplier(raises=RuntimeError("registry exploded")),
        asset_installer=assets,
        service_runner=services,
    )

    assert result["applied"] is True
    assert any("registry exploded" in error for error in result["errors"])
    assert len(assets.calls) == 1
    assert len(services.calls) == 1


def test_a_raising_env_writer_is_recorded_and_later_appliers_still_run():
    assets = RecordingApplier(result={"installed": [], "kept": []})
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=boom,
        provider_applier=RecordingApplier(result=[]),
        asset_installer=assets,
        service_runner=RecordingApplier(result={}),
    )

    assert any("disk full" in error for error in result["errors"])
    assert len(assets.calls) == 1


def test_plan_dir_and_worktree_root_are_created_when_core_changed(tmp_path):
    plan = wizard.build_plan(
        make_args(yes=True, data_dir=str(tmp_path / "data")),
        state=make_state(),
        interactive=False,
    )

    wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=RecordingApplier(result={}),
    )

    assert (tmp_path / "data" / "plans").is_dir()
    assert (tmp_path / "data" / "worktrees").is_dir()


def test_plan_dir_is_not_created_when_core_is_skipped(tmp_path):
    plan = wizard.build_plan(
        make_args(yes=True, data_dir=str(tmp_path / "data"), skip={"core"}),
        state=make_state(),
        interactive=False,
    )

    wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=RecordingApplier(result={}),
    )

    assert not (tmp_path / "data" / "plans").exists()


def test_env_is_written_before_services_start():
    order = []

    def writer(*_args, **_kwargs):
        order.append("env")
        return {"changed": True, "backup": None, "mode": "0644", "path": "x"}

    def runner(**_kwargs):
        order.append("services")
        return {}

    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)
    for entry in plan["sections"]:
        if entry["name"] == "services":
            entry["enabled"] = True
            entry["reason"] = None

    wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=writer,
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=runner,
    )

    assert order == ["env", "services"]


# ---------------------------------------------------------------------------
# Health verification
# ---------------------------------------------------------------------------


def test_health_check_uses_the_shared_secret_and_never_reports_it(monkeypatch):
    seen = {}

    def fake_verify(*, port, api_key, **kwargs):
        seen["port"] = port
        seen["api_key"] = api_key
        return (True, "ok")

    monkeypatch.setattr(wizard.services, "verify_health", fake_verify)
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.auth",
        SimpleNamespace(get_or_create_api_key=lambda: SECRET_SENTINEL),
    )

    result = wizard._verify_health(8001)

    assert seen["port"] == 8001
    assert seen["api_key"] == SECRET_SENTINEL
    assert result == {"healthy": True, "reason": "ok"}
    assert SECRET_SENTINEL not in str(result)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_running_the_same_plan_twice_is_idempotent(tmp_path):
    plan = wizard.build_plan(
        make_args(yes=True, repo_root=str(tmp_path)),
        state=make_state(),
        interactive=False,
    )

    def run():
        return wizard.run_plan(
            copy.deepcopy(plan),
            output=lambda _line: None,
            provider_applier=RecordingApplier(result=[]),
            asset_installer=RecordingApplier(result={}),
            service_runner=RecordingApplier(result={}),
        )

    first = run()
    content_after_first = (tmp_path / ".pipeline.env").read_text(encoding="utf-8")
    second = run()
    content_after_second = (tmp_path / ".pipeline.env").read_text(encoding="utf-8")

    assert first["env"]["changed"] is True
    assert second["env"]["changed"] is False
    assert content_after_first == content_after_second


# ---------------------------------------------------------------------------
# render_summary
# ---------------------------------------------------------------------------


def _summary_result(**overrides):
    base = {
        "applied": True,
        "changed": {"PIPELINE_AUTONOMY": "dry-run"},
        "sections": [
            {"name": "core", "enabled": True, "reason": None},
            {"name": "services", "enabled": False, "reason": "not started"},
        ],
        "errors": [],
        "next_steps": [],
        "assets": {"assets": {"installed": [], "kept": []}, "mcp": {"status": "registered"}},
    }
    base.update(overrides)
    return base


def test_summary_contains_the_dry_run_to_gated_sentence():
    text = wizard.render_summary(_summary_result())

    assert describe_autonomy("dry-run") in text


def test_summary_omits_the_mcp_manual_command_when_registration_succeeded():
    text = wizard.render_summary(_summary_result())

    assert "claude mcp add -s user pipeline" not in text


def test_summary_contains_the_mcp_manual_command_when_registration_was_skipped():
    result = _summary_result(
        assets={
            "assets": {"installed": [], "kept": []},
            "mcp": {
                "status": "failed",
                "manual": "claude mcp add -s user pipeline /usr/bin/python /repo/server.py",
            },
        }
    )

    text = wizard.render_summary(result)

    assert "claude mcp add -s user pipeline" in text


def test_summary_always_names_the_restart_and_test_steps():
    text = wizard.render_summary(_summary_result())

    assert "Restart Claude Code so it picks up the registered server." in text
    assert "Run .venv/bin/python -m pytest -q" in text


def test_summary_lists_every_section():
    text = wizard.render_summary(_summary_result())

    assert "core" in text
    assert "services" in text


def test_summary_never_contains_a_secret_sentinel():
    result = _summary_result(
        changed={"PLANE_API_KEY": "***", "PIPELINE_AUTONOMY": "dry-run"},
        errors=["PLANE_API_KEY: could not be validated"],
    )

    text = wizard.render_summary(result)

    assert SECRET_SENTINEL not in text
    assert "PLANE_API_KEY=***" in text


def test_summary_reports_errors_instead_of_claiming_success():
    result = _summary_result(errors=["writing .pipeline.env failed: disk full"])

    text = wizard.render_summary(result)

    assert "disk full" in text


def test_summary_of_an_empty_plan_is_still_rendered():
    text = wizard.render_summary(
        {"applied": False, "changed": {}, "sections": [], "errors": [], "next_steps": []}
    )

    assert isinstance(text, str)
    assert text.strip()
    assert "Run .venv/bin/python -m pytest -q" in text


def test_summary_includes_plan_next_steps():
    result = _summary_result(next_steps=["Set the managed repository with --repo-root"])

    text = wizard.render_summary(result)

    assert "--repo-root" in text


def test_summary_is_deterministic():
    result = _summary_result()

    assert wizard.render_summary(result) == wizard.render_summary(result)


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_build_plan_performs_no_filesystem_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    before = sorted(p.name for p in tmp_path.iterdir())

    wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_wizard_module_has_exactly_three_public_functions():
    assert sorted(wizard.__all__) == ["build_plan", "render_summary", "run_plan"]


def test_apply_result_carries_sections_so_the_summary_can_list_them():
    plan = wizard.build_plan(make_args(yes=True), state=make_state(), interactive=False)

    result = wizard.run_plan(
        plan,
        output=lambda _line: None,
        env_writer=RecordingWriter(),
        provider_applier=RecordingApplier(result=[]),
        asset_installer=RecordingApplier(result={}),
        service_runner=RecordingApplier(result={}),
    )

    assert [entry["name"] for entry in result["sections"]] == list(SECTION_ORDER)
    text = wizard.render_summary(result)
    for name in SECTION_ORDER:
        assert name in text


def test_summary_of_a_dry_run_result_still_names_the_dry_run_sentence():
    plan = wizard.build_plan(
        make_args(yes=True, dry_run=True), state=make_state(), interactive=False
    )

    result = wizard.run_plan(plan, output=lambda _line: None)

    assert describe_autonomy("dry-run") in wizard.render_summary(result)


def test_default_adapter_core_prompt_asks_for_a_data_directory(monkeypatch):
    asked = []

    def fake_prompt(label, *, kind, default=None, choices=(), validate=None, **_kw):
        asked.append((label, default))
        if "Data directory" in label:
            return "/tmp/agent-data"
        if "port" in label.lower():
            return "8001"
        return default

    monkeypatch.setattr(wizard.ui, "prompt_value", fake_prompt)
    monkeypatch.setattr(wizard.ui, "ask_yes_no", lambda *_a, **_k: False)
    monkeypatch.setattr(wizard, "_offers_repo_root", lambda: False)

    answers = wizard._default_ask(state=make_state(), flags={"port": 8001})("core", {})

    assert any("Data directory" in label for label, _default in asked)
    assert any(default == "~/.claude" for _label, default in asked)
    assert answers["PLAN_DIR"] == "/tmp/agent-data/plans"
    assert answers["WORKTREE_ROOT"] == "/tmp/agent-data/worktrees"
