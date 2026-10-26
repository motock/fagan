"""Plan approval is enforced at activation, not only in the tick (ENT-LC-04).

``pipeline/plan_approval.py`` records an opt-in ``approval`` block on the
manifest. ENT-LC-01 wired the record and the ingest-time digest reset; this
story makes the *activation* paths honour it:

* ``pipeline.advance._advance_pipeline_locked_impl`` must not dispatch,
  review or merge a plan whose approval is not current - it returns
  ``{"ok": True, "skipped": "plan_not_approved"}`` immediately after the
  pause check and before any resource gate.
* ``pipeline.dispatch._dispatch_story_impl`` must refuse a direct call that
  bypasses the tick with ``{"ok": False, "error": "plan is not approved",
  "plan": <plan>}`` before any worktree or agent work.

With ``PIPELINE_PLAN_APPROVAL`` unset both paths are byte-identical to
today: a manifest that *looks* pending still advances and dispatches.

The digest is what ties an approval to the content it approved. A record
whose ``digest`` no longer matches ``canonical_digest(manifest)`` (a writer
that bypassed ingest, e.g. ``set_plan_role_config``) must fail closed, so
absent, malformed and digest-mismatched records are covered here too.

No git runs: a refused call returns before any worktree work, and the
allowed case stops at a stubbed ``_create_fresh_worktree`` - the first
launch step, which is what "no agent is spawned" is asserted against.
"""

import json

import pytest

import pipeline.advance as advance_module
import pipeline.config as config_module
from pipeline import concurrency as pcon
from pipeline import dispatch as pdispatch
from pipeline import persistence as ppers
from pipeline import plan_approval as pa
from pipeline import server as p

PLAN = "lca1"
STORY = "S-1"

_UNSET = object()


# ---------------------------------------------------------------------------
# Environment


@pytest.fixture(autouse=True)
def _approval_env(monkeypatch):
    """Start every test with the gate unset; tests opt in explicitly."""
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)


@pytest.fixture
def approval_enabled(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")


# ---------------------------------------------------------------------------
# Manifest helpers


def _manifest(approval=_UNSET, status="todo"):
    story = {
        "key": STORY,
        "title": f"story {STORY}",
        "summary": "s",
        "status": status,
        "risk": "low",
        "correlation_id": "c0ffee000001",
        "dispatch_attempts": 0,
    }
    manifest = {"name": PLAN, "paused": False, "stories": {STORY: story}}
    if approval is not _UNSET:
        manifest["approval"] = approval
    return manifest


def _approved(manifest):
    """An approval record made against exactly this manifest's content."""
    return {"status": "approved", "digest": pa.canonical_digest(manifest)}


def _pending(manifest):
    return {"status": "pending_approval", "digest": pa.canonical_digest(manifest)}


# ---------------------------------------------------------------------------
# dispatch_story seams


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


@pytest.fixture
def launcher(monkeypatch):
    """Stub the first launch step; records calls and stops before git.

    ``_create_fresh_worktree`` is the first side-effecting step of a real
    dispatch (it runs ``git fetch`` / ``git worktree add`` and is immediately
    followed by the agent launch), so a call here means the gate let the
    dispatch through and an agent would have been spawned.
    """
    calls = []

    def _stub(plan_name, branch, worktree_path):
        calls.append((plan_name, branch, str(worktree_path)))
        return {"ok": False, "error": "stub: stop before git"}

    monkeypatch.setattr(pdispatch, "_create_fresh_worktree", _stub)
    # Runs just before the worktree step and shells out to git.
    monkeypatch.setattr(
        pdispatch, "_refuse_if_remote_branch_taken", lambda *a, **k: None
    )
    return calls


def _write(plan_dir, manifest):
    path = plan_dir / f"{PLAN}.manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


# ---------------------------------------------------------------------------
# Tick seams (mirrors tests/unit/test_advance_dispatch_per_tick_cap.py)


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


class _FakeStore:
    def __init__(self, path):
        self.path = path

    def manifest_path(self, plan_name):
        return self.path

    def get_manifest(self, plan_name):
        return json.loads(self.path.read_text())


@pytest.fixture(autouse=True)
def _pin_tick_environment(monkeypatch):
    """Pin every out-of-scope gate wide open so only approval can bind."""
    for name, value in (
        ("MAX_CONCURRENT_AGENTS", 8),
        ("DISPATCH_MAX_ATTEMPTS", 3),
        ("SESSION_PAUSE_THRESHOLD", 101),
        ("SESSION_RESUME_THRESHOLD", 101),
        ("WEEK_PAUSE_THRESHOLD", 101),
        ("WEEK_RESUME_THRESHOLD", 101),
    ):
        monkeypatch.setattr(config_module, name, value, raising=False)
        if hasattr(advance_module, name):
            monkeypatch.setattr(advance_module, name, value, raising=False)


@pytest.fixture
def tick(monkeypatch, tmp_path):
    """Wire the tick's seams; returns (dispatched, install(manifest))."""
    dispatched = []

    def _recorder(plan_name, key):
        dispatched.append((plan_name, key))
        return {"ok": True}

    monkeypatch.setattr(advance_module, "backend", _FakeBackend())
    monkeypatch.setattr(
        advance_module, "_role_resource_ok", lambda *a, **k: (True, "")
    )
    monkeypatch.setattr(
        advance_module, "_count_on_device_in_progress_agents", lambda: 0, raising=True
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(
        advance_module, "interrupt_story", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(
        advance_module, "check_story_status", lambda *a, **k: {"ok": True}
    )
    monkeypatch.setattr(advance_module, "review_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "dispatch_story", _recorder)

    def _install(manifest):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        monkeypatch.setattr(advance_module, "_store", _FakeStore(path))
        return path

    return dispatched, _install


def _run_tick():
    return advance_module._advance_pipeline_locked_impl(PLAN)


# ---------------------------------------------------------------------------
# Tick: disabled gate is byte-identical to today
# ---------------------------------------------------------------------------


def test_tick_advances_a_pending_manifest_when_approval_is_disabled(tick):
    dispatched, install = tick
    manifest = _manifest()
    manifest["approval"] = {"status": "pending_approval", "digest": "stale"}
    install(manifest)

    result = _run_tick()

    assert result.get("skipped") != "plan_not_approved"
    assert dispatched == [(PLAN, STORY)]


# ---------------------------------------------------------------------------
# Tick: enabled gate
# ---------------------------------------------------------------------------


def test_tick_skips_with_plan_not_approved_when_approval_is_pending(
    tick, approval_enabled
):
    dispatched, install = tick
    manifest = _manifest()
    manifest["approval"] = _pending(manifest)
    install(manifest)

    result = _run_tick()

    assert result == {"ok": True, "skipped": "plan_not_approved"}
    assert dispatched == []


def test_tick_treats_an_approved_plan_as_ready(tick, approval_enabled):
    dispatched, install = tick
    manifest = _manifest()
    manifest["approval"] = _approved(manifest)
    install(manifest)

    result = _run_tick()

    assert result.get("skipped") != "plan_not_approved"
    assert dispatched == [(PLAN, STORY)]


@pytest.mark.parametrize(
    "approval",
    [
        pytest.param(_UNSET, id="absent"),
        pytest.param("approved", id="malformed-string"),
        pytest.param({"status": "approved"}, id="malformed-no-digest"),
        pytest.param({"status": "approved", "digest": "old"}, id="digest-mismatch"),
    ],
)
def test_tick_refuses_an_unusable_approval_record(tick, approval_enabled, approval):
    dispatched, install = tick
    install(_manifest(approval=approval))

    result = _run_tick()

    assert result == {"ok": True, "skipped": "plan_not_approved"}
    assert dispatched == []


def test_tick_proceeds_once_the_approval_digest_matches_again(
    tick, approval_enabled
):
    """The refusal must not be cached or repair the record behind our back."""
    dispatched, install = tick
    manifest = _manifest()
    manifest["approval"] = {"status": "approved", "digest": "old"}
    path = install(manifest)

    assert _run_tick() == {"ok": True, "skipped": "plan_not_approved"}
    assert dispatched == []

    stored = json.loads(path.read_text())
    stored["approval"] = _approved(stored)
    path.write_text(json.dumps(stored))

    result = _run_tick()

    assert result.get("skipped") != "plan_not_approved"
    assert dispatched == [(PLAN, STORY)]


# ---------------------------------------------------------------------------
# dispatch_story: disabled gate is byte-identical to today
# ---------------------------------------------------------------------------


def test_dispatch_proceeds_for_a_pending_manifest_when_approval_is_disabled(
    plan_dir, worktree_root, launcher
):
    manifest = _manifest()
    manifest["approval"] = {"status": "pending_approval", "digest": "stale"}
    _write(plan_dir, manifest)

    result = p.dispatch_story(PLAN, STORY)

    assert result["error"] == "stub: stop before git"
    assert len(launcher) == 1


# ---------------------------------------------------------------------------
# dispatch_story: enabled gate
# ---------------------------------------------------------------------------


def test_dispatch_refuses_a_pending_approval_without_spawning(
    plan_dir, worktree_root, launcher, approval_enabled
):
    manifest = _manifest()
    manifest["approval"] = _pending(manifest)
    path = _write(plan_dir, manifest)
    before = path.read_bytes()

    result = p.dispatch_story(PLAN, STORY)

    assert result == {"ok": False, "error": "plan is not approved", "plan": PLAN}
    assert launcher == []
    assert path.read_bytes() == before


def test_dispatch_proceeds_for_an_approved_plan(
    plan_dir, worktree_root, launcher, approval_enabled
):
    manifest = _manifest()
    manifest["approval"] = _approved(manifest)
    _write(plan_dir, manifest)

    result = p.dispatch_story(PLAN, STORY)

    assert result["error"] == "stub: stop before git"
    assert len(launcher) == 1


@pytest.mark.parametrize(
    "approval",
    [
        pytest.param(_UNSET, id="absent"),
        pytest.param("approved", id="malformed-string"),
        pytest.param({"status": "approved"}, id="malformed-no-digest"),
        pytest.param({"status": "approved", "digest": "old"}, id="digest-mismatch"),
    ],
)
def test_dispatch_refuses_an_unusable_approval_record(
    plan_dir, worktree_root, launcher, approval_enabled, approval
):
    _write(plan_dir, _manifest(approval=approval))

    result = p.dispatch_story(PLAN, STORY)

    assert result == {"ok": False, "error": "plan is not approved", "plan": PLAN}
    assert launcher == []


def test_direct_dispatch_bypassing_the_tick_is_refused(
    plan_dir, worktree_root, launcher, approval_enabled
):
    """The tick gate alone is not enough: the MCP/dashboard entry point is
    reachable directly, so dispatch_story must refuse on its own."""
    manifest = _manifest()
    manifest["approval"] = _pending(manifest)
    _write(plan_dir, manifest)

    result = p.dispatch_story(PLAN, STORY)

    assert result == {"ok": False, "error": "plan is not approved", "plan": PLAN}
    assert launcher == []


def test_dispatch_proceeds_once_the_approval_digest_matches_again(
    plan_dir, worktree_root, launcher, approval_enabled
):
    manifest = _manifest()
    manifest["approval"] = {"status": "approved", "digest": "old"}
    path = _write(plan_dir, manifest)

    assert p.dispatch_story(PLAN, STORY) == {
        "ok": False,
        "error": "plan is not approved",
        "plan": PLAN,
    }
    assert launcher == []

    stored = json.loads(path.read_text())
    stored["approval"] = _approved(stored)
    path.write_text(json.dumps(stored))

    result = p.dispatch_story(PLAN, STORY)

    assert result["error"] == "stub: stop before git"
    assert len(launcher) == 1
