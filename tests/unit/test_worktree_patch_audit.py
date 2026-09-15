"""Tests for the patch AUDIT side effects (WAP-8).

This story EDITS the two existing functions in ``pipeline/worktree_patch.py``
-- it does not add new module-level functions and does not restructure them:

* ``create_patch_record`` -- after the record is stored, append ONE journal
  entry (``action == "patch_proposed"``) and publish ONE ``"notification"``
  bus event (``payload["kind"] == "patch_proposed"``).
* ``apply_patch`` -- after a SUCCESSFUL apply, the same two side effects with
  ``"patch_applied"`` and ``payload["applied_paths"]``.  A refused or failed
  apply emits nothing new.

The audit records carry the path list and the diff HASH only -- never the diff
body (Secure by Design: no sensitive payload in logs or events).

Scope note: ``__all__`` and ``EVENT_TYPES`` are shared artifacts that later
sibling stories may extend, so they are graded by MEMBERSHIP only, never by
exact contents.

Hermeticity: the git fixture is a real repository under pytest's ``tmp_path``,
resolved first, because on macOS the raw temp path can run through
``/var -> /private/var`` and a symlinked root component would make the write
resolver reject every path for the wrong reason.
"""

from __future__ import annotations

import difflib
import hashlib
import inspect
import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pipeline import event_wiring, events, persistence, worktree_patch
from pipeline.worktree_patch import apply_patch, create_patch_record

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PLAN_NAME = "wap8plan"
STORY_KEY = "WAP-8"

#: A string that appears ONLY inside the diff body.  If it ever shows up in a
#: journal file, an event payload or a log line, the audit leaked the patch.
MARKER = "UNIQUE_DIFF_MARKER_9c4f"

#: Captured at import time so the test's own git plumbing is never intercepted.
_REAL_RUN = subprocess.run

#: Public module-level functions that existed BEFORE this story.  WAP-8 adds
#: none, so the public surface must stay a SUBSET of this set.
_BASELINE_PUBLIC_FUNCTIONS = {
    "is_denied_relative_path",
    "resolve_write_target",
    "parse_unified_diff",
    "validate_for_propose",
    "create_patch_record",
    "get_patch_record",
    "apply_patch",
    # WAP-10 review fix (reviewer-gated, overlord ruling Option A): the
    # confirmation-token HMAC derivation is now a single public helper that
    # create_patch_record, apply_patch and the dashboard GET route all share.
    "derive_confirmation_token",
    "confirmation_token_for",
}

#: The exact key set the brief specifies for the PROPOSED journal entry.
_PROPOSED_JOURNAL_KEYS = {
    "action",
    "patch_id",
    "paths",
    "added_lines",
    "diff_hash",
    "ts",
}

#: The exact key set the brief specifies for the PROPOSED event payload.
_PROPOSED_PAYLOAD_KEYS = {"kind", "patch_id", "paths", "diff_hash"}


# --------------------------------------------------------------------------
# helpers
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


def _write_manifest(plan_dir: Path, stories: dict, plan_name: str = PLAN_NAME) -> Path:
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps({"stories": stories}))
    return path


def _journal_path(plan_name: str, story_key: str) -> Path:
    """The story's journal file, located via the persistence helper."""
    return persistence._journal_path(plan_name, story_key)


def _journal_entries(plan_name: str, story_key: str) -> list:
    """The parsed journal entries for the story (``[]`` when absent)."""
    path = _journal_path(plan_name, story_key)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _journal_text(plan_name: str, story_key: str) -> str:
    path = _journal_path(plan_name, story_key)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _record(diff_text: str, paths: list[str], added_lines: int = 1) -> dict:
    return create_patch_record(PLAN_NAME, STORY_KEY, diff_text, paths, added_lines)


def _diff_hash(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def _assert_utc_iso(value: object) -> None:
    assert isinstance(value, str) and value, value
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, value
    assert parsed.utcoffset() == timedelta(0), value


class _RecordingBus:
    """A bus double that records every published event and never dispatches."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


@pytest.fixture
def bus(monkeypatch) -> _RecordingBus:
    """Capture events published through the LAZY ``event_wiring.get_bus``.

    The implementation is required to import ``get_bus`` lazily inside the
    function (the ``pipeline/dispatch.py`` pattern), so patching the
    ``event_wiring`` module attribute is what the real code will see.  If a
    module-level binding also exists it is patched too, so the test measures
    the publish call rather than the import style.
    """
    recording = _RecordingBus()
    monkeypatch.setattr(event_wiring, "get_bus", lambda: recording)
    if hasattr(worktree_patch, "get_bus"):
        monkeypatch.setattr(worktree_patch, "get_bus", lambda: recording)
    return recording


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
    _write_manifest(plan_dir, stories)
    return stories


def _applicable_diff(repo: Path) -> str:
    """A diff that applies cleanly to *repo* and carries the unique marker."""
    return _unified(
        "app.py", (repo / "app.py").read_text(), f"alpha\n{MARKER}\ngamma\n"
    )


# --------------------------------------------------------------------------
# export surface / no-restructure guards
# --------------------------------------------------------------------------


def test_audited_functions_remain_exported():
    """Both edited functions stay in ``__all__`` (membership, not exact)."""
    assert "create_patch_record" in worktree_patch.__all__
    assert "apply_patch" in worktree_patch.__all__


def test_create_patch_record_signature_is_unchanged():
    params = list(inspect.signature(create_patch_record).parameters)
    assert params == ["plan_name", "story_key", "diff_text", "paths", "added_lines"]


def test_apply_patch_signature_is_unchanged():
    params = list(inspect.signature(apply_patch).parameters)
    assert params == ["plan_name", "story_key", "patch_id", "confirmation_token"]


def test_this_story_adds_no_new_public_function():
    """WAP-8 edits two existing functions; it adds no new module-level one."""
    public = {
        name
        for name, value in vars(worktree_patch).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and getattr(value, "__module__", None) == worktree_patch.__name__
    }
    assert public <= _BASELINE_PUBLIC_FUNCTIONS, public - _BASELINE_PUBLIC_FUNCTIONS


def test_notification_event_type_is_the_existing_one():
    """The audit reuses the EXISTING ``notification`` type (no new EVENT_TYPES)."""
    assert "notification" in events.EVENT_TYPES
    ev = events.make_event(
        "notification", PLAN_NAME, story_key=STORY_KEY, payload={}, correlation_id="x"
    )
    assert ev["type"] == "notification"
    assert ev["correlation_id"] == "x"


#: Every module-level function defined in ``pipeline/worktree_patch.py`` BEFORE
#: this story.  WAP-8 EDITS two of them and adds none.
_BASELINE_MODULE_FUNCTIONS = {
    "is_denied_relative_path",
    "resolve_write_target",
    "_c_unquote",
    "_consume_hunk_line",
    "parse_unified_diff",
    "validate_for_propose",
    "create_patch_record",
    "get_patch_record",
    "_run_git_apply",
    "apply_patch",
    # WAP-10 review fix (reviewer-gated, overlord ruling Option A): the
    # confirmation-token HMAC derivation is now a single public helper that
    # create_patch_record, apply_patch and the dashboard GET route all share.
    "derive_confirmation_token",
    "confirmation_token_for",
}


def _module_source() -> str:
    return inspect.getsource(worktree_patch)


def test_this_story_adds_no_new_module_level_function():
    """The brief forbids new module-level functions (private ones included)."""
    defined = {
        name
        for name, value in vars(worktree_patch).items()
        if inspect.isfunction(value)
        and getattr(value, "__module__", None) == worktree_patch.__name__
    }
    assert defined <= _BASELINE_MODULE_FUNCTIONS, defined - _BASELINE_MODULE_FUNCTIONS


def test_journal_appender_is_imported_from_persistence():
    """The journal appender is the EXISTING ``persistence._append_journal``.

    The brief pins the import to the ``pipeline/checkpoint.py`` pattern:
    ``from .persistence import _append_journal``.
    """
    source = _module_source()
    assert "from .persistence import _append_journal" in source, source
    assert "_append_journal(" in source


def test_event_publish_uses_the_lazy_dispatch_pattern():
    """Lazy ``from .event_wiring import get_bus`` + ``from .events import make_event``."""
    source = _module_source()
    assert "from .event_wiring import get_bus" in source, source
    assert "from .events import make_event" in source, source
    assert "get_bus().publish(" in source, source


def test_module_does_not_mutate_the_shared_event_type_registry():
    """No new EVENT_TYPES: the module never assigns to ``events.EVENT_TYPES``."""
    source = _module_source()
    assert "EVENT_TYPES =" not in source, source
    assert "EVENT_TYPES.add" not in source, source
    assert "EVENT_TYPES |=" not in source, source


# --------------------------------------------------------------------------
# create_patch_record: journal side effect
# --------------------------------------------------------------------------


def test_create_patch_record_appends_exactly_one_journal_entry(plan_dir, bus):
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", f"alpha\n{MARKER}\ngamma\n")
    rec = create_patch_record(PLAN_NAME, STORY_KEY, diff, ["app.py"], 1)

    entries = _journal_entries(PLAN_NAME, STORY_KEY)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["action"] == "patch_proposed"
    assert entry["patch_id"] == rec["patch_id"]
    assert entry["paths"] == ["app.py"]
    assert entry["added_lines"] == 1
    assert entry["diff_hash"] == _diff_hash(diff)
    _assert_utc_iso(entry["ts"])


def test_create_patch_record_journal_entry_has_exactly_the_specified_keys(
    plan_dir, bus
):
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", f"alpha\n{MARKER}\ngamma\n")
    create_patch_record(PLAN_NAME, STORY_KEY, diff, ["app.py"], 1)

    entry = _journal_entries(PLAN_NAME, STORY_KEY)[0]
    assert set(entry) == _PROPOSED_JOURNAL_KEYS, entry


def test_create_patch_record_journal_never_contains_the_diff_body(plan_dir, bus):
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", f"alpha\n{MARKER}\ngamma\n")
    create_patch_record(PLAN_NAME, STORY_KEY, diff, ["app.py"], 1)

    entry = _journal_entries(PLAN_NAME, STORY_KEY)[0]
    assert "diff_text" not in entry
    assert "diff_text" not in json.dumps(entry)
    assert MARKER not in json.dumps(entry)
    assert MARKER not in _journal_text(PLAN_NAME, STORY_KEY)


def test_create_patch_record_empty_paths_and_zero_added_lines(plan_dir, bus):
    """Boundary: an empty path list and a zero added-line count still audit."""
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", "alpha\nbeta\ngamma\n")
    rec = create_patch_record(PLAN_NAME, STORY_KEY, diff, [], 0)

    entries = _journal_entries(PLAN_NAME, STORY_KEY)
    assert len(entries) == 1, entries
    assert entries[0]["paths"] == []
    assert entries[0]["added_lines"] == 0
    assert entries[0]["patch_id"] == rec["patch_id"]

    assert len(bus.events) == 1, bus.events
    assert bus.events[0]["payload"]["paths"] == []


def test_create_patch_record_return_shape_is_unchanged(plan_dir, bus):
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", f"alpha\n{MARKER}\ngamma\n")
    rec = create_patch_record(PLAN_NAME, STORY_KEY, diff, ["app.py"], 1)

    assert rec["ok"] is True
    assert set(rec) >= {
        "ok",
        "patch_id",
        "paths",
        "added_lines",
        "confirmation_token",
        "diff_hash",
    }
    assert rec["paths"] == ["app.py"]
    assert rec["added_lines"] == 1
    assert rec["diff_hash"] == _diff_hash(diff)


# --------------------------------------------------------------------------
# create_patch_record: event side effect
# --------------------------------------------------------------------------


def test_create_patch_record_emits_one_notification_event(plan_dir, bus):
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", f"alpha\n{MARKER}\ngamma\n")
    rec = create_patch_record(PLAN_NAME, STORY_KEY, diff, ["app.py"], 1)

    assert len(bus.events) == 1, bus.events
    ev = bus.events[0]
    assert ev["type"] == "notification"
    assert ev["plan"] == PLAN_NAME
    assert ev["story_key"] == STORY_KEY
    assert ev["correlation_id"] == rec["patch_id"]
    assert ev["payload"]["kind"] == "patch_proposed"
    assert ev["payload"]["patch_id"] == rec["patch_id"]
    assert ev["payload"]["paths"] == ["app.py"]
    assert ev["payload"]["diff_hash"] == _diff_hash(diff)


def test_create_patch_record_event_payload_has_exactly_the_specified_keys(
    plan_dir, bus
):
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", f"alpha\n{MARKER}\ngamma\n")
    create_patch_record(PLAN_NAME, STORY_KEY, diff, ["app.py"], 1)

    assert set(bus.events[0]["payload"]) == _PROPOSED_PAYLOAD_KEYS, bus.events[0]


def test_create_patch_record_event_never_contains_the_diff_body(plan_dir, bus):
    diff = _unified("app.py", "alpha\nbeta\ngamma\n", f"alpha\n{MARKER}\ngamma\n")
    create_patch_record(PLAN_NAME, STORY_KEY, diff, ["app.py"], 1)

    blob = json.dumps(bus.events)
    assert MARKER not in blob
    assert "diff_text" not in blob


# --------------------------------------------------------------------------
# apply_patch: happy path audit
# --------------------------------------------------------------------------


def test_apply_patch_appends_one_patch_applied_journal_entry(
    plan_dir, repo, manifest, bus
):
    diff = _applicable_diff(repo)
    rec = _record(diff, ["app.py"], 1)
    bus.events.clear()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )
    assert result["ok"] is True, result

    entries = _journal_entries(PLAN_NAME, STORY_KEY)
    applied = [e for e in entries if e.get("action") == "patch_applied"]
    assert len(applied) == 1, entries
    entry = applied[0]
    assert entry["patch_id"] == rec["patch_id"]
    assert entry["paths"] == ["app.py"]
    assert entry["added_lines"] == 1
    assert entry["diff_hash"] == _diff_hash(diff)
    assert "diff_text" not in entry
    _assert_utc_iso(entry["ts"])
    # Same record shape as the proposed entry (an optional applied_paths is
    # tolerated, but nothing else may appear).
    assert _PROPOSED_JOURNAL_KEYS <= set(entry) <= _PROPOSED_JOURNAL_KEYS | {
        "applied_paths"
    }, entry


def test_apply_patch_emits_one_patch_applied_notification(plan_dir, repo, manifest, bus):
    diff = _applicable_diff(repo)
    rec = _record(diff, ["app.py"], 1)
    bus.events.clear()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )
    assert result["ok"] is True, result

    assert len(bus.events) == 1, bus.events
    ev = bus.events[0]
    assert ev["type"] == "notification"
    assert ev["plan"] == PLAN_NAME
    assert ev["story_key"] == STORY_KEY
    assert ev["correlation_id"] == rec["patch_id"]
    assert ev["payload"]["kind"] == "patch_applied"
    assert ev["payload"]["patch_id"] == rec["patch_id"]
    assert ev["payload"]["diff_hash"] == _diff_hash(diff)
    assert sorted(ev["payload"]["applied_paths"]) == ["app.py"]
    # The applied payload is the proposed payload PLUS applied_paths.
    assert set(ev["payload"]) == _PROPOSED_PAYLOAD_KEYS | {"applied_paths"}, ev["payload"]
    assert ev["payload"]["paths"] == ["app.py"]


def test_apply_patch_audit_never_carries_the_diff_body(plan_dir, repo, manifest, bus):
    diff = _applicable_diff(repo)
    rec = _record(diff, ["app.py"], 1)

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )
    assert result["ok"] is True, result

    assert MARKER not in _journal_text(PLAN_NAME, STORY_KEY)
    assert MARKER not in json.dumps(bus.events)
    assert "diff_text" not in json.dumps(bus.events)


# --------------------------------------------------------------------------
# apply_patch: refusals emit nothing new
# --------------------------------------------------------------------------


def test_refused_apply_token_mismatch_emits_nothing(plan_dir, repo, manifest, bus):
    diff = _applicable_diff(repo)
    rec = _record(diff, ["app.py"], 1)
    before = len(_journal_entries(PLAN_NAME, STORY_KEY))
    bus.events.clear()

    result = apply_patch(PLAN_NAME, STORY_KEY, rec["patch_id"], "not-the-token")

    assert result["ok"] is False, result
    assert result["error"] == "invalid confirmation token"
    assert result["status_code"] == 403
    assert len(_journal_entries(PLAN_NAME, STORY_KEY)) == before
    assert bus.events == []


def test_refused_apply_unknown_patch_emits_nothing(plan_dir, repo, manifest, bus):
    bus.events.clear()

    result = apply_patch(PLAN_NAME, STORY_KEY, "wp-does-not-exist", "whatever")

    assert result["ok"] is False, result
    assert result["error"] == "unknown patch"
    assert result["status_code"] == 404
    assert _journal_entries(PLAN_NAME, STORY_KEY) == []
    assert bus.events == []


def test_refused_apply_active_story_emits_nothing(plan_dir, repo, bus):
    _write_manifest(
        plan_dir, {STORY_KEY: {"status": "in_progress", "worktree": str(repo)}}
    )
    diff = _applicable_diff(repo)
    rec = _record(diff, ["app.py"], 1)
    before = len(_journal_entries(PLAN_NAME, STORY_KEY))
    bus.events.clear()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is False, result
    assert result["error"] == "story is active"
    assert result["status_code"] == 409
    assert len(_journal_entries(PLAN_NAME, STORY_KEY)) == before
    assert bus.events == []


def test_failed_git_apply_emits_nothing(plan_dir, repo, manifest, bus):
    """A diff that does not apply is refused -- and audited as nothing."""
    diff = _unified(
        "app.py",
        "totally\ndifferent\ncontent\n",
        f"totally\ndifferent\ncontent\n{MARKER}\n",
    )
    rec = _record(diff, ["app.py"], 1)
    before = len(_journal_entries(PLAN_NAME, STORY_KEY))
    bus.events.clear()

    result = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert result["ok"] is False, result
    assert len(_journal_entries(PLAN_NAME, STORY_KEY)) == before
    assert bus.events == []


def test_second_apply_is_refused_and_emits_nothing(plan_dir, repo, manifest, bus):
    """Single-use: the second apply is refused and adds no audit record."""
    diff = _applicable_diff(repo)
    rec = _record(diff, ["app.py"], 1)

    first = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )
    assert first["ok"] is True, first
    after_first = len(_journal_entries(PLAN_NAME, STORY_KEY))
    bus.events.clear()

    second = apply_patch(
        PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
    )

    assert second["ok"] is False, second
    assert second["error"] == "patch already applied"
    assert second["status_code"] == 409
    assert len(_journal_entries(PLAN_NAME, STORY_KEY)) == after_first
    assert bus.events == []


# --------------------------------------------------------------------------
# Secure by Design: no diff content in logs
# --------------------------------------------------------------------------


def test_no_log_statement_carries_the_diff_body(plan_dir, repo, manifest, bus, caplog):
    diff = _applicable_diff(repo)
    with caplog.at_level(logging.DEBUG, logger="pipeline.worktree_patch"):
        rec = _record(diff, ["app.py"], 1)
        result = apply_patch(
            PLAN_NAME, STORY_KEY, rec["patch_id"], rec["confirmation_token"]
        )
    assert result["ok"] is True, result

    text = "\n".join(
        f"{record.getMessage()} {record.args!r}" for record in caplog.records
    )
    assert MARKER not in text
    assert "diff_text" not in text
