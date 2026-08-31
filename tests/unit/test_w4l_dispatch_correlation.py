"""W4L-02 — correlation ID minted at dispatch and carried through.

Covers the dispatch half of the W4-logging slice
(docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, "Logging"):

1. First dispatch of a story mints a non-empty ``story["correlation_id"]``
   and persists it on the story manifest.
2. Redispatch (re-entry with the id already set) keeps the SAME id.
3. The subprocess env handed to the dispatched agent carries
   ``PIPELINE_CORRELATION_ID`` equal to the minted id.
4. The ``agent_dispatched`` event dispatch emits carries the correlation id.
5. Mint-before-launch ordering: if the agent subprocess launch fails, the
   minted id is still persisted on the story.
6. Two different stories in the same plan get different correlation ids.

These tests patch the ``pipeline.server`` seams (the ``_ServerRef`` pattern
used by pipeline/dispatch.py) and the subprocess layer — the real remote and
real agent subprocesses are never touched.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pipeline.dispatch as dispatch_module
import pipeline.server as server

PLAN = "w4l-corr-plan"


# ---------------------------------------------------------------------------
# Fakes for the pipeline.server seams dispatch.py resolves at call time.
# ---------------------------------------------------------------------------


def _make_story(**overrides: Any) -> dict[str, Any]:
    story: dict[str, Any] = {
        "key": "S1",
        "title": "Do the thing",
        "status": "pending",
        "role": "engineer",
        "backend": "local",
        "model": "test-model",
        "dispatch_attempts": 0,
    }
    story.update(overrides)
    return story


class FakeStore:
    """Minimal stand-in for ``pipeline.server._store``."""

    def __init__(self, manifest: dict[str, Any], root: Path) -> None:
        self._manifest = manifest
        self._root = root
        self.transactions: list[str] = []

    @contextmanager
    def transaction(self, plan_name: str):
        self.transactions.append(plan_name)
        yield True

    def manifest_path(self, plan_name: str) -> Path:
        return self._root / f"{plan_name}.manifest.json"

    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        return self._manifest


class Harness:
    """Wires fake server seams around ``_dispatch_story_impl``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.stories: dict[str, dict[str, Any]] = {
            "S1": _make_story(),
        }
        self.manifest: dict[str, Any] = {
            "plan": PLAN,
            "stories": self.stories,
        }
        self.store = FakeStore(self.manifest, tmp_path)
        self.manifest_writes: list[tuple[Path, dict[str, Any]]] = []
        self.notifications: list[dict[str, Any]] = []
        self.subprocess_calls: list[dict[str, Any]] = []
        self.launch_error: BaseException | None = None

        monkeypatch.setattr(server, "_store", self.store)
        monkeypatch.setattr(server, "_validate_key", lambda key: None)
        monkeypatch.setattr(server, "WORKTREE_ROOT", tmp_path / "worktrees")
        monkeypatch.setattr(server, "_read_journal", lambda plan, key: [])
        monkeypatch.setattr(
            server, "_default_branch", lambda: "main"
        )

        @contextmanager
        def fake_scoped_repo_root(plan_name: str):
            yield tmp_path / "repo"

        @contextmanager
        def fake_git_lock(repo_root: Path):
            yield False

        monkeypatch.setattr(server, "_scoped_repo_root", fake_scoped_repo_root)
        monkeypatch.setattr(server, "_try_acquire_git_lock", fake_git_lock)
        monkeypatch.setattr(
            server,
            "_build_dispatch_command",
            lambda *a, **kw: ["echo", "dispatched"],
        )

        def fake_atomic_write_json(path: Path, data: Any) -> None:
            self.manifest_writes.append((Path(path), data))

        monkeypatch.setattr(server, "_atomic_write_json", fake_atomic_write_json)

        def fake_notify_user(plan_name: str, msg: str, **kwargs: Any) -> None:
            self.notifications.append(
                {"plan": plan_name, "msg": msg, **kwargs}
            )

        monkeypatch.setattr(server, "_notify_user", fake_notify_user)

        real_run = subprocess.run
        real_popen = subprocess.Popen

        def fake_run(cmd, *args: Any, **kwargs: Any):
            self.subprocess_calls.append(
                {"cmd": cmd, "env": kwargs.get("env"), "kind": "run"}
            )
            if self.launch_error is not None:
                raise self.launch_error
            return SimpleNamespace(
                returncode=0, stdout="0", stderr="", args=cmd
            )

        def fake_popen(cmd, *args: Any, **kwargs: Any):
            self.subprocess_calls.append(
                {"cmd": cmd, "env": kwargs.get("env"), "kind": "popen"}
            )
            if self.launch_error is not None:
                raise self.launch_error
            return SimpleNamespace(pid=4242, poll=lambda: 0, wait=lambda: 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        self._real_run = real_run
        self._real_popen = real_popen

    # -- helpers ------------------------------------------------------------

    def dispatch(self, story_key: str = "S1") -> dict[str, Any]:
        return dispatch_module._dispatch_story_impl(PLAN, story_key)

    def persisted_story(self, story_key: str = "S1") -> dict[str, Any]:
        assert self.manifest_writes, "expected at least one manifest write"
        _path, data = self.manifest_writes[-1]
        return data["stories"][story_key]

    def dispatched_envs(self) -> list[dict[str, str]]:
        return [
            call["env"]
            for call in self.subprocess_calls
            if call["env"] is not None
        ]


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# 1. First dispatch mints and persists a correlation id.
# ---------------------------------------------------------------------------


def test_first_dispatch_mints_and_persists_correlation_id(harness: Harness) -> None:
    harness.dispatch("S1")

    story = harness.stories["S1"]
    cid = story.get("correlation_id")
    assert isinstance(cid, str) and cid, (
        "first dispatch must set a non-empty story['correlation_id']"
    )
    # Persisted on the manifest write path, not just mutated in memory.
    assert harness.persisted_story("S1").get("correlation_id") == cid


# ---------------------------------------------------------------------------
# 2. Redispatch keeps the same id.
# ---------------------------------------------------------------------------


def test_redispatch_keeps_existing_correlation_id(harness: Harness) -> None:
    existing = "abc123def456"
    harness.stories["S1"]["correlation_id"] = existing

    harness.dispatch("S1")

    assert harness.stories["S1"]["correlation_id"] == existing
    assert harness.persisted_story("S1").get("correlation_id") == existing


# ---------------------------------------------------------------------------
# 3. The dispatched agent's env carries PIPELINE_CORRELATION_ID.
# ---------------------------------------------------------------------------


def test_dispatch_env_contains_pipeline_correlation_id(harness: Harness) -> None:
    harness.dispatch("S1")

    minted = harness.stories["S1"]["correlation_id"]
    assert minted, "expected a minted correlation id"
    envs = harness.dispatched_envs()
    assert envs, "expected the dispatch launch to pass an env mapping"
    matching = [e for e in envs if e.get("PIPELINE_CORRELATION_ID") == minted]
    assert matching, (
        f"no subprocess env carried PIPELINE_CORRELATION_ID={minted!r}; "
        f"got envs: {envs!r}"
    )


# ---------------------------------------------------------------------------
# 4. The agent_dispatched event carries the correlation id.
# ---------------------------------------------------------------------------


def test_agent_dispatched_event_carries_correlation_id(harness: Harness) -> None:
    harness.dispatch("S1")

    minted = harness.stories["S1"]["correlation_id"]
    stamped = [
        n for n in harness.notifications if n.get("correlation_id") == minted
    ]
    assert stamped, (
        "expected at least one _notify_user event stamped with the minted "
        f"correlation_id {minted!r}; got: {harness.notifications!r}"
    )


# ---------------------------------------------------------------------------
# 5. Mint-before-launch: a failed launch still leaves the id persisted.
# ---------------------------------------------------------------------------


def test_failed_launch_still_persists_minted_id(harness: Harness) -> None:
    harness.launch_error = OSError("agent launch failed")

    try:
        harness.dispatch("S1")
    except OSError:
        pass  # dispatch may propagate or swallow the launch failure

    story = harness.stories["S1"]
    cid = story.get("correlation_id")
    assert isinstance(cid, str) and cid, (
        "the correlation id minted before launch must outlive a failed launch"
    )
    assert harness.persisted_story("S1").get("correlation_id") == cid


# ---------------------------------------------------------------------------
# 6. Different stories in the same plan get different ids.
# ---------------------------------------------------------------------------


def test_different_stories_get_different_correlation_ids(
    harness: Harness,
) -> None:
    harness.stories["S2"] = _make_story(key="S2", title="Do the other thing")

    harness.dispatch("S1")
    harness.dispatch("S2")

    cid1 = harness.stories["S1"]["correlation_id"]
    cid2 = harness.stories["S2"]["correlation_id"]
    assert cid1 and cid2, "both stories must end up with a correlation id"
    assert cid1 != cid2, "distinct stories must not share a correlation id"