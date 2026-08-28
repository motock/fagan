"""Regression test: advance_pipeline's returned "autonomy" field must be a
plain string, not the internal `pipeline.advance._ServerRef` proxy object.

`_advance_pipeline_locked_impl` (pipeline/advance.py) reads the module-level
`PIPELINE_AUTONOMY = _ServerRef("PIPELINE_AUTONOMY")` binding and embeds it
directly into the dict it returns (`"autonomy": PIPELINE_AUTONOMY`), in both
the dry-run branch and the normal-tick summary. `_ServerRef` implements the
comparison/arithmetic dunders needed for `PIPELINE_AUTONOMY == "dry-run"` and
`MAX_CONCURRENT_AGENTS > 0` to work correctly, but it is not a JSON-native
type, so every real call through the MCP tool boundary (which must
JSON-serialize the return value) fails with:

    Unable to serialize unknown type: <class 'pipeline.advance._ServerRef'>

No existing test caught this because the suite calls `advance_pipeline`
directly in-process and only ever asserts on individual dict keys (e.g.
"paused", "dispatched") - it never round-trips the result through
`json.dumps`, so the unresolved proxy object sitting under "autonomy" was
never exercised. This test reproduces the failure at the same boundary the
real MCP transport uses.
"""

import json

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _plane_configured,
    _write_manifest,
    plan_dir,
)


def test_advance_pipeline_result_is_json_serializable(plan_dir):
    """Normal-tick path: an empty-stories manifest still reaches the final
    `return {"ok": True, **summary}` with "autonomy" set - it must be a str
    that survives json.dumps, not the raw _ServerRef proxy."""
    _write_manifest(plan_dir, "autonomyplan", {})

    result = p.advance_pipeline("autonomyplan")

    assert isinstance(result["autonomy"], str), (
        f"'autonomy' must be a plain str, got {type(result['autonomy'])!r}: "
        f"{result['autonomy']!r}"
    )
    json.dumps(result)  # must not raise "Unable to serialize unknown type"


def test_advance_pipeline_dry_run_result_is_json_serializable(plan_dir, monkeypatch):
    """Dry-run branch builds its own dict with the same "autonomy": PIPELINE_AUTONOMY
    line - it must be fixed too, not just the normal-tick summary."""
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "dry-run")
    _write_manifest(plan_dir, "autonomyplan_dryrun", {})

    result = p.advance_pipeline("autonomyplan_dryrun")

    assert result["dry_run"] is True
    assert isinstance(result["autonomy"], str), (
        f"'autonomy' must be a plain str, got {type(result['autonomy'])!r}: "
        f"{result['autonomy']!r}"
    )
    assert result["autonomy"] == "dry-run"
    json.dumps(result)
