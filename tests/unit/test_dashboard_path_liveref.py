"""Grade the dashboard path-constant handoff to pipeline.server LiveRefs.

Story: app/dashboard.py must stop resolving PLAN_DIR / USAGE_STATE_PATH /
WORKTREE_ROOT itself (module-load ``os.environ.get`` snapshots frozen at
import time) and instead hold :class:`pipeline.live_ref.LiveRef` instances
that re-read the canonical ``pipeline.server`` bindings on every access.

Why this matters: the test suite patches ``pipeline.server.PLAN_DIR`` (and
friends) to temp dirs. If the dashboard takes a module-load copy, those
patches never reach it. A LiveRef keeps the dashboard glued to the canonical
binding while preserving the module-global names the rest of the suite
monkeypatches (rebinding ``app.dashboard.PLAN_DIR`` still wins -- it merely
shadows the ref, which is exactly what the four existing setattr tests do).

The tests below are RED until the dashboard edit lands:

* the three globals must be LiveRef instances (not Paths),
* they must follow live ``pipeline.server`` patches -- including a re-patch
  mid-test, proving nothing is snapshotted at import time,
* ``_dashboard_ui_state_path()`` must track the patched PLAN_DIR,
* the source must no longer contain an ``os.environ.get``/``os.getenv``
  resolution for any of the three names,
* the module-level assignment lines must survive (the existing
  test_dashboard_no_direct_plan_dir.py greps for ``^PLAN_DIR\\s*=`` and four
  existing tests rebind these globals), and
* FAILURE_MODES_DATASET_PATH and STATIC_DIR must be left exactly as they are.
"""
import inspect
import re
from pathlib import Path

import pytest

import pipeline.server
from app import dashboard
from pipeline.live_ref import LiveRef


def _source() -> str:
    """Return the full source text of app/dashboard.py as a single string."""
    return inspect.getsource(dashboard)


# The three names this story moves from os.environ snapshots to LiveRefs.
_MOVED_NAMES = ("PLAN_DIR", "USAGE_STATE_PATH", "WORKTREE_ROOT")


# ---------------------------------------------------------------------------
# 1. The three globals must be LiveRef instances.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", _MOVED_NAMES)
def test_moved_constants_are_liveref_instances(name):
    """Each moved global must be a pipeline.live_ref.LiveRef instance.

    A plain Path (the old os.environ.get snapshot) or any other proxy type
    fails this: the brief pins the concrete LiveRef class.
    """
    value = getattr(dashboard, name)
    assert isinstance(value, LiveRef), (
        f"app.dashboard.{name} must be a pipeline.live_ref.LiveRef instance; "
        f"got {type(value)!r} -- the os.environ.get snapshot is still in place"
    )


def test_liveref_imported_from_pipeline_live_ref():
    """The dashboard must import LiveRef from pipeline.live_ref.

    The brief adds ``from pipeline.live_ref import LiveRef`` to the existing
    import block, so the class must be reachable (and be the canonical class)
    via the dashboard module namespace.
    """
    assert getattr(dashboard, "LiveRef", None) is LiveRef, (
        "app/dashboard.py must add `from pipeline.live_ref import LiveRef` "
        "to its existing import block"
    )


# ---------------------------------------------------------------------------
# 2. The refs must follow the canonical pipeline.server bindings LIVE.
# ---------------------------------------------------------------------------
def test_plan_dir_follows_pipeline_server_binding(monkeypatch, tmp_path):
    """Patching pipeline.server.PLAN_DIR must be visible through the dashboard.

    The re-patch halfway through proves the ref re-reads per access instead
    of snapshotting the binding at import time.
    """
    first = tmp_path / "plans-a"
    second = tmp_path / "plans-b"
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", first)
    assert dashboard.PLAN_DIR / "probe.txt" == first / "probe.txt", (
        "app.dashboard.PLAN_DIR did not follow pipeline.server.PLAN_DIR"
    )
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", second)
    assert dashboard.PLAN_DIR / "probe.txt" == second / "probe.txt", (
        "app.dashboard.PLAN_DIR kept a stale value after pipeline.server."
        "PLAN_DIR was re-patched (it must resolve live, per access)"
    )


def test_worktree_root_follows_pipeline_server_binding(monkeypatch, tmp_path):
    """Same live-follow contract for WORKTREE_ROOT."""
    patched = tmp_path / "worktrees"
    monkeypatch.setattr(pipeline.server, "WORKTREE_ROOT", patched)
    assert dashboard.WORKTREE_ROOT / "story-1" == patched / "story-1", (
        "app.dashboard.WORKTREE_ROOT did not follow pipeline.server."
        "WORKTREE_ROOT"
    )


def test_usage_state_path_follows_pipeline_server_binding(monkeypatch, tmp_path):
    """Same live-follow contract for USAGE_STATE_PATH, via real file reads.

    Exercises attribute forwarding (exists/read_text) as well as resolution.
    """
    state = tmp_path / "usage_state.json"
    state.write_text('{"stories": []}', encoding="utf-8")
    monkeypatch.setattr(pipeline.server, "USAGE_STATE_PATH", state)
    assert dashboard.USAGE_STATE_PATH / "x" == state / "x", (
        "app.dashboard.USAGE_STATE_PATH did not follow pipeline.server."
        "USAGE_STATE_PATH"
    )
    assert dashboard.USAGE_STATE_PATH.exists() is True, (
        "app.dashboard.USAGE_STATE_PATH.exists() did not see the file written "
        "at the patched pipeline.server.USAGE_STATE_PATH"
    )
    assert (
        dashboard.USAGE_STATE_PATH.read_text(encoding="utf-8")
        == '{"stories": []}'
    ), (
        "app.dashboard.USAGE_STATE_PATH.read_text() did not read the file at "
        "the patched pipeline.server.USAGE_STATE_PATH"
    )


def test_liveref_resolution_does_not_require_existing_path(monkeypatch, tmp_path):
    """Boundary: resolving the ref must not require the path to exist yet."""
    missing = tmp_path / "plans" / "not_created_yet"
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", missing)
    assert dashboard.PLAN_DIR / "state.json" == missing / "state.json", (
        "resolving app.dashboard.PLAN_DIR against a not-yet-existing "
        "pipeline.server.PLAN_DIR must work without touching the filesystem"
    )


# ---------------------------------------------------------------------------
# 3. _dashboard_ui_state_path must track the patched PLAN_DIR.
# ---------------------------------------------------------------------------
def test_dashboard_ui_state_path_tracks_patched_plan_dir(monkeypatch, tmp_path):
    """``_dashboard_ui_state_path()`` returns <patched PLAN_DIR>/.dashboard_ui_state.json.

    This is the call site the brief says must keep working with NO edit,
    because LiveRef implements __truediv__.
    """
    monkeypatch.setattr(pipeline.server, "PLAN_DIR", tmp_path)
    result = dashboard._dashboard_ui_state_path()
    assert result == tmp_path / ".dashboard_ui_state.json", (
        "_dashboard_ui_state_path() did not track the patched "
        "pipeline.server.PLAN_DIR"
    )
    assert isinstance(result, Path), (
        "_dashboard_ui_state_path() must return a real Path "
        f"(LiveRef.__truediv__ yields one); got {type(result)!r}"
    )


def test_rebinding_dashboard_module_global_still_wins(monkeypatch, tmp_path):
    """Compat: monkeypatch.setattr(app.dashboard, "PLAN_DIR", ...) still works.

    Four existing tests rebind the module global over the LiveRef; that
    rebind must shadow the ref for _dashboard_ui_state_path.
    """
    monkeypatch.setattr(dashboard, "PLAN_DIR", tmp_path)
    assert dashboard._dashboard_ui_state_path() == (
        tmp_path / ".dashboard_ui_state.json"
    ), "rebinding app.dashboard.PLAN_DIR no longer drives _dashboard_ui_state_path"


# ---------------------------------------------------------------------------
# 4. NEGATIVE/REGRESSION: no self-resolved env lookups for the moved names.
# ---------------------------------------------------------------------------
def test_no_os_environ_resolution_of_moved_paths():
    """app/dashboard.py must not resolve the three paths via os.environ.

    Mirrors the source-grep style of test_dashboard_no_direct_plan_dir.py:
    both the brief's double-quoted form and a single-quoted / os.getenv
    variant are forbidden, so the snapshot cannot dodge the grep by
    re-quoting.
    """
    src = _source()
    for name in _MOVED_NAMES:
        for pattern in (
            r'os\.environ\.get\(\s*["\']' + name + r'["\']',
            r'os\.getenv\(\s*["\']' + name + r'["\']',
        ):
            assert not re.search(pattern, src), (
                f"app/dashboard.py still resolves {name} itself via "
                f"{pattern!r}; it must read the canonical pipeline.server "
                "binding through a LiveRef instead"
            )


def test_module_level_assignment_lines_survive():
    """The three module-global assignment lines must remain.

    test_dashboard_no_direct_plan_dir.py asserts ``^PLAN_DIR\\s*=`` and
    hasattr(dashboard, "PLAN_DIR"); the same must hold for all three names so
    the existing monkeypatch.setattr call sites keep working.
    """
    src = _source()
    for name in _MOVED_NAMES:
        assert re.search(r"^" + name + r"\s*=", src, re.MULTILINE), (
            f"the module-level `{name} =` assignment line was removed from "
            "app/dashboard.py; it must stay (as a LiveRef instance)"
        )
        assert hasattr(dashboard, name), (
            f"app.dashboard.{name} module global was removed"
        )


def test_failure_modes_dataset_path_and_static_dir_untouched():
    """FAILURE_MODES_DATASET_PATH and STATIC_DIR must be left exactly as-is."""
    src = _source()
    assert 'os.environ.get("FAILURE_MODES_DATASET_PATH"' in src, (
        "FAILURE_MODES_DATASET_PATH must keep its os.environ.get resolution; "
        "it is not part of this story"
    )
    assert hasattr(dashboard, "FAILURE_MODES_DATASET_PATH"), (
        "FAILURE_MODES_DATASET_PATH module global was removed"
    )
    assert 'STATIC_DIR = Path(__file__).parent.parent / "static"' in src, (
        "the STATIC_DIR assignment was changed; it must be left exactly as "
        "it was"
    )
    assert hasattr(dashboard, "STATIC_DIR"), "STATIC_DIR module global was removed"