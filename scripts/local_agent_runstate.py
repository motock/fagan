"""Run-scoped state and helpers moved out of scripts/local_agent.py (RH-10).

Pure bookkeeping with no dependency on local_agent's namespace. local_agent.py
re-exports every name here, so `la.X` reads and `monkeypatch.setattr(la, "X", ...)`
keep working; the callers (run_tool_impl via origin[...], write_done_marker,
_main_impl) stay in local_agent.py and resolve these names through its globals.
"""
from __future__ import annotations

import os


def read_correlation_id() -> str:
    """The orchestrator-minted correlation ID for this dispatch (W4L-02 mints
    one per story and exports it as PIPELINE_CORRELATION_ID into the agent
    subprocess env). Read at call time — never cached at import — so a fresh
    exec of this module under a mutated environment sees the current value.
    Empty string when unset; callers treat "" as "no correlation id"."""
    return os.environ.get("PIPELINE_CORRELATION_ID", "")


# Paths successfully written via create_file THIS process run. The
# non-destructive-editor guard (see run_tool's create_file branch) exists to
# protect PRE-EXISTING repo/seed files from being clobbered by a confused
# model - it was never meant to also block the model from overwriting a file
# it wrote itself moments ago. A weak model that can't construct a correct
# str_replace old_str often has "rewrite the whole small file" as its only
# real recovery strategy; forcing surgical edits it can't produce just
# deadlocks it. Observed live 2026-07-15 (lru_cache): a model alternated
# rejected create_file / rejected str_replace calls for dozens of steps,
# never finishing, because create_file on its own just-created file was
# unconditionally rejected. Scoped to this process's lifetime (module-level,
# reset on every fresh dispatch/rework subprocess) so a REWORK's inherited
# file - which may need a surgical fix, not a wholesale rewrite - is still
# protected until the model creates it again itself in the new process.
_CREATED_THIS_RUN: set[str] = set()

# Companion to _CREATED_THIS_RUN for the RESUME/rework case. On a step-cap
# resume, the impl and test files already exist on disk from the interrupted
# run, so they are NOT in _CREATED_THIS_RUN in the fresh process - and the
# create_file guard would force the weak model onto str_replace it cannot
# construct. Requiring the model to view_file the target first makes the
# overwrite an INFORMED one (it read the current contents before replacing
# them), which preserves the guard's real purpose - stopping a blind clobber
# of a file the model has never seen - while unblocking the whole-file rewrite
# recovery path. Observed live 2026-07-16 (interval_merge resume): the guard
# steered a resumed run to str_replace, which then ground through 28+ rejected
# surgical-edit cycles (~2310s) instead of one whole-file rewrite.
_VIEWED_THIS_RUN: set[str] = set()

_DONE_REASONS = {0: "done", 1: "error", 2: "parked", 3: "infra_failure"}
