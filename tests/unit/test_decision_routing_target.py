"""RT-02: an escalated decision records WHO must answer it.

ADR 0006 routes a decision a story may not settle itself to the plan's owner
(and the delegates the owner named). ``request_decision`` already records every
ruling, but nothing on the record said who the answer now belongs to, so an
operator reading the decisions log could not tell an automated ruling from one
waiting on a named human.

Two fields carry that:

* ``routed_to`` -- ``{"owner": <manifest owner or "local-operator">,
  "delegates": [...]}`` -- present only where a human must answer: the fail-open
  record (the overlord errored, so a human is asked) and any parsed ruling whose
  ACTION is ``park_for_human`` or whose NOTIFY_USER is truthy. A ruling the
  overlord settles itself carries no ``routed_to`` at all.
* ``requested_by`` -- the acting principal's subject, read per call so it
  follows whatever principal the transport bound for this request.

The routing target is descriptive only: delivery is not this module's job.
"""
import json

import pytest

import pipeline.server  # noqa: F401 - imported before other pipeline imports on purpose: pre-existing import cycle (see test_triage_cap_silent_skip.py)
from app.identity import Principal
from pipeline import server as p
from pipeline import service_decisions
from pipeline.actor import bind_principal
from pipeline.authorization import ENFORCE_ENV
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

# Re-export the shared fixtures under their canonical names so pytest registers
# them for this module (assigning rather than importing keeps ruff's F811 quiet,
# as in the sibling decision tests).
plan_dir = _plan_dir_fixture
agents_dir = _agents_dir_fixture

PLAN = "payments-rollout"
STORY = "RT-1"
LOCAL = "local-operator"
OWNED_TARGET = {"owner": "alice", "delegates": ["bob", "carol"]}

#: ACTION park_for_human -- the overlord explicitly hands the choice to a human.
ESCALATING = (
    "RULING: Only a human can waive the contract.\n"
    "TIER: routine\n"
    "RISK: low\n"
    "RATIONALE: A signature is required.\n"
    "ACTION: park_for_human\n"
    "NOTIFY_USER: no\n"
)

#: ACTION settled by the overlord itself, nobody notified.
SELF_SETTLED = (
    "RULING: Reuse the existing client.\n"
    "TIER: routine\n"
    "RISK: low\n"
    "RATIONALE: httpx is already a dependency.\n"
    "ACTION: mark_done\n"
    "NOTIFY_USER: no\n"
)

#: ACTION settled by the overlord, but the user is asked to read the outcome.
SETTLED_BUT_NOTIFIED = (
    "RULING: Reuse the existing client, then tell the user.\n"
    "TIER: routine\n"
    "RISK: low\n"
    "RATIONALE: httpx is already a dependency.\n"
    "ACTION: mark_done\n"
    "NOTIFY_USER: yes\n"
)


def _seed(pdir, plan_extra=None):
    """Write a plan manifest, optionally with routing metadata at top level."""
    manifest = {
        "epics": {},
        "stories": {STORY: {"summary": "s", "status": "in_review"}},
    }
    if plan_extra:
        manifest.update(plan_extra)
    (pdir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest, indent=2))


def _decisions(pdir):
    path = pdir / f"{PLAN}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


@pytest.fixture(autouse=True)
def quiet_notify(monkeypatch):
    """Keep every test in this module off the real operator notification path.

    ``_notify_user`` reaches Plane, the desktop notifier and - on a machine with
    credentials - the network, and it records process-wide reachability state.
    These tests are about the decision RECORD, and an escalation record is
    written whether or not a notification is delivered, so nothing here needs a
    real delivery. Tests that assert on the message patch ``_notify_user``
    themselves; that patch is applied after this one and therefore wins.
    """
    sent: list = []
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: sent.append((a, k)))
    return sent


def _raiser(exc):
    def _boom(prompt, **kwargs):
        raise exc

    return _boom


def test_escalating_ruling_records_owner_and_delegates(plan_dir, agents_dir, monkeypatch):
    """A human-escalated ruling names the plan owner and the owner's delegates."""
    _seed(plan_dir, {"owner": "alice", "delegates": ["bob", "carol"]})
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: ESCALATING)

    with bind_principal(None):
        record = p.request_decision(PLAN, STORY, "q", ["a", "b"])

    assert record["routed_to"] == OWNED_TARGET
    assert _decisions(plan_dir)[0]["routed_to"] == OWNED_TARGET

    # The other half of the contract: a ruling the overlord settles but asks the
    # user to read (NOTIFY_USER: yes) still needs a named human to answer.
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: SETTLED_BUT_NOTIFIED)
    with bind_principal(None):
        notified = p.request_decision(PLAN, STORY, "q2", ["a", "b"])

    assert notified["action"] == "mark_done"
    assert notified["routed_to"] == OWNED_TARGET


def test_unowned_plan_routes_to_local_operator(plan_dir, agents_dir, monkeypatch):
    """A plan whose manifest names no owner routes to the local operator."""
    _seed(plan_dir)
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: ESCALATING)

    with bind_principal(None):
        record = p.request_decision(PLAN, STORY, "q", ["a", "b"])

    assert record["routed_to"] == {"owner": LOCAL, "delegates": []}


def test_fail_open_records_routing_target_and_notifies_owner(plan_dir, agents_dir, monkeypatch):
    """The fail-open record routes to the owner and the message names them."""
    monkeypatch.setenv(ENFORCE_ENV, "0")  # isolate routing from policy
    _seed(plan_dir, {"owner": "alice", "delegates": ["bob"]})
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError("backend down")))
    sent = []
    monkeypatch.setattr(
        p,
        "_notify_user",
        lambda plan_name, message, **kwargs: sent.append((plan_name, message, kwargs)),
    )

    with bind_principal(Principal(subject="user:alice", auth_method="oidc")):
        message = p.request_decision(PLAN, STORY, "q", ["a", "b"])

    record = _decisions(plan_dir)[0]
    assert record["failed_open"] is True
    assert record["routed_to"] == {"owner": "alice", "delegates": ["bob"]}
    assert record["requested_by"] == "user:alice"
    # The caller's escalation line is unchanged; the operator notification is
    # the surface that names who must now answer.
    assert message == "decision escalated to human: story parked, see decisions log"
    assert sent, "the fail-open park must reach the operator"
    text = sent[0][1]
    assert "(routed to alice)" in text
    # Secure-by-Design: a name, never the raw routing dict or exception text.
    assert "delegates" not in text


def test_self_settled_ruling_has_no_routed_to(plan_dir, agents_dir, monkeypatch):
    """A ruling the overlord settles itself carries no routing target."""
    _seed(plan_dir, {"owner": "alice", "delegates": ["bob"]})
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: SELF_SETTLED)

    with bind_principal(None):
        record = p.request_decision(PLAN, STORY, "q", ["a", "b"])

    assert record["action"] == "mark_done"
    assert record["notify_user"] is False
    assert "routed_to" not in record
    assert "routed_to" not in _decisions(plan_dir)[0]
    assert record["requested_by"] == LOCAL


def test_requested_by_follows_the_bound_principal(plan_dir, agents_dir, monkeypatch):
    """requested_by is read per call, not cached from an earlier request."""
    monkeypatch.setenv(ENFORCE_ENV, "0")  # isolate actor stamping from policy
    _seed(plan_dir, {"owner": "alice", "delegates": ["bob"]})
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: SELF_SETTLED)

    with bind_principal(None):
        unbound = p.request_decision(PLAN, STORY, "q1", ["a", "b"])
    with bind_principal(Principal(subject="user:alice", auth_method="oidc")):
        bound = p.request_decision(PLAN, STORY, "q2", ["a", "b"])
    with bind_principal(None):
        rebound = p.request_decision(PLAN, STORY, "q3", ["a", "b"])

    assert unbound["requested_by"] == LOCAL
    assert bound["requested_by"] == "user:alice"
    assert rebound["requested_by"] == LOCAL
    assert [r["requested_by"] for r in _decisions(plan_dir)] == [
        LOCAL,
        "user:alice",
        LOCAL,
    ]


def test_unreadable_manifest_falls_back_without_raising(plan_dir, agents_dir, monkeypatch):
    """An unreadable manifest yields the local-operator target, every call."""
    manifest_path = plan_dir / f"{PLAN}.manifest.json"
    manifest_path.write_text("{ this is not json")

    assert service_decisions._routing_target(PLAN) == {"owner": LOCAL, "delegates": []}

    # Not cached: once the manifest is readable again the owner is the answer.
    manifest_path.write_text(json.dumps({"owner": "alice", "delegates": ["bob", "carol"]}))
    assert service_decisions._routing_target(PLAN) == {
        "owner": "alice",
        "delegates": ["bob", "carol"],
    }

    # A loader that raises for a reason other than a decode error falls back too.
    def _boom(name):
        raise OSError("unreadable")

    with monkeypatch.context() as patched:
        patched.setattr(service_decisions, "_load_manifest", _boom)
        assert service_decisions._routing_target(PLAN) == {"owner": LOCAL, "delegates": []}

    # The fail-open path uses the same default, and says so in the message.
    # The manifest itself stays readable here: a corrupt manifest already fails
    # earlier, in the server's repo-root lookup, which this story does not own.
    manifest_path.write_text(json.dumps({"delegates": []}))
    monkeypatch.setattr(p, "_invoke_overlord", _raiser(RuntimeError("backend down")))
    sent = []
    monkeypatch.setattr(
        p,
        "_notify_user",
        lambda plan_name, message, **kwargs: sent.append((plan_name, message, kwargs)),
    )

    with monkeypatch.context() as patched, bind_principal(None):
        patched.setattr(service_decisions, "_load_manifest", _boom)
        message = p.request_decision(PLAN, STORY, "q", ["a", "b"])

    assert message == "decision escalated to human: story parked, see decisions log"
    assert "(routed to local-operator)" in sent[0][1]
    assert _decisions(plan_dir)[0]["routed_to"] == {"owner": LOCAL, "delegates": []}
