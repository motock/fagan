"""``allow_loosen``: the layer above opts a ceiling key in to being loosened.

Writes go through ``settings_tools.set_setting`` (and the seed CLI) against a
real file-backed store and are read back through the real resolver
(``effective_setting``). Only the authorizer and the settings-plugin probe are
stubbed.
"""

import pytest

from app.identity import Principal
from pipeline import audit, authorization, persistence, settings_seed, settings_tools
from pipeline import server as p
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.settings_registry import REGISTRY, SettingKey
from pipeline.settings_store import effective_setting
from pipeline.store_records import DEFAULT_NAMESPACE

REPO = "github.com/acme/widgets"
TEAM = "team-1"
HUMAN = Principal("user:alice", auth_method="oidc")


class _Allow:
    def allow(self, principal, action, resource=None):
        return True


class _Deny:
    def allow(self, principal, action, resource=None):
        return False


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    return p._store


@pytest.fixture
def authorized(monkeypatch, store):
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    monkeypatch.setattr(authorization, "default_authorizer", lambda: _Allow())
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: False)


@pytest.fixture
def lock_key(monkeypatch):
    lock = SettingKey(
        name="lock_probe",
        type="bool",
        kind="lock",
        safe_value=True,
        allowed_layers=("deployment",),
        legacy_env="PIPELINE_LOCK_PROBE",
    )
    monkeypatch.setitem(REGISTRY, lock.name, lock)
    return lock.name


def _set(**overrides):
    args = {
        "scope": "deployment",
        "scope_id": "",
        "repo": "",
        "key": "merge_ci_gate",
        "value": True,
        "expected_version": None,
    }
    args.update(overrides)
    return settings_tools.set_setting(**args)


def _effective(key="merge_ci_gate"):
    return effective_setting(key, namespace=DEFAULT_NAMESPACE, team=TEAM, repo=REPO)


def _record(scope="deployment", scope_id="", repo=""):
    return settings_tools.get_settings(scope, scope_id, repo)["record"]


def _set_team_repo(value, expected_version=None):
    return _set(
        scope="team_repo",
        scope_id=TEAM,
        repo=REPO,
        value=value,
        expected_version=expected_version,
    )


# --- resolver behaviour through the real store -----------------------------


def test_team_repo_loosening_a_ceiling_is_refused_without_allow_loosen(authorized):
    _set(value=True)
    _set_team_repo(False)

    effective = _effective()

    assert effective["value"] is True
    assert effective["source"] == "deployment"
    assert [r["layer"] for r in effective["lock_rejections"]] == ["team_repo"]


def test_team_repo_loosening_applies_once_the_deployment_layer_allows_it(authorized):
    _set(value=True, allow_loosen=["merge_ci_gate"])
    _set_team_repo(False)

    effective = _effective()

    assert effective["value"] is False
    assert effective["source"] == "team_repo"
    assert effective["lock_rejections"] == []


def test_stored_allow_loosen_is_kept_when_a_later_write_omits_it(authorized):
    _set(value=True, allow_loosen=["merge_ci_gate"])

    _set(value=False, expected_version=1)

    assert _record()["allow_loosen"] == ["merge_ci_gate"]


# --- set_setting: allow_loosen validation (negative cases) ------------------


def test_allow_loosen_naming_a_pref_key_is_refused_and_writes_nothing(authorized):
    result = _set(value=True, allow_loosen=["merge_ci_timeout_s"])

    assert result == {
        "ok": False,
        "reason": "invalid_allow_loosen",
        "error": "allow_loosen must name ceiling settings keys: 'merge_ci_timeout_s'",
    }
    assert _record() is None


def test_allow_loosen_naming_a_lock_key_is_refused_and_writes_nothing(
    authorized, lock_key
):
    result = _set(key=lock_key, value=True, allow_loosen=[lock_key])

    assert result["ok"] is False
    assert result["reason"] == "invalid_allow_loosen"
    assert _record() is None


def test_allow_loosen_naming_an_unknown_key_is_refused_naming_only_the_key(authorized):
    result = _set(value=True, allow_loosen=["no_such_key"])

    assert result["ok"] is False
    assert result["reason"] == "invalid_allow_loosen"
    assert "'no_such_key'" in result["error"]
    assert _record() is None


def test_allow_loosen_that_is_not_a_list_of_strings_is_refused(authorized):
    result = _set(value=True, allow_loosen="merge_ci_gate")

    assert result["ok"] is False
    assert result["reason"] == "invalid_allow_loosen"


def test_allow_loosen_is_refused_at_team_repo_because_nothing_is_below_it(authorized):
    result = _set_team_repo(True, expected_version=None)
    assert result["ok"] is True

    refused = _set(
        scope="team_repo",
        scope_id=TEAM,
        repo=REPO,
        value=True,
        expected_version=1,
        allow_loosen=["merge_ci_gate"],
    )

    assert refused == {
        "ok": False,
        "reason": "allow_loosen_layer",
        "error": "allow_loosen is not writable at the team_repo scope",
    }
    assert _record("team_repo", TEAM, REPO)["allow_loosen"] == []


@pytest.mark.parametrize("scope, scope_id", [("deployment", ""), ("org", "org-1"), ("team", TEAM)])
def test_allow_loosen_is_writable_at_each_layer_above_team_repo(authorized, scope, scope_id):
    result = _set(scope=scope, scope_id=scope_id, value=True, allow_loosen=["merge_ci_gate"])

    assert result["ok"] is True
    assert _record(scope, scope_id)["allow_loosen"] == ["merge_ci_gate"]


def test_allow_loosen_is_not_written_when_the_caller_is_denied(monkeypatch, store):
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    monkeypatch.setattr(authorization, "default_authorizer", lambda: _Deny())
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: False)

    result = _set(value=True, allow_loosen=["merge_ci_gate"])

    assert result["ok"] is False
    assert _record() is None


def test_stale_version_leaves_the_old_record_and_its_allow_loosen_unchanged(authorized):
    _set(value=True, allow_loosen=["merge_ci_gate"])

    result = _set(value=False, expected_version=7, allow_loosen=[])

    assert result["ok"] is False
    assert result["reason"] == "version_conflict"
    record = _record()
    assert record["version"] == 1
    assert record["values"] == {"merge_ci_gate": True}
    assert record["allow_loosen"] == ["merge_ci_gate"]


def test_allowed_write_is_audited_with_the_old_and_new_allow_loosen(authorized):
    _set(value=True, allow_loosen=["merge_ci_gate"])

    chain = audit._audit_log().read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)
    detail = [r for r in chain if r["action"] == "settings:set"][-1]["detail"]

    assert detail["old_allow_loosen"] == []
    assert detail["new_allow_loosen"] == ["merge_ci_gate"]


# --- seed CLI ---------------------------------------------------------------


def _seed(environ, **kwargs):
    return settings_seed.seed_from_env(environ, **kwargs)


def test_seed_writes_allow_loosen_with_the_legacy_value(authorized):
    report = _seed({"PIPELINE_MERGE_CI_GATE": "0"}, allow_loosen=["merge_ci_gate"])

    assert report["ok"] is True
    assert _record()["values"] == {"merge_ci_gate": False}
    assert _record()["allow_loosen"] == ["merge_ci_gate"]


def test_seed_updates_allow_loosen_on_an_already_seeded_layer(authorized):
    _seed({"PIPELINE_MERGE_CI_GATE": "0"})

    report = _seed({}, allow_loosen=["merge_ci_gate"])

    assert report["ok"] is True
    assert _record()["values"] == {"merge_ci_gate": False}
    assert _record()["allow_loosen"] == ["merge_ci_gate"]


def test_seed_dry_run_lists_allow_loosen_and_writes_nothing(authorized):
    report = _seed(
        {"PIPELINE_MERGE_CI_GATE": "0"}, allow_loosen=["merge_ci_gate"], dry_run=True
    )

    assert report["ok"] is True
    assert report["allow_loosen"] == ["merge_ci_gate"]
    assert _record() is None


@pytest.mark.parametrize("bad_key", ["merge_ci_timeout_s", "no_such_key"])
def test_seed_refuses_a_non_ceiling_allow_loosen_key_and_writes_nothing(authorized, bad_key):
    report = _seed({"PIPELINE_MERGE_CI_GATE": "0"}, allow_loosen=[bad_key])

    assert report["ok"] is False
    assert report["reason"] == "invalid_allow_loosen"
    assert _record() is None


def test_seed_refuses_allow_loosen_for_a_team_repo_record(authorized):
    report = _seed(
        {"PIPELINE_MERGE_CI_GATE": "0"},
        team=TEAM,
        repo=REPO,
        allow_loosen=["merge_ci_gate"],
    )

    assert report["ok"] is False
    assert report["reason"] == "allow_loosen_layer"
    assert _record("team_repo", TEAM, REPO) is None


def test_seed_skips_allow_loosen_for_a_key_with_no_stored_value(authorized):
    report = _seed({}, allow_loosen=["merge_ci_gate"])

    assert report["ok"] is False
    assert report["reason"] == "not_set"
    assert _record() is None


# --- no plugin, no allow_loosen: the unchanged path -------------------------


def test_seed_without_allow_loosen_leaves_the_stored_list_alone(authorized):
    _set(value=True, allow_loosen=["merge_ci_gate"])

    report = _seed({"PIPELINE_MERGE_CI_GATE": "0"}, overwrite=True)

    assert report["ok"] is True
    assert "allow_loosen" not in report
    assert _record()["allow_loosen"] == ["merge_ci_gate"]
