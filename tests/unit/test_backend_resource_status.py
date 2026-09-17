"""Tests for the backend driver registry and OllamaDriver: resource_status() gates (free-memory floor, cloud-aware, model-too-big-for-host), Claude provider-redirect env isolation, served-vs-requested model auditing, and fail-closed identity preflight.

Split out of test_backend.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._backend_helpers.
"""
import json
from pathlib import Path

import pytest

from app import backend as b
from app import backend_claude as bc
from app import backend_ollama as bo
from tests.unit._backend_helpers import (  # noqa: F401
    _PROVIDER_REDIRECT_ENV_SAMPLE,
    _clear_model_weights_cache,
    _fake_vm_stat_run,
    _FakeCompletedProcess,
    _FakePopenResult,
    _FakeResponse,
    _set_provider_redirect_env,
    _vm_stat_output,
)


# ---------- resource_status() (per-backend gate, Step 5) ----------
def test_ollama_resource_status_ok_when_endpoint_reachable(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    # T13 (2026-07-12): isolate from live host memory state, same as the
    # httpx mock above isolates from live network state - this test asserts
    # reachability only, not the machine's actual free memory at test time.
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    status = driver.resource_status()
    assert status["ok"] is True


def test_ollama_resource_status_not_ok_when_endpoint_unreachable(monkeypatch):
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)
    status = b.OllamaDriver().resource_status()
    assert status["ok"] is False
    assert "unreachable" in status["reason"]


# ---------- T13: free-memory floor gate on resource_status() ----------
def test_ollama_resource_status_not_ok_when_free_memory_below_floor(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 512)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_ollama_resource_status_ok_when_free_memory_above_floor(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")

    status = driver.resource_status()

    assert status["ok"] is True


def test_ollama_resource_status_memory_floor_defaults_to_512mb(monkeypatch):
    """MEMFLOOR-1: with no PIPELINE_LOCAL_MIN_FREE_MEMORY_MB* set, ollama's
    floor is the provider-aware 512mb default (ollama can evict a resident
    model under pressure), NOT the old hardcoded 2048mb that gated every
    provider identically. Boundary: 511mb gates, 512mb clears."""
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_OLLAMA", raising=False)
    driver = b.OllamaDriver(provider_name="ollama")

    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 511)
    assert driver.resource_status()["ok"] is False

    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 512)
    assert driver.resource_status()["ok"] is True


# ---------- Cloud-aware resource_status (model_tag param) ----------
def test_cloud_model_skips_memory_floor(monkeypatch):
    """A :cloud-tagged model is served via Ollama with zero local VRAM
    footprint, so the free-memory floor is irrelevant to it - the gate must
    skip the floor for a :cloud tag (reachability still applies)."""
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 500)  # < 2048 floor
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")

    status = driver.resource_status(model_tag="deepseek-v4-flash:cloud")

    assert status["ok"] is True


def test_local_model_enforces_memory_floor(monkeypatch):
    """An on-device (non-:cloud) tag keeps the free-memory floor exactly as
    today - a :cloud suffix is the ONLY thing that exempts a model."""
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 500)  # < 2048 floor
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")

    status = driver.resource_status(model_tag="gemma4:26b-a4b-it-qat")

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_cloud_model_still_requires_reachable(monkeypatch):
    """A :cloud tag is NOT exempt from reachability - an unreachable server
    short-circuits before the floor and gates regardless of the tag."""
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 500)

    status = driver.resource_status(model_tag="deepseek-v4-flash:cloud")

    assert status["ok"] is False
    assert "unreachable" in status["reason"]


def test_resource_status_default_tag_unchanged(monkeypatch):
    """resource_status() with no model_tag arg must behave identically to
    today: it uses PIPELINE_LOCAL_MODEL_DEFAULT (an on-device tag), so the
    floor applies."""
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "gemma4:26b-a4b-it-qat")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    driver = b.OllamaDriver()

    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 500)
    assert driver.resource_status()["ok"] is False
    assert "insufficient free memory" in driver.resource_status()["reason"]

    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    assert driver.resource_status()["ok"] is True


def test_resource_status_memory_floor_uses_provider_specific_override(monkeypatch):
    # MLX pins one model's full footprint in memory for the server's entire
    # lifetime (no VRAM-swap eviction like Ollama), so free memory settles at
    # a permanently lower steady state once a model is loaded - the generic
    # 2048mb floor (calibrated for Ollama's evictable footprint) never clears
    # again for the life of the server, livelocking dispatch forever. A
    # provider-scoped override lets MLX use a floor suited to its own memory
    # model without weakening Ollama's.
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver(provider_name="mlx")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1024)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", "512")

    status = driver.resource_status()

    assert status["ok"] is True


def test_resource_status_memory_floor_falls_back_to_generic_without_override(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver(provider_name="mlx")
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1024)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_resource_status_memory_floor_override_is_provider_scoped(monkeypatch):
    # An override set for mlx must not loosen ollama's own floor - each
    # provider's override is read by its own provider name, never globally.
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver(provider_name="ollama")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", "128")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1024)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_ollama_resource_status_fails_open_when_memory_read_fails(monkeypatch):
    # A vm_stat parse failure (or a platform without vm_stat) must not block
    # dispatch - "can't determine memory" is not the same as "low memory".
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: None)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "999999")  # would fail if checked

    status = driver.resource_status()

    assert status["ok"] is True


# ---------- model-too-big-for-this-host gate (2026-07-29) ----------
# An earlier attempt at this added the model's weight size to the FREE-memory
# floor (require free >= floor + weights). That paralyzed dispatch outright:
# measured live on a 24576mb host, free was 10837mb against a 15202mb
# requirement, so resource_status returned ok=False forever and
# _role_resource_ok blocked every local dispatch - the same "silently
# paralyze all future dispatch" failure the floor's own docstring warns
# about (T12/T13). Free memory is the wrong denominator: macOS compresses
# and evicts under pressure, so a 13GB model demonstrably loads with under
# 13GB "available".
#
# The real, stable discriminator is the model's size against TOTAL physical
# RAM: gpt-oss:20b (13154mb = 53.5% of 24576mb) is the validated workhorse,
# while devstral:24b (~15GB, ~61%) 500-storms on every request and is
# unusable. So: leave the free-memory floor exactly as it was, and add an
# ORTHOGONAL check on weights-vs-total-RAM. This cannot be tripped by
# transient Chrome pressure, only by a genuinely oversized model.

def test_ollama_model_weights_mb_reads_size_from_tags(monkeypatch):
    tags_payload = {"models": [
        {"name": "gpt-oss:20b", "size": 13793441244},
        {"name": "gemma4:12b-mlx", "size": 7651251181},
    ]}
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse(tags_payload))
    mb = b._ollama_model_weights_mb("http://localhost:11434", "gpt-oss:20b")
    assert mb == 13793441244 // (1024 * 1024)


def test_ollama_model_weights_mb_returns_none_when_tag_absent(monkeypatch):
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({"models": []}))
    assert b._ollama_model_weights_mb("http://localhost:11434", "gpt-oss:20b") is None


def test_ollama_model_weights_mb_fails_open_on_network_error(monkeypatch):
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)
    assert b._ollama_model_weights_mb("http://localhost:11434", "gpt-oss:20b") is None


def test_ollama_model_weights_mb_caches_per_endpoint_and_tag(monkeypatch):
    """resource_status runs on every scheduler tick; a tag's on-disk size is
    effectively immutable, so the /api/tags round trip is cached."""
    calls = {"n": 0}
    tags_payload = {"models": [{"name": "gpt-oss:20b", "size": 13793441244}]}

    def _counting_get(url, timeout):
        calls["n"] += 1
        return _FakeResponse(tags_payload)

    monkeypatch.setattr(b.httpx, "get", _counting_get)
    for _ in range(3):
        assert b._ollama_model_weights_mb("http://localhost:11434", "gpt-oss:20b") == 13154
    assert calls["n"] == 1, f"expected the lookup to be cached, made {calls['n']} calls"


def test_ollama_resource_status_not_ok_when_model_too_large_for_total_ram(monkeypatch):
    """devstral:24b (~15GB) on a 24GB host: ~61% of total RAM, which live
    500-storms on every request (1-3 min per failure) and is unusable."""
    tags_payload = {"models": [{"name": "devstral:24b", "size": 16106127360}]}  # ~15360mb
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse(tags_payload))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "devstral:24b")
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 20000)  # floor is satisfied
    monkeypatch.setattr(bo, "_total_memory_mb", lambda: 24576)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "too large" in status["reason"]
    assert "devstral:24b" in status["reason"]


def test_ollama_resource_status_ok_for_validated_workhorse_model(monkeypatch):
    """gpt-oss:20b (13154mb = 53.5% of a 24576mb host) is the validated
    local workhorse and must never be gated - this is the regression guard
    for the paralysis bug described in this section's header."""
    tags_payload = {"models": [{"name": "gpt-oss:20b", "size": 13793441244}]}  # ~13154mb
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse(tags_payload))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "gpt-oss:20b")
    driver = b.OllamaDriver()
    # The live free-memory reading that the old floor+weights math rejected.
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 10837)
    monkeypatch.setattr(bo, "_total_memory_mb", lambda: 24576)

    status = driver.resource_status()

    assert status["ok"] is True, f"must not gate the validated model: {status['reason']}"


def test_ollama_resource_status_model_size_check_fails_open_when_total_ram_unknown(
    monkeypatch,
):
    tags_payload = {"models": [{"name": "devstral:24b", "size": 16106127360}]}
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse(tags_payload))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "devstral:24b")
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 20000)
    monkeypatch.setattr(bo, "_total_memory_mb", lambda: None)  # non-macOS / sysctl failure

    assert driver.resource_status()["ok"] is True


def test_ollama_resource_status_model_size_check_falls_open_when_tag_unknown(monkeypatch):
    """A tag /api/tags doesn't list (not yet pulled) must not block dispatch
    on an estimate the gate couldn't make."""
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({"models": []}))
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    monkeypatch.setattr(bo, "_total_memory_mb", lambda: 24576)

    assert driver.resource_status()["ok"] is True


def test_ollama_resource_status_cloud_model_zero_size_never_gated(monkeypatch):
    """A cloud-served tag (glm-5.2:cloud) reports size 0 in /api/tags - it has
    no local footprint at all and must never trip the size gate."""
    tags_payload = {"models": [{"name": "glm-5.2:cloud", "size": 0}]}
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse(tags_payload))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "glm-5.2:cloud")
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    monkeypatch.setattr(bo, "_total_memory_mb", lambda: 24576)

    assert driver.resource_status()["ok"] is True


def test_ollama_resource_status_model_size_fraction_is_configurable(monkeypatch):
    tags_payload = {"models": [{"name": "gpt-oss:20b", "size": 13793441244}]}  # 53.5%
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse(tags_payload))
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "gpt-oss:20b")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_MODEL_RAM_FRACTION", "0.4")  # stricter than 53.5%
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 20000)
    monkeypatch.setattr(bo, "_total_memory_mb", lambda: 24576)

    assert driver.resource_status()["ok"] is False


def test_resource_status_model_size_check_skipped_for_non_ollama_provider(monkeypatch):
    """MLX has no /api/tags equivalent (and pins one model for the server's
    whole lifetime - already covered by its provider-scoped floor override),
    so the weights lookup is Ollama-specific and must not run for it."""
    def _boom(url, timeout):
        raise AssertionError("must not query /api/tags for a non-ollama provider")

    monkeypatch.setattr(b.httpx, "get", _boom)
    driver = b.OllamaDriver(provider_name="mlx")
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", "512")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 4096)
    # reachable() also calls httpx.get - bypass it directly to isolate the
    # memory branch under test (reachability is exercised elsewhere).
    monkeypatch.setattr(driver.provider, "reachable", lambda endpoint: (True, ""))

    assert driver.resource_status()["ok"] is True


def test_total_memory_mb_reads_sysctl(monkeypatch):
    class _R:
        returncode = 0
        stdout = "25769803776\n"

    monkeypatch.setattr(b.subprocess, "run", lambda *a, **k: _R())
    assert b._total_memory_mb() == 24576


def test_total_memory_mb_returns_none_on_failure(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no sysctl")

    monkeypatch.setattr(b.subprocess, "run", _boom)
    assert b._total_memory_mb() is None


def test_ollama_resource_status_reachability_failure_takes_priority_over_memory(monkeypatch):
    # When both the endpoint is unreachable AND memory is below floor, the
    # reason must reflect the (actionable) reachability failure - an
    # unreachable server can't dispatch regardless of memory, so the memory
    # check should never even run.
    def _boom(url, timeout):
        raise b.httpx.ConnectError("connection refused")

    monkeypatch.setattr(b.httpx, "get", _boom)
    driver = b.OllamaDriver()

    def _boom_memory():
        raise AssertionError("memory should not be checked when unreachable")
    monkeypatch.setattr(driver, "_free_memory_mb", _boom_memory)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "unreachable" in status["reason"]


# ---------- T12 (2026-07-13 review): _free_memory_mb counts reclaimable pages ----------






def test_free_memory_mb_sums_free_inactive_and_purgeable_pages(monkeypatch):
    # 64 pages each of free/inactive/purgeable at a 16384-byte page size is
    # 1MB per bucket - strict free-only would read 1MB here; the fix must
    # report 3MB, matching how macOS's own memory-pressure tooling treats
    # inactive/purgeable pages as reclaimable, not scarce.
    stdout = _vm_stat_output(free=64, inactive=64, purgeable=64, page_size=16384)
    monkeypatch.setattr(b.subprocess, "run", _fake_vm_stat_run(stdout))

    assert b.OllamaDriver()._free_memory_mb() == 3


def test_free_memory_mb_defaults_missing_inactive_or_purgeable_to_zero(monkeypatch):
    # An unexpected vm_stat output shape missing one of the optional fields
    # should degrade to treating that term as 0, not abort the whole read -
    # free alone is still a valid (if less generous) answer.
    stdout = _vm_stat_output(free=64, page_size=16384, include_inactive=False, include_purgeable=False)
    monkeypatch.setattr(b.subprocess, "run", _fake_vm_stat_run(stdout))

    assert b.OllamaDriver()._free_memory_mb() == 1


def test_free_memory_mb_still_returns_none_when_free_pages_missing(monkeypatch):
    # "Pages free" is the one field that must be present - without it there's
    # no baseline to report, so the read still fails open as before.
    stdout = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages active: 100.\n"
    monkeypatch.setattr(b.subprocess, "run", _fake_vm_stat_run(stdout))

    assert b.OllamaDriver()._free_memory_mb() is None


def test_claude_resource_status_reflects_usage_paused_flag(monkeypatch):
    from app import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": True})
    assert b.ClaudeCliDriver().resource_status()["ok"] is False
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": False})
    assert b.ClaudeCliDriver().resource_status()["ok"] is True


def test_claude_resource_status_fails_open_when_no_usage_state(monkeypatch):
    from app import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", dict)
    assert b.ClaudeCliDriver().resource_status()["ok"] is True


# ---------- T1: Claude backend provider-redirect env isolation ----------
# A `claude` subprocess launched with no `env=` kwarg inherits the calling
# process's full environment. If that shell has a 3rd-party-provider
# redirect exported (ANTHROPIC_BASE_URL et al — a real, documented `claude`
# CLI feature), every review/dispatch/overlord call silently rides it while
# the audit trail still claims "backend": "claude". These vars must be
# stripped before every `claude` subprocess call unless explicitly
# re-enabled via PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV.




def test_complete_strips_provider_redirect_env_vars(monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.delenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", raising=False)
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["env"] = env
        return _FakeCompletedProcess(stdout="ok")

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().complete("hi", model="sonnet")

    env = captured["env"]
    assert env is not None
    for var in _PROVIDER_REDIRECT_ENV_SAMPLE:
        assert var not in env
    assert env["MY_HARMLESS_TEST_VAR"] == "keep-me"
    assert env["PATH"] == "/usr/bin:/bin"


# ---------- bare passthrough: suppress `claude` CLI's own CLAUDE.md auto-discovery ----------


def test_complete_passes_bare_flag_when_requested(monkeypatch):
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(stdout="ok")

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().complete("hi", model="sonnet", bare=True)

    assert "--bare" in captured["cmd"]


def test_complete_omits_bare_flag_by_default(monkeypatch):
    """Regression guard: every existing caller (review, planner, decompose,
    overlord, test_author, security roles) calls complete() without `bare`
    and must see identical behavior to before this parameter existed."""
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(stdout="ok")

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().complete("hi", model="sonnet")

    assert "--bare" not in captured["cmd"]


def test_complete_omits_bare_flag_when_explicitly_false(monkeypatch):
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(stdout="ok")

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().complete("hi", model="sonnet", bare=False)

    assert "--bare" not in captured["cmd"]


def test_dispatch_strips_provider_redirect_env_vars(tmp_path, monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.delenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", raising=False)
    captured = {}

    def _fake_popen(cmd, cwd, env, stdout, stderr):
        captured["env"] = env
        return _FakePopenResult(42)

    monkeypatch.setattr(b.subprocess, "Popen", _fake_popen)

    b.ClaudeCliDriver().dispatch(
        "implement", system=None, model="sonnet", allowed_tools="Bash",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    env = captured["env"]
    assert env is not None
    for var in _PROVIDER_REDIRECT_ENV_SAMPLE:
        assert var not in env
    assert env["MY_HARMLESS_TEST_VAR"] == "keep-me"


def test_usage_probe_text_strips_provider_redirect_env_vars(monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.delenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", raising=False)
    captured = {}

    def _fake_run(cmd, capture_output, text, check, env=None):
        captured["env"] = env
        return _FakeCompletedProcess(stdout=json.dumps({"result": "usage text"}))

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().usage_probe_text()

    env = captured["env"]
    assert env is not None
    for var in _PROVIDER_REDIRECT_ENV_SAMPLE:
        assert var not in env


def test_claude_provider_env_allow_escape_hatch_restores_full_inheritance(monkeypatch):
    _set_provider_redirect_env(monkeypatch)
    monkeypatch.setenv("PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV", "1")
    captured = {}

    def _fake_run(cmd, cwd, capture_output, text, env=None):
        captured["env"] = env
        return _FakeCompletedProcess(stdout="ok")

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    b.ClaudeCliDriver().complete("hi", model="sonnet")

    env = captured["env"]
    for var, value in _PROVIDER_REDIRECT_ENV_SAMPLE.items():
        assert env[var] == value


# ---------- T4: served vs requested model in the audit sidecar ----------
def test_complete_records_served_model_alongside_requested_tier(tmp_path, monkeypatch):
    """The JSON payload's own "model" field is what the CLI actually served -
    distinct from the requested tier string ("sonnet"). Both must land in the
    sidecar so a provider-redirect drift is visible even without T2/T3."""
    payload = {
        "result": "the answer",
        "model": "claude-sonnet-4-5-20260101",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "total_cost_usd": 0.01,
        "duration_ms": 123,
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    b.ClaudeCliDriver().complete(
        "hi", model="sonnet", cell_dir=str(tmp_path),
    )

    lines = (tmp_path / "review_token_costs.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["model"] == "sonnet"
    assert record["served_model"] == "claude-sonnet-4-5-20260101"


def test_record_token_usage_writes_null_served_model_when_absent(tmp_path):
    """Older call sites (or the non-JSON text-output path) don't have a
    served-model value at all - the sidecar write must degrade to null,
    not raise KeyError."""
    b.ClaudeCliDriver().record_token_usage(
        {"input_tokens": 1, "output_tokens": 1, "model": "sonnet"},
        cell_dir=str(tmp_path),
    )

    lines = (tmp_path / "review_token_costs.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["served_model"] is None


# ---------- T2: served model must match the requested tier ----------
def test_complete_raises_provider_identity_mismatch_when_served_model_diverges(
    tmp_path, monkeypatch,
):
    """If the requested tier is "sonnet" but the CLI's own JSON payload
    reports a non-Anthropic model string, a 3rd-party-provider redirect got
    through despite T1 (e.g. PIPELINE_CLAUDE_ALLOW_PROVIDER_ENV set
    intentionally, or a redirect var outside the known set) - this must be a
    loud failure, not a silently wrong review/dispatch."""
    payload = {
        "result": "the answer",
        "model": "mistral-large-2",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    with pytest.raises(b.ProviderIdentityMismatch):
        b.ClaudeCliDriver().complete("hi", model="sonnet", cell_dir=str(tmp_path))


def test_complete_records_usage_even_on_identity_mismatch(tmp_path, monkeypatch):
    """The mismatch must still be visible in the audit sidecar (feeds T4) -
    raising must not skip the record_token_usage() call."""
    payload = {
        "result": "the answer",
        "model": "mistral-large-2",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    with pytest.raises(b.ProviderIdentityMismatch):
        b.ClaudeCliDriver().complete("hi", model="sonnet", cell_dir=str(tmp_path))

    lines = (tmp_path / "review_token_costs.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["served_model"] == "mistral-large-2"


def test_complete_passes_silently_when_served_model_matches_tier_prefix(
    tmp_path, monkeypatch,
):
    payload = {
        "result": "the answer",
        "model": "claude-opus-4-1-20260101",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().complete("hi", model="opus", cell_dir=str(tmp_path))
    assert result == "the answer"


def test_complete_skips_identity_check_when_cell_dir_none(monkeypatch):
    """Callers that don't request structured output (cell_dir=None, e.g. the
    overlord path) never parse the JSON payload at all - no identity check to
    skip, and a non-JSON stdout still passes through unaffected."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="plain text reply, not JSON"
        ),
    )

    result = b.ClaudeCliDriver().complete("hi", model="sonnet")
    assert result == "plain text reply, not JSON"


def test_complete_cell_dir_none_raises_on_api_error_stdout(monkeypatch):
    """Mode 25: on the cell_dir=None path a CLI transport error returned as
    stdout (e.g. 'API Error: Connection closed mid-response...', returncode
    0) must raise, not pass through as if it were valid output - otherwise
    the planner feeds the error string to the executor as its tech-lead
    checklist. _run_planner's fails-open-to-None guard catches the raised
    exception."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="API Error: Connection closed mid-response. Last token:"
        ),
    )
    with pytest.raises(RuntimeError, match="API Error"):
        b.ClaudeCliDriver().complete("hi", model="sonnet")


def test_complete_cell_dir_none_raises_on_nonzero_returncode(monkeypatch):
    """Mode 25: a non-zero returncode on the cell_dir=None path must raise
    even when stdout is empty (the error detail lives in stderr), so a
    failed CLI invocation can never be mistaken for a successful empty
    reply."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="", returncode=1,
        ),
    )
    with pytest.raises(RuntimeError, match="returncode=1"):
        b.ClaudeCliDriver().complete("hi", model="sonnet")


def test_complete_cell_dir_none_passes_clean_text(monkeypatch):
    """Mode 25 regression guard: a normal non-JSON reply with returncode 0
    and no 'API Error:' marker still passes through unchanged - the new
    fail-closed check must not fire on legitimate output."""
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout="the overlord's policy decision text",
        ),
    )
    result = b.ClaudeCliDriver().complete("hi", model="sonnet")
    assert result == "the overlord's policy decision text"


def test_complete_skips_identity_check_for_unrecognized_tier(tmp_path, monkeypatch):
    """A model string outside the known opus/sonnet/haiku tiers has no
    expected prefix to check against - fail open (no crash) rather than
    guessing, matching the documented "only known tiers" scope."""
    payload = {
        "result": "the answer",
        "model": "anything-at-all",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, cwd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().complete(
        "hi", model="some-custom-tier", cell_dir=str(tmp_path),
    )
    assert result == "the answer"


# ---------- T3: fail-closed identity preflight wired into resource_status() ----------
def test_verify_identity_returns_ok_true_for_genuine_anthropic_response(monkeypatch):
    monkeypatch.setattr(bc, "_claude_identity_status", None)
    payload = {"model": "claude-sonnet-4-5-20260101"}
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().verify_identity()

    assert result == {"ok": True, "model": "claude-sonnet-4-5-20260101", "reason": ""}


def test_verify_identity_returns_ok_false_for_non_anthropic_model(monkeypatch):
    monkeypatch.setattr(bc, "_claude_identity_status", None)
    payload = {"model": "mistral-large-2"}
    monkeypatch.setattr(
        b.subprocess, "run",
        lambda cmd, capture_output, text, env=None: _FakeCompletedProcess(
            stdout=json.dumps(payload)
        ),
    )

    result = b.ClaudeCliDriver().verify_identity()

    assert result["ok"] is False
    assert "mistral-large-2" in result["reason"]


def test_verify_identity_caches_result_and_does_not_reprobe(monkeypatch):
    monkeypatch.setattr(bc, "_claude_identity_status", None)
    calls = []

    def _fake_run(cmd, capture_output, text, env=None):
        calls.append(cmd)
        return _FakeCompletedProcess(stdout=json.dumps({"model": "claude-sonnet-4-5"}))

    monkeypatch.setattr(b.subprocess, "run", _fake_run)

    driver = b.ClaudeCliDriver()
    first = driver.verify_identity()
    second = driver.verify_identity()

    assert first == second
    assert len(calls) == 1


def test_resource_status_reflects_failed_identity_check_same_as_usage_pause(monkeypatch):
    from app import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": False})
    monkeypatch.setattr(
        bc, "_claude_identity_status",
        {"ok": False, "model": "mistral-large-2",
         "reason": "Claude backend identity check failed: served 'mistral-large-2', expected claude-sonnet-*"},
    )

    status = b.ClaudeCliDriver().resource_status()

    assert status["ok"] is False
    assert "identity" in status["reason"].lower()


def test_resource_status_ok_when_identity_not_yet_checked(monkeypatch):
    """An empty (never-probed) identity cache must fail OPEN, not block every
    role before any preflight has ever run - matches resource_status()'s
    existing fail-open behavior for missing usage state."""
    from app import pipeline_mcp_server as p
    monkeypatch.setattr(p, "_read_usage_state", lambda: {"paused": False})
    monkeypatch.setattr(bc, "_claude_identity_status", None)

    assert b.ClaudeCliDriver().resource_status()["ok"] is True


# ---------- MEMFLOOR-1: provider-aware unset-env default floor ----------
# The unset-env default used to be one hardcoded 2048mb for every provider,
# which permanently gated ollama/lmstudio on hosts whose steady-state free
# memory sits between 512mb and 2048mb. Providers that can evict a resident
# model under pressure (ollama, lmstudio) now default to 512mb; mlx, which
# pins one model's full footprint for its process lifetime with nothing to
# evict, keeps 2048mb. An explicit PIPELINE_LOCAL_MIN_FREE_MEMORY_MB or
# per-provider override always wins over either default.
def _clear_min_free_memory_env(monkeypatch, provider_name):
    """Unset both the generic and the provider-scoped floor env vars so the
    provider-aware default is what resource_status() actually reads."""
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", raising=False)
    monkeypatch.delenv(
        f"PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_{provider_name.upper()}",
        raising=False,
    )


def _fake_reachable_driver(monkeypatch, provider_name):
    """An OllamaDriver pinned to `provider_name` whose reachability is faked
    (never depends on a live server) and whose httpx.get is stubbed so the
    Ollama-only model-weights probe fails open instead of probing the host."""
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    driver = b.OllamaDriver(provider_name=provider_name)
    monkeypatch.setattr(driver.provider, "reachable", lambda endpoint: (True, ""))
    return driver


def test_ollama_unset_env_default_floor_is_512mb(monkeypatch):
    """1500mb free used to fail under the old hardcoded 2048mb default; with
    the provider-aware default it clears ollama's 512mb floor."""
    _clear_min_free_memory_env(monkeypatch, "ollama")
    driver = _fake_reachable_driver(monkeypatch, "ollama")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1500)

    status = driver.resource_status()

    assert status["ok"] is True


def test_ollama_unset_env_default_floor_gates_below_512mb(monkeypatch):
    """The lower default is still a real floor: 400mb free gates, and the
    reason names the 512mb floor it tripped."""
    _clear_min_free_memory_env(monkeypatch, "ollama")
    driver = _fake_reachable_driver(monkeypatch, "ollama")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 400)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]
    assert "512mb floor" in status["reason"]


def test_mlx_unset_env_default_floor_stays_2048mb(monkeypatch):
    """MLX pins one model for its process lifetime with nothing to evict, so
    its unset-env default must NOT drop to the generic 512mb: 1900mb free
    (the observed steady state) still gates, naming the 2048mb floor."""
    _clear_min_free_memory_env(monkeypatch, "mlx")
    driver = _fake_reachable_driver(monkeypatch, "mlx")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 1900)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]
    assert "2048mb floor" in status["reason"]


def test_mlx_unset_env_default_floor_clears_above_2048mb(monkeypatch):
    """Regression guard on the other side of MLX's unchanged default."""
    _clear_min_free_memory_env(monkeypatch, "mlx")
    driver = _fake_reachable_driver(monkeypatch, "mlx")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 2200)

    status = driver.resource_status()

    assert status["ok"] is True


def test_lmstudio_unset_env_default_floor_is_512mb(monkeypatch):
    """lmstudio JIT-loads and can evict like ollama, so it inherits the same
    512mb generic default: 600mb free clears (it would have failed at 2048)."""
    _clear_min_free_memory_env(monkeypatch, "lmstudio")
    driver = _fake_reachable_driver(monkeypatch, "lmstudio")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 600)

    status = driver.resource_status()

    assert status["ok"] is True


def test_generic_env_override_beats_provider_aware_default(monkeypatch):
    """An explicit PIPELINE_LOCAL_MIN_FREE_MEMORY_MB still overrides the new
    provider-aware default: 1000mb set, 800mb free -> gated at 1000mb."""
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "1000")
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_OLLAMA", raising=False)
    driver = _fake_reachable_driver(monkeypatch, "ollama")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 800)

    status = driver.resource_status()

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]
    assert "1000mb floor" in status["reason"]


def test_provider_env_override_beats_its_own_provider_aware_default(monkeypatch):
    """A provider-scoped env var still overrides that provider's own new
    default: PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX=100 with 150mb free clears
    even though mlx's unset default is 2048mb."""
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_MLX", "100")
    driver = _fake_reachable_driver(monkeypatch, "mlx")
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 150)

    status = driver.resource_status()

    assert status["ok"] is True


def test_provider_aware_default_constants():
    """The two module-level constants the provider-aware default is built
    from: a generic 512mb string default plus a per-provider registry that
    pins only mlx (the non-evicting provider) at 2048mb."""
    assert bo._DEFAULT_MIN_FREE_MEMORY_MB == "512"
    assert bo._PROVIDER_MIN_FREE_MEMORY_MB_DEFAULTS["mlx"] == "2048"
    # ollama/lmstudio can evict a resident model under pressure, so they must
    # NOT pin a higher default - they fall through to the generic 512.
    assert bo._PROVIDER_MIN_FREE_MEMORY_MB_DEFAULTS.get(
        "ollama", bo._DEFAULT_MIN_FREE_MEMORY_MB
    ) == "512"
    assert bo._PROVIDER_MIN_FREE_MEMORY_MB_DEFAULTS.get(
        "lmstudio", bo._DEFAULT_MIN_FREE_MEMORY_MB
    ) == "512"


def _normalized_backend_source():
    """backend_ollama.py with all whitespace runs collapsed to single spaces,
    so docstring assertions survive any line-wrapping the implementer picks.
    Em-dashes are folded to `--` because this file's docstrings use both
    glyphs interchangeably for the same clause break."""
    return " ".join(Path(bo.__file__).read_text().replace("\u2014", "--").split())


def test_resource_status_docstring_documents_provider_aware_default():
    """The floor paragraph must document the new provider-aware default (and
    keep its pre-existing closing sentence verbatim)."""
    src = _normalized_backend_source()
    new_sentence = (
        "The unset-env default is itself provider-aware: 2048mb for mlx "
        "(matching the steady-state measurement above), 512mb generically for "
        "providers that can evict a resident model (ollama, lmstudio) -- an "
        "explicit PIPELINE_LOCAL_MIN_FREE_MEMORY_MB or per-provider override "
        "always wins over either default."
    )
    assert new_sentence in src
    assert (
        "The override does not change the generic floor or any other "
        "provider's default." in src
    )
    # Placement: the new sentence belongs at the END of the floor paragraph,
    # i.e. after the paragraph's opening line and before the next paragraph
    # (the orthogonal model-too-big-for-total-RAM check).
    floor_para_start = src.index("The floor itself is per-provider")
    next_para_start = src.index("For Ollama there is a third, ORTHOGONAL check")
    assert floor_para_start < src.index(new_sentence) < next_para_start


def test_hardcoded_2048_fallback_is_replaced_by_provider_default():
    """The old single hardcoded fallback is gone, replaced by the
    provider-aware lookup."""
    src = _normalized_backend_source()
    assert 'os.environ.get("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")' not in src
    assert "_PROVIDER_MIN_FREE_MEMORY_MB_DEFAULTS.get(" in src


