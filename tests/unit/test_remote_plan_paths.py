"""SA-03: refuse server paths supplied by a remote plan author.

Two ways a caller can point the server at an arbitrary directory, both closed
here:

* ``PipelineService.save_plan`` accepts a model-authored ``repo_root`` and a
  server-side ``workspace`` argument.  A remote principal must name
  ``repo_url`` instead (wired by a later story), so both are refused before
  anything is written.
* ``pipeline.workspace.validate_workspace`` used to accept any git repo on the
  host that missed a sensitive-location deny list.  It now also enforces the
  opt-in ``PIPELINE_ALLOWED_REPO_ROOTS`` allowlist, which stays a no-op when
  the variable is unset or blank.

These are regression guards: the refusal must happen *before* any filesystem
mutation, and the allowlist must not change local behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.identity import LOCAL_OPERATOR, Principal
from pipeline import actor, remote_scope, service
from pipeline.service import PipelineService
from pipeline.workspace import ALLOWED_REPO_ROOTS_ENV, validate_workspace

OIDC_PRINCIPAL = Principal(subject="remote-alice", auth_method="oidc")

REFUSAL = "a remote plan must name repo_url, not repo_root or a workspace"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect the service's PLAN_DIR into tmp_path."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(service, "PLAN_DIR", d)
    return d


def _make_repo(path: Path) -> Path:
    """Create a git repo at *path* with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "--allow-empty",
            "-qm",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    return path


def _plan(**overrides) -> dict:
    plan = {
        "name": "p1",
        "epics": [{"stories": [{}, {}]}, {"stories": [{}]}],
    }
    plan.update(overrides)
    return plan


# --- helper ---------------------------------------------------------------


def _helper():
    helper = getattr(remote_scope, "remote_plan_path_error", None)
    assert helper is not None, (
        "pipeline.remote_scope must expose remote_plan_path_error"
    )
    return helper


@pytest.mark.parametrize(
    "plan,workspace",
    [
        ({"repo_root": "/srv/repo"}, None),
        ({"repo_root": ""}, None),
        ({"repo_root": None}, None),
        ({}, ""),
        ({}, "/srv/repo"),
    ],
)
def test_remote_plan_path_error_reports_server_paths(plan, workspace):
    assert _helper()(plan, workspace) == REFUSAL


def test_remote_plan_path_error_allows_repo_url_only_plan():
    assert _helper()({"repo_url": "https://example.invalid/r.git"}, None) is None


def test_remote_plan_path_error_allows_plan_without_server_paths():
    assert _helper()({"name": "p1"}, None) is None


# --- save_plan: remote principals ----------------------------------------


def test_remote_plan_with_repo_root_is_refused_and_writes_nothing(plan_dir):
    plan = _plan(repo_root="/srv/remote/repo")
    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan("p1", json.dumps(plan))
    assert result == {"ok": False, "error": REFUSAL}
    assert not (plan_dir / "p1.json").exists()


def test_remote_plan_with_empty_repo_root_is_refused(plan_dir):
    plan = _plan(repo_root="")
    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan("p1", json.dumps(plan))
    assert result == {"ok": False, "error": REFUSAL}
    assert not (plan_dir / "p1.json").exists()


def test_remote_plan_with_workspace_is_refused(plan_dir, tmp_path):
    repo = _make_repo(tmp_path / "ws")
    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan(
            "p1", json.dumps(_plan()), workspace=str(repo)
        )
    assert result == {"ok": False, "error": REFUSAL}
    assert not (plan_dir / "p1.json").exists()


def test_remote_plan_with_neither_server_path_is_saved(plan_dir):
    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan("p1", json.dumps(_plan()))
    assert result.get("ok") is True
    assert (plan_dir / "p1.json").exists()


def test_local_operator_with_allowed_repo_root_is_saved(plan_dir, tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = _make_repo(allowed / "repo")
    monkeypatch.setenv(ALLOWED_REPO_ROOTS_ENV, str(allowed))
    with actor.bind_principal(LOCAL_OPERATOR):
        result = PipelineService().save_plan(
            "p1", json.dumps(_plan(repo_root=str(repo)))
        )
    assert result.get("ok") is True
    assert (plan_dir / "p1.json").exists()


# --- validate_workspace: allowlist enforcement ---------------------------


def test_validate_workspace_rejects_repo_outside_allowed_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    other = _make_repo(tmp_path / "other")
    monkeypatch.setenv(ALLOWED_REPO_ROOTS_ENV, str(allowed))

    result = validate_workspace(str(other))

    assert result.get("ok") is False
    assert result.get("error")
    assert str(other) not in (result.get("error") or "")


def test_validate_workspace_accepts_repo_inside_allowed_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = _make_repo(allowed / "repo")
    monkeypatch.setenv(ALLOWED_REPO_ROOTS_ENV, str(allowed))

    assert validate_workspace(str(repo)).get("ok") is True


def test_validate_workspace_unset_allowlist_allows_outside_repo(tmp_path, monkeypatch):
    monkeypatch.delenv(ALLOWED_REPO_ROOTS_ENV, raising=False)
    other = _make_repo(tmp_path / "other")

    assert validate_workspace(str(other)).get("ok") is True


# --- refusal leaves no blocking state ------------------------------------


def test_refused_remote_save_leaves_no_partial_file_and_local_save_succeeds(
    plan_dir, tmp_path, monkeypatch
):
    """A refusal must not write or lock anything: the same name saves locally."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    repo = _make_repo(allowed / "repo")
    monkeypatch.setenv(ALLOWED_REPO_ROOTS_ENV, str(allowed))

    with actor.bind_principal(OIDC_PRINCIPAL):
        refused = PipelineService().save_plan(
            "p1", json.dumps(_plan(repo_root=str(repo)))
        )
    assert refused == {"ok": False, "error": REFUSAL}
    assert not (plan_dir / "p1.json").exists()

    with actor.bind_principal(LOCAL_OPERATOR):
        saved = PipelineService().save_plan("p1", json.dumps(_plan(repo_root=str(repo))))
    assert saved.get("ok") is True
    assert json.loads((plan_dir / "p1.json").read_text())["repo_root"] == str(repo)

