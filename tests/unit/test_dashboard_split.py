"""RH-09: split app/dashboard.py under the 1000-line limit.

Contract (design chosen by the test author because the brief names no
module): the story-scoped routes move VERBATIM into ``app.dashboard_story_routes``
-- the journal / log / checklist / replay readers and the worktree-patch
propose / get / apply routes. ``app.dashboard`` keeps re-exporting every moved
name so existing ``from app.dashboard import X`` and ``d.X`` call sites keep
working, and every route path stays registered on ``dashboard.app`` ahead of
the catch-all StaticFiles mount.

The new module must NOT import ``app.dashboard`` (circular); shared state
(``_service``, ``PLAN_DIR`` ...) is resolved so that monkeypatching
``app.dashboard.<name>`` still reaches the moved routes.
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest

from app import dashboard as d
from tests.unit._dashboard_helpers import client, plan_dir  # noqa: F401

NEW_MODULE = "app.dashboard_story_routes"
LINE_LIMIT = 1000
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_line_limit_script():
    spec = importlib.util.spec_from_file_location(
        "check_line_limit_rh09", REPO_ROOT / "scripts" / "check_line_limit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_line_limit = _load_line_limit_script()


def _new():
    return importlib.import_module(NEW_MODULE)


def _line_count(rel: str) -> int:
    return check_line_limit.count_lines(REPO_ROOT / rel)


# --- size gate -------------------------------------------------------------


def test_should_keep_dashboard_at_or_under_line_limit():
    assert _line_count("app/dashboard.py") <= LINE_LIMIT


def test_should_keep_new_module_at_or_under_line_limit():
    assert _line_count("app/dashboard_story_routes.py") <= LINE_LIMIT


def test_should_drop_dashboard_allowlist_entry():
    assert "app/dashboard.py" not in check_line_limit._BLOCKING_ALLOWLIST


def test_should_leave_other_allowlist_entries_untouched():
    assert "scripts/local_agent.py" in check_line_limit._BLOCKING_ALLOWLIST


def test_should_pass_line_limit_gate_on_repo():
    assert check_line_limit.check(REPO_ROOT) == 0


# --- importability / no cycle ----------------------------------------------


def test_should_import_new_module():
    assert _new().__name__ == NEW_MODULE


def test_should_not_import_dashboard_from_new_module():
    source = (REPO_ROOT / "app/dashboard_story_routes.py").read_text()
    assert "app.dashboard import" not in source.replace("app.dashboard_", "")


# --- re-export identity ----------------------------------------------------


def test_should_reexport_journal_route_by_identity():
    assert d.get_story_journal is _new().get_story_journal


def test_should_reexport_log_route_by_identity():
    assert d.get_story_log is _new().get_story_log


def test_should_reexport_checklist_route_by_identity():
    assert d.get_story_checklist is _new().get_story_checklist


def test_should_reexport_replay_route_by_identity():
    assert d.get_story_replay is _new().get_story_replay


def test_should_reexport_patch_propose_route_by_identity():
    assert d.propose_worktree_patch_route is _new().propose_worktree_patch_route


def test_should_reexport_patch_get_route_by_identity():
    assert d.get_worktree_patch_route is _new().get_worktree_patch_route


def test_should_reexport_patch_apply_route_by_identity():
    assert d.apply_worktree_patch_route is _new().apply_worktree_patch_route


def test_should_define_moved_function_in_new_module():
    assert _new().get_story_journal.__module__ == NEW_MODULE


# --- routes still served ---------------------------------------------------


def _paths():
    return [getattr(r, "path", None) for r in d.app.routes]


@pytest.mark.parametrize(
    "path",
    [
        "/api/plans/{plan_name}/stories/{story_key}/journal",
        "/api/plans/{plan_name}/stories/{story_key}/log",
        "/api/plans/{plan_name}/stories/{story_key}/checklist",
        "/api/plans/{plan_name}/stories/{story_key}/replay",
        "/api/worktree/patch/propose",
        "/api/worktree/patch/{patch_id}",
        "/api/worktree/patch/{patch_id}/apply",
    ],
)
def test_should_keep_route_registered_on_app(path):
    assert path in _paths()


def test_should_keep_static_mount_as_last_route_after_moved_routes():
    last = d.app.routes[-1]
    assert (getattr(last, "name", None), _paths().index("/api/worktree/patch/propose") < len(d.app.routes) - 1) == ("static", True)


# --- monkeypatch reach -----------------------------------------------------


class _StubService:
    def __init__(self):
        self.asked = []
        self.manifest = None

    def get_manifest_or_none(self, plan_name):
        self.asked.append(plan_name)
        return self.manifest


def test_should_reach_moved_route_when_patching_dashboard_service(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(d, "_service", stub)

    resp = client.get("/api/plans/p1/stories/s1/journal")

    assert (resp.status_code, stub.asked) == (404, ["p1"])


def test_should_reach_moved_route_when_patching_new_module_service(client, monkeypatch):
    stub = _StubService()
    monkeypatch.setattr(_new(), "_service", stub)

    resp = client.get("/api/plans/p2/stories/s1/journal")

    assert (resp.status_code, stub.asked) == (404, ["p2"])


# --- negative: what was not moved stays put --------------------------------


def test_should_keep_health_route_defined_in_dashboard():
    assert d.health.__module__ == "app.dashboard"


def test_should_keep_app_defined_in_dashboard():
    assert d.app.title == "Agent Pipeline Dashboard"


def test_should_not_define_app_in_new_module():
    assert not hasattr(_new(), "app")
