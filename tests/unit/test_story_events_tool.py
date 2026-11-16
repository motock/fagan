"""The ``get_story_events`` read tool: a story's newest-first event history.

The tool answers "what happened to this story?" from the plan's notification
history, which is the only place a security ``REQUEST_CHANGES`` verdict can
be recorded (the manifest keeps the review feedback, never the verdict
itself).  It is a plain read: it never writes, and it returns the newest
events first so the most recent verdict is the first thing a caller reads.

Every case here drives the tool through its public entry point and reads the
answers out of the returned payload.  The store is the seam: the tool reads
the plan's notification history through the configured store, in the namespace
bound for the request, so the doubles below stand in for that store rather
than for the tool's internals.
"""

# ruff: noqa: I001
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from pipeline import server
from pipeline import server_tools_read, story_evidence
from pipeline.actor import bind_namespace, current_namespace
from pipeline.remote_tool_policy import (
    PLAN_SCOPED_READ,
    REMOTE_READ,
    remote_tool_allowed,
    remote_tool_policy,
)

TOOL_NAME = "get_story_events"

#: The fields every returned event carries.
EVENT_FIELDS = {"seq", "ts", "event", "message"}

#: The refusal the other plan-scoped read tools answer with.
NOT_FOUND = {"ok": False, "status": 404, "error": "unknown plan/story"}


def _tool():
    """Return the public ``get_story_events`` callable.

    The tool is looked up in the read-tool module first, then in the story
    evidence module, and finally through the server's registered tool
    manager -- so the test binds to the tool's public name, not to the module
    that happens to host it today.
    """
    for module in (server_tools_read, story_evidence):
        candidate = getattr(module, TOOL_NAME, None)
        if callable(candidate):
            return candidate
    registered = server.mcp._tool_manager.get_tool(TOOL_NAME)
    assert registered is not None, f"{TOOL_NAME} is not registered"
    return registered.fn


def _events(result: dict) -> list[dict]:
    """Return the event list out of a tool result."""
    assert isinstance(result, dict), result
    for key in ("events", "entries"):
        value = result.get(key)
        if isinstance(value, list):
            return value
    raise AssertionError(f"no event list in the result: {sorted(result)}")


def _not_found(result: dict) -> bool:
    """Return whether *result* is the shared unknown plan/story refusal."""
    return (
        result.get("ok") is False
        and result.get("status") == 404
        and result.get("error") == NOT_FOUND["error"]
    )


# ---------------------------------------------------------------------------
# Store doubles
# ---------------------------------------------------------------------------


class _Records:
    """The namespaced record view a store exposes."""

    def __init__(self, owner: _Store) -> None:
        self._owner = owner

    def get_notification_history(self, namespace: str, plan_name: str):
        self._owner.reads.append((namespace, plan_name))
        records = self._owner.history.get((namespace, plan_name), [])
        return copy.deepcopy(records), 0

    def get_manifest(self, namespace: str, plan_name: str):
        self._owner.manifest_reads.append((namespace, plan_name))
        manifest = self._owner.manifests.get((namespace, plan_name))
        return copy.deepcopy(manifest) if manifest is not None else None

    def list_plans(self, namespace: str) -> list[str]:
        return sorted(plan for (ns, plan) in self._owner.manifests if ns == namespace)


class _Store:
    """A stand-in for the configured store.

    It serves the plan manifests and the notification history, and records
    which namespace every read asked for, so a test can prove the tool reads
    the namespace bound for the request rather than a fixed one.  Every write
    it is asked to perform fails loudly: the tool is a read.
    """

    def __init__(self) -> None:
        self.manifests: dict[tuple[str, str], dict] = {}
        self.history: dict[tuple[str, str], list[dict]] = {}
        self.reads: list[tuple[str, str]] = []
        self.manifest_reads: list[tuple[str, str]] = []
        self.writes: list[str] = []
        self._records = _Records(self)

    # -- the record view ---------------------------------------------
    def records(self) -> _Records:
        return self._records

    # -- the manifest view --------------------------------------------
    def get_manifest_or_none(self, plan_name: str):
        for (_namespace, plan), manifest in self.manifests.items():
            if plan == plan_name:
                return copy.deepcopy(manifest)
        return None

    def get_manifest(self, plan_name: str):
        manifest = self.get_manifest_or_none(plan_name)
        if manifest is None:
            raise FileNotFoundError(plan_name)
        return manifest

    def list_plans(self, namespace: str) -> list[str]:
        return self._records.list_plans(namespace)

    def get_notification_records(self, plan_name: str, limit: int = 100):
        """The legacy reader: one fixed namespace, no namespace argument."""
        records, _ = self._records.get_notification_history("default", plan_name)
        return records[-limit:] if limit > 0 else records

    # -- writes are refused -------------------------------------------
    def _refuse(self, what: str):
        self.writes.append(what)
        raise AssertionError(f"the read tool must not write: {what}")

    def append_notification(self, namespace, plan_name, record):
        self._refuse("append_notification")

    def save_manifest(self, plan_name, manifest):
        self._refuse("save_manifest")

    def put_manifest(self, namespace, plan_name, manifest):
        self._refuse("put_manifest")


def _notification(
    story_key: str,
    event: str,
    message: str | None,
    ts: str,
) -> dict:
    """Return one notification record as the pipeline writes it."""
    return {
        "ts": ts,
        "plan": "alpha",
        "message": message,
        "story_key": story_key,
        "severity": "info",
        "event": event,
        "dedup_key": f"{story_key}:{event}",
    }


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    """Install a stand-in store as the configured store."""
    fake = _Store()
    monkeypatch.setattr(server, "_store", fake)
    return fake


@pytest.fixture
def plan(store: _Store) -> _Store:
    """A plan with one story, ``S-1``, and no events yet."""
    store.manifests[(current_namespace(), "alpha")] = {
        "plan": "alpha",
        "stories": {"S-1": {"status": "in_progress"}},
    }
    return store


# ---------------------------------------------------------------------------
# The tool is registered and classified
# ---------------------------------------------------------------------------


def test_should_register_the_tool_on_the_mcp_tool_manager():
    registered = server.mcp._tool_manager.get_tool(TOOL_NAME)
    assert registered is not None
    assert callable(registered.fn)


def test_should_classify_the_tool_as_a_plan_scoped_remote_read():
    assert TOOL_NAME in REMOTE_READ
    assert TOOL_NAME in PLAN_SCOPED_READ
    assert remote_tool_allowed(TOOL_NAME) is True


def test_server_stays_under_the_line_cap():
    assert len(Path(server.__file__).read_text().splitlines()) < 1000


# ---------------------------------------------------------------------------
# The events themselves
# ---------------------------------------------------------------------------


def test_should_return_the_story_events_newest_first(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
        _notification("S-1", "review_requested", "S-1 under review", "t2"),
        _notification("S-1", "story_parked", "S-1 parked", "t3"),
    ]

    result = _tool()("alpha", "S-1")

    events = _events(result)
    assert [event["ts"] for event in events] == ["t3", "t2", "t1"]
    assert [event["event"] for event in events] == [
        "story_parked",
        "review_requested",
        "story_started",
    ]


def test_should_return_only_the_events_of_the_named_story(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
        _notification("S-2", "story_started", "S-2 started", "t2"),
        _notification("S-1", "story_parked", "S-1 parked", "t3"),
    ]

    result = _tool()("alpha", "S-1")

    events = _events(result)
    assert [event["ts"] for event in events] == ["t3", "t1"]
    assert all("S-2" not in str(event.get("message", "")) for event in events)


def test_should_give_every_event_the_documented_fields(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
    ]

    event = _events(_tool()("alpha", "S-1"))[0]

    assert EVENT_FIELDS <= set(event)
    assert isinstance(event["seq"], int)
    assert not isinstance(event["seq"], bool)
    assert event["ts"] == "t1"
    assert event["event"] == "story_started"
    assert event["message"] == "S-1 started"


def test_should_give_every_event_a_distinct_integer_seq(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "first", "t1"),
        _notification("S-1", "story_parked", "second", "t2"),
    ]

    events = _events(_tool()("alpha", "S-1"))

    seqs = [event["seq"] for event in events]
    assert len(set(seqs)) == len(seqs), seqs


def test_should_order_the_events_consistently_by_seq(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "first", "t1"),
        _notification("S-2", "story_started", "other story", "t2"),
        _notification("S-1", "story_parked", "second", "t3"),
    ]

    events = _events(_tool()("alpha", "S-1"))

    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs) or seqs == sorted(seqs, reverse=True), seqs
    assert len(set(seqs)) == len(seqs), seqs
    assert {event["ts"] for event in events} == {"t1", "t3"}


def test_should_redact_the_secret_values_in_a_message(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "using GITHUB_TOKEN=abc123 now", "t1"),
    ]

    event = _events(_tool()("alpha", "S-1"))[0]

    assert "abc123" not in event["message"]
    assert "[redacted]" in event["message"]
    assert "GITHUB_TOKEN" in event["message"]


def test_should_leave_a_message_without_secrets_unchanged(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "the sky is blue", "t1"),
    ]

    event = _events(_tool()("alpha", "S-1"))[0]

    assert event["message"] == "the sky is blue"


def test_should_carry_a_message_less_notification_without_a_message(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", None, "t1"),
    ]

    event = _events(_tool()("alpha", "S-1"))[0]

    assert event["message"] is None


# ---------------------------------------------------------------------------
# The limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        (1, 1),
        (2, 2),
        (50, 3),
        (0, 1),
        (-1, 1),
        (-1000, 1),
        (200, 3),
        (201, 3),
        (10**9, 3),
    ],
)
def test_should_clamp_the_limit_to_the_documented_range(plan, given, expected):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "one", "t1"),
        _notification("S-1", "review_requested", "two", "t2"),
        _notification("S-1", "story_parked", "three", "t3"),
    ]

    result = _tool()("alpha", "S-1", limit=given)

    assert len(_events(result)) == expected


def test_should_keep_the_newest_events_when_the_limit_bites(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "one", "t1"),
        _notification("S-1", "review_requested", "two", "t2"),
        _notification("S-1", "story_parked", "three", "t3"),
    ]

    events = _events(_tool()("alpha", "S-1", limit=1))

    assert [event["ts"] for event in events] == ["t3"]


def test_should_cap_a_long_history_at_the_maximum_limit(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", f"message {index}", f"t{index}")
        for index in range(250)
    ]

    events = _events(_tool()("alpha", "S-1", limit=10**9))

    assert len(events) == 200
    assert events[0]["ts"] == "t249"
    assert events[-1]["ts"] == "t50"


# ---------------------------------------------------------------------------
# The story's own fields
# ---------------------------------------------------------------------------


def test_should_return_the_story_review_feedback(plan):
    plan.manifests[(current_namespace(), "alpha")]["stories"]["S-1"][
        "review_feedback"
    ] = "the parser drops the last record"

    result = _tool()("alpha", "S-1")

    assert result["review_feedback"] == "the parser drops the last record"


def test_should_return_the_story_parked_reason(plan):
    plan.manifests[(current_namespace(), "alpha")]["stories"]["S-1"][
        "parked_reason"
    ] = "parked: needs human review"

    result = _tool()("alpha", "S-1")

    assert result["parked_reason"] == "parked: needs human review"


def test_should_return_the_story_failure_reason(plan):
    plan.manifests[(current_namespace(), "alpha")]["stories"]["S-1"][
        "failure_reason"
    ] = "tests failed"

    result = _tool()("alpha", "S-1")

    assert result["failure_reason"] == "tests failed"


def test_should_return_the_inconclusive_output_excerpt(plan):
    plan.manifests[(current_namespace(), "alpha")]["stories"]["S-1"][
        "last_inconclusive_output_excerpt"
    ] = "VERDICT: REQUEST_CHANGES - the guard is missing"

    result = _tool()("alpha", "S-1")

    assert (
        result["last_inconclusive_output_excerpt"]
        == "VERDICT: REQUEST_CHANGES - the guard is missing"
    )


def test_should_redact_the_inconclusive_output_excerpt(plan):
    plan.manifests[(current_namespace(), "alpha")]["stories"]["S-1"][
        "last_inconclusive_output_excerpt"
    ] = "reviewer said GITHUB_TOKEN=abc123 is wrong"

    result = _tool()("alpha", "S-1")

    excerpt = result["last_inconclusive_output_excerpt"]
    assert "abc123" not in excerpt
    assert "[redacted]" in excerpt


def test_should_bound_the_inconclusive_output_excerpt(plan):
    plan.manifests[(current_namespace(), "alpha")]["stories"]["S-1"][
        "last_inconclusive_output_excerpt"
    ] = "x" * 6000

    result = _tool()("alpha", "S-1")

    excerpt = result["last_inconclusive_output_excerpt"]
    assert len(excerpt) <= 4000
    assert len(excerpt) > 0


def test_should_omit_the_story_fields_a_story_does_not_carry(plan):
    result = _tool()("alpha", "S-1")

    for field in (
        "review_feedback",
        "parked_reason",
        "failure_reason",
        "last_inconclusive_output_excerpt",
    ):
        assert not result.get(field), (field, result)


# ---------------------------------------------------------------------------
# Boundaries and refusals
# ---------------------------------------------------------------------------


def test_should_answer_a_story_without_events_with_an_empty_list(plan):
    result = _tool()("alpha", "S-1")

    assert _events(result) == []
    assert result["available"] is True


def test_should_answer_an_unknown_plan_with_the_shared_404(plan):
    result = _tool()("ghost", "S-1")

    assert _not_found(result), result


def test_should_answer_an_unknown_story_with_the_shared_404(plan):
    result = _tool()("alpha", "S-9")

    assert _not_found(result), result


def test_should_answer_a_plan_without_a_manifest_with_the_shared_404(store):
    result = _tool()("ghost", "S-1")

    assert _not_found(result), result


def test_should_not_serve_events_for_an_unknown_story(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-9", "story_started", "S-9 started", "t1"),
    ]

    result = _tool()("alpha", "S-9")

    assert _not_found(result), result


# ---------------------------------------------------------------------------
# The store is the seam
# ---------------------------------------------------------------------------


def test_should_read_the_history_through_the_configured_store(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
    ]

    _tool()("alpha", "S-1")

    assert (current_namespace(), "alpha") in plan.reads


def test_should_read_the_namespace_bound_for_the_request(store):
    namespace = "a/b"
    store.manifests[(namespace, "alpha")] = {
        "plan": "alpha",
        "stories": {"S-1": {"status": "in_progress"}},
    }
    store.history[(namespace, "alpha")] = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
    ]

    with bind_namespace(namespace):
        result = _tool()("alpha", "S-1")

    assert [event["ts"] for event in _events(result)] == ["t1"]
    assert {read[0] for read in store.reads} == {namespace}


def test_should_not_see_another_namespace_history(store):
    store.manifests[(current_namespace(), "alpha")] = {
        "plan": "alpha",
        "stories": {"S-1": {"status": "in_progress"}},
    }
    store.history[("other", "alpha")] = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
    ]

    result = _tool()("alpha", "S-1")

    assert _events(result) == []


def test_should_not_write_to_the_store(plan):
    plan.history[(current_namespace(), "alpha")] = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
    ]
    before = copy.deepcopy(plan.history)

    _tool()("alpha", "S-1")

    assert plan.history == before
    assert plan.writes == []


# ---------------------------------------------------------------------------
# The unchanged path: no plugin configured
# ---------------------------------------------------------------------------


def test_should_read_the_plain_file_store_and_leave_it_byte_for_byte(
    tmp_path, monkeypatch
):
    """With no plugin configured the tool reads the ordinary file store.

    The tool is additive: the plan's files are exactly what they were before
    the call, byte for byte.
    """
    import json

    from pipeline.store import FileStore

    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(server, "_store", FileStore())
    manifest = {
        "plan": "alpha",
        "stories": {"S-1": {"status": "in_progress", "review_feedback": "fix it"}},
    }
    (plans / "alpha.manifest.json").write_text(json.dumps(manifest))
    records = [
        _notification("S-1", "story_started", "S-1 started", "t1"),
        _notification("S-1", "story_parked", "S-1 parked", "t2"),
    ]
    sidecar = plans / "alpha.notifications.jsonl"
    sidecar.write_text("".join(json.dumps(record) + "\n" for record in records))
    before = sidecar.read_bytes()

    result = _tool()("alpha", "S-1")

    events = _events(result)
    assert [event["ts"] for event in events] == ["t2", "t1"]
    assert result["review_feedback"] == "fix it"
    assert sidecar.read_bytes() == before



# ---------------------------------------------------------------------------
# The remote transport
# ---------------------------------------------------------------------------


class _Ctx:
    method = "tools/call"

    def __init__(self, params):
        self.params = params


def test_should_answer_a_hidden_plan_and_a_missing_plan_identically(
    tmp_path, monkeypatch
):
    import pipeline.persistence as ppers
    from app.identity import Principal
    from pipeline.actor import bind_principal

    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    (plans / "alpha.manifest.json").write_text(
        '{"plan": "alpha", "owner": "https://idp#alice"}'
    )
    stranger = Principal(subject="https://idp#eve", auth_method="oidc")

    async def call(plan_name):
        async def nxt(_ctx):
            return "REACHED"

        with bind_principal(stranger):
            return await remote_tool_policy(
                _Ctx(
                    {
                        "name": TOOL_NAME,
                        "arguments": {"plan_name": plan_name, "story_key": "S-1"},
                    }
                ),
                nxt,
            )

    hidden = asyncio.run(call("alpha"))
    missing = asyncio.run(call("ghost"))

    assert hidden != "REACHED"
    assert hidden["isError"] is True
    assert hidden == missing


def test_should_reach_the_handler_for_a_plan_the_caller_may_see(
    tmp_path, monkeypatch
):
    import pipeline.persistence as ppers
    from app.identity import Principal
    from pipeline.actor import bind_principal

    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", plans)
    monkeypatch.setattr(ppers, "PLAN_DIR", plans)
    (plans / "alpha.manifest.json").write_text(
        '{"plan": "alpha", "owner": "https://idp#alice"}'
    )
    owner = Principal(subject="https://idp#alice", auth_method="oidc")

    async def nxt(_ctx):
        return "REACHED"

    with bind_principal(owner):
        result = asyncio.run(
            remote_tool_policy(
                _Ctx(
                    {
                        "name": TOOL_NAME,
                        "arguments": {"plan_name": "alpha", "story_key": "S-1"},
                    }
                ),
                nxt,
            )
        )

    assert result == "REACHED"

