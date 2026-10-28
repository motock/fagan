"""RT-01: a plan's owner and delegates may answer its decisions.

ADR 0006 escalates a decision to the plan's owner, and ADR 0009 defines
``delegate`` as "may answer decisions", but ``LocalPolicy`` granted a plan's
``owner``/``delegates`` only ``plan:approve``, ``plan:reject`` and
``story:merge``. The dashboard is the one place a decision is answered
(``POST /api/plans/{plan}/decisions``) and it asks for ``decision:answer`` --
so until the ownership tuple names that action, the people the ADRs say may
answer a decision are refused.

The contract pinned here:

* an owner may answer a decision on its own plan, and a delegate may answer on
  the plan that delegates them;
* an unrelated principal may not, and an owner may not answer on a plan it does
  not own;
* the grant stays additive: the local operator still answers everywhere,
  including on a plan someone else owns, and a plan with no owner simply falls
  through to the role rule;
* ownership still grants nothing beyond the four actions it governs -- it is
  not a general "may do anything on my plan";
* end to end through the dashboard, an owner's answer is recorded, a stranger's
  is refused with no decision written, and that refusal leaves no state behind
  for the next caller.

``pipeline.server`` is imported explicitly because the import graph only
resolves cold in that order (see ``tests/unit/test_triage_cap_silent_skip.py``).
The route tests drive a real ``TestClient`` and swap the dashboard's identity
provider, exactly as ``tests/unit/test_dashboard_decision_enforcement.py``
does, so the request-scoped principal reaches the policy the way it does in a
deployment.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import pipeline.server  # noqa: F401  (import order; see tests/unit/test_triage_cap_silent_skip.py)
from app import auth
from app import dashboard as d
from app.identity import LOCAL_OPERATOR, Principal
from app.policy import LocalPolicy, Resource

#: The deployment's policy, the same one ``default_authorizer`` returns.
POLICY = LocalPolicy()

#: Actions a plan's ownership governs.
OWNED_ACTIONS = ("plan:approve", "plan:reject", "story:merge", "decision:answer")

#: Actions ownership must never reach.
NOT_OWNED_ACTIONS = ("story:patch", "plan:dispatch", "plan:ingest", "config:write")

#: Plans.
PLAN_A = Resource(owner="alice", delegates=("bob",))
PLAN_B = Resource(owner="erin")
PLAN_NO_OWNER = Resource()

#: Principals. None of them holds a role: ownership is the only thing that can
#: allow them, which is exactly what makes these assertions meaningful.
ALICE = Principal(subject="alice", roles=())
BOB = Principal(subject="bob", roles=())
CAROL = Principal(subject="carol", roles=())
DANA = LOCAL_OPERATOR


def _allow(principal: Principal | None, action: str, resource: Resource) -> bool:
    """Ask the policy directly. ``action`` stays a raw string on purpose: the
    route asks with the string ``"decision:answer"``, so the tests do too."""
    return POLICY.allow(principal, action, resource)


# --------------------------------------------------------------------------- #
# Ownership governs decision:answer
# --------------------------------------------------------------------------- #

def test_owner_may_answer_a_decision_on_its_own_plan() -> None:
    assert _allow(ALICE, "decision:answer", PLAN_A) is True


def test_delegate_may_answer_a_decision_on_the_plan_that_delegates_them() -> None:
    assert _allow(BOB, "decision:answer", PLAN_A) is True


def test_third_principal_may_not_answer_a_decision() -> None:
    assert _allow(CAROL, "decision:answer", PLAN_A) is False


def test_owner_may_not_answer_a_decision_on_a_plan_it_does_not_own() -> None:
    assert _allow(ALICE, "decision:answer", PLAN_B) is False


@pytest.mark.parametrize("action", OWNED_ACTIONS)
def test_owner_may_take_every_action_ownership_governs(action: str) -> None:
    assert _allow(ALICE, action, PLAN_A) is True


# --------------------------------------------------------------------------- #
# A plan with no owner falls through to the role rule
# --------------------------------------------------------------------------- #

def test_plan_with_no_owner_denies_a_non_operator() -> None:
    assert _allow(CAROL, "decision:answer", PLAN_NO_OWNER) is False


def test_plan_with_no_owner_still_allows_the_local_operator() -> None:
    assert _allow(DANA, "decision:answer", PLAN_NO_OWNER) is True


# --------------------------------------------------------------------------- #
# The grant is additive only: the operator answers on someone else's plan
# --------------------------------------------------------------------------- #

def test_local_operator_may_answer_on_a_plan_owned_by_someone_else() -> None:
    assert _allow(DANA, "decision:answer", PLAN_A) is True


# --------------------------------------------------------------------------- #
# Ownership grants nothing beyond the actions it governs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("action", NOT_OWNED_ACTIONS)
@pytest.mark.parametrize("principal", [ALICE, BOB], ids=["owner", "delegate"])
def test_ownership_does_not_extend_to_other_actions(
    principal: Principal, action: str
) -> None:
    assert _allow(principal, action, PLAN_A) is False


# --------------------------------------------------------------------------- #
# State must not leak across calls
# --------------------------------------------------------------------------- #

def test_decisions_across_several_calls_do_not_leak_state() -> None:
    """Seven calls in one test: a memoised, cached or plan-mutating policy gets
    at least one of these wrong, in both directions."""
    assert _allow(CAROL, "decision:answer", PLAN_A) is False
    assert _allow(ALICE, "decision:answer", PLAN_A) is True
    assert _allow(ALICE, "decision:answer", PLAN_B) is False
    assert _allow(BOB, "decision:answer", PLAN_A) is True
    assert _allow(DANA, "decision:answer", PLAN_NO_OWNER) is True
    assert _allow(CAROL, "decision:answer", PLAN_NO_OWNER) is False
    assert _allow(DANA, "decision:answer", PLAN_A) is True


# --------------------------------------------------------------------------- #
# Through the dashboard route
# --------------------------------------------------------------------------- #

_HEADER = "X-Pipeline-Api-Key"
#: Two plans alice owns on disk; the route reads owner/delegates from here.
_PLAN_A = "plan-a"
_PLAN_B = "plan-b"


class _FixedPrincipalProvider:
    """An identity provider that authenticates every accepted credential as one
    fixed principal.

    The shared-secret gate has already accepted the credential; this stands in
    for a deployment whose verified caller is a named human rather than the
    local operator.
    """

    def __init__(self, principal):
        self._principal = principal

    def authenticate(self, credential):
        return self._principal


def _bind(monkeypatch, principal):
    """Make every authenticated dashboard request run as *principal*."""
    monkeypatch.setattr(d, "_identity", _FixedPrincipalProvider(principal))


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file, never this repo's real one."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def plans(plan_dir):
    """Write the two plans the route's existence check and the policy read."""
    for name, manifest in (
        (
            _PLAN_A,
            {
                "repo_root": "/tmp/repo",
                "stories": {},
                "owner": "alice",
                "delegates": ["bob"],
            },
        ),
        (
            _PLAN_B,
            {"repo_root": "/tmp/repo", "stories": {}, "owner": "alice"},
        ),
    ):
        (plan_dir / f"{name}.manifest.json").write_text(json.dumps(manifest))
    return plan_dir


def _client():
    return TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})


def _answer(client, plan: str = _PLAN_A):
    return client.post(
        f"/api/plans/{plan}/decisions",
        json={
            "story_key": "RT-01",
            "question": "May an owner answer?",
            "answer": "yes",
            "context": "ownership grants decision:answer",
            "decided_by": "human",
        },
    )


def _decision_records(plan_dir, plan: str = _PLAN_A):
    path = plan_dir / f"{plan}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(errors="replace"))


def _total_decisions(plan_dir) -> int:
    return sum(len(_decision_records(plan_dir, plan)) for plan in (_PLAN_A, _PLAN_B))


def test_owner_answer_is_accepted_and_recorded_once(key_file, plans, monkeypatch):
    _bind(monkeypatch, ALICE)

    response = _answer(_client())

    assert (
        response.status_code,
        [record["actor"] for record in _decision_records(plans)],
    ) == (200, ["alice"])


def test_stranger_answer_is_refused_and_records_nothing(
    key_file, plans, monkeypatch
):
    _bind(monkeypatch, CAROL)

    response = _answer(_client())

    assert (response.status_code, _decision_records(plans)) == (403, [])


def test_refused_answer_leaves_no_state_behind_for_the_next_caller(
    key_file, plans, monkeypatch
):
    client = _client()
    assert _total_decisions(plans) == 0

    _bind(monkeypatch, ALICE)
    assert _answer(client).status_code == 200
    assert (
        [record["actor"] for record in _decision_records(plans)],
        _total_decisions(plans),
    ) == (["alice"], 1)

    _bind(monkeypatch, CAROL)
    assert _answer(client).status_code == 403
    assert _total_decisions(plans) == 1

    _bind(monkeypatch, ALICE)
    assert _answer(client, _PLAN_B).status_code == 200
    assert _total_decisions(plans) == 2
