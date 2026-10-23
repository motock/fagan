"""Opt-in PIPELINE_BRANCH_NAMESPACE and the recorded per-story ``branch``
(docs/plans/LOCAL_FLEET_BRANCH_SAFETY_PLAN.md section 2)."""
import json
import subprocess

import pytest
from test_branch_guard import _FakeProc, _manifest, _story, env, origin_and_repo  # noqa: F401

from pipeline import pr as ppr
from pipeline.branch_names import story_branch


@pytest.fixture(autouse=True)
def _clear_namespace(monkeypatch):
    monkeypatch.delenv("PIPELINE_BRANCH_NAMESPACE", raising=False)


def test_should_keep_plain_name_when_namespace_unset():
    assert story_branch("SETUP-01") == "agent/setup-01"


def test_should_keep_plain_name_when_namespace_empty(monkeypatch):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "")
    assert story_branch("S1") == "agent/s1"


def test_should_insert_namespace_when_set(monkeypatch):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "alice")
    assert story_branch("SETUP-01") == "agent/alice/setup-01"


def test_should_prefer_recorded_branch_over_namespace(monkeypatch):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "alice")
    assert story_branch("S1", recorded="agent/s1") == "agent/s1"


@pytest.mark.parametrize("bad", ["Alice", "-a", "a/b", "a b", "x" * 33, "a_b"])
def test_should_reject_invalid_namespace_naming_variable(monkeypatch, bad):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", bad)
    with pytest.raises(ValueError, match="PIPELINE_BRANCH_NAMESPACE"):
        story_branch("S1")


def test_should_accept_32_char_namespace(monkeypatch):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "a" * 32)
    assert story_branch("S1") == f"agent/{'a' * 32}/s1"


def test_should_refuse_key_equal_to_namespace(monkeypatch):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "alice")
    with pytest.raises(ValueError, match="alice"):
        story_branch("ALICE")


def test_should_allow_recorded_branch_even_when_key_equals_namespace(monkeypatch):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "alice")
    assert story_branch("alice", recorded="agent/alice") == "agent/alice"


def test_resolver_should_treat_namespaced_alias_suffix_as_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "alice")
    subprocess.run(["git", "init", "-q", "-b", "agent/alice/s1-followup", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    assert ppr._resolve_story_branch(str(tmp_path), "S1") == "agent/alice/s1-followup"


def test_resolver_should_use_recorded_branch_for_alias_check(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "agent/old-followup", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    assert (ppr._resolve_story_branch(str(tmp_path), "NEW", recorded="agent/old")
            == "agent/old-followup")


def test_dispatch_should_record_namespaced_branch(env, origin_and_repo, monkeypatch):  # noqa: F811
    plans, _wt = env
    _origin, repo = origin_and_repo
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "alice")
    _manifest(plans, repo)
    from pipeline import server as p

    result = p.dispatch_story("g", "S1")

    assert result["ok"] is True
    assert _story(plans)["branch"] == "agent/alice/s1"


def test_dispatch_should_record_plain_branch_when_unset(env, origin_and_repo):  # noqa: F811
    plans, _wt = env
    _origin, repo = origin_and_repo
    _manifest(plans, repo)
    from pipeline import server as p

    p.dispatch_story("g", "S1")

    assert _story(plans)["branch"] == "agent/s1"


def test_dispatch_should_refuse_key_equal_to_namespace(env, origin_and_repo, monkeypatch):  # noqa: F811
    plans, wt = env
    _origin, repo = origin_and_repo
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "s1")
    _manifest(plans, repo)
    from pipeline import server as p

    result = p.dispatch_story("g", "S1")

    assert result["ok"] is False
    assert "PIPELINE_BRANCH_NAMESPACE" in result["error"]
    assert not (wt / "S1").exists()


def test_dispatch_should_keep_recorded_branch_for_story_dispatched_before_namespace(
        env, origin_and_repo, monkeypatch):  # noqa: F811
    plans, _wt = env
    _origin, repo = origin_and_repo
    monkeypatch.setenv("PIPELINE_BRANCH_NAMESPACE", "alice")
    _manifest(plans, repo)
    path = plans / "g.manifest.json"
    data = json.loads(path.read_text())
    data["stories"]["S1"]["branch"] = "agent/s1"
    path.write_text(json.dumps(data))
    from pipeline import server as p

    p.dispatch_story("g", "S1")

    assert _story(plans)["branch"] == "agent/s1"


def test_story_without_recorded_branch_resumes_on_convention_name():
    assert story_branch("S1", recorded=None) == "agent/s1"
    assert ppr._convention_branch("S1") == "agent/s1"
