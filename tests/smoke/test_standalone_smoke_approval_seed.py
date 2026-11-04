"""The standalone smoke must run its stack with the plan-approval gate ON.

``install.sh`` writes ``PIPELINE_PLAN_APPROVAL=0`` when it finds a pre-existing
state ``.env`` that does not name the key (SAD-02 behaviour), and ``1`` when the
key is absent and the file is new.  ``deploy/standalone/smoke.sh`` seeds a
partial state ``.env`` through ``seed_credentials()`` and then runs
``install.sh --non-interactive``; if the seeded file omits the key, the
installer treats the state dir as a pre-existing install, writes ``=0``, and
the smoke never grades the on-by-default gate - its ``approve_plan`` call
silently approves nothing.

Two edits keep the gate on, and both are needed:

* ``seed_credentials()`` writes ``PIPELINE_PLAN_APPROVAL=1`` into ``$ENV_FILE``;
* the ``grep -E`` alternation that copies seeded keys from ``$ENV_FILE`` into
  ``$STATE_DIR/.env`` lists ``PIPELINE_PLAN_APPROVAL``, otherwise the line is
  dropped and the installer appends ``=0``.

These are static text checks on the script: no Docker daemon, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = REPO_ROOT / "deploy" / "standalone" / "smoke.sh"

APPROVAL_KEY = "PIPELINE_PLAN_APPROVAL"


def smoke_text() -> str:
    return SMOKE.read_text(encoding="utf-8")


def seed_credentials_body() -> str:
    match = re.search(
        r"^seed_credentials\(\) \{\n(.*?)^\}", smoke_text(), re.MULTILINE | re.DOTALL
    )
    assert match, "seed_credentials() not found in deploy/standalone/smoke.sh"
    return match.group(1)


def seed_grep_alternation() -> str:
    """The alternation that copies seeded keys into ``$STATE_DIR/.env``."""
    match = re.search(
        r"grep -E ['\"]\^\(([^)]*)\)=.*?\"\$ENV_FILE\"\s*>\s*\"\$STATE_DIR/\.env\"",
        smoke_text(),
        re.DOTALL,
    )
    assert match, (
        "deploy/standalone/smoke.sh must seed the state .env by grepping "
        "$ENV_FILE; the pattern could not be found"
    )
    return match.group(1)


def test_seed_credentials_writes_the_approval_gate_on() -> None:
    """The seeded $ENV_FILE must carry an explicit ``PIPELINE_PLAN_APPROVAL=1``."""
    body = seed_credentials_body()
    lines = [
        line.strip()
        for line in body.splitlines()
        if re.fullmatch(
            rf"printf ['\"]{APPROVAL_KEY}=1\\n['\"]", line.strip()
        )
    ]
    assert lines, (
        "seed_credentials() must write PIPELINE_PLAN_APPROVAL=1 into $ENV_FILE; "
        f"without it install.sh writes =0 and the smoke never grades the "
        f"approval gate. Body was:\n{body}"
    )


def test_seed_grep_alternation_copies_the_approval_gate() -> None:
    """The copy into $STATE_DIR/.env must include the approval key.

    Writing the line into $ENV_FILE is not enough: only keys matching the
    alternation reach $STATE_DIR/.env, and that is the file install.sh reads.
    """
    names = set(seed_grep_alternation().split("|"))
    assert APPROVAL_KEY in names, (
        "the seed grep alternation must include PIPELINE_PLAN_APPROVAL; "
        f"otherwise PIPELINE_PLAN_APPROVAL=1 is dropped from $STATE_DIR/.env and "
        f"install.sh appends =0. Alternation was: {sorted(names)}"
    )


def test_smoke_never_seeds_the_gate_off() -> None:
    """Negative guard: the fix must not be ``PIPELINE_PLAN_APPROVAL=0``."""
    assert f"{APPROVAL_KEY}=0" not in smoke_text(), (
        "deploy/standalone/smoke.sh must not seed PIPELINE_PLAN_APPROVAL=0: the "
        "whole point is to grade the on-by-default gate"
    )


def test_approve_plan_step_is_still_driven() -> None:
    """With the gate on, the approve_plan call must still be part of the run."""
    text = smoke_text()
    assert "mcp_tool approve_plan" in text, (
        "the smoke must still call the approve_plan MCP tool; with the gate on "
        "that call is what actually approves the plan before dispatch"
    )
