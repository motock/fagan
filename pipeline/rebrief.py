"""Automate the diagnosis CLAUDE.md Step 9 asks for: "encode the diagnosis
into the next attempt's instructions - state the exact defect and the minimal
fix required, not an open-ended retry." Before this module, that step was
done by hand: an operator read agent.log after a failed/stalled dispatch and
hand-wrote a root-cause block into agent_instructions before re-dispatching.

collect_failure_evidence gathers a bounded summary of why a dispatched attempt
failed, from the artifacts a run leaves behind (the story's own summary, its
last recorded test failure, and the tail of its agent.log - the tail is where
a failure is, the head is orientation).

diagnose_failure turns that evidence into a short root-cause statement via a
configurable "diagnosis" role, and FAILS OPEN (returns None) on every error
path - a diagnosis is an optimization that saves the next attempt some steps,
never a gate. It must never block, crash, or change whether a redispatch
happens.

compose_rebriefed_instructions folds a diagnosis into a story's
agent_instructions, replacing any prior diagnosis block rather than stacking -
a rework loop can call this several times on the same story, and a growing
prompt defeats the point.
"""

import logging
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from app import backend, role_registry

DIAGNOSIS_HEADER = "=== PRIOR-ATTEMPT DIAGNOSIS (read this FIRST) ==="

CLEANUP_HEADER = "=== WORKTREE HYGIENE (read this too) ==="

FACTS_HEADER = "=== PRIOR-ATTEMPT FACTS (measured from the worktree, not guessed) ==="

# Tool names the local agent prints in its `[step N] <tool>: ...` log lines.
_EDIT_TOOLS = frozenset({"create_file", "str_replace", "replace_lines", "restore_file"})

# Static allowlist of test-runner tokens (never runtime language detection):
# a bash command containing one of these counts as "the agent ran the tests".
_TEST_COMMAND_TOKENS = (
    "pytest", "py.test", "npm test", "yarn test", "cargo test",
    "go test", "mvn test", "gradlew test", "make test",
)

# Facts are prepended to the next dispatch's prompt alongside the diagnosis, so
# they compete for the same context budget the rebrief exists to save.
_FACTS_LIMIT = 3000

# A file whose diff deletes at least this many lines, and deletes at least this
# many times more than it adds, is very likely a whole-file rewrite that
# dropped unrelated code rather than a targeted edit (seen live 2026-08-06:
# pipeline/config_provenance.py gutted from 327 lines to 107 by one
# create_file). Thresholds are deliberately loose - this is a prompt to look,
# not a gate.
_CLOBBER_MIN_DELETIONS = 40
_CLOBBER_DELETE_RATIO = 3

_RERUN_MAX_NODES = 3
_RERUN_TIMEOUT_SECONDS = 180


def collect_failure_evidence(
    worktree, story: dict[str, Any], limit: int = 6000, facts: str | None = None
) -> str:
    """Gather a bounded summary of why a dispatched attempt on `story` failed,
    from `worktree`'s agent.log and the story's own recorded state. Never
    raises - a missing worktree, missing agent.log, or unreadable file all
    yield a valid (possibly minimal) string.

    `facts` is the measured-fact block from collect_attempt_facts; it is
    computed here when not supplied, and placed ahead of the log tail so the
    over-budget trim below sacrifices raw log text rather than measurement."""
    sections = [f"STORY: {story.get('summary', '?')}"]

    if facts is None:
        facts = collect_attempt_facts(worktree, story)
    if facts:
        sections.append(f"MEASURED FACTS ABOUT THE LAST ATTEMPT:\n{facts}")

    last_test_check = story.get("last_test_check") or {}
    # Staleness gate (2026-08-15): last_test_check records the worktree HEAD
    # sha at the moment the check ran. If the worktree has since moved to a
    # new commit, the recorded failure may no longer exist - do NOT feed it
    # into the diagnosis. Only include the LAST TEST FAILURE section when the
    # recorded sha matches the worktree's current HEAD. A missing sha (stories
    # written before this fix) against a real git HEAD also omits the section.
    try:
        current_head = _git(worktree, ["rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError, AttributeError):
        current_head = None
    last_error = last_test_check.get("error")
    if last_test_check.get("sha") == current_head and last_error:
        sections.append(f"LAST TEST FAILURE:\n{last_error}")
    elif (
        last_test_check.get("sha") == current_head
        and last_test_check.get("returncode") not in (None, 0)
    ):
        tail = (last_test_check.get("stdout_tail") or "") + (last_test_check.get("stderr_tail") or "")
        if tail.strip():
            sections.append(f"LAST TEST FAILURE (rc={last_test_check['returncode']}):\n{tail}")

    try:
        log_path = Path(worktree) / "agent.log"
        if log_path.is_file():
            text = log_path.read_text(errors="replace")
            sections.append(f"AGENT LOG TAIL:\n{text[-4000:]}")
    except OSError:
        pass

    evidence = "\n\n".join(sections)
    if len(evidence) <= limit:
        return evidence

    # Over budget: trim the log tail first, keep the summary/test-error
    # sections (a large log dominates the length; those two are compact and
    # carry the most task-identifying context per character).
    head = "\n\n".join(sections[:-1]) if len(sections) > 1 else ""
    remaining = max(limit - len(head) - 2, 0)
    tail = sections[-1][-remaining:] if remaining else ""
    return (head + "\n\n" + tail).strip()[:limit] if head else tail[:limit]


def _git(worktree, args: list[str], timeout: int = 15) -> str | None:
    """Run a read-only git command in `worktree`. Returns None on any failure -
    a missing worktree, a non-repo directory, git absent, or a non-zero exit."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(worktree),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _base_commit(worktree) -> str | None:
    """The commit this attempt's branch diverged from, so the diff below
    describes the ATTEMPT rather than the whole branch history. Candidates are
    tried in order because the pipeline operates across repos that differ on
    default-branch naming and on whether a remote exists at all."""
    for candidate in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        merge_base = _git(worktree, ["merge-base", "HEAD", candidate])
        if merge_base and merge_base.strip():
            return merge_base.strip()
    return None


def _diff_facts(worktree) -> list[str]:
    """What this attempt actually changed, measured against its base commit."""
    base = _base_commit(worktree)
    if base is None:
        return []
    numstat = _git(worktree, ["diff", "--numstat", base, "HEAD"])
    if numstat is None:
        return []

    rows = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or parts[0] == "-" or parts[1] == "-":
            continue  # malformed, or a binary file with no line counts
        rows.append((int(parts[0]), int(parts[1]), parts[2]))

    if not rows:
        return [(
            "NO CODE CHANGE: this attempt's branch is identical to its base "
            "commit - not one edit has landed. Whatever the log narrates, "
            "nothing was written. Stop reading and make the smallest edit that "
            "moves the task forward, then run the tests."
        )]

    facts = [
        "FILES CHANGED so far (+added/-removed vs base): "
        + ", ".join(f"{path} (+{added}/-{removed})" for added, removed, path in rows[:12])
    ]
    for added, removed, path in rows:
        if removed >= _CLOBBER_MIN_DELETIONS and removed >= _CLOBBER_DELETE_RATIO * max(added, 1):
            facts.append(
                f"POSSIBLE CLOBBER: {path} lost {removed} lines and added only "
                f"{added}. That shape means a whole-file rewrite dropped code "
                "unrelated to this task. Read `git diff` on that file and "
                "restore what was deleted before making any further edit."
            )

    status = _git(worktree, ["status", "--porcelain"])
    if status:
        stray = [
            line for line in status.splitlines()
            if line.strip() and not line.rstrip().endswith("agent.log")
        ]
        if stray:
            facts.append(
                f"UNCOMMITTED CHANGES: {len(stray)} path(s) are still "
                "uncommitted in the worktree."
            )
    return facts


def _current_attempt_log(worktree) -> str | None:
    """The agent.log text for the LAST attempt only. agent.log is appended
    across resumes, so counting the whole file reports dozens of attempts'
    nudges as if they happened in the last few dozen steps."""
    try:
        log_path = Path(worktree) / "agent.log"
        if not log_path.is_file():
            return None
        text = log_path.read_text(errors="replace")
    except OSError:
        return None
    boot = text.rfind("[boot]")
    return text[boot:] if boot != -1 else text


def _log_facts(worktree) -> list[str]:
    """How the last attempt spent its steps, from the markers the local agent
    prints: which tools it called, and which guards fired on it."""
    text = _current_attempt_log(worktree)
    if not text:
        return []

    tool_calls = re.findall(r"^\[step (\d+)\] ([a-z_]+):", text, re.MULTILINE)
    bash_commands = re.findall(r"^\[step \d+\] bash: (.*)$", text, re.MULTILINE)
    steps = {step for step, _ in tool_calls}
    facts = []

    if steps:
        counts = Counter(tool for _, tool in tool_calls)
        facts.append(
            f"LAST ATTEMPT USED {len(steps)} step(s): "
            + ", ".join(f"{tool} x{n}" for tool, n in counts.most_common(8))
        )
        if not counts.keys() & _EDIT_TOOLS:
            facts.append(
                "NO EDIT TOOL WAS CALLED in the whole attempt - it only looked "
                "at the code. Reading more will not finish this story; the next "
                "step must be an edit."
            )
        if not any(
            token in command for command in bash_commands for token in _TEST_COMMAND_TOKENS
        ):
            facts.append(
                "NEVER RAN THE TESTS during the attempt, so none of its edits "
                "were ever verified. Run the story's test command after each edit."
            )

    guards = Counter(
        line.strip() for line in text.splitlines()
        if line.strip().startswith("[") and line.strip().endswith("]")
        and ("nudge" in line or "parking" in line)
    )
    if guards:
        facts.append(
            "GUARDS THAT FIRED on the attempt (the harness already told it this): "
            + "; ".join(
                f"{marker} x{n}" if n > 1 else marker for marker, n in guards.most_common(6)
            )
        )

    trims = text.count("RESUME TRIMMED") + text.count("CONTEXT EVICTED")
    if trims:
        facts.append(
            f"CONTEXT WAS TRIMMED {trims} time(s) during the attempt: earlier "
            "tool output was dropped, so any line number or file content read "
            "early is likely stale. Re-read a span immediately before editing "
            "it, and prefer anchored str_replace over line numbers."
        )
    return facts


def _rerun_failing_tests(worktree, story: dict[str, Any]) -> list[str]:
    """Re-run the specific tests the last recorded run reported as failing.

    The stored stdout_tail is only the last 2000 characters of pytest output,
    which on a multi-failure run is the `FAILED name` summary and nothing
    else - the tracebacks scrolled past long before. Re-running just those node
    ids with a full traceback turns "some test failed" into the exact failing
    line, which is what the next attempt actually needs. Bounded on both
    axes (node count and wall clock), and skipped entirely for runners whose
    output this cannot parse."""
    if os.environ.get("PIPELINE_REBRIEF_TEST_RERUN", "").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return []
    last_test_check = story.get("last_test_check") or {}
    cmd = last_test_check.get("cmd") or []
    if not any("pytest" in str(part) or "py.test" in str(part) for part in cmd):
        return []

    tail = (last_test_check.get("stdout_tail") or "") + "\n" + (
        last_test_check.get("stderr_tail") or "")
    nodes: list[str] = []
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("FAILED "):
            continue
        node = line.split(None, 1)[1].split(" ", 1)[0].strip()
        if "::" in node and node not in nodes:
            nodes.append(node)
    if not nodes:
        return []
    nodes = nodes[:_RERUN_MAX_NODES]

    prefix: list[str] = []
    for part in cmd:
        prefix.append(part)
        if "pytest" in str(part) or "py.test" in str(part):
            break
    try:
        result = subprocess.run(
            [*prefix, "-vv", "--tb=long", "--no-header", "-p", "no:cacheprovider", *nodes],
            cwd=last_test_check.get("cwd") or str(worktree),
            check=False,
            capture_output=True,
            text=True,
            timeout=_RERUN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if result.returncode == 0:
        return [(
            f"RE-RAN the {len(nodes)} previously-failing test(s) against the "
            f"current worktree ({', '.join(nodes)}): they now PASS. The recorded "
            "failure is stale - do not spend steps on it."
        )]
    output = ((result.stdout or "") + (result.stderr or ""))[-1500:]
    return [(
        f"RE-RAN the previously-failing test(s) against the current worktree "
        f"({', '.join(nodes)}) - they STILL FAIL. Full traceback:\n{output}"
    )]


def collect_attempt_facts(worktree, story: dict[str, Any]) -> str:
    """A bounded, MEASURED description of what the last attempt did, gathered
    from git and the agent.log markers rather than from a model's narrative.

    This exists because the diagnosis role is optional and fails open: when no
    diagnosis provider is configured (or it errors), these facts are still
    folded into the next attempt's brief. When one IS configured, the facts
    ground it - a model cannot invent a code defect for a branch git says has
    no diff. Never raises; an unusable worktree simply yields ""."""
    facts: list[str] = []
    for collector in (_diff_facts, _log_facts):
        try:
            facts.extend(collector(worktree))
        except Exception:  # noqa: BLE001 (fail-open by design; facts are never a gate)
            logging.getLogger("pipeline").warning(
                f"attempt-fact collector {collector.__name__} failed; continuing")
    try:
        facts.extend(_rerun_failing_tests(worktree, story))
    except Exception:  # noqa: BLE001 (fail-open by design)
        logging.getLogger("pipeline").warning("attempt-fact test re-run failed; continuing")
    if not facts:
        return ""
    return "\n".join(f"- {fact}" for fact in facts)[:_FACTS_LIMIT]


# Backends that must NOT be silently selected as the diagnosis diagnoser by the
# story-backend default below: "claude" would spend an operator's Claude budget
# on a diagnosis they never opted into, and "auto" is not a concrete driver
# (get_backend rejects it). Anything else (local/ollama/mlx/lmstudio) is a local,
# free model the story already spent — a safe default diagnoser.
_CLAUDE_SPEND_BACKENDS = ("claude", "auto")


def _run_diagnosis_role(
    evidence: str, story: dict[str, Any], plan_role_config: dict | None = None
) -> str | None:
    """Resolve and dispatch the "diagnosis" role, asking for the root cause
    and minimal fix in a few sentences. Returns None when no diagnosis provider
    can be resolved safely. May raise on backend failure - diagnose_failure is
    responsible for catching that.

    Resolution priority:
      1. An explicit "diagnosis" role config (plan_role_config, the
         PIPELINE_BACKEND_DIAGNOSIS env var, or the registry's roles.diagnosis).
      2. DEFAULT (when none of the above is set): reuse the story's OWN local
         backend + the concrete model that ran it (story["backend"] +
         dispatched_model / declared model). The model that ran the struggle is
         the cheapest sensible diagnoser, and step-cap / escalation-to-claude
         only reach this default on local backends, so it never surprises an
         operator with Claude spend. A claude/auto/absent backend or a missing
         model fails open (None) - no diagnosis, plain resume, exactly as before
         this default existed. Operators who want a stronger diagnoser set
         roles.diagnosis or PIPELINE_BACKEND_DIAGNOSIS."""
    registry = role_registry.load_registry()
    provider_override = (
        (plan_role_config or {}).get("diagnosis", {}).get("provider")
        or os.environ.get("PIPELINE_BACKEND_DIAGNOSIS")
        or registry.get("roles", {}).get("diagnosis", {}).get("provider")
    )
    prompt = (
        "A dispatched coding attempt failed or stalled. Given the evidence "
        "below, state the ROOT CAUSE and the MINIMAL fix required, in a few "
        "sentences. Do not restate the evidence; be specific and actionable.\n\n"
        "The next attempt only has targeted line-ranged file reads, search, "
        "and anchored str_replace/replace_lines-style edits available; it "
        "does NOT have `git apply` and cannot reliably rewrite a whole file "
        "at once. The suggested fix MUST be achievable with those tools: "
        "recommend targeted reads/searches and small anchored edits. NEVER "
        "recommend `git apply`, a full-file rewrite, or an in-place re-indent "
        "of a large existing function.\n\n"
        "Ground every claim in the MEASURED FACTS section: those are read from "
        "git and the harness's own guard markers, and they outrank anything "
        "the log narrates. If the facts report NO CODE CHANGE, the root cause "
        "is that no edit ever landed - say exactly that and name the one edit "
        "to make; do not invent a defect in code that was never written. If "
        "they report a possible clobber, the minimal fix is restoring the "
        "deleted code. If they carry a traceback, name the exact file and line "
        "it points at. Do not speculate beyond the evidence.\n\n"
        f"{evidence}"
    )
    if provider_override:
        resolution = role_registry.resolve_role(
            "diagnosis",
            plan_role_config=plan_role_config,
            registry=registry,
            model_fallback=lambda: None,
        )
        return backend.get_backend("diagnosis", name=resolution.provider).complete(
            prompt=prompt, system=None, model=resolution.model,
        )

    # No explicit diagnosis role configured: fall back to the story's own local
    # backend + the model that ran it. Fail open for claude/auto/absent or a
    # missing model so this never spends Claude by default and never dispatches
    # a None model to a driver.
    provider = (story.get("backend") or "").strip().lower()
    model = story.get("dispatched_model") or story.get("model")
    if not provider or provider in _CLAUDE_SPEND_BACKENDS or not model:
        return None
    return backend.get_backend("diagnosis", name=provider).complete(
        prompt=prompt, system=None, model=model,
    )


def diagnose_failure(
    evidence: str, story: dict[str, Any], plan_role_config: dict | None = None
) -> str | None:
    """Turn `evidence` into a short root-cause statement via the "diagnosis"
    role. FAILS OPEN (returns None) when the role is unconfigured, raises, or
    returns empty/whitespace-only text - this must never gate a redispatch."""
    if not evidence or not evidence.strip():
        return None
    try:
        diagnosis = _run_diagnosis_role(evidence, story, plan_role_config)
    except Exception as exc:  # noqa: BLE001 (fail-open by design; log type only)
        logging.getLogger("pipeline").warning(
            f"diagnosis role failed with {type(exc).__name__}; "
            "falling back to an undiagnosed redispatch"
        )
        return None
    if diagnosis is None or not diagnosis.strip():
        return None
    return diagnosis.strip()
def compose_rebriefed_instructions(agent_instructions: str, diagnosis: str | None) -> str:
    """Return `agent_instructions` with exactly one PRIOR-ATTEMPT DIAGNOSIS
    block appended, replacing any existing one rather than stacking. Returns
    `agent_instructions` unchanged when `diagnosis` is None/empty."""
    if not diagnosis or not diagnosis.strip():
        return agent_instructions

    base = agent_instructions
    existing = base.find(DIAGNOSIS_HEADER)
    if existing != -1:
        base = base[:existing].rstrip()

    block = f"{DIAGNOSIS_HEADER}\n{diagnosis.strip()}"
    return f"{base}\n\n{block}" if base else block


def compose_attempt_facts(agent_instructions: str, facts: str | None) -> str:
    """Return `agent_instructions` carrying exactly one measured-facts block.

    Unlike compose_rebriefed_instructions, empty `facts` is not a plain no-op:
    any existing block is still removed. A facts block describes ONE attempt,
    so one left behind by a previous attempt would point the next one at
    evidence that is no longer true - worse than no facts at all.

    Call this AFTER compose_rebriefed_instructions: a later diagnosis truncates
    the brief at DIAGNOSIS_HEADER, which would otherwise take a facts block
    appended before it along with no replacement."""
    base = agent_instructions or ""
    existing = base.find(FACTS_HEADER)
    if existing != -1:
        base = base[:existing].rstrip()
    if not facts or not facts.strip():
        return base

    block = f"{FACTS_HEADER}\n{facts.strip()}"
    return f"{base}\n\n{block}" if base else block


def detect_unsatisfiable_signal(evidence: str) -> str | None:
    """Detect unsatisfiable-as-specified signals in failure evidence."""
    if not isinstance(evidence, str):
        return None
    if not evidence or not evidence.strip():
        return None
    signals = [
        ("got an unexpected keyword argument", "unexpected keyword argument - the callee may not accept the injection the tests pass"),
        ("takes no keyword arguments", "takes no keyword arguments - the function does not accept keyword args"),
        ("cannot import name", "cannot import name - the module or symbol is missing"),
        ("has no attribute", "has no attribute - the object lacks the referenced attribute"),
        ("is not defined", "is not defined - the identifier is undefined"),
    ]
    lower = evidence.lower()
    for sig, reason in signals:
        if sig.lower() in lower:
            return reason
    return None


def append_cleanup_guidance(agent_instructions: str) -> str:
    """Append or replace a worktree hygiene guidance block.

    The block is prefixed by :data:`CLEANUP_HEADER`. If the header already
    exists in *agent_instructions*, the existing block (including any prior
    content) is removed and replaced with a fresh one.  This mirrors the
    behaviour of :func:`compose_rebriefed_instructions`.

    Parameters
    ----------
    agent_instructions:
        The current instruction string to augment.

    Returns
    -------
    str
        The augmented instruction string.
    """
    if not agent_instructions:
        base = ""
    else:
        base = agent_instructions
    existing = base.find(CLEANUP_HEADER)
    if existing != -1:
        # Remove the old block and any trailing whitespace.
        base = base[:existing].rstrip()
    guidance = (
        f"{CLEANUP_HEADER}\n"
        "This worktree may still contain files from an earlier, interrupted attempt at this story.\n"
        "The step-cap checkpoint commits whatever was in progress, including off-track experiments;\n"
        "before finishing, check `git status` / diff against the default branch for anything not needed for this task, and remove or revert stray files (especially stray test files) left over from the earlier attempt, since they can break review even when your own changes are correct."
    )
    return f"{base}\n\n{guidance}" if base else guidance



__all__ = [
    "CLEANUP_HEADER",
    "DIAGNOSIS_HEADER",
    "FACTS_HEADER",
    "append_cleanup_guidance",
    "collect_attempt_facts",
    "collect_failure_evidence",
    "compose_attempt_facts",
    "compose_rebriefed_instructions",
    "diagnose_failure",
]
