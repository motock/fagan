"""The deployment test-policy lock: ``tenant_test_policy_locked``.

When the deployment layer sets the lock to exactly ``True``, its ``test_scope``
is final: no org, team, team_repo, plan, story or env value can tighten or
loosen it. ``reverify_full_suite`` reads the deployment layer alone under the
same lock. A lock that is not exactly ``True`` changes nothing.
"""

import pytest

from app.identity import Principal
from pipeline import authorization, persistence, settings_tools
from pipeline import server as p
from pipeline.actor import bind_principal
from pipeline.ci import SettingsContext
from pipeline.policy_settings import SETTINGS_ERROR_KEY, SETTINGS_PROVIDER_ERROR
from pipeline.settings_registry import REGISTRY
from pipeline.store_records import DEFAULT_NAMESPACE
from pipeline.test_scope import TEST_SCOPE_FULL, TEST_SCOPE_SCOPED, resolve_test_scope

LOCK = "tenant_test_policy_locked"
REPO = "github.com/acme/widgets"
HUMAN = Principal("user:alice", auth_method="oidc")


class _Allow:
    def allow(self, principal, action, resource=None):
        return True


def _settings(deployment=None, **layers):
    settings = dict(layers)
    if deployment is not None:
        settings["deployment"] = deployment
    return settings


def _locked(test_scope=TEST_SCOPE_SCOPED, lock=True):
    deployment = {LOCK: lock}
    if test_scope is not None:
        deployment["test_scope"] = test_scope
    return deployment


def _rejection(layer, requested=TEST_SCOPE_FULL):
    return {"layer": layer, "requested": requested, "by": "deployment"}


# --- lock off: today's behaviour is unchanged -------------------------------


@pytest.mark.parametrize("lock", [None, False, "true", 1])
def test_a_lock_that_is_not_exactly_true_does_not_bind_a_team_full_request(lock):
    deployment = {"test_scope": TEST_SCOPE_SCOPED}
    if lock is not None:
        deployment[LOCK] = lock
    result = resolve_test_scope(
        settings=_settings(deployment=deployment, team={"test_scope": TEST_SCOPE_FULL})
    )
    assert result["test_scope"] == TEST_SCOPE_FULL
    assert result["source"] == "team"
    assert result["lock_rejections"] == []


def test_a_missing_lock_key_does_not_bind_a_team_full_request():
    result = resolve_test_scope(
        settings=_settings(
            deployment={"test_scope": TEST_SCOPE_SCOPED},
            team={"test_scope": TEST_SCOPE_FULL},
        )
    )
    assert result["test_scope"] == TEST_SCOPE_FULL
    assert result["lock_rejections"] == []


# --- lock on: the deployment value is final ---------------------------------


@pytest.mark.parametrize(
    "layer, request_kwargs",
    [
        ("org", {"org": {"test_scope": TEST_SCOPE_FULL}}),
        ("team", {"team": {"test_scope": TEST_SCOPE_FULL}}),
        ("team_repo", {"team_repo": {"test_scope": TEST_SCOPE_FULL}}),
    ],
)
def test_a_policy_layer_full_request_resolves_to_the_locked_scoped(layer, request_kwargs):
    result = resolve_test_scope(settings=_settings(deployment=_locked(), **request_kwargs))
    assert result["test_scope"] == TEST_SCOPE_SCOPED
    assert result["source"] == "deployment"
    assert result["lock_rejections"] == [_rejection(layer)]


@pytest.mark.parametrize(
    "kwargs, layer",
    [
        ({"plan_value": TEST_SCOPE_FULL}, "plan"),
        ({"story_value": TEST_SCOPE_FULL}, "story"),
        ({"env_value": TEST_SCOPE_FULL}, "env"),
    ],
)
def test_a_request_full_resolves_to_the_locked_scoped(kwargs, layer):
    result = resolve_test_scope(settings=_settings(deployment=_locked()), **kwargs)
    assert result["test_scope"] == TEST_SCOPE_SCOPED
    assert result["source"] == "deployment"
    assert result["lock_rejections"] == [_rejection(layer)]


def test_a_locked_deployment_without_a_test_scope_resolves_full():
    result = resolve_test_scope(
        settings=_settings(deployment=_locked(test_scope=None), team={"test_scope": TEST_SCOPE_SCOPED})
    )
    assert result["test_scope"] == TEST_SCOPE_FULL
    assert result["source"] == "deployment"


def test_a_locked_full_deployment_rejects_a_story_scoped_request():
    result = resolve_test_scope(
        settings=_settings(deployment=_locked(test_scope=TEST_SCOPE_FULL)),
        story_value=TEST_SCOPE_SCOPED,
    )
    assert result["test_scope"] == TEST_SCOPE_FULL
    assert result["source"] == "deployment"
    assert result["lock_rejections"] == [_rejection("story", TEST_SCOPE_SCOPED)]


def test_a_locked_deployment_with_an_invalid_test_scope_resolves_full():
    result = resolve_test_scope(
        settings=_settings(deployment={LOCK: True, "test_scope": "sometimes"})
    )
    assert result["test_scope"] == TEST_SCOPE_FULL
    assert result["source"] == "deployment"


# --- a settings error still resolves full ------------------------------------


def test_a_settings_error_resolves_full_even_when_the_lock_is_on():
    result = resolve_test_scope(
        settings={SETTINGS_ERROR_KEY: "x", "deployment": _locked()}
    )
    assert result["test_scope"] == TEST_SCOPE_FULL
    assert result["source"] == SETTINGS_PROVIDER_ERROR


# --- set_setting: the lock key is deployment-only ----------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: False)
    monkeypatch.setattr(authorization, "default_authorizer", lambda: _Allow())
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    return p._store


def _set(**overrides):
    args = {
        "scope": "deployment",
        "scope_id": "",
        "repo": "",
        "key": LOCK,
        "value": True,
        "expected_version": None,
    }
    args.update(overrides)
    with bind_principal(HUMAN):
        return settings_tools.set_setting(**args)


def test_the_lock_key_is_a_deployment_only_bool_lock_with_safe_value_off():
    key = REGISTRY[LOCK]
    assert (key.kind, key.type, key.safe_value, key.allowed_layers) == (
        "lock",
        "bool",
        False,
        ("deployment",),
    )


@pytest.mark.parametrize(
    "scope, scope_id, repo",
    [
        ("org", "acme", ""),
        ("team", "team-1", ""),
        ("team_repo", "team-1", REPO),
    ],
)
def test_set_setting_refuses_the_lock_key_below_the_deployment_scope(
    store, scope, scope_id, repo
):
    refused = _set(scope=scope, scope_id=scope_id, repo=repo)
    assert refused["ok"] is False
    assert refused["reason"] == "lock_key"


def test_set_setting_accepts_the_lock_key_at_deployment(store):
    accepted = _set()
    assert accepted["ok"] is True


# --- reverify_full_suite under the lock --------------------------------------


def _context():
    return SettingsContext(namespace=DEFAULT_NAMESPACE, team="team-1", repo=REPO)


def _put(scope, scope_id, repo, key, value, expected_version=None):
    with bind_principal(HUMAN):
        result = settings_tools.set_setting(
            scope=scope,
            scope_id=scope_id,
            repo=repo,
            key=key,
            value=value,
            expected_version=expected_version,
        )
    assert result["ok"] is True, result
    return result


def test_reverify_reads_false_when_the_locked_deployment_says_false_and_team_says_true(
    store,
):
    lock = _put("deployment", "", "", LOCK, True)
    _put("deployment", "", "", "reverify_full_suite", False, lock["new_version"])
    _put("team", "team-1", "", "reverify_full_suite", True)

    assert _context().value("reverify_full_suite") is False


def test_reverify_reads_the_safe_value_when_the_lock_is_on_and_deployment_is_unset(
    store,
):
    _put("deployment", "", "", LOCK, True)
    _put("team", "team-1", "", "reverify_full_suite", True)

    assert _context().value("reverify_full_suite") is REGISTRY["reverify_full_suite"].safe_value


def test_reverify_reads_the_team_value_when_the_lock_is_off(store):
    _put("team", "team-1", "", "reverify_full_suite", True)

    assert _context().value("reverify_full_suite") is True
