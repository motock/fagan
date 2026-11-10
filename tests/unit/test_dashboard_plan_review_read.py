"""The read-only plan-review endpoint: ``GET /api/plans/{plan_name}/review``.

Contract under test (PAN-07):

* the endpoint is mounted under ``/api``, so the dashboard's existing
  ``/api/`` middleware authenticates it: an unauthenticated caller is a 401
  before the handler ever runs, and the principal the middleware binds is the
  one the handler's visibility check sees;
* visibility is ``app.policy.can_view`` over
  ``pipeline.authorization.resource_for(manifest, plan_name, None)`` — the owner
  and a delegate see the payload, everybody else does not;
* an invisible plan, a nonexistent plan and an invalid plan key are the SAME
  404 with the SAME body (``{"detail": "plan not found"}``): the endpoint must
  not say whether a plan exists to a caller who may not see it;
* the two 409s are ordered AFTER the visibility check — "gate disabled" and
  "no approval record" are only ever reported for a plan the caller may see;
* the 200 payload is an explicit projection: the manifest is never passed
  through, and the ``approval`` block is reported as stored (the handler must
  not write a recomputed digest back onto it);
* ``digest_current`` compares the stored approval digest with the digest of
  the manifest as it is on disk *now*, recomputed on every call;
* a GET never mutates the manifest file.

``docs/specs/PLAN_APPROVAL.md`` must document the endpoint (doc pin).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from app.identity import IdentityError, Principal
from app.oidc import OidcProvider
from pipeline import plan_approval
from pipeline.plan_approval import canonical_digest

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"

#: The stub provider's token -> subject map. One token per principal under
#: test; any other token is refused, which the middleware turns into a 401.
_TOKENS = {"tok-alice": "alice", "tok-bob": "bob", "tok-carol": "carol"}

#: A story whose brief carries no ``Preflight:`` line while declaring a
#: non-Claude dispatch tier, so ``plan_review.review_warnings`` reports
#: ``preflight: missing`` for it. Deterministic: no environment dependency.
_WARNED_STORY = {
    "summary": "ship it",
    "risk": "low",
    "backend": "codex",
    "agent_instructions": "Deploy the thing.\n",
}

#: A story no review helper flags.
_CLEAN_STORY = {
    "summary": "Add a small CLI flag",
    "risk": "low",
    "backend": "claude",
    "agent_instructions": (
        "Add a --verbose flag to the CLI parser.\n"
        "Preflight: .venv/bin/python -m pytest -q tests/unit/test_cli.py\n"
    ),
}

#: The exact keys the 200 payload carries — no more, no less.
_PAYLOAD_KEYS = {
    "plan",
    "approval",
    "current_digest",
    "digest_current",
    "viewer_is_author",
    "owner",
    "role_config",
    "stories",
    "warnings",
}

#: The exact keys of the projected ``approval`` block.
_APPROVAL_KEYS = {"status", "digest", "decided_by", "at", "self_approved", "reason"}

#: The exact keys of one projected story.
_STORY_KEYS = {
    "key",
    "summary",
    "agent_instructions",
    "risk",
    "persona",
    "model",
    "backend",
    "files",
    "dependencies",
    "blocked_by",
    "preflight",
}

#: The one fixed 404 the endpoint may ever emit for an unreadable or
#: invisible plan.
_NOT_FOUND = {"detail": "plan not found"}


class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` that maps a few tokens to subjects, no network I/O.

    Subclasses the real provider because the middleware's bearer branch is
    gated on ``isinstance(_identity, OidcProvider)``. ``__init__`` deliberately
    does not call ``super().__init__`` (as ``test_dashboard_oidc.py``'s stub
    does): no real settings or network client is needed here.
    """

    def __init__(self) -> None:
        pass

    def authenticate(self, credential: str | None) -> Principal:
        subject = _TOKENS.get(credential or "")
        if subject is None:
            raise IdentityError("invalid token")
        return Principal(subject=subject, auth_method="oidc")


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Never read or create the repo's real .dashboard_api_key."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def oidc_identity(monkeypatch):
    """Activate the middleware's bearer branch with the stub provider."""
    provider = _StubOidcProvider()
    monkeypatch.setattr(d, "_identity", provider)
    return provider


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A private plan directory per test, so no manifest or store cache leaks."""
    from pipeline import server

    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def gate_on(monkeypatch):
    """Enable the plan-approval gate for the duration of one test."""
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "1")


@pytest.fixture
def client(key_file, oidc_identity):
    return TestClient(d.app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _url(plan_name: str) -> str:
    return f"/api/plans/{plan_name}/review"


def _base_manifest(stories: dict | None = None, **overrides) -> dict:
    """A manifest alice owns, bob is a delegate of, and carol cannot see."""
    manifest = {
        "repo_root": "/repo",
        "context": {},
        "role_config": {"reviewer": "alice"},
        "owner": "alice",
        "delegates": ["bob"],
        "ingested_by": "alice",
        "stories": dict(stories) if stories is not None else {"deploy": dict(_WARNED_STORY)},
    }
    manifest.update(overrides)
    return manifest


def _with_approval(manifest: dict, **overrides) -> dict:
    """``manifest`` plus an approval record whose digest is its current one."""
    approval = {
        "status": "approved",
        "digest": canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
        "decided_by": "alice",
        "at": "2026-11-01T09:00:00Z",
        "self_approved": True,
        "reason": "looks good",
    }
    approval.update(overrides)
    return {**manifest, "approval": approval}


def _write(plan_dir: Path, name: str, manifest: dict) -> Path:
    path = plan_dir / f"{name}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


# --------------------------------------------------------------------------- #
# Visibility: owner and delegate see it, everybody else is the same 404
# --------------------------------------------------------------------------- #

def test_owner_gets_the_review_payload(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url("alpha"), headers=_bearer("tok-alice"))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == _PAYLOAD_KEYS
    assert body["plan"] == "alpha"
    assert body["current_digest"] == canonical_digest(manifest)
    assert body["digest_current"] is True
    assert body["viewer_is_author"] is True
    assert body["owner"] == "alice"
    assert body["role_config"] == {"reviewer": "alice"}
    assert set(body["approval"]) == _APPROVAL_KEYS


def test_payload_reports_the_warnings_for_a_story_that_has_one(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert "preflight: missing" in body["warnings"]["deploy"]


def test_delegate_gets_the_review_payload(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url("alpha"), headers=_bearer("tok-bob"))

    assert response.status_code == 200
    assert response.json()["plan"] == "alpha"


def test_other_authenticated_principal_gets_the_fixed_404(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url("alpha"), headers=_bearer("tok-carol"))

    assert response.status_code == 404
    assert response.json() == _NOT_FOUND


def test_nonexistent_plan_is_the_byte_identical_404(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    invisible = client.get(_url("alpha"), headers=_bearer("tok-carol"))
    missing = client.get(_url("does-not-exist"), headers=_bearer("tok-carol"))
    missing_for_owner = client.get(_url("does-not-exist"), headers=_bearer("tok-alice"))

    assert invisible.status_code == missing.status_code == missing_for_owner.status_code == 404
    assert invisible.content == missing.content == missing_for_owner.content
    assert missing.json() == _NOT_FOUND


@pytest.mark.parametrize("plan_name", ["%2e%2e", "bad%20name", "%2e"])
def test_invalid_plan_key_is_the_fixed_404(client, plan_dir, gate_on, plan_name):
    """A dot-only key is a path component, not a plan name.

    Percent-encoded so the client's URL normalization cannot rewrite the path
    before the route sees it (a literal `.` segment is stripped in transit).
    """
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url(plan_name), headers=_bearer("tok-alice"))

    assert response.status_code == 404
    assert response.json() == _NOT_FOUND


def test_unauthenticated_request_is_refused_by_the_middleware(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url("alpha"))

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


# --------------------------------------------------------------------------- #
# The two 409s come after the visibility check
# --------------------------------------------------------------------------- #

def test_gate_disabled_is_409_but_only_for_a_visible_plan(client, plan_dir, monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "0")
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    ghost = client.get(_url("ghost"), headers=_bearer("tok-alice"))
    alpha = client.get(_url("alpha"), headers=_bearer("tok-alice"))
    invisible = client.get(_url("alpha"), headers=_bearer("tok-carol"))

    assert ghost.status_code == 404 and ghost.json() == _NOT_FOUND
    assert alpha.status_code == 409
    assert alpha.json() == {"detail": "plan approval is not enabled"}
    assert invisible.status_code == 404 and invisible.json() == _NOT_FOUND


@pytest.mark.parametrize("approval", [None, "approved", ["approved"]])
def test_no_approval_record_is_409(client, plan_dir, gate_on, approval):
    manifest = _base_manifest()
    if approval is not None:
        manifest["approval"] = approval
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url("alpha"), headers=_bearer("tok-alice"))

    assert response.status_code == 409
    assert response.json() == {"detail": "plan has no approval record; re-ingest it"}


# --------------------------------------------------------------------------- #
# Stateful: digest staleness and authorship, across more than one call
# --------------------------------------------------------------------------- #

def test_digest_state_across_two_calls(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    stored_digest = canonical_digest(manifest)
    assert manifest["approval"]["digest"] == stored_digest
    path = _write(plan_dir, "alpha", manifest)

    first = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert first["current_digest"] == stored_digest
    assert first["digest_current"] is True
    assert first["approval"]["digest"] == stored_digest

    # Edit one authored field on disk: the plan on disk no longer matches the
    # digest that was approved.
    edited = json.loads(path.read_bytes())
    edited["stories"]["deploy"]["summary"] = "ship it now"
    path.write_text(json.dumps(edited))
    after_edit = path.read_bytes()
    new_digest = canonical_digest(edited)
    assert new_digest != stored_digest

    second = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert second["current_digest"] == new_digest
    assert second["digest_current"] is False
    # The stored approval record is reported as stored -- never overwritten
    # with the recomputed digest.
    assert second["approval"]["digest"] == stored_digest
    assert second["approval"]["status"] == "approved"
    assert path.read_bytes() == after_edit


def test_viewer_is_author_compares_the_ingester_not_the_decider(client, plan_dir, gate_on):
    manifest = _with_approval(
        _base_manifest(), decided_by="bob", self_approved=False
    )
    manifest["ingested_by"] = "alice"
    _write(plan_dir, "alpha", manifest)

    as_alice = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()
    as_bob = client.get(_url("alpha"), headers=_bearer("tok-bob"))
    as_carol = client.get(_url("alpha"), headers=_bearer("tok-carol"))

    assert as_alice["viewer_is_author"] is True
    assert as_bob.status_code == 200
    assert as_bob.json()["viewer_is_author"] is False
    assert as_carol.status_code == 404
    assert "viewer_is_author" not in as_carol.json()


# --------------------------------------------------------------------------- #
# The projection is explicit and read-only
# --------------------------------------------------------------------------- #

def test_story_projection_carries_exactly_the_named_fields(client, plan_dir, gate_on):
    story = {
        **_WARNED_STORY,
        "persona": "engineer",
        "model": "gpt-x",
        "files": ["app/x.py"],
        "dependencies": ["S1"],
        "blocked_by": ["S0"],
        "preflight": ".venv/bin/python -m pytest -q tests/unit/test_x.py",
        "runtime_state": "must not leak",
    }
    manifest = _with_approval(_base_manifest(stories={"deploy": story}))
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    projected = body["stories"][0]
    assert set(projected) == _STORY_KEYS
    assert projected["key"] == "deploy"
    assert projected["summary"] == "ship it"
    assert projected["agent_instructions"] == "Deploy the thing.\n"
    assert projected["risk"] == "low"
    assert projected["persona"] == "engineer"
    assert projected["model"] == "gpt-x"
    assert projected["backend"] == "codex"
    assert projected["files"] == ["app/x.py"]
    assert projected["dependencies"] == ["S1"]
    assert projected["blocked_by"] == ["S0"]
    assert projected["preflight"] == ".venv/bin/python -m pytest -q tests/unit/test_x.py"


def test_get_does_not_modify_the_manifest_file(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest())
    path = _write(plan_dir, "alpha", manifest)
    before = path.read_bytes()

    response = client.get(_url("alpha"), headers=_bearer("tok-alice"))

    assert response.status_code == 200
    assert path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Doc pin
# --------------------------------------------------------------------------- #

def test_spec_documents_the_review_endpoint():
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "## 9. Dashboard surfacing" in text
    assert "/api/plans/{plan_name}/review" in text
