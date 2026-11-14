"""``python -m pipeline.settings_seed``: legacy env vars into the settings Store.

The seed reads the eight legacy variables from the environment it is given,
parses them with the old readers' rules, and writes through the settings MCP
tools so authorization and the host audit chain apply. The store is real and
file-backed over a throwaway plans directory; only the authorizer and the
settings-plugin probe are stubbed.
"""

import json

import pytest

from pipeline import audit, authorization, persistence, settings_seed, settings_tools
from pipeline import server as p
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.store_records import DEFAULT_NAMESPACE

REPO = "github.com/motock/fagan-enterprise"


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


def _seed(environ, **kwargs):
    return settings_seed.seed_from_env(environ, **kwargs)


def _deployment_values():
    record = settings_tools.get_settings("deployment", "", "")["record"]
    return None if record is None else record["values"]


def _change_records():
    chain = audit._audit_log().read_chain(DEFAULT_NAMESPACE, HOST_CHAIN_ID)
    return [r for r in chain if r["action"] == "settings:set"]


def _written_keys(report):
    return [entry["key"] for entry in report["written"]]


def _skipped(report):
    return {entry["key"]: entry["reason"] for entry in report["skipped"]}


# --- positive ---------------------------------------------------------------


def test_dry_run_reports_planned_writes_and_writes_nothing(authorized):
    report = _seed({"PIPELINE_MERGE_CI_TIMEOUT": "600"}, dry_run=True)

    assert report["ok"] is True
    assert report["dry_run"] is True
    assert [entry["key"] for entry in report["planned"]] == ["merge_ci_timeout_s"]
    assert report["written"] == []
    assert _deployment_values() is None
    assert _change_records() == []


def test_real_run_writes_each_set_legacy_variable_at_the_deployment_layer(authorized):
    environ = {
        "PIPELINE_MERGE_CI_GATE": "0",
        "PIPELINE_MERGE_CI_TIMEOUT": "600",
        "PIPELINE_REWORK_ON_CI_FAIL": "1",
    }

    report = _seed(environ)

    assert sorted(_written_keys(report)) == [
        "merge_ci_gate",
        "merge_ci_timeout_s",
        "rework_on_ci_fail",
    ]
    assert _deployment_values() == {
        "merge_ci_gate": False,
        "merge_ci_timeout_s": 600,
        "rework_on_ci_fail": True,
    }
    assert len(_change_records()) == 3


def test_second_run_is_a_no_op_and_reports_every_key_unchanged(authorized):
    environ = {"PIPELINE_MERGE_CI_TIMEOUT": "600", "PIPELINE_TEST_SCOPE": "scoped"}
    _seed(environ)
    audited_before = len(_change_records())

    report = _seed(environ)

    assert report["written"] == []
    assert sorted(entry["key"] for entry in report["unchanged"]) == [
        "merge_ci_timeout_s",
        "test_scope",
    ]
    assert _deployment_values() == {"merge_ci_timeout_s": 600, "test_scope": "scoped"}
    assert len(_change_records()) == audited_before


def test_differing_stored_value_is_skipped_without_overwrite(authorized):
    _seed({"PIPELINE_MERGE_CI_TIMEOUT": "600"})

    report = _seed({"PIPELINE_MERGE_CI_TIMEOUT": "900"})

    assert report["written"] == []
    assert _skipped(report) == {"merge_ci_timeout_s": "already_set"}
    assert _deployment_values() == {"merge_ci_timeout_s": 600}


def test_differing_stored_value_is_replaced_with_overwrite(authorized):
    _seed({"PIPELINE_MERGE_CI_TIMEOUT": "600"})

    report = _seed({"PIPELINE_MERGE_CI_TIMEOUT": "900"}, overwrite=True)

    assert _written_keys(report) == ["merge_ci_timeout_s"]
    assert report["written"][0]["version"] == 2
    assert _deployment_values() == {"merge_ci_timeout_s": 900}


def test_team_and_repo_write_a_team_repo_record_instead_of_the_deployment_layer(
    authorized,
):
    report = _seed({"PIPELINE_MERGE_CI_GATE": "0"}, team="local", repo=REPO)

    assert _written_keys(report) == ["merge_ci_gate"]
    assert report["scope"] == "team_repo"
    assert _deployment_values() is None
    record = settings_tools.get_settings("team_repo", "local", REPO)["record"]
    assert record["values"] == {"merge_ci_gate": False}


def test_test_scope_surrounding_whitespace_is_stripped_before_the_check(authorized):
    report = _seed({"PIPELINE_TEST_SCOPE": "  scoped  "})

    assert _written_keys(report) == ["test_scope"]
    assert _deployment_values() == {"test_scope": "scoped"}


def test_unset_and_blank_variables_are_ignored(authorized):
    report = _seed({"PIPELINE_MERGE_CI_TIMEOUT": "", "PIPELINE_TEST_SCOPE": "   "})

    assert report["written"] == []
    assert report["unchanged"] == []
    assert report["skipped"] == []
    assert _deployment_values() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", False), ("1", True), ("true", True), ("false", True)],
)
def test_merge_ci_gate_is_off_only_for_zero_like_the_gate_reader(
    authorized, raw, expected
):
    # ci.py reads ``!= "0"``: any spelling but "0" leaves the gate on.
    _seed({"PIPELINE_MERGE_CI_GATE": raw})

    assert _deployment_values() == {"merge_ci_gate": expected}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("0", False), ("true", False)],
)
def test_rework_on_ci_fail_is_on_only_for_one_like_its_reader(authorized, raw, expected):
    # advance_merge.py reads ``== "1"``: "true" does not turn the rework on.
    _seed({"PIPELINE_REWORK_ON_CI_FAIL": raw})

    assert _deployment_values() == {"rework_on_ci_fail": expected}


@pytest.mark.parametrize(("raw", "expected"), [("0", False), ("1", True)])
def test_reverify_full_suite_parses_zero_and_one(authorized, raw, expected):
    _seed({"PIPELINE_REVERIFY_FULL_SUITE": raw})

    assert _deployment_values() == {"reverify_full_suite": expected}


def test_main_prints_one_json_object_and_exits_zero(authorized, capsys):
    code = settings_seed.main(
        ["--dry-run"], environ={"PIPELINE_MERGE_CI_TIMEOUT": "600"}
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["ok"] is True
    assert report["planned"][0]["key"] == "merge_ci_timeout_s"


# --- negative ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "key", "reason"),
    [
        ({"PIPELINE_MERGE_CI_TIMEOUT": "0"}, "merge_ci_timeout_s", "invalid_value"),
        ({"PIPELINE_MERGE_CI_TIMEOUT": "abc"}, "merge_ci_timeout_s", "invalid_value"),
        ({"PIPELINE_MERGE_CI_TIMEOUT": "1e3"}, "merge_ci_timeout_s", "invalid_value"),
        ({"PIPELINE_MERGE_CI_TIMEOUT": "86401"}, "merge_ci_timeout_s", "invalid_value"),
        ({"PIPELINE_GRADE_XDIST_WORKERS": "65"}, "grade_xdist_workers", "invalid_value"),
        ({"PIPELINE_GRADE_XDIST_WORKERS": "-1"}, "grade_xdist_workers", "invalid_value"),
        ({"PIPELINE_TEST_SCOPE": "partial"}, "test_scope", "invalid_value"),
    ],
)
def test_an_invalid_legacy_value_is_skipped_and_never_written(
    authorized, environ, key, reason
):
    report = _seed(environ)

    assert report["written"] == []
    assert _skipped(report) == {key: reason}
    assert _deployment_values() is None
    assert _change_records() == []


def test_one_invalid_value_does_not_block_the_valid_ones(authorized):
    report = _seed({"PIPELINE_MERGE_CI_TIMEOUT": "abc", "PIPELINE_TEST_SCOPE": "scoped"})

    assert _written_keys(report) == ["test_scope"]
    assert _skipped(report) == {"merge_ci_timeout_s": "invalid_value"}


def test_repo_without_team_is_refused_before_anything_is_read(authorized, capsys):
    with pytest.raises(SystemExit) as refused:
        settings_seed.main(["--repo", REPO], environ={"PIPELINE_MERGE_CI_GATE": "0"})

    assert refused.value.code == 2
    assert "--team and --repo" in capsys.readouterr().err
    assert _deployment_values() is None


def test_team_without_repo_is_refused(authorized):
    with pytest.raises(SystemExit) as refused:
        settings_seed.main(["--team", "local"], environ={"PIPELINE_MERGE_CI_GATE": "0"})

    assert refused.value.code == 2


def test_a_refused_write_is_reported_as_skipped_with_the_refusal_reason(
    monkeypatch, store
):
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    monkeypatch.setattr(authorization, "default_authorizer", lambda: _Deny())
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: False)

    report = _seed({"PIPELINE_MERGE_CI_GATE": "0"})

    assert report["written"] == []
    assert _skipped(report) == {"merge_ci_gate": "forbidden"}
    assert _deployment_values() is None

