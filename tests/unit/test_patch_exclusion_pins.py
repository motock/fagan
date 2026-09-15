"""Exclusion pins for the worktree-patch apply surface (WAP-12).

This file grades two NEGATIVE contracts that the patch epic must keep:

PIN 1 -- plan-lock serialization.
    ``pipeline.worktree_patch.apply_patch`` runs its WHOLE body inside
    ``pipeline.concurrency._plan_lock(plan_name)``.  Consequences, both
    graded here:

    * while an apply is in flight (blocked inside the lock), a real
      ``advance_pipeline`` tick for the same plan must observe the lock and
      return the documented skip shape -- ``{"ok": True, "skipped":
      "locked", "reason": ...}`` (see ``_plan_lock``'s docstring and
      ``PipelineService.advance_pipeline``) -- and must NOT dispatch or
      redispatch the story (the manifest is untouched: no new ``pid``, no
      ``dispatched_at``);
    * the inverse: when the lock is already held by someone else,
      ``apply_patch`` DEFERS -- it returns ``{"ok": False, "error": "plan
      busy", "status_code": 409}`` immediately.  It never waits for the
      lock and never interleaves a ``git apply`` with the holder.

PIN 2 -- no service/overlord apply path under full autonomy.
    The apply surface exists ONLY at the HTTP layer (the dashboard route)
    and in the chat registry only as ``propose_patch``.  Nothing in the
    service/overlord layer may reach an apply:

    (a) STATIC: ``pipeline/server.py``, ``pipeline/dispatch.py``,
        ``pipeline/triage.py`` and ``pipeline/overlord.py`` contain no
        reference to ``worktree_patch`` / ``apply_patch`` /
        ``/api/worktree/patch/``.  This is the guard that the exclusion
        cannot be silently widened: wiring an apply path into the service
        layer turns this test red.
    (b) BEHAVIORAL: with ``PIPELINE_AUTONOMY=full``, one real tick over a
        plan whose stuck story has a PENDING patch record completes without
        error, leaves the record ``pending``, leaves the worktree
        byte-identical, and never reaches ``git apply``.
    (c) ROUTE: ``propose_patch`` is in ``app.chat.TOOLS`` and no TOOLS entry
        can apply; the apply route is registered on ``app.dashboard.app``
        but never in the chat registry.

Scope note: assertions are kept to the pinned contracts.  ``TOOLS`` is
checked by MEMBERSHIP (never by exact enumeration) so a later story can add
tools without breaking this file, and no file is byte-hashed.

Hermeticity: the git fixture is a real repository under pytest's
``tmp_path``, resolved first, because on macOS the raw temp path can run
through ``/var -> /private/var`` and a symlinked root component would make
the write resolver reject every path for the wrong reason.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import inspect
import json
import subprocess
import threading
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import worktree_patch
from pipeline.server import PipelineService
from pipeline.worktree_patch import (
    apply_patch,
    create_patch_record,
    get_patch_record,
)

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PLAN_NAME = "wap12plan"
STORY_KEY = "WAP-12"

#: The story is STUCK (``failed``): the apply engine's stuck-only gate admits
#: it, and the scheduler's ready list (``todo``/``interrupted``/
#: ``changes_requested``) does not, so a tick can never dispatch it.
STUCK_STATUS = "failed"

#: Captured at import time so a test's own git plumbing never goes through a
#: monkeypatched ``_run_git_apply``.
_REAL_RUN = subprocess.run

#: The service/overlord modules that must never grow an apply path.
_SERVICE_LAYER_FILES = (
    "pipeline/server.py",
    "pipeline/dispatch.py",
    "pipeline/triage.py",
    "pipeline/overlord.py",
)

#: Tokens that would mean an apply path leaked into the service layer.
_FORBIDDEN_SERVICE_TOKENS = (
    "worktree_patch",
    "apply_patch",
    "/api/worktree/patch/",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git command in *repo* (never through a monkeypatched seam)."""
    return _REAL_RUN(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _unified(rel_path: str, old_text: str, new_text: str) -> str:
    """A plain unified diff (no ``diff --git``/``index`` headers).

    ``parse_unified_diff`` recognizes only ``--- ``/``+++ ``/``@@`` blocks, so
    the diff is built without git's extra headers; ``git apply`` accepts this
    format natively.
    """
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )


def _write_manifest(plan_dir: Path, stories: dict, plan_name: str = PLAN_NAME) -> Path:
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps({"stories": stories}))
    return path


def _manifest_path(plan_dir: Path, plan_name: str = PLAN_NAME) -> Path:
    return plan_dir / f"{plan_name}.manifest.json"


def _patch_store() -> dict:
    """The module-level patch-record store dict.

    The brief names it ``_PATCH_STORE``; the fallback discovery keeps this
    file working if a later refactor renames the private attribute.
    """
    store = getattr(worktree_patch, "_PATCH_STORE", None)
    if isinstance(store, dict):
        return store
    for name, value in vars(worktree_patch).items():
        if name.startswith("__") or not isinstance(value, dict):
            continue
        if any(isinstance(v, dict) and "patch_id" in v for v in value.values()):
            return value
    raise AssertionError("pipeline.worktree_patch exposes no patch-record store")


def _record(diff_text: str, paths: list[str], added_lines: int = 1) -> dict:
    return create_patch_record(PLAN_NAME, STORY_KEY, diff_text, paths, added_lines)


def _worktree_digest(root: Path) -> dict[str, str]:
    """sha256 of every tracked file in the worktree, keyed by relative path.

    ``.git`` internals are excluded: the pin is that the WORKING TREE is
    byte-identical, and git's own bookkeeping is not part of that contract.
    """
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        digest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def _advance_tick(plan_name: str) -> dict:
    """One real orchestration tick, entered exactly as the dashboard does.

    ``app/dashboard.py`` does ``from pipeline.server import PipelineService``
    and calls ``_service.advance_pipeline(plan_name)``; this mirrors that
    import path so the tick under test is the production one.
    """
    return PipelineService().advance_pipeline(plan_name)


def _code_only(path: Path) -> str:
    """Source text with comments and docstrings removed.

    Comments and docstrings are documentation, not wiring.  The pin is that
    no service/overlord module *references* the apply surface in code: an
    import (``ast.alias``), a name/attribute access (``ast.Name`` /
    ``ast.Attribute``), or a string literal such as the route path
    (``ast.Constant``).  String literals are deliberately KEPT -- a route
    path spelled as a literal is exactly the wiring this pin must catch.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None) or []
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))

    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
        elif isinstance(node, ast.alias):
            parts.append(node.name)
            if node.asname:
                parts.append(node.asname)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            parts.append(node.value)
    return "\n".join(parts)


class _BlockingGitApply:
    """A ``_run_git_apply`` stand-in that parks inside the plan lock.

    The first call sets ``entered`` and blocks on ``release``; once released
    it delegates to the REAL ``_run_git_apply`` so the apply genuinely
    completes.  Because ``apply_patch`` calls this only after acquiring the
    plan lock, ``entered`` being set proves the lock is held.
    """

    def __init__(self, real, entered: threading.Event, release: threading.Event):
        self._real = real
        self.entered = entered
        self.release = release
        self.calls: list[dict] = []

    def __call__(self, worktree, diff_text, check_only):
        self.calls.append({"worktree": worktree, "check_only": check_only})
        self.entered.set()
        if not self.release.wait(timeout=30):
            raise AssertionError("test never released the blocked git apply")
        return self._real(worktree, diff_text, check_only)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_patch_store():
    """Snapshot/restore the in-process patch store around every test."""
    store = _patch_store()
    snapshot = dict(store)
    yield
    store.clear()
    store.update(snapshot)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """PLAN_DIR for every binding the tick and the apply engine read.

    ``pipeline.concurrency.PLAN_DIR`` is where ``_plan_lock`` puts the
    ``<plan>.lock`` file, ``pipeline.server.PLAN_DIR`` is where the manifest
    is read, and ``pipeline.persistence.PLAN_DIR`` is where the journal
    lands.  All three must point at the same tmp directory or the lock and
    the manifest would live in different worlds.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, committed git repository to act as the story worktree."""
    root = (tmp_path / "worktree").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test Author")
    (root / "app.py").write_text("alpha\nbeta\ngamma\n")
    (root / "notes.txt").write_text("one\ntwo\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def manifest(plan_dir: Path, repo: Path) -> dict:
    """A manifest whose one story is STUCK with a real worktree."""
    stories = {STORY_KEY: {"status": STUCK_STATUS, "worktree": str(repo)}}
    _write_manifest(plan_dir, stories)
    return stories


@pytest.fixture
def pending_patch(repo: Path) -> dict:
    """A pending patch record that would rewrite ``app.py``."""
    diff = _unified("app.py", (repo / "app.py").read_text(), "alpha\nBETA\ngamma\n")
    return _record(diff, ["app.py"])


# --------------------------------------------------------------------------
# PIN 1 -- the apply engine acquires the plan lock
# --------------------------------------------------------------------------


def test_apply_patch_acquires_the_plan_lock():
    """Static half of PIN 1: the engine's body is wrapped in ``_plan_lock``."""
    source = inspect.getsource(worktree_patch.apply_patch)
    assert "_plan_lock" in source, (
        "apply_patch must run its whole body inside "
        "pipeline.concurrency._plan_lock(plan_name)"
    )


def test_tick_skips_locked_while_an_apply_holds_the_plan_lock(
    plan_dir, repo, manifest, pending_patch, monkeypatch
):
    """A tick that lands mid-apply must skip, not dispatch or redispatch.

    The apply is parked inside ``_run_git_apply`` -- i.e. inside the plan
    lock -- while the real advance path for the same plan is invoked.  The
    tick must return the documented skip shape and leave the manifest
    untouched.
    """
    real = worktree_patch._run_git_apply
    entered = threading.Event()
    release = threading.Event()
    fake = _BlockingGitApply(real, entered, release)
    monkeypatch.setattr(worktree_patch, "_run_git_apply", fake)

    outcome: dict = {}

    def _worker():
        try:
            outcome["result"] = apply_patch(
                PLAN_NAME,
                STORY_KEY,
                pending_patch["patch_id"],
                pending_patch["confirmation_token"],
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
            outcome["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=30), "apply_patch never reached git apply"
        assert fake.calls, "the blocking git-apply seam was never called"

        manifest_path = _manifest_path(plan_dir)
        before = manifest_path.read_bytes()

        # The real advance path, entered exactly as the dashboard does.
        tick = _advance_tick(PLAN_NAME)
        assert tick.get("ok") is True, tick
        assert tick.get("skipped") == "locked", tick
        assert isinstance(tick.get("reason"), str) and tick["reason"], tick

        # The module-level MCP tool wrapper must agree (same lock, same shape).
        tool_tick = p.advance_pipeline(PLAN_NAME)
        assert tool_tick.get("ok") is True, tool_tick
        assert tool_tick.get("skipped") == "locked", tool_tick

        # No dispatch / redispatch: the manifest is byte-identical and the
        # story gained neither a pid nor a dispatched_at stamp.
        assert manifest_path.read_bytes() == before, (
            "a tick that observed the plan lock must not mutate the manifest"
        )
        story = json.loads(before)["stories"][STORY_KEY]
        assert "pid" not in story, story
        assert "dispatched_at" not in story, story
    finally:
        release.set()
        thread.join(timeout=30)

    assert not thread.is_alive(), "the apply thread never finished"
    assert "error" not in outcome, outcome
    result = outcome["result"]
    assert result["ok"] is True, result
    assert sorted(result["applied"]) == ["app.py"], result
    assert (repo / "app.py").read_text() == "alpha\nBETA\ngamma\n"
    assert get_patch_record(pending_patch["patch_id"])["status"] == "applied"


def test_apply_defers_when_the_plan_lock_is_held_elsewhere(
    plan_dir, repo, manifest, pending_patch, monkeypatch
):
    """Inverse of PIN 1: apply DEFERS on a busy plan -- it never waits.

    A helper thread holds ``_plan_lock`` for the plan.  ``apply_patch`` must
    return the documented refusal immediately, must not reach ``git apply``,
    and must leave both the record and the worktree untouched.
    """
    calls: list[bool] = []

    def _spy(worktree, diff_text, check_only):
        calls.append(check_only)
        raise AssertionError(
            "apply_patch must not reach git apply while the plan lock is held"
        )

    monkeypatch.setattr(worktree_patch, "_run_git_apply", _spy)

    acquired = threading.Event()
    release = threading.Event()
    holder: dict = {}

    def _hold():
        with pcon._plan_lock(PLAN_NAME) as got:
            holder["acquired"] = got
            acquired.set()
            release.wait(timeout=30)

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    try:
        assert acquired.wait(timeout=30), "the helper thread never took the lock"
        assert holder.get("acquired") is True, holder

        before = _worktree_digest(repo)
        result = apply_patch(
            PLAN_NAME,
            STORY_KEY,
            pending_patch["patch_id"],
            pending_patch["confirmation_token"],
        )

        assert result["ok"] is False, result
        assert result["error"] == "plan busy", result
        assert result["status_code"] == 409, result
        assert calls == [], "apply_patch interleaved a git apply with the lock holder"
        assert get_patch_record(pending_patch["patch_id"])["status"] == "pending"
        assert _worktree_digest(repo) == before
    finally:
        release.set()
        thread.join(timeout=30)

    assert not thread.is_alive()


# --------------------------------------------------------------------------
# PIN 2(a) -- STATIC: no apply path in the service/overlord layer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", _SERVICE_LAYER_FILES)
def test_service_layer_never_references_the_apply_surface(rel_path):
    """The exclusion cannot be silently widened.

    Any import, attribute access or string literal naming the patch apply
    surface in the service/overlord layer turns this red.
    """
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"{rel_path} is missing from the repository"
    code = _code_only(path)
    for token in _FORBIDDEN_SERVICE_TOKENS:
        assert token not in code, (
            f"{rel_path} references {token!r}: the patch apply surface must "
            "stay out of the service/overlord layer"
        )


# --------------------------------------------------------------------------
# PIN 2(b) -- BEHAVIORAL: a full-autonomy tick never applies
# --------------------------------------------------------------------------


def test_full_autonomy_tick_never_applies_a_pending_patch(
    plan_dir, repo, manifest, pending_patch, monkeypatch
):
    """There is no code path from a tick to an apply.

    With ``PIPELINE_AUTONOMY=full`` the tick runs to completion, the pending
    record is still pending, the worktree is byte-identical, and ``git
    apply`` was never reached.
    """
    monkeypatch.setenv("PIPELINE_AUTONOMY", "full")
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "full")
    # Hermeticity: the failure-triage sweep is a separate concern and would
    # otherwise make model calls when an operator has opted in via the env.
    monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)

    calls: list[bool] = []

    def _spy(worktree, diff_text, check_only):
        calls.append(check_only)
        raise AssertionError("no service-layer tick may reach git apply")

    monkeypatch.setattr(worktree_patch, "_run_git_apply", _spy)

    before = _worktree_digest(repo)
    result = _advance_tick(PLAN_NAME)

    assert result.get("ok") is True, result
    assert calls == [], "the tick reached git apply"
    assert get_patch_record(pending_patch["patch_id"])["status"] == "pending", (
        "nothing in a tick may consume a pending patch record"
    )
    assert _worktree_digest(repo) == before, "the tick modified the worktree"

    story = json.loads(_manifest_path(plan_dir).read_text())["stories"][STORY_KEY]
    assert "pid" not in story, story
    assert "dispatched_at" not in story, story


# --------------------------------------------------------------------------
# PIN 2(c) -- ROUTE: the apply surface is HTTP-only
# --------------------------------------------------------------------------


def test_apply_surface_is_registered_on_the_dashboard_but_not_in_chat():
    """The apply route exists at the HTTP layer and nowhere in the chat registry."""
    from app import chat, dashboard

    # Membership, never exact enumeration: a later story may add tools.
    assert "propose_patch" in chat.TOOLS
    assert not any("apply" in name for name in chat.TOOLS), sorted(chat.TOOLS)

    routes = {
        getattr(route, "path", None): set(getattr(route, "methods", None) or set())
        for route in dashboard.app.routes
    }
    apply_path = "/api/worktree/patch/{patch_id}/apply"
    assert apply_path in routes, sorted(p for p in routes if p)
    assert "POST" in routes[apply_path], routes[apply_path]

    # The chat registry must not carry the apply surface: the propose path is
    # wired there (the anchor proving the scan works), the apply path is not.
    chat_source = Path(chat.__file__).read_text(encoding="utf-8")
    assert "/api/worktree/patch/propose" in chat_source
    assert apply_path not in chat_source
