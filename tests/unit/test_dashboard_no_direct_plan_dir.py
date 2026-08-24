"""Regression guard for W3b-A2b: dashboard must not parse PLAN_DIR directly.

After A2a rerouted every GET handler to PipelineService/Store, the dashboard's
local on-disk parse helpers became dead code. A2b deletes them so the dashboard
no longer depends on the on-disk layout (which blocks W4's DB move).

This test grades that the decoupling *fully* landed — not half-done. It fails
today (the helpers still exist) and passes only once every listed helper and
direct PLAN_DIR-parse pattern has been removed from ``app/dashboard.py``.

Scope notes:
  * ``PLAN_DIR`` itself is allowed to remain — it is still used by
    ``_dashboard_ui_state_path`` (a UI-preference file, not a plan artifact).
  * The forbidden helpers are the manifest/journal/decision/notification/story
    readers plus their path builders.
"""

from __future__ import annotations

import inspect

from app import dashboard

_SOURCE = inspect.getsource(dashboard)


def _assert_absent(pattern: str, label: str) -> None:
    assert pattern not in _SOURCE, (
        f"app/dashboard.py still contains the forbidden pattern {label!r}: "
        f"{pattern!r}. The A2b decoupling must delete this direct PLAN_DIR "
        f"parse helper/usage."
    )


def test_no_manifest_path_helper() -> None:
    """``_manifest_path(`` must be gone — manifest paths come from the Store."""
    _assert_absent("_manifest_path(", "_manifest_path(")


def test_no_read_manifest_helper() -> None:
    """``_read_manifest(`` must be gone — manifests are read via the Store."""
    _assert_absent("_read_manifest(", "_read_manifest(")


def test_no_list_plan_names_helper() -> None:
    """``_list_plan_names(`` must be gone — plan listing is the Store's job."""
    _assert_absent("_list_plan_names(", "_list_plan_names(")


def test_no_journal_helpers() -> None:
    """Both journal path builder and reader must be gone."""
    _assert_absent("_journal_path(", "_journal_path(")
    _assert_absent("_read_journal(", "_read_journal(")
    _assert_absent("_journal_final_ts(", "_journal_final_ts(")


def test_no_decisions_helpers() -> None:
    """``_decisions_path(`` and ``_read_decisions(`` must be gone."""
    _assert_absent("_decisions_path(", "_decisions_path(")
    _assert_absent("_read_decisions(", "_read_decisions(")


def test_no_notifications_helpers() -> None:
    """All notification path/tail helpers must be gone."""
    _assert_absent("_notifications_path(", "_notifications_path(")
    _assert_absent("_notifications_jsonl_path(", "_notifications_jsonl_path(")
    _assert_absent("_tail_notifications(", "_tail_notifications(")
    _assert_absent("_tail_notification_records(", "_tail_notification_records(")


def test_no_story_log_helper() -> None:
    """``_read_story_log(`` must be gone — story logs come from the Store."""
    _assert_absent("_read_story_log(", "_read_story_log(")


def test_no_worktree_file_helper() -> None:
    """``_read_worktree_file(`` must be gone — worktree reads come from the Store."""
    _assert_absent("_read_worktree_file(", "_read_worktree_file(")


def test_no_plan_dir_glob() -> None:
    """No direct ``PLAN_DIR.glob(...)`` directory scans remain.

    The plan list is served by the Store, so the dashboard must not glob the
    plan directory itself.
    """
    _assert_absent("PLAN_DIR.glob", "PLAN_DIR.glob")


def test_no_direct_manifest_json_loads() -> None:
    """No inline ``json.loads(<manifest>.read_text())`` parse of a manifest.

    A grep-style substring check catches a hand-rolled replacement reader that
    sidesteps the named helpers: any ``json.loads`` over a manifest file's
    ``read_text()`` is forbidden.
    """
    import re

    # ``json.loads(....manifest....read_text())`` in any spacing/ordering.
    pattern = re.compile(r"json\.loads\([^)]*manifest[^)]*read_text\(\)\)")
    assert not pattern.search(_SOURCE), (
        "app/dashboard.py still parses a manifest file inline via "
        "json.loads(...manifest...read_text()); route manifest reads through "
        "the Store instead."
    )


def test_plan_dir_still_defined_for_ui_state() -> None:
    """``PLAN_DIR`` itself is NOT removed — it stays for ``_dashboard_ui_state_path``.

    This guards against an over-deletion that rips out the UI-preference file
    support, which would break the archived-plans sidebar feature.
    """
    assert hasattr(dashboard, "PLAN_DIR"), (
        "PLAN_DIR must remain in app/dashboard.py — it backs "
        "_dashboard_ui_state_path (UI preferences), not plan parsing."
    )
    assert hasattr(dashboard, "_dashboard_ui_state_path"), (
        "_dashboard_ui_state_path must remain — PLAN_DIR stays ONLY for it."
    )
    assert "_dashboard_ui_state_path(" in _SOURCE, (
        "_dashboard_ui_state_path must still be defined and referenced."
    )


def test_dashboard_module_imports_cleanly() -> None:
    """Sanity: after deletions the module still imports without error.

    A deletion that leaves a dangling reference would surface here as an
    AttributeError/NameError at import time or on attribute access.
    """
    assert hasattr(dashboard, "app"), "dashboard.app FastAPI instance must exist"