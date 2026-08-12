from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentHandle:
    """A non-blocking agentic run (dispatch/review-style), identified by pid."""
    pid: int
    # The concrete model the agent actually boots with — for the local backend
    # this is the RESOLVED model (a logical tier like "sonnet" maps to e.g.
    # "minimax-m3:cloud" via PIPELINE_LOCAL_MODEL_DEFAULT), for the Claude
    # backend it's the model string passed verbatim. The orchestrator records
    # this on the manifest so the dashboard shows what really ran, not the
    # plan's declared tier. None for backends that don't surface it.
    model: str | None = None
