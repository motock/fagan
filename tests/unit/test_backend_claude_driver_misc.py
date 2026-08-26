"""Tests for the backend driver registry and OllamaDriver: ClaudeCliDriver.dispatch(), Ollama 429 RateLimitedError surfacing, large-diff reviewer guidance, the /api/ps loaded-model probe, the one-time transport-only env-var warning, and complete()'s role= passthrough.

Split out of test_backend.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._backend_helpers.
"""
import json

import pytest

from app import backend as b
from app import backend_ollama as bo
from app import ollama_prompt_utils as bo_tuning
from tests.unit._backend_helpers import (  # noqa: F401
    _REAL_LLAMA_SERVER_LINE,
    _TRANSPORT_VARS,
    _claude_complete_monkeypatch,
    _claude_usage_payload,
    _clean_transport_env,
    _clear_model_weights_cache,
    _fake_ps_run,
    _FakeCompletedProcess,
    _FakePopenResult,
    _FakePsResponse,
    _FakeStatusResponse,
    _ollama_complete_driver,
    _ollama_usage_envelope,
    _reload_backend,
)


# ---------- ClaudeCliDriver.dispatch() ----------
def test_dispatch_streams_claude_cli_output_so_log_size_is_a_reliable_signal(
    tmp_path, monkeypatch,
):
    """check_story_status treats a 0-byte agent.log (after the process exits)
    as a failed launch. claude -p's default text output format only writes
    once, at the very end, so a long-running-but-legitimate agent looks
    identical to a launch that never produced anything. --output-format
    stream-json --verbose makes the CLI emit an event immediately on
    startup, so 0 bytes after exit reliably means it never even started."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: captured.update(cmd=cmd) or _FakePopenResult(99),
    )

    b.ClaudeCliDriver().dispatch(
        "implement the story", system="be careful", model="sonnet",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    cmd = captured["cmd"]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd




def test_dispatch_handle_records_resolved_local_model(tmp_path, monkeypatch):
    """The AgentHandle carries the RESOLVED model (the concrete name the agent
    actually boots with), not the logical tier — so the dashboard can show
    what really ran (e.g. minimax-m3:cloud) instead of the plan's declared
    tier (e.g. 'sonnet'). A story declares model='sonnet'; under the local
    backend with PIPELINE_LOCAL_MODEL_DEFAULT=minimax-m3:cloud the agent boots
    minimax, and the handle must reflect that."""
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: _FakePopenResult(11),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "minimax-m3:cloud")
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_SONNET", raising=False)

    handle = b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )
    assert handle.pid == 11
    assert handle.model == "minimax-m3:cloud"


def test_claude_dispatch_handle_records_passed_model(tmp_path, monkeypatch):
    """The Claude CLI backend uses the model string verbatim (no tier
    resolution), so the handle records exactly what was passed."""
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda cmd, cwd, env, stdout, stderr: _FakePopenResult(12),
    )
    handle = b.ClaudeCliDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Read", cwd=tmp_path, log_path=tmp_path / "agent.log",
        append=False,
    )
    assert handle.pid == 12
    assert handle.model == "opus"


# ---------- Gap 5: Ollama 429 must surface as RateLimitedError, not a generic HTTPError ----------


def test_chat_raises_rate_limited_on_429(monkeypatch):
    """_chat must surface a 429 from the chat endpoint as RateLimitedError
    (a RuntimeError subclass) so the orchestrator can route it to deferral.
    It must NOT be wrapped into a generic HTTPError, which would later be
    converted to a bare RuntimeError("unreachable") and misclassified as an
    inconclusive review by review_story."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: _FakeStatusResponse(429),
    )

    driver = b.OllamaDriver()
    with pytest.raises(b.RateLimitedError, match="429"):
        driver._chat([{"role": "user", "content": "hi"}], model="gpt-oss:20b")
    # And it must NOT be the generic httpx error - the whole point of the
    # short-circuit is to keep RateLimitedError distinct from HTTPError.
    assert not isinstance(
        b.RateLimitedError("x"),
        b.httpx.HTTPError,
    )


def test_complete_propagates_rate_limited_not_generic_runtime_error(monkeypatch):
    """A 429 on a single-shot complete() must propagate as RateLimitedError,
    NOT as the generic RuntimeError('unreachable') the callers wrap
    httpx.HTTPError into. The orchestrator distinguishes the two via
    `except backend.RateLimitedError` BEFORE the generic fallback."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: _FakeStatusResponse(429),
    )

    driver = b.OllamaDriver()
    with pytest.raises(b.RateLimitedError):
        driver.complete("p", model="gpt-oss:20b")
    # Negative assertion: must NOT be a plain RuntimeError with the
    # "unreachable" message that the httpx.HTTPError handler would produce.
    try:
        driver.complete("p", model="gpt-oss:20b")
    except b.RateLimitedError as e:
        assert "unreachable" not in str(e)
    except RuntimeError as e:
        pytest.fail(f"got generic RuntimeError({e!r}); expected RateLimitedError")


def test_review_loop_propagates_rate_limited_through_complete(tmp_path, monkeypatch):
    """_review_loop (via complete() in review mode) must let RateLimitedError
    escape — wrapping it in a generic RuntimeError would defeat the
    orchestrator's `except backend.RateLimitedError` branch and route the
    429 into the inconclusive path instead of deferral."""
    driver = b.OllamaDriver()

    def _boom(*args, **kwargs):
        raise b.RateLimitedError("simulated 429")

    monkeypatch.setattr(driver, "_chat", _boom)

    with pytest.raises(b.RateLimitedError):
        driver.complete(
            "review this", system="s", model="gpt-oss:20b",
            allowed_tools="Bash,Read", cwd=str(tmp_path),
        )


def test_non_429_httpx_error_still_wraps_as_generic_runtime_error(monkeypatch):
    """Guard: only 429 must trigger RateLimitedError. A real backend error
    (500, network drop, etc.) should still surface as the generic
    RuntimeError so existing failure paths in the orchestrator continue to
    handle it the same way."""
    def _boom(url, json, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "post", _boom)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    with pytest.raises(RuntimeError, match="unreachable"):
        b.OllamaDriver().complete("p", model="gpt-oss:20b")


# ---------- Gap 4: large-diff reviewer guidance ----------
def test_review_loop_preamble_mentions_diff_stat():
    """The local-model review preamble must guide the model to use
    `git diff --stat` first and per-file `git diff -- <file>` for large
    diffs, because bash output is truncated to 3000 chars. Drop this
    guidance and a multi-file change can be silently cut off. Assert by
    inspecting the module source directly (the preamble is a multi-line
    string built inside _review_loop)."""
    module_src = open(bo.__file__).read()  # noqa: SIM115 (existing test; not modified per workflow rule against touching tests without approval)
    assert "git diff --stat" in module_src, (
        "reviewer preamble must mention `git diff --stat` so the model "
        "scopes large diffs before reading them"
    )
    assert "git diff -- <file>" in module_src, (
        "reviewer preamble must mention per-file `git diff -- <file>` "
        "as the safe way to read a multi-file change past the 3000-char "
        "bash-output cap"
    )
    # Sanity: confirm _run_readonly_tool actually truncates bash output,
    # so the preamble's advice is grounded in real behavior. _run_readonly_tool
    # itself lives in app/ollama_prompt_utils.py, not this module.
    tool_src = open(bo_tuning.__file__).read()  # noqa: SIM115 (mirrors module_src above)
    assert "(pr.stdout + pr.stderr)[:3000]" in tool_src


def test_claude_reviewer_prompt_mentions_diff_stat():
    """The Claude reviewer's prompt must also tell Claude to scope large
    diffs via `git diff --stat` first — same 3000-char bash cap applies
    on the Claude side, and Claude was shown to be similarly vulnerable
    to silently-truncated diffs on Tier-2 multi-file stories."""
    import importlib
    p = importlib.import_module("app.pipeline_mcp_server")
    server_src = open(p.__file__).read()  # noqa: SIM115 (existing test; not modified per workflow rule against touching tests without approval)
    # The reviewer prompt now lives in pipeline/review.py; check both.
    review = importlib.import_module("pipeline.review")
    review_src = open(review.__file__).read()  # noqa: SIM115 (existing test; not modified per workflow rule against touching tests without approval)
    combined = server_src + review_src
    assert "git diff --stat" in combined
    # And it should explicitly warn about the truncation.
    assert "3000 chars" in combined or "truncat" in combined.lower()


def test_run_readonly_tool_truncates_bash_output_at_3000_chars(tmp_path, monkeypatch):
    """Bash output from the reviewer's read-only tool is truncated to 3000
    chars. The reviewer's preamble explicitly tells the model to avoid
    relying on a single `git diff` for large changes because of this cap;
    this test guards the cap itself so a future 'fix' doesn't silently
    inflate it."""
    import subprocess as _sp
    long_output = "x" * 5000

    class _FakeProc:
        stdout = long_output
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _FakeProc())

    result = b._run_readonly_tool("bash", {"command": "echo x"}, tmp_path)
    assert len(result) <= 3000
    assert result == "x" * 3000  # the cap is a hard cut, not a smart trim


# ---------- Gap 7: /api/ps loaded-model probe ----------


def test_ollama_loaded_models_parses_api_ps(monkeypatch):
    """_ollama_loaded_models must return the set of `name` fields from
    /api/ps so dispatch_story can compare against the about-to-load model."""
    monkeypatch.setattr(
        b.httpx, "get",
        lambda url, timeout: _FakePsResponse([
            {"name": "gpt-oss:20b", "size_vram": 12000000000},
            {"name": "devstral:24b", "size_vram": 14000000000},
        ]),
    )

    loaded = b._ollama_loaded_models("http://localhost:11434")
    assert loaded == {"gpt-oss:20b", "devstral:24b"}


def test_ollama_loaded_models_returns_empty_set_on_http_error(monkeypatch):
    """Observability hook, never a gate: on any failure (endpoint down,
    timeout, malformed JSON), the helper must return an empty set so the
    caller skips the mismatch warning rather than crashing dispatch."""
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)

    assert b._ollama_loaded_models("http://localhost:11434") == set()


def test_ollama_loaded_models_handles_missing_models_field(monkeypatch):
    """Some Ollama versions / proxies return an empty payload; we must not
    crash on that."""
    monkeypatch.setattr(
        b.httpx, "get",
        lambda url, timeout: _FakePsResponse([]),
    )
    assert b._ollama_loaded_models("http://localhost:11434") == set()


# ---------------------------------------------------------------------------
# _ollama_serving_parallelism: detect the -np flag of the llama-server
# runner Ollama spawns per loaded model. Closes the Mode 2 regression where
# an Ollama.app upgrade silently drops OLLAMA_NUM_PARALLEL back to 1: with
# MAX_CONCURRENT_AGENTS>1 against a 1-slot runner, concurrent dispatches
# queue behind each other and hit the 180s read-silence timeout. The value
# is read from the live process table at dispatch time rather than trusted
# to stay in sync with a launchctl env var a human must re-apply after every
# upgrade.
# ---------------------------------------------------------------------------





def test_ollama_serving_parallelism_parses_np_flag(monkeypatch):
    """The -np flag of the running llama-server process is the actual
    serving parallelism Ollama was started with - the value that decides
    whether a second concurrent dispatch queues or runs in parallel."""
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_ps_run(f"  PID COMMAND\n{_REAL_LLAMA_SERVER_LINE}\n"),
    )
    assert b._ollama_serving_parallelism() == 2


def test_ollama_serving_parallelism_returns_none_when_no_runner(monkeypatch):
    """No model loaded yet -> no llama-server process -> can't detect. The
    caller must treat None as 'unknown, don't warn' rather than 0, which
    would false-warn on every first dispatch."""
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_ps_run("  PID COMMAND\n  344 /Applications/Ollama.app/Contents/MacOS/Ollama\n"),
    )
    assert b._ollama_serving_parallelism() is None


def test_ollama_serving_parallelism_takes_min_across_runners(monkeypatch):
    """With multiple models loaded each on its own runner, the binding
    constraint on concurrent dispatch is the SMALLEST -np (a 1-slot runner
    queues any second request to that model). OLLAMA_NUM_PARALLEL is a
    server-wide default so all runners normally share one value; taking the
    min is the conservative bound when they differ (e.g. a Modelfile
    override). Captured live 2026-07-27: the Mode 2 upgrade-drop sets
    every runner to -np 1."""
    line_a = _REAL_LLAMA_SERVER_LINE.replace("-np 2", "-np 2")
    line_b = _REAL_LLAMA_SERVER_LINE.replace("-np 2", "-np 1").replace("59546", "59547")
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_ps_run(f"  PID COMMAND\n{line_a}\n{line_b}\n"),
    )
    assert b._ollama_serving_parallelism() == 1


def test_ollama_serving_parallelism_ignores_unrelated_np_args(monkeypatch):
    """-np is not a unique flag name; only llama-server lines count, so an
    unrelated process carrying an -np token must not pollute the result."""
    unrelated = "  999 some-other-daemon -np 8 --foo bar"
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_ps_run(f"  PID COMMAND\n{unrelated}\n"),
    )
    assert b._ollama_serving_parallelism() is None


def test_ollama_serving_parallelism_returns_none_on_subprocess_error(monkeypatch):
    """Observability hook, never a gate: a ps failure (permissions, missing
    binary on a non-macOS host) must return None, not raise."""
    def _boom(cmd, capture_output=True, text=True, timeout=None):
        raise OSError("command not found")
    monkeypatch.setattr(b.subprocess, "run", _boom)
    assert b._ollama_serving_parallelism() is None


def test_ollama_serving_parallelism_returns_none_on_nonzero_returncode(monkeypatch):
    """A nonzero ps exit must not be trusted as 'no runners' blindly via an
    empty-parse path - return None so the caller can't misread a degraded
    ps as 'parallelism is zero'."""
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_ps_run("ps: illegal argument", returncode=1),
    )
    assert b._ollama_serving_parallelism() is None


def test_ollama_serving_parallelism_handles_runner_without_np_flag(monkeypatch):
    """An older or non-Ollama llama-server build without -np in its args
    must yield None (unknown), not a false 0."""
    line = _REAL_LLAMA_SERVER_LINE.replace(" -np 2", "")
    monkeypatch.setattr(
        b.subprocess, "run",
        _fake_ps_run(f"  PID COMMAND\n{line}\n"),
    )
    assert b._ollama_serving_parallelism() is None
# ---------- One-time transport-only env-var warning ----------
# LOCAL_AGENT_MAX_STEPS / LOCAL_AGENT_NUM_CTX / LOCAL_AGENT_TEMPERATURE are
# transport-only values backend.py overwrites on every dispatch into the
# subprocess env dict; the real operator-facing input knobs are the
# PIPELINE_LOCAL_* vars. If an operator sets a LOCAL_AGENT_* var directly in
# their shell/plist (a natural mistake), their setting is silently clobbered.
# backend.py must emit a ONE-TIME (per process, not per dispatch) warning at
# module-load time when any of the three is already present in os.environ.
#
# Because the check runs at import time and Python caches imports, each test
# reloads backend AFTER setting the env for that case, then reloads again in
# the teardown fixture to restore normal state so nothing leaks into siblings.






def test_transport_warning_fires_for_local_agent_max_steps(monkeypatch, caplog, _clean_transport_env):
    """With LOCAL_AGENT_MAX_STEPS set in os.environ before backend loads, the
    module-load warning must name both the wrong var and the correct one."""
    monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "99")
    with caplog.at_level("WARNING", logger="pipeline"):
        _reload_backend()
    msgs = [r.getMessage() for r in caplog.records if r.name == "pipeline"]
    transport_msgs = [m for m in msgs if "LOCAL_AGENT_MAX_STEPS" in m]
    assert len(transport_msgs) == 1, f"expected exactly one warning for LOCAL_AGENT_MAX_STEPS, got {len(transport_msgs)}"
    assert any("LOCAL_AGENT_MAX_STEPS" in m and "PIPELINE_LOCAL_MAX_STEPS" in m for m in msgs), (
        f"expected a warning naming LOCAL_AGENT_MAX_STEPS and PIPELINE_LOCAL_MAX_STEPS, got {msgs}"
    )


def test_transport_warning_fires_for_local_agent_num_ctx(monkeypatch, caplog, _clean_transport_env):
    """With LOCAL_AGENT_NUM_CTX set in os.environ before backend loads, the
    module-load warning must name both the wrong var and the correct one."""
    monkeypatch.setenv("LOCAL_AGENT_NUM_CTX", "8192")
    with caplog.at_level("WARNING", logger="pipeline"):
        _reload_backend()
    msgs = [r.getMessage() for r in caplog.records if r.name == "pipeline"]
    assert any("LOCAL_AGENT_NUM_CTX" in m and "PIPELINE_LOCAL_NUM_CTX" in m for m in msgs), (
        f"expected a warning naming LOCAL_AGENT_NUM_CTX and PIPELINE_LOCAL_NUM_CTX, got {msgs}"
    )


def test_transport_warning_fires_for_local_agent_temperature(monkeypatch, caplog, _clean_transport_env):
    """With LOCAL_AGENT_TEMPERATURE set in os.environ before backend loads,
    the module-load warning must name both the wrong var and the correct one."""
    monkeypatch.setenv("LOCAL_AGENT_TEMPERATURE", "0.7")
    with caplog.at_level("WARNING", logger="pipeline"):
        _reload_backend()
    msgs = [r.getMessage() for r in caplog.records if r.name == "pipeline"]
    assert any("LOCAL_AGENT_TEMPERATURE" in m and "PIPELINE_LOCAL_TEMPERATURE" in m for m in msgs), (
        f"expected a warning naming LOCAL_AGENT_TEMPERATURE and PIPELINE_LOCAL_TEMPERATURE, got {msgs}"
    )


def test_transport_warning_silent_when_none_set(monkeypatch, caplog, _clean_transport_env):
    """Negative test: with NONE of the three transport vars set, reloading
    backend must NOT emit any transport-only warning."""
    for wrong, _correct in _TRANSPORT_VARS:
        monkeypatch.delenv(wrong, raising=False)
    with caplog.at_level("WARNING", logger="pipeline"):
        _reload_backend()
    msgs = [r.getMessage() for r in caplog.records if r.name == "pipeline"]
    transport_msgs = [
        m for m in msgs
        if any(wrong in m for wrong, _c in _TRANSPORT_VARS)
    ]
    assert transport_msgs == [], (
        f"expected no transport-only warning when none are set, got {transport_msgs}"
    )


def test_transport_warning_fires_for_all_three_independently(monkeypatch, caplog, _clean_transport_env):
    """With all three transport vars set simultaneously, all three distinct
    warnings must fire — proving they're checked independently, not
    short-circuited after the first."""
    monkeypatch.setenv("LOCAL_AGENT_MAX_STEPS", "99")
    monkeypatch.setenv("LOCAL_AGENT_NUM_CTX", "8192")
    monkeypatch.setenv("LOCAL_AGENT_TEMPERATURE", "0.7")
    with caplog.at_level("WARNING", logger="pipeline"):
        _reload_backend()
    msgs = [r.getMessage() for r in caplog.records if r.name == "pipeline"]
    assert any("LOCAL_AGENT_MAX_STEPS" in m and "PIPELINE_LOCAL_MAX_STEPS" in m for m in msgs), msgs
    assert any("LOCAL_AGENT_NUM_CTX" in m and "PIPELINE_LOCAL_NUM_CTX" in m for m in msgs), msgs
    assert any("LOCAL_AGENT_TEMPERATURE" in m and "PIPELINE_LOCAL_TEMPERATURE" in m for m in msgs), msgs


# ---------- complete() role= passthrough into review_token_costs.jsonl ----
# Step 0 of TOKEN_CONTEXT_OPTIMIZATION_PLAN: complete() on the Backend
# Protocol, ClaudeCliDriver, and OllamaDriver must accept an optional
# role: str = "complete" kwarg and thread it into the internal
# record_token_usage(...) call so a complete()-originated sidecar record
# is labeled with the caller's role (planner/reviewer/...) instead of
# being indistinguishably hardcoded to "complete". Default "complete"
# preserves byte-for-byte behavior for every existing caller.





def test_claude_complete_role_kwarg_threads_into_sidecar(tmp_path, monkeypatch):
    """ClaudeCliDriver.complete(..., role='planner') must write a
    review_token_costs.jsonl record whose 'role' field is 'planner', not
    the hardcoded 'complete'."""
    _claude_complete_monkeypatch(monkeypatch, _claude_usage_payload())

    b.ClaudeCliDriver().complete(
        "hi", model="sonnet", cell_dir=str(tmp_path), role="planner",
    )

    record = json.loads(
        (tmp_path / "review_token_costs.jsonl").read_text().splitlines()[0]
    )
    assert record["role"] == "planner"


def test_claude_complete_default_role_is_complete(tmp_path, monkeypatch):
    """Regression guard: when role= is not passed, the sidecar record must
    still read 'role': 'complete' (the documented default). This must pass
    both before and after the signature-widening change."""
    _claude_complete_monkeypatch(monkeypatch, _claude_usage_payload())

    b.ClaudeCliDriver().complete(
        "hi", model="sonnet", cell_dir=str(tmp_path),
    )

    record = json.loads(
        (tmp_path / "review_token_costs.jsonl").read_text().splitlines()[0]
    )
    assert record["role"] == "complete"


def test_claude_complete_role_kwarg_is_keyword_only(tmp_path, monkeypatch):
    """role must be a keyword-only parameter (it sits after the existing
    keyword-only *, system/model/... block). Passing it positionally must
    raise TypeError, proving it was added to the keyword-only block and
    not accidentally as a positional param."""
    _claude_complete_monkeypatch(monkeypatch, _claude_usage_payload())

    with pytest.raises(TypeError):
        # positional role must be rejected
        b.ClaudeCliDriver().complete(
            "hi", "be careful", "sonnet", None, None, None, str(tmp_path), "planner",
        )






def test_ollama_complete_role_kwarg_threads_into_sidecar(tmp_path, monkeypatch):
    """OllamaDriver.complete(..., role='reviewer') must write a
    review_token_costs.jsonl record whose 'role' field is 'reviewer', not
    the hardcoded 'complete'."""
    driver = _ollama_complete_driver(monkeypatch, _ollama_usage_envelope())

    driver.complete(
        "do the thing", model="opus", cell_dir=str(tmp_path), role="reviewer",
    )

    record = json.loads(
        (tmp_path / "review_token_costs.jsonl").read_text().splitlines()[0]
    )
    assert record["role"] == "reviewer"


def test_ollama_complete_default_role_is_complete(tmp_path, monkeypatch):
    """Regression guard: when role= is not passed, the sidecar record must
    still read 'role': 'complete'. Must pass both before and after the
    signature-widening change."""
    driver = _ollama_complete_driver(monkeypatch, _ollama_usage_envelope())

    driver.complete(
        "do the thing", model="opus", cell_dir=str(tmp_path),
    )

    record = json.loads(
        (tmp_path / "review_token_costs.jsonl").read_text().splitlines()[0]
    )
    assert record["role"] == "complete"


def test_ollama_complete_role_kwarg_is_keyword_only(tmp_path, monkeypatch):
    """role must be keyword-only on OllamaDriver.complete() too."""
    driver = _ollama_complete_driver(monkeypatch, _ollama_usage_envelope())

    with pytest.raises(TypeError):
        # positional role must be rejected
        driver.complete(
            "do the thing", None, "opus", None, None, None, str(tmp_path), "reviewer",
        )


def test_backend_protocol_complete_declares_role_kwarg():
    """The Backend Protocol's complete() declaration must also carry the
    role: str = 'complete' parameter so every driver shares one widened
    contract. Inspect the signature directly (the Protocol body is just
    `...`, so we assert on the parameter list, not behavior)."""
    import inspect

    sig = inspect.signature(b.Backend.complete)
    assert "role" in sig.parameters, (
        "Backend.complete must declare a 'role' parameter"
    )
    param = sig.parameters["role"]
    assert param.default == "complete", (
        f"Backend.complete role default must be 'complete', got {param.default!r}"
    )
    assert param.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ), f"Backend.complete role must be keyword-only, got {param.kind}"


def test_claude_complete_signature_declares_role_kwarg_with_default():
    """ClaudeCliDriver.complete()'s signature must declare role: str =
    'complete' (default preserved)."""
    import inspect

    sig = inspect.signature(b.ClaudeCliDriver.complete)
    assert "role" in sig.parameters
    param = sig.parameters["role"]
    assert param.default == "complete"
    assert param.kind == inspect.Parameter.KEYWORD_ONLY


def test_ollama_complete_signature_declares_role_kwarg_with_default():
    """OllamaDriver.complete()'s signature must declare role: str =
    'complete' (default preserved)."""
    import inspect

    sig = inspect.signature(b.OllamaDriver.complete)
    assert "role" in sig.parameters
    param = sig.parameters["role"]
    assert param.default == "complete"
    assert param.kind == inspect.Parameter.KEYWORD_ONLY


def test_claude_complete_role_empty_string_threads_through(tmp_path, monkeypatch):
    """Boundary: an empty-string role is a valid str and must be threaded
    through verbatim (the driver does not validate role semantics). This
    guards against an implementation that special-cases falsy roles."""
    _claude_complete_monkeypatch(monkeypatch, _claude_usage_payload())

    b.ClaudeCliDriver().complete(
        "hi", model="sonnet", cell_dir=str(tmp_path), role="",
    )

    record = json.loads(
        (tmp_path / "review_token_costs.jsonl").read_text().splitlines()[0]
    )
    assert record["role"] == ""


def test_ollama_complete_role_empty_string_threads_through(tmp_path, monkeypatch):
    """Boundary: an empty-string role must thread through verbatim on
    OllamaDriver too."""
    driver = _ollama_complete_driver(monkeypatch, _ollama_usage_envelope())

    driver.complete(
        "do the thing", model="opus", cell_dir=str(tmp_path), role="",
    )

    record = json.loads(
        (tmp_path / "review_token_costs.jsonl").read_text().splitlines()[0]
    )
    assert record["role"] == ""


def test_claude_complete_role_none_not_accepted(tmp_path, monkeypatch):
    """Negative: role is typed str, so passing role=None must be rejected
    by the type contract at call time is not enforceable at runtime here,
    but the parameter annotation must be str (not Optional[str]) so a
    future type-checker catches None callers."""
    import inspect

    # eval_str=True resolves the string form `from __future__ import
    # annotations` produces back to the real type object, so this assertion
    # holds regardless of that module-level import's presence.
    sig = inspect.signature(b.ClaudeCliDriver.complete, eval_str=True)
    param = sig.parameters["role"]
    assert param.annotation is str, (
        f"role annotation must be str (not Optional), got {param.annotation!r}"
    )


def test_ollama_complete_role_annotation_is_str():
    """Negative: OllamaDriver.complete() role annotation must be str."""
    import inspect

    sig = inspect.signature(b.OllamaDriver.complete, eval_str=True)
    param = sig.parameters["role"]
    assert param.annotation is str, (
        f"role annotation must be str (not Optional), got {param.annotation!r}"
    )


def test_backend_protocol_role_annotation_is_str():
    """Negative: Backend.complete role annotation must be str."""
    import inspect

    sig = inspect.signature(b.Backend.complete, eval_str=True)
    param = sig.parameters["role"]
    assert param.annotation is str, (
        f"role annotation must be str (not Optional), got {param.annotation!r}"
    )
