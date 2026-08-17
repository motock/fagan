"""Tests for W1c step 3 (docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md).

This story deliberately breaks app/dashboard.py's previous read-only
import-graph boundary: it must import PipelineService from pipeline.server
and instantiate a module-level singleton (`_service`) near the `app =
FastAPI(...)` line, and it must update the module docstring and the
WORKTREE_ROOT comment to stop claiming the dashboard is read-only / never
touches those files / carries none of the pipeline's risk surface.

No HTTP routes may be added in this story - route wiring that delegates to
`_service` is later work in this epic.

These tests describe behavior for code that does not exist yet on this
branch and must fail (ImportError/AttributeError/AssertionError) until
app/dashboard.py is updated.
"""
from __future__ import annotations

import re
from pathlib import Path

DASHBOARD_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "dashboard.py"

# The dashboard's HTTP surface as of the start of this story. This story must
# not add or remove any route - if this set no longer matches, route wiring
# was added that belongs to a later story in the epic.
_EXPECTED_API_ROUTES = {
    ("/api/health", "GET"),
    ("/api/dispatch_health", "GET"),
    ("/api/usage", "GET"),
    ("/api/plans", "GET"),
    ("/api/plans/{plan_name}/archive", "POST"),
    ("/api/plans/{plan_name}/unarchive", "POST"),
    ("/api/plans/{plan_name}/save", "POST"),
    ("/api/plans/{plan_name}/ingest", "POST"),
    ("/api/plans/{plan_name}/pause", "POST"),
    ("/api/plans/{plan_name}/resume", "POST"),
    ("/api/plans/{plan_name}", "GET"),
    ("/api/plans/{plan_name}/stories/{story_key}/journal", "GET"),
    ("/api/plans/{plan_name}/stories/{story_key}/log", "GET"),
    ("/api/plans/{plan_name}/stories/{story_key}/checklist", "GET"),
    ("/api/plans/{plan_name}/stories/{story_key}/dispatch", "POST"),
    ("/api/plans/{plan_name}/stories/{story_key}/start", "POST"),
    ("/api/plans/{plan_name}/stories/{story_key}/review", "POST"),
    ("/api/plans/{plan_name}/stories/{story_key}/approve_merge", "POST"),
    ("/api/plans/{plan_name}/stories/{story_key}/done", "POST"),
    ("/api/plans/{plan_name}/advance", "POST"),
    ("/api/plans/advance_all", "POST"),
    ("/api/plans/{plan_name}/stories/{story_key}/checkpoint", "POST"),
    ("/api/config", "GET"),
}


def _dashboard_source() -> str:
    return DASHBOARD_PATH.read_text()


def _normalize(text: str) -> str:
    """Collapse comment/docstring line-wrapping into one space-joined string.

    Lets substring checks survive the implementer rewrapping a sentence
    across a different set of lines than the original.
    """
    return " ".join(line.lstrip("#").strip() for line in text.splitlines())


def _worktree_root_comment_block() -> str:
    source = _dashboard_source()
    lines = source.splitlines()
    target_idx = next(
        i for i, line in enumerate(lines) if line.strip().startswith("WORKTREE_ROOT =")
    )
    start = target_idx
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    return "\n".join(lines[start:target_idx])


# --- happy path: the import + singleton exist and are wired correctly ---


def test_import_app_dashboard_succeeds():
    import app.dashboard  # noqa: F401 - success of the import is the assertion


def test_service_attribute_is_pipeline_service_instance():
    import app.dashboard as d
    from pipeline.server import PipelineService

    assert isinstance(d._service, PipelineService)


def test_dashboard_imports_pipeline_service_symbol_directly():
    import app.dashboard as d
    import pipeline.server

    assert d.PipelineService is pipeline.server.PipelineService


def test_pipeline_service_import_statement_present_in_source():
    source = _dashboard_source()
    assert re.search(
        r"^from pipeline\.server import .*\bPipelineService\b", source, re.MULTILINE
    ), "expected a top-level 'from pipeline.server import PipelineService' import"


def test_service_singleton_constructed_with_no_arguments():
    source = _dashboard_source()
    assert re.search(r"^_service\s*=\s*PipelineService\(\)\s*$", source, re.MULTILINE), (
        "expected a module-level '_service = PipelineService()' line with no "
        "constructor arguments"
    )


def test_service_singleton_declared_near_fastapi_app_line():
    source = _dashboard_source()
    lines = source.splitlines()
    app_line_idx = next(
        i for i, line in enumerate(lines) if line.strip().startswith("app = FastAPI(")
    )
    singleton_idx = next(
        i
        for i, line in enumerate(lines)
        if re.match(r"^_service\s*=\s*PipelineService\(\)\s*$", line.strip())
    )
    assert abs(singleton_idx - app_line_idx) <= 5, (
        "expected '_service = PipelineService()' to be declared near the "
        "'app = FastAPI(...)' line, not elsewhere in the module"
    )


# --- negative / boundary: no unrequested behavior slipped in ---


def test_no_http_routes_added_or_removed():
    import app.dashboard as d

    actual_routes = {
        (route.path, method)
        for route in d.app.routes
        for method in (getattr(route, "methods", None) or set())
        if method != "HEAD" and route.path.startswith("/api/")
    }
    assert actual_routes == _EXPECTED_API_ROUTES, (
        "this story must not add or remove any HTTP route - route wiring "
        "that delegates to _service belongs to a later story in this epic"
    )


def test_module_docstring_drops_read_only_never_touches_claim():
    import app.dashboard as d

    doc = d.__doc__ or ""
    assert "it never touches those files" not in doc


def test_module_docstring_drops_carries_none_of_risk_surface_claim():
    import app.dashboard as d

    doc = d.__doc__ or ""
    assert "carries none of the pipeline's risk surface" not in doc


def test_module_docstring_describes_write_delegation_to_service():
    import app.dashboard as d

    doc = _normalize(d.__doc__ or "")
    assert "_service" in doc, (
        "expected the module docstring to mention '_service' when describing "
        "the new read+write contract"
    )


def test_worktree_root_comment_drops_must_not_pull_write_surface_claim():
    comment = _normalize(_worktree_root_comment_block())
    assert "must not pull pipeline_mcp_server's write surface into its import graph" not in comment


def test_worktree_root_comment_drops_contract_is_read_only_claim():
    comment = _normalize(_worktree_root_comment_block())
    assert "the dashboard's contract is read-only" not in comment


def test_dashboard_module_still_has_a_docstring():
    import app.dashboard as d

    assert d.__doc__ is not None and d.__doc__.strip() != "", (
        "the module docstring must be updated in place, not deleted"
    )
