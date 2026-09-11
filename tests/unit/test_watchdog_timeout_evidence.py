"""TDD spec: the dispatch_watchdog_timeout journal entry gains completion
evidence (last_log_line, branch_commit_count, agent_done_marker).

Background (PLANREFRESH-1 follow-up): the stale-activity watchdog terminated
a dispatch whose run had in fact FINISHED — its last agent.log line was
"[step 2] ORACLE GREEN — acceptance tests pass; committed & done.", its
commits were all on the branch, and the suite was green — but it never wrote
.agent_done and never exited. The journal entry it wrote carried no evidence
of that, so the completed work was discarded as 'interrupted'
indistinguishably from a genuinely stalled run.

This story does NOT change the watchdog's decision or the resulting status.
It only adds three fields to the dispatch_watchdog_timeout journal entry,
alongside the existing ones:

  * last_log_line:       the last non-empty line of <worktree>/agent.log,
                         truncated to 300 characters
  * branch_commit_count: commits on the story's branch ahead of the default
                         branch, as an int
  * agent_done_marker:   whether <worktree>/.agent_done exists (true/false)

FAIL-OPEN CONTRACT (graded throughout):
  every lookup is wrapped INDIVIDUALLY; a missing worktree, an unreadable
  log, a git command that errors or times out, or any exception at all
  leaves that field absent (or null) and lets the termination proceed
  exactly as today. One failing lookup must not suppress the other two.
  The git call is bounded by an explicit timeout. The evidence goes to the
  journal entry ONLY — not the return value, not the summary text, not
  ERROR logs, and no worktree absolute paths anywhere user-facing.

The journal entry is a CUMULATIVE artifact: these tests assert MEMBERSHIP
and individual field values only — never the entry's exact key set, exact
length, or a full-dict equality — so later stories can add more fields.

REBINDING TRAP (same as test_story_status_activity_watchdog.py):
check_story_status is rebound via types.FunctionType against
pipeline.server's namespace, so every bare name in its body resolves there
at call time — the thresholds, _store, subprocess, _terminate_and_checkpoint,
_rebrief_step_cap_struggle. Threshold/rebrief/ps stubs therefore go on
``p`` (pipeline.server). The journal entry itself is written by the REAL
pipeline.checkpoint._terminate_and_checkpoint (deliberately NOT stubbed —
the evidence must flow through it), whose module-level _append_journal and
_commit_wip names resolve against pipeline.checkpoint; those two are
stubbed there. The real _terminate_and_checkpoint SIGTERMs the story pid,
so every story below carries a real live pid: a throwaway sleeper
subprocess, never the pytest process.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Import server BEFORE story_status: story_status rebinds check_story_status
# against pipeline.server's namespace, and importing story_status first trips
# the module-level import cycle (story_status <-> server).
from pipeline import server as p
from pipeline import story_status

REPO_ROOT = Path(__file__).resolve().parents[2]
STORY_STATUS_SRC = REPO_ROOT / "pipeline" / "story_status.py"
CHECKPOINT_SRC = REPO_ROOT / "pipeline" / "checkpoint.py"

# Stubbed thresholds — resolution must never depend on configured values.
WATCHDOG_SECONDS = 3600
STALE_SECONDS = 1800

# agent.log mtime this far in the past -> activity_age > STALE_SECONDS, so
# the stale-activity watchdog branch triggers (same shape the existing
# test_story_status_activity_watchdog.py uses).
LOG_AGE_SECONDS = 4000
# dispatched_at this far in the past -> elapsed > WATCHDOG_SECONDS, so the
# wall-clock backstop branch fires when there is no agent.log at all.
DISPATCHED_SECONDS_AGO = 7200

MAX_LOG_LINE = 300
STORY_KEY = "story-1"
STUB_SHA = "c" * 40

ORACLE_LINE = "[step 2] ORACLE GREEN — acceptance tests pass; committed & done."

STALE_SUMMARY_RE = (
    r"no activity for (\d+)s \(stale-activity watchdog\); "
    r"elapsed (\d+)s; process terminated\."
)

EVIDENCE_FIELDS = ("last_log_line", "branch_commit_count", "agent_done_marker")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _make_story(worktree, pid, *, log_text=None, agent_done=False,
                make_worktree=True):
    """Build an in_progress story with a live pid and a stale dispatched_at.

    ``log_text`` writes <worktree>/agent.log with that content and an mtime
    LOG_AGE_SECONDS in the past (so the stale-activity branch triggers).
    ``make_worktree=False`` leaves the worktree directory nonexistent.
    """
    worktree = Path(worktree)
    if make_worktree:
        worktree.mkdir(parents=True, exist_ok=True)
    story = {
        "status": "in_progress",
        "pid": pid,
        "dispatched_at": (
            datetime.now(timezone.utc) - timedelta(seconds=DISPATCHED_SECONDS_AGO)
        ).isoformat(),
        "worktree": str(worktree),
    }
    if log_text is not None:
        agent_log = worktree / "agent.log"
        agent_log.write_text(log_text, encoding="utf-8")
        mtime = time.time() - LOG_AGE_SECONDS
        os.utime(agent_log, (mtime, mtime))
    if agent_done:
        (worktree / ".agent_done").write_text("", encoding="utf-8")
    return story


def _write_manifest(plan_dir, plan_name, story):
    """Write a real manifest for the store to read from the patched PLAN_DIR."""
    manifest = {
        "name": plan_name,
        "stories": {STORY_KEY: story},
        "role_config": {},
    }
    manifest_path = p._store.manifest_path(plan_name)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_git_worktree(tmp_path):
    """Smallest real git fixture for branch_commit_count.

    upstream: one commit on main; worktree: a clone of it with a story branch
    carrying two more commits -> exactly 2 commits ahead of the default
    branch, however the implementation resolves "default branch" (local
    ``main``, ``origin/main``, ``origin/HEAD`` or ``@{upstream}``).
    """
    upstream = tmp_path / "evidence-upstream"
    _git(["init", str(upstream)], cwd=tmp_path)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=upstream)
    (upstream / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "."], cwd=upstream)
    _git(["commit", "-m", "base"], cwd=upstream)
    worktree = tmp_path / "evidence-story-wt"
    _git(["clone", str(upstream), str(worktree)], cwd=tmp_path)
    _git(["checkout", "-b", "story-branch"], cwd=worktree)
    _git(["branch", "--set-upstream-to=origin/main"], cwd=worktree)
    for name, text in (("one.txt", "one\n"), ("two.txt", "two\n")):
        (worktree / name).write_text(text, encoding="utf-8")
        _git(["add", "."], cwd=worktree)
        _git(["commit", "-m", name], cwd=worktree)
    return worktree


def _watchdog_entry(journal_records):
    """Return THE dispatch_watchdog_timeout journal record (membership only:
    the entry is cumulative, so its exact key set is never asserted)."""
    entries = [
        record for record in journal_records
        if record.get("step") == "dispatch_watchdog_timeout"
    ]
    assert len(entries) == 1, (
        "expected exactly one dispatch_watchdog_timeout journal entry, "
        f"got: {entries!r}")
    return entries[0]


def _assert_absent_or_null(entry, field):
    assert entry.get(field) is None, (
        f"{field} must be absent (or null) when its lookup fails; "
        f"got {entry.get(field)!r} in entry {entry!r}")


def _assert_int_field(entry, field, expected):
    value = entry.get(field)
    assert isinstance(value, int) and not isinstance(value, bool), (
        f"{field} must be recorded as an int; got {value!r} "
        f"in entry {entry!r}")
    assert value == expected, (
        f"{field}: expected {expected!r}, got {value!r} in entry {entry!r}")


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
@pytest.fixture()
def watchdog_env(tmp_path, monkeypatch):
    """Redirect PLAN_DIR at pipeline.server, stub the thresholds on
    pipeline.server (the rebound body resolves them there at call time), and
    stub only the true external boundaries.

    The REAL pipeline.checkpoint._terminate_and_checkpoint runs (the evidence
    must flow through it); only its _commit_wip (git commit of the worktree)
    and _append_journal (the journal write, spied) boundaries are stubbed, on
    pipeline.checkpoint where its body resolves them. It SIGTERMs the story
    pid, so stories use a throwaway live sleeper pid, never os.getpid().
    """
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_name = f"plan-alpha-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(p, "DISPATCH_WATCHDOG_SECONDS", WATCHDOG_SECONDS)
    monkeypatch.setattr(p, "DISPATCH_STALE_ACTIVITY_SECONDS", STALE_SECONDS)
    monkeypatch.setattr(p, "_rebrief_step_cap_struggle", lambda *a, **k: None)

    real_run = subprocess.run

    def _fake_run(cmd, *args, **kwargs):
        # Only the liveness `ps` probe is stubbed; every other subprocess
        # call — including the evidence git invocation under test — runs
        # for real.
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "ps":
            return types.SimpleNamespace(stdout=" S", returncode=0)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    journal_records = []

    def _spy_append_journal(plan_name_, story_key_, record, *args, **kwargs):
        journal_records.append(dict(record))

    monkeypatch.setattr(
        "pipeline.checkpoint._append_journal", _spy_append_journal)
    monkeypatch.setattr(
        "pipeline.checkpoint._commit_wip", lambda *args, **kwargs: STUB_SHA)

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        yield types.SimpleNamespace(
            plan_dir=plan_dir,
            plan_name=plan_name,
            pid=sleeper.pid,
            journal_records=journal_records,
        )
    finally:
        sleeper.kill()
        sleeper.wait()


# --------------------------------------------------------------------------
# positive cases
# --------------------------------------------------------------------------
def test_last_log_line_records_last_non_empty_line_verbatim(
        watchdog_env, tmp_path):
    """Case 1: a readable agent.log records its last non-empty line,
    verbatim, as last_log_line."""
    worktree = tmp_path / "wt"
    story = _make_story(
        worktree, watchdog_env.pid, log_text=f"step 1 ok\n{ORACLE_LINE}\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    assert entry.get("last_log_line") == ORACLE_LINE, (
        "last_log_line must be the last non-empty agent.log line verbatim; "
        f"entry: {entry!r}")


def test_branch_commit_count_uses_real_git_ahead_of_default_branch(
        watchdog_env, tmp_path):
    """Case 2: with a real git fixture (base commit on main, story branch
    with two more commits) branch_commit_count is the int 2."""
    worktree = _make_git_worktree(tmp_path)
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"{ORACLE_LINE}\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    _assert_int_field(entry, "branch_commit_count", 2)


def test_agent_done_marker_true_when_marker_present(watchdog_env, tmp_path):
    """Case 3a: .agent_done present in the worktree -> agent_done_marker is
    exactly True."""
    worktree = tmp_path / "wt"
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"{ORACLE_LINE}\n", agent_done=True)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    assert entry.get("agent_done_marker") is True, (
        f"agent_done_marker must be true when .agent_done exists; "
        f"entry: {entry!r}")


def test_agent_done_marker_false_when_marker_absent(watchdog_env, tmp_path):
    """Case 3b: worktree present but no .agent_done -> agent_done_marker is
    exactly False (not absent, not null)."""
    worktree = tmp_path / "wt"
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"{ORACLE_LINE}\n", agent_done=False)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    assert entry.get("agent_done_marker") is False, (
        f"agent_done_marker must be false when .agent_done is missing; "
        f"entry: {entry!r}")


# --------------------------------------------------------------------------
# negative / boundary cases
# --------------------------------------------------------------------------
def test_missing_worktree_fields_absent_entry_written_story_interrupted(
        watchdog_env, tmp_path):
    """Case 4: missing worktree directory — all three evidence fields absent
    or null, the journal entry still written, and the story still set to
    'interrupted' exactly as today."""
    missing = tmp_path / "no-such-worktree"
    story = _make_story(missing, watchdog_env.pid, make_worktree=False)
    manifest_path = _write_manifest(
        watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    assert result.get("watchdog_killed") is True
    assert result.get("pid") == watchdog_env.pid
    entry = _watchdog_entry(watchdog_env.journal_records)
    for field in EVIDENCE_FIELDS:
        _assert_absent_or_null(entry, field)
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["stories"][STORY_KEY]["status"] == "interrupted"


def test_missing_agent_log_last_log_line_absent_other_fields_populated(
        watchdog_env, tmp_path):
    """Case 5: agent.log missing -> last_log_line absent, while
    branch_commit_count (real git repo -> 2) and agent_done_marker (False)
    are still populated."""
    worktree = _make_git_worktree(tmp_path)
    story = _make_story(worktree, watchdog_env.pid, log_text=None)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    _assert_absent_or_null(entry, "last_log_line")
    _assert_int_field(entry, "branch_commit_count", 2)
    assert entry.get("agent_done_marker") is False


def test_trailing_blank_lines_last_non_empty_line_chosen(
        watchdog_env, tmp_path):
    """Case 6a: agent.log ending in several blank lines -> the last
    NON-EMPTY line is recorded."""
    worktree = tmp_path / "wt"
    story = _make_story(worktree, watchdog_env.pid,
                        log_text="alpha\nbeta\n\n\n\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    assert entry.get("last_log_line") == "beta", (
        "trailing blank lines must be skipped; the last non-empty line is "
        f"recorded; entry: {entry!r}")


def test_empty_agent_log_last_log_line_absent(watchdog_env, tmp_path):
    """Case 6b: a wholly empty agent.log -> last_log_line absent (not an
    empty string), while agent_done_marker is still populated."""
    worktree = tmp_path / "wt"
    story = _make_story(worktree, watchdog_env.pid, log_text="")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    _assert_absent_or_null(entry, "last_log_line")
    assert entry.get("agent_done_marker") is False


def test_blank_lines_only_agent_log_last_log_line_absent(
        watchdog_env, tmp_path):
    """Case 6c boundary: agent.log containing only blank lines has no
    non-empty line -> last_log_line absent."""
    worktree = tmp_path / "wt"
    story = _make_story(worktree, watchdog_env.pid, log_text="\n\n\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    _assert_absent_or_null(entry, "last_log_line")


def test_log_line_longer_than_300_chars_truncated_to_300(
        watchdog_env, tmp_path):
    """Case 7: a 500-character last line is truncated to exactly 300
    characters (its first 300)."""
    worktree = tmp_path / "wt"
    long_line = "x" * 500
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"step 1 ok\n{long_line}\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    recorded = entry.get("last_log_line")
    assert isinstance(recorded, str), (
        f"last_log_line must be a string; entry: {entry!r}")
    assert len(recorded) == MAX_LOG_LINE, (
        f"a log line longer than {MAX_LOG_LINE} characters must be "
        f"truncated to {MAX_LOG_LINE}; got {len(recorded)}")
    assert recorded == long_line[:MAX_LOG_LINE]


def test_log_line_exactly_300_chars_recorded_verbatim(
        watchdog_env, tmp_path):
    """Case 7 boundary: a line of exactly 300 characters is recorded
    verbatim, not truncated."""
    worktree = tmp_path / "wt"
    line = "y" * MAX_LOG_LINE
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"step 1 ok\n{line}\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    assert entry.get("last_log_line") == line, (
        f"a line of exactly {MAX_LOG_LINE} characters must be recorded "
        f"verbatim; entry: {entry!r}")


def test_git_failure_branch_commit_count_absent_no_exception_escapes(
        watchdog_env, tmp_path):
    """Case 8: the worktree is a plain (non-repo) directory -> the git
    command fails; branch_commit_count is absent, no exception escapes, the
    other two lookups still land, and termination still proceeds."""
    worktree = tmp_path / "plain-dir"
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"{ORACLE_LINE}\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    _assert_absent_or_null(entry, "branch_commit_count")
    # individually wrapped: the failing git lookup must not suppress the
    # other two evidence fields
    assert entry.get("last_log_line") == ORACLE_LINE
    assert entry.get("agent_done_marker") is False


def test_unreadable_agent_log_fails_open(watchdog_env, tmp_path):
    """Fail-open: an unreadable agent.log (a directory where the log should
    be) leaves last_log_line absent without disturbing the termination or
    the other two lookups."""
    worktree = _make_git_worktree(tmp_path)
    agent_log = worktree / "agent.log"
    agent_log.mkdir()
    mtime = time.time() - LOG_AGE_SECONDS
    os.utime(agent_log, (mtime, mtime))
    story = _make_story(worktree, watchdog_env.pid, log_text=None)
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    _assert_absent_or_null(entry, "last_log_line")
    _assert_int_field(entry, "branch_commit_count", 2)
    assert entry.get("agent_done_marker") is False


# --------------------------------------------------------------------------
# contract guards
# --------------------------------------------------------------------------
def test_existing_fields_step_summary_commit_unchanged(
        watchdog_env, tmp_path):
    """Case 9: the entry's existing contract is intact — step, summary and
    commit keep today's presence and shape (membership only: the entry is
    cumulative and later stories may add more fields)."""
    worktree = tmp_path / "wt"
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"{ORACLE_LINE}\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    result = story_status.check_story_status(
        watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    entry = _watchdog_entry(watchdog_env.journal_records)
    assert entry.get("step") == "dispatch_watchdog_timeout"
    summary = entry.get("summary")
    assert isinstance(summary, str) and summary, (
        f"summary must remain a non-empty string; entry: {entry!r}")
    match = re.search(STALE_SUMMARY_RE, summary)
    assert match, f"summary lost the stale-activity shape: {summary!r}"
    commit = entry.get("commit")
    assert isinstance(commit, str) and commit, (
        f"commit must remain a non-empty string; entry: {entry!r}")


def test_evidence_stays_out_of_user_facing_surfaces(
        watchdog_env, tmp_path, caplog):
    """The log line and the worktree absolute path go to the journal entry
    ONLY: not in check_story_status's return value, not in the user-facing
    summary text, and never logged at ERROR."""
    worktree = tmp_path / "wt"
    story = _make_story(worktree, watchdog_env.pid,
                        log_text=f"{ORACLE_LINE}\n")
    _write_manifest(watchdog_env.plan_dir, watchdog_env.plan_name, story)

    with caplog.at_level(logging.ERROR):
        result = story_status.check_story_status(
            watchdog_env.plan_name, STORY_KEY)

    assert result["status"] == "interrupted"
    blob = json.dumps(result)
    assert ORACLE_LINE not in blob
    assert str(worktree) not in blob
    for field in EVIDENCE_FIELDS:
        assert field not in result, (
            f"the evidence belongs in the journal entry only, not the "
            f"return value; found {field!r} in {result!r}")
    entry = _watchdog_entry(watchdog_env.journal_records)
    summary = entry.get("summary") or ""
    assert ORACLE_LINE not in summary
    assert str(worktree) not in summary
    for record in caplog.records:
        assert ORACLE_LINE not in record.getMessage(), (
            "the agent.log line must not be logged at ERROR: "
            f"{record.getMessage()!r}")


def test_git_commit_count_call_is_bounded_by_explicit_timeout():
    """The branch_commit_count git invocation must carry an explicit
    timeout= so a hung git fails open instead of stalling the watchdog, in
    pipeline/story_status.py or pipeline/checkpoint.py."""
    for src in (STORY_STATUS_SRC, CHECKPOINT_SRC):
        text = src.read_text(encoding="utf-8")
        for match in re.finditer(r"[\"']git[\"']", text):
            window = text[max(0, match.start() - 300):match.start() + 600]
            if (re.search(r"timeout\s*=", window)
                    and re.search(r"subprocess\.|Popen", window)):
                return
    pytest.fail(
        "no git subprocess invocation bounded by an explicit timeout= was "
        "found in pipeline/story_status.py or pipeline/checkpoint.py — "
        "bound the branch_commit_count git call with an explicit timeout "
        "(fail open on timeout)")


# --------------------------------------------------------------------------
# regression: consumed done-marker (review blocking finding, LOCKSTARVE-D2)
# --------------------------------------------------------------------------
def test_agent_done_marker_true_when_only_consumed_marker_present(tmp_path):
    """Regression: a FINISHED run whose .agent_done was already consumed
    must still yield agent_done_marker True.

    pipeline.watchers.scan_done_markers renames .agent_done to
    .agent_done.consumed immediately after publishing the done event, so in
    production the watchdog almost always observes ONLY the consumed name —
    the fresh `.agent_done` state is transient. pipeline.wedge_io already
    defines the correct invariant for this exact question: finished iff
    `.agent_done` OR `.agent_done.consumed` exists. _agent_done_marker must
    mirror it; otherwise the journal records agent_done_marker: False for a
    run that actually finished — systematically wrong, and worse than
    omitting the field.
    """
    # Local import: this case targets pipeline.checkpoint's evidence helpers
    # directly and must not depend on the rebound story_status machinery.
    from pipeline.checkpoint import _agent_done_marker, _watchdog_evidence

    worktree = tmp_path / "wt"
    # pid=0 is never signalled: only the pure evidence helpers run here (no
    # _terminate_and_checkpoint), so no live sleeper pid is needed.
    story = _make_story(worktree, 0, log_text="step 2 finished\n")
    # The exact on-disk state scan_done_markers leaves behind after
    # consuming: .agent_done gone, .agent_done.consumed present.
    (worktree / ".agent_done.consumed").write_text("", encoding="utf-8")
    assert not (worktree / ".agent_done").exists(), (
        "fixture error: this case must contain ONLY the consumed marker")

    assert _agent_done_marker(worktree) is True, (
        "a worktree whose .agent_done was consumed (renamed to "
        ".agent_done.consumed by scan_done_markers) is a finished run; "
        "_agent_done_marker must return True for it, matching wedge_io's "
        ".agent_done OR .agent_done.consumed invariant")

    evidence = _watchdog_evidence(story, STORY_KEY)
    assert evidence.get("agent_done_marker") is True, (
        "the watchdog evidence must record agent_done_marker True for a "
        f"finished run whose marker was consumed; got {evidence!r}")
