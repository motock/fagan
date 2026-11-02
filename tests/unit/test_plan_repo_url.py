"""SA-05: a remote plan names ``repo_url``; the server clones it.

SA-03 refuses ``repo_root`` and ``workspace`` from a network caller, leaving it
one way to name code: ``repo_url``. This story wires ``pipeline/repo_clone.py``
(SA-04) into the plan flow, so the server clones the repository into its
managed volume and points the plan's ``repo_root`` at that clone:

* ``remote_scope.resolve_repo_url`` owns the rule (one of the two fields, a
  non-empty string URL, and the clone/error mapping);
* ``PipelineService.save_plan`` applies it for *any* principal -- a local
  operator may name ``repo_url`` too -- before the workspace/allow-list block,
  so the existing ``check_repo_root_allowed`` sees the managed clone path;
* ``PipelineService.ingest_plan`` refreshes the clone from the saved plan so an
  ingest never reads a stale checkout, and leaves the plan file untouched.

The clone is exercised for real against a bare repo under ``tmp_path`` reached
through a ``file://`` URL, which is why the test-only
``PIPELINE_REPO_URL_ALLOW_FILE=1`` escape hatch is set.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.service/ingest (import-cycle note in
# tests/unit/test_ingest_keyless_reingest.py).
import pipeline.server as p
from app.identity import LOCAL_OPERATOR, Principal
from pipeline import actor, remote_scope, repo_clone, service
from pipeline.service import PipelineService
from pipeline.workspace import ALLOWED_REPO_ROOTS_ENV

OIDC_PRINCIPAL = Principal(subject="remote-alice", auth_method="oidc")

BOTH_ERROR = "a plan names repo_url or repo_root, not both"
REMOTE_PATH_ERROR = "a remote plan must name repo_url, not repo_root or a workspace"

_GIT_IDENTITY = ("-c", "user.name=SA-05", "-c", "user.email=sa05@example.invalid")
_GITHUB_URL = "https://github.com/acme/widget.git"
_DISALLOWED_URL = "https://evil.example.com/acme/widget.git"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture()
def bare_repo(tmp_path: Path) -> str:
    """A ``file://`` URL for a bare repo with one commit on ``main``."""
    bare = tmp_path / "origin" / "acme" / "widget.git"
    bare.parent.mkdir(parents=True)
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "seed-work"
    _git("clone", str(bare), str(work), cwd=tmp_path)
    (work / "README.md").write_text("seed\n")
    _git(*_GIT_IDENTITY, "add", "README.md", cwd=work)
    _git(*_GIT_IDENTITY, "commit", "-m", "seed", cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return f"file://{bare}"


@pytest.fixture()
def repos_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The managed clone root, its allow-list, and the test-only file:// hatch."""
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    monkeypatch.setenv("PIPELINE_REPO_URL_ALLOW_FILE", "1")
    monkeypatch.setenv("PIPELINE_REPO_URL_HOSTS", "github.com")
    # A standalone deployment points the plan allow-list at the same volume.
    monkeypatch.setenv(ALLOWED_REPO_ROOTS_ENV, str(root))
    return root


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every ``PLAN_DIR`` the service reads into ``tmp_path``."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(service, "PLAN_DIR", d)
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _plan(**overrides: object) -> dict:
    plan: dict = {
        "name": "p1",
        "epics": [{"stories": [{}, {}]}, {"stories": [{}]}],
    }
    plan.update(overrides)
    return plan


def _resolve():
    """Return ``remote_scope.resolve_repo_url``, failing loudly if absent."""
    helper = getattr(remote_scope, "resolve_repo_url", None)
    assert helper is not None, "pipeline.remote_scope must expose resolve_repo_url"
    return helper


def _assert_is_clone(path: Path) -> None:
    assert (path / ".git").exists(), f"{path} is not a git clone"
    assert _git("rev-parse", "--is-inside-work-tree", cwd=path).stdout.strip() == "true"


# --- resolve_repo_url ------------------------------------------------------


def test_resolve_repo_url_points_repo_root_at_the_managed_clone(
    repos_root: Path, bare_repo: str
) -> None:
    plan = _plan(repo_url=bare_repo)

    assert _resolve()(plan) is None

    managed = repo_clone.managed_path(bare_repo)
    assert plan["repo_root"] == str(managed)
    _assert_is_clone(managed)


def test_resolve_repo_url_without_repo_url_is_a_no_op(repos_root: Path) -> None:
    plan = _plan()

    assert _resolve()(plan) is None
    assert "repo_root" not in plan


def test_resolve_repo_url_refuses_both_fields(repos_root: Path) -> None:
    plan = _plan(repo_url=_GITHUB_URL, repo_root="/srv/other")

    assert _resolve()(plan) == {"ok": False, "error": BOTH_ERROR}
    assert plan["repo_root"] == "/srv/other"
    assert not (repos_root / "github.com").exists()


@pytest.mark.parametrize("url", ["", 42, None, ["https://github.com/acme/widget"]])
def test_resolve_repo_url_refuses_a_non_string_or_empty_url(
    repos_root: Path, url: object
) -> None:
    plan = _plan(repo_url=url)

    result = _resolve()(plan)

    assert result is not None
    assert result["ok"] is False
    assert result["error"]


def test_resolve_repo_url_maps_a_clone_error_to_a_refusal(
    repos_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(url: str, *args: object, **kwargs: object) -> Path:
        raise repo_clone.RepoCloneError(f"git clone failed for {url}")

    monkeypatch.setattr(repo_clone, "ensure_clone", _boom)
    plan = _plan(repo_url=_GITHUB_URL)

    result = _resolve()(plan)

    assert result == {"ok": False, "error": f"git clone failed for {_GITHUB_URL}"}
    assert "repo_root" not in plan


def test_resolve_repo_url_maps_a_url_error_to_a_refusal(repos_root: Path) -> None:
    plan = _plan(repo_url=_DISALLOWED_URL)

    result = _resolve()(plan)

    assert result is not None
    assert result["ok"] is False
    assert "evil.example.com" in result["error"]
    assert "repo_root" not in plan


# --- save_plan ------------------------------------------------------------


def test_remote_save_plan_with_repo_url_saves_the_managed_clone(
    plan_dir: Path, repos_root: Path, bare_repo: str
) -> None:
    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan("p1", json.dumps(_plan(repo_url=bare_repo)))

    assert result.get("ok") is True, result
    saved = json.loads((plan_dir / "p1.json").read_text())
    managed = repo_clone.managed_path(bare_repo)
    assert saved["repo_root"] == str(managed)
    _assert_is_clone(managed)


def test_local_plan_naming_both_repo_url_and_repo_root_is_refused(
    plan_dir: Path, repos_root: Path, tmp_path: Path
) -> None:
    """The "not both" rule applies to the local operator too, and writes nothing."""
    plan = _plan(repo_url=_GITHUB_URL, repo_root=str(tmp_path / "elsewhere"))

    with actor.bind_principal(LOCAL_OPERATOR):
        result = PipelineService().save_plan("p1", json.dumps(plan))

    assert result == {"ok": False, "error": BOTH_ERROR}
    assert not (plan_dir / "p1.json").exists()


@pytest.mark.parametrize("url", ["", 42])
def test_remote_save_plan_with_an_unusable_repo_url_is_refused(
    plan_dir: Path, repos_root: Path, url: object
) -> None:
    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan("p1", json.dumps(_plan(repo_url=url)))

    assert result["ok"] is False
    assert result["error"]
    assert not (plan_dir / "p1.json").exists()


def test_remote_save_plan_with_a_disallowed_host_is_refused(
    plan_dir: Path, repos_root: Path
) -> None:
    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan(
            "p1", json.dumps(_plan(repo_url=_DISALLOWED_URL))
        )

    assert result["ok"] is False
    assert result["error"]
    assert not (plan_dir / "p1.json").exists()
    assert not (repos_root / "evil.example.com").exists()


def test_remote_save_plan_with_workspace_is_still_refused_before_any_clone(
    plan_dir: Path, repos_root: Path, bare_repo: str, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git("init", "-q", str(ws), cwd=tmp_path)

    with actor.bind_principal(OIDC_PRINCIPAL):
        result = PipelineService().save_plan(
            "p1", json.dumps(_plan(repo_url=bare_repo)), workspace=str(ws)
        )

    assert result == {"ok": False, "error": REMOTE_PATH_ERROR}
    assert not (repos_root / "local").exists()
    assert not (plan_dir / "p1.json").exists()


# --- ingest_plan ----------------------------------------------------------


class _EnsureRecorder:
    """Records ``ensure_clone`` calls and returns a fixed managed path."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, *args: object, **kwargs: object) -> Path:
        self.calls.append(url)
        return Path("/nonexistent/managed/clone")


@pytest.fixture()
def ingest_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the real ingest and the wake write-back; return the names ingested."""
    calls: list[str] = []

    def _fake_ingest(name, only_epics=None, overwrite=False):
        calls.append(name)
        return {"ok": True, "path": f"/plans/{name}.json"}

    monkeypatch.setattr(service, "_ingest_plan_impl", _fake_ingest)
    monkeypatch.setattr(
        service.wake, "after", lambda name, reason, result: result
    )
    return calls


def test_ingest_plan_refreshes_the_clone_before_ingesting(
    plan_dir: Path, repos_root: Path, monkeypatch: pytest.MonkeyPatch, ingest_calls
) -> None:
    recorder = _EnsureRecorder()
    monkeypatch.setattr(repo_clone, "ensure_clone", recorder)
    (plan_dir / "p1.json").write_text(json.dumps(_plan(repo_url=_GITHUB_URL)))

    result = PipelineService().ingest_plan("p1")

    assert result.get("ok") is True, result
    assert recorder.calls == [_GITHUB_URL]
    assert ingest_calls == ["p1"]
    # The refresh must not rewrite the saved plan.
    saved = json.loads((plan_dir / "p1.json").read_text())
    assert saved["repo_url"] == _GITHUB_URL
    assert "repo_root" not in saved


def test_ingest_plan_refuses_when_the_refresh_fails(
    plan_dir: Path, repos_root: Path, monkeypatch: pytest.MonkeyPatch, ingest_calls
) -> None:
    def _boom(url: str, *args: object, **kwargs: object) -> Path:
        raise repo_clone.RepoCloneError(f"git fetch failed for {url}")

    monkeypatch.setattr(repo_clone, "ensure_clone", _boom)
    (plan_dir / "p1.json").write_text(json.dumps(_plan(repo_url=_GITHUB_URL)))

    result = PipelineService().ingest_plan("p1")

    assert result == {"ok": False, "error": f"git fetch failed for {_GITHUB_URL}"}
    assert ingest_calls == []


def test_ingest_plan_without_repo_url_does_not_clone(
    plan_dir: Path, repos_root: Path, monkeypatch: pytest.MonkeyPatch, ingest_calls
) -> None:
    recorder = _EnsureRecorder()
    monkeypatch.setattr(repo_clone, "ensure_clone", recorder)
    (plan_dir / "p1.json").write_text(json.dumps(_plan(repo_root="/srv/repo")))

    result = PipelineService().ingest_plan("p1")

    assert result.get("ok") is True, result
    assert recorder.calls == []
    assert ingest_calls == ["p1"]
