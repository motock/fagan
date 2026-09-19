"""LD90-W0-04: ``patch_story`` must emit an ``event="brief_patched"``
notification when it changes the ``agent_instructions`` of a story that has
already been dispatched.

The local-dispatch success metric counts "a human rewrote the brief after
dispatch" as a miss, so the rewrite needs a structured trace. The emission is
gated on three conditions holding at once: the patch touches
``agent_instructions``, the new text differs from the pre-patch text, and the
pre-patch manifest entry carries a truthy ``dispatched_at`` (stamped by
dispatch.py at dispatch time).

Escalation's automatic rebrief (``escalation.compose_rebriefed_instructions``)
does not route through ``patch_story`` and is already counted as ``escalated``,
so it must not produce ``brief_patched``.
"""
import json
from pathlib import Path

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _write_manifest,
    plan_dir,
)


def _records(pdir, plan):
    """Return the plan's notification records; a missing file means none."""
    path = pdir / f"{plan}.notifications.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _brief_patched(pdir, plan):
    return [r for r in _records(pdir, plan) if r.get("event") == "brief_patched"]


def test_patch_story_emits_brief_patched_for_dispatched_story(plan_dir):  # noqa: F811
    _write_manifest(plan_dir, "bp1", {
        "S1": {
            "summary": "s",
            "status": "in_progress",
            "agent_instructions": "Old.",
            "dispatched_at": "2026-09-18T00:00:00+00:00",
            "correlation_id": "cid-7",
        },
    })
    result = p.patch_story("bp1", "S1", {"agent_instructions": "New."})
    assert result["ok"] is True
    records = _brief_patched(plan_dir, "bp1")
    assert len(records) == 1
    assert records[0]["event"] == "brief_patched"
    assert records[0]["story_key"] == "S1"
    assert records[0]["correlation_id"] == "cid-7"


def test_patch_story_no_brief_patched_for_never_dispatched_story(plan_dir):  # noqa: F811
    _write_manifest(plan_dir, "bp2", {
        "S1": {"summary": "s", "status": "todo", "agent_instructions": "Old."},
    })
    result = p.patch_story("bp2", "S1", {"agent_instructions": "New."})
    assert result["ok"] is True
    assert _brief_patched(plan_dir, "bp2") == []


def test_patch_story_no_brief_patched_when_text_unchanged(plan_dir):  # noqa: F811
    _write_manifest(plan_dir, "bp3", {
        "S1": {
            "summary": "s",
            "status": "in_progress",
            "agent_instructions": "Old.",
            "dispatched_at": "2026-09-18T00:00:00+00:00",
        },
    })
    result = p.patch_story("bp3", "S1", {"agent_instructions": "Old."})
    assert result["ok"] is True
    assert _brief_patched(plan_dir, "bp3") == []


def test_patch_story_no_brief_patched_for_other_field(plan_dir):  # noqa: F811
    _write_manifest(plan_dir, "bp4", {
        "S1": {
            "summary": "s",
            "status": "in_progress",
            "agent_instructions": "Old.",
            "model": "sonnet",
            "dispatched_at": "2026-09-18T00:00:00+00:00",
        },
    })
    result = p.patch_story("bp4", "S1", {"model": "opus"})
    assert result["ok"] is True
    assert _brief_patched(plan_dir, "bp4") == []


def test_patch_story_no_brief_patched_for_unknown_story(plan_dir):  # noqa: F811
    _write_manifest(plan_dir, "bp5", {})
    result = p.patch_story("bp5", "NOPE", {"agent_instructions": "New."})
    assert result["ok"] is False
    assert _brief_patched(plan_dir, "bp5") == []


def test_reference_documents_brief_patched_event():
    lines = Path("REFERENCE.md").read_text().splitlines()
    start = lines.index("## Notification records")
    end = next(
        i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")
    )
    section = "\n".join(lines[start:end])
    assert "`brief_patched`" in section
