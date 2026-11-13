"""The approval digest binds the *authored* ``backend``, not the runtime one (dig-1).

``story["backend"]`` is both authored (a plan may pin it) and runtime-written
(the dispatcher, escalation and the review orchestrator stamp the resolved
provider on the story). Hashing it directly made every first dispatch reset,
or appear to reset, an approval nobody edited.

The fix records the author's value as ``authored_backend`` at ingest and hashes
that. Graded here:

* a manifest without ``authored_backend`` (every story ingested before the
  change) keeps its exact digest, so ``APPROVAL_DIGEST_VERSION`` stays 2;
* a runtime stamp on ``backend`` never moves the digest;
* a change to the authored value always does, including a malformed value,
  which is hashed as given and never coerced;
* export -> ingest reproduces the digest after dispatch has stamped ``backend``,
  and re-ingest after dispatch refreshes ``authored_backend``.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

# pipeline.server must load before pipeline.ingest (import-cycle note, see
# tests/unit/test_ingest_keyless_reingest.py).
import pipeline.server as p
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline import plan_approval as pa
from pipeline.plan_approval import canonical_digest, reset_if_changed
from pipeline.service import PipelineService

PLAN = "ab"

#: A non-Claude dispatch provider is gated on a real ``Preflight:`` line.
_PREFLIGHT = "Preflight: test fixture, not a real plan"

#: The authored story fields the digest hashed before ``authored_backend``
#: existed. Spelled out here, not imported, so the legacy payload shape is
#: checked against this list rather than against the implementation.
_LEGACY_STORY_FIELDS = (
    "summary",
    "agent_instructions",
    "dependencies",
    "persona",
    "model",
    "acceptance",
    "risk",
    "backend",
    "tdd_split",
    "files",
    "blocked_by",
    "description",
)


def _expected_legacy_digest(manifest: dict) -> str:
    """The digest of a legacy manifest, built from the documented payload shape.

    Computed rather than pinned as a hex literal: the repo's frozen-pin guard
    (tests/unit/test_pin_guard_baseline.py) refuses new literals, and a
    computed value still fails loudly if the legacy payload shape changes.
    """
    payload = {
        field: manifest.get(field) for field in ("repo_root", "role_config", "context", "team")
    }
    payload["stories"] = {
        key: {field: story.get(field) for field in _LEGACY_STORY_FIELDS}
        for key, story in manifest["stories"].items()
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_manifest() -> dict:
    """A manifest ingested before ``authored_backend`` was recorded."""
    story = {
        "summary": "ship",
        "agent_instructions": "do it",
        "dependencies": [],
        "persona": "software-engineer",
        "model": "sonnet",
        "acceptance": [],
        "risk": "low",
        "backend": "claude",
        "tdd_split": False,
        "files": ["a.py"],
        "blocked_by": [],
        "description": "d",
    }
    return {
        "repo_root": "/repo",
        "role_config": {},
        "context": {},
        "team": "core",
        "stories": {"S1": story},
    }


def _manifest_with(authored_backend, backend="claude") -> dict:
    manifest = _legacy_manifest()
    story = manifest["stories"]["S1"]
    story["authored_backend"] = authored_backend
    story["backend"] = backend
    return manifest


# --------------------------------------------------------------------------- #
# 1. Digest of the manifest shape
# --------------------------------------------------------------------------- #


def test_should_keep_legacy_digest_for_manifest_without_authored_backend():
    manifest = _legacy_manifest()

    assert canonical_digest(manifest) == _expected_legacy_digest(manifest)


def test_should_not_move_digest_when_runtime_stamp_overwrites_backend():
    authored = _manifest_with(authored_backend="claude", backend="claude")
    stamped = _manifest_with(authored_backend="claude", backend="local")

    assert canonical_digest(stamped) == canonical_digest(authored)


def test_should_not_move_digest_when_dispatch_stamps_backend_on_unpinned_story():
    unpinned = _manifest_with(authored_backend=None, backend=None)
    stamped = _manifest_with(authored_backend=None, backend="claude")

    assert canonical_digest(stamped) == canonical_digest(unpinned)


def test_should_hash_authored_none_not_the_runtime_stamp():
    # Negative control: the stamp is not what the digest reads.
    unpinned = _manifest_with(authored_backend=None, backend="claude")
    pinned = _manifest_with(authored_backend="claude", backend="claude")

    assert canonical_digest(unpinned) != canonical_digest(pinned)


def test_should_change_digest_when_authored_backend_changes():
    before = _manifest_with(authored_backend="local", backend="local")
    after = _manifest_with(authored_backend="ollama", backend="local")

    assert canonical_digest(before) != canonical_digest(after)


def test_should_change_digest_when_authored_backend_is_removed_to_none():
    pinned = _manifest_with(authored_backend="local", backend="local")
    unpinned = _manifest_with(authored_backend=None, backend="local")

    assert canonical_digest(pinned) != canonical_digest(unpinned)


@pytest.mark.parametrize("malformed", [123, ["local"], {"name": "local"}, True])
def test_should_hash_malformed_authored_backend_as_given(malformed):
    stamped = _manifest_with(authored_backend=malformed, backend="claude")
    clean = _manifest_with(authored_backend="local", backend="claude")

    assert canonical_digest(stamped) != canonical_digest(clean)


def test_should_not_coerce_malformed_authored_backend_to_its_string_form():
    as_int = _manifest_with(authored_backend=123)
    as_str = _manifest_with(authored_backend="123")

    assert canonical_digest(as_int) != canonical_digest(as_str)


def test_should_still_move_digest_for_backend_without_authored_backend_key():
    # Legacy behaviour kept: a manifest with no authored_backend key hashes
    # ``backend`` directly, so an edit to it is still a content change.
    before = _legacy_manifest()
    after = copy.deepcopy(before)
    after["stories"]["S1"]["backend"] = "local"

    assert canonical_digest(before) != canonical_digest(after)


def test_should_not_mutate_manifest_when_hashing_authored_backend():
    manifest = _manifest_with(authored_backend="local", backend="claude")
    before = copy.deepcopy(manifest)

    canonical_digest(manifest)

    assert manifest == before


# --------------------------------------------------------------------------- #
# 2. Ingest / approval round trips
# --------------------------------------------------------------------------- #


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return d


@pytest.fixture
def provider(monkeypatch):
    """A ticket provider that issues no ids, so story keys come from the plan."""

    class _Provider:
        enabled = False

        def create_epic(self, summary):
            return None

        def create_story(self, summary, description, epic_id, agent):
            return None

    prov = _Provider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    return prov


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")


def _story(**over) -> dict:
    story = {
        "summary": "Do the thing",
        "key": "S1",
        "agent_instructions": _PREFLIGHT + "\nDo it carefully.",
        "description": "The original description.",
    }
    story.update(over)
    return story


def _document(tmp_path, stories=None) -> dict:
    return {
        "repo_root": str(tmp_path),
        "team": "core",
        "epics": [
            {"summary": "E1", "stories": stories if stories is not None else [_story()]}
        ],
    }


def _stage(document: dict, name: str = PLAN) -> None:
    result = p.save_plan(name, json.dumps(document))
    assert result["ok"] is True, result


def _ingest(document: dict, name: str = PLAN) -> dict:
    _stage(document, name)
    result = p.ingest_plan(name)
    assert result["ok"] is True, result
    return result


def _manifest_path(plan_dir, name: str = PLAN):
    return plan_dir / f"{name}.manifest.json"


def _read_manifest(plan_dir, name: str = PLAN) -> dict:
    return json.loads(_manifest_path(plan_dir, name).read_text())


def _write_manifest(plan_dir, manifest: dict, name: str = PLAN) -> None:
    _manifest_path(plan_dir, name).write_text(json.dumps(manifest))


def _approval(plan_dir, name: str = PLAN) -> dict:
    return _read_manifest(plan_dir, name).get("approval", {})


def _approve(name: str = PLAN) -> None:
    result = PipelineService().approve_plan(name)
    assert result["ok"] is True, result
    assert result["status"] == "approved", result


def _simulate_dispatch_stamp(plan_dir, backend: str, name: str = PLAN) -> None:
    """Write the runtime ``backend`` the dispatcher/escalation stamps on a story."""
    manifest = _read_manifest(plan_dir, name)
    for story in manifest["stories"].values():
        story["backend"] = backend
    _write_manifest(plan_dir, manifest, name)


def test_should_record_authored_backend_none_for_unpinned_story(
    plan_dir, provider, gate_on
):
    _ingest(_document(plan_dir.parent))

    story = _read_manifest(plan_dir)["stories"]["S1"]

    assert story["authored_backend"] is None
    assert story["backend"] is None


def test_should_record_authored_backend_from_plan(plan_dir, provider, gate_on):
    _ingest(_document(plan_dir.parent, [_story(backend="local")]))

    story = _read_manifest(plan_dir)["stories"]["S1"]

    assert story["authored_backend"] == "local"
    assert story["backend"] == "local"


def test_should_keep_approval_after_dispatch_stamps_backend_on_unpinned_story(
    plan_dir, provider, gate_on
):
    _ingest(_document(plan_dir.parent))
    _approve()

    _simulate_dispatch_stamp(plan_dir, "claude")

    manifest = _read_manifest(plan_dir)
    assert canonical_digest(manifest) == _approval(plan_dir)["digest"]
    assert reset_if_changed(manifest) is False
    assert _approval(plan_dir)["status"] == "approved"


def test_should_reset_approval_when_authored_backend_changes(
    plan_dir, provider, gate_on
):
    _ingest(_document(plan_dir.parent, [_story(backend="local")]))
    _approve()

    _ingest(_document(plan_dir.parent, [_story(backend="ollama")]))

    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval", approval
    assert _read_manifest(plan_dir)["stories"]["S1"]["authored_backend"] == "ollama"


def test_should_keep_approval_when_escalation_stamps_local_over_authored_claude(
    plan_dir, provider, gate_on
):
    _ingest(_document(plan_dir.parent, [_story(backend="claude")]))
    _approve()

    _simulate_dispatch_stamp(plan_dir, "local")

    manifest = _read_manifest(plan_dir)
    assert reset_if_changed(manifest) is False
    assert _approval(plan_dir)["status"] == "approved"


def test_should_reproduce_digest_after_export_and_ingest_post_dispatch(
    plan_dir, provider, gate_on, tmp_path
):
    _ingest(_document(tmp_path, [_story(backend="local")]))
    _approve()
    approved_digest = _approval(plan_dir)["digest"]
    _simulate_dispatch_stamp(plan_dir, "claude")

    exported = p.export_plan(PLAN)
    assert exported["ok"] is True, exported
    exported_story = exported["plan"]["epics"][0]["stories"][0]
    assert exported_story["backend"] == "local"

    _ingest(exported["plan"])

    assert _approval(plan_dir)["status"] == "approved"
    assert _approval(plan_dir)["digest"] == approved_digest


def test_should_refresh_authored_backend_on_reingest_after_dispatch(
    plan_dir, provider, gate_on, tmp_path
):
    _ingest(_document(tmp_path, [_story(backend="local")]))
    _approve()
    _simulate_dispatch_stamp(plan_dir, "claude")

    _ingest(_document(tmp_path, [_story(backend="local")]))

    story = _read_manifest(plan_dir)["stories"]["S1"]
    assert story["authored_backend"] == "local"
    assert _approval(plan_dir)["status"] == "approved"
