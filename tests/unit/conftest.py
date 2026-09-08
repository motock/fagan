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
def _isolate_registry_state():
    """Reset the model-registry memo around every test (function-scoped,
    autouse — mirrors _isolate_environ).

    app.role_registry.load_registry() memoizes the parsed model_registry.json
    per file version (stat-keyed) and returns a deep copy of that master, so
    a test that mutates the dict it got back can no longer poison a later
    caller. This fixture is the belt-and-braces guarantee that NO parsed
    registry state survives across tests on a shared pytest-xdist worker:
    before each test the memo is dropped (so the test re-parses whatever its
    environment points at — a leaked PIPELINE_MODEL_REGISTRY_PATH from a
    prior test can still redirect it, but never serve stale contents), and
    after each test it is dropped again (so a test that poisoned the memo
    cannot leak into the next test on the same worker).

    Without this, order/worker-dependent contamination is possible: worker
    runs [test_A (rewrites/redirects the registry), test_B (reads it)] —
    with the before-each reset, test_B's load_registry() always observes
    pristine state regardless of what test_A left behind.
    """
    from app import role_registry

    role_registry.reset_registry_cache()
    yield
    role_registry.reset_registry_cache()


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



# test_backend_*.py and test_acceptance_ollama_chat_retry.py instantiate
# OllamaDriver directly and call .complete()/.dispatch() to exercise its
# real retry/parsing/probe logic against a mocked HTTP transport - stubbing
# the method itself out from under them would make every assertion measure
# the stub instead of the driver. Excluded from the default stub below;
# every other file in the suite gets the fast no-op by default.
_REAL_OLLAMA_DRIVER_MODULES = {
    "test_backend_resource_status",
    "test_backend_review_loop",
    "test_backend_claude_driver_misc",
    "test_backend_local_provider_dispatch",
    "test_backend_tuning_knobs",
    "test_backend_role_routing_complete",
    "test_acceptance_ollama_chat_retry",
    "test_role_call_timeout",
    "test_role_call_timeout_messages",
    "test_chat_bare_passthrough",
}


@pytest.fixture(autouse=True)
def _hermetic_ollama_seams_default(request, monkeypatch):
    """Suite-wide default: stub the live-Ollama HTTP seams dispatch_story
    fires on every fresh local-family dispatch (the guided-decomposition
    planner's /api/chat call, the /api/tags loaded-model probe, and the
    serving-parallelism probe) to fast no-ops, so a test file that drives
    dispatch_story for real without its own stub doesn't silently pay for a
    live network round-trip.

    This is the same seam test_pipeline_mcp_server_*.py's own
    `_hermetic_ollama_seams` fixture (see _pipeline_mcp_server_test_helpers.py)
    already stubs for that file family - promoted here so a new test file
    can't reintroduce the same non-hermetic-dispatch bug two other files
    (test_w4l_dispatch_correlation.py, test_tdd_split_always_on.py) hit
    live: each drove real dispatch_story() calls with no stub in sight and
    paid tens of seconds per call once every role started routing to a
    cloud-backed model (4e11de2). A test that needs the real seam value
    overrides it with its own `monkeypatch.setattr` afterward, which runs
    later on this same function-scoped monkeypatch and wins.
    """
    module = request.module.__name__.rsplit(".", 1)[-1]
    from app import backend
    from pipeline import server as p

    if module not in _REAL_OLLAMA_DRIVER_MODULES:
        monkeypatch.setattr(
            backend.OllamaDriver, "complete", lambda self, prompt, **kw: ""
        )
        monkeypatch.setattr(backend, "_ollama_loaded_models", lambda ep: set())
        monkeypatch.setattr(backend, "_ollama_serving_parallelism", lambda: None)
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _authenticated_test_client(monkeypatch):
    """Attach the dashboard's API key header to every TestClient in this suite.

    app/dashboard.py registers `require_api_key` as an application-level
    dependency, so every route now answers 401 without an
    X-Pipeline-Api-Key header. The ~29 existing dashboard/chat test modules
    each build their own `TestClient(d.app)` and none send that header;
    patching the constructor here attaches it in one place instead of
    editing every call site. This adds the credential the tests were always
    implicitly running with — it does not relax the check itself.

    Caller-supplied headers win, so a test can still pass a wrong key (or
    pop the header off `client.headers`) to exercise the denial path.

    Modules in _SELF_MANAGED_AUTH_MODULES build their own TestClients and
    manage their own auth headers (including deliberately sending none to
    exercise the 401 denial path), so the force-attach is skipped for them
    — attaching a valid key there would make the denial path unrunnable
    (a headerless request is impossible, so an expected 401 comes back 200).
    """
    from fastapi.testclient import TestClient

    from app.auth import get_or_create_api_key

    self_managed = {
        "test_dashboard_index_serves_key",
    }

    original_init = TestClient.__init__

    def _init_with_api_key(self, *args, headers=None, **kwargs):
        module = os.environ.get("PYTEST_CURRENT_TEST", "").split("::")[0]
        module = module.rsplit("/", 1)[-1].removesuffix(".py")
        if module in self_managed:
            return original_init(self, *args, **kwargs)
        merged = {"X-Pipeline-Api-Key": get_or_create_api_key()}
        merged.update(headers or {})
        original_init(self, *args, headers=merged, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", _init_with_api_key)
