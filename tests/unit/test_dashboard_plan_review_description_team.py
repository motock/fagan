"""The review payload shows the reviewer the content they are approving.

USER DECISION 2026-11-07: once a story's ``description`` and the plan's
``team`` are part of the approved content, the reviewer must SEE them.  So
``GET /api/plans/{plan_name}/review`` carries, per story, its ``description``
(the ``null`` for a story that has none), and, at the top level, the plan's
``team``.

The payload change is **purely additive**: no key is removed, renamed or
retyped.  That is pinned here structurally (the key set of the payload and of
one projected story is the documented set plus exactly the new names), so a
later story that adds another field does not have to touch this file.

``docs/specs/PLAN_APPROVAL.md`` §9 ("The review endpoint") must name both
fields.
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

#: The token -> subject map of the stub provider.
_TOKENS = {"tok-alice": "alice", "tok-bob": "bob", "tok-carol": "carol"}

#: The payload keys the endpoint carries.  The documented set plus the two
#: names this story adds; a superset is allowed (a later story may add
#: fields), a *missing* name is not.
_DOCUMENTED_PAYLOAD_KEYS = {
    "plan",
    "approval",
    "current_digest",
    "digest_current",
    "viewer_is_author",
    "owner",
    "role_config",
    "stories",
    "warnings",
    "team",
}

#: The projected story keys: the documented set plus ``description``.
_DOCUMENTED_STORY_KEYS = {
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
    "description",
}


class _StubOidcProvider(OidcProvider):
    """An ``OidcProvider`` that maps a few tokens to subjects, no network I/O."""

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
    provider = _StubOidcProvider()
    monkeypatch.setattr(d, "_identity", provider)
    return provider


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    from pipeline import server

    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "1")


@pytest.fixture
def client(key_file, oidc_identity):
    return TestClient(d.app)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _url(plan_name: str) -> str:
    return f"/api/plans/{plan_name}/review"


def _story(**overrides) -> dict:
    story = {
        "summary": "ship it",
        "risk": "low",
        "backend": "claude",
        "agent_instructions": (
            "Add a --verbose flag to the CLI parser.\n"
            "Preflight: .venv/bin/python -m pytest -q tests/unit/test_cli.py\n"
        ),
    }
    story.update(overrides)
    return story


def _base_manifest(stories: dict | None = None, **overrides) -> dict:
    manifest = {
        "repo_root": "/repo",
        "context": {},
        "role_config": {"reviewer": "alice"},
        "owner": "alice",
        "delegates": ["bob"],
        "ingested_by": "alice",
        "team": "core",
        "stories": dict(stories) if stories is not None else {"deploy": _story()},
    }
    manifest.update(overrides)
    return manifest


def _with_approval(manifest: dict, **overrides) -> dict:
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
# The payload carries the description and the team
# --------------------------------------------------------------------------- #

def test_the_payload_carries_the_story_description(client, plan_dir, gate_on):
    manifest = _with_approval(
        _base_manifest(stories={"deploy": _story(description="Do the thing.")})
    )
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert body["stories"][0]["description"] == "Do the thing."


def test_the_payload_carries_the_plan_team(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest(team="payments"))
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert body["team"] == "payments"


def test_a_story_without_a_description_reports_null(client, plan_dir, gate_on):
    manifest = _with_approval(_base_manifest(stories={"deploy": _story()}))
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert body["stories"][0]["description"] is None


def test_a_plan_without_a_team_reports_null(client, plan_dir, gate_on):
    manifest = _base_manifest()
    manifest.pop("team")
    manifest = _with_approval(manifest)
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert body["team"] is None


def test_a_script_tag_in_a_description_is_returned_verbatim(client, plan_dir, gate_on):
    """The description is data, not markup: it is returned as it was written."""
    payload = "<script>alert('xss')</script>"
    manifest = _with_approval(
        _base_manifest(stories={"deploy": _story(description=payload)})
    )
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url("alpha"), headers=_bearer("tok-alice"))

    assert response.status_code == 200
    body = response.json()
    assert body["stories"][0]["description"] == payload
    # The raw payload is JSON-encoded, so the angle brackets are escaped by
    # the JSON encoder and the response is not the raw script text.
    assert "<script>" not in response.text


# --------------------------------------------------------------------------- #
# The change is additive: no key is removed, renamed or retyped
# --------------------------------------------------------------------------- #

def test_the_payload_key_set_is_the_documented_one_plus_the_new_names(
    client, plan_dir, gate_on
):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert set(body) == _DOCUMENTED_PAYLOAD_KEYS


def test_the_story_key_set_is_the_documented_one_plus_the_new_name(
    client, plan_dir, gate_on
):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert set(body["stories"][0]) == _DOCUMENTED_STORY_KEYS


def test_the_approval_block_and_the_other_payload_keys_keep_their_names(
    client, plan_dir, gate_on
):
    manifest = _with_approval(_base_manifest())
    _write(plan_dir, "alpha", manifest)

    body = client.get(_url("alpha"), headers=_bearer("tok-alice")).json()

    assert set(body["approval"]) == {
        "status",
        "digest",
        "decided_by",
        "at",
        "self_approved",
        "reason",
    }
    assert body["plan"] == "alpha"
    assert body["owner"] == "alice"
    assert body["role_config"] == {"reviewer": "alice"}
    assert isinstance(body["warnings"], dict)


# --------------------------------------------------------------------------- #
# Visibility is unchanged: an unauthorized viewer still gets the opaque 404
# --------------------------------------------------------------------------- #

def test_an_unauthorized_viewer_gets_the_opaque_404_and_no_description(
    client, plan_dir, gate_on
):
    manifest = _with_approval(
        _base_manifest(stories={"deploy": _story(description="Secret plan.")})
    )
    _write(plan_dir, "alpha", manifest)

    response = client.get(_url("alpha"), headers=_bearer("tok-carol"))

    assert response.status_code == 404
    assert response.json() == {"detail": "plan not found"}
    assert "Secret plan." not in response.text
    assert "description" not in response.text


# --------------------------------------------------------------------------- #
# Doc pin: §9 names both fields
# --------------------------------------------------------------------------- #

def test_the_review_endpoint_section_names_description_and_team():
    text = DOC_PATH.read_text(encoding="utf-8")
    section = text.split("### The review endpoint", 1)
    assert len(section) == 2, "the '### The review endpoint' section is gone"
    body = section[1]
    assert "description" in body
    assert "team" in body
