"""Generate the legacy audit fixtures from the pre-facade implementation.

Run on ``origin/master`` (before ``pipeline/audit.py`` became a facade over
``pipeline.audit_file.FileAuditLog``) with the clock frozen, so the committed
fixtures are byte-for-byte what the legacy writer produced:

    python tests/fixtures/audit/gen_legacy_audit_fixtures.py

The fixtures are the compatibility contract: a chain written before the
facade has to verify through the new API, and the bytes the new writer
appends next have to be the bytes the legacy writer would have appended.

This script deliberately lives outside ``scripts/``: ``tests/unit/
test_local_agent_split.py`` treats every new ``scripts/*.py`` as an extracted
sibling of ``scripts/local_agent.py`` and would demand it re-export
``_frozen_clock``/``main``.  Do not move it back.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent

# Importing ``app``/``pipeline`` needs the repository root on ``sys.path``.
REPO_ROOT = FIXTURES.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.identity import Principal
from pipeline import audit
from pipeline.actor import bind_correlation_id, bind_principal

#: The frozen moments the fixtures were written at.
PLAN_TS = "2026-11-07T09:15:00+00:00"
HOST_TS = "2026-11-07T10:30:00+00:00"


def _frozen_clock(ts: str) -> type[datetime]:
    """A ``datetime`` whose ``now()`` is the moment *ts* names."""
    moment = datetime.fromisoformat(ts)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment

    return _Clock


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    tmp = FIXTURES / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    audit.audit_path = lambda plan: tmp / f"{plan}.audit.jsonl"
    audit.host_audit_path = lambda: tmp / audit.HOST_AUDIT_FILE

    principal = Principal(
        subject="legacy-fixture",
        roles=frozenset({"admin"}),
        auth_method="fixture",
        correlation_id="fixture-correlation",
        client_id="fixture-client",
        principal_type="human",
    )

    with bind_principal(principal), bind_correlation_id("fixture-correlation"):
        audit.datetime = _frozen_clock(PLAN_TS)  # type: ignore[assignment]
        audit.append_audit(
            "legacy-plan",
            action="story.merge",
            outcome="allowed",
            target={"plan": "legacy-plan", "story": "S1"},
            detail={"branch": "feature/legacy"},
        )
        audit.datetime = _frozen_clock(PLAN_TS)  # type: ignore[assignment]
        audit.append_audit(
            "legacy-plan",
            action="plan.review",
            outcome="denied",
            target={"plan": "legacy-plan"},
            detail={"reason": "digest_mismatch"},
        )
        audit.datetime = _frozen_clock(HOST_TS)  # type: ignore[assignment]
        audit.append_host_audit(
            action="auth.remote_mcp",
            outcome="denied",
            target={"plan": ""},
            detail={"reason": "credential rejected"},
        )

    plan_bytes = (tmp / "legacy-plan.audit.jsonl").read_bytes()
    host_bytes = (tmp / ".audit.host.jsonl").read_bytes()
    (FIXTURES / "legacy.audit.jsonl").write_bytes(plan_bytes)
    (FIXTURES / "legacy.audit.host.jsonl").write_bytes(host_bytes)

    for name in os.listdir(tmp):
        (tmp / name).unlink()
    tmp.rmdir()
    print("wrote", FIXTURES / "legacy.audit.jsonl")
    print("wrote", FIXTURES / "legacy.audit.host.jsonl")


if __name__ == "__main__":
    main()
