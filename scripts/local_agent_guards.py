"""Off-task-drift and same-path-edit-churn guards for scripts/local_agent.py's
step loop. Pure functions (no module-level mutable state, no dependency on
the loop's git-commit helpers) split out purely to keep local_agent.py under
the project's line-count target. local_agent.py re-exports these names,
since its step loop references them as bare names. The two functions in this
family with side effects or done-gate coupling — _apply_off_task_action
(it calls local_agent.worktree_dirty()/auto_wip_commit()) and
_reject_done_for_suite — also live here as ``<name>_impl`` functions using
the same per-instance origin-dict routing as scripts/local_agent_git.py
(see that module's docstring for the worked example): each delegating
wrapper in local_agent.py passes its own module's ``globals()`` dict, and
agent-module-owned free variables are read as ``origin["NAME"]`` at call
time so monkeypatched re-binds land on the instance the test patched.
"""
import os
import re

_COMPLETION_PHRASES = ("all done", "i'm done", "i am done", "all finished", "finished")


def _no_tool_nudge(consecutive: int, content: str = "") -> str:
    """Nudge for an assistant turn that emitted no tool call.

    Early turns get the plain call-to-action (the model may simply have
    forgotten) - unless `content` itself narrates completion (e.g. "All
    done."), in which case it's directed to call the `done` tool specifically
    (live 2026-07-29: a model that narrates "All done." as prose instead of
    calling a tool can burn turns toward NO_TOOL_CAP before the generic nudge
    happens to work). From the third consecutive narration turn onward,
    escalate to behavioral guidance regardless of content: a weak model stuck
    looping on a failing self-test is usually chasing a phantom — its own
    test asserts behavior the correct implementation can never satisfy. Tell
    it to re-check the spec and fix the *test*, not the implementation, then
    call done.

    Phrase matching uses \\b word boundaries, not bare substring search - a
    naive `"finished" in content` also matches inside "unfinished"/
    "refinished", wrongly flagging genuinely incomplete work as completion
    (caught in review 2026-07-29; the acceptance oracle that shipped first
    only exercised "not done yet"/"not finished" and missed this compound-
    word case). A phrase match immediately preceded by the word "not" (e.g.
    "not finished") is a negation, not completion, and is excluded too.
    """
    if consecutive < 3:
        lc = content.lower()
        for phrase in _COMPLETION_PHRASES:
            for match in re.finditer(r"\b" + re.escape(phrase) + r"\b", lc):
                preceding_words = lc[:match.start()].split()
                if preceding_words and preceding_words[-1] == "not":
                    continue
                return "You reported being done — call the done tool now to finish."
        return "Call a tool now (do not write prose)."
    return (
        "You have not called a tool for several turns. If you are stuck on a "
        "failing test that you wrote, that test may assert the wrong behavior — "
        "re-read the task spec. If your implementation already matches the spec, "
        "fix or delete the failing test rather than the implementation, then call "
        "done. Otherwise call a tool now (do not write prose)."
    )


_TASK_PATH_RE = re.compile(r"""`([\w./-]+\.\w+)`|\*\*([\w./-]+\.\w+)\*\*""")


def _expected_task_paths(task: str) -> set[str]:
    """Extract file paths the task brief explicitly names, from backtick-quoted
    (`path/to/file.py`) or bold-markdown (**path/to/file.py**) spans - the two
    conventions this pipeline's agent_instructions consistently use to name
    files. An empty result means the brief named no files, in which case the
    off-task-drift guard that consumes this must fail open (see
    _is_off_task_path) rather than flag every edit as off-task."""
    paths: set[str] = set()
    for m in _TASK_PATH_RE.finditer(task or ""):
        p = m.group(1) or m.group(2)
        if p:
            paths.add(p.lstrip("./"))
    return paths


def _is_off_task_path(path: str, expected: set[str]) -> bool:
    """True if `path` (a mutating tool call's target) matches none of the
    paths named in the task brief - by exact match, path-suffix containment
    (handles './'-prefixed or differently-rooted relative forms), or shared
    basename. Returns False (never flags) when `expected` is empty or `path`
    is empty: a brief that names no files gives the guard nothing reliable to
    compare against, and failing open there is safer than flagging every
    edit as off-task."""
    if not expected or not path:
        return False
    norm = path.lstrip("./")
    name = norm.rsplit("/", 1)[-1]
    for e in expected:
        if norm == e or norm.endswith("/" + e) or e.endswith("/" + norm):
            return False
        if name == e.rsplit("/", 1)[-1]:
            return False
    return True


def _off_task_step(path_arg: str, expected: set[str], off_task_targets: set,
                    already_nudged: bool) -> tuple[str, bool]:
    """One state transition of the off-task-drift guard for a single flagged
    mutation. Returns (action, new_already_nudged):

    - ("none", already_nudged) if `path_arg` is not off-task at all.
    - ("nudge", True) on the first-ever off-task mutation this run.
    - ("escalate", True) on EVERY off-task mutation after that first nudge —
      whether it's a further touch of the SAME path or a different one.

    Mode 31 follow-up (2026-08-07): the original guard only escalated on a
    second DISTINCT off-task path (`already_seen = path_arg in
    off_task_targets`), so a model that fixated on the ONE off-task file it
    was already nudged about — the actual live failure (W3a story 2:
    env_var_catalog.py nudged once at step 21, then mutated 8 more times
    through step 36 with zero further guard action) — passed every
    subsequent mutation of that same path through unguarded. Escalation no
    longer depends on distinctness; `off_task_targets` is now purely
    informational (kept for logging/tests)."""
    if not (path_arg and _is_off_task_path(path_arg, expected)):
        return "none", already_nudged
    off_task_targets.add(path_arg)
    if not already_nudged:
        return "nudge", True
    return "escalate", True


# Best-effort detection of a bash command that mutates a file directly,
# bypassing the file-editing tools the off-task-drift guard otherwise
# watches — a formatter/linter --fix flag, sed/perl -i, or shell
# redirection. Live 2026-08-07 (W3a story 2): `ruff check
# pipeline/env_var_catalog.py --fix` mutated the off-task file through
# `bash`, invisible to the off-task guard (which only inspected
# create_file/str_replace/replace_lines `path` args) and to MUTATING_TOOLS
# (bash is not a member). Deliberately conservative: a command with no
# recognized mutating marker is never flagged, so ordinary read-only
# commands (pytest, grep, cat) never trip this.
_BASH_MUTATION_MARKERS = re.compile(
    r"--fix\b|--write\b|\bsed\s+[^|;&\n]*-i\b|\bperl\s+[^|;&\n]*-i\b|"
    r"\bblack\s|\bisort\s|\bautopep8\b|\bprettier\b|>>?\s*[\w./-]+\.\w+"
)
_BASH_PATH_TOKEN_RE = re.compile(r"[\w./-]+\.\w+")


def _bash_off_task_path(cmd: str, expected: set[str]) -> str | None:
    """Return the first off-task path a mutation-looking `bash` command
    appears to write to, or None if the command has no recognized mutating
    marker or names no off-task path."""
    if not cmd or not _BASH_MUTATION_MARKERS.search(cmd):
        return None
    for token in _BASH_PATH_TOKEN_RE.findall(cmd):
        candidate = token.lstrip("./")
        if _is_off_task_path(candidate, expected):
            return candidate
    return None


# Same-path edit-churn guard (Mode 31 follow-up, 2026-08-07): the off-task,
# repetition, and read-heavy guards all key on FAILURE or INACTION — a model
# that keeps making SUCCESSFUL edits to the same file without ever running
# its tests defeats every one of them (each success resets `seen` and
# `last_progress_step`, and keeps the read-heavy window from ever filling
# with non-mutating calls, since the mutating call itself lands in that
# window). Live 2026-08-07 (W3a story 2): 22 consecutive successful
# replace_lines/create_file calls to config_provenance.py — the ON-task
# file — zero test runs in between, tripped no existing guard, and the run
# step-capped with no `done`. Tracks consecutive successful mutations to the
# SAME path with no intervening test run: first breach nudges toward
# running the tests, a second breach after that parks (or re-nudges under
# PARK_ENABLED=0, matching every other guard's shape).
CHURN_SAME_PATH_MAX_EDITS = int(os.environ.get("LOCAL_AGENT_CHURN_MAX_EDITS", "12"))
_CHURN_TEST_RUN_RE = re.compile(r"\bpytest\b")


def _churn_step(path_arg: str, churn_state: dict) -> str:
    """Returns "none", "nudge" (the first time CHURN_SAME_PATH_MAX_EDITS
    consecutive same-path successful edits happen with no test run between
    them), or "escalate" (every time after that). `churn_state` is a dict
    with keys "path"/"count"/"nudged", mutated in place across calls. A
    successful mutation to a DIFFERENT path resets the streak — this guard
    is about fixating on one file, not the total edit count."""
    if not path_arg:
        return "none"
    if path_arg == churn_state["path"]:
        churn_state["count"] += 1
    else:
        churn_state["path"] = path_arg
        churn_state["count"] = 1
        churn_state["nudged"] = False
    if churn_state["count"] < CHURN_SAME_PATH_MAX_EDITS:
        return "none"
    if not churn_state["nudged"]:
        churn_state["nudged"] = True
        churn_state["count"] = 0
        return "nudge"
    churn_state["count"] = 0
    return "escalate"


def _churn_note_test_run(cmd: str, churn_state: dict) -> None:
    """A bash command that runs pytest is a real verification step, not
    blind churn — reset the same-path edit streak so the churn guard doesn't
    fire on a model that IS checking its work between edits."""
    if cmd and _CHURN_TEST_RUN_RE.search(cmd):
        churn_state["count"] = 0


__all__ = [
    "CHURN_SAME_PATH_MAX_EDITS",
    "_bash_off_task_path",
    "_churn_note_test_run",
    "_churn_step",
    "_expected_task_paths",
    "_is_off_task_path",
    "_no_tool_nudge",
    "_off_task_step",
]


def _apply_off_task_action_impl(origin, action, path_arg, messages):
    """Verbatim body of local_agent._apply_off_task_action, moved here by
    LA-VERIFY-FOLLOWUP. ``origin`` is the delegating wrapper's ``globals()``
    (routing convention: see this module's docstring, and
    scripts/local_agent_git.py for the worked example) — worktree_dirty and
    auto_wip_commit are agent-module-owned and read from it at call time."""
    if action == "nudge":
        print(f"   [off-task nudge: {path_arg} not in assigned scope]", flush=True)
        messages.append({"role": "user", "content": (
            f"You just touched {path_arg}, which was not named anywhere "
            f"in your assigned task. If this file is genuinely required "
            f"to complete the task, explain why in your next message and "
            f"continue. Otherwise STOP editing unrelated files and refocus "
            f"on the files named in your instructions.")})
        return False
    if action == "escalate":
        print(f"   [parking: off-task drift onto {path_arg} after nudge]", flush=True)
        if origin["worktree_dirty"]():
            origin["auto_wip_commit"]("parked on off-task drift")
        return True
    return False


def _reject_done_for_suite_impl(origin, messages, step, suite_tail, gate):
    """Verbatim body of local_agent._reject_done_for_suite, moved here by
    LA-VERIFY-FOLLOWUP. No agent-module free variables today; ``origin`` is
    kept first for signature symmetry with the other origin-routing impls."""
    if gate == 'lint':
        print(f"[step {step}] done rejected — lint gate still fails (rework done-bar); asking agent to fix the lint failure", flush=True)
        content = (
            f"Your tests PASS, but the lint check (`ruff check .`) fails. The merge-gate CI lint gate will reject this on the same failure:\n{suite_tail}\n\nMost lint errors are auto-fixable: run `ruff check . --fix`, then `ruff check .` to confirm it is clean.\n\nDo NOT edit implementation logic — this is a formatting/import/style error, not a correctness bug, and editing logic will not fix it. Do not call done until `ruff check .` passes in full."
        )
    else:
        print(f"[step {step}] done rejected — full test suite still fails (rework done-bar); asking agent to fix the failure", flush=True)
        content = (
            f"The full test suite still fails. The merge-gate CI will reject this on the same failure:\n{suite_tail}\n\nThe bug could be in the implementation you just changed, or in a test file - do not assume either side is correct. Re-read the failing test and the code it exercises, identify which one is actually wrong, and make ONE targeted fix there. do not call done until pytest passes in full."
        )
    # A byte-identical notice still present in context adds nothing: across
    # resumed rework cycles the same failure re-trips the done-bar and
    # verbatim duplicates accumulated 5x in one measured transcript
    # (2026-09-04). If compaction dropped the earlier notice, the membership
    # check fails and the re-append fires.
    if not any(m.get("role") == "user" and m.get("content") == content for m in messages):
        messages.append({"role": "user", "content": content})
