"""The settings MCP tools: ``set_setting``, ``get_settings`` and ``list_settings``.

Writes are host-scoped ``config:write`` (ADR 0014 decision 3: an agent principal
never writes configuration). Every write and every refusal lands in the host
audit chain. The store is real and file-backed over a throwaway plans directory;
only the authorizer and the settings-plugin probe are stubbed.
"""

import pytest

from app.identity import Principal
from pipeline import audit, authorization, persistence, settings_tools
from pipeline import server as p
from pipeline.actor import bind_principal
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.settings_registry import REGISTRY, SettingKey
from pipeline.settings_store import effective_setting
from pipeline.store_records import DEFAULT_NAMESPACE

REPO = "github.com/acme/widgets"
HUMAN = Principal("user:alice", auth_method="oidc")
AGENT = Principal("agent:x", principal_type="agent", team_scope="acme/team-1")


class _Allow:
    def __init__(self):
        self.calls = 0

    def allow(self, principal, action, resource=None):
        self.calls += 1
        return True


class _Deny:
    def allow(self, principal, action, resource=None):
        return False


class _Raise:
    def allow(self, principal, action, resource=None):
        raise RuntimeError("policy backend unreachable")


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    return p._store


@pytest.fixture
def authorizer(monkeypatch):
    """Install a stub authorizer and keep enforcement on; return a setter."""
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)

    def install(stub):
        monkeypatch.setattr(authorization, "default_authorizer", lambda: stub)
        return stub

    return install


@pytest.fixture
def no_settings_plugin(monkeypatch):
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: False)


def _set(**overrides):
    args = {
        "scope": "deployment",
        "scope_id": "",
        "repo": "",
        "key": "merge_ci_timeout_s",
        "value": 600,
        "expected_version": None,
    }
    args.update(overrides)
    return settings_tools.set_setting(**args)


def _host_records():
    return audit._audit_log().read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)


def _change_records():
    return [r for r in _host_records() if r["action"] == "settings:set"]


def _run_as(principal):
    return bind_principal(principal)


# --- positive ---------------------------------------------------------------


def test_deployment_write_then_get_returns_the_stored_record(
    store, authorizer, no_settings_plugin
):
    authorizer(_Allow())

    written = _set(value=600)
    fetched = settings_tools.get_settings("deployment", "", "")

    assert written["ok"] is True
    assert written["new_version"] == 1
    assert fetched["ok"] is True
    assert fetched["record"]["values"] == {"merge_ci_timeout_s": 600}
    assert fetched["record"]["version"] == 1


def test_team_repo_write_is_reflected_by_effective_setting(
    store, authorizer, no_settings_plugin
):
    authorizer(_Allow())

    _set(scope="team_repo", scope_id="team-1", repo=REPO, value=60)

    effective = effective_setting(
        "merge_ci_timeout_s", namespace=DEFAULT_NAMESPACE, team="team-1", repo=REPO
    )
    assert effective["value"] == 60
    assert effective["source"] == "team_repo"


def test_a_second_key_at_the_same_layer_keeps_the_first(
    store, authorizer, no_settings_plugin
):
    authorizer(_Allow())
    _set(value=600)

    second = _set(key="merge_readiness_timeout_s", value=90, expected_version=1)

    assert second["new_version"] == 2
    fetched = settings_tools.get_settings("deployment", "", "")
    assert fetched["record"]["values"] == {
        "merge_ci_timeout_s": 600,
        "merge_readiness_timeout_s": 90,
    }


def test_list_settings_returns_every_record(store, authorizer, no_settings_plugin):
    authorizer(_Allow())
    _set(value=600)
    _set(scope="team_repo", scope_id="team-1", repo=REPO, value=60)

    listed = settings_tools.list_settings()

    assert listed["ok"] is True
    assert [(r["scope"], r["version"]) for r in listed["records"]] == [
        ("deployment", 1),
        ("team_repo", 1),
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [("merge_ci_timeout_s", 1), ("merge_ci_timeout_s", 86400), ("grade_xdist_workers", 64)],
)
def test_range_boundaries_inside_the_bound_are_accepted(
    store, authorizer, no_settings_plugin, key, value
):
    authorizer(_Allow())

    assert _set(key=key, value=value)["ok"] is True


def test_the_tools_are_registered_on_the_mcp_server():
    registered = p.mcp._tool_manager._tools

    for name in ("set_setting", "get_settings", "list_settings"):
        assert registered[name].fn is getattr(settings_tools, name)


# --- authorization ----------------------------------------------------------


def test_agent_principal_is_refused_before_the_authorizer_is_asked(
    store, authorizer, no_settings_plugin
):
    stub = authorizer(_Allow())

    with _run_as(AGENT):
        result = _set()

    assert result["ok"] is False
    assert result["forbidden"] is True
    assert stub.calls == 0
    assert settings_tools.get_settings("deployment", "", "")["record"] is None


def test_unauthorized_principal_is_refused_and_nothing_is_written(
    store, authorizer, no_settings_plugin
):
    authorizer(_Deny())

    with _run_as(HUMAN):
        result = _set()

    assert result["ok"] is False
    assert result["forbidden"] is True
    assert settings_tools.get_settings("deployment", "", "")["record"] is None


def test_an_authorizer_that_raises_results_in_a_refusal(
    store, authorizer, no_settings_plugin
):
    authorizer(_Raise())

    with _run_as(HUMAN):
        result = _set()

    assert result["ok"] is False
    assert settings_tools.get_settings("deployment", "", "")["record"] is None


# --- validation -------------------------------------------------------------


def test_an_unknown_key_is_refused(store, authorizer, no_settings_plugin):
    authorizer(_Allow())

    result = _set(key="no_such_key")

    assert result == {"ok": False, "reason": "unknown_key", "error": result["error"]}
    assert "no_such_key" in result["error"]


def test_a_lock_key_is_refused_at_team_scope(
    store, authorizer, no_settings_plugin, monkeypatch
):
    monkeypatch.setitem(
        REGISTRY,
        "store_location",
        SettingKey(
            name="store_location",
            type="int",
            kind="lock",
            safe_value=1,
            allowed_layers=("deployment",),
            legacy_env="PIPELINE_STORE_LOCATION",
        ),
    )
    authorizer(_Allow())

    refused = _set(scope="team", scope_id="team-1", key="store_location", value=2)
    accepted = _set(key="store_location", value=2)

    assert refused["ok"] is False
    assert refused["reason"] == "lock_key"
    assert accepted["ok"] is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("merge_ci_gate", "yes"),
        ("merge_ci_gate", 1),
        ("merge_ci_timeout_s", "600"),
        ("merge_ci_timeout_s", True),
        ("test_scope", "partial"),
    ],
)
def test_a_wrong_type_or_choice_is_refused(
    store, authorizer, no_settings_plugin, key, value
):
    authorizer(_Allow())

    result = _set(key=key, value=value)

    assert result["ok"] is False
    assert result["reason"] == "invalid_value"
    assert settings_tools.get_settings("deployment", "", "")["record"] is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("merge_ci_timeout_s", 0),
        ("merge_readiness_timeout_s", 86401),
        ("grade_xdist_workers", 0),
        ("grade_xdist_workers", 65),
    ],
)
def test_a_value_outside_its_range_is_refused(
    store, authorizer, no_settings_plugin, key, value
):
    authorizer(_Allow())

    result = _set(key=key, value=value)

    assert result["ok"] is False
    assert result["reason"] == "invalid_value"


@pytest.mark.parametrize(
    ("scope", "scope_id", "repo"),
    [
        ("galaxy", "", ""),
        ("team", "Team One", ""),
        ("team_repo", "team-1", "not-a-repo"),
    ],
)
def test_a_malformed_scope_is_refused(
    store, authorizer, no_settings_plugin, scope, scope_id, repo
):
    authorizer(_Allow())

    result = _set(scope=scope, scope_id=scope_id, repo=repo)

    assert result["ok"] is False
    assert result["reason"] == "invalid_key"


@pytest.mark.parametrize("version", ["1", True, -1])
def test_a_malformed_expected_version_is_refused(
    store, authorizer, no_settings_plugin, version
):
    authorizer(_Allow())

    result = _set(expected_version=version)

    assert result["ok"] is False
    assert result["reason"] == "invalid_version"


def test_allow_loosen_on_a_pref_key_is_refused(store, authorizer, no_settings_plugin):
    authorizer(_Allow())

    result = _set(allow_loosen=["merge_ci_timeout_s"])

    assert result["ok"] is False
    assert result["reason"] == "invalid_allow_loosen"


def test_allow_loosen_naming_an_unknown_key_is_refused(
    store, authorizer, no_settings_plugin
):
    authorizer(_Allow())

    result = _set(key="merge_ci_gate", value=False, allow_loosen=["no_such_key"])

    assert result["ok"] is False
    assert result["reason"] == "invalid_allow_loosen"


# --- compare-and-set ---------------------------------------------------------


def test_a_stale_expected_version_is_refused_and_the_record_is_unchanged(
    store, authorizer, no_settings_plugin
):
    authorizer(_Allow())
    _set(value=600)

    result = _set(value=900, expected_version=5)

    assert result["ok"] is False
    assert result["reason"] == "version_conflict"
    fetched = settings_tools.get_settings("deployment", "", "")
    assert fetched["record"]["values"] == {"merge_ci_timeout_s": 600}
    assert fetched["record"]["version"] == 1


def test_creating_over_an_existing_record_is_a_version_conflict(
    store, authorizer, no_settings_plugin
):
    authorizer(_Allow())
    _set(value=600)

    result = _set(value=900, expected_version=None)

    assert result["reason"] == "version_conflict"


# --- settings plugin --------------------------------------------------------


@pytest.mark.parametrize(("scope", "scope_id"), [("org", "acme"), ("team", "team-1")])
def test_org_and_team_writes_are_refused_while_a_settings_plugin_is_configured(
    store, authorizer, monkeypatch, scope, scope_id
):
    authorizer(_Allow())
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: True)

    result = _set(scope=scope, scope_id=scope_id)

    assert result["ok"] is False
    assert result["reason"] == "settings_plugin"
    assert "PIPELINE_SETTINGS_PLUGIN" in result["error"]


def test_a_failing_plugin_probe_refuses_org_writes(store, authorizer, monkeypatch):
    authorizer(_Allow())

    def broken():
        raise RuntimeError("plugin load failed")

    monkeypatch.setattr(settings_tools, "provider_configured", broken)

    result = _set(scope="org", scope_id="acme")

    assert result["ok"] is False
    assert result["reason"] == "settings_plugin"


def test_deployment_writes_are_allowed_while_a_settings_plugin_is_configured(
    store, authorizer, monkeypatch
):
    authorizer(_Allow())
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: True)

    assert _set(value=600)["ok"] is True


# --- read tools -------------------------------------------------------------


def test_get_settings_refuses_a_malformed_scope(store):
    result = settings_tools.get_settings("galaxy", "", "")

    assert result["ok"] is False
    assert result["reason"] == "invalid_key"


# --- audit ------------------------------------------------------------------


def test_a_successful_write_appends_one_change_record(
    store, authorizer, no_settings_plugin
):
    authorizer(_Allow())

    with _run_as(HUMAN):
        _set(value=600)

    (record,) = _change_records()
    assert record["outcome"] == "allowed"
    assert record["detail"]["actor"] == "user:alice"
    assert record["target"]["key"] == "merge_ci_timeout_s"
    assert record["target"]["scope"] == "deployment"
    assert record["detail"]["old"] is None
    assert record["detail"]["new"] == 600
    assert record["detail"]["new_version"] == 1


def test_an_overwrite_records_the_old_value(store, authorizer, no_settings_plugin):
    authorizer(_Allow())
    _set(value=600)

    _set(value=900, expected_version=1)

    newest = _change_records()[-1]
    assert newest["detail"]["old"] == 600
    assert newest["detail"]["new"] == 900
    assert newest["detail"]["new_version"] == 2


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"key": "no_such_key"}, "unknown_key"),
        ({"key": "merge_ci_gate", "value": "yes"}, "invalid_value"),
        ({"value": 0}, "invalid_value"),
        ({"expected_version": 9}, "version_conflict"),
        ({"scope": "org", "scope_id": "acme"}, "settings_plugin"),
    ],
)
def test_each_validation_refusal_appends_a_denied_record(
    store, authorizer, no_settings_plugin, monkeypatch, kwargs, reason
):
    authorizer(_Allow())
    if reason == "settings_plugin":
        monkeypatch.setattr(settings_tools, "provider_configured", lambda: True)

    result = _set(**kwargs)

    assert result["ok"] is False
    denied = [r for r in _change_records() if r["outcome"] == "denied"]
    assert [r["detail"]["reason"] for r in denied] == [reason]


def test_an_agent_refusal_is_audited(store, authorizer, no_settings_plugin):
    authorizer(_Allow())

    with _run_as(AGENT):
        _set()

    decisions = [r for r in _host_records() if r["action"] == "config:write"]
    assert [r["outcome"] for r in decisions] == ["denied"]
    assert decisions[0]["detail"]["reason"] == "agent_principal"
    assert [r["outcome"] for r in _change_records()] == ["denied"]


def test_an_authorizer_error_is_audited_as_a_denial(
    store, authorizer, no_settings_plugin
):
    authorizer(_Raise())

    with _run_as(HUMAN):
        _set()

    assert [r["outcome"] for r in _change_records()] == ["denied"]
    assert _change_records()[0]["detail"]["reason"] == "not_authorized"
