"""ingest_plan accepts, validates and refreshes the story ``blocked_by`` field.

Separator rule: each entry is ``<plan>:<story_key>`` - the plan part (before
the first colon) is non-empty and colon-free; the key part is non-empty and may
itself contain colons (so ``a:b:c`` is plan ``a``, key ``b:c``). Existence of
the referenced plan/story is not checked at ingest time.
"""
import json

import pytest

from pipeline import server as p
from pipeline import ticketing as pt


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **kw: pytest.fail("Plane must not be called")
    )
    return d


def _ingest(plan_dir, tmp_path, **story_extra):
    story = {"summary": "Do it", "key": "S1", **story_extra}
    plan = {"repo_root": str(tmp_path), "epics": [{"summary": "E", "stories": [story]}]}
    (plan_dir / "bb.json").write_text(json.dumps(plan))
    return p.ingest_plan("bb")


def _manifest(plan_dir):
    return json.loads((plan_dir / "bb.manifest.json").read_text())


def test_should_carry_valid_blocked_by_into_manifest(plan_dir, tmp_path):
    result = _ingest(plan_dir, tmp_path, blocked_by=["other:K1", "x.y-z:K_2"])

    assert result["ok"] is True
    assert _manifest(plan_dir)["stories"]["S1"]["blocked_by"] == ["other:K1", "x.y-z:K_2"]


def test_should_reject_bare_string_blocked_by(plan_dir, tmp_path):
    result = _ingest(plan_dir, tmp_path, blocked_by="plan:KEY")

    assert result["ok"] is False
    assert "'Do it'" in result["error"]
    assert "'plan:KEY'" in result["error"]


@pytest.mark.parametrize("bad", ["plan:", "nocolon", ":key", "a b:c", 5])
def test_should_reject_malformed_entry(plan_dir, tmp_path, bad):
    result = _ingest(plan_dir, tmp_path, blocked_by=[bad])

    assert result["ok"] is False
    assert "'Do it'" in result["error"]
    assert repr(bad) in result["error"]


def test_should_accept_colon_inside_key_part(plan_dir, tmp_path):
    result = _ingest(plan_dir, tmp_path, blocked_by=["a:b:c"])

    assert result["ok"] is True


def test_should_accept_empty_and_null_blocked_by(plan_dir, tmp_path):
    assert _ingest(plan_dir, tmp_path, blocked_by=[])["ok"] is True
    assert _ingest(plan_dir, tmp_path, blocked_by=None)["ok"] is True


def test_should_not_add_blocked_by_when_absent(plan_dir, tmp_path):
    _ingest(plan_dir, tmp_path)

    assert "blocked_by" not in _manifest(plan_dir)["stories"]["S1"]


def test_should_refresh_blocked_by_on_reingest_keeping_runtime_state(plan_dir, tmp_path):
    _ingest(plan_dir, tmp_path, blocked_by=["a:K1"])
    manifest = _manifest(plan_dir)
    manifest["stories"]["S1"]["status"] = "pr_open"
    (plan_dir / "bb.manifest.json").write_text(json.dumps(manifest))

    result = _ingest(plan_dir, tmp_path, blocked_by=["b:K2"])

    story = _manifest(plan_dir)["stories"]["S1"]
    assert result["ok"] is True
    assert story["blocked_by"] == ["b:K2"]
    assert story["status"] == "pr_open"


def test_should_clear_blocked_by_when_dropped_from_plan_on_reingest(plan_dir, tmp_path):
    _ingest(plan_dir, tmp_path, blocked_by=["a:K1"])

    _ingest(plan_dir, tmp_path)

    assert "blocked_by" not in _manifest(plan_dir)["stories"]["S1"]
