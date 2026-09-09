"""Adversarial tests for the active-workspace surface (GET/POST
``/api/workspace`` plus the save-plan and decompose active-workspace
fallbacks).

Threat model
------------
An attacker who can reach the dashboard HTTP API (WS-07's assumption) tries
to:

(a) set the durable active workspace to a sensitive or traversing location;
(b) smuggle a path through percent-encoding or a symlink;
(c) make an error response leak resolved filesystem structure;
(d) exploit a stale or corrupt ``active_workspace.json`` record.

``pipeline/workspace.py`` is the PRIMARY control and is covered by
``tests/unit/test_workspace_path_security.py``; neither is touched here.
This module tests the surface LAYERED ON TOP of it -- the routes, the
durable active record, and the two fallbacks -- so that a bypass of any one
layer is not sufficient (defense in depth).

The two properties every denial must satisfy:

* **fail closed** -- a rejected path never mutates durable state, and never
  lets a plan reach disk with an unvalidated ``repo_root``;
* **error hygiene** -- every 400 detail is one of the fixed generic strings
  ``pipeline.workspace.sanitize_error_message`` guarantees, carrying no
  on-disk absolute path and no stack-trace marker.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline.workspace import REPO_ROOT

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding this surface touches to a tmp dir.

    Mirrors the fixture in tests/unit/test_dashboard_workspace_active.py:
    ``active_workspace.json`` and saved plans resolve PLAN_DIR through
    ``pipeline.server`` at call time, and the dashboard module holds its own
    binding, so all four are patched.
    """
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def client():
    return TestClient(d.app)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _init_git_repo(path: Path) -> Path:
    """A real, minimal git repo with one commit -- a legitimate workspace."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("commit", "--allow-empty", "-m", "init", cwd=path)
    return path


def _active_record(plan_dir: Path) -> Path:
    return plan_dir / "active_workspace.json"


# The fixed, path-free strings the workspace module is allowed to surface.
# Membership, not exact enumeration: a new generic string may be added, but
# every observed detail must be one that carries no filesystem structure.
GENERIC_DETAILS = {
    "workspace path must be a non-empty string",
    "workspace path must not be empty",
    "workspace path must be absolute",
    "workspace path must not contain control characters",
    "workspace path must not contain backslash separators",
    "workspace path must not contain '..' segments",
    "workspace path must not contain separator look-alikes",
    "workspace path must not traverse symbolic links",
    "workspace path could not be safety-checked",
    "workspace path is inside a protected system location",
    "workspace path is outside the allowed create roots",
    "path does not exist",
    "path is not a directory",
    "not a git repository",
    "git repository has no commits",
}

# Markers that would betray internal structure if they ever appeared.
TRACE_MARKERS = ("Traceback", "site-packages", ".py", 'File "', "line ")


def _assert_no_structure_leak(text: str, *secret_paths: object) -> None:
    """Assert *text* leaks no filesystem structure or stack-trace detail."""
    assert isinstance(text, str)
    for marker in TRACE_MARKERS:
        assert marker not in text, f"stack-trace marker {marker!r} in {text!r}"
    for secret in secret_paths:
        secret_str = str(secret)
        assert secret_str not in text, f"leaked {secret_str!r} in {text!r}"
        # Also reject a leak of the resolved (realpath) spelling.
        assert os.path.realpath(secret_str) not in text
    # No on-disk absolute path may appear at all.
    for token in text.split():
        candidate = token.strip("'\"“”,.;:()[]")
        if candidate.startswith("/"):
            assert not os.path.exists(candidate), (
                f"detail names an on-disk absolute path: {candidate!r}"
            )


def _hostile_paths(tmp_path: Path, link_target: Path) -> list[tuple[str, str]]:
    """(label, path) for each smuggling vector in the threat model."""
    symlink = tmp_path / "link-to-repo"
    if not symlink.exists():
        symlink.symlink_to(link_target)
    return [
        ("under_etc", "/etc/ws-attacker"),
        ("under_repo_root", str(REPO_ROOT / "ws-attacker")),
        ("symlinked", str(symlink)),
        ("percent_encoded_traversal", "/tmp/%2e%2e/etc/ws-attacker"),
        ("literal_traversal", f"{tmp_path}/ws/../../etc/ws-attacker"),
    ]


# ---------------------------------------------------------------------------
# 1. POST /api/workspace -- every hostile spelling is denied, generically,
#    and a denial never mutates the durable active record.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "under_etc",
        "under_repo_root",
        "symlinked",
        "percent_encoded_traversal",
        "literal_traversal",
    ],
)
def test_post_workspace_denies_hostile_path_with_400(
    client, plan_dir, tmp_path, label
):
    repo = _init_git_repo(tmp_path / "legit-repo")
    path = dict(_hostile_paths(tmp_path, repo))[label]

    res = client.post("/api/workspace", json={"path": path})

    assert res.status_code == 400


@pytest.mark.parametrize(
    "label",
    [
        "under_etc",
        "under_repo_root",
        "symlinked",
        "percent_encoded_traversal",
        "literal_traversal",
    ],
)
def test_post_workspace_denial_detail_is_generic_and_path_free(
    client, plan_dir, tmp_path, label
):
    repo = _init_git_repo(tmp_path / "legit-repo")
    path = dict(_hostile_paths(tmp_path, repo))[label]

    res = client.post("/api/workspace", json={"path": path})

    detail = res.json()["detail"]
    assert detail in GENERIC_DETAILS
    _assert_no_structure_leak(detail, tmp_path, repo, REPO_ROOT)


@pytest.mark.parametrize(
    "label",
    [
        "under_etc",
        "under_repo_root",
        "symlinked",
        "percent_encoded_traversal",
        "literal_traversal",
    ],
)
def test_post_workspace_denial_never_mutates_active_workspace(
    client, plan_dir, tmp_path, label
):
    """A denial must leave the PREVIOUS selection exactly as it was."""
    repo = _init_git_repo(tmp_path / "legit-repo")
    accepted = client.post("/api/workspace", json={"path": str(repo)})
    assert accepted.status_code == 200
    previous_active = accepted.json()["path"]

    hostile = dict(_hostile_paths(tmp_path, repo))[label]
    denied = client.post("/api/workspace", json={"path": hostile})
    assert denied.status_code == 400

    assert client.get("/api/workspace").json() == {"active": previous_active}


def test_post_workspace_denial_on_fresh_state_leaves_active_unset(
    client, plan_dir, tmp_path
):
    """Boundary: denial with NO previous selection must not create one."""
    assert client.get("/api/workspace").json() == {"active": None}

    res = client.post("/api/workspace", json={"path": "/etc/ws-attacker"})
    assert res.status_code == 400

    assert client.get("/api/workspace").json() == {"active": None}
    assert not _active_record(plan_dir).exists()


def test_post_workspace_denial_does_not_record_a_recent_workspace(
    client, plan_dir
):
    """Denied paths must not leak into the recents list either."""
    res = client.post("/api/workspace", json={"path": "/etc/ws-attacker"})
    assert res.status_code == 400

    assert p._store.get_recent_workspaces() == []


# ---------------------------------------------------------------------------
# 2. Corrupt / hostile active_workspace.json -- reads as UNSET, and a save
#    relying on the fallback behaves exactly as if no workspace were set.
# ---------------------------------------------------------------------------


CORRUPT_RECORDS = {
    "traversing_path": '{"path": "../escape"}',
    "non_string_path": '{"path": 5}',
    "not_json": "not json",
    "nested_dict_path": '{"path": {"path": "/tmp"}}',
}


@pytest.mark.parametrize("label", sorted(CORRUPT_RECORDS))
def test_get_workspace_reports_corrupt_record_as_unset(client, plan_dir, label):
    _active_record(plan_dir).write_text(CORRUPT_RECORDS[label])

    res = client.get("/api/workspace")

    assert res.status_code == 200
    assert res.json() == {"active": None}


@pytest.mark.parametrize("label", sorted(CORRUPT_RECORDS))
def test_get_workspace_never_500s_on_corrupt_record(client, plan_dir, label):
    _active_record(plan_dir).write_text(CORRUPT_RECORDS[label])

    assert client.get("/api/workspace").status_code == 200


@pytest.mark.parametrize("label", sorted(CORRUPT_RECORDS))
def test_get_workspace_never_echoes_corrupt_record_contents(
    client, plan_dir, label
):
    """The hostile string itself must never come back out of the API."""
    _active_record(plan_dir).write_text(CORRUPT_RECORDS[label])

    body = client.get("/api/workspace").text

    assert "../escape" not in body
    assert "escape" not in body


@pytest.mark.parametrize("label", sorted(CORRUPT_RECORDS))
def test_save_with_corrupt_record_matches_no_workspace_behavior(
    client, plan_dir, label
):
    """A corrupt record must degrade to 'no workspace selected', not to an
    error and not to a stamped repo_root."""
    plan = {"epics": [{"summary": "E", "stories": [{"summary": "S"}]}]}

    control = client.post(
        "/api/plans/control/save", json={"plan_json": json.dumps(plan)}
    )
    assert control.status_code == 200
    control_plan = json.loads((plan_dir / "control.json").read_text())

    _active_record(plan_dir).write_text(CORRUPT_RECORDS[label])
    corrupt = client.post(
        "/api/plans/corrupt/save", json={"plan_json": json.dumps(plan)}
    )

    assert corrupt.status_code == control.status_code == 200
    assert json.loads((plan_dir / "corrupt.json").read_text()) == control_plan


@pytest.mark.parametrize("label", sorted(CORRUPT_RECORDS))
def test_save_with_corrupt_record_does_not_stamp_a_repo_root(
    client, plan_dir, label
):
    _active_record(plan_dir).write_text(CORRUPT_RECORDS[label])
    plan = {"epics": [{"summary": "E", "stories": [{"summary": "S"}]}]}

    res = client.post(
        "/api/plans/corrupt/save", json={"plan_json": json.dumps(plan)}
    )

    assert res.status_code == 200
    assert "repo_root" not in json.loads((plan_dir / "corrupt.json").read_text())


@pytest.mark.parametrize("label", sorted(CORRUPT_RECORDS))
def test_save_with_corrupt_record_never_echoes_the_record(
    client, plan_dir, label
):
    _active_record(plan_dir).write_text(CORRUPT_RECORDS[label])
    plan = {"epics": []}

    res = client.post(
        "/api/plans/corrupt/save", json={"plan_json": json.dumps(plan)}
    )

    assert res.status_code != 500
    assert "escape" not in res.text


# ---------------------------------------------------------------------------
# 3. Stale active workspace -- the fallback RE-VALIDATES rather than trusting
#    the stored string, so a workspace that has since been removed fails
#    closed instead of writing a plan against a path that no longer resolves.
# ---------------------------------------------------------------------------


@pytest.fixture
def stale_active_workspace(client, plan_dir, tmp_path):
    """Record a genuinely valid workspace, then delete the directory."""
    repo = _init_git_repo(tmp_path / "vanishing-repo")
    res = client.post("/api/workspace", json={"path": str(repo)})
    assert res.status_code == 200
    recorded = res.json()["path"]

    subprocess.run(["rm", "-rf", str(repo)], check=True)
    assert not repo.exists()
    return recorded


def test_save_with_stale_active_workspace_fails_closed_with_400(
    client, plan_dir, stale_active_workspace
):
    res = client.post(
        "/api/plans/stale/save",
        json={"plan_json": json.dumps({"epics": []})},
    )

    assert res.status_code == 400


def test_save_with_stale_active_workspace_does_not_write_the_plan(
    client, plan_dir, stale_active_workspace
):
    """Proves service.save_plan re-validated BEFORE touching disk."""
    res = client.post(
        "/api/plans/stale/save",
        json={"plan_json": json.dumps({"epics": []})},
    )

    assert res.status_code == 400
    assert not (plan_dir / "stale.json").exists()


def test_save_with_stale_active_workspace_does_not_leak_the_stale_path(
    client, plan_dir, stale_active_workspace
):
    res = client.post(
        "/api/plans/stale/save",
        json={"plan_json": json.dumps({"epics": []})},
    )

    detail = res.json()["detail"]
    assert detail in GENERIC_DETAILS
    _assert_no_structure_leak(detail, stale_active_workspace)


def test_stale_active_workspace_is_still_reported_by_get(
    client, plan_dir, stale_active_workspace
):
    """The record is stale, not malformed: it must still be reported so the
    operator can see WHAT is selected. Fail-closed happens at use time, not
    by silently forgetting the selection."""
    assert client.get("/api/workspace").json() == {
        "active": stale_active_workspace
    }


# ---------------------------------------------------------------------------
# 4. Error hygiene across the whole surface.
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_decompose_backend(monkeypatch):
    """Stub ONLY the LLM backend; workspace validation stays real.

    ``pipeline.service`` resolves ``_run_decompose`` through
    ``pipeline.server``, the seam established by
    tests/unit/test_dashboard_decompose_workspace.py.
    """
    plan = {"epics": [{"summary": "E", "stories": [{"summary": "S"}]}]}
    raw = "```json\n" + json.dumps(plan) + "\n```"
    monkeypatch.setattr(p, "_run_decompose_detailed", lambda request, **kwargs: (raw, None))


@pytest.mark.parametrize(
    "label",
    [
        "under_etc",
        "under_repo_root",
        "symlinked",
        "percent_encoded_traversal",
        "literal_traversal",
    ],
)
def test_save_route_rejects_hostile_workspace_with_clean_detail(
    client, plan_dir, tmp_path, label
):
    repo = _init_git_repo(tmp_path / "legit-repo")
    hostile = dict(_hostile_paths(tmp_path, repo))[label]

    res = client.post(
        "/api/plans/hostile/save",
        json={"plan_json": json.dumps({"epics": []}), "workspace": hostile},
    )

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail in GENERIC_DETAILS
    _assert_no_structure_leak(detail, tmp_path, repo, REPO_ROOT)
    assert not (plan_dir / "hostile.json").exists()


@pytest.mark.parametrize(
    "label",
    [
        "under_etc",
        "under_repo_root",
        "symlinked",
        "percent_encoded_traversal",
        "literal_traversal",
    ],
)
def test_decompose_route_rejects_hostile_workspace_with_clean_detail(
    client, plan_dir, tmp_path, label, _stub_decompose_backend
):
    repo = _init_git_repo(tmp_path / "legit-repo")
    hostile = dict(_hostile_paths(tmp_path, repo))[label]

    res = client.post(
        "/api/decompose", json={"request": "build a thing", "workspace": hostile}
    )

    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail in GENERIC_DETAILS
    _assert_no_structure_leak(detail, tmp_path, repo, REPO_ROOT)


def test_decompose_with_hostile_workspace_returns_no_plan(
    client, plan_dir, _stub_decompose_backend
):
    """Fail closed: a rejected workspace must not yield a stamped plan."""
    res = client.post(
        "/api/decompose",
        json={"request": "build a thing", "workspace": "/etc/ws-attacker"},
    )

    assert res.status_code == 400
    assert "plan" not in res.json()
    assert "repo_root" not in res.text


def test_no_response_body_on_the_surface_contains_a_stack_trace(
    client, plan_dir, tmp_path, _stub_decompose_backend
):
    """Sweep: every rejection across the surface stays trace-free."""
    repo = _init_git_repo(tmp_path / "legit-repo")
    hostile = "/etc/ws-attacker"

    bodies = [
        client.post("/api/workspace", json={"path": hostile}).text,
        client.post(
            "/api/plans/x/save",
            json={"plan_json": json.dumps({"epics": []}), "workspace": hostile},
        ).text,
        client.post(
            "/api/decompose",
            json={"request": "r", "workspace": hostile},
        ).text,
    ]

    for body in bodies:
        _assert_no_structure_leak(json.loads(body)["detail"], tmp_path, repo)


# ---------------------------------------------------------------------------
# 5. GET /api/workspace exposes the selection and nothing else.
# ---------------------------------------------------------------------------


def test_get_workspace_response_has_exactly_the_active_key(client, plan_dir):
    body = client.get("/api/workspace").json()

    assert set(body) == {"active"}


def test_get_workspace_response_keys_unchanged_when_a_workspace_is_set(
    client, plan_dir, tmp_path
):
    repo = _init_git_repo(tmp_path / "legit-repo")
    assert client.post("/api/workspace", json={"path": str(repo)}).status_code == 200

    body = client.get("/api/workspace").json()

    assert set(body) == {"active"}


def test_get_workspace_echoes_only_the_stored_selection_string(
    client, plan_dir, tmp_path
):
    """No sibling filesystem structure (existence, git status, contents,
    recents) may ride along -- data minimization."""
    repo = _init_git_repo(tmp_path / "legit-repo")
    stored = client.post("/api/workspace", json={"path": str(repo)}).json()["path"]

    body = client.get("/api/workspace").json()

    assert body["active"] == stored
    assert isinstance(body["active"], str)
    for leaky in ("exists", "valid", "recent", "recents", "workspaces", "git"):
        assert leaky not in body


def test_get_workspace_does_not_expose_the_recents_list(client, plan_dir, tmp_path):
    """Two accepted workspaces build a recents list; GET must show only the
    active one, never the history."""
    first = _init_git_repo(tmp_path / "repo-one")
    second = _init_git_repo(tmp_path / "repo-two")
    client.post("/api/workspace", json={"path": str(first)})
    latest = client.post("/api/workspace", json={"path": str(second)}).json()["path"]

    body = client.get("/api/workspace").json()

    assert body == {"active": latest}
    assert str(first) not in json.dumps(body)


# ---------------------------------------------------------------------------
# Fail-closed wiring: an unexpected error in the fallback must SURFACE, not
# be swallowed into "proceed without a workspace" (which would let a plan
# reach disk carrying a model-authored, unvalidated repo_root).
# ---------------------------------------------------------------------------


def test_save_surfaces_an_exploding_active_workspace_lookup(
    client, plan_dir, monkeypatch
):
    def boom():
        raise RuntimeError("active workspace lookup exploded")

    monkeypatch.setattr(d._service, "get_active_workspace", boom)

    with pytest.raises(RuntimeError, match="active workspace lookup exploded"):
        client.post(
            "/api/plans/boom/save",
            json={"plan_json": json.dumps({"epics": []})},
        )

    assert not (plan_dir / "boom.json").exists()


def test_decompose_surfaces_an_exploding_active_workspace_lookup(
    client, plan_dir, monkeypatch, _stub_decompose_backend
):
    def boom():
        raise RuntimeError("active workspace lookup exploded")

    monkeypatch.setattr(d._service, "get_active_workspace", boom)

    with pytest.raises(RuntimeError, match="active workspace lookup exploded"):
        client.post("/api/decompose", json={"request": "build a thing"})


def test_set_workspace_records_only_the_validated_resolved_path(
    client, plan_dir, tmp_path
):
    """The active record must hold ``resolve_workspace``'s resolved output,
    never the caller's raw spelling.

    A ``/./`` spelling is legitimately accepted (``PurePath`` drops the
    no-op ``.`` component, unlike ``..`` which is rejected outright), so it
    is the sharpest available probe: an accepted-but-unnormalized spelling
    must still be stored in resolved form, so nothing that later reads the
    record has to re-derive what the operator meant.
    """
    repo = _init_git_repo(tmp_path / "legit-repo")
    spelling = f"{tmp_path}/./legit-repo"

    res = client.post("/api/workspace", json={"path": spelling})
    assert res.status_code == 200

    on_disk = json.loads(_active_record(plan_dir).read_text())
    assert on_disk["path"] == os.path.realpath(str(repo))
    assert on_disk["path"] != spelling
