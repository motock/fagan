"""Story listing and single-story read tools: list_plan_stories and get_story.

The real ``PipelineService`` runs (``list_ready_stories`` is the readiness
rule), and only the store is stubbed. The fake store keys manifests by the
namespace bound for the call, as the real store does.
"""

import pytest

from pipeline import server
from pipeline.actor import bind_namespace
from pipeline.plan_story_read import get_story, list_plan_stories
from pipeline.story_evidence import NOT_FOUND

PLAN = "alpha"
REDACTED = "[redacted]"


class _Records:
    def __init__(self, store):
        self._store = store

    def list_plans(self, namespace):
        return sorted(p for (ns, p) in self._store.manifests if ns == namespace)

    def get_manifests(self, namespace):
        return {p: m for (ns, p), m in self._store.manifests.items() if ns == namespace}


class FakeStore:
    """Manifests keyed by (namespace, plan); counts every manifest read."""

    def __init__(self):
        self.manifests = {}
        self.reads = 0

    def add(self, plan, stories, namespace="default"):
        self.manifests[(namespace, plan)] = {"name": plan, "stories": stories}

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
        return _Records(self)


def _story(status="todo", **extra):
    return {"summary": "a story", "status": status, "dependencies": [], "agent_instructions": "brief", **extra}


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(server, "_store", fake)
    return fake


# --- list_plan_stories -----------------------------------------------------


def test_should_list_every_story_in_manifest_order_with_the_ten_fields(store):
    store.add(PLAN, {"B": _story(), "A": _story("done", risk="high", dependencies=["X"], blocked_by=["p:q"], pr_url="https://x/1")})
    out = list_plan_stories(PLAN)
    assert [s["key"] for s in out["stories"]] == ["B", "A"]
    assert set(out["stories"][1]) == {
        "key", "summary", "status", "risk", "dependencies", "blocked_by",
        "pr_url", "parked_reason", "failure_reason", "ready",
    }
    assert out["stories"][1]["risk"] == "high"
    assert out["stories"][1]["pr_url"] == "https://x/1"


def test_should_carry_the_parked_reason_of_a_parked_story(store):
    store.add(PLAN, {"P": _story("parked", parked_reason="tests red twice")})
    out = list_plan_stories(PLAN)
    assert out["stories"][0]["parked_reason"] == "tests red twice"


def test_should_count_stories_by_status(store):
    store.add(PLAN, {"A": _story("done"), "B": _story("todo"), "C": _story("done")})
    assert list_plan_stories(PLAN)["counts"] == {"done": 2, "todo": 1}


def test_should_mark_ready_only_for_the_keys_list_ready_stories_returns(store):
    store.add(PLAN, {"A": _story("todo"), "B": _story("parked"), "C": _story("todo", dependencies=["A"])})
    out = list_plan_stories(PLAN)
    ready = {s["key"] for s in out["stories"] if s["ready"]}
    assert ready == {"A"}


def test_should_mask_a_secret_value_in_the_summary(store):
    store.add(PLAN, {"A": _story(summary="deploy with API_KEY=hunter2")})
    summary = list_plan_stories(PLAN)["stories"][0]["summary"]
    assert "hunter2" not in summary
    assert REDACTED in summary


def test_should_cut_a_long_parked_reason_to_300_characters(store):
    store.add(PLAN, {"P": _story("parked", parked_reason="r" * 301)})
    assert len(list_plan_stories(PLAN)["stories"][0]["parked_reason"]) == 300


def test_should_cap_the_listing_at_500_stories_and_flag_truncation(store):
    store.add(PLAN, {f"S{i}": _story() for i in range(501)})
    out = list_plan_stories(PLAN)
    assert (len(out["stories"]), out["truncated"]) == (500, True)


def test_should_return_an_empty_listing_for_a_plan_with_no_stories(store):
    store.add(PLAN, {})
    assert list_plan_stories(PLAN) == {"ok": True, "plan": PLAN, "stories": [], "counts": {}}


def test_should_refuse_an_unknown_plan_with_the_404_reply(store):
    assert list_plan_stories("missing") == NOT_FOUND


def test_should_refuse_a_manifest_whose_stories_is_not_a_dict(store):
    store.add(PLAN, ["A", "B"])
    assert list_plan_stories(PLAN) == NOT_FOUND


def test_should_refuse_an_invalid_plan_name_before_any_store_read(store):
    with pytest.raises(ValueError):
        list_plan_stories("../etc")
    assert store.reads == 0


def test_should_omit_agent_instructions_from_the_listing(store):
    store.add(PLAN, {"A": _story(acceptance=[{"path": "t.py"}], log="x", worktree="/w", pid=7)})
    story = list_plan_stories(PLAN)["stories"][0]
    assert "agent_instructions" not in story
    assert not {"acceptance", "log", "worktree", "pid"} & set(story)


def test_should_not_show_a_manifest_from_another_namespace(store):
    store.add(PLAN, {"A": _story()}, namespace="team-a")
    with bind_namespace("team-b"):
        assert list_plan_stories(PLAN) == NOT_FOUND


# --- get_story -------------------------------------------------------------


def test_should_return_the_brief_and_the_listed_fields_for_one_story(store):
    store.add(PLAN, {"A": _story("parked", risk="low", persona="software-engineer", files=["a.py"], attempts=2, parked_reason="stuck")})
    out = get_story(PLAN, "A")
    assert out["ok"] is True
    assert out["key"] == "A"
    assert out["agent_instructions"] == "brief"
    assert out["attempts"] == 2
    assert out["files"] == ["a.py"]
    assert out["parked_reason"] == "stuck"


def test_should_mask_a_secret_value_in_the_brief(store):
    store.add(PLAN, {"A": _story(agent_instructions="use token=hunter2 here")})
    brief = get_story(PLAN, "A")["agent_instructions"]
    assert "hunter2" not in brief
    assert REDACTED in brief


def test_should_mask_a_secret_value_in_one_story_summary(store):
    store.add(PLAN, {"A": _story(summary="rotate PASSWORD=hunter2")})
    assert "hunter2" not in get_story(PLAN, "A")["summary"]


def test_should_cut_a_brief_over_60000_characters_and_flag_it(store):
    store.add(PLAN, {"A": _story(agent_instructions="b" * 60001)})
    out = get_story(PLAN, "A")
    assert (len(out["agent_instructions"]), out["agent_instructions_truncated"]) == (60000, True)


def test_should_return_the_404_reply_for_an_unknown_story(store):
    store.add(PLAN, {"A": _story()})
    assert get_story(PLAN, "NOPE") == NOT_FOUND


def test_should_return_the_404_reply_for_an_unknown_plan(store):
    assert get_story("missing", "A") == NOT_FOUND


def test_should_refuse_an_invalid_story_key_before_any_store_read(store):
    store.add(PLAN, {"A": _story()})
    with pytest.raises(ValueError):
        get_story(PLAN, "a/../b")
    assert store.reads == 0


def test_should_omit_log_worktree_and_pid_from_one_story(store):
    store.add(PLAN, {"A": _story(log="secret log", worktree="/srv/w", pid=99, owner="bob", review_scores={"x": 1})})
    out = get_story(PLAN, "A")
    assert not {"log", "worktree", "pid", "owner", "review_scores"} & set(out)


def test_should_not_show_one_story_from_another_namespace(store):
    store.add(PLAN, {"A": _story()}, namespace="team-a")
    with bind_namespace("team-b"):
        assert get_story(PLAN, "A") == NOT_FOUND
