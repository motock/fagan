"""Companion MCP server exposing only the pipeline's adoptable subset.

Plan B5's companion-server item (B5-03): a second, smaller MCP server
named ``pipeline-companion`` that a foreign harness can adopt piecemeal,
without taking on the whole pipeline orchestrator. It exposes exactly the two
exported ideas from Plan B5:

  * the overlord decision path (``escalate_decision`` - the same
    question/options/context/policy decision-prompt shape the main pipeline
    server's ``request_decision`` tool assembles, ruled on by the overlord
    persona), and
  * the acceptance-oracle helpers (``classify_oracle_outcome`` /
    ``acceptance_digests`` - grade on fixtures, not the model's own tests).

No logic is duplicated here: the tools import the real ``pipeline.overlord``
and ``pipeline.oracle_gate`` modules and delegate to them (no duplication of
their implementations), so a fix landed upstream is picked up here too. The
imports happen lazily inside each tool body (the same circular-avoidance
pattern pipeline/overlord.py uses for its server globals) and go through the
module object, so tests - and any harness - can patch
``pipeline.overlord._invoke_overlord`` / ``pipeline.oracle_gate.*`` and the
companion picks up the patched binding at call time.

Run with: python -m pipeline.companion_server
"""

import logging

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("pipeline-companion")

# MCPServer's constructor calls logging.basicConfig(level=INFO), which the httpx
# and httpcore loggers (NOTSET) then inherit - so every HTTP call logs an INFO
# "HTTP Request: ..." line. Under launchd's stderr redirect that floods the
# unattended logs. Cap them at WARNING so genuine HTTP problems still surface
# but routine request chatter doesn't (same suppression as pipeline/server.py).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@mcp.tool()
def escalate_decision(
    question: str,
    options: list[str],
    context: str = "",
) -> str:
    """
    Escalate a blocking decision to the overlord, which rules on the user's
    behalf per the decision policy. Assembles the same decision-prompt shape
    the main pipeline server's request_decision tool uses (question,
    enumerated options, context, decision policy) and returns the overlord's
    ruling text. Call this when you are blocked on a choice the user would
    normally make, in a harness that has adopted only this companion server.
    """
    # Lazy import, called through the module object: pipeline.overlord's
    # helpers read server globals patched by tests via p.<name>, and resolving
    # the attribute at call time sees any patch of
    # pipeline.overlord._invoke_overlord / _load_policy (same seam the main
    # server relies on; a module-top import would freeze the unpatched
    # reference). Also avoids a module-load import cycle.
    from pipeline import overlord

    opts = "\n".join(f"  - {option}" for option in options)
    prompt = (
        f"QUESTION: {question}\n\n"
        f"OPTIONS:\n{opts}\n\n"
        f"CONTEXT: {context}\n\n"
        f"DECISION POLICY:\n{overlord._load_policy()}\n\n"
        f"Rule now, using your output contract exactly."
    )
    return overlord._invoke_overlord(prompt)


@mcp.tool()
# The {} default is part of the exported tool signature; the dict is never
# mutated (and is not forwarded to the classifier at all), so the
# shared-mutable-default hazard does not apply.
def classify_oracle_outcome(returncode: int, output: str, story: dict = {}) -> dict:  # noqa: B006
    """
    Classify a test-runner outcome as "passes", "empty", "errors", or
    "fails_correctly" per the acceptance-oracle pattern. ``story`` is accepted
    for call-shape compatibility with a dispatching harness but is not part of
    the classification - the oracle grades the fixture run (returncode +
    output), never the story manifest.
    """
    from pipeline import oracle_gate

    return oracle_gate.classify_oracle_outcome(returncode, output)


@mcp.tool()
def acceptance_digests(story: dict) -> dict:
    """
    Map each acceptance entry's path to a sha256 hex digest of its
    authoritative manifest ``source`` (never of the file on disk, which could
    already have been rewritten). Returns {} when the story has no acceptance
    entries.
    """
    from pipeline import oracle_gate

    return oracle_gate.acceptance_digests(story)


if __name__ == "__main__":
    mcp.run()
