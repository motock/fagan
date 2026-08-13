import os

import pytest

# Cleared here, at conftest.py's own module-import time, because pytest
# imports conftest.py before collecting test modules - clearing only inside
# an autouse fixture is too late for module-level constants (e.g.
# pipeline.usage's SESSION_PAUSE_THRESHOLD, scripts/local_agent.py's
# PARK_ENABLED) that read PIPELINE_*/LOCAL_AGENT_* via os.environ.get(...)
# once at their own import time, during collection. LOCAL_AGENT_* matters
# when the suite runs inside a dispatched agent's own bash tool: the
# scheduler plist sets LOCAL_AGENT_PARK_ENABLED=0, which leaks into the
# agent's pytest subprocess and makes every park-expecting guard test fail
# on a correct codebase (observed live 2026-07-22 - the agent could never
# see green, so it "fixed" a correct file until it broke).
_ISOLATED_ENV_PREFIXES = ("PIPELINE_", "LOCAL_AGENT_")

for _key in list(os.environ):
    if _key.startswith(_ISOLATED_ENV_PREFIXES):
        del os.environ[_key]


@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(_ISOLATED_ENV_PREFIXES):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _isolate_plan_dir(tmp_path_factory, monkeypatch):
    """Redirect pipeline.persistence.PLAN_DIR away from the operator's real
    ~/.claude/plans/ for every test in this suite by default.

    pipeline/persistence.py reads its own module-level PLAN_DIR binding
    (imported from pipeline.paths at module load), separate from
    pipeline.server's own binding. A test that patches only p.PLAN_DIR (the
    server's copy) therefore leaves _append_journal/_notify_user writing
    into the real plans directory - observed live: 344 step-cap journal
    records accumulated in ~/.claude/plans/cap1.S1.journal.json from
    test_check_story_status_lint_gate.py alone. Tests that need a specific
    shared PLAN_DIR (e.g. the `plan_dir` fixture, which also patches
    pipeline.server.PLAN_DIR/pipeline.concurrency.PLAN_DIR to the same
    directory) simply monkeypatch persistence.PLAN_DIR again afterward,
    which composes fine with this default.

    Uses tmp_path_factory (a directory tree independent of this test's own
    tmp_path) rather than tmp_path itself: several existing test files
    define their own `plan_dir` fixture that does `(tmp_path /
    "plans").mkdir()`, and pre-creating that same path here would collide
    with it (FileExistsError).
    """
    from pipeline import persistence
    default_plan_dir = tmp_path_factory.mktemp("default_plan_dir")
    monkeypatch.setattr(persistence, "PLAN_DIR", default_plan_dir)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Shared PLAN_DIR fixture for test modules that do not define their own.

    Patches pipeline.server, pipeline.persistence, and pipeline.concurrency
    PLAN_DIR bindings to the same directory so manifest writes/journals land
    in the test tmp_path. Test modules that define their own ``plan_dir``
    fixture shadow this one (pytest resolves module-level fixtures first).
    """
    import pipeline.server as p
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d
