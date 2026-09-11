"""Tests for the claim-before-publish reorder in pipeline.watchers.scan_done_markers.

Written FIRST (TDD). The reorder described in the plan (read -> claim via
os.replace -> publish) is NOT implemented yet, so the ordering/race tests in
this file are expected to be RED until a later dispatch makes the rename the
claim. The happy-path and parse-guard tests are end-state guards that should
already be green and must stay green after the reorder.

Background: the bus is in-process and synchronous, and bus.publish can run for
minutes while holding the plan lock. Two scheduler scan passes can legitimately
be in flight at once (the watchdog abandons a slow worker thread without
killing it), so both can see the same unconsumed marker. Making os.replace the
claim means only the pass that wins the rename ever publishes; the loser sees
FileNotFoundError from the replace and must continue quietly WITHOUT
publishing.
"""

import ast
import errno
import inspect
import json
import logging
import os
import textwrap

from pipeline import watchers

PLAN = "plan-claim"


class FakeBus:
    """A minimal bus that records every published event."""

    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


class ClaimAssertingBus:
    """A bus whose publish() asserts the marker was ALREADY claimed.

    "Claimed" means the os.replace from .agent_done to .agent_done.consumed
    has already happened when publish is called. This is the ordering pin:
    against the current (publish-then-rename) code the first assertion fails,
    because the marker is still on disk under its original name at publish
    time.
    """

    def __init__(self, worktree):
        self.worktree = worktree
        self.published = []

    def publish(self, event):
        marker = self.worktree / ".agent_done"
        consumed = self.worktree / ".agent_done.consumed"
        assert not marker.exists(), (
            "bus.publish was called before the marker was claimed: "
            f"{marker} still exists at publish time"
        )
        assert consumed.exists(), (
            "bus.publish was called before the marker was claimed: "
            f"{consumed} does not exist at publish time"
        )
        self.published.append(event)


def _marker_path(worktree):
    return worktree / ".agent_done"


def _consumed_path(worktree):
    return worktree / ".agent_done.consumed"


def _write_marker(worktree, payload):
    _marker_path(worktree).write_text(json.dumps(payload))


def _manifest(stories):
    return {"plan": PLAN, "stories": dict(stories)}


def _one_story_manifest(worktree):
    return _manifest({"S-1": {"status": "in_progress", "worktree": str(worktree)}})


# ---------------------------------------------------------------------------
# Positive
# ---------------------------------------------------------------------------

def test_happy_path_publishes_one_event_and_leaves_consumed_marker(tmp_path):
    """The reorder must not break the normal single-pass happy path."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    payload = {"exit_code": 0, "note": "done"}
    _write_marker(worktree, payload)

    bus = FakeBus()
    result = watchers.scan_done_markers(_one_story_manifest(worktree), PLAN, bus)

    assert len(bus.published) == 1
    assert result == bus.published
    ev = bus.published[0]
    assert ev["type"] == "agent_done"
    assert ev["plan"] == PLAN
    assert ev["story_key"] == "S-1"
    assert ev["payload"] == payload
    # End state: marker consumed.
    assert not _marker_path(worktree).exists()
    assert _consumed_path(worktree).exists()


def test_marker_is_claimed_renamed_before_publish(tmp_path):
    """os.replace must run BEFORE bus.publish: the rename is the claim.

    RED against the current code (publish at line ~95, replace at line ~99):
    the ClaimAssertingBus assertion fires inside publish because the marker is
    still on disk under its original name.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    bus = ClaimAssertingBus(worktree)
    result = watchers.scan_done_markers(_one_story_manifest(worktree), PLAN, bus)

    assert len(bus.published) == 1
    assert result == bus.published
    assert not _marker_path(worktree).exists()
    assert _consumed_path(worktree).exists()


def test_scan_done_markers_calls_os_replace_before_bus_publish():
    """Structural pin: inside scan_done_markers the os.replace call site
    appears at a lower line number than the bus.publish call site."""
    source = textwrap.dedent(inspect.getsource(watchers.scan_done_markers))
    tree = ast.parse(source)
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)

    replace_lines = []
    publish_lines = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "replace" and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
            replace_lines.append(node.lineno)
        elif node.func.attr == "publish":
            publish_lines.append(node.lineno)

    assert replace_lines, "scan_done_markers must call os.replace"
    assert publish_lines, "scan_done_markers must call bus.publish"
    assert min(replace_lines) < min(publish_lines), (
        "os.replace (the claim) must appear before bus.publish in "
        "scan_done_markers; got replace at "
        f"{min(replace_lines)} and publish at {min(publish_lines)}"
    )


def test_docstring_notes_describe_claim_before_publish():
    """The docstring must no longer claim the rename happens after publishing."""
    doc = inspect.getdoc(watchers.scan_done_markers) or ""
    assert "After successfully publishing an event" not in doc, (
        "scan_done_markers docstring still says the rename happens after "
        "publishing; it must describe the new claim-before-publish order"
    )
    assert ".agent_done.consumed" in doc
    lowered = doc.lower()
    assert "rename" in lowered or "claim" in lowered
    assert "publish" in lowered


# ---------------------------------------------------------------------------
# Negative / boundary
# ---------------------------------------------------------------------------

def test_lost_race_file_not_found_publishes_nothing(tmp_path, monkeypatch):
    """FileNotFoundError from os.replace means another pass already claimed
    the marker: continue WITHOUT publishing and without raising."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    real_replace = os.replace

    def losing_replace(src, dst, **kwargs):
        if str(src).endswith(".agent_done"):
            raise FileNotFoundError(
                errno.ENOENT, "No such file or directory", str(src)
            )
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", losing_replace)

    bus = FakeBus()
    # Must not raise.
    result = watchers.scan_done_markers(_one_story_manifest(worktree), PLAN, bus)

    assert bus.published == []
    assert result == []


def test_lost_race_is_quiet_no_error_logged(tmp_path, monkeypatch, caplog):
    """The losing racer is an expected, benign outcome: it must log at DEBUG,
    not emit an ERROR record (the 103 tracebacks in the error log are exactly
    the noise this removes)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    real_replace = os.replace

    def losing_replace(src, dst, **kwargs):
        if str(src).endswith(".agent_done"):
            raise FileNotFoundError(
                errno.ENOENT, "No such file or directory", str(src)
            )
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", losing_replace)

    bus = FakeBus()
    with caplog.at_level(logging.DEBUG, logger="pipeline.watchers"):
        watchers.scan_done_markers(_one_story_manifest(worktree), PLAN, bus)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], (
        "a lost race (FileNotFoundError on the claim) must not be logged at "
        "ERROR level; got: "
        + "; ".join(r.getMessage() for r in errors)
    )
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debugs, "the lost-race outcome should be logged at DEBUG level"


def test_other_os_error_permission_denied_publishes_nothing_but_logs_error(
    tmp_path, monkeypatch, caplog
):
    """A non-FileNotFoundError OSError on the claim must NOT publish, must not
    raise, and must be logged at ERROR with the existing 'Failed to rename'
    message."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    real_replace = os.replace

    def denying_replace(src, dst, **kwargs):
        if str(src).endswith(".agent_done"):
            raise PermissionError(
                errno.EACCES, "Permission denied", str(src)
            )
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", denying_replace)

    bus = FakeBus()
    # Must not raise.
    result = watchers.scan_done_markers(_one_story_manifest(worktree), PLAN, bus)

    assert bus.published == []
    assert result == []

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "an unexpected OSError on the claim must be logged at ERROR"
    assert any(
        "Failed to rename" in r.getMessage() for r in error_records
    ), "the ERROR log must keep the existing 'Failed to rename' message"


def test_malformed_marker_left_on_disk_and_never_claimed(tmp_path, caplog):
    """The parse guard must run BEFORE the claim: a non-JSON marker is left
    under its original name, unrenamed, and nothing is published."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _marker_path(worktree).write_text("{ this is not valid json")

    bus = FakeBus()
    with caplog.at_level(logging.WARNING, logger="pipeline.watchers"):
        result = watchers.scan_done_markers(_one_story_manifest(worktree), PLAN, bus)

    assert result == []
    assert bus.published == []
    assert _marker_path(worktree).exists()
    assert not _consumed_path(worktree).exists()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_non_dict_json_payload_left_on_disk_and_never_claimed(tmp_path, caplog):
    """A marker whose JSON payload is not a dict (e.g. a list) is skipped by
    the existing validation guard before any claim is attempted."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _marker_path(worktree).write_text(json.dumps([1, 2]))

    bus = FakeBus()
    with caplog.at_level(logging.WARNING, logger="pipeline.watchers"):
        result = watchers.scan_done_markers(_one_story_manifest(worktree), PLAN, bus)

    assert result == []
    assert bus.published == []
    assert _marker_path(worktree).exists()
    assert not _consumed_path(worktree).exists()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_two_sequential_scans_publish_exactly_one_event(tmp_path):
    """After the first scan claims the marker, a second pass over the same
    manifest finds nothing to claim: exactly one event in total."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_marker(worktree, {"exit_code": 0})

    manifest = _one_story_manifest(worktree)
    bus = FakeBus()

    first = watchers.scan_done_markers(manifest, PLAN, bus)
    second = watchers.scan_done_markers(manifest, PLAN, bus)

    assert len(first) == 1
    assert second == []
    assert len(bus.published) == 1
    assert not _marker_path(worktree).exists()
    assert _consumed_path(worktree).exists()