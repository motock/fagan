"""PAN-09: the dashboard's *deciding* plan-review routes.

``POST /api/plans/{plan_name}/review/approve`` and
``POST /api/plans/{plan_name}/review/request-changes`` let an operator decide a
plan from the dashboard, bound to the digest they were shown. The contract:

* a decision requires an **OIDC** principal (a login). A shared-key caller --
  including the local operator -- is refused with 403
  ``dashboard approval requires login`` and an audit record, because those
  callers keep using the MCP ``approve_plan`` tool;
* policy enforcement must be on: otherwise 403 ``policy enforcement is
  disabled``. The check order is principal, then enforcement, then plan name,
  then the decision, so a refusal never reports whether a plan exists;
* ``digest`` must be 64 lowercase hex characters and the service compares it
  with the plan's current canonical digest: a stale digest is a 409 and writes
  nothing, so nobody approves content they did not see. Retrying the same
  stale digest stays a 409; the digest the user was shown is never silently
  replaced with the current one;
* ``reason`` for request-changes is stripped first and then length-checked:
  ``"   "`` is empty, and 2000 characters of content survive any surrounding
  whitespace;
* the success body is exactly ``{status, digest, decided_by, at,
  self_approved}`` -- never the service result, the principal, a path or an
  exception text;
* the decision record, the audit record and the notification are written by
  ``pipeline.plan_approval``; the route duplicates none of them, and a skipped
  or refused decision writes no decision record;
* a GET never decides (405), and the routes inherit the dashboard's CSRF guard
  because the session cookie is an ambient credential.

``docs/specs/PLAN_APPROVAL.md`` must document both routes (doc pin).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pipeline.server  # noqa: F401  (import cycle: pipeline.server first)
from app import auth, oidc_login, policy
from app import dashboard as d
from app.identity import IdentityError, Principal
from app.oidc import OidcProvider
from pipeline import audit, authorization, persistence, plan_approval, server
from pipeline.plan_approval import canonical_digest

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"

REDIRECT_URI = "https://dashboard.example/auth/callback"
ISSUER = "https://issuer.example"

APPROVE = "/api/plans/{}/review/approve"
REQUEST_CHANGES = "/api/plans/{}/review/request-changes"

#: One token per principal under test. ``tok-shared`` is the shared-key caller:
#: the middleware's OIDC branch returns whatever the provider yields, so a
#: principal whose ``auth_method`` is not ``oidc`` reproduces exactly the
#: condition the route must refuse.
_TOKENS = {
    "tok-alice": Principal(subject="alice", auth_method="oidc"),
    "tok-bob": Principal(subject="bob", auth_method="oidc"),
    "tok-carol": Principal(subject="carol", auth_method="oidc"),
    "tok-erin": Principal(
        subject="erin", roles=frozenset({"approver"}), auth_method="oidc"
    ),
    "tok-hank": Principal(subject="hank", auth_method="oidc"),
    "tok-shared": Principal(
        subject=policy.LOCAL_OPERATOR_SUBJECT, auth_method="api_key"
    ),
}

#: The keys the 200 body carries -- no more, no less.
_SUCCESS_KEYS = {"status", "digest", "decided_by", "at", "self_approved"}


class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` that maps tokens to principals, with no network I/O.

    Subclasses the real provider because the middleware's bearer branch is
    gated on ``isinstance(_identity, OidcProvider)``. ``__init__`` deliberately
    does not call ``super().__init__``: no real settings or client are needed.
    """

    def __init__(self) -> None:
        pass

    def authenticate(self, credential: str | None) -> Principal:
        principal = _TOKENS.get(credential or "")
        if principal is None:
            raise IdentityError("invalid token")
        return principal


@pytest.fixture(autouse=True)
def _clean_oidc_state():
    """Sessions are module state; never let one test's session reach another."""
    oidc_login.reset_state()
    yield
    oidc_login.reset_state()


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Never read or create the repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def oidc_identity(monkeypatch):
    """Select the stub provider and a login configuration with an origin."""
    monkeypatch.setattr(d, "_identity", _StubOidcProvider())
    monkeypatch.setattr(
        d,
        "_oidc_login_config",
        oidc_login.OidcLoginConfig(
            enabled=True,
            client_id="pipeline-dashboard",
            redirect_uri=REDIRECT_URI,
            issuer=ISSUER,
        ),
    )


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A private plan directory per test: no manifest, decision or audit leaks."""
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")


@pytest.fixture
def enforcement_on(monkeypatch):
    monkeypatch.setenv(authorization.ENFORCE_ENV, "on")


@pytest.fixture
def client(key_file, oidc_identity, gate_on, enforcement_on):
    return TestClient(d.app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _base_manifest(**overrides) -> dict:
    """A manifest alice owns, bob is a delegate of, and carol/erin cannot see."""
    manifest = {
        "repo_root": "/repo",
        "context": {},
        "role_config": {"reviewer": "alice"},
        "owner": "alice",
        "delegates": ["bob"],
        "ingested_by": "alice",
        "stories": {
            "ship": {
                "summary": "ship it",
                "risk": "low",
                "backend": "claude",
                "agent_instructions": (
                    "Do the thing.\n"
                    "Preflight: .venv/bin/python -m pytest -q tests/unit/test_x.py\n"
                ),
            }
        },
    }
    manifest.update(overrides)
    return manifest


def _write_pending(plan_dir: Path, name: str, manifest: dict | None = None) -> str:
    """Write a ``pending_approval`` manifest for *name*; return its digest."""
    base = dict(_base_manifest() if manifest is None else manifest)
    base.pop("approval", None)
    digest = canonical_digest(base)
    base["approval"] = {
        "status": "pending_approval",
        "digest": digest,
        "warnings_seen": [],
    }
    (plan_dir / f"{name}.manifest.json").write_text(json.dumps(base))
    return digest


def _stored(plan_dir: Path, name: str) -> dict:
    """The manifest as it is on disk now."""
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


def _approval(plan_dir: Path, name: str) -> dict:
    return _stored(plan_dir, name).get("approval") or {}


def _decisions(plan_dir: Path, name: str) -> list:
    """The decision log for *name*, empty when nothing was appended."""
    path = plan_dir / f"{name}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _audit_records(name: str) -> list:
    """The plan's audit stream, oldest first."""
    path = audit.audit_path(name)
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


# --------------------------------------------------------------------------- #
# Who may decide: owner and delegate yes, everyone else no
# --------------------------------------------------------------------------- #

def test_owner_approves_the_current_digest(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-alice")
    )

    assert response.status_code == 200
    assert _approval(plan_dir, "alpha")["status"] == "approved"
    assert _approval(plan_dir, "alpha")["digest"] == digest
    assert len(_decisions(plan_dir, "alpha")) == 1
    assert _decisions(plan_dir, "alpha")[0]["decided_by"] == "alice"


def test_delegate_approves_and_is_not_flagged_as_the_author(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-bob")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decided_by"] == "bob"
    assert body["self_approved"] is False
    assert _approval(plan_dir, "alpha")["status"] == "approved"


def test_authenticated_stranger_is_refused_and_nothing_is_written(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-carol")
    )

    assert response.status_code == 403
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []


def test_approver_role_alone_is_not_enough(client, plan_dir):
    """The current policy grants ``plan:approve`` by ownership, not by role."""
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-erin")
    )

    assert response.status_code == 403
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []


def test_author_approving_its_own_plan_is_flagged(client, plan_dir):
    own = _base_manifest(owner="hank", delegates=[], ingested_by="hank")
    own_digest = _write_pending(plan_dir, "own", own)
    beta_digest = _write_pending(plan_dir, "beta", _base_manifest())

    mine = client.post(
        APPROVE.format("own"), json={"digest": own_digest}, headers=_bearer("tok-hank")
    )
    delegated = client.post(
        APPROVE.format("beta"), json={"digest": beta_digest}, headers=_bearer("tok-bob")
    )

    assert mine.status_code == 200
    assert mine.json()["self_approved"] is True
    assert delegated.status_code == 200
    assert delegated.json()["self_approved"] is False


def test_success_body_is_the_five_field_projection(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-alice")
    )

    assert response.status_code == 200
    assert set(response.json()) == _SUCCESS_KEYS
    assert response.json()["status"] == "approved"
    assert response.json()["digest"] == digest
    assert "path" not in response.text
    assert "principal" not in response.text


# --------------------------------------------------------------------------- #
# The digest: 64 lowercase hex characters, and it must still be current
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-digest"),
        pytest.param({"digest": "a" * 63}, id="63-chars"),
        pytest.param({"digest": "a" * 65}, id="65-chars"),
        pytest.param({"digest": "A" * 64}, id="uppercase"),
        pytest.param({"digest": "g" * 64}, id="non-hex"),
        # ``re.match(r"^[0-9a-f]{64}$", ...)`` would ACCEPT this: ``$`` matches
        # before one trailing newline. The model must not hand-roll the check.
        pytest.param({"digest": "a" * 64 + "\n"}, id="trailing-newline"),
    ],
)
def test_malformed_digest_is_422(client, plan_dir, body):
    _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"), json=body, headers=_bearer("tok-alice")
    )

    assert response.status_code == 422
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []


def test_unexpected_body_field_is_422(client, plan_dir):
    """``extra="forbid"``: a caller cannot smuggle an actor or a status in."""
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"),
        json={"digest": digest, "actor": "x"},
        headers=_bearer("tok-alice"),
    )

    assert response.status_code == 422
    assert _decisions(plan_dir, "alpha") == []


def test_stale_digest_is_refused_three_times_consistently(client, plan_dir):
    """A stale digest writes nothing -- and never becomes current by retrying."""
    d1 = _write_pending(plan_dir, "alpha", _base_manifest())
    d2 = "2" * 64
    stale_detail = "plan changed since it was displayed; reload before deciding"

    first = client.post(
        APPROVE.format("alpha"), json={"digest": d2}, headers=_bearer("tok-alice")
    )
    second = client.post(
        APPROVE.format("alpha"), json={"digest": d2}, headers=_bearer("tok-alice")
    )

    assert first.status_code == 409
    assert first.json()["detail"] == stale_detail
    assert second.status_code == 409
    assert _approval(plan_dir, "alpha")["digest"] == d1
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []

    third = client.post(
        APPROVE.format("alpha"), json={"digest": d1}, headers=_bearer("tok-alice")
    )

    assert third.status_code == 200
    assert _approval(plan_dir, "alpha")["status"] == "approved"
    assert len(_decisions(plan_dir, "alpha")) == 1


# --------------------------------------------------------------------------- #
# The service's skipped outcomes map to 409 without writing anything
# --------------------------------------------------------------------------- #

def test_locked_plan_is_409_and_a_retry_succeeds(client, plan_dir, monkeypatch):
    """A skipped decision writes no record, so the retry is not a phantom."""
    digest = _write_pending(plan_dir, "alpha", _base_manifest())
    real_approve = d._service.approve_plan
    calls = {"n": 0}

    def _approve(plan_name, *, expected_digest=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "skipped": "locked"}
        return real_approve(plan_name, expected_digest=expected_digest)

    monkeypatch.setattr(d._service, "approve_plan", _approve)

    busy = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-alice")
    )

    assert busy.status_code == 409
    assert busy.json()["detail"] == "plan is busy; retry"
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []

    retried = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-alice")
    )

    assert retried.status_code == 200
    assert _approval(plan_dir, "alpha")["status"] == "approved"
    assert len(_decisions(plan_dir, "alpha")) == 1


def test_approval_disabled_is_409(client, plan_dir, monkeypatch):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())
    monkeypatch.setattr(
        d._service,
        "approve_plan",
        lambda plan_name, *, expected_digest=None: {
            "ok": True,
            "skipped": "approval_disabled",
        },
    )

    response = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-alice")
    )

    assert response.status_code == 409
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []


def test_unknown_plan_is_404_plan_not_found(client, plan_dir):
    response = client.post(
        APPROVE.format("ghost"), json={"digest": "a" * 64}, headers=_bearer("tok-alice")
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "plan not found"
    assert _decisions(plan_dir, "ghost") == []


def test_invalid_plan_name_is_the_same_404(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    # ``a b`` is outside ``[A-Za-z0-9._-]``, so _validate_key rejects it. (A
    # ``..`` key cannot be tested here: the client collapses it out of the path
    # before routing, so it never reaches the handler.)
    response = client.post(
        APPROVE.format("a b"),
        json={"digest": digest},
        headers=_bearer("tok-alice"),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "plan not found"


# --------------------------------------------------------------------------- #
# Only an OIDC principal may decide, and enforcement must be on
# --------------------------------------------------------------------------- #

def test_shared_key_principal_is_refused_with_an_audit_record(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-shared")
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "dashboard approval requires login"
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []

    refusals = [r for r in _audit_records("alpha") if r.get("outcome") == "refused"]
    assert len(refusals) == 1
    refusal = refusals[0]
    assert refusal["target"] == {"plan": "alpha"}
    assert refusal["detail"] == {"reason": "unverified_principal"}
    # The action names the state the plan was refused in, never the requested
    # verb ("plan.approve"): the decision may not have been an approval at all.
    #
    # DRIFT FROM BRIEF, recorded deliberately: the brief says the literal
    # ``"plan.pending_review"``. No status by that name exists anywhere in this
    # repository -- ``pipeline.plan_approval`` and docs/specs/PLAN_APPROVAL.md
    # both call the pre-decision state ``pending_approval`` -- so this test pins
    # the repo's real value instead of importing a second, orphan status name.
    assert refusal["action"] == "plan.pending_approval"


def test_enforcement_disabled_is_403_before_the_plan_is_resolved(
    client, plan_dir, monkeypatch
):
    """Order is principal -> enforcement -> plan, so a refusal never says
    whether a plan exists."""
    monkeypatch.setenv(authorization.ENFORCE_ENV, "0")

    response = client.post(
        APPROVE.format("ghost"), json={"digest": "a" * 64}, headers=_bearer("tok-alice")
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "policy enforcement is disabled"

    refusals = [r for r in _audit_records("ghost") if r.get("outcome") == "refused"]
    assert len(refusals) == 1
    assert refusals[0]["detail"] == {"reason": "policy_enforcement_disabled"}


def test_unverified_principal_is_refused_before_enforcement(
    client, plan_dir, monkeypatch
):
    monkeypatch.setenv(authorization.ENFORCE_ENV, "0")

    response = client.post(
        APPROVE.format("alpha"),
        json={"digest": "a" * 64},
        headers=_bearer("tok-shared"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "dashboard approval requires login"


def test_unverified_principal_is_refused_before_the_plan_name_is_validated(
    client, plan_dir
):
    """An invalid key must not turn the principal refusal into a 404, and must
    never name an audit file."""
    response = client.post(
        APPROVE.format("a b"),
        json={"digest": "a" * 64},
        headers=_bearer("tok-shared"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "dashboard approval requires login"
    assert _audit_records("a b") == []


# --------------------------------------------------------------------------- #
# Requesting changes: the reason is stripped, then length-checked
# --------------------------------------------------------------------------- #

def test_request_changes_with_blank_reason_is_422(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    for reason in ("", "   "):
        response = client.post(
            REQUEST_CHANGES.format("alpha"),
            json={"digest": digest, "reason": reason},
            headers=_bearer("tok-alice"),
        )
        assert response.status_code == 422, reason

    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []


def test_request_changes_with_over_long_reason_is_422(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        REQUEST_CHANGES.format("alpha"),
        json={"digest": digest, "reason": "a" * 2001},
        headers=_bearer("tok-alice"),
    )

    assert response.status_code == 422
    assert _decisions(plan_dir, "alpha") == []


def test_request_changes_strips_the_reason_before_the_length_check(client, plan_dir):
    """2002 raw characters, 2000 of content: the padded form must be accepted
    and the stored reason must be the stripped one."""
    digest = _write_pending(plan_dir, "alpha", _base_manifest())
    padded = " " + "a" * 2000 + " "

    response = client.post(
        REQUEST_CHANGES.format("alpha"),
        json={"digest": digest, "reason": padded},
        headers=_bearer("tok-alice"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "changes_requested"
    assert set(response.json()) == _SUCCESS_KEYS
    assert _approval(plan_dir, "alpha")["status"] == "changes_requested"
    assert _decisions(plan_dir, "alpha")[0]["reason"] == "a" * 2000


def test_request_changes_with_a_valid_reason_is_200(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        REQUEST_CHANGES.format("alpha"),
        json={"digest": digest, "reason": "  split story S2 out  "},
        headers=_bearer("tok-alice"),
    )

    assert response.status_code == 200
    assert _approval(plan_dir, "alpha")["status"] == "changes_requested"
    assert _decisions(plan_dir, "alpha")[0]["reason"] == "split story S2 out"


def test_request_changes_requires_a_digest(client, plan_dir):
    _write_pending(plan_dir, "alpha", _base_manifest())

    response = client.post(
        REQUEST_CHANGES.format("alpha"),
        json={"reason": "nope"},
        headers=_bearer("tok-alice"),
    )

    assert response.status_code == 422
    assert _decisions(plan_dir, "alpha") == []


# --------------------------------------------------------------------------- #
# A GET never decides, and the CSRF guard covers the POSTs
# --------------------------------------------------------------------------- #

def test_get_does_not_decide_and_the_following_post_does(client, plan_dir):
    digest = _write_pending(plan_dir, "alpha", _base_manifest())

    read = client.get(APPROVE.format("alpha"), headers=_bearer("tok-alice"))

    assert read.status_code == 405
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []

    decided = client.post(
        APPROVE.format("alpha"), json={"digest": digest}, headers=_bearer("tok-alice")
    )

    assert decided.status_code == 200
    assert _approval(plan_dir, "alpha")["status"] == "approved"


def test_cross_site_cookie_post_is_refused_before_the_handler(client, plan_dir):
    """The session cookie is ambient: a cross-site POST must never decide."""
    digest = _write_pending(plan_dir, "alpha", _base_manifest())
    session_id = oidc_login._create_session(
        Principal(subject="alice", auth_method="oidc")
    )
    cookie = {"Cookie": f"{oidc_login.SESSION_COOKIE}={session_id}"}

    forged = client.post(
        APPROVE.format("alpha"),
        json={"digest": digest},
        headers={**cookie, "Sec-Fetch-Site": "cross-site"},
    )

    assert forged.status_code == 403
    assert _approval(plan_dir, "alpha")["status"] == "pending_approval"
    assert _decisions(plan_dir, "alpha") == []

    same_origin = client.post(
        APPROVE.format("alpha"),
        json={"digest": digest},
        headers={**cookie, "Origin": "https://dashboard.example"},
    )

    assert same_origin.status_code == 200
    assert _approval(plan_dir, "alpha")["status"] == "approved"


# --------------------------------------------------------------------------- #
# The spec documents both routes (doc pin)
# --------------------------------------------------------------------------- #

def test_spec_documents_the_deciding_routes():
    text = DOC_PATH.read_text()

    assert "/review/approve" in text
    assert "/review/request-changes" in text
