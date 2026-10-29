"""SYS-01: the ``system:scheduler`` and ``system:worker`` principals and grants.

The scheduler daemon and the workers it dispatches act on their own behalf, not
on a human's: ``app/identity.py`` defines one principal for each, and
``app.policy.LocalPolicy`` holds an explicit grant table for them instead of the
local operator's blanket allow. These tests pin the table down:

* each grant the scheduler tick actually reaches is allowed, and nothing else
  is -- in particular not ``plan:approve``/``plan:reject`` (ADR 0009 forbids a
  scheduler approving its own work), not ``plan:pause`` (only the pause/resume
  tools ask for it) and not ``story:patch`` (a worker-side action);
* the worker may patch a story and dispatch/merge nothing;
* ownership never widens a ``system:`` subject: a manifest naming one as
  ``owner`` or ``delegate`` still denies it ``plan:approve``/``plan:reject``,
  while a human owner keeps the authority she has today;
* no OIDC token can mint a ``system:`` subject: ``app/oidc.py`` refuses a
  ``sub`` that claims the reserved prefix, so the reservations are reachable
  only through the module's own constants.

Assertions are membership/absence only, never the whole table or a count: the
table grows as later work adds system actions.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import identity, oidc
from app.identity import Principal
from app.policy import Action, LocalPolicy, Resource

ISSUER = "https://idp.example"
AUDIENCE = "pipeline"
JWKS_URL = "https://idp.example/.well-known/jwks.json"
KEY_ID = "sys01-key"

#: Exactly the actions the scheduler tick reaches (see the commit body for the
#: call site behind each one).
SCHEDULER_GRANTED = (
    "plan:dispatch",
    "story:merge",
    "story:interrupt",
)

#: Actions a system principal must never hold.
SYSTEM_DENIED = (
    "plan:approve",
    "plan:reject",
    "plan:ingest",
    "config:write",
    "decision:answer",
)

#: The scheduler's reserved rights: neither is reachable from the tick.
SCHEDULER_ONLY_DENIED = ("plan:pause", "story:patch")
WORKER_DENIED = (
    "plan:dispatch",
    "story:merge",
    "plan:approve",
    "plan:reject",
    "plan:ingest",
    "config:write",
    "decision:answer",
)


def _manifest(**overrides) -> Resource:
    """Return the ADR 0009 resource for a plan named ``demo``."""
    fields = {"plan": "demo", "story": "SYS-01"}
    fields.update(overrides)
    return Resource(**fields)


def _allowed(principal: Principal, action: Action | str, resource=None) -> bool:
    return LocalPolicy().allow(principal, action, resource)


def _token_for(subject: str, key) -> str:
    """Return a signed RS256 token whose ``sub`` is ``subject``."""
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "iat": now,
            "exp": now + 300,
        },
        key,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def oidc_settings(signing_key, monkeypatch):
    """Serve a local JWKS, so verification never touches the network."""
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update({"kid": KEY_ID, "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(oidc, "_jwks", lambda *args, **kwargs: {"keys": [jwk]})
    return oidc.OidcSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL)


def test_system_principals_defined():
    assert hasattr(identity, "SYSTEM_SCHEDULER"), "SYSTEM_SCHEDULER missing"
    scheduler = identity.SYSTEM_SCHEDULER
    assert scheduler.subject == "system:scheduler"
    assert scheduler.auth_method == "system"

    assert hasattr(identity, "SYSTEM_WORKER"), "SYSTEM_WORKER missing"
    worker = identity.SYSTEM_WORKER
    assert worker.subject == "system:worker"
    assert worker.auth_method == "system"


@pytest.mark.parametrize("action", SCHEDULER_GRANTED)
def test_scheduler_granted_actions(action):
    assert _allowed(identity.SYSTEM_SCHEDULER, action, _manifest()) is True


@pytest.mark.parametrize("action", SCHEDULER_GRANTED)
def test_scheduler_granted_actions_hold_for_action_members(action):
    """The table must match the enum, not only the raw string a call site passes."""
    assert _allowed(identity.SYSTEM_SCHEDULER, Action(action), _manifest()) is True


@pytest.mark.parametrize("action", SYSTEM_DENIED + SCHEDULER_ONLY_DENIED)
def test_scheduler_denied_actions(action):
    assert _allowed(identity.SYSTEM_SCHEDULER, action, _manifest()) is False


def test_scheduler_denied_story_mark_done():
    """The tick's merge path writes ``status="done"`` itself.

    ``story:mark_done`` is only ever asked for by the human ``mark_story_done``
    tool, so the scheduler holds no grant for it.
    """
    assert _allowed(identity.SYSTEM_SCHEDULER, "story:mark_done", _manifest()) is False


@pytest.mark.parametrize("action", ("plan:approve", "plan:reject"))
def test_scheduler_denied_when_manifest_names_it_owner(action):
    manifest = _manifest(owner="system:scheduler")
    assert _allowed(identity.SYSTEM_SCHEDULER, action, manifest) is False


@pytest.mark.parametrize("action", ("plan:approve", "plan:reject"))
def test_scheduler_denied_when_manifest_names_it_delegate(action):
    manifest = _manifest(owner="alice", delegates=("system:scheduler",))
    assert _allowed(identity.SYSTEM_SCHEDULER, action, manifest) is False


def test_human_owner_still_approves_own_plan():
    """Ownership is not disabled globally: only the reserved subjects lose it."""
    manifest = _manifest(owner="alice")
    alice = Principal(subject="alice", auth_method="oidc")
    assert _allowed(alice, "plan:approve", manifest) is True
    assert _allowed(alice, "plan:reject", manifest) is True


def test_worker_granted_action():
    assert _allowed(identity.SYSTEM_WORKER, "story:patch", _manifest()) is True


@pytest.mark.parametrize("action", WORKER_DENIED)
def test_worker_denied_actions(action):
    assert _allowed(identity.SYSTEM_WORKER, action, _manifest()) is False


def test_reserved_subject_is_confined_whatever_its_auth_method():
    """Keying on the subject prefix keeps a reserved identity inside its grants."""
    impostor = Principal(subject="system:worker", auth_method="shared-secret")
    assert _allowed(impostor, "story:patch", _manifest()) is True
    assert _allowed(impostor, "plan:dispatch", _manifest()) is False
    assert _allowed(impostor, "plan:approve", _manifest(owner="system:worker")) is False


def test_decisions_do_not_mutate_the_grant_table():
    """A denial must never add the action to the shared, module-level table."""
    from app.policy import SYSTEM_PRINCIPAL_GRANTS

    _allowed(identity.SYSTEM_SCHEDULER, "plan:approve", _manifest())
    _allowed(identity.SYSTEM_WORKER, "story:merge", _manifest())
    assert "plan:approve" not in SYSTEM_PRINCIPAL_GRANTS["system:scheduler"]
    assert "plan:dispatch" in SYSTEM_PRINCIPAL_GRANTS["system:scheduler"]
    assert "story:merge" not in SYSTEM_PRINCIPAL_GRANTS["system:worker"]
    assert "story:patch" in SYSTEM_PRINCIPAL_GRANTS["system:worker"]


def test_oidc_sub_system_scheduler_is_rejected(oidc_settings, signing_key):
    """A token cannot claim a reserved subject, so OIDC yields no system principal."""
    token = _token_for("system:scheduler", signing_key)
    with pytest.raises(ValueError):
        oidc.verify_token(token, oidc_settings)


def test_oidc_normal_subject_is_issuer_qualified(oidc_settings, signing_key):
    """Normal login is unaffected: the subject is still qualified by its issuer."""
    token = _token_for("alice", signing_key)
    principal = oidc.verify_token(token, oidc_settings)
    assert principal.subject == f"{ISSUER}#alice"
    assert principal.auth_method == "oidc"
