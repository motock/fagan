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
# OTEL_EXPORTER_OTLP_* is scrubbed too, so an exported standard OpenTelemetry
# endpoint cannot switch telemetry on under the suite.
_ISOLATED_ENV_PREFIXES = ("PIPELINE_", "LOCAL_AGENT_", "OTEL_EXPORTER_OTLP_")

# Set by tests/conftest.py to a nonexistent path so no test reads a developer's
# real notifications.json; this scrub must not undo it.
_SESSION_ENV_KEPT = "PIPELINE_NOTIFY_CONFIG_PATH"

for _key in list(os.environ):
    if _key.startswith(_ISOLATED_ENV_PREFIXES) and _key != _SESSION_ENV_KEPT:
        del os.environ[_key]


@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(_ISOLATED_ENV_PREFIXES) and key != _SESSION_ENV_KEPT:
            del os.environ[key]
    # Tests that hold a plan lock and assert `skipped: "locked"` would otherwise
    # wait out the 30s default; a test needing a longer wait sets its own.
    os.environ["PIPELINE_COMMAND_LOCK_WAIT_SECONDS"] = "0.2"
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _no_remote_branch_probe(request, monkeypatch):
    """Dispatch tests fake subprocess.run/Popen wholesale, which the fresh-
    dispatch remote-branch guard would trip over. Neutralize it everywhere
    except test_branch_guard.py, which exercises it for real."""
    if request.module.__name__.endswith("test_branch_guard"):
        return
    from pipeline import dispatch

    monkeypatch.setattr(dispatch, "remote_branch_exists", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _isolate_registry_state():
    """Reset the model-registry memo around every test (function-scoped,
    autouse - mirrors _isolate_environ).

    app.role_registry.load_registry() memoizes the parsed model_registry.json
    per file version (stat-keyed) and returns a deep copy of that master, so
    a test that mutates the dict it got back can no longer poison a later
    caller. This fixture is the belt-and-braces guarantee that NO parsed
    registry state survives across tests on a shared pytest-xdist worker:
    before each test the memo is dropped (so the test re-parses whatever its
    environment points at - a leaked PIPELINE_MODEL_REGISTRY_PATH from a
    prior test can still redirect it, but never serve stale contents), and
    after each test it is dropped again (so a test that poisoned the memo
    cannot leak into the next test on the same worker).

    Without this, order/worker-dependent contamination is possible: worker
    runs [test_A (rewrites/redirects the registry), test_B (reads it)] -
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

    Also patches pipeline.scheduler_daemon.PLAN_DIR and
    pipeline.notification_outbox.PLAN_DIR, each its own module-level binding
    imported from pipeline.paths at load time (PLANNOTIFY-06's drain phase
    reads scheduler_daemon's copy directly and drain_outbox reads its own) -
    without this, any pre-existing test calling run_once() drains the
    operator's real ~/.claude/plans outbox files on every run.

    Finally patches pipeline.server.PLAN_DIR itself - the binding every
    server-side read actually goes through. pipeline/server.py imports
    PLAN_DIR from pipeline.paths at module load, and pipeline/concurrency.py,
    pipeline/service.py and pipeline/store.py resolve it at call time through
    their LiveRef/_ServerRef indirection, which delegates straight to the
    live ``pipeline.server`` attribute. Without this the *default* for any
    test that never asked for the ``plan_dir`` fixture was still the
    operator's real plans directory: every ``@mcp.tool()`` call reachable
    from a unit test (list_plans, read_plan, dispatch, _plan_lock, ...)
    globbed/mkdir'd/locked ~/.claude/plans, so a developer's live plans
    leaked into assertions and the suite could block on a live MCP server's
    flock. Patching it here makes "the server sees a throwaway empty plans
    dir" the invariant for the whole unit package; the ``plan_dir`` fixture
    (and per-module ``plan_dir`` fixtures) still override it afterward with
    their own tmp_path, which composes fine (monkeypatch unwinds LIFO).
    """
    from pipeline import notification_outbox, persistence, scheduler_daemon
    from pipeline import paths as pipeline_paths
    from pipeline import server as pserver

    default_plan_dir = tmp_path_factory.mktemp("default_plan_dir")
    # pipeline.paths.PLAN_DIR is the load-time source every other module
    # imported its own copy of; redirecting it keeps "the default plan dir"
    # a single consistent throwaway path for tests that compare an endpoint's
    # reported plan_dir against pipeline.paths.PLAN_DIR (see
    # test_dashboard_health_plan_dir.py) instead of pointing them at two
    # different directories.
    monkeypatch.setattr(pipeline_paths, "PLAN_DIR", default_plan_dir)
    monkeypatch.setattr(persistence, "PLAN_DIR", default_plan_dir)
    if hasattr(scheduler_daemon, "PLAN_DIR"):
        # Pre-migration the daemon held its own module-load copy of PLAN_DIR;
        # once it resolves the host-local root through the host state this
        # binding is gone and the server binding below is what counts.
        monkeypatch.setattr(scheduler_daemon, "PLAN_DIR", default_plan_dir)
    monkeypatch.setattr(notification_outbox, "PLAN_DIR", default_plan_dir)
    monkeypatch.setattr(pserver, "PLAN_DIR", default_plan_dir)


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
        # collect_backend_status (pipeline/usage.py) probes
        # OllamaDriver.resource_status(), which reaches a live HTTP endpoint
        # via self.provider.reachable(self.endpoint). Stub it ONLY for the
        # module that exercises that new path: class-level stubbing here
        # broke the real fail-closed resource_status assertions in
        # test_resource_status_hosted_provider.py and the gate assertions in
        # test_pipeline_mcp_server_fresh_rework.py (verified by A/B run).
        # A test needing the real seam overrides it with its own
        # monkeypatch.setattr afterward, as this fixture's docstring says.
        if module == "test_usage_backend_status":
            monkeypatch.setattr(
                backend.OllamaDriver,
                "resource_status",
                lambda self, model_tag=None: {"ok": True, "reason": ""},
            )
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
    implicitly running with - it does not relax the check itself.

    Caller-supplied headers win, so a test can still pass a wrong key (or
    pop the header off `client.headers`) to exercise the denial path.

    Modules in _SELF_MANAGED_AUTH_MODULES build their own TestClients and
    manage their own auth headers (including deliberately sending none to
    exercise the 401 denial path), so the force-attach is skipped for them
    - attaching a valid key there would make the denial path unrunnable
    (a headerless request is impossible, so an expected 401 comes back 200).
    """
    from fastapi.testclient import TestClient

    from app.auth import get_or_create_api_key

    self_managed = {
        "test_dashboard_index_serves_key",
        "test_chat_internal_loopback_auth",
        # OIDC-03: the bearer branch must be exercised with NO shared secret
        # present, and the shared-secret branch must reject a bearer-only
        # request — a force-attached valid key would turn both denials into
        # 200s.
        "test_dashboard_oidc",
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


STUB_HEAD = "stub-head"


@pytest.fixture(autouse=True)
def _stub_review_binding_head_probe(request, monkeypatch):
    """Stand in for the one git/network probe of the review/merge binding.

    Merge-path fixtures run without a remote, so the real probe finds no tip.
    Only that I/O boundary is faked, as a one-branch remote: the tip is
    ``STUB_HEAD`` until the pipeline pushes (the merge gate re-binds to the
    pushed sha), after which it is that sha - as a real remote's would be. The
    real ``check`` still runs, so a fixture must carry
    ``review_verdict_sha == STUB_HEAD`` to merge. ``test_review_merge_binding``
    opts out and uses the real probe seam.
    """
    if request.module.__name__.endswith("test_review_merge_binding"):
        return
    from pipeline import review_merge_binding

    tip = {"sha": STUB_HEAD}
    real_rebind = review_merge_binding.rebind_after_pipeline_push

    def rebind(story, pushed_sha):
        if isinstance(pushed_sha, str) and pushed_sha:
            tip["sha"] = pushed_sha
        return real_rebind(story, pushed_sha)

    monkeypatch.setattr(review_merge_binding, "branch_head_sha", lambda *a, **k: tip["sha"])
    monkeypatch.setattr(review_merge_binding, "rebind_after_pipeline_push", rebind)
