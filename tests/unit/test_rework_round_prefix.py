"""Tests for the rework round-number prefix added to the IMPLEMENTER-facing
prompt when a story has been routed back to rework two or more times
(``story['rework_attempts'] >= 2``).

Background: ``rework_attempts`` is incremented on the review_story path (and
the merge-CI-fail router) *before* the story is redispatched. So the FIRST
rework redispatch carries ``rework_attempts == 1`` and must keep today's
wording byte-for-byte; only the second-and-later redispatch
(``rework_attempts >= 2``) gets a one-sentence round marker prefix.

Two sites build the rework instruction text:
  1. ``pipeline.persona._build_dispatch_command`` -- the ``rework_instruction``
     block, built from the ``review_feedback`` parameter while ``story`` (which
     carries ``rework_attempts``) is in scope as the first positional arg.
  2. ``pipeline.server._dispatch_story_impl``'s local-backend
     transcript-resume path, which sets ``dispatch_kwargs['resume_append_content']``
     from ``review_feedback`` (and ``fix_checklist`` when present).

These tests assert:
  * round 1 -> today's wording unchanged (no round marker);
  * round >=2 -> the round number + previous-attempt note is prefixed;
  * the prefix is read from ``story['rework_attempts']`` directly inside
    ``_build_dispatch_command`` (no new parameter added);
  * the ``_run_rework_planner`` fix_checklist mechanism is untouched.
"""

import json

import pytest

from app import backend
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import persona as pper
from pipeline import server as p
from pipeline import ticketing as pt
from pipeline import usage as pusage


# ---------------------------------------------------------------------------
# Fixtures (mirror test_pipeline_mcp_server.py so this file is self-contained).
# ---------------------------------------------------------------------------
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


@pytest.fixture(autouse=True)
def _isolate_usage_state(tmp_path, monkeypatch):
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", path)
    return path


# ---------------------------------------------------------------------------
# Shared helpers (mirror the conventions in test_pipeline_mcp_server.py so the
# implementer does not need any new fixture machinery).
# ---------------------------------------------------------------------------
def _story(**over):
    base = {"summary": "Do the thing", "agent_instructions": "Build it with tests."}
    base.update(over)
    return base


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


# The exact round-1 rework_instruction text produced today by
# _build_dispatch_command (pipeline/persona.py lines 100-106). Round 1 must
# reproduce this byte-for-byte -- only round>=2 adds a prefix.
_ROUND1_PERSONA_REWORK = (
    "The code reviewer REQUESTED CHANGES on the previous attempt. "
    "Address this feedback before finishing:\n{feedback}\n\n"
)

# The exact round-1 resume_append_content text produced today by the local
# backend transcript-resume path (pipeline/server.py ~lines 2185-2190) when no
# fix_checklist / revised-instructions / rework-tests note is present.
_ROUND1_SERVER_APPEND = (
    "The code reviewer REQUESTED CHANGES on your previous attempt. "
    "Address this feedback:\n{feedback}"
)

FEEDBACK = "The SQL is injectable; parameterize it."


# ---------------------------------------------------------------------------
# Site 1: pipeline.persona._build_dispatch_command rework_instruction
# ---------------------------------------------------------------------------
def test_build_dispatch_command_round1_no_round_marker():
    """rework_attempts == 1 (the FIRST rework redispatch) must produce the
    exact same rework_instruction wording as today -- no round marker, no
    previous-attempt note."""
    story = _story(rework_attempts=1)
    spec = pper._build_dispatch_command(story, "PIPE-1", review_feedback=FEEDBACK)
    prompt = spec["prompt"]

    expected_rework = _ROUND1_PERSONA_REWORK.format(feedback=FEEDBACK)
    assert expected_rework in prompt
    # No round marker may appear for round 1.
    assert "rework round" not in prompt.lower()
    assert "previous attempt already redispatched" not in prompt.lower()


def test_build_dispatch_command_round2_has_round_marker():
    """rework_attempts == 2 (the SECOND rework redispatch on this feedback)
    must prefix the rework instruction with a one-sentence round marker that
    names the round number and notes a previous attempt already redispatched
    on this same feedback."""
    story = _story(rework_attempts=2)
    spec = pper._build_dispatch_command(story, "PIPE-1", review_feedback=FEEDBACK)
    prompt = spec["prompt"]

    assert "rework round 2" in prompt.lower()
    assert "previous attempt already redispatched" in prompt.lower()
    # The original feedback text must still be present.
    assert FEEDBACK in prompt
    assert "REQUESTED CHANGES" in prompt


def test_build_dispatch_command_round5_has_correct_round_number():
    """A higher round number must be reflected literally (not hardcoded to 2)."""
    story = _story(rework_attempts=5)
    spec = pper._build_dispatch_command(story, "PIPE-1", review_feedback=FEEDBACK)
    prompt = spec["prompt"]

    assert "rework round 5" in prompt.lower()
    assert "previous attempt already redispatched" in prompt.lower()
    assert "rework round 2" not in prompt.lower()


def test_build_dispatch_command_round0_treated_as_round1():
    """rework_attempts missing/0 must behave like round 1 (no marker). The
    threshold is >= 2, so 0 and 1 are both marker-free."""
    story = _story()  # no rework_attempts key
    spec = pper._build_dispatch_command(story, "PIPE-1", review_feedback=FEEDBACK)
    prompt = spec["prompt"]
    assert "rework round" not in prompt.lower()
    assert _ROUND1_PERSONA_REWORK.format(feedback=FEEDBACK) in prompt


def test_build_dispatch_command_reads_rework_attempts_from_story_not_param():
    """The round number must be read from story['rework_attempts'] directly
    inside _build_dispatch_command -- no new function parameter was added.
    _build_dispatch_command's signature must still accept only
    (story, story_key, plan_name, resume_journal, review_feedback)."""
    import inspect

    sig = inspect.signature(pper._build_dispatch_command)
    params = list(sig.parameters)
    assert params == ["story", "story_key", "plan_name", "resume_journal",
                      "review_feedback"]
    # No 'rework_attempts' / 'rework_round' parameter leaked into the signature.
    assert "rework_attempts" not in params
    assert "rework_round" not in params


def test_build_dispatch_command_no_feedback_no_marker_even_at_high_round():
    """When there is no review_feedback, no rework_instruction is built at all
    -- so a high rework_attempts alone must NOT inject a round marker (the
    marker only prefixes an actual rework instruction)."""
    story = _story(rework_attempts=9)
    spec = pper._build_dispatch_command(story, "PIPE-1")
    prompt = spec["prompt"]
    assert "rework round" not in prompt.lower()
    assert "REQUESTED CHANGES" not in prompt.upper()


def test_build_dispatch_command_round_marker_is_one_sentence_prefix():
    """The round marker must be a PREFIX to the existing rework_instruction,
    not a replacement: the existing 'REQUESTED CHANGES ... Address this
    feedback before finishing:' sentence must still follow it."""
    story = _story(rework_attempts=2)
    spec = pper._build_dispatch_command(story, "PIPE-1", review_feedback=FEEDBACK)
    prompt = spec["prompt"]

    marker_idx = prompt.lower().index("rework round 2")
    base_idx = prompt.index("The code reviewer REQUESTED CHANGES on the previous attempt.")
    # Marker comes before the existing sentence.
    assert marker_idx < base_idx
    # The existing sentence is still intact after the marker.
    assert _ROUND1_PERSONA_REWORK.format(feedback=FEEDBACK) in prompt


# ---------------------------------------------------------------------------
# Site 2: pipeline.server local-backend transcript-resume resume_append_content
# ---------------------------------------------------------------------------
def _local_rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                      story_over, plan_name="localrwround"):
    """Drive dispatch_story through the local-backend transcript-resume path
    for a changes_requested story and return the captured subprocess env
    (which carries LOCAL_AGENT_RESUME_APPEND_CONTENT)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    base = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "_dispatched_agent_instructions": "Build it.",
        "status": "changes_requested", "worktree": str(worktree_path),
        "review_feedback": FEEDBACK,
    }
    base.update(story_over)
    _write_manifest(plan_dir, plan_name, {"S1": base})

    # No fix_checklist -> exercises the else branch (feedback-only append).
    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: None)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7001)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story(plan_name, "S1")
    assert popen_calls, "dispatch_story did not invoke the local backend"
    return popen_calls[0]["env"]


def test_server_local_rework_round1_append_unchanged(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """rework_attempts == 1 (first rework redispatch) on the local-backend
    resume path must produce byte-for-byte today's resume_append_content --
    no round marker."""
    env = _local_rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                            {"rework_attempts": 1})
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert append == _ROUND1_SERVER_APPEND.format(feedback=FEEDBACK)
    assert "rework round" not in append.lower()
    assert "previous attempt already redispatched" not in append.lower()


def test_server_local_rework_round2_append_has_round_marker(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """rework_attempts == 2 on the local-backend resume path must prefix the
    append content with the round marker naming round 2 and the
    previous-attempt note, while keeping the existing feedback text."""
    env = _local_rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                            {"rework_attempts": 2})
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]

    assert "rework round 2" in append.lower()
    assert "previous attempt already redispatched" in append.lower()
    # The existing feedback-only sentence must still be present (prefix, not
    # replacement).
    assert "The code reviewer REQUESTED CHANGES on your previous attempt." in append
    assert FEEDBACK in append


def test_server_local_rework_round5_append_has_correct_round_number(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """A higher round must be reflected literally in the local-backend append."""
    env = _local_rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                            {"rework_attempts": 5})
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "rework round 5" in append.lower()
    assert "rework round 2" not in append.lower()


def test_server_local_rework_round0_append_unchanged(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """rework_attempts missing/0 on the local-backend resume path behaves like
    round 1 (threshold is >= 2)."""
    env = _local_rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                            {})  # no rework_attempts key
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "rework round" not in append.lower()
    assert append == _ROUND1_SERVER_APPEND.format(feedback=FEEDBACK)


def test_server_local_rework_round2_marker_is_prefix_to_existing(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """The round marker must precede the existing 'REQUESTED CHANGES ...'
    sentence in the local-backend append (prefix, not replacement)."""
    env = _local_rework_env(plan_dir, worktree_root, agents_dir, monkeypatch,
                            {"rework_attempts": 2})
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    marker_idx = append.lower().index("rework round 2")
    base_idx = append.index("The code reviewer REQUESTED CHANGES on your previous attempt.")
    assert marker_idx < base_idx


# ---------------------------------------------------------------------------
# Site 2 with fix_checklist: the round marker prefixes the checklist branch too
# ---------------------------------------------------------------------------
def _local_rework_env_with_checklist(plan_dir, worktree_root, agents_dir,
                                     monkeypatch, story_over, checklist):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    worktree_path = worktree_root / "S1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    (worktree_path / ".agent_transcript.json").write_text(json.dumps([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
    ]))
    base = {
        "summary": "Do thing", "agent_instructions": "Build it.",
        "_dispatched_agent_instructions": "Build it.",
        "status": "changes_requested", "worktree": str(worktree_path),
        "review_feedback": FEEDBACK,
    }
    base.update(story_over)
    _write_manifest(plan_dir, "localrwchk", {"S1": base})

    monkeypatch.setattr(p, "_run_rework_planner", lambda *a, **k: checklist)

    popen_calls = []

    def _fake_popen(cmd, env, **kw):
        popen_calls.append({"cmd": cmd, "env": env})
        return _FakeProc(7002)

    monkeypatch.setattr(p.subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(backend.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pt, "plane_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no plane")))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")

    p.dispatch_story("localrwchk", "S1")
    return popen_calls[0]["env"]


def test_server_local_rework_round2_with_checklist_has_marker(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """When a fix_checklist is present (the if-branch), round>=2 must still
    prefix the round marker -- the checklist mechanism itself is unchanged,
    the marker is added alongside it."""
    checklist = "1. Parameterize the SQL query.\n2. Add a regression test."
    env = _local_rework_env_with_checklist(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        {"rework_attempts": 2}, checklist)
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]

    assert "rework round 2" in append.lower()
    assert "previous attempt already redispatched" in append.lower()
    # The fix_checklist text is untouched / still present.
    assert checklist in append
    assert "fix checklist" in append.lower()
    # Original feedback still referenced.
    assert FEEDBACK in append


def test_server_local_rework_round1_with_checklist_no_marker(
    plan_dir, worktree_root, agents_dir, monkeypatch,
):
    """Round 1 with a fix_checklist must NOT get a round marker -- the
    checklist branch is also gated on >= 2."""
    checklist = "1. Parameterize the SQL query."
    env = _local_rework_env_with_checklist(
        plan_dir, worktree_root, agents_dir, monkeypatch,
        {"rework_attempts": 1}, checklist)
    append = env["LOCAL_AGENT_RESUME_APPEND_CONTENT"]
    assert "rework round" not in append.lower()
    assert checklist in append


# ---------------------------------------------------------------------------
# Source-level mechanical checks (the implementer makes the minimum edit that
# turns these green; these guard the "read rework_attempts directly inside
# _build_dispatch_command" requirement).
# ---------------------------------------------------------------------------
def test_persona_build_dispatch_command_reads_rework_attempts():
    """pipeline/persona.py must read rework_attempts inside
    _build_dispatch_command (git grep rework_attempts pipeline/persona.py)."""
    import inspect

    src = inspect.getsource(pper._build_dispatch_command)
    assert "rework_attempts" in src, (
        "_build_dispatch_command must read story['rework_attempts'] directly "
        "to gate the round>=2 prefix"
    )


def test_server_dispatch_path_reads_rework_attempts_for_round_prefix():
    """pipeline/server.py's resume_append_content construction must read
    rework_attempts to gate the round>=2 prefix. We assert the source of the
    module contains a rework_attempts read near the append construction (the
    implementer adds it)."""
    import inspect

    # _dispatch_story_impl is the function containing the resume_append_content
    # construction; fall back to whole-module source if the name differs.
    target = getattr(p, "_dispatch_story_impl", None)
    if target is not None:
        src = inspect.getsource(target)
    else:
        src = inspect.getsource(p)
    assert "rework_attempts" in src, (
        "server.py dispatch path must read story['rework_attempts'] to gate "
        "the round>=2 prefix on resume_append_content"
    )