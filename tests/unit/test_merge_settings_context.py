"""Merge-gate settings context resolved per (namespace, team, repo) (ts-08).

The merge gates are tenant settings. Each one resolves for the triple the
plan manifest names, through the real Store under ``tmp_path``. Nothing here
reads the process environment or a module-level constant, so two plans in one
process resolve independently.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import persistence, server
from pipeline.ci import (
    DEFAULT_NAMESPACE,
    SettingsContext,
    context_from_manifest,
    gate_record,
)
from pipeline.repo_clone import RepoUrlError

NAMESPACE = "default"
REPO_A = "github.com/acme/app-a"
REPO_B = "github.com/acme/app-b"
LOGGER = "pipeline.settings_store"


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(persistence, "PLAN_DIR", plans)
    yield plans


@pytest.fixture
def records():
    return server._store.records()


def _put(records, scope, scope_id="", repo="", values=None, allow_loosen=None):
    return records.put_settings(
        NAMESPACE,
        scope,
        scope_id,
        repo,
        values or {},
        allow_loosen or [],
        "ops",
        None,
    )


def _manifest(team="alpha", repo_url=f"https://{REPO_A}.git", **extra):
    return {"team": team, "repo_url": repo_url, **extra}


# --- two teams, one repo: each merge resolves its own gate ---


def test_should_resolve_each_team_gate_independently_on_one_repo(records):
    _put(records, "team_repo", "alpha", REPO_A, {"merge_ci_gate": False})

    alpha = context_from_manifest(_manifest(team="alpha"))
    beta = context_from_manifest(_manifest(team="beta"))

    assert alpha.value("merge_ci_gate") is False
    assert beta.value("merge_ci_gate") is True


def test_should_name_the_team_and_repo_each_gate_was_resolved_for(records):
    _put(records, "team_repo", "alpha", REPO_A, {"merge_ci_gate": False})

    record = gate_record(context_from_manifest(_manifest(team="alpha")))

    assert record["merge_ci_gate"] == {
        "value": False,
        "source": "team_repo",
        "team": "alpha",
        "repo": REPO_A,
        "lock_rejections": [],
    }


def test_should_record_a_refused_loosening_in_the_merge_gate_record(records):
    _put(records, "deployment", values={"merge_ci_gate": True})
    _put(records, "team_repo", "alpha", REPO_A, {"merge_ci_gate": False})

    record = gate_record(context_from_manifest(_manifest(team="alpha")))

    assert record["merge_ci_gate"] == {
        "value": True,
        "source": "deployment",
        "team": "alpha",
        "repo": REPO_A,
        "lock_rejections": [
            {"layer": "team_repo", "requested": False, "by": "deployment"}
        ],
    }


def test_should_name_source_team_and_repo_for_every_merge_gate_key(records):
    record = gate_record(context_from_manifest(_manifest(team="beta")))

    assert set(record) == {
        "merge_ci_gate",
        "merge_ci_timeout_s",
        "merge_build_gate",
        "reverify_full_suite",
    }
    for entry in record.values():
        assert entry["source"] == "safe_default"
        assert entry["team"] == "beta"
        assert entry["repo"] == REPO_A


# --- one process, two repos, interleaved calls: no leak ---


def test_should_not_leak_a_gate_from_one_repo_into_interleaved_calls_on_another(records):
    _put(records, "team_repo", "alpha", REPO_A, {"merge_ci_gate": False})
    ctx_a = context_from_manifest(_manifest(repo_url=f"https://{REPO_A}"))
    ctx_b = context_from_manifest(_manifest(repo_url=f"https://{REPO_B}"))

    observed = []
    for ctx in (ctx_a, ctx_b, ctx_a, ctx_b):
        observed.append(ctx.value("merge_ci_gate"))

    assert observed == [False, True, False, True]


def test_should_not_apply_a_team_repo_record_to_another_repo(records):
    _put(records, "team_repo", "alpha", REPO_A, {"merge_ci_gate": False})

    other = context_from_manifest(_manifest(repo_url=f"https://{REPO_B}"))

    assert other.value("merge_ci_gate") is True


# --- Store unreadable: safe values, never a silent OFF ---


class _UnreadableStore:
    def records(self):
        raise OSError("store offline")


def test_should_resolve_gate_on_and_timeout_900_when_store_is_unreadable(
    monkeypatch, caplog
):
    monkeypatch.setattr(server, "_store", _UnreadableStore())
    ctx = context_from_manifest(_manifest())

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gate = ctx.resolve("merge_ci_gate")
        timeout = ctx.resolve("merge_ci_timeout_s")

    assert (gate["value"], gate["source"]) == (True, "settings_error")
    assert (timeout["value"], timeout["source"]) == (900, "settings_error")


def test_should_not_resolve_gate_off_when_store_is_unreadable(monkeypatch):
    monkeypatch.setattr(server, "_store", _UnreadableStore())

    assert context_from_manifest(_manifest()).value("merge_ci_gate") is True


# --- a higher ceiling holds against a narrower loosening ---


def test_should_keep_deployment_ceiling_on_and_record_the_rejection(records):
    _put(records, "deployment", values={"merge_ci_gate": True})
    _put(records, "team_repo", "alpha", REPO_A, {"merge_ci_gate": False})

    result = context_from_manifest(_manifest()).resolve("merge_ci_gate")

    assert (result["value"], result["source"]) == (True, "deployment")
    assert result["lock_rejections"] == [
        {"layer": "team_repo", "requested": False, "by": "deployment"}
    ]


# --- context resolution: defaults, fallbacks, and invalid input ---


def test_should_default_namespace_and_team_when_manifest_names_neither():
    ctx = context_from_manifest({"repo_url": f"https://{REPO_A}"})

    assert ctx.namespace == DEFAULT_NAMESPACE == "default"
    assert ctx.team == "local"


def test_should_use_the_manifest_namespace_when_it_names_one():
    ctx = context_from_manifest(_manifest(namespace="tenant-x"))

    assert ctx.namespace == "tenant-x"


def test_should_fall_back_to_repo_root_when_manifest_has_no_repo_url():
    ctx = context_from_manifest({"team": "alpha", "repo_root": "/srv/repos/app"})

    assert ctx.repo == "/srv/repos/app"


def test_should_normalize_repo_url_to_host_org_name_without_git_suffix():
    ctx = context_from_manifest(_manifest(repo_url=f"https://{REPO_A}.git"))

    assert ctx.repo == REPO_A


def test_should_refuse_a_non_https_repo_url_with_repo_url_error():
    with pytest.raises(RepoUrlError):
        context_from_manifest(_manifest(repo_url=f"http://{REPO_A}"))


def test_should_require_namespace_team_and_repo_with_no_defaults():
    with pytest.raises(TypeError):
        SettingsContext(namespace=NAMESPACE, team="alpha")  # type: ignore[call-arg]


def test_should_raise_key_error_for_a_key_outside_the_registry():
    ctx = context_from_manifest(_manifest())

    with pytest.raises(KeyError):
        ctx.resolve("not_a_setting")
