"""WS-11: ``PipelineService.save_plan`` must establish ``repo_root`` server-side.

The model-authored plan JSON is untrusted: when a ``workspace`` is supplied the
service must validate it with ``pipeline.workspace.validate_workspace`` and
OVERWRITE/INJECT the plan's top-level ``repo_root`` with the validated resolved
path.  Without ``workspace`` today's behaviour is preserved byte-for-byte.

``validate_workspace`` is stubbed (synthetic results, ``tmp_path`` only) at its
real integration point — the name ``pipeline.service.validate_workspace`` — and
stubs the REAL convention: a dict ``{"ok": bool, "path": str, "error": str|None}``
that never raises.
"""

import json

import pytest

from pipeline import server as p


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """PLAN_DIR redirected into tmp_path (mirrors test_save_plan_migration)."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


def _install_fake_validate(monkeypatch, ws_valid):
    """Stub validate_workspace with the real dict convention, synthetic only.

    ``raising=False`` because the red phase runs before service.py imports the
    name; the patch still binds once the import lands.
    """

    def fake(workspace):
        if "forbidden" in workspace:
            return {"ok": False, "path": "", "error": "workspace is not allowed"}
        # Simulate normalize_workspace_path: the RESOLVED clean path comes back,
        # never the raw input (which may contain "..").
        return {"ok": True, "path": ws_valid, "error": None}

    monkeypatch.setattr("pipeline.service.validate_workspace", fake, raising=False)


# --- T1: workspace=None preserves today's behaviour byte-for-byte -----------


def test_t1_workspace_none_preserves_existing_repo_root(plan_dir):
    """Regression guard for existing callers: no workspace -> the plan JSON's
    own repo_root is written untouched (file is the impl's indent=2 dump of the
    parsed plan, exactly as today)."""
    plan = '{"repo_root": "/model/x","epics":[]}'
    svc = p.PipelineService()
    r = svc.save_plan("t1", plan)
    assert r["ok"] is True
    assert r["path"].endswith("t1.json")
    written = (plan_dir / "t1.json").read_text()
    assert written == json.dumps(json.loads(plan), indent=2)
    assert '"repo_root": "/model/x"' in written


# --- T2: valid workspace OVERWRITES a wrong repo_root (asserted on DISK) ----


def test_t2_valid_workspace_overwrites_hallucinated_repo_root(
    plan_dir, tmp_path, monkeypatch
):
    ws_valid = str(tmp_path / "data_ws")
    _install_fake_validate(monkeypatch, ws_valid)
    plan = '{"repo_root": "/home/model/hallucinated", "epics": []}'
    svc = p.PipelineService()
    r = svc.save_plan("t2", plan, workspace=ws_valid)
    assert r["ok"] is True
    text = (plan_dir / "t2.json").read_text()
    assert f'"repo_root": "{ws_valid}"' in text
    assert "hallucinated" not in text


# --- T3: valid workspace INJECTS repo_root when the plan has none -----------


def test_t3_valid_workspace_injects_missing_repo_root(
    plan_dir, tmp_path, monkeypatch
):
    ws_valid = str(tmp_path / "data_ws")
    _install_fake_validate(monkeypatch, ws_valid)
    svc = p.PipelineService()
    r = svc.save_plan("t3", '{"epics": []}', workspace=ws_valid)
    assert r["ok"] is True
    text = (plan_dir / "t3.json").read_text()
    assert f'"repo_root": "{ws_valid}"' in text


# --- T4: invalid workspace -> ok=False AND nothing written (no corruption) --


def test_t4_invalid_workspace_returns_error_and_writes_nothing(
    plan_dir, tmp_path, monkeypatch
):
    ws_valid = str(tmp_path / "data_ws")
    _install_fake_validate(monkeypatch, ws_valid)
    svc = p.PipelineService()

    r1 = svc.save_plan("t4", '{"repo_root": "/orig/root", "epics": []}')
    assert r1["ok"] is True
    path = plan_dir / "t4.json"
    before = path.read_text()
    assert '"repo_root": "/orig/root"' in before

    r = svc.save_plan(
        "t4",
        '{"repo_root": "/new/stuff", "epics": []}',
        workspace=str(tmp_path / "forbidden_ws"),
    )
    assert r["ok"] is False
    assert r["error"] == "workspace is not allowed"
    # The rejected call must not have corrupted the previously saved plan.
    assert path.read_text() == before

    # Follow-up call proves the rejected call left clean on-disk state.
    r2 = svc.save_plan("t4", '{"repo_root": "/junk", "epics": []}', workspace=ws_valid)
    assert r2["ok"] is True
    assert f'"repo_root": "{ws_valid}"' in path.read_text()


# --- T5: malformed JSON -> ok=False, no raise, nothing written --------------


def test_t5_malformed_json_returns_error_not_raise(plan_dir):
    svc = p.PipelineService()
    r1 = svc.save_plan("t5", '{"repo_root": "/seed", "epics": []}')
    assert r1["ok"] is True
    path = plan_dir / "t5.json"
    before = path.read_text()

    r = svc.save_plan("t5", '{"repo_root": /broken,')
    assert r["ok"] is False
    assert r["error"]
    assert path.read_text() == before


# --- T6: valid JSON that is a LIST -> ok=False, no raise, nothing written ---


def test_t6_json_list_returns_error_not_raise(plan_dir):
    svc = p.PipelineService()
    r1 = svc.save_plan("t6", '{"repo_root": "/seed", "epics": []}')
    assert r1["ok"] is True
    path = plan_dir / "t6.json"
    before = path.read_text()

    r = svc.save_plan("t6", '[{"repo_root": "/x"}]')
    assert r["ok"] is False
    assert r["error"]
    assert path.read_text() == before


# --- T7: injected repo_root is the RESOLVED path, never the raw input ------


def test_t7_injected_repo_root_is_resolved_not_raw(plan_dir, tmp_path, monkeypatch):
    ws_valid = str(tmp_path / "data_ws")
    _install_fake_validate(monkeypatch, ws_valid)
    raw = ws_valid + "/../data_ws"
    assert "/.." in raw  # the raw input really does contain traversal
    svc = p.PipelineService()
    r = svc.save_plan("t7", '{"epics": []}', workspace=raw)
    assert r["ok"] is True
    text = (plan_dir / "t7.json").read_text()
    assert f'"repo_root": "{ws_valid}"' in text
    assert "/.." not in text