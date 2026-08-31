"""W4L-02 — correlation ID minted at dispatch and carried through.

Covers the dispatch half of the W4-logging slice
(docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, "Logging"):

1. First dispatch of a story mints a non-empty ``story["correlation_id"]``
   (uuid4 hex, 12 chars) and persists it on the story manifest.
2. Redispatch (re-entry with the id already set) keeps the SAME id.
3. The subprocess env handed to the dispatched agent carries
   ``PIPELINE_CORRELATION_ID`` equal to the minted id.
4. The success-path ``agent_dispatched`` notification/event dispatch emits
   carries the correlation id.
5. Mint-before-launch ordering: if the agent subprocess launch fails, the
   minted id is still persisted on the story.
6. Two different stories in the same plan get different correlation ids.

Fixtures/helpers are copied from test_dispatch_staleness.py /
test_dispatch_worktree_from_origin.py per this repo's convention — there is
no shared conftest.py for these. The real remote is never touched: git runs
against a throwaway local origin, and the agent subprocess launch is faked.
"""

import json
import re
import subprocess

import pytest

from app import backend
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt


# ---------- Fixtures (copied from test_dispatch_staleness.py) ----------
@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "overlord.md").write_text(
        '---\nname: "overlord"\nmodel: opus\nmemory: user\n---\n\n'
        "You are the Overlord body text.\n"
    )
    (d / "software-engineer.md").write_text(
        '---\nname: "software-engineer"\nmodel: sonnet\n---\n\nEngineer body.\n'
    )
    (d / "code-reviewer.md").write_text(
        '---\nname: "code-reviewer"\nmodel: sonnet\n---\n\nReviewer body.\n'
    )
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.args = []
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        return ("", "")

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _make_repo(tmp_path):
    """A throwaway origin + clone so dispatch's git fetch/worktree add run
    for real without touching any remote."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", str(origin)], tmp_path)
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("x\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", "main"], repo)
    return origin, repo


def _write_manifest(plan_dir, plan_name, stories, repo_root=None):
    manifest = {"epics": {}, "stories": stories}
    if repo_root is not None:
        manifest["repo_root"] = str(repo_root)
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _read_manifest(plan_dir, plan_name):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _make_story(**overrides):
    story = {
        "summary": "Do the thing",
        "agent_instructions": "Build it.",
        "status": "pending",
        "dependencies": [],
        "role": "software-engineer",
        "backend": "local",
        "model": "test-model",
        "dispatch_attempts": 0,
    }
    story.update(overrides)
    return story


class _Recorder:
    """Records _notify_user calls and agent Popen launches (with env)."""

    def __init__(self):
        self.notifies = []  # list of (args, kwargs)
        self.popens = []  # list of (cmd, kwargs)

    def notify(self, *args, **kwargs):
        self.notifies.append((args, kwargs))

    def popen(self, cmd, **kwargs):
        self.popens.append((list(cmd), kwargs))
        return _FakeProc(4242)

    def popen_envs(self):
        return [kw.get("env") for _cmd, kw in self.popens if kw.get("env")]

    def notified_cids(self):
        """All correlation_id values passed through the _notify_user seam,
        however W4L-01 threads them in (top-level kwarg or nested in a
        payload dict)."""
        found = []
        for _args, kwargs in self.notifies:
            for value in kwargs.values():
                if isinstance(value, dict) and "correlation_id" in value:
                    found.append(value["correlation_id"])
            if "correlation_id" in kwargs:
                found.append(kwargs["correlation_id"])
        return found


class _RecordingBus:
    """Stands in for pipeline.event_wiring.get_bus() so tests capture every
    event published during dispatch without wiring real sinks."""

    def __init__(self):
        self.events = []
        self._handlers = {}

    def publish(self, evt):
        self.events.append(evt)


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    rec.bus = _RecordingBus()
    monkeypatch.setattr(p, "_notify_user", rec.notify)

    from pipeline import event_wiring

    monkeypatch.setattr(event_wiring, "get_bus", lambda: rec.bus)

    real_popen = subprocess.Popen

    def _discriminating_popen(cmd, **kwargs):
        # Let real git (worktree add, fetch, ...) run; fake only agent
        # launches, mirroring test_dispatch_staleness.py's approach.
        if cmd and cmd[0] == "git":
            return real_popen(cmd, **kwargs)
        return rec.popen(cmd, **kwargs)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)
    return rec


def _setup_plan(plan_dir, repo, stories):
    _write_manifest(plan_dir, "w4lcorr", stories, repo_root=repo)
    return "w4lcorr"


def _no_plane(*_a, **_k):
    return (_ for _ in ()).throw(RuntimeError("no plane"))


@pytest.fixture
def dispatched_plan(
    plan_dir, worktree_root, agents_dir, recorder, tmp_path, monkeypatch
):
    """A plan with one pending story, dispatched once, against a real
    throwaway repo. Returns (plan_name, repo)."""
    _origin, repo = _make_repo(tmp_path)
    plan_name = _setup_plan(plan_dir, repo, {"S1": _make_story()})
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request", _no_plane)
    result = p.dispatch_story(plan_name, "S1")
    assert result.get("ok") is True, f"dispatch failed: {result}"
    return plan_name, repo


# 1. First dispatch mints and persists a correlation id. ---------------------
def test_first_dispatch_mints_and_persists_correlation_id(dispatched_plan, plan_dir):
    plan_name, _repo = dispatched_plan

    story = _read_manifest(plan_dir, plan_name)["stories"]["S1"]
    cid = story.get("correlation_id")
    assert isinstance(cid, str) and cid, (
        "first dispatch must set a non-empty story['correlation_id']"
    )
    assert re.fullmatch(r"[0-9a-f]{12}", cid), (
        f"correlation id should be a 12-char lowercase hex uuid4 slice, got {cid!r}"
    )


# 2. Redispatch keeps the same id. -------------------------------------------
def test_redispatch_keeps_existing_correlation_id(dispatched_plan, plan_dir):
    plan_name, _repo = dispatched_plan
    existing = _read_manifest(plan_dir, plan_name)["stories"]["S1"]["correlation_id"]

    result = p.dispatch_story(plan_name, "S1")
    assert result.get("ok") is True, f"redispatch failed: {result}"

    story = _read_manifest(plan_dir, plan_name)["stories"]["S1"]
    assert story["correlation_id"] == existing, (
        "redispatch must NOT mint a new correlation id"
    )


# 3. The dispatched agent's env carries PIPELINE_CORRELATION_ID. -------------
def test_dispatch_env_contains_pipeline_correlation_id(
    dispatched_plan, plan_dir, recorder
):
    plan_name, _repo = dispatched_plan
    minted = _read_manifest(plan_dir, plan_name)["stories"]["S1"]["correlation_id"]

    envs = recorder.popen_envs()
    assert envs, "expected the agent launch to receive an env mapping"
    matching = [e for e in envs if e.get("PIPELINE_CORRELATION_ID") == minted]
    assert matching, (
        f"no subprocess env carried PIPELINE_CORRELATION_ID={minted!r}; "
        f"got envs: {envs!r}"
    )


# 4. The agent_dispatched event carries the correlation id. ------------------
def test_agent_dispatched_event_carries_correlation_id(
    dispatched_plan, plan_dir, recorder
):
    plan_name, _repo = dispatched_plan
    minted = _read_manifest(plan_dir, plan_name)["stories"]["S1"]["correlation_id"]

    # The event may travel via the process bus (make_event with a
    # top-level correlation_id) or via the _notify_user seam (correlation_id
    # kwarg / payload key). Accept either, require at least one.
    bus_cids = [
        evt.get("correlation_id")
        for evt in recorder.bus.events
        if evt.get("correlation_id")
    ]
    assert recorder.notifies or recorder.bus.events, (
        "expected dispatch to emit an event on success"
    )
    assert minted in bus_cids or minted in recorder.notified_cids(), (
        f"expected an event stamped with correlation_id={minted!r}; "
        f"got bus events: {recorder.bus.events!r}, "
        f"notify kwargs: {[kw for _a, kw in recorder.notifies]!r}"
    )


# 5. Mint-before-launch: a failed launch still leaves the id persisted. ------
def test_failed_launch_still_persists_minted_id(
    plan_dir, worktree_root, agents_dir, recorder, monkeypatch, tmp_path
):
    _origin, repo = _make_repo(tmp_path)
    plan_name = _setup_plan(plan_dir, repo, {"S1": _make_story()})
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request", _no_plane)

    def _exploding_popen(cmd, **kwargs):
        raise OSError("agent launch failed")

    monkeypatch.setattr(backend.subprocess, "Popen", _exploding_popen)

    try:
        p.dispatch_story(plan_name, "S1")
    except Exception as exc:  # noqa: BLE001
        # Dispatch may propagate or swallow the launch failure; either way
        # the id minted before launch must already be persisted.
        print(f"dispatch raised (acceptable): {exc!r}")

    story = _read_manifest(plan_dir, plan_name)["stories"]["S1"]
    cid = story.get("correlation_id")
    assert isinstance(cid, str) and cid, (
        "the correlation id minted before launch must outlive a failed "
        "launch and be persisted on the story manifest"
    )


# 6. Different stories in the same plan get different ids. -------------------
def test_different_stories_get_different_correlation_ids(
    plan_dir, worktree_root, agents_dir, recorder, monkeypatch, tmp_path
):
    _origin, repo = _make_repo(tmp_path)
    plan_name = _setup_plan(
        plan_dir,
        repo,
        {"S1": _make_story(), "S2": _make_story(summary="Other thing")},
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(pt, "plane_request", _no_plane)

    r1 = p.dispatch_story(plan_name, "S1")
    r2 = p.dispatch_story(plan_name, "S2")
    assert r1.get("ok") is True, f"dispatch S1 failed: {r1}"
    assert r2.get("ok") is True, f"dispatch S2 failed: {r2}"

    stories = _read_manifest(plan_dir, plan_name)["stories"]
    cid1 = stories["S1"].get("correlation_id")
    cid2 = stories["S2"].get("correlation_id")
    assert cid1 and cid2, "both stories must end up with a correlation id"
    assert cid1 != cid2, "distinct stories must not share a correlation id"
