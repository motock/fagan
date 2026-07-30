from pathlib import Path

import pytest

import pipeline.server as p

# Test that the registered tool function is the guarded wrapper

def test_registry_identity():
    assert p.mcp._tool_manager._tools["review_story"].fn is p.review_story
    assert "advance_pipeline" in p.mcp._tool_manager._tools
# Test that validation occurs before attempting to acquire a lock

def test_validation_ordering(tmp_path):
    bad_plan = "../../../../tmp/x"
    with pytest.raises(ValueError):
        p.review_story(bad_plan, "S1")
    # Verify no lock file was created outside the plan directory
    lock_file = Path.cwd() / f"{bad_plan}.lock"
    assert not lock_file.exists()
