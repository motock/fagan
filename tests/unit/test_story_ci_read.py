"""The get_story_ci read tool: one CI snapshot for a story's pull request.

Only the forge adapter (``get_forge``) and the store are stubbed. The forge
fake records every call, so a test can assert both the answer and whether the
forge was asked at all. ``gh`` is never run.
"""

import inspect
import logging

import pytest

from pipeline import server, story_ci_read
from pipeline.actor import bind_namespace
from pipeline.forge import FAIL, NONE, PASS, PENDING, CIStatusUnreadable
from pipeline.story_evidence import NOT_FOUND

PLAN = "alpha"
KEY = "S-1"
PR_URL = "https://github.com/acme/app/pull/7"
SECRET_URL = "https://user:secret-url@ci.example.com/logs"
REDACTED = "[redacted]"


class FakeStore:
    """Manifests keyed by (namespace, plan), read the way the real store reads them."""

    def __init__(self):
        self.manifests = {}
        self.reads = 0

    def add(self, plan, stories, namespace="default", repo_root="/repos/alpha"):
        self.manifests[(namespace, plan)] = {"name": plan, "repo_root": repo_root, "stories": stories}

    def get_manifest_or_none(self, plan_name):
        from pipeline.actor import current_namespace

        self.reads += 1
        return self.manifests.get((current_namespace(), plan_name))

    def get_manifest(self, plan_name):
        manifest = self.get_manifest_or_none(plan_name)
        if manifest is None:
            raise FileNotFoundError(plan_name)
        return manifest

    def records(self):
        return self

    def list_plans(self, namespace):
        return sorted(p for (ns, p) in self.manifests if ns == namespace)


class FakeForge:
    """Answers the three adapter questions and records each call."""

    def __init__(self, snapshot=None, excerpt=None, ci_configured=True, error=None):
        self.snapshot = {} if snapshot is None else snapshot
        self.excerpt = excerpt
        self.ci_configured = ci_configured
        self.error = error
        self.snapshot_calls = []
        self.excerpt_calls = []
        self.config_calls = []

    def checks_snapshot(self, branch, sha):
        self.snapshot_calls.append((branch, sha))
        if self.error is not None:
            raise self.error
        return self.snapshot

    def has_ci_config(self, repo_root):
        self.config_calls.append(repo_root)
        return self.ci_configured

    def failure_log_excerpt(self, branch):
        self.excerpt_calls.append(branch)
        return self.excerpt

    def calls(self):
        return len(self.snapshot_calls) + len(self.excerpt_calls) + len(self.config_calls)


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    fake.add(PLAN, {KEY: {"summary": "a story", "status": "done", "pr_url": PR_URL}})
    monkeypatch.setattr(server, "_store", fake)
    return fake


@pytest.fixture
def forge(store, monkeypatch):
    fake = FakeForge()
    monkeypatch.setattr(story_ci_read, "get_forge", lambda root: fake)
    return fake


# --- reply shape -----------------------------------------------------------


def test_should_report_failing_state_with_sorted_checks_and_excerpt(forge):
    forge.snapshot = {"test": FAIL, "lint": PASS, "build": FAIL}
    forge.excerpt = "FAILED tests/test_x.py::test_y"
    out = story_ci_read.get_story_ci(PLAN, KEY)
    assert out == {
        "available": True,
        "pr_url": PR_URL,
        "state": FAIL,
        "checks": [
            {"name": "build", "state": FAIL},
            {"name": "lint", "state": PASS},
            {"name": "test", "state": FAIL},
        ],
        "failure_excerpt": "FAILED tests/test_x.py::test_y",
    }


def test_should_return_no_excerpt_and_never_fetch_one_when_passing(forge):
    forge.snapshot = {"test": PASS}
    out = story_ci_read.get_story_ci(PLAN, KEY)
    assert out["failure_excerpt"] is None
    assert forge.excerpt_calls == []


def test_should_report_pending_for_an_empty_snapshot_when_ci_is_configured(forge):
    forge.snapshot = {}
    forge.ci_configured = True
    assert story_ci_read.get_story_ci(PLAN, KEY)["state"] == PENDING


def test_should_report_none_for_an_empty_snapshot_when_ci_is_not_configured(forge):
    forge.snapshot = {}
    forge.ci_configured = False
    assert story_ci_read.get_story_ci(PLAN, KEY)["state"] == NONE


def test_should_ask_the_forge_for_one_snapshot_of_the_branch_head(forge):
    forge.snapshot = {"test": PASS}
    story_ci_read.get_story_ci(PLAN, KEY)
    assert forge.snapshot_calls == [(f"agent/{KEY.lower()}", "")]


# --- no pull request -------------------------------------------------------


def test_should_report_no_pull_request_without_calling_the_forge(store, forge):
    store.add(PLAN, {KEY: {"summary": "a story", "status": "in_progress"}})
    out = story_ci_read.get_story_ci(PLAN, KEY)
    assert out == {"available": False, "reason": "no_pull_request"}
    assert forge.calls() == 0


# --- masking and bounds ----------------------------------------------------


def test_should_mask_a_token_shaped_value_in_the_excerpt_and_a_check_name(forge):
    token = "ghp_" + "a" * 36
    forge.snapshot = {f"job {token}": FAIL}
    forge.excerpt = f"auth failed with {token}"
    out = story_ci_read.get_story_ci(PLAN, KEY)
    text = repr(out)
    assert token not in text
    assert REDACTED in out["failure_excerpt"]
    assert REDACTED in out["checks"][0]["name"]


def test_should_mask_a_secret_straddling_character_4000_instead_of_splitting_it(forge):
    token = "ghp_" + "b" * 36
    # The token starts at character 3986 and ends at 4026: a raw cut at 4000 would split it.
    prefix = "x" * 3985 + " "
    forge.snapshot = {"test": FAIL}
    forge.excerpt = prefix + token + " tail"
    out = story_ci_read.get_story_ci(PLAN, KEY)
    assert "ghp_" not in out["failure_excerpt"]
    assert "bbbb" not in out["failure_excerpt"]
    assert REDACTED in out["failure_excerpt"]
    assert len(out["failure_excerpt"]) <= 4000


def test_should_cut_a_long_excerpt_to_4000_characters(forge):
    forge.snapshot = {"test": FAIL}
    forge.excerpt = "a" * 4001
    assert len(story_ci_read.get_story_ci(PLAN, KEY)["failure_excerpt"]) == 4000


def test_should_keep_at_most_100_checks_and_mark_the_cut(forge):
    forge.snapshot = {f"check-{i:03d}": PASS for i in range(101)}
    out = story_ci_read.get_story_ci(PLAN, KEY)
    assert len(out["checks"]) == 100
    assert out["truncated"] is True


def test_should_cut_a_check_name_to_200_characters(forge):
    forge.snapshot = {"n" * 300: PASS}
    assert len(story_ci_read.get_story_ci(PLAN, KEY)["checks"][0]["name"]) == 200


# --- unreadable CI ---------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [CIStatusUnreadable("gh said secret-url"), RuntimeError(f"cannot reach {SECRET_URL}")],
    ids=["CIStatusUnreadable", "RuntimeError"],
)
def test_should_report_ci_unreadable_without_the_error_text(forge, caplog, error):
    forge.error = error
    with caplog.at_level(logging.WARNING, logger=story_ci_read.__name__):
        out = story_ci_read.get_story_ci(PLAN, KEY)
    assert out["available"] is False
    assert out["reason"] == "ci_unreadable"
    assert "correlation_id" in out
    assert "secret-url" not in repr(out)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "secret-url" not in warnings[0].getMessage()
    assert "secret-url" not in repr(warnings[0].__dict__)


# --- refusals --------------------------------------------------------------


def test_should_return_the_404_reply_for_an_unknown_plan_without_a_forge_call(store, forge):
    out = story_ci_read.get_story_ci("nope", KEY)
    assert out == NOT_FOUND
    assert forge.calls() == 0


def test_should_return_the_404_reply_for_an_unknown_story_without_a_forge_call(store, forge):
    out = story_ci_read.get_story_ci(PLAN, "S-404")
    assert out == NOT_FOUND
    assert forge.calls() == 0


@pytest.mark.parametrize("plan,key", [("../etc", KEY), ("a/b", KEY), (PLAN, "S 1"), (PLAN, "..")])
def test_should_refuse_an_invalid_key_before_any_store_read(store, forge, plan, key):
    with pytest.raises(ValueError):
        story_ci_read.get_story_ci(plan, key)
    assert store.reads == 0
    assert forge.calls() == 0


# --- branch, signature, gate, namespace -----------------------------------


def test_should_pass_exactly_the_story_branch_to_the_forge(forge):
    forge.snapshot = {"test": PASS}
    story_ci_read.get_story_ci(PLAN, KEY)
    assert forge.snapshot_calls[0][0] == "agent/s-1"


def test_should_take_only_plan_name_and_story_key_parameters():
    assert list(inspect.signature(story_ci_read.get_story_ci).parameters) == ["plan_name", "story_key"]


def test_should_return_the_same_result_with_the_merge_ci_gate_off(store, forge, monkeypatch):
    forge.snapshot = {"test": FAIL}
    forge.excerpt = "boom"
    gated_on = story_ci_read.get_story_ci(PLAN, KEY)
    monkeypatch.setenv("PIPELINE_MERGE_CI_GATE", "false")
    assert story_ci_read.get_story_ci(PLAN, KEY) == gated_on


def test_should_not_read_a_manifest_from_another_namespace(store, forge):
    store.add(PLAN, {KEY: {"summary": "a story", "status": "done", "pr_url": PR_URL}}, namespace="team-a")
    with bind_namespace("team-b"):
        assert story_ci_read.get_story_ci(PLAN, KEY) == NOT_FOUND
    assert forge.calls() == 0
