"""5xx oversized-transcript recovery, relocated from the oracle dispatch
script (LAO-RECOVERY, twin of LA-RECOVERY). Behavior-preserving move: every
oracle-owned name the body needs is read through the caller's live module
dict (``origin``), which the delegating wrapper in the oracle script passes
as its own globals().
"""

import os
import time

# Backoff between escalation rounds. The pre-2026-08-07 helper fired all its
# rounds back-to-back with no pause at all, which is the worst possible
# response when the 500 is load-induced (memory pressure, a model reload,
# concurrent agents) rather than an overflow.
RECOVERY_BACKOFF_SECONDS = float(
    os.environ.get("LOCAL_AGENT_RECOVERY_BACKOFF_SECONDS", "2"))
# (context fraction, backoff multiplier) per round, in order.
_RECOVERY_ROUNDS = ((0.75, 1), (0.50, 3), (0.30, 6))

# time is deliberately NOT routed through `origin`: tests patch
# `lao.time.sleep`, which mutates the shared stdlib time module object; this
# module's plain `import time` binds that same object, so the patch is
# observed here.
# Backoff patches must target scripts.local_agent_oracle_recovery.RECOVERY_BACKOFF_SECONDS, not the oracle module.


def recover_from_oversized_5xx_impl(origin, messages, chat_fn, *, step=None):
    """Recover from a backend error on an oversized transcript by retrying
    with an escalating (shrinking) context budget before giving up. A single
    trim-retry can also fail on a still-oversized payload, so shrink harder
    each round, and pause between rounds so a load-induced failure gets time
    to clear. Returns the assistant message dict on success, or None if every
    round fails (caller gives up). Mutates ``messages`` in place. Bounded:
    3 rounds.

    When the trim cannot shrink the payload the request is retried UNCHANGED
    rather than abandoned. An unshrinkable payload is positive evidence that
    the failure was not an overflow at all - which is exactly the case where
    waiting works. The old code returned None here, which is how a transient
    Ollama 500 killed a ~95%-complete converging run on 2026-07-30.

    Ported from local_agent.py - keep both copies in sync. The oracle runs the
    production path for acceptance-bearing dispatches (oracle_mode = bool(acceptance)
    in backend.py), so a transient 5xx killing a ~95%-complete converging run here
    (2026-07-30 LAUNCHD-PLIST-PORTABILITY) is the live failure this recovers from.
    """
    print(f"[step {step}] backend error after "
          f"{origin['CHAT_MAX_ATTEMPTS']} attempts on a large transcript; "
          f"escalating trim and retrying (with backoff)",
          flush=True)
    for fraction, backoff_mult in _RECOVERY_ROUNDS:
        budget_chars = int(origin["NUM_CTX"]
                           * origin["_effective_chars_per_token"]() * fraction)
        trimmed = origin["_trim_resumed_transcript"](messages, budget_chars)
        # Compare CHARS, not message count: the eviction tier shrinks payload
        # without removing any message, so a length test reports "no change"
        # for a trim that in fact reclaimed most of the transcript.
        if origin["_total_chars"](trimmed) < origin["_total_chars"](messages):
            messages[:] = trimmed
        else:
            # Could not shrink - so this very likely isn't an overflow. Wait
            # it out instead of giving up; the payload goes back unchanged.
            print(f"[step {step}] transcript could not be shrunk further; "
                  f"treating as a transient fault and retrying after backoff",
                  flush=True)
        time.sleep(RECOVERY_BACKOFF_SECONDS * backoff_mult)
        try:
            return chat_fn(messages)
        except origin["httpx"].HTTPStatusError as e:
            if not origin["_is_context_overflow_error"](e):
                raise  # a real bad request - propagate rather than retry
            continue  # overflow-shaped: shrink harder on the next round
    return None  # all rounds exhausted on a persistent failure
