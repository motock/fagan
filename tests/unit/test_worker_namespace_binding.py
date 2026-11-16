"""Tests for the worker namespace binding (pipeline/worker_scope.py).

The agent loop runs its tool calls in its own process, so the namespace the
dispatcher is bound to has to travel with it as
``PIPELINE_WORKER_NAMESPACE``; without the binding those calls write the
default partition.

Covers:
* ``pipeline.worker_scope.worker_namespace`` binds / unbinds / refuses;
* ``app/backend_ollama.py``'s ``dispatch()`` carries the bound namespace into
  the child env;
* ``scripts/local_agent.py`` and ``scripts/local_agent_oracle.py`` bind the
  namespace around their run.
"""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

from pipeline.actor import bind_namespace, current_namespace
from pipeline.store_records import InvalidRecordKeyError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_script(name: str):
    """Load a scripts/*.py file as a module (the way the agent-loop tests do)."""
    # scripts/local_agent*.py read LOCAL_AGENT_MODEL at import time
    # (os.environ["LOCAL_AGENT_MODEL"]). tests/conftest.py's autouse
    # _isolate_environ fixture strips every LOCAL_AGENT_* variable before each
    # test, so this has to be (re)set per load, not once at collection time.
    os.environ.setdefault("LOCAL_AGENT_MODEL", "test-model")
    spec = importlib.util.spec_from_file_location(
        name, str(REPO_ROOT / "scripts" / f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------- pipeline/worker_scope.py ----------


def test_worker_namespace_binds_when_env_var_is_set(monkeypatch):
    """Positive: with PIPELINE_WORKER_NAMESPACE set, the namespace is bound
    for the body of the context and released again afterwards."""
    from pipeline.worker_scope import worker_namespace

    monkeypatch.setenv("PIPELINE_WORKER_NAMESPACE", "a/b")

    with worker_namespace():
        assert current_namespace() == "a/b"

    assert current_namespace() == "default"


def test_worker_namespace_keeps_default_when_env_var_is_absent(monkeypatch):
    """Negative: an absent variable keeps the default partition, exactly as
    before the binding existed."""
    from pipeline.worker_scope import worker_namespace

    monkeypatch.delenv("PIPELINE_WORKER_NAMESPACE", raising=False)

    with worker_namespace():
        assert current_namespace() == "default"

    assert current_namespace() == "default"


def test_worker_namespace_rejects_invalid_namespace_values(monkeypatch):
    """Negative: a malformed namespace is refused and the body never runs."""
    from pipeline.worker_scope import worker_namespace

    for bad in ("a b", "a/b/c"):
        monkeypatch.setenv("PIPELINE_WORKER_NAMESPACE", bad)
        entered = False
        with pytest.raises(InvalidRecordKeyError), worker_namespace():
            entered = True
        assert entered is False, f"body ran for an invalid namespace {bad!r}"
        assert current_namespace() == "default"


def test_worker_namespace_treats_blank_value_as_unset(monkeypatch):
    """Boundary: an empty or whitespace-only value is unset, not invalid --
    it must not raise and must leave the default partition in place."""
    from pipeline.worker_scope import worker_namespace

    for blank in ("", "   ", "\t"):
        monkeypatch.setenv("PIPELINE_WORKER_NAMESPACE", blank)
        with worker_namespace():
            assert current_namespace() == "default"
        assert current_namespace() == "default"


# ---------- app/backend_ollama.py ----------


def _dispatch_env(tmp_path, monkeypatch, model: str = "opus") -> dict:
    """Run OllamaDriver.dispatch() with the spawn captured, return the child env."""
    from app import backend as b
    from tests.unit._backend_helpers import _FakePopenResult

    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    captured: dict = {}
    monkeypatch.setattr(
        b.subprocess,
        "Popen",
        lambda argv, cwd, env, stdout, stderr:
        captured.update(env=env) or _FakePopenResult(303),
    )

    driver = b.OllamaDriver()
    driver.dispatch(
        "do it",
        system=None,
        model=model,
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        append=False,
    )
    return captured["env"]


def test_dispatch_carries_the_bound_namespace_into_the_child_env(
    tmp_path, monkeypatch
):
    """Positive: the namespace the dispatcher is bound to travels to the agent
    child process as PIPELINE_WORKER_NAMESPACE."""
    with bind_namespace("a/b"):
        env = _dispatch_env(tmp_path, monkeypatch)

    assert env["PIPELINE_WORKER_NAMESPACE"] == "a/b"


def test_dispatch_carries_the_default_namespace_when_none_is_bound(
    tmp_path, monkeypatch
):
    """Boundary: with nothing bound the child sees the default partition, not
    an empty or missing value."""
    assert current_namespace() == "default"
    env = _dispatch_env(tmp_path, monkeypatch)

    assert env["PIPELINE_WORKER_NAMESPACE"] == "default"


def test_backend_ollama_sets_the_worker_namespace_env_var():
    """Structural: dispatch() writes the bound namespace into the child env."""
    source = (REPO_ROOT / "app" / "backend_ollama.py").read_text()
    assert re.search(
        r'env\[[\'"]PIPELINE_WORKER_NAMESPACE[\'"]\]\s*=\s*current_namespace\(\)',
        source,
    ), "dispatch() must set env[\"PIPELINE_WORKER_NAMESPACE\"] = current_namespace()"


# ---------- scripts/local_agent.py and scripts/local_agent_oracle.py ----------


@pytest.mark.parametrize("script_name", ["local_agent", "local_agent_oracle"])
def test_agent_script_main_binds_the_worker_namespace_for_the_run(
    script_name, monkeypatch
):
    """Positive: the agent loop runs with the namespace the dispatcher sent,
    so its tool calls do not land in the default partition."""
    module = _load_script(script_name)

    monkeypatch.setenv("PIPELINE_WORKER_NAMESPACE", "a/b")

    seen: list[str] = []

    def _fake_main_impl() -> int:
        seen.append(current_namespace())
        return 0

    monkeypatch.setattr(module, "_main_impl", _fake_main_impl)
    monkeypatch.setattr(module, "write_done_marker", lambda rc: None)

    rc = module.main()

    assert rc == 0
    assert seen == ["a/b"], f"{script_name} ran under namespace {seen!r}"


@pytest.mark.parametrize("script_name", ["local_agent", "local_agent_oracle"])
def test_agent_script_main_keeps_the_default_namespace_when_unset(
    script_name, monkeypatch
):
    """Negative: without the variable the run stays in the default partition."""
    module = _load_script(script_name)

    monkeypatch.delenv("PIPELINE_WORKER_NAMESPACE", raising=False)

    seen: list[str] = []

    def _fake_main_impl() -> int:
        seen.append(current_namespace())
        return 0

    monkeypatch.setattr(module, "_main_impl", _fake_main_impl)
    monkeypatch.setattr(module, "write_done_marker", lambda rc: None)

    module.main()

    assert seen == ["default"]


@pytest.mark.parametrize("script_name", ["local_agent", "local_agent_oracle"])
def test_agent_script_done_marker_runs_outside_the_namespace_binding(
    script_name, monkeypatch
):
    """The completion marker is written after the binding is released: the
    marker must not be attributed to the worker's namespace."""
    module = _load_script(script_name)

    monkeypatch.setenv("PIPELINE_WORKER_NAMESPACE", "a/b")

    at_marker: list[str] = []

    monkeypatch.setattr(
        module, "_main_impl", lambda: 0
    )
    monkeypatch.setattr(
        module, "write_done_marker", lambda rc: at_marker.append(current_namespace())
    )

    module.main()

    assert at_marker == ["default"]


@pytest.mark.parametrize(
    "script_name", ["local_agent", "local_agent_oracle"]
)
def test_agent_script_imports_and_uses_the_worker_namespace_helper(script_name):
    """Structural: each script imports the helper and wraps its run in it."""
    source = (REPO_ROOT / "scripts" / f"{script_name}.py").read_text()
    assert "from pipeline.worker_scope import worker_namespace" in source
    assert "with worker_namespace():" in source
