"""Tests for the mlx_lm.server lifecycle supervisor (scripts/mlx_server_supervisor.py).

External boundaries (httpx reachability probe, subprocess.Popen launch) are
mocked - these cover the pure decision logic.
"""
import importlib.util
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "mlx_server_supervisor", str(Path(__file__).parent.parent.parent / "scripts" / "mlx_server_supervisor.py")
)
sup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sup)


def test_is_reachable_true_on_200(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(sup.httpx, "get", lambda url, timeout: _Resp())
    assert sup.is_reachable("http://localhost:8080") is True


def test_is_reachable_false_on_http_error(monkeypatch):
    def _raise(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(sup.httpx, "get", _raise)
    assert sup.is_reachable("http://localhost:8080") is False


def test_ensure_running_skips_start_when_already_reachable(monkeypatch):
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: True)
    monkeypatch.setattr(sup, "is_serving", lambda endpoint, timeout=30.0: True)
    started = []
    monkeypatch.setattr(sup, "start_server", lambda model_path, port: started.append((model_path, port)))

    result = sup.ensure_running("http://localhost:8080", "/path/to/model", "8080")

    assert result == "already_running"
    assert started == []


def test_ensure_running_starts_server_when_not_reachable(monkeypatch):
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: False)
    started = []
    monkeypatch.setattr(sup, "start_server", lambda model_path, port: started.append((model_path, port)))

    result = sup.ensure_running("http://localhost:8080", "/path/to/model", "8080")

    assert result == "started"
    assert started == [("/path/to/model", "8080")]


def test_ensure_running_does_not_check_serving_when_not_reachable(monkeypatch):
    """No point probing completions on a server that isn't even up - go
    straight to starting one."""
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: False)
    probed = []
    monkeypatch.setattr(sup, "is_serving", lambda endpoint, timeout=30.0: probed.append(1) or True)
    monkeypatch.setattr(sup, "start_server", lambda model_path, port: None)

    sup.ensure_running("http://localhost:8080", "/path/to/model", "8080")

    assert probed == []


def test_ensure_running_restarts_when_reachable_but_wedged(monkeypatch):
    """mlx_lm.server can be reachable (answers /v1/models instantly) while
    every /v1/chat/completions call hangs forever - observed live 2026-07-14
    after a client disconnected mid-generation. Nothing about plain
    reachability catches this, so a wedged server would otherwise sit
    "already_running" forever with no real dispatch ever completing again."""
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: True)
    monkeypatch.setattr(sup, "is_serving", lambda endpoint, timeout=30.0: False)
    killed = []
    monkeypatch.setattr(sup, "_kill_listening_process", lambda port: killed.append(port))
    started = []
    monkeypatch.setattr(sup, "start_server", lambda model_path, port: started.append((model_path, port)))

    result = sup.ensure_running("http://localhost:8080", "/path/to/model", "8080")

    assert result == "restarted_wedged"
    assert killed == ["8080"]
    assert started == [("/path/to/model", "8080")]


def test_ensure_running_raises_without_model_path(monkeypatch):
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: False)

    with pytest.raises(ValueError, match="MLX_SERVER_MODEL_PATH"):
        sup.ensure_running("http://localhost:8080", None, "8080")


def test_ensure_running_does_not_probe_reachability_before_checking_model_path(monkeypatch):
    """A misconfigured supervisor should fail fast on the missing model path,
    not waste a network round-trip first."""
    probed = []
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: probed.append(1) or True)

    with pytest.raises(ValueError):
        sup.ensure_running("http://localhost:8080", "", "8080")

    assert probed == []


def test_ensure_running_raises_without_python_interpreter(monkeypatch):
    """MLX_SERVER_PYTHON must be explicit, same as MLX_SERVER_MODEL_PATH - a
    silent 'python3' default would resolve to whatever interpreter happens to
    be first on PATH, which on this host has no mlx-lm installed and fails
    with output swallowed by start_server's DEVNULL redirect (the exact
    failure mode that made a real crash hard to diagnose)."""
    monkeypatch.setattr(sup, "PYTHON", None)
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: False)

    with pytest.raises(ValueError, match="MLX_SERVER_PYTHON"):
        sup.ensure_running("http://localhost:8080", "/path/to/model", "8080")


def test_ensure_running_does_not_probe_reachability_before_checking_python(monkeypatch):
    """Same fail-fast contract as the model-path check."""
    monkeypatch.setattr(sup, "PYTHON", None)
    probed = []
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: probed.append(1) or True)

    with pytest.raises(ValueError):
        sup.ensure_running("http://localhost:8080", "/path/to/model", "8080")

    assert probed == []


def test_is_serving_true_on_successful_completion(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(sup.httpx, "post", lambda url, json, timeout: _Resp())
    assert sup.is_serving("http://localhost:8080") is True


def test_is_serving_false_on_timeout(monkeypatch):
    def _raise(url, json, timeout):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(sup.httpx, "post", _raise)
    assert sup.is_serving("http://localhost:8080") is False


def test_is_serving_false_on_http_error(monkeypatch):
    def _raise(url, json, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(sup.httpx, "post", _raise)
    assert sup.is_serving("http://localhost:8080") is False


def test_is_serving_sends_minimal_completion_request(monkeypatch):
    """The probe must be cheap (tiny max_tokens) - it runs on every
    supervisor tick, not just once."""
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

    def _fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(sup.httpx, "post", _fake_post)
    sup.is_serving("http://localhost:8080")

    assert captured["url"] == "http://localhost:8080/v1/chat/completions"
    assert captured["json"]["max_tokens"] == 1
    assert captured["json"]["stream"] is False




def test_start_server_launches_wrapper_with_model_and_port(monkeypatch, tmp_path):
    """start_server launches scripts/mlx_server_wrapper.py (not `-m mlx_lm
    server` directly) so the wrapper's memory-limit + instrumentation hooks
    are always in effect - see mlx_server_wrapper.py's module docstring for
    why (2026-07-14 IOGPUGroupMemory panics)."""
    captured = {}

    class _FakeProc:
        pid = 999

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(sup.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "LOG_PATH", str(tmp_path / "mlx-server.log"))
    monkeypatch.setattr(sup, "PROMPT_CONCURRENCY", "1")
    monkeypatch.setattr(sup, "PROMPT_CACHE_SIZE", "1")
    monkeypatch.setattr(sup, "PROMPT_CACHE_BYTES", "4G")

    proc = sup.start_server("/path/to/model", "8080")

    assert captured["cmd"] == [
        "python3", str(sup.WRAPPER_PATH), "--model", "/path/to/model", "--port", "8080",
        "--prompt-concurrency", "1",
        "--prompt-cache-size", "1", "--prompt-cache-bytes", "4G",
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert proc.pid == 999


def test_start_server_uses_configured_prompt_cache_bounds(monkeypatch, tmp_path):
    """MLX_PROMPT_CACHE_SIZE/MLX_PROMPT_CACHE_BYTES must reach the launched
    server unbounded by the module's defaults - an operator raising or
    lowering the bound needs it to actually take effect."""
    captured = {}

    class _FakeProc:
        pid = 999

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(sup.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "LOG_PATH", str(tmp_path / "mlx-server.log"))
    monkeypatch.setattr(sup, "PROMPT_CACHE_SIZE", "5")
    monkeypatch.setattr(sup, "PROMPT_CACHE_BYTES", "8G")

    sup.start_server("/path/to/model", "8080")

    assert captured["cmd"][-4:] == [
        "--prompt-cache-size", "5", "--prompt-cache-bytes", "8G",
    ]


def test_start_server_uses_configured_prompt_concurrency(monkeypatch, tmp_path):
    """MLX_PROMPT_CONCURRENCY must reach the launched server - mlx_lm.server's
    own default (--prompt-concurrency 8) batches multiple prompts through
    concurrent GPU graph evaluation, which MLX documents as not thread-safe
    (ml-explore/mlx#2133) and which mlx-lm's own issue tracker has already
    tied to crashes under concurrency (ml-explore/mlx-lm#754, #965). Forcing
    it to 1 serializes GPU access as a stopgap pending an upstream fix."""
    captured = {}

    class _FakeProc:
        pid = 999

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(sup.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "LOG_PATH", str(tmp_path / "mlx-server.log"))
    monkeypatch.setattr(sup, "PROMPT_CONCURRENCY", "1")
    monkeypatch.setattr(sup, "PROMPT_CACHE_SIZE", "1")
    monkeypatch.setattr(sup, "PROMPT_CACHE_BYTES", "4G")

    sup.start_server("/path/to/model", "8080")

    assert "--prompt-concurrency" in captured["cmd"]
    idx = captured["cmd"].index("--prompt-concurrency")
    assert captured["cmd"][idx + 1] == "1"


def test_start_server_does_not_discard_output(monkeypatch, tmp_path):
    """A wrong interpreter, a bad model path, or an mlx-lm import error must
    land somewhere readable - DEVNULL made a real failed launch indistinguishable
    from a slow cold-load, with nothing to grep to find out which."""
    captured = {}

    class _FakeProc:
        pid = 999

    def _fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(sup.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "LOG_PATH", str(tmp_path / "mlx-server.log"))

    sup.start_server("/path/to/model", "8080")

    assert captured["kwargs"]["stdout"] != sup.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] != sup.subprocess.DEVNULL


def test_main_prints_error_and_returns_1_when_model_path_unset(monkeypatch, capsys):
    monkeypatch.setattr(sup, "MODEL_PATH", None)
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: False)

    exit_code = sup.main()

    assert exit_code == 1
    assert "MLX_SERVER_MODEL_PATH" in capsys.readouterr().err


def test_main_prints_already_running_and_returns_0(monkeypatch, capsys):
    monkeypatch.setattr(sup, "MODEL_PATH", "/path/to/model")
    monkeypatch.setattr(sup, "PYTHON", "python3")
    monkeypatch.setattr(sup, "is_reachable", lambda endpoint, timeout=5.0: True)
    monkeypatch.setattr(sup, "is_serving", lambda endpoint, timeout=30.0: True)

    exit_code = sup.main()

    assert exit_code == 0
    assert "already_running" in capsys.readouterr().out
