"""Ingest-time warning for stories that declare a file with no line-limit headroom."""

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline.scope_headroom import scope_headroom_warning
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _isolate_usage_state,
)

PREFLIGHT = "Preflight: test fixture - not a real plan\n"


def _write(root, rel, lines):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines)


def test_should_warn_with_headroom_one_for_999_line_file(tmp_path):
    _write(tmp_path, "pipeline/big.py", 999)

    warning = scope_headroom_warning({"files": ["pipeline/big.py"]}, str(tmp_path))

    assert "pipeline/big.py" in warning
    assert "999" in warning
    assert "headroom 1" in warning
    assert warning.endswith(
        "declare the sibling module the new code will move to, or split the story"
    )


def test_should_not_warn_for_900_line_file(tmp_path):
    _write(tmp_path, "pipeline/ok.py", 900)

    assert scope_headroom_warning({"files": ["pipeline/ok.py"]}, str(tmp_path)) is None


def test_should_not_warn_when_headroom_equals_margin(tmp_path):
    _write(tmp_path, "pipeline/edge.py", 950)

    assert scope_headroom_warning({"files": ["pipeline/edge.py"]}, str(tmp_path)) is None


def test_should_warn_when_headroom_is_one_below_margin(tmp_path):
    _write(tmp_path, "pipeline/edge.py", 951)

    warning = scope_headroom_warning({"files": ["pipeline/edge.py"]}, str(tmp_path))

    assert "headroom 49" in warning


def test_should_name_only_the_full_file_of_two(tmp_path):
    _write(tmp_path, "pipeline/full.py", 990)
    _write(tmp_path, "pipeline/small.py", 10)

    warning = scope_headroom_warning(
        {"files": ["pipeline/full.py", "pipeline/small.py"]}, str(tmp_path)
    )

    assert "pipeline/full.py" in warning
    assert "pipeline/small.py" not in warning


def test_should_honor_custom_limit_and_margin(tmp_path):
    _write(tmp_path, "pipeline/a.py", 90)

    assert scope_headroom_warning({"files": ["pipeline/a.py"]}, str(tmp_path), limit=100, margin=20)
    assert scope_headroom_warning({"files": ["pipeline/a.py"]}, str(tmp_path), limit=100, margin=5) is None


def test_should_ignore_missing_file(tmp_path):
    assert scope_headroom_warning({"files": ["pipeline/nope.py"]}, str(tmp_path)) is None


def test_should_ignore_non_python_file(tmp_path):
    (tmp_path / "REFERENCE.md").write_text("line\n" * 999)

    assert scope_headroom_warning({"files": ["REFERENCE.md"]}, str(tmp_path)) is None


def test_should_ignore_test_path(tmp_path):
    _write(tmp_path, "tests/unit/test_big.py", 999)

    assert scope_headroom_warning({"files": ["tests/unit/test_big.py"]}, str(tmp_path)) is None


def test_should_return_none_without_files(tmp_path):
    assert scope_headroom_warning({"summary": "s"}, str(tmp_path)) is None


@pytest.mark.parametrize("files", [None, [], "pipeline/a.py", [None, 3]])
def test_should_not_raise_on_malformed_files(tmp_path, files):
    assert scope_headroom_warning({"files": files}, str(tmp_path)) is None


def test_should_not_raise_on_undecodable_file(tmp_path):
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\x00" * 10)

    scope_headroom_warning({"files": ["bad.py"]}, str(tmp_path))


@pytest.fixture
def headroom_plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def test_ingest_emits_one_scope_headroom_notice_and_succeeds(
    headroom_plan_dir, monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: calls.append((a, k)))
    repo = tmp_path / "repo"
    _write(repo, "pipeline/near.py", 999)
    story = {
        "key": "HR-1",
        "summary": "edit a near-limit file",
        "agent_instructions": PREFLIGHT + "Do it.",
        "files": ["pipeline/near.py"],
    }
    plan = {"repo_root": str(repo), "epics": [{"summary": "E1", "stories": [story]}]}
    (headroom_plan_dir / "headroom1.json").write_text(json.dumps(plan))

    result = p.ingest_plan("headroom1")

    assert result["ok"] is True
    scoped = [(a, k) for a, k in calls if k.get("event") == "scope_headroom"]
    assert len(scoped) == 1
    assert scoped[0][1]["story_key"] == "HR-1"
    assert "pipeline/near.py" in scoped[0][0][1]
