"""The WAP-8 patch audit journal goes through the record store.

``pipeline/worktree_patch.py`` used to reach the journal writer by resolving
``importlib.import_module("pipeline.persistence")._append_journal`` at call
time.  ADR 0012 stage B moves every journal write onto the namespaced record
store, so both audit sites (``create_patch_record``'s ``patch_proposed`` and
``apply_patch``'s success-ONLY ``patch_applied``) must write through

    importlib.import_module("pipeline.server")._store.records().append_journal(
        "default", plan_name, story_key, record
    )

resolved at call time (no module-level import: the module keeps its
stdlib-only import purity, and no new module-level function is added).

Every test here installs a recording stand-in for the store the module
resolves at call time, so the assertions measure the audit call itself - the
namespace, the plan/story keys and the record - and never the import style.

Hermeticity: the apply tests need a real git repository, resolved first, for
the same reason ``test_worktree_patch_audit.py`` does (a symlinked root
component would make the write resolver reject every path).
"""

from __future__ import annotations

import difflib
import json
import subprocess
from pathlib import Path

import pytest

from pipeline import event_wiring, server, worktree_patch
from pipeline.worktree_patch import apply_patch, create_patch_record

PLAN_NAME = "journalplan"
STORY_KEY = "J-1"

#: Captured at import time so the test's own git plumbing is never intercepted.
_REAL_RUN = subprocess.run


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class _Recorder:
    """The record view double: records every ``append_journal`` call.

    ``fail_next`` makes the next write raise BEFORE anything is recorded, the
    way a full disk would: no half-written entry, no placeholder.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str, dict]] = []
        self.fail_next: bool = False
        self.failures: list[str] = []

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        if self.fail_next:
            self.fail_next = False
            self.failures.append(story_key)
            raise RuntimeError("disk full")
        self.entries.append((namespace, plan_name, story_key, record))

    def actions(self) -> list[str]:
        return [entry[3].get("action") for entry in self.entries]


class _RecordView:
    """The store's record view: journal writes are recorded, manifests absent.

    Manifest reads are NOT this story's seam, so they stay on the file source
    the fixture writes: ``get_manifest`` answers ``None`` (unknown plan) and
    ``get_manifests`` answers ``{}``, which makes the shared store-first read
    fall back to the plan directory exactly as a fresh deployment would.
    ``append_journal`` is the call under test and is delegated to the
    recorder.
    """

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None:
        self._recorder.append_journal(namespace, plan_name, story_key, record)

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        return None

    def get_manifests(self, namespace: str) -> dict[str, dict]:
        return {}


class _StoreDouble:
    """The store double: ``records()`` returns the record view."""

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def records(self) -> _RecordView:
        return _RecordView(self._recorder)

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None:
        return None

    def get_manifests(self, namespace: str) -> dict[str, dict]:
        return {}


class _RecordingBus:
    """A bus double that records every published event and never dispatches."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    """Install the store double the module resolves at call time.

    The implementation is required to resolve ``pipeline.server`` lazily
    inside the function, so patching the ``server`` module attribute is what
    the real code sees.  A module-level binding, if one ever exists, is
    patched too, so the test measures the journal call rather than the import
    style.
    """
    recording = _Recorder()
    store = _StoreDouble(recording)
    monkeypatch.setattr(server, "_store", store)
    if hasattr(worktree_patch, "_store"):
        monkeypatch.setattr(worktree_patch, "_store", store)
    return recording


@pytest.fixture
def bus(monkeypatch) -> _RecordingBus:
    """Capture events published through the LAZY ``event_wiring.get_bus``."""
    recording = _RecordingBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: recording)
    if hasattr(worktree_patch, "get_bus"):
        monkeypatch.setattr(worktree_patch, "get_bus", lambda: recording)
    return recording


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git command in *repo* (never through a spy)."""
    return _REAL_RUN(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _unified(rel_path: str, old_text: str, new_text: str) -> str:
    """A unified diff for *rel_path* from *old_text* to *new_text*."""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real, committed git repository to act as the story worktree."""
    root = (tmp_path / "worktree").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test Author")
    (root / "app.py").write_text("alpha\nbeta\ngamma\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def manifest(plan_dir: Path, repo: Path) -> dict:
    """A manifest whose one story is STUCK (``failed``) with a real worktree."""
    stories = {STORY_KEY: {"status": "failed", "worktree": str(repo)}}
    path = plan_dir / f"{PLAN_NAME}.manifest.json"
    path.write_text(json.dumps({"stories": stories}))
    return stories


def _applicable_diff(repo: Path) -> str:
    """A diff that applies cleanly to *repo*."""
    return _unified(
        "app.py", (repo / "app.py").read_text(), "alpha\nbeta\ngamma\ndelta\n"
    )


def _record(repo: Path, plan_name: str = PLAN_NAME, story_key: str = STORY_KEY) -> dict:
    """Propose one patch the way the dashboard does and return its envelope."""
    diff_text = _applicable_diff(repo)
    return create_patch_record(plan_name, story_key, diff_text, ["app.py"], 1)


# --------------------------------------------------------------------------
# 1. a REFUSED patch still writes its proposal journal through the store
# --------------------------------------------------------------------------


def test_should_journal_a_refused_patch_through_the_record_store(
    repo: Path, manifest: dict, bus, recorder: _Recorder
) -> None:
    """A patch later REFUSED at apply carries exactly ONE journal entry.

    The audit that exists for a refused patch is the proposal entry: it was
    written when the patch was recorded.  The refusal itself adds nothing
    (the refuse-only rule of WAP-8), so the recorder holds exactly one record
    and it is the ``patch_proposed`` one, written through the record store
    under the ``default`` namespace with this plan and story.
    """
    # The apply is refused because the diff targets a deny-listed path --
    # a refusal decided after the record exists, so the proposal audit stands.
    diff_text = _unified("CLAUDE.md", "old\n", "new\n")
    proposed = create_patch_record(
        PLAN_NAME, STORY_KEY, diff_text, ["CLAUDE.md"], 1
    )

    refused = apply_patch(
        PLAN_NAME, STORY_KEY, proposed["patch_id"], proposed["confirmation_token"]
    )

    assert refused["ok"] is False, refused
    assert refused["status_code"] == 403, refused

    assert len(recorder.entries) == 1, recorder.entries
    namespace, plan_name, story_key, record = recorder.entries[0]
    assert namespace == "default"
    assert plan_name == PLAN_NAME
    assert story_key == STORY_KEY
    assert record["action"] == "patch_proposed"
    assert record["patch_id"] == proposed["patch_id"]
    assert record["paths"] == ["CLAUDE.md"]


# --------------------------------------------------------------------------
# 2. a successful apply adds its own journal entry through the store
# --------------------------------------------------------------------------


def test_should_journal_an_applied_patch_through_the_record_store(
    repo: Path, manifest: dict, bus, recorder: _Recorder
) -> None:
    """A SUCCESSFUL apply writes proposal + applied, both through the store.

    Success only: the second entry exists because the apply succeeded, and it
    carries the applied paths.  Both entries name the ``default`` namespace
    and this plan/story, and nothing was written anywhere else.
    """
    proposed = _record(repo)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, proposed["patch_id"], proposed["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert result["applied"] == ["app.py"], result

    assert len(recorder.entries) == 2, recorder.entries
    assert recorder.actions() == ["patch_proposed", "patch_applied"]
    for namespace, plan_name, story_key, _record_ in recorder.entries:
        assert namespace == "default"
        assert plan_name == PLAN_NAME
        assert story_key == STORY_KEY
    applied_entry = recorder.entries[1][3]
    assert applied_entry["patch_id"] == proposed["patch_id"]
    assert applied_entry["paths"] == ["app.py"]
    assert applied_entry["applied_paths"] == ["app.py"]


# --------------------------------------------------------------------------
# 3. nothing is journaled for a patch refused BEFORE its record exists
# --------------------------------------------------------------------------


def test_should_not_journal_when_the_patch_is_refused_before_the_record_is_built(
    repo: Path, manifest: dict, plan_dir: Path, bus, recorder: _Recorder
) -> None:
    """The refusal that precedes the record writes NO journal entry.

    Gate 4 of ``apply_patch`` is the patch record itself: a patch id the store
    does not know returns the fail-closed ``404 unknown patch`` BEFORE any
    journal site can be reached (both sites live behind an existing record).
    The recorder must stay empty - and stay empty of placeholders, not just of
    real records - and the next, fully successful call for a DIFFERENT story
    must journal normally, so a half-built record from the refused call can
    never be reused.
    """
    refused = apply_patch(PLAN_NAME, STORY_KEY, "wp-never-proposed", "irrelevant")

    assert refused == {
        "ok": False,
        "error": "unknown patch",
        "status_code": 404,
    }, refused
    assert recorder.entries == []

    # A follow-up, fully successful call for a different story still journals.
    other = "J-2"
    stories = dict(manifest)
    stories[other] = {"status": "failed", "worktree": str(repo)}
    (plan_dir / f"{PLAN_NAME}.manifest.json").write_text(json.dumps({"stories": stories}))

    proposed = create_patch_record(
        PLAN_NAME, other, _applicable_diff(repo), ["app.py"], 1
    )
    applied = apply_patch(
        PLAN_NAME, other, proposed["patch_id"], proposed["confirmation_token"]
    )

    assert applied["ok"] is True, applied
    assert recorder.actions() == ["patch_proposed", "patch_applied"]
    assert [entry[2] for entry in recorder.entries] == [other, other]
    assert recorder.entries[0][3]["patch_id"] == proposed["patch_id"]


# --------------------------------------------------------------------------
# 4. a raising journal write keeps the patch result and the audit intact
# --------------------------------------------------------------------------


def test_should_still_return_the_patch_result_when_the_journal_write_raises(
    repo: Path, manifest: dict, plan_dir: Path, bus, recorder: _Recorder
) -> None:
    """A failed journal write must not cost the patch its result.

    Today's handling, read off the code and preserved by the migration: the
    journal write is NOT wrapped in a ``try`` -- neither site has one, and
    ``pipeline.checkpoint`` only ever catches ``InvalidRecordKeyError`` around
    the same seam -- so a raising writer propagates exactly as it does now.
    What this test pins is that the patch RESULT survives it:

    * call 1 (``J-1``, writer raises ``RuntimeError("disk full")`` BEFORE
      recording): the apply already happened, so the patch record is
      ``applied`` with an ``applied_at`` stamp and the worktree change stands
      -- the result is not lost with the audit -- and NOTHING reached the
      journal: no entry, no ``None`` placeholder;
    * call 2 in the same test (``J-2``, writer healthy): the function resolves
      ``records()`` afresh, so it writes exactly ONE entry for ``J-2`` and
      never reuses the dead writer from call 1.
    """
    stories = dict(manifest)
    stories["J-2"] = {"status": "failed", "worktree": str(repo)}
    (plan_dir / f"{PLAN_NAME}.manifest.json").write_text(json.dumps({"stories": stories}))

    # --- call 1: the journal write fails -------------------------------
    first = create_patch_record(PLAN_NAME, "J-1", _applicable_diff(repo), ["app.py"], 1)
    recorder.fail_next = True

    with pytest.raises(RuntimeError, match="disk full"):
        apply_patch(PLAN_NAME, "J-1", first["patch_id"], first["confirmation_token"])

    assert recorder.failures == ["J-1"], recorder.failures
    # The proposal written before the failure stands; the failed audit write
    # added no entry and no placeholder of its own.
    assert recorder.actions() == ["patch_proposed"], recorder.entries
    assert [entry[2] for entry in recorder.entries] == ["J-1"]
    # The patch result itself survived the failed audit write.
    applied_record = worktree_patch.get_patch_record(first["patch_id"])
    assert applied_record is not None
    assert applied_record["status"] == "applied"
    assert applied_record["applied_at"]
    assert applied_record["paths"] == ["app.py"]

    # --- call 2: a healthy writer in the same test ---------------------
    # Call 1's apply really landed, so restore the committed file first: the
    # second diff is measured against the same context as the first.
    _git(repo, "checkout", "--", "app.py")
    second = create_patch_record(PLAN_NAME, "J-2", _applicable_diff(repo), ["app.py"], 1)
    result = apply_patch(
        PLAN_NAME, "J-2", second["patch_id"], second["confirmation_token"]
    )

    assert result["ok"] is True, result
    assert len(recorder.entries) == 3, recorder.entries
    assert recorder.actions() == ["patch_proposed", "patch_proposed", "patch_applied"]
    assert [entry[2] for entry in recorder.entries] == ["J-1", "J-2", "J-2"]
    assert recorder.entries[-1][3]["patch_id"] == second["patch_id"]
    assert all(entry[3] is not None for entry in recorder.entries)

