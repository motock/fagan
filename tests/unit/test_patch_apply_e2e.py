"""WAP-11: end-to-end patch propose -> review -> apply through the REAL routes.

This module is the integration counterpart to the per-story unit suites.  It
does not call ``pipeline.worktree_patch`` directly for the graded flows: it
drives the three merged HTTP routes (WAP-9 propose, WAP-10 review + apply)
with a real ``fastapi.testclient.TestClient`` against ``app.dashboard.app``
(including its real API-key middleware), so a break anywhere in the
propose -> store -> review -> apply -> audit chain shows up here.

The fixture is REAL, not a double:

* a temp git repo (``git init`` / ``config`` / ``add`` / ``commit``) stands in
  for the story worktree, so ``git apply --check`` / ``git apply`` and
  ``git status --porcelain`` are the genuine article;
* the manifest lives in a patched ``PLAN_DIR`` (the ``plan_dir`` fixture
  convention from ``tests/unit/conftest.py``) and carries a STUCK story
  (``status == "failed"``) whose ``worktree`` points at that repo, plus an
  ACTIVE story (``status == "in_progress"``) and a story with no worktree;
* the client carries the real dashboard key from ``get_or_create_api_key``
  (the ``tests/unit/test_chat_internal_loopback_auth.py`` pattern).

Graded scenarios (one test each): the full happy flow, single-use, TTL
expiry, the active-story refusal, the ``.git/config`` deny-list negative, the
context-drift negative, whole-file-replacement refusal, and the cross-patch
token negative.  Route-level boundaries (origin gates, unknown ids, malformed
input, missing fields) follow.

Hermeticity: the in-process record store is cleared around every test, and
``event_wiring.get_bus`` is replaced with a recording double so no real
notification dispatch happens.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard_module
import pipeline.server as pserver
from app.auth import ORIGIN_CHAT, ORIGIN_HEADER, ORIGIN_UI, get_or_create_api_key
from pipeline import concurrency as pcon
from pipeline import event_wiring, worktree_patch
from pipeline import persistence as ppers

PLAN_NAME = "wap11plan"
STUCK_STORY = "WAP-11"
ACTIVE_STORY = "WAP-11-ACTIVE"
NOWORKTREE_STORY = "WAP-11-NOWT"
MISSING_STORY = "WAP-11-NOPE"

PROPOSE_PATH = "/api/worktree/patch/propose"
GET_PATH = "/api/worktree/patch/{patch_id}"
APPLY_PATH = "/api/worktree/patch/{patch_id}/apply"

GENERIC_DETAIL = "origin not permitted"
NO_SUCH_PATCH_DETAIL = "no such patch"

TARGET_REL = "src/app.py"
ORIGINAL_TEXT = "x = 1\ny = 2\n"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A private PLAN_DIR shared by the store, the engine and the journal.

    Shadows the conftest ``plan_dir`` fixture so the dashboard module's own
    ``PLAN_DIR`` binding (when it has one) is redirected too -- the same
    belt-and-braces the WAP-10 route suite uses.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(pserver, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    if hasattr(dashboard_module, "PLAN_DIR"):
        monkeypatch.setattr(dashboard_module, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree(tmp_path):
    """A real, committed, symlink-free git repo standing in for a worktree."""
    root = (tmp_path / "worktree").resolve()
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(ORIGINAL_TEXT)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test Author")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def manifest(plan_dir, worktree):
    """A manifest with a stuck story, an active story and a worktree-less one."""
    _write_manifest(
        plan_dir,
        PLAN_NAME,
        {
            STUCK_STORY: {"status": "failed", "worktree": str(worktree)},
            ACTIVE_STORY: {"status": "in_progress", "worktree": str(worktree)},
            NOWORKTREE_STORY: {"status": "failed"},
        },
    )
    return plan_dir / f"{PLAN_NAME}.manifest.json"


@pytest.fixture
def client():
    """A real TestClient against the real app, carrying the real API key."""
    return TestClient(
        dashboard_module.app,
        headers={"X-Pipeline-Api-Key": get_or_create_api_key()},
    )


@pytest.fixture(autouse=True)
def _clean_patch_store():
    """Keep the in-process record store from leaking between tests."""
    store = getattr(worktree_patch, "_PATCH_STORE", None)
    if isinstance(store, dict):
        store.clear()
    yield
    if isinstance(store, dict):
        store.clear()


class _RecordingBus:
    """A bus double that records every published event and never dispatches."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def bus(monkeypatch) -> _RecordingBus:
    """Capture events published through the LAZY ``event_wiring.get_bus``."""
    recording = _RecordingBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: recording)
    if hasattr(worktree_patch, "get_bus"):
        monkeypatch.setattr(worktree_patch, "get_bus", lambda: recording)
    return recording


# ---------------------------------------------------------------------------
# helpers (shared within this file only)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git command in *repo* and fail loudly on a non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_manifest(plan_dir: Path, plan_name: str, stories: dict) -> Path:
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps({"stories": stories}))
    return path


def _diff(rel_path: str, old_text: str, new_text: str, n: int = 3) -> str:
    """A unified diff for *rel_path* from *old_text* to *new_text*."""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=n,
        )
    )


def _propose(
    client: TestClient,
    diff_text: str,
    *,
    origin: str | None = ORIGIN_CHAT,
    story: str = STUCK_STORY,
    plan: str = PLAN_NAME,
):
    headers = {} if origin is None else {ORIGIN_HEADER: origin}
    return client.post(
        PROPOSE_PATH,
        json={"plan_name": plan, "story_key": story, "unified_diff": diff_text},
        headers=headers,
    )


def _review(client: TestClient, patch_id: str, *, origin: str | None = ORIGIN_UI):
    headers = {} if origin is None else {ORIGIN_HEADER: origin}
    return client.get(GET_PATH.format(patch_id=patch_id), headers=headers)


def _apply(
    client: TestClient,
    patch_id: str,
    token: str,
    *,
    origin: str | None = ORIGIN_UI,
    extra: dict | None = None,
):
    headers = {} if origin is None else {ORIGIN_HEADER: origin}
    body = {"confirmation_token": token}
    if extra:
        body.update(extra)
    return client.post(APPLY_PATH.format(patch_id=patch_id), json=body, headers=headers)


def _journal_entries(plan_name: str, story_key: str) -> list:
    """The parsed journal entries for the story (``[]`` when absent)."""
    path = ppers._journal_path(plan_name, story_key)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _tree_digest(root: Path) -> str:
    """sha256 over every TRACKED file's path + content, in sorted order."""
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    rel_paths = sorted(p for p in listing.decode("utf-8").split("\0") if p)
    digest = hashlib.sha256()
    for rel in rel_paths:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _porcelain(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _propose_and_review(client: TestClient, diff_text: str, *, story: str = STUCK_STORY):
    """Propose (chat origin) then review (ui origin); return (patch_id, token)."""
    proposed = _propose(client, diff_text, origin=ORIGIN_CHAT, story=story)
    assert proposed.status_code == 200, proposed.text
    patch_id = proposed.json()["patch_id"]
    reviewed = _review(client, patch_id, origin=ORIGIN_UI)
    assert reviewed.status_code == 200, reviewed.text
    return patch_id, reviewed.json()["confirmation_token"]


# ---------------------------------------------------------------------------
# 1. full happy flow
# ---------------------------------------------------------------------------


def test_full_happy_flow_propose_review_apply(client, manifest, worktree):
    """propose (chat) -> review (ui) -> apply (ui) end to end."""
    target = worktree / "src" / "app.py"
    original = target.read_text()
    patched = original.replace("y = 2", "y = 3")
    diff_text = _diff(TARGET_REL, original, patched)
    assert diff_text, "sanity: the fixture diff must not be empty"

    # act 1: the model proposes (chat origin).
    proposed = _propose(client, diff_text, origin=ORIGIN_CHAT)
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    patch_id = body["patch_id"]
    assert isinstance(patch_id, str) and patch_id

    # act 2: the human reviews (ui origin) and receives the token.
    reviewed = _review(client, patch_id, origin=ORIGIN_UI)
    assert reviewed.status_code == 200, reviewed.text
    review = reviewed.json()
    assert review["diff_text"] == diff_text, "the human must see the exact diff"
    assert review["status"] == "pending"
    token = review["confirmation_token"]
    assert isinstance(token, str) and token

    # act 3: the human applies.
    applied = _apply(client, patch_id, token, origin=ORIGIN_UI)
    assert applied.status_code == 200, applied.text
    assert applied.json()["ok"] is True

    # assert: the worktree file now holds the patched content.
    assert target.read_text() == patched

    # assert: git sees the modification.
    porcelain = _porcelain(worktree)
    lines = [line for line in porcelain.splitlines() if line.strip()]
    assert any(line.endswith(TARGET_REL) for line in lines), porcelain
    assert any(line[:2].strip() == "M" for line in lines), porcelain

    # assert: WAP-8 audit is wired end to end -- both journal entries exist.
    entries = _journal_entries(PLAN_NAME, STUCK_STORY)
    actions = [entry.get("action") for entry in entries]
    assert "patch_proposed" in actions, actions
    assert "patch_applied" in actions, actions

    proposed_entry = next(e for e in entries if e.get("action") == "patch_proposed")
    applied_entry = next(e for e in entries if e.get("action") == "patch_applied")
    assert proposed_entry["patch_id"] == patch_id
    assert applied_entry["patch_id"] == patch_id
    # Secure by Design: the audit carries the diff HASH, never the diff body.
    assert proposed_entry["diff_hash"] == hashlib.sha256(
        diff_text.encode("utf-8")
    ).hexdigest()
    assert "y = 3" not in json.dumps(entries)


# ---------------------------------------------------------------------------
# 2. single use
# ---------------------------------------------------------------------------


def test_second_apply_with_the_same_token_is_refused(client, manifest, worktree):
    """A successful apply is single-use: the same token cannot apply twice."""
    target = worktree / "src" / "app.py"
    original = target.read_text()
    patched = original.replace("y = 2", "y = 3")
    diff_text = _diff(TARGET_REL, original, patched)

    patch_id, token = _propose_and_review(client, diff_text)

    first = _apply(client, patch_id, token, origin=ORIGIN_UI)
    assert first.status_code == 200, first.text
    assert target.read_text() == patched

    second = _apply(client, patch_id, token, origin=ORIGIN_UI)
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "patch already applied"
    # The worktree still holds the FIRST apply's content, and only that.
    assert target.read_text() == patched


# ---------------------------------------------------------------------------
# 3. TTL expiry
# ---------------------------------------------------------------------------


def test_expired_record_is_refused_through_the_route(client, manifest, worktree):
    """An expired record is indistinguishable from an unknown one: 404."""
    target = worktree / "src" / "app.py"
    original = target.read_text()
    patched = original.replace("y = 2", "y = 3")
    diff_text = _diff(TARGET_REL, original, patched)

    patch_id, token = _propose_and_review(client, diff_text)

    record = worktree_patch.get_patch_record(patch_id)
    assert record is not None, "sanity: the record must exist before expiry"
    record["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()

    refused = _apply(client, patch_id, token, origin=ORIGIN_UI)
    assert refused.status_code == 404, refused.text
    assert target.read_text() == original, "an expired patch must not touch the worktree"


# ---------------------------------------------------------------------------
# 4. active story
# ---------------------------------------------------------------------------


def test_active_story_refuses_both_propose_and_apply(client, manifest, worktree):
    """A story still owned by a running agent takes no patch, at either end."""
    target = worktree / "src" / "app.py"
    original = target.read_text()
    patched = original.replace("y = 2", "y = 3")
    diff_text = _diff(TARGET_REL, original, patched)

    proposed = _propose(client, diff_text, origin=ORIGIN_CHAT, story=ACTIVE_STORY)
    assert proposed.status_code == 409, proposed.text
    assert worktree_patch._PATCH_STORE == {}, "no record may be created"

    # A record that predates the story going active is still refused at apply.
    record = worktree_patch.create_patch_record(
        PLAN_NAME, ACTIVE_STORY, diff_text, [TARGET_REL], 1
    )
    applied = _apply(
        client, record["patch_id"], record["confirmation_token"], origin=ORIGIN_UI
    )
    assert applied.status_code == 409, applied.text
    assert applied.json()["detail"] == "story is active"
    assert target.read_text() == original


# ---------------------------------------------------------------------------
# 5. NEGATIVE TEST 1: the .git deny list
# ---------------------------------------------------------------------------


def test_git_config_patch_is_accepted_for_review_but_refused_at_apply(
    client, manifest, worktree
):
    """A ``.git``-touching patch is inspectable, then refused at apply."""
    config = worktree / ".git" / "config"
    before = config.read_bytes()
    original = config.read_text()
    lines = original.splitlines(keepends=True)
    assert len(lines) >= 3, "sanity: .git/config needs context lines to anchor a hunk"
    middle = len(lines) // 2
    lines[middle] = lines[middle].rstrip("\n") + " # patched-by-model\n"
    patched = "".join(lines)
    diff_text = _diff(".git/config", original, patched)
    assert diff_text

    # propose: ACCEPTED, so the human can see what the model tried.
    proposed = _propose(client, diff_text, origin=ORIGIN_CHAT)
    assert proposed.status_code == 200, proposed.text
    patch_id = proposed.json()["patch_id"]

    # review: the human sees exactly the attempted diff.
    reviewed = _review(client, patch_id, origin=ORIGIN_UI)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["diff_text"] == diff_text

    # apply: refused by the deny list.
    applied = _apply(
        client, patch_id, reviewed.json()["confirmation_token"], origin=ORIGIN_UI
    )
    assert applied.status_code == 403, applied.text
    assert applied.json()["detail"] == "patch target refused"

    # assert: .git/config is byte-identical before and after.
    assert config.read_bytes() == before


# ---------------------------------------------------------------------------
# 6. NEGATIVE TEST 7: context drift
# ---------------------------------------------------------------------------


def test_context_drift_is_refused_and_worktree_is_untouched(client, manifest, worktree):
    """A file edited under a pending patch makes the apply fail closed."""
    target = worktree / "src" / "app.py"
    original = target.read_text()
    patched = original.replace("y = 2", "y = 3")
    diff_text = _diff(TARGET_REL, original, patched)

    patch_id, token = _propose_and_review(client, diff_text)

    # drift: the target changes after the patch was proposed.
    drifted = original.replace("y = 2", "y = 42")
    target.write_text(drifted)
    before = _tree_digest(worktree)

    refused = _apply(client, patch_id, token, origin=ORIGIN_UI)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == "patch does not apply (context drift)"

    # assert: the worktree is byte-identical to its post-drift state.
    assert _tree_digest(worktree) == before
    assert target.read_text() == drifted


# ---------------------------------------------------------------------------
# 7. whole-file replacement
# ---------------------------------------------------------------------------


def test_whole_file_replacement_is_refused_at_propose(client, manifest, worktree):
    """A no-context delete-all/add-all payload is refused before it is stored."""
    diff_text = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-x = 1\n"
        "-y = 2\n"
        "+x = 9\n"
        "+y = 9\n"
    )

    proposed = _propose(client, diff_text, origin=ORIGIN_CHAT)
    assert proposed.status_code == 413, proposed.text
    assert "whole-file replacement refused" in proposed.json()["detail"]
    assert worktree_patch._PATCH_STORE == {}, "nothing may be stored"
    assert (worktree / "src" / "app.py").read_text() == ORIGINAL_TEXT


# ---------------------------------------------------------------------------
# 8. cross-patch token
# ---------------------------------------------------------------------------


def test_token_from_another_patch_is_refused_and_both_stay_pending(
    client, manifest, worktree
):
    """A token is bound to one patch id: it cannot be replayed on another."""
    target = worktree / "src" / "app.py"
    original = target.read_text()

    diff_a = _diff(TARGET_REL, original, original.replace("y = 2", "y = 3"))
    diff_b = _diff(TARGET_REL, original, original.replace("x = 1", "x = 5"))

    proposed_a = _propose(client, diff_a, origin=ORIGIN_CHAT)
    proposed_b = _propose(client, diff_b, origin=ORIGIN_CHAT)
    assert proposed_a.status_code == 200, proposed_a.text
    assert proposed_b.status_code == 200, proposed_b.text
    id_a = proposed_a.json()["patch_id"]
    id_b = proposed_b.json()["patch_id"]
    assert id_a != id_b

    token_b = _review(client, id_b, origin=ORIGIN_UI).json()["confirmation_token"]

    refused = _apply(client, id_a, token_b, origin=ORIGIN_UI)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "invalid confirmation token"

    # assert: neither record was consumed, and the worktree is untouched.
    assert worktree_patch.get_patch_record(id_a)["status"] == "pending"
    assert worktree_patch.get_patch_record(id_b)["status"] == "pending"
    assert target.read_text() == original


# ---------------------------------------------------------------------------
# route-level boundaries
# ---------------------------------------------------------------------------


def test_propose_with_chat_origin_withholds_the_confirmation_token(
    client, manifest, worktree
):
    """The constrained party never holds the credential (WAP-9 design)."""
    original = (worktree / "src" / "app.py").read_text()
    diff_text = _diff(TARGET_REL, original, original.replace("y = 2", "y = 3"))

    proposed = _propose(client, diff_text, origin=ORIGIN_CHAT)
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["patch_id"]
    assert "confirmation_token" not in body

    # The ui-origin review is where the human picks the token up.
    reviewed = _review(client, body["patch_id"], origin=ORIGIN_UI)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["confirmation_token"]


def test_propose_origin_gate_refuses_absent_and_unknown_origins(
    client, manifest, worktree
):
    """The propose allow-list is exactly chat | ui."""
    original = (worktree / "src" / "app.py").read_text()
    diff_text = _diff(TARGET_REL, original, original.replace("y = 2", "y = 3"))

    for origin in (None, "", "bogus", "UI", "Chat"):
        refused = _propose(client, diff_text, origin=origin)
        assert refused.status_code == 403, (origin, refused.text)
        assert refused.json()["detail"] == GENERIC_DETAIL
    assert worktree_patch._PATCH_STORE == {}


def test_review_and_apply_routes_are_ui_only(client, manifest, worktree):
    """GET and apply refuse chat, absent and unknown origins alike."""
    original = (worktree / "src" / "app.py").read_text()
    diff_text = _diff(TARGET_REL, original, original.replace("y = 2", "y = 3"))
    patch_id, token = _propose_and_review(client, diff_text)

    for origin in (ORIGIN_CHAT, None, "bogus"):
        reviewed = _review(client, patch_id, origin=origin)
        assert reviewed.status_code == 403, (origin, reviewed.text)
        assert reviewed.json()["detail"] == GENERIC_DETAIL

        applied = _apply(client, patch_id, token, origin=origin)
        assert applied.status_code == 403, (origin, applied.text)
        assert applied.json()["detail"] == GENERIC_DETAIL

    assert (worktree / "src" / "app.py").read_text() == original


def test_unknown_patch_id_is_404_on_review_and_apply(client, manifest):
    """An unknown patch id leaks nothing beyond 'not available'."""
    reviewed = _review(client, "wp-does-not-exist", origin=ORIGIN_UI)
    assert reviewed.status_code == 404, reviewed.text
    assert reviewed.json()["detail"] == NO_SUCH_PATCH_DETAIL

    applied = _apply(client, "wp-does-not-exist", "any-token", origin=ORIGIN_UI)
    assert applied.status_code == 404, applied.text
    assert applied.json()["detail"] == NO_SUCH_PATCH_DETAIL


def test_propose_unknown_plan_and_story_are_404(client, manifest, worktree):
    """A missing manifest or story is a 404, not a 500."""
    original = (worktree / "src" / "app.py").read_text()
    diff_text = _diff(TARGET_REL, original, original.replace("y = 2", "y = 3"))

    unknown_plan = _propose(client, diff_text, origin=ORIGIN_CHAT, plan="no-such-plan")
    assert unknown_plan.status_code == 404, unknown_plan.text

    unknown_story = _propose(
        client, diff_text, origin=ORIGIN_CHAT, story=MISSING_STORY
    )
    assert unknown_story.status_code == 404, unknown_story.text
    assert worktree_patch._PATCH_STORE == {}


def test_propose_story_without_a_worktree_is_409(client, manifest, worktree):
    """A stuck story with no usable worktree cannot take a patch."""
    original = (worktree / "src" / "app.py").read_text()
    diff_text = _diff(TARGET_REL, original, original.replace("y = 2", "y = 3"))

    refused = _propose(client, diff_text, origin=ORIGIN_CHAT, story=NOWORKTREE_STORY)
    assert refused.status_code == 409, refused.text
    assert worktree_patch._PATCH_STORE == {}


def test_malformed_diff_is_refused_at_propose(client, manifest, worktree):
    """Unparseable input is a 413 and is never stored."""
    refused = _propose(client, "this is not a unified diff\n", origin=ORIGIN_CHAT)
    assert refused.status_code == 413, refused.text
    assert worktree_patch._PATCH_STORE == {}


def test_escaping_path_is_refused_at_propose(client, manifest, worktree):
    """A path that escapes the worktree is a 400 (security refusal)."""
    diff_text = (
        "--- a/../escape.txt\n"
        "+++ b/../escape.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "-b\n"
        "+c\n"
    )
    refused = _propose(client, diff_text, origin=ORIGIN_CHAT)
    assert refused.status_code == 400, refused.text
    assert worktree_patch._PATCH_STORE == {}


def test_missing_required_field_is_422(client, manifest, worktree):
    """The propose body requires plan_name, story_key and unified_diff."""
    for body in (
        {"story_key": STUCK_STORY, "unified_diff": "x"},
        {"plan_name": PLAN_NAME, "unified_diff": "x"},
        {"plan_name": PLAN_NAME, "story_key": STUCK_STORY},
    ):
        refused = client.post(
            PROPOSE_PATH, json=body, headers={ORIGIN_HEADER: ORIGIN_CHAT}
        )
        assert refused.status_code == 422, (body, refused.text)

    refused_apply = client.post(
        APPLY_PATH.format(patch_id="wp-anything"),
        json={},
        headers={ORIGIN_HEADER: ORIGIN_UI},
    )
    assert refused_apply.status_code == 422, refused_apply.text


def test_empty_diff_boundary_never_touches_the_worktree(client, manifest, worktree):
    """Boundary: an empty diff carries no hunks.

    WAP-5's parser accepts it (zero paths, zero added lines) and the route
    stores a record with an empty path list; nothing in the worktree is
    touched either way.  Pinned so a later change to the parser's
    empty-input verdict is a deliberate, visible decision.
    """
    before = _tree_digest(worktree)

    proposed = _propose(client, "", origin=ORIGIN_CHAT)
    assert proposed.status_code in (200, 413), proposed.text
    if proposed.status_code == 200:
        assert proposed.json()["paths"] == []

    assert _tree_digest(worktree) == before


def test_forged_diff_in_the_apply_body_is_inert(client, manifest, worktree):
    """The apply route applies the STORED diff, never anything the caller sends."""
    target = worktree / "src" / "app.py"
    original = target.read_text()
    stored_diff = _diff(TARGET_REL, original, original.replace("y = 2", "y = 3"))
    forged_diff = _diff(TARGET_REL, original, original.replace("y = 2", "y = 999"))

    patch_id, token = _propose_and_review(client, stored_diff)

    applied = _apply(
        client,
        patch_id,
        token,
        origin=ORIGIN_UI,
        extra={"unified_diff": forged_diff},
    )
    assert applied.status_code == 200, applied.text
    assert target.read_text() == original.replace("y = 2", "y = 3")
    assert target.read_text() != original.replace("y = 2", "y = 999")
