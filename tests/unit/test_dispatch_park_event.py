"""Tests for the ``event="story_parked"`` stamp on ``_dispatch_story_impl``'s
rebase-conflict park notification.

Background
----------
``pipeline/notification_outbox.py:outbox_sink`` selects records for e-mail
delivery using ONLY the structured field ``event["payload"]["event"]``.  The
message text is never matched, so a notification emitted without an ``event=``
kwarg can never be e-mailed no matter what it says.

``pipeline/dispatch.py:_dispatch_story_impl`` has exactly ONE branch that sets
``story["status"] = "parked"``: the unresolvable rebase conflict on a RESUMED
worktree (``elif result["conflict"]:``).  It called ``_notify_user`` without
``event=``, so the single most important alert -- "your story parked and needs
a human" -- was unreachable.  This story stamps that one call with the bare
string literal ``event="story_parked"`` (matching the existing stamped sites in
``pipeline/advance.py``, which use bare literals such as
``event="dispatch_failed"``).

What is graded here
-------------------
These tests drive the REAL ``_dispatch_story_impl`` (through
``pipeline.server.dispatch_story`` -> ``PipelineService.dispatch_story`` ->
``dispatch._dispatch_story_impl``) against a REAL git origin/repo/worktree, with
``_notify_user`` monkeypatched on ``pipeline.server`` -- which is exactly what
the module under test resolves at call time (``dispatch._notify_user`` is a
``_ServerRef`` that reads the live ``pipeline.server`` binding).  They therefore
prove the *wiring*, not the existence of a constant.  A source-text grep over
``pipeline/dispatch.py`` would pass even if the call were unreachable or the
kwarg landed on the wrong call, so no such test is used.

Coverage:

* positive -- the park path emits ``event="story_parked"``;
* the stamp is a bare ``str`` literal, passed as a keyword, with the two
  positional arguments and the message text unchanged, and no other kwargs
  introduced;
* behaviour preservation -- ``status``/``parked_reason``/return value unchanged;
* negative -- non-park notifications from the SAME function (the successful
  rebase notice and the non-conflict fail-open notice) do NOT carry
  ``event="story_parked"``, which proves the right call was stamped;
* boundary -- the non-conflict fail-open path does NOT park the story;
* the existing never-break contract -- a notification sink that raises still
  does not break ``_dispatch_story_impl``.

The implementation does not exist yet, so this file is expected to be RED
(failing assertions) until it lands.  No real backend, git remote, or PR is ever
contacted.
"""

import json
import subprocess

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import dispatch as pdisp
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
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
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


# ---------- Real-git harness (copied from test_dispatch_staleness.py) ----------
def _write_manifest(plan_dir, plan_name, stories, repo_root=None):
    manifest = {"epics": {}, "stories": stories}
    if repo_root is not None:
        manifest["repo_root"] = str(repo_root)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _run(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True,
                          text=True)


def _make_origin_and_repo(tmp_path, branch="main"):
    """Bare `origin` + a real local clone `repo`, both on `branch`, one
    commit deep. Returns (origin, repo, branch)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", "-b", branch, str(origin)], tmp_path)

    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", branch, str(repo)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-qm", "init"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "-q", "-u", "origin", branch], repo)
    return origin, repo, branch


def _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra"):
    """Push a new commit to `origin` from a THIRD clone, so `repo` never sees
    it locally until a fetch pulls it in."""
    other = tmp_path / f"other-clone-{name}"
    _run(["git", "clone", "-q", str(origin), str(other)], tmp_path)
    _run(["git", "config", "user.email", "t@e.com"], other)
    _run(["git", "config", "user.name", "t"], other)
    (other / name).write_text("more\n")
    _run(["git", "add", "-A"], other)
    _run(["git", "commit", "-qm", "more"], other)
    _run(["git", "push", "-q", "origin", branch], other)
    return _run(["git", "rev-parse", branch], other).stdout.strip()


def _make_resumed_worktree(tmp_path, worktree_root, repo, branch, story_key="S1"):
    """Create a REAL git worktree at worktree_root/<story_key> on an
    `agent/<story_key>` branch off repo's current HEAD, so the staleness
    check's `git rev-list --count` runs against real refs."""
    worktree_path = worktree_root / story_key
    agent_branch = f"agent/{story_key.lower()}"
    _run(
        ["git", "worktree", "add", "-b", agent_branch, str(worktree_path), "HEAD"],
        repo,
    )
    return worktree_path


# The exact error text the fake rebase reports; asserted verbatim in the
# park notification message and the persisted parked_reason.
_CONFLICT_ERROR = "CONFLICT (content): merge conflict in pipeline/foo.py"


def _dispatch_resumed(
    plan_dir, worktree_root, agents_dir, monkeypatch, plan_name, story_key,
    repo, branch, *, rebase_result, notify_calls=None, notify_raises=False,
    entry=None,
):
    """Dispatch a RESUMED story against a REAL repo (with a real worktree).

    ``_rebase_onto_master`` is monkeypatched to return ``rebase_result`` so the
    conflict / success / non-conflict branches are reached deterministically.
    ``_notify_user`` is monkeypatched on BOTH ``pipeline.server`` (the live
    binding the module under test resolves through its ``_ServerRef``) and
    ``pipeline.dispatch`` (the module under test itself), capturing every call
    as ``{"args": tuple, "kwargs": dict}``.
    """
    worktree_path = worktree_root / story_key
    _write_manifest(plan_dir, plan_name, {
        story_key: {"summary": "Do thing", "agent_instructions": "Build it.",
                    "status": "interrupted", "dependencies": [],
                    "worktree": str(worktree_path)},
    }, repo_root=repo)
    monkeypatch.setattr(p, "_default_branch", lambda: branch)
    monkeypatch.setattr(pt, "plane_request",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_rebase_onto_master",
                        lambda wt, br: dict(rebase_result))

    real_popen = backend.subprocess.Popen

    def _discriminating_popen(cmd, **kw):
        if cmd and cmd[0] == "claude":
            return _FakeProc(4242)
        return real_popen(cmd, **kw)

    monkeypatch.setattr(backend.subprocess, "Popen", _discriminating_popen)

    if notify_calls is not None:
        if notify_raises:
            def fake_notify(*args, **kwargs):
                notify_calls.append({"args": args, "kwargs": kwargs})
                raise RuntimeError("notification sink exploded")
        else:
            def fake_notify(*args, **kwargs):
                notify_calls.append({"args": args, "kwargs": kwargs})
        monkeypatch.setattr(p, "_notify_user", fake_notify)
        monkeypatch.setattr(pdisp, "_notify_user", fake_notify)

    if entry is None:
        entry = p.dispatch_story
    return entry(plan_name, story_key)


def _park_calls(notify_calls):
    """The captured calls that carry ``event="story_parked"``."""
    return [c for c in notify_calls if c["kwargs"].get("event") == "story_parked"]


# ---------------------------------------------------------------------------
# (1) POSITIVE: the park path emits event="story_parked".
# ---------------------------------------------------------------------------


def test_park_notification_carries_story_parked_event(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "park1", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": True, "error": _CONFLICT_ERROR},
        notify_calls=notify_calls)

    assert result["status"] == "parked"
    park = _park_calls(notify_calls)
    assert len(park) == 1, (
        f"expected exactly one stamped park notification, got: {notify_calls}")
    assert park[0]["kwargs"]["event"] == "story_parked"


def test_park_event_is_bare_string_keyword_literal(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """The stamp is a bare ``str`` literal passed as a KEYWORD, the two
    positional arguments and the message text are unchanged, and no other
    kwargs were introduced."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "park2", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": True, "error": _CONFLICT_ERROR},
        notify_calls=notify_calls)

    park = _park_calls(notify_calls)
    assert len(park) == 1, f"expected one stamped park call, got: {notify_calls}"
    call = park[0]

    # Keyword, not positional: the event lives in kwargs, not args.
    assert "event" in call["kwargs"]
    assert isinstance(call["kwargs"]["event"], str)
    assert call["kwargs"]["event"] == "story_parked"
    # No other kwargs introduced on this call.
    assert call["kwargs"] == {"event": "story_parked"}

    # The two positional args are unchanged: (plan_name, message).
    assert call["args"][0] == "park2"
    assert call["args"][1] == (
        f"story S1 parked: rebase conflict against origin/{branch} - "
        f"{_CONFLICT_ERROR}"
    )


# ---------------------------------------------------------------------------
# (2) Behaviour preservation: status + parked_reason + return value.
# ---------------------------------------------------------------------------


def test_park_preserves_status_and_parked_reason(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """The stamp must not disturb the park itself."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "park3", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": True, "error": _CONFLICT_ERROR},
        notify_calls=notify_calls)

    on_disk = _read_story(plan_dir, "park3", "S1")
    assert on_disk["status"] == "parked"
    assert on_disk["parked_reason"] == (
        f"rebase conflict: {_CONFLICT_ERROR}; "
        f"worktree still behind origin/{branch}"
    )


def test_park_return_value_unchanged(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "park4", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": True, "error": _CONFLICT_ERROR},
        notify_calls=notify_calls)

    assert result == {
        "status": "parked",
        "reason": "rebase_conflict",
        "parked_reason": (
            f"rebase conflict: {_CONFLICT_ERROR}; "
            f"worktree still behind origin/{branch}"
        ),
    }


# ---------------------------------------------------------------------------
# (3) NEGATIVE: non-park notifications from the SAME function are NOT stamped.
# ---------------------------------------------------------------------------


def test_rebased_notification_is_not_stamped(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """The successful-rebase notice (``result["ok"]``) is a non-park
    notification from the same function and must NOT carry the stamp."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "neg1", "S1", repo, branch,
        rebase_result={"ok": True, "conflict": False, "error": ""},
        notify_calls=notify_calls)

    assert notify_calls, "the successful-rebase notice should have fired"
    assert _park_calls(notify_calls) == [], (
        f"a successful rebase must not be stamped story_parked: {notify_calls}")
    assert _read_story(plan_dir, "neg1", "S1")["status"] != "parked"


def test_non_conflict_failure_notification_is_not_stamped(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """The adjacent ``else`` (non-conflict fail-open) notice must NOT carry the
    stamp -- this proves the kwarg landed on the conflict call, not its
    neighbour."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "neg2", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": False, "error": "dirty tree"},
        notify_calls=notify_calls)

    assert notify_calls, "the non-conflict fail-open notice should have fired"
    assert _park_calls(notify_calls) == [], (
        f"the non-conflict fail-open path must not be stamped: {notify_calls}")


# ---------------------------------------------------------------------------
# (4) BOUNDARY: the non-conflict fail-open path does NOT park.
# ---------------------------------------------------------------------------


def test_non_conflict_failure_does_not_park(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "neg3", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": False, "error": "dirty tree"},
        notify_calls=notify_calls)

    on_disk = _read_story(plan_dir, "neg3", "S1")
    assert on_disk["status"] != "parked"
    assert "parked_reason" not in on_disk


# ---------------------------------------------------------------------------
# (5) The existing never-break contract: a raising sink must not break dispatch.
# ---------------------------------------------------------------------------


def test_notify_user_raising_does_not_break_dispatch(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """``_notify_user`` raising inside the park branch is swallowed by the
    enclosing observability hook; ``_dispatch_story_impl`` must not raise."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "raise1", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": True, "error": _CONFLICT_ERROR},
        notify_calls=notify_calls, notify_raises=True)

    # The park notification was attempted (the park branch was reached)...
    assert notify_calls, "the park notification should have been attempted"
    # ...and the raise did not escape the enclosing function.
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# (6) Drive the module-level function directly, not just the service wrapper.
# ---------------------------------------------------------------------------


def test_dispatch_story_impl_directly_stamps_park_event(
    plan_dir, worktree_root, agents_dir, monkeypatch, tmp_path,
):
    """Call ``pipeline.dispatch._dispatch_story_impl`` itself so the wiring is
    proven on the real function, not merely through the service facade."""
    origin, repo, branch = _make_origin_and_repo(tmp_path)
    _make_resumed_worktree(tmp_path, worktree_root, repo, branch, "S1")
    _push_extra_commit_directly_to_origin(tmp_path, origin, branch, name="extra0")

    notify_calls = []
    result = _dispatch_resumed(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        "direct1", "S1", repo, branch,
        rebase_result={"ok": False, "conflict": True, "error": _CONFLICT_ERROR},
        notify_calls=notify_calls, entry=pdisp._dispatch_story_impl)

    assert result["status"] == "parked"
    park = _park_calls(notify_calls)
    assert len(park) == 1, f"expected one stamped park call, got: {notify_calls}"
    assert park[0]["kwargs"]["event"] == "story_parked"
