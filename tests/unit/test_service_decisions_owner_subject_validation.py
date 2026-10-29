"""FU-04: an unvalidated plan owner must not reach the notifications log.

``_routing_target`` used to interpolate ``manifest["owner"]`` into the fail-open
notification, which ``persistence`` appends to ``<plan>.notifications.log`` -
one physical line per record, read back through ``get_notifications``. A ``\\n``
in the owner therefore forged a line in that append-only evidence log, CR/ANSI
spoofed it, and a multi-megabyte value buried neighbouring records.

The bar the repo already sets for a weaker input (``pipeline/actor.py``'s
CORRELATION_ID_RE) is applied here at a larger bound: a subject must be a single
line of ``[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}``. Anything else - and every
non-``str`` - falls back to ``LOCAL_OPERATOR.subject``, the deny-to-safe-default
``_routing_target`` already uses for an unreadable manifest. The notification is
still sent: control flow is unchanged.
"""

import json
import re

import pytest

import pipeline.server  # noqa: F401 - imported before other pipeline imports on purpose: pre-existing import cycle (see test_triage_cap_silent_skip.py)
from app.identity import LOCAL_OPERATOR
from pipeline import server as p
from pipeline.actor import bind_principal
from pipeline.authorization import ENFORCE_ENV
from pipeline.service_decisions import _routing_target
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _isolate_usage_state,
    _plane_configured,
    _read_manifest,
    _write_manifest,
)
from tests.unit._pipeline_mcp_server_test_helpers import (
    agents_dir as _agents_dir_fixture,
)
from tests.unit._pipeline_mcp_server_test_helpers import (
    plan_dir as _plan_dir_fixture,
)

plan_dir = _plan_dir_fixture
agents_dir = _agents_dir_fixture

PLAN = "payments-rollout"
STORY = "FU4-1"
LOCAL = LOCAL_OPERATOR.subject

#: Hardcoded on purpose: importing a not-yet-existing constant would fail on
#: ImportError instead of the assertion this suite is about.
MAX_DELEGATES = 64
SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}")

HOSTILE_OWNERS = ["a\nb", "\x1b[2J", "x" * 10000, {"x": 1}, 123, "alice\n"]
VALID_OWNERS = ["alice", "team@example.com", "ops:oncall"]

TRAILER = "see decisions log (routed to "


def _seed(pdir, **extra):
    manifest = {
        "epics": {},
        "stories": {STORY: {"summary": "s", "status": "in_review"}},
    }
    manifest.update(extra)
    (pdir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _raiser(exc):
    def _boom(prompt, **kwargs):
        raise exc

    return _boom


def _fail_open(monkeypatch):
    """Drive the real fail-open path, which is what renders the notification."""
    monkeypatch.setenv(ENFORCE_ENV, "0")  # isolate routing from policy
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError("backend down")))
    with bind_principal(None):
        return p.request_decision(PLAN, STORY, "q", ["a", "b"])


def _records():
    """Read the notification records back through the real read path."""
    return p._store.get_notifications(PLAN)


def _log_lines(pdir):
    path = pdir / f"{PLAN}.notifications.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _expected_message(routed_to):
    return f"{TRAILER}{routed_to})"


def _assert_single_line(pdir, routed_to):
    records = _records()
    lines = _log_lines(pdir)
    assert len(records) == 1, f"expected exactly one notification, got {records!r}"
    assert len(lines) == 1, f"expected one physical log line, got {lines!r}"
    message = records[0]
    assert message.endswith(_expected_message(routed_to)), message
    assert "\n" not in message
    assert "\r" not in message
    assert "\x1b" not in message
    # The physical line carries the same single-line message.
    assert lines[0].endswith(message)


@pytest.mark.parametrize("owner", HOSTILE_OWNERS)
def test_hostile_owner_routes_to_local_operator_and_renders_one_line(
    plan_dir, agents_dir, monkeypatch, owner
):
    """A hostile owner never reaches the evidence log; the operator is told."""
    _seed(plan_dir, owner=owner)

    # Legacy manifests are already on disk: the routing fallback alone must hold.
    assert _routing_target(PLAN)["owner"] == LOCAL

    _fail_open(monkeypatch)

    _assert_single_line(plan_dir, LOCAL)


def test_no_owner_plan_renders_the_same_single_line(plan_dir, agents_dir, monkeypatch):
    """Control flow unchanged: an ownerless plan still notifies the operator."""
    _seed(plan_dir)

    _fail_open(monkeypatch)

    _assert_single_line(plan_dir, LOCAL)


@pytest.mark.parametrize("owner", VALID_OWNERS)
def test_valid_owner_is_unchanged(plan_dir, agents_dir, monkeypatch, owner):
    """A valid subject keeps routing to itself and renders one line."""
    _seed(plan_dir, owner=owner)

    assert _routing_target(PLAN)["owner"] == owner

    _fail_open(monkeypatch)

    _assert_single_line(plan_dir, owner)


def test_owner_already_local_operator_is_unchanged(plan_dir, agents_dir, monkeypatch):
    """An owner equal to the local operator is not doubled or emptied."""
    _seed(plan_dir, owner=LOCAL)

    assert _routing_target(PLAN)["owner"] == LOCAL

    _fail_open(monkeypatch)

    _assert_single_line(plan_dir, LOCAL)


def test_hostile_then_valid_owner_appends_one_line_each(
    plan_dir, agents_dir, monkeypatch
):
    """Worked example A: the hostile record cannot forge or swallow a line."""
    _seed(plan_dir, owner="a\nb")
    assert _log_lines(plan_dir) == []

    _fail_open(monkeypatch)
    first = _records()
    assert len(first) == 1 and first[0].endswith(_expected_message(LOCAL))

    _seed(plan_dir, owner="alice")
    _fail_open(monkeypatch)
    records = _records()
    lines = _log_lines(plan_dir)
    assert len(records) == 2, records
    assert len(lines) == 2, lines
    assert records[0].endswith(_expected_message(LOCAL))
    assert records[1].endswith(_expected_message("alice"))


def test_delegates_are_bounded_and_capped(plan_dir, agents_dir):
    """Every delegate is a valid subject and the list is capped."""
    _seed(
        plan_dir,
        owner="alice",
        delegates=["bob", "a\nb", "x" * 10000] + [f"d{i}" for i in range(100)],
    )

    delegates = _routing_target(PLAN)["delegates"]

    assert len(delegates) == MAX_DELEGATES
    assert "a\nb" not in delegates
    assert "x" * 10000 not in delegates
    for delegate in delegates:
        assert isinstance(delegate, str)
        assert SUBJECT_RE.fullmatch(delegate), delegate

    # A second call must not see a shared, accumulating module-level list.
    _seed(plan_dir, owner="alice", delegates=["bob"])
    assert _routing_target(PLAN)["delegates"] == ["bob"]


def test_non_list_delegates_yield_empty_list(plan_dir, agents_dir):
    """The existing empty/default shape is preserved for a malformed value."""
    _seed(plan_dir, owner="alice", delegates="bob")

    assert _routing_target(PLAN) == {"owner": "alice", "delegates": []}
