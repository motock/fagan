"""Tests for the backend driver registry and OllamaDriver: LocalInferenceProvider selection, PIPELINE_LOCAL_ENDPOINT resolution, and OllamaDriver.dispatch() (transcript persistence/resume, step-cap plumbing).

Split out of test_backend.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._backend_helpers.
"""
import plistlib
from pathlib import Path

import pytest

from app import backend as b
from tests.unit._backend_helpers import (  # noqa: F401
    _clear_model_weights_cache,
    _FakePopenResult,
    _UnimplementedFakeProvider,
)


# ---------- LocalInferenceProvider selection (PIPELINE_LOCAL_PROVIDER) ----------
def test_ollama_driver_defaults_to_ollama_provider(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)
    driver = b.OllamaDriver()
    assert isinstance(driver.provider, b.inference_providers.OllamaProvider)


# ---------- T16: OllamaDriver(provider_name=) explicit pin ----------
def test_ollama_driver_provider_name_overrides_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    driver = b.OllamaDriver(provider_name="mlx")
    assert isinstance(driver.provider, b.inference_providers.MLXProvider)


def test_ollama_driver_no_provider_name_still_reads_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "lmstudio")
    driver = b.OllamaDriver()
    assert isinstance(driver.provider, b.inference_providers.LMStudioProvider)


# ---------- PIPELINE_LOCAL_ENDPOINT provider-scoped resolution ----------
# Regression coverage for a live-discovered bug: PIPELINE_LOCAL_ENDPOINT is a
# single global env var, so a dispatch role pinned to mlx (endpoint :8080)
# and a review role pinned to ollama (endpoint :11434) running in the SAME
# process silently shared one endpoint - review's reachability probe hit
# MLX's port and 404'd, permanently gating review ("Review backend gated...
# 404 Not Found for url 'http://localhost:8080/api/tags'") even though a
# real Ollama server was listening on :11434 the whole time. Found running a
# live production-shaped benchmark trial (dispatch=mlx, review/overlord/
# planner=ollama).
def test_endpoint_defaults_to_providers_own_default_when_nothing_set(monkeypatch):
    """Regression guard for the latent half of the same bug: with NO env var
    set at all, every provider previously defaulted to Ollama's port
    (11434) regardless of which provider was actually selected - only
    masked in practice because callers always set PIPELINE_LOCAL_ENDPOINT
    explicitly. mlx's own default_endpoint is :8080; that must now win."""
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_MLX", raising=False)
    driver = b.OllamaDriver(provider_name="mlx")
    assert driver.endpoint == "http://localhost:8080"


def test_endpoint_global_env_var_still_works_when_provider_scoped_unset(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_MLX", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:9999")
    driver = b.OllamaDriver(provider_name="mlx")
    assert driver.endpoint == "http://localhost:9999"


def test_endpoint_provider_scoped_env_var_wins_over_global(monkeypatch):
    """The actual bug fix: dispatch (mlx) and review (ollama) must resolve
    independent endpoints even when the process-wide PIPELINE_LOCAL_ENDPOINT
    is set for one of them."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:8080")
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", "http://localhost:11434")
    mlx_driver = b.OllamaDriver(provider_name="mlx")
    ollama_driver = b.OllamaDriver(provider_name="ollama")
    assert mlx_driver.endpoint == "http://localhost:8080"
    assert ollama_driver.endpoint == "http://localhost:11434"




def test_ollama_driver_complete_raises_on_unimplemented_provider(monkeypatch):
    # An unimplemented provider must surface NotImplementedError from
    # complete() (via _chat -> provider.chat), not silently fall back to
    # Ollama or hang.
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "provider", _UnimplementedFakeProvider())
    with pytest.raises(NotImplementedError):
        driver.complete("p", model="opus")


def test_ollama_driver_resource_status_reports_not_ok_for_unimplemented_provider(
    monkeypatch,
):
    # resource_status()'s contract is "never raises, always returns an
    # {ok, reason} dict" - an unimplemented provider must report itself as
    # unavailable, not propagate NotImplementedError out of the gate check.
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "provider", _UnimplementedFakeProvider())
    status = driver.resource_status()
    assert status["ok"] is False
    assert "fake stub provider" in status["reason"]


# ---------- OllamaDriver.dispatch() ----------
def test_dispatch_refuses_read_only_allowed_tools():
    """The local agent loop is a writing/coding harness - routing a read-only
    role (e.g. review) here would silently let the agent edit files anyway."""
    with pytest.raises(NotImplementedError, match="read-only"):
        b.OllamaDriver().dispatch(
            "p", model="opus", allowed_tools="Bash,Read",
            cwd=b.Path("."), log_path=b.Path("x.log"), append=False,
        )


def test_dispatch_launches_local_agent_subprocess(tmp_path, monkeypatch):
    captured = {}

    def _fake_popen(argv, cwd, env, stdout, stderr):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env"] = env
        return _FakePopenResult(4242)

    monkeypatch.setattr(b.subprocess, "Popen", _fake_popen)
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    handle = b.OllamaDriver().dispatch(
        "fix the bug", system="be careful", model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert handle.pid == 4242
    # Runs the standalone agent loop with this project's venv python.
    assert captured["argv"][0].endswith(".venv/bin/python3")
    assert captured["argv"][1].endswith("scripts/local_agent.py")
    assert captured["cwd"] == tmp_path
    # Config goes through the environment; system and prompt stay separate.
    assert captured["env"]["LOCAL_AGENT_MODEL"] == "devstral:24b"
    assert captured["env"]["LOCAL_AGENT_SYSTEM"] == "be careful"
    assert captured["env"]["LOCAL_AGENT_TASK"] == "fix the bug"
    assert captured["env"]["LOCAL_AGENT_ENDPOINT"] == "http://localhost:11434"
    assert (tmp_path / "agent.log").exists()


def test_dispatch_passes_local_agent_provider_env_default(tmp_path, monkeypatch):
    """Unset PIPELINE_LOCAL_PROVIDER resolves to "ollama" and dispatch()
    forwards it to the subprocess as LOCAL_AGENT_PROVIDER, so local_agent.py
    can tell which wire protocol to speak without re-deriving it itself."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4243),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_PROVIDER"] == "ollama"


def test_dispatch_passes_local_agent_provider_env_lmstudio(tmp_path, monkeypatch):
    """PIPELINE_LOCAL_PROVIDER=lmstudio must reach the dispatch subprocess as
    LOCAL_AGENT_PROVIDER=lmstudio, not silently stay pinned to ollama."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4244),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:1234")
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "lmstudio")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_PROVIDER"] == "lmstudio"


def test_dispatch_resolves_model_tier_and_passes_runtime_knobs(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env, argv=argv)
        or _FakePopenResult(7),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_SONNET", "qwen2.5-coder:14b")
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "8192")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "12")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="sonnet", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    # Logical tier resolves to a concrete local model, knobs pass through.
    assert captured["env"]["LOCAL_AGENT_MODEL"] == "qwen2.5-coder:14b"
    assert captured["env"]["LOCAL_AGENT_SYSTEM"] == ""
    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "8192"
    assert captured["env"]["PIPELINE_TRANSPORT_MAX_STEPS"] == "12"


def test_dispatch_passes_think_flag_to_subprocess(tmp_path, monkeypatch):
    """PIPELINE_LOCAL_THINK=false must reach the dispatch subprocess as
    LOCAL_AGENT_THINK=false so local_agent.py can pass "think": false to
    Ollama's /api/chat for a Qwen3 hybrid model. Re-read live per dispatch
    (mirroring PIPELINE_LOCAL_MAX_STEPS) so an env edit takes effect without
    restarting the MCP server."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4245),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "false")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_THINK"] == "false"


def test_dispatch_omits_think_flag_when_unset(tmp_path, monkeypatch):
    """Unset PIPELINE_LOCAL_THINK must not inject LOCAL_AGENT_THINK at all —
    non-Qwen3 models (devstral, gpt-oss, qwen3-coder) get an unchanged env and
    local_agent.py omits the `think` key from the request body."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4246),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_THINK", raising=False)

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert "LOCAL_AGENT_THINK" not in captured["env"]


def test_dispatch_omits_think_flag_for_invalid_value(tmp_path, monkeypatch):
    """A garbage PIPELINE_LOCAL_THINK value (not "true"/"false") must not
    inject LOCAL_AGENT_THINK — the backend guard mirrors local_agent.py's
    `if THINK in ("true", "false")` so a typo neither silently disables
    reasoning on a model the caller intended to think nor enables it on one
    they didn't. Only the exact tokens opt in."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr: captured.update(env=env)
        or _FakePopenResult(4247),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "yes")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert "LOCAL_AGENT_THINK" not in captured["env"]


# ---------- Transcript persistence / resume env-var plumbing ----------

def test_dispatch_always_sets_transcript_path_inside_cwd(tmp_path, monkeypatch):
    """Every local dispatch must persist its transcript so a later rework
    can resume it. LOCAL_AGENT_TRANSCRIPT_PATH is always set to a deterministic
    path inside the given cwd (cwd / ".agent_transcript.json") on every
    dispatch call — not just rework ones."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env, cwd=cwd) or _FakePopenResult(4248),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    transcript = captured["env"]["LOCAL_AGENT_TRANSCRIPT_PATH"]
    assert transcript == str(tmp_path / ".agent_transcript.json")


def test_dispatch_passes_resume_transcript_path_when_set(tmp_path, monkeypatch):
    """When resume_transcript_path is given, it must reach the subprocess as
    LOCAL_AGENT_RESUME_TRANSCRIPT_PATH so local_agent.py loads the prior
    transcript instead of cold-starting."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4249),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    resume_path = tmp_path / "prior_transcript.json"

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        resume_transcript_path=resume_path,
    )

    assert captured["env"]["LOCAL_AGENT_RESUME_TRANSCRIPT_PATH"] == str(resume_path)


def test_dispatch_omits_resume_transcript_path_when_unset(tmp_path, monkeypatch):
    """Without resume_transcript_path (a first/non-rework dispatch),
    LOCAL_AGENT_RESUME_TRANSCRIPT_PATH must not appear in the subprocess env
    at all — no behavior change for a cold-start dispatch."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4250),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert "LOCAL_AGENT_RESUME_TRANSCRIPT_PATH" not in captured["env"]


def test_dispatch_passes_resume_append_content_when_set(tmp_path, monkeypatch):
    """When resume_append_content is given alongside resume_transcript_path,
    it must reach the subprocess as LOCAL_AGENT_RESUME_APPEND_CONTENT so
    local_agent.py appends it as a user message after loading the transcript."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4251),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    resume_path = tmp_path / "prior_transcript.json"
    append_content = "The code reviewer REQUESTED CHANGES on your previous attempt. Address this feedback:\nfix the bug"

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        resume_transcript_path=resume_path,
        resume_append_content=append_content,
    )

    assert captured["env"]["LOCAL_AGENT_RESUME_APPEND_CONTENT"] == append_content


def test_dispatch_omits_resume_append_content_when_unset(tmp_path, monkeypatch):
    """Without resume_append_content, LOCAL_AGENT_RESUME_APPEND_CONTENT must
    not appear in the subprocess env — local_agent.py just resumes the
    transcript with no appended user message."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4252),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    resume_path = tmp_path / "prior_transcript.json"

    b.OllamaDriver().dispatch(
        "fix the bug", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        resume_transcript_path=resume_path,
    )

    assert "LOCAL_AGENT_RESUME_APPEND_CONTENT" not in captured["env"]


def test_dispatch_passes_acceptance_to_oracle_harness(tmp_path, monkeypatch):
    """When dispatch() is given an `acceptance` list, it switches to the
    oracle-graded harness variant and passes the paths through env. This is
    the single lever that closed the "model writes buggy self-tests" gap."""
    import json as _json
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(argv=argv, env=env) or _FakePopenResult(50),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        acceptance=["tests/test_x.py", "tests/test_y.py"],
    )

    assert captured["argv"][1].endswith("scripts/local_agent_oracle.py")
    assert captured["env"]["LOCAL_AGENT_MODE"] == "oracle"
    assert _json.loads(captured["env"]["LOCAL_AGENT_ACCEPTANCE"]) == [
        "tests/test_x.py", "tests/test_y.py",
    ]


def test_dispatch_uses_base_harness_when_no_acceptance(tmp_path, monkeypatch):
    """Regression guard: stories without an `acceptance` block stay on the
    base local_agent.py harness — same script, no oracle env, no behavior
    change. Without this, every existing plan would silently switch."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(argv=argv, env=env) or _FakePopenResult(51),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["argv"][1].endswith("scripts/local_agent.py")
    assert "LOCAL_AGENT_ACCEPTANCE" not in captured["env"]
    assert "LOCAL_AGENT_MODE" not in captured["env"]


def test_dispatch_passes_rework_full_suite_env(tmp_path, monkeypatch):
    """L1 (REVIEWER_ESCALATION_PLAN.md): rework_full_suite=True must reach the
    agent subprocess as LOCAL_AGENT_REWORK_FULL_SUITE=1 so the harness raises
    the done-bar to full-suite-green on a CI-fail rework round. Absent by
    default so cold-start dispatches keep the oracle-green bar."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(52),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "fix the test", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        acceptance=["tests/test_x.py"],
        rework_full_suite=True,
    )
    assert captured["env"]["LOCAL_AGENT_REWORK_FULL_SUITE"] == "1"


def test_dispatch_omits_rework_full_suite_env_by_default(tmp_path, monkeypatch):
    """Regression guard: a cold-start dispatch (rework_full_suite unset) must
    NOT set LOCAL_AGENT_REWORK_FULL_SUITE, or the full-suite done-bar would
    silently apply to fresh dispatches and change cold-start behavior."""
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(53),
    )
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        acceptance=["tests/test_x.py"],
    )
    assert "LOCAL_AGENT_REWORK_FULL_SUITE" not in captured["env"]


# ---------- OllamaDriver.dispatch() step-cap plumbing (issue 7f1e9923) ----------
#
# Bug: OllamaDriver.__init__ read PIPELINE_LOCAL_MAX_STEPS once at singleton
# construction, so the scheduler plist could set it forever and nothing would
# change at the dispatch site — overnight launchd runs were stuck on the
# __init__ default. Fix: dispatch() re-reads the env var on every call and
# uses the live value as the override, falling back to __init__'s value only
# when the env var is unset. These three tests pin that contract.

def test_dispatch_rereads_pipeline_local_max_steps_on_each_call(
    tmp_path, monkeypatch,
):
    """Positive: construct ONE OllamaDriver and call dispatch() twice with
    different PIPELINE_LOCAL_MAX_STEPS values. The second call must reflect
    the new env var — proves the value is read per-dispatch, not cached
    at __init__."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_STEPS", raising=False)

    captures = []
    def fake_popen(argv, cwd, env, stdout, stderr):
        captures.append({"env": dict(env)})
        return _FakePopenResult(101 + len(captures))

    monkeypatch.setattr(b.subprocess, "Popen", fake_popen)

    driver = b.OllamaDriver()

    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "12")
    driver.dispatch(
        "first run", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent1.log", append=False,
    )

    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "7")
    driver.dispatch(
        "second run", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent2.log", append=False,
    )

    assert len(captures) == 2
    assert captures[0]["env"]["PIPELINE_TRANSPORT_MAX_STEPS"] == "12"
    assert captures[1]["env"]["PIPELINE_TRANSPORT_MAX_STEPS"] == "7"


def test_dispatch_falls_back_to_init_default_when_env_unset(tmp_path, monkeypatch):
    """Negative/boundary: with PIPELINE_LOCAL_MAX_STEPS unset, the subprocess
    sees PIPELINE_TRANSPORT_MAX_STEPS equal to the __init__ default (40) — proving
    self.max_steps remains the fallback when the env var is absent."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_STEPS", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(202),
    )

    # Defaults: OllamaDriver reads PIPELINE_LOCAL_MAX_STEPS at __init__ — also
    # unset, so it lands on 40, which is what we expect to see in the env.
    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_MAX_STEPS"] == "40"


def test_scheduler_plist_sets_pipeline_local_max_steps():
    """Boundary guard against silent removal: parse the launchd plist with
    plistlib and confirm PIPELINE_LOCAL_MAX_STEPS is present in
    EnvironmentVariables. Without this, the plist could drop the var and
    launchd would fall back to whatever OllamaDriver.__init__ cached — the
    regression that motivated this fix."""
    plist_path = (
        Path(__file__).resolve().parent.parent.parent / "launchd"
        / "com.fagan.pipeline.advance-scheduler.plist"
    )
    with open(plist_path, "rb") as f:
        plist = plistlib.load(f)
    env_vars = plist.get("EnvironmentVariables", {})
    assert "PIPELINE_LOCAL_MAX_STEPS" in env_vars, (
        "scheduler plist must declare PIPELINE_LOCAL_MAX_STEPS so launchd "
        "runs honor the knob (ollama driver re-reads it per dispatch)"
    )
    # Value must be a parseable positive int — we don't pin the exact number
    # so ops can tune it, but we reject typos like "forty".
    int(str(env_vars["PIPELINE_LOCAL_MAX_STEPS"]))


