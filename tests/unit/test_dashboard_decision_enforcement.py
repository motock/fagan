"""Tests for AUTH-09: the dashboard decision route enforces ``decision:answer``.

``decision:answer`` is the last unenforced action, and a decision is answered
in exactly one place -- ``POST /api/plans/{plan}/decisions``. The middleware
already binds the authenticated principal for the request (TEN-WIRE-01) and
``pipeline.actor`` exposes it to the service layer (ACT-04); these tests pin
that the route now *asks* the policy about that principal and acts on the
answer.

Everything runs through a real ``TestClient`` so the middleware wiring is
exercised rather than a direct function call:

* the default shared-secret principal is ``local-operator``, whom the local
  policy allows -- a decision POST must keep recording ``decided_by`` exactly
  as before (the no-regression case);
* a principal the policy denies is refused with ``403`` -- never a ``500`` --
  and nothing is written to ``<plan>.decisions.json``;
* the refusal body names the action and the plan and never the principal's
  subject or credential;
* ``PIPELINE_POLICY_ENFORCE=0`` turns enforcement off, so the same denied
  principal's POST succeeds;
* a refusal appends exactly one ``authorization_denied`` record to the plan's
  notification sidecar, via ``pipeline.authorization``.

The denied identity is injected as the dashboard's identity provider: the API
key gate still authenticates the shared secret (so the 401 path is untouched),
it is only *who* the gate says the caller is that changes.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from app.identity import Principal
from pipeline.authorization import ENFORCE_ENV

_HEADER = "X-Pipeline-Api-Key"
_PLAN = "demo"
_DENIED_SUBJECT = "svc-dashboard@example.invalid"
_DENIAL_EVENT = "authorization_denied"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so these tests never read or
    create this repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


class _FixedPrincipalProvider:
    """An identity provider that authenticates every accepted credential as
    one fixed principal.

    The shared-secret gate has already accepted the credential, so this stands
    in for a deployment whose verified caller is *not* the local operator.
    """

    def __init__(self, principal):
        self._principal = principal

    def authenticate(self, credential):
        return self._principal


@pytest.fixture
def denied_principal(monkeypatch):
    """Make every authenticated dashboard request run as a denied principal."""
    provider = _FixedPrincipalProvider(
        Principal(subject=_DENIED_SUBJECT, auth_method="shared-secret")
    )
    monkeypatch.setattr(d, "_identity", provider)
    return provider


@pytest.fixture
def manifest(plan_dir):
    """Write the plan manifest the route's existence check and the policy both
    read, and return the plan directory."""
    (plan_dir / f"{_PLAN}.manifest.json").write_text(
        json.dumps({"repo_root": "/tmp/repo", "stories": {}})
    )
    return plan_dir


def _client():
    return TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})


def _answer_decision(client):
    return client.post(
        f"/api/plans/{_PLAN}/decisions",
        json={
            "story_key": "AUTH-09",
            "question": "Enforce decision:answer?",
            "answer": "yes",
            "context": "policy enforcement",
            "decided_by": "human",
        },
    )


def _decision_records(plan_dir):
    path = plan_dir / f"{_PLAN}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(errors="replace"))


def _denial_records(plan_dir):
    path = plan_dir / f"{_PLAN}.notifications.jsonl"
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    return [r for r in records if r.get("event") == _DENIAL_EVENT]


# --------------------------------------------------------------------------- #
# No regression: the default shared-secret principal still answers decisions
# --------------------------------------------------------------------------- #

def test_local_operator_post_still_records_the_decision(key_file, manifest):
    _answer_decision(_client())

    assert [r["decided_by"] for r in _decision_records(manifest)] == ["human"]


# --------------------------------------------------------------------------- #
# A denied principal is refused and nothing is persisted
# --------------------------------------------------------------------------- #

def test_denied_principal_post_is_403_and_writes_no_decision(
    key_file, manifest, denied_principal
):
    response = _answer_decision(_client())

    assert (response.status_code, _decision_records(manifest)) == (403, [])


def test_refusal_body_names_the_action_and_plan_without_the_principal(
    key_file, manifest, denied_principal
):
    response = _answer_decision(_client())

    assert (
        "decision:answer" in response.text,
        _PLAN in response.text,
        _DENIED_SUBJECT in response.text,
    ) == (True, True, False)


# --------------------------------------------------------------------------- #
# Enforcement can be turned off explicitly
# --------------------------------------------------------------------------- #

def test_disabled_enforcement_allows_the_denied_principal(
    key_file, manifest, denied_principal, monkeypatch
):
    monkeypatch.setenv(ENFORCE_ENV, "0")

    response = _answer_decision(_client())

    assert (response.status_code, len(_decision_records(manifest))) == (200, 1)


# --------------------------------------------------------------------------- #
# The refusal is visible to the operator
# --------------------------------------------------------------------------- #

def test_refusal_appends_one_denial_record_for_the_action(
    key_file, manifest, denied_principal
):
    _answer_decision(_client())

    assert [r["message"] for r in _denial_records(manifest)] == [
        f"authorization denied: decision:answer by {_DENIED_SUBJECT}"
    ]
