from pathlib import Path

import pipeline
from pipeline.branch_names import story_branch


def test_should_lowercase_story_key_into_branch_name():
    assert story_branch("SETUP-01") == "agent/setup-01"


def test_should_leave_lowercase_key_unchanged():
    assert story_branch("setup-01") == "agent/setup-01"


def test_should_not_hardcode_branch_literal_outside_branch_names():
    pipeline_dir = Path(pipeline.__file__).parent
    offenders = [
        str(path.relative_to(pipeline_dir))
        for path in pipeline_dir.rglob("*.py")
        if path.name != "branch_names.py" and "agent/{" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
