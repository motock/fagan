"""Concurrency helpers for the pipeline MCP server.

_count_in_progress_agents / _reap_zombie_in_progress_stories manage the
MAX_CONCURRENT_AGENTS slot accounting across all plans. _plan_lock is the
flock-based per-plan mutation lock. _heavy_lock serializes heavy build/test
invocations. _is_heavy classifies a command as heavy.

_count_in_progress_agents / _is_heavy are patched via p.<name> by tests;
server call sites use bare names -> re-export -> patch lands. PLAN_DIR is
resolved at call time through the ``_ServerRef`` binding below (the same
pattern pipeline/store.py uses) rather than imported from .paths at module
load: a module-load copy freezes the real ~/.claude/plans path into every
test run, so _plan_lock flocked the REAL plans directory even in a test
that patched p.PLAN_DIR to a tmp_path — and any live process holding a
lock there (e.g. a long-running MCP server) made dispatch silently return
skipped:"locked" and mint nothing (root cause of the W4L-02
test_w4l_dispatch_correlation failures). Patching
pipeline_concurrency.PLAN_DIR directly also still works: setattr replaces
this module global and the bare-name reads below resolve at call time.
"""

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager

from .parsers import _atomic_write_json


class _ServerRef:
    """Delegates to the *current* ``pipeline.server`` binding for a name.

    Mirrors the ``_ServerRef`` pattern already used by ``pipeline/store.py``
    and ``pipeline/service.py``: the class body references server-sourced
    names (``PLAN_DIR``) as bare module globals, and those names must be read
    from the live ``pipeline.server`` value at call time so
    ``monkeypatch.setattr(pipeline.server, "PLAN_DIR", ...)`` lands.
    """

    def __init__(self, name: str):
        self._name = name

    def _value(self):
        from . import server as _server
        return getattr(_server, self._name)

    def __getattr__(self, attr: str):
        return getattr(self._value(), attr)

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)

    def __truediv__(self, other):
        return self._value() / other


PLAN_DIR = _ServerRef("PLAN_DIR")


def _count_in_progress_agents() -> int:
    """Count *actually running* dispatched agents (status in_progress with a
    live pid) across every plan's manifest, not just one plan — the usage
    window MAX_CONCURRENT_AGENTS protects is shared across all plans running
    in this session.

    Checks each pid is still alive rather than trusting the status field: a
    story can be stuck at in_progress with a pid whose process already
    exited (e.g. a plan whose own advance_pipeline tick never ran again to
    notice, or a zombie left by a crashed agent) - left uncorrected, that
    permanently consumes a concurrency slot for every other plan forever.

    Skips dead-pid stories rather than reaping them here so this function
    remains a pure read for callers that size dispatch slots. The reap
    itself runs separately in _reap_zombie_in_progress_stories (called from
    advance_all_plans before any per-plan tick), so a dead-pid story in one
    plan doesn't get clobbered before another plan's check_story_status
    has a chance to grade it.
    """
    count = 0
    for manifest_path in PLAN_DIR.glob("*.manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        for story in manifest.get("stories", {}).values():
            if story.get("status") != "in_progress" or "pid" not in story:
                continue
            try:
                os.kill(story["pid"], 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            count += 1
    return count


def _reap_zombie_in_progress_stories() -> int:
    """In-place reap of in_progress stories whose pid has exited, so they
    stop consuming a MAX_CONCURRENT_AGENTS slot forever.

    Sets status → todo and drops pid. Returns the number reaped. Idempotent:
    a manifest already free of zombies is rewritten only if at least one
    reap happened (avoids touching mtime on every tick).

    Called from advance_all_plans before the per-plan advance_pipeline tick,
    so a freshly crashed agent from plan X doesn't block dispatch sizing
    for plan Y on the same scheduler tick. Per-plan advance_pipeline callers
    (e.g. tests, MCP `advance` tool) don't go through here, so a zombie in
    one plan doesn't get clobbered before another plan's check_story_status
    has a chance to grade it on the same tick.

    Without this, observed 2026-06-28: two audio-bugfixes stories with dead
    pids held 2 of 3 concurrency slots for ~19h, blocking all e2e dispatch.
    """
    reaped = 0
    for manifest_path in PLAN_DIR.glob("*.manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        changed = False
        for story in manifest.get("stories", {}).values():
            if story.get("status") != "in_progress" or "pid" not in story:
                continue
            try:
                os.kill(story["pid"], 0)
                # pid is alive — leave the story alone.
                continue
            except ProcessLookupError:
                # Zombie: agent exited but no one updated the manifest. Reap
                # so the slot frees up and the story becomes dispatchable on
                # the next tick. Setting status back to todo is the correct
                # recovery — the work is unfinished and needs another agent
                # pass; we don't have signal that it's the model's fault
                # vs a harness crash, so don't penalize it with 'failed'.
                story["status"] = "todo"
                story.pop("pid", None)
                changed = True
                reaped += 1
            except PermissionError:
                # Process exists but we can't signal it (owned by another
                # user). Trust that it's alive and don't reap.
                continue
        if changed:
            _atomic_write_json(manifest_path, manifest)
    return reaped


@contextmanager
def _plan_lock(plan_name: str):
    """Exclusive, non-blocking lock scoped to one plan's mutations.

    Used by every tool that mutates the manifest or the worktree
    (advance_pipeline, _set_plan_paused, dispatch_story, interrupt_story).
    The lock is `flock`-based, so it serializes across MCP server processes
    too - two Claude sessions with two MCP server PIDs calling
    dispatch_story on the same story in the same window both want to write
    to the same manifest and create the same worktree, and without this
    guard the second one treats the first's half-built worktree as
    resumable and spawns a second agent into the same directory. Multiple
    agents fighting over one worktree's git state is what produces the
    repeated zero-output agent deaths, not per-story flakiness.

    Reentrant within a single thread: advance_pipeline acquires this lock
    for its whole tick and then calls dispatch_story / interrupt_story,
    which each re-acquire it. flock locks are held per open-file-description
    (a fresh os.open makes a new description), so a nested exclusive flock
    on the same file fails with BlockingIOError *even within the same
    process* — without reentrance the nested call would return
    skipped:"locked" and advance_pipeline would falsely count it as
    dispatched/interrupted while doing nothing. The per-thread held-set
    lets the nested call proceed without re-flocking; cross-thread and
    cross-process serialization is still enforced by flock itself.

    Yields whether the lock was acquired; the caller must check it and skip
    all work if not - this never blocks waiting for the lock.
    """
    held = _held_plan_locks()
    if plan_name in held:
        # Same thread already holds the flock for this plan (nested call
        # from within an advance_pipeline tick). Don't re-flock — a second
        # exclusive flock on a new fd would fail.
        yield True
        return
    lock_path = PLAN_DIR / f"{plan_name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        if acquired:
            held.add(plan_name)
            _held_plan_lock_fds()[plan_name] = fd
        try:
            yield acquired
        finally:
            if acquired:
                held.discard(plan_name)
                _held_plan_lock_fds().pop(plan_name, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


_plan_lock_state = threading.local()


def _held_plan_locks() -> set[str]:
    """Per-thread set of plan names whose flock this thread currently holds,
    for _plan_lock reentrance. threading.local keeps each thread's view
    independent, so thread A holding a plan does not let thread B bypass the
    flock — B's set is empty, so it hits the real flock and serializes."""
    held = getattr(_plan_lock_state, "held", None)
    if held is None:
        held = set()
        _plan_lock_state.held = held
    return held


def _held_plan_lock_fds() -> dict[str, int]:
    """Per-thread map of plan name -> the fd whose open-file-description
    carries this thread's flock, parallel to _held_plan_locks().

    _released_plan_lock needs the actual fd to drop and re-take the REAL
    flock (LOCK_UN / LOCK_EX on the same open-file-description): discarding
    only the name would leave the flock held, and opening a fresh fd here
    would target a different description, so the release would be invisible
    to other threads and processes. The fd stays owned by the _plan_lock
    call that opened it — it is recorded here at acquire and removed at that
    call's release, and _plan_lock's own finally still closes it exactly
    once, so a release/re-acquire cycle leaks no descriptor.
    """
    fds = getattr(_plan_lock_state, "fds", None)
    if fds is None:
        fds = {}
        _plan_lock_state.fds = fds
    return fds


class PlanLockReacquireTimeout(RuntimeError):
    """Raised when a _released_plan_lock exit cannot re-acquire the plan
    flock within PIPELINE_PLAN_LOCK_REACQUIRE_TIMEOUT_SECONDS.

    Deliberately a hard failure rather than a silent continue: the caller is
    in the middle of an advance_pipeline tick and every subsequent manifest
    mutation it makes would race whichever other thread/process took the
    flock during the released window. Failing open here would corrupt the
    plan; raising lets the tick abort with a clear cause instead.
    """


def _plan_reacquire_timeout() -> float:
    """Bounded blocking re-acquire timeout, in seconds.

    Read at call time (never at import) from
    PIPELINE_PLAN_LOCK_REACQUIRE_TIMEOUT_SECONDS; default 300. A missing or
    malformed value degrades to the default instead of raising — a broken
    env var must not take down the tick, and float() (not int()) so a
    fractional value like "0.2" is honoured rather than degrading to the
    long default.
    """
    raw = os.environ.get("PIPELINE_PLAN_LOCK_REACQUIRE_TIMEOUT_SECONDS")
    if raw is None:
        return 300.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 300.0


@contextmanager
def _released_plan_lock(plan_name: str):
    """Temporarily release this thread's plan flock, then re-acquire it.

    advance_pipeline holds the per-plan flock for its whole tick, including
    multi-minute synchronous model calls. This primitive lets a caller drop
    that lock around such a phase so another thread or process (a second MCP
    server, an operator's approve_merge) can take it, then take it back
    before the tick continues.

    Semantics:
      - If this thread does not currently hold the plan (per
        _held_plan_locks), this is a silent no-op: the body runs, nothing is
        opened, flocked, or recorded, and exit does nothing. Releasing a
        lock you do not hold must never be an error and must never mint a
        phantom held-set entry.
      - Otherwise the real flock is released (LOCK_UN on the same fd
        _plan_lock is holding — another thread AND another process can
        acquire it while the body runs) and the plan is removed from the
        held set for the duration of the body.
      - On exit the flock is re-acquired on the same fd, BLOCKING, bounded
        by _plan_reacquire_timeout(). On timeout PlanLockReacquireTimeout is
        raised and the held-set entry stays absent — the thread must not
        claim a lock it does not own, and must not continue without it.
      - A successful re-acquire restores the held-set entry so later nested
        _plan_lock calls in the same tick still see the plan as held.
      - The re-acquire lives in a finally, so an exception raised by the
        body still re-acquires the lock before propagating.

    No new fd is opened here: the release and the re-acquire both act on the
    fd recorded by the enclosing _plan_lock, which closes it in its own
    finally — so a release/re-acquire cycle leaks no descriptor and the
    outer exit still releases the flock for real.
    """
    held = _held_plan_locks()
    if plan_name not in held:
        # Not ours to release: silent no-op, and the finally below must do
        # nothing (fd is None) — re-acquiring anyway would flock a lock this
        # thread never held and insert a phantom held-set entry that no
        # _plan_lock finally would ever pop.
        yield
        return
    fd = _held_plan_lock_fds().pop(plan_name, None)
    if fd is None:
        # Defensive: the name is held but no fd is recorded (should not
        # happen — _plan_lock records both together). Treat as no-op rather
        # than guessing an fd.
        yield
        return
    # Pop the held-set entry BEFORE dropping the flock: the held set must
    # never claim a lock this thread has already released (fail-open).
    held.discard(plan_name)
    fcntl.flock(fd, fcntl.LOCK_UN)
    try:
        yield
    finally:
        # Bounded blocking re-acquire on the SAME fd: the open-file
        # description is ours, so this can never conflict with our own
        # lock — no self-deadlock. monotonic(), not time(): a wall-clock
        # deadline breaks if the system clock steps backwards mid-wait.
        deadline = time.monotonic() + _plan_reacquire_timeout()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    # Leave the held-set entry ABSENT on timeout: the
                    # thread does not own the flock, and restoring the name
                    # would make the next nested _plan_lock short-circuit
                    # to True while someone else owns the real lock.
                    raise PlanLockReacquireTimeout(
                        f"timed out re-acquiring the plan flock for "
                        f"'{plan_name}' after releasing it for "
                        f"_released_plan_lock "
                        f"({_plan_reacquire_timeout():g}s)"
                    ) from None
                time.sleep(0.05)
        # Success: restore the held-set entry so nested _plan_lock calls in
        # the same tick still see the plan as held (and skip re-flocking).
        held.add(plan_name)
        _held_plan_lock_fds()[plan_name] = fd


@contextmanager
def _heavy_lock():
    """Serializes heavy build/test invocations across all local-agent
    dispatch paths.

    Three concurrent cold builds can push a 24GB M4 to its knees (observed
    in the post-PR #30 e2e rerun: 33GB total pressure, CPU saturated).
    Each worktree has its own target/ (or build/), so concurrent
    invocations don't share cache — they multiply memory pressure rather
    than amortizing it.

    Blocking acquire (LOCK_EX, not LOCK_EX | LOCK_NB) is the right call
    here: callers are already prepared to wait minutes for a build, and
    skipping entirely would just give the agent a false "build failed"
    error and waste more time. The queueing cost is invisible when the
    model is doing non-build work in the meantime.

    Held by every site that runs a heavy build/test:
      - check_story_status (orchestrator's post-dispatch grading)
      - local_agent.py / local_agent_oracle.py `bash` tool (model-invoked)
      - backend.py reviewer bash (reviewer-invoked)
    Decide what counts as heavy with `_is_heavy()`.
    """
    lock_path = PLAN_DIR / "heavy.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# Heavy build/test executables: these typically spend GB-seconds of memory
# running (linkers, type checkers, full compilers). Lock them across
# dispatchees so we never run more than one at a time, regardless of
# language. `make` is gated on a build/test target because make is also
# used for trivial scripts — we don't want to serialize `make clean`.
HEAVY_EXECUTABLES = frozenset({
    "cargo", "npm", "yarn", "pnpm", "npx",
    "mvn", "gradle", "./gradlew",
    "sbt", "bazel", "buck",
    "go", "rustc", "swift", "swiftc",
})


def _is_heavy(cmd: list[str]) -> bool:
    """True iff a subprocess command should acquire the heavy lock.

    Matched by argv[0] against a static list of build/test executables.
    No parsing of the command body — keep the check O(1) and language-
    agnostic. `make` is special-cased to only the well-known heavy
    targets (`test`/`build`/`check`/`all`/`ci`) because make is also
    used for trivial scripts where the lock would just add latency.
    """
    if not cmd:
        return False
    exe = cmd[0]
    if exe in HEAVY_EXECUTABLES:
        return True
    return bool(exe == "make" and len(cmd) > 1 and cmd[1] in ("test", "build", "check", "all", "ci"))


__all__ = [
    "HEAVY_EXECUTABLES",
    "PlanLockReacquireTimeout",
    "_count_in_progress_agents",
    "_heavy_lock",
    "_held_plan_lock_fds",
    "_held_plan_locks",
    "_is_heavy",
    "_plan_lock",
    "_plan_lock_state",
    "_plan_reacquire_timeout",
    "_reap_zombie_in_progress_stories",
    "_released_plan_lock",
]