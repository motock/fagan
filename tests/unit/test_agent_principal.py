"""Agent principals bound to a team scope (ADR 0014 decision 3).

The three facts that make a caller an *agent* — that it is an agent, which
client id it authenticated as, and which ``<org>/<team>`` scope it is bound to
— all come from the signed token or from operator configuration. None of them
may ever be read from a request field.

This module covers the ``Principal`` validation rules and the OIDC agent-token
path. Authorization, actor helpers and audit attribution live further down.
"""

import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import oidc
from app.identity import (
    LOCAL_OPERATOR,
    SYSTEM_SCHEDULER,
    SYSTEM_WORKER,
    IdentityError,
    Principal,
)
from app.policy import Action
from pipeline import actor, audit, authorization, persistence
from pipeline.actor import bind_principal

# --------------------------------------------------------------------------- #
# Principal: principal_type / client_id / team_scope validation
# --------------------------------------------------------------------------- #


def test_existing_two_argument_construction_defaults_to_human():
    principal = Principal("alice", frozenset({"author"}))

    assert principal.principal_type == "human"
    assert principal.client_id == ""
    assert principal.team_scope == ""


def test_unknown_principal_type_is_refused():
    with pytest.raises(IdentityError) as excinfo:
        Principal("alice", principal_type="robot")

    assert str(excinfo.value) == "invalid principal type"


@pytest.mark.parametrize("principal_type", ["human", "agent", "system"])
def test_known_principal_types_are_accepted(principal_type):
    team_scope = "acme/team-1" if principal_type == "agent" else ""

    principal = Principal("alice", principal_type=principal_type, team_scope=team_scope)

    assert principal.principal_type == principal_type


def test_agent_without_team_scope_is_refused():
    with pytest.raises(IdentityError):
        Principal("agent:x", principal_type="agent", team_scope="")


@pytest.mark.parametrize(
    "team_scope",
    [
        "platform",  # no org/team separator
        "acme/Team-1",  # uppercase is not in the char class
        "acme/_team",  # every segment must start [a-z0-9]
        "acme/team-1\n",  # trailing newline must not slip past the match
        "acme/team 1",  # space
        "/team-1",  # empty org segment
        "acme/",  # empty team segment
    ],
)
def test_agent_with_malformed_team_scope_is_refused(team_scope):
    with pytest.raises(IdentityError):
        Principal("agent:x", principal_type="agent", team_scope=team_scope)


@pytest.mark.parametrize("team_scope", ["acme/team-1", "acme/team_1"])
def test_agent_keeps_valid_team_scope_verbatim(team_scope):
    principal = Principal(
        "agent:x",
        principal_type="agent",
        client_id="svc-agent-1",
        team_scope=team_scope,
    )

    assert principal.principal_type == "agent"
    assert principal.client_id == "svc-agent-1"
    assert principal.team_scope == team_scope


def test_human_with_team_scope_is_refused():
    with pytest.raises(IdentityError):
        Principal("alice", principal_type="human", team_scope="acme/team-1")


def test_system_principal_is_a_system_type():
    assert SYSTEM_SCHEDULER.principal_type == "system"
    assert SYSTEM_WORKER.principal_type == "system"


# --------------------------------------------------------------------------- #
# OIDC: an allowlisted client id becomes an agent bound to its team scope
# --------------------------------------------------------------------------- #

ISSUER = "https://issuer.example"
AUDIENCE = "pipeline"
JWKS_URL = "https://issuer.example/jwks"
AGENT_CLIENTS_ENV = "PIPELINE_OIDC_AGENT_CLIENTS"
AGENT_SCOPE_CLAIM_ENV = "PIPELINE_OIDC_AGENT_SCOPE_CLAIM"

_REMOVE = object()


@pytest.fixture(scope="module")
def rsa_key():
    """One 2048-bit RSA key pair for the whole module; no network is used."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def settings(monkeypatch, rsa_key):
    """``OidcSettings`` plus a stubbed key lookup (the JWKS fetch boundary)."""
    monkeypatch.setattr(oidc, "_signing_key", lambda *args, **kwargs: rsa_key.public_key())
    return oidc.OidcSettings(issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL)


def mint(rsa_key, **overrides) -> str:
    """Mint a valid token for ``ISSUER``/``AUDIENCE`` with claim overrides."""
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice",
        "exp": now + 300,
        "iat": now,
    }
    for name, value in overrides.items():
        if value is _REMOVE:
            claims.pop(name, None)
        else:
            claims[name] = value
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "k1"})


def test_allowlisted_client_with_team_scope_is_an_agent(monkeypatch, settings, rsa_key):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "svc-agent-1")
    token = mint(rsa_key, azp="svc-agent-1", team_scope="acme/team-1")

    principal = oidc.verify_token(token, settings)

    assert principal.principal_type == "agent"
    assert principal.subject == "agent:acme/team-1"
    assert principal.client_id == "svc-agent-1"
    assert principal.team_scope == "acme/team-1"
    assert principal.roles == frozenset()
    assert principal.auth_method == "oidc"


def test_allowlisted_client_is_read_from_env_on_every_call(monkeypatch, settings, rsa_key):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "svc-agent-1, svc-agent-2,,")
    token = mint(rsa_key, azp="svc-agent-2", team_scope="acme/team-1")

    assert oidc.verify_token(token, settings).principal_type == "agent"
    # Reading the env at import time would freeze the allowlist here.
    assert oidc.verify_token(token, settings).principal_type == "agent"

    monkeypatch.setenv(AGENT_CLIENTS_ENV, "")

    assert oidc.verify_token(token, settings).principal_type == "human"


def test_client_outside_the_allowlist_is_human_and_scope_is_ignored(
    monkeypatch, settings, rsa_key
):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "svc-agent-1")
    token = mint(rsa_key, azp="web-ui", team_scope="acme/team-1")

    principal = oidc.verify_token(token, settings)

    assert principal.principal_type == "human"
    assert principal.client_id == "web-ui"
    assert principal.team_scope == ""
    assert principal.subject == f"{ISSUER}#alice"


def test_empty_agent_allowlist_means_no_agents(monkeypatch, settings, rsa_key):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "")
    token = mint(rsa_key, azp="svc-agent-1", team_scope="acme/team-1")

    principal = oidc.verify_token(token, settings)

    assert principal.principal_type == "human"
    assert principal.team_scope == ""


def test_human_without_azp_has_an_empty_client_id(monkeypatch, settings, rsa_key):
    monkeypatch.delenv(AGENT_CLIENTS_ENV, raising=False)

    principal = oidc.verify_token(mint(rsa_key), settings)

    assert principal.principal_type == "human"
    assert principal.client_id == ""


@pytest.mark.parametrize(
    "scope",
    [_REMOVE, 5, None, "platform", "acme/Team-1", "acme/team-1\n", ""],
    ids=["missing", "int", "null", "no-pair", "uppercase", "newline", "empty"],
)
def test_allowlisted_client_with_bad_scope_is_refused(
    monkeypatch, settings, rsa_key, scope
):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "svc-agent-1")
    token = mint(rsa_key, azp="svc-agent-1", team_scope=scope)

    with pytest.raises(IdentityError) as excinfo:
        oidc.verify_token(token, settings)

    assert str(excinfo.value) == "invalid token: agent scope"


def test_agent_subject_prefix_is_reserved(monkeypatch, settings, rsa_key):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "svc-agent-1")
    token = mint(
        rsa_key,
        sub="agent:acme/team-1",
        azp="svc-agent-1",
        team_scope="acme/team-1",
    )

    with pytest.raises(IdentityError):
        oidc.verify_token(token, settings)


def test_scope_claim_name_is_configurable(monkeypatch, settings, rsa_key):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "svc-agent-1")
    monkeypatch.setenv(AGENT_SCOPE_CLAIM_ENV, "tenant")
    token = mint(rsa_key, azp="svc-agent-1", tenant="acme/team-2")

    principal = oidc.verify_token(token, settings)

    assert principal.principal_type == "agent"
    assert principal.team_scope == "acme/team-2"
    assert principal.subject == "agent:acme/team-2"


def test_rejections_never_log_the_scope_or_the_token(
    monkeypatch, settings, rsa_key, caplog
):
    monkeypatch.setenv(AGENT_CLIENTS_ENV, "svc-agent-1")
    secret_scope = "Acme/Secret-Team"
    token = mint(rsa_key, azp="svc-agent-1", team_scope=secret_scope)

    with (
        caplog.at_level(logging.DEBUG, logger="pipeline.identity"),
        pytest.raises(IdentityError) as excinfo,
    ):
        oidc.verify_token(token, settings)

    rendered = [str(excinfo.value)] + [record.getMessage() for record in caplog.records]
    for text in rendered:
        assert secret_scope not in text
        assert token not in text
        assert "svc-agent-1" not in text


# --------------------------------------------------------------------------- #
# Authorization: an agent never approves a plan or writes configuration
# --------------------------------------------------------------------------- #

AGENT = Principal(
    "agent:acme/team-1",
    roles=frozenset(),
    auth_method="oidc",
    principal_type="agent",
    client_id="svc-agent-1",
    team_scope="acme/team-1",
)


class _AllowEverything:
    """An operator authorizer that says yes to every request it is asked."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def allow(self, principal, action, resource=None) -> bool:
        self.calls.append(action)
        return True


@pytest.fixture
def authorizer(monkeypatch):
    """Install an allow-everything authorizer and keep enforcement on."""
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    stub = _AllowEverything()
    monkeypatch.setattr(authorization, "default_authorizer", lambda: stub)
    return stub


@pytest.fixture
def denials(monkeypatch):
    """Record every ``_record_denial`` call instead of writing a sidecar."""
    calls: list[dict] = []
    monkeypatch.setattr(
        authorization,
        "_record_denial",
        lambda action, subject, plan_name, story_key, reason="": calls.append(
            {
                "action": action,
                "subject": subject,
                "plan": plan_name,
                "story": story_key,
                "reason": reason,
            }
        ),
    )
    monkeypatch.setattr(authorization, "record_audit", lambda *args, **kwargs: None)
    return calls


@pytest.mark.parametrize(
    "action", [Action.PLAN_APPROVE, Action.CONFIG_WRITE], ids=["approve", "config"]
)
def test_agent_is_denied_high_risk_actions(authorizer, denials, action):
    with bind_principal(AGENT):
        refusal = authorization.authorize(action, plan_name="p", manifest={})

    assert refusal is not None
    assert refusal["ok"] is False
    assert refusal["forbidden"] is True
    # The guard runs before the authorizer: it is never consulted.
    assert authorizer.calls == []
    assert len(denials) == 1
    assert denials[0]["action"] == action.value
    assert denials[0]["reason"] == "agent_principal"


def test_agent_may_reject_a_plan(authorizer, denials):
    with bind_principal(AGENT):
        refusal = authorization.authorize(
            Action.PLAN_REJECT, plan_name="p", manifest={}
        )

    assert refusal is None
    assert authorizer.calls == [Action.PLAN_REJECT]
    assert denials == []


def test_agent_may_perform_an_action_the_policy_allows(authorizer, denials):
    with bind_principal(AGENT):
        refusal = authorization.authorize(
            Action.PLAN_DISPATCH, plan_name="p", manifest={}
        )

    assert refusal is None
    assert authorizer.calls == [Action.PLAN_DISPATCH]
    assert denials == []


def test_human_operator_may_approve(authorizer, denials):
    with bind_principal(LOCAL_OPERATOR):
        refusal = authorization.authorize(
            Action.PLAN_APPROVE, plan_name="p", manifest={}
        )

    assert refusal is None
    assert authorizer.calls == [Action.PLAN_APPROVE]
    assert denials == []


# --------------------------------------------------------------------------- #
# Actor helpers and audit attribution
# --------------------------------------------------------------------------- #


def test_actor_helpers_read_the_bound_principal():
    with bind_principal(AGENT):
        assert actor.actor_id() == "agent:acme/team-1"
        assert actor.principal_type() == "agent"
        assert actor.client_id() == "svc-agent-1"


def test_actor_helpers_default_to_the_local_operator():
    assert actor.principal_type() == "human"
    assert actor.client_id() == ""


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    """Point the plan audit chain at a throwaway directory."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def test_new_audit_record_carries_principal_type_and_client_id(audit_dir):
    with bind_principal(AGENT):
        record = audit.append_audit("p", action="plan:dispatch", outcome="allowed")

    assert record["principal_type"] == "agent"
    assert record["client_id"] == "svc-agent-1"


def test_chain_mixing_old_and_new_records_verifies_intact(audit_dir):
    # An "old" record: written before the two attribution fields existed, so it
    # has neither key and its hash covers only what it does have.
    old = {
        "seq": 0,
        "prev_hash": "",
        "ts": "2024-01-01T00:00:00+00:00",
        "actor": "local-operator",
        "correlation_id": "",
        "action": "plan:ingest",
        "outcome": "allowed",
        "target": {},
        "detail": {},
    }
    old["hash"] = audit._record_hash(old)
    path = audit_dir / "p.audit.jsonl"
    path.write_text(audit._canonical(old) + "\n")

    second = audit.append_audit("p", action="plan:dispatch", outcome="allowed")
    third = audit.append_audit("p", action="story:patch", outcome="allowed")

    assert second["seq"] == 1
    assert third["seq"] == 2
    assert second["principal_type"] == "human"
    assert second["client_id"] == ""
    assert audit.verify_audit_chain("p") is None


def test_new_records_do_not_change_what_the_hash_covers(audit_dir):
    record = audit.append_audit("p", action="plan:dispatch", outcome="allowed")

    without_hash = {key: value for key, value in record.items() if key != "hash"}

    assert audit._record_hash(without_hash) == record["hash"]
