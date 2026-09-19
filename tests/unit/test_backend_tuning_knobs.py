"""Tests for the backend driver registry and OllamaDriver: OllamaDriver num_ctx/temperature per-dispatch/per-chat plumbing and the per-model tuning table (_LOCAL_MODEL_TUNING).

Split out of test_backend.py to keep it under the project's line-count target; shared fixtures/helpers moved to tests.unit._backend_helpers.
"""
from pathlib import Path

import pytest

from app import backend as b
from app import backend_ollama as bo
from app import ollama_prompt_utils as bo_tuning
from tests.unit._backend_helpers import (  # noqa: F401
    _clear_model_weights_cache,
    _FakePopenResult,
    _FakeResponse,
)

# ---------- OllamaDriver num_ctx/temperature per-dispatch/per-chat plumbing (ENV-KNOBS) ----------
#
# Bug: OllamaDriver.__init__ read PIPELINE_LOCAL_NUM_CTX/PIPELINE_LOCAL_TEMPERATURE
# once at construction, so dispatch() and _chat() always wrote the captured
# values into the child env / request body, silently clobbering per-model
# overrides (e.g. tests/benchmark/models.py's gptoss row) set by the invoking
# harness after the driver already existed. Fix: both re-read the env vars on
# every call, mirroring PIPELINE_LOCAL_MAX_STEPS's existing per-dispatch
# pattern above.

def test_dispatch_rereads_num_ctx_and_temperature_on_each_call(tmp_path, monkeypatch):
    """Positive: construct ONE driver, THEN set PIPELINE_LOCAL_NUM_CTX /
    PIPELINE_LOCAL_TEMPERATURE in the env, call dispatch(), and assert the
    child env reflects the post-construction values — proves they are read
    per-dispatch, not cached at __init__."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    driver = b.OllamaDriver()

    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(303),
    )

    driver.dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "32768"
    assert captured["env"]["PIPELINE_TRANSPORT_TEMPERATURE"] == "1.0"


def test_dispatch_falls_back_to_init_defaults_for_num_ctx_and_temperature(
    tmp_path, monkeypatch,
):
    """Negative/boundary: with the env vars unset, the subprocess sees the
    __init__ defaults (16384 / 0.3)."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(304),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "16384"
    assert captured["env"]["PIPELINE_TRANSPORT_TEMPERATURE"] == "0.3"


def test_dispatch_malformed_num_ctx_raises_value_error_like_max_steps(
    tmp_path, monkeypatch,
):
    """Boundary: an empty-string override must fail the same way
    PIPELINE_LOCAL_MAX_STEPS already does today (a bare ValueError from the
    int() conversion) rather than crashing some other, inconsistent way or
    being silently swallowed into a bogus value."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    driver = b.OllamaDriver()
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "")

    with pytest.raises(ValueError):
        driver.dispatch(
            "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )


def test_dispatch_malformed_temperature_raises_value_error_like_max_steps(
    tmp_path, monkeypatch,
):
    """Same boundary as above, for PIPELINE_LOCAL_TEMPERATURE."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    driver = b.OllamaDriver()
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "")

    with pytest.raises(ValueError):
        driver.dispatch(
            "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
            cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
        )


# ---------------------------------------------------------------------------
# Cloud-model relaxation: a ":cloud"-tagged model (deepseek-v4-flash:cloud,
# glm-5.2:cloud, minimax-m3:cloud) is a frontier model proxied through the
# local Ollama-compatible endpoint, NOT a constrained on-device model. The
# local agent loop's 32K context ceiling, 60-step cap, and weak-model park
# guards (off-task-drift, read-heavy, net-progress) are training wheels for
# ~20B on-device models and actively derail a capable cloud model doing hard
# multi-file work. The dispatch path relaxes ONLY for ":cloud" tags so genuine
# on-device dispatch (gemma4, gpt-oss, devstral, qwen-mlx) is unchanged.
# ---------------------------------------------------------------------------


def test_tuned_num_ctx_cloud_model_uses_cloud_default_not_local(monkeypatch):
    """A :cloud model ignores the on-device PIPELINE_LOCAL_NUM_CTX ceiling and
    gets a cloud-specific default, so its transcript isn't trimmed to 32K."""
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.delenv("PIPELINE_CLOUD_NUM_CTX", raising=False)
    # 131072 cloud default, NOT the 32768 on-device value and NOT the fallback.
    assert bo._tuned_num_ctx("deepseek-v4-flash:cloud", 16384) == 131072


def test_tuned_num_ctx_cloud_model_respects_cloud_env_override(monkeypatch):
    """PIPELINE_CLOUD_NUM_CTX is the cloud-model context knob; an explicit
    value wins over the cloud default."""
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_CLOUD_NUM_CTX", "200000")
    assert bo._tuned_num_ctx("glm-5.2:cloud", 16384) == 200000


def test_tuned_num_ctx_on_device_model_ignores_cloud_knob(monkeypatch):
    """Negative: an on-device model tag never reads PIPELINE_CLOUD_NUM_CTX -
    the cloud knob must not leak into on-device dispatch."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.setenv("PIPELINE_CLOUD_NUM_CTX", "200000")
    assert bo._tuned_num_ctx("gemma4:26b-a4b-it-qat", 16384) == 16384


def test_dispatch_cloud_model_gets_raised_ctx_steps_and_disabled_park(
    tmp_path, monkeypatch,
):
    """A :cloud model dispatches with a raised context ceiling (131072), a
    raised step cap (120), and the weak-model park guards disabled
    (LOCAL_AGENT_PARK_ENABLED=0) - regardless of the on-device knobs, which
    stay set for on-device dispatch."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "60")
    monkeypatch.delenv("PIPELINE_CLOUD_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_CLOUD_MAX_STEPS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_PARK_ENABLED", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(330),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="deepseek-v4-flash:cloud",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_MODEL"] == "deepseek-v4-flash:cloud"
    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "131072"
    assert captured["env"]["PIPELINE_TRANSPORT_MAX_STEPS"] == "120"
    assert captured["env"]["LOCAL_AGENT_PARK_ENABLED"] == "0"


def test_dispatch_cloud_model_respects_explicit_park_enabled(tmp_path, monkeypatch):
    """Boundary: an explicit LOCAL_AGENT_PARK_ENABLED in the environment is
    honored, not clobbered - an operator can re-enable park guards for a
    cloud model if they want the training wheels back."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("LOCAL_AGENT_PARK_ENABLED", "1")
    monkeypatch.delenv("PIPELINE_CLOUD_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_CLOUD_MAX_STEPS", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(331),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="glm-5.2:cloud",
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["LOCAL_AGENT_PARK_ENABLED"] == "1"


def test_dispatch_on_device_model_unchanged_by_cloud_relaxation(tmp_path, monkeypatch):
    """Negative: an on-device model keeps the on-device 32K/60 knobs and gets
    NO LOCAL_AGENT_PARK_ENABLED injection - the cloud relaxation must not
    touch on-device dispatch."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_MAX_STEPS", "60")
    monkeypatch.delenv("PIPELINE_CLOUD_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_CLOUD_MAX_STEPS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_PARK_ENABLED", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(332),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "32768"
    assert captured["env"]["PIPELINE_TRANSPORT_MAX_STEPS"] == "60"
    assert "LOCAL_AGENT_PARK_ENABLED" not in captured["env"]


def test_chat_rereads_num_ctx_and_temperature_on_each_call(monkeypatch):
    """_chat() backs both complete() and the review loop, so it must also
    honor env changes made after construction, not just dispatch()."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    driver = b.OllamaDriver()

    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver.complete("p", model="opus")

    assert captured["options"]["num_ctx"] == 32768
    assert captured["options"]["temperature"] == 1.0


def test_chat_falls_back_to_init_defaults_for_num_ctx_and_temperature(monkeypatch):
    """Negative/boundary: with the env vars unset, _chat() sends the
    __init__ defaults (16384 / 0.3)."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["options"]["num_ctx"] == 16384
    assert captured["options"]["temperature"] == 0.3


# ---------- OllamaDriver per-model tuning table (_LOCAL_MODEL_TUNING) ----------
#
# temperature/num_ctx findings from tests/benchmark A/B experiments are tied
# to a specific model tag (e.g. "gpt-oss:20b"), not a global default. Without
# a per-model table, applying a finding means remembering to flip
# PIPELINE_LOCAL_TEMPERATURE/PIPELINE_LOCAL_NUM_CTX every time the active
# local model tag changes — easy to forget, and silently wrong when
# forgotten. Fix: a module-level table keyed by the RESOLVED model tag,
# consulted after the env var (operator override still wins) and before the
# constructor-captured default.

def test_chat_uses_tuned_table_values_when_present_and_no_env_override(monkeypatch):
    """Positive: a model tag present in the tuning table drives num_ctx/
    temperature for _chat(), with no env var set."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver = b.OllamaDriver()
    driver._chat([{"role": "user", "content": "hi"}], "fake-model:1b")

    assert captured["options"]["num_ctx"] == 8192
    assert captured["options"]["temperature"] == 0.5


def test_dispatch_uses_tuned_table_values_when_present_and_no_env_override(
    tmp_path, monkeypatch,
):
    """Positive: same as above, but through dispatch()'s child-env plumbing.
    model="opus" resolves (via PIPELINE_LOCAL_MODEL_DEFAULT, unset here) to
    the RESOLVED tag "fake-model:1b", which is what the table is keyed on."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(305),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "8192"
    assert captured["env"]["PIPELINE_TRANSPORT_TEMPERATURE"] == "0.5"


def test_chat_env_override_wins_over_tuned_table(monkeypatch):
    """Operator override (PIPELINE_LOCAL_TEMPERATURE/NUM_CTX) must win over a
    table entry for the same model tag."""
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver = b.OllamaDriver()
    driver._chat([{"role": "user", "content": "hi"}], "fake-model:1b")

    assert captured["options"]["num_ctx"] == 32768
    assert captured["options"]["temperature"] == 1.0


def test_dispatch_env_override_wins_over_tuned_table(tmp_path, monkeypatch):
    """Same override precedence as above, through dispatch()."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.setenv("PIPELINE_LOCAL_TEMPERATURE", "1.0")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING",
        {"fake-model:1b": {"temperature": 0.5, "num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(306),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "32768"
    assert captured["env"]["PIPELINE_TRANSPORT_TEMPERATURE"] == "1.0"


def test_gptoss_20b_tuned_to_low_temperature_from_ab_experiment():
    """gpt-oss:20b's entry reflects the 2026-07-03 temperature A/B experiment
    (tests/benchmark/_runs/full_20260703_postfix vs temp_tune_20260703):
    temp=1.0 scored 6/15 success (3 zero-code-landed failures); temp=0.3
    scored 9/15 success (1 zero-code-landed failure) with an unchanged
    ground-truth-pass rate (11/15 both). No num_ctx entry: that value was
    never itself A/B-tested and PIPELINE_LOCAL_NUM_CTX always overrides this
    table anyway (see _tuned_num_ctx) - num_ctx is decided by the deployment
    env, not this tuning table."""
    assert b._LOCAL_MODEL_TUNING["gpt-oss:20b"] == {"temperature": 0.3}


def test_gemma4_26b_qat_tuned_to_medium_think_from_manual_probe():
    """See the table's own comment: 2026-08-14 manual probe, 5 tasks, not a
    full benchmark matrix - "medium" was a reasonable default, not shown
    optimal versus low/high for this specific tag."""
    assert b._LOCAL_MODEL_TUNING["gemma4:26b-a4b-it-qat"] == {"think": "medium"}


def test_chat_falls_back_to_init_defaults_when_model_tag_absent_from_table(
    monkeypatch,
):
    """Regression guard: a model tag with NO entry in the (now non-empty)
    table must still fall back to self.num_ctx/self.temperature exactly as
    before the table existed - only gpt-oss:20b is tuned, "opus" (which
    resolves to the devstral default in this test env) must not be."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_OPUS", raising=False)
    assert b._resolve_local_model("opus") not in b._LOCAL_MODEL_TUNING

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    b.OllamaDriver().complete("p", model="opus")

    assert captured["options"]["num_ctx"] == 16384
    assert captured["options"]["temperature"] == 0.3


def test_dispatch_falls_back_to_init_defaults_when_model_tag_absent_from_table(
    tmp_path, monkeypatch,
):
    """Same regression guard as above, through dispatch()."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MODEL_OPUS", raising=False)
    assert b._resolve_local_model("opus") not in b._LOCAL_MODEL_TUNING

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(307),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "16384"
    assert captured["env"]["PIPELINE_TRANSPORT_TEMPERATURE"] == "0.3"


def test_tuned_think_returns_none_when_absent_from_env_and_table(monkeypatch):
    """No universal fallback for think (unlike temperature/num_ctx) - a
    model/deployment with no opinion should get an unchanged request body,
    so absent-everywhere resolves to None, not some default level."""
    monkeypatch.delenv("PIPELINE_LOCAL_THINK", raising=False)
    monkeypatch.setattr(bo_tuning, "_LOCAL_MODEL_TUNING", {})
    assert bo._tuned_think("fake-model:1b") is None


def test_tuned_think_reads_bool_env_override(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "false")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"think": "medium"}},
    )
    assert bo._tuned_think("fake-model:1b") is False


def test_tuned_think_reads_level_env_override(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "high")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"think": "medium"}},
    )
    assert bo._tuned_think("fake-model:1b") == "high"


def test_tuned_think_invalid_env_value_falls_through_to_table(monkeypatch):
    """A garbage PIPELINE_LOCAL_THINK (not true/false/low/medium/high/max)
    must not silently disable reasoning on a model the caller intended to
    think - fall through to the table instead of coercing to a bogus value."""
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "yes")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"think": "medium"}},
    )
    assert bo._tuned_think("fake-model:1b") == "medium"


def test_tuned_think_reads_table_entry_when_no_env(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_THINK", raising=False)
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"think": "low"}},
    )
    assert bo._tuned_think("fake-model:1b") == "low"


def test_chat_passes_tuned_think_to_provider(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_THINK", raising=False)
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"think": "medium"}},
    )
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )
    b.OllamaDriver()._chat([{"role": "user", "content": "hi"}], "fake-model:1b")
    assert captured["think"] == "medium"


def test_chat_omits_think_when_not_tuned(monkeypatch):
    monkeypatch.delenv("PIPELINE_LOCAL_THINK", raising=False)
    monkeypatch.setattr(bo_tuning, "_LOCAL_MODEL_TUNING", {})
    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )
    b.OllamaDriver()._chat([{"role": "user", "content": "hi"}], "fake-model:1b")
    assert "think" not in captured


def test_dispatch_uses_tuned_table_think_level(tmp_path, monkeypatch):
    """A per-model table `think` level (e.g. gemma4:26b-a4b-it-qat's tuned
    "medium") reaches the dispatch subprocess as LOCAL_AGENT_THINK, same
    plumbing as the pre-existing bool-only PIPELINE_LOCAL_THINK env path."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_THINK", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"think": "medium"}},
    )
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4248),
    )
    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )
    assert captured["env"]["LOCAL_AGENT_THINK"] == "medium"


def test_dispatch_env_level_override_wins_over_tuned_table_think(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", "high")
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"think": "medium"}},
    )
    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(4249),
    )
    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )
    assert captured["env"]["LOCAL_AGENT_THINK"] == "high"


def test_chat_partial_table_entry_only_overrides_the_key_present(monkeypatch):
    """Negative/boundary: a table entry need not set both keys. Only
    temperature is tuned here, so num_ctx must still resolve via the
    existing fallback chain (env, then constructor default)."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"temperature": 0.5}},
    )

    captured = {}
    monkeypatch.setattr(
        b.httpx, "post",
        lambda url, json, timeout: captured.update(json) or _FakeResponse(
            {"message": {"content": "ok"}}
        ),
    )

    driver = b.OllamaDriver()
    driver._chat([{"role": "user", "content": "hi"}], "fake-model:1b")

    assert captured["options"]["num_ctx"] == 16384
    assert captured["options"]["temperature"] == 0.5


def test_dispatch_partial_table_entry_only_overrides_the_key_present(
    tmp_path, monkeypatch,
):
    """Same partial-entry boundary as above, through dispatch(): only
    num_ctx is tuned, so temperature falls back to the constructor default."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", "http://localhost:11434")
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_TEMPERATURE", raising=False)
    monkeypatch.setenv("PIPELINE_LOCAL_MODEL_DEFAULT", "fake-model:1b")
    monkeypatch.setattr(
        bo_tuning, "_LOCAL_MODEL_TUNING", {"fake-model:1b": {"num_ctx": 8192}},
    )

    captured = {}
    monkeypatch.setattr(
        b.subprocess, "Popen",
        lambda argv, cwd, env, stdout, stderr:
            captured.update(env=env) or _FakePopenResult(308),
    )

    b.OllamaDriver().dispatch(
        "do it", system=None, model="opus", allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path, log_path=tmp_path / "agent.log", append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "8192"
    assert captured["env"]["PIPELINE_TRANSPORT_TEMPERATURE"] == "0.3"


# ---------- gpt-oss-20b-high:latest num_ctx entry (per-slot budget) ----------
#
# Behavior: _LOCAL_MODEL_TUNING has an entry for the EXACT tag
# "gpt-oss-20b-high:latest" (a DIFFERENT tag from the existing, untouched
# "gpt-oss:20b" entry) pinning num_ctx=81920. That value is PER SLOT: Ollama
# runs -np OLLAMA_NUM_PARALLEL slots of that size each, so the runner's total
# context is num_ctx x OLLAMA_NUM_PARALLEL and has to fit the host's 100%-GPU
# KV budget (~163840 tokens here). The advance-scheduler launchd plist pins
# neither PIPELINE_LOCAL_NUM_CTX nor OLLAMA_NUM_PARALLEL in a way that
# overrides the table, so this per-model entry governs num_ctx for this tag;
# an explicit PIPELINE_LOCAL_NUM_CTX env var still always overrides the table
# (and is likewise per slot) - see _tuned_num_ctx.


def test_gpt_oss_20b_high_num_ctx_is_sized_per_slot_for_the_parallel_budget():
    """gpt-oss-20b-high:latest's entry reflects the 2026-09-18 measurement on
    this machine (Apple M4, 24GB unified memory). num_ctx here is per slot:
    the runner allocates num_ctx x OLLAMA_NUM_PARALLEL tokens in total, and
    the GPU fits ~163840 tokens total (81920 per slot at
    OLLAMA_NUM_PARALLEL=2, which is what the shipped plist pairs it with).
    Above that ceiling Ollama falls back to CPU_REPACK and the runner goes
    mostly to CPU (262144 total -> 78% GPU; 524288 total -> 68% CPU with a
    129s load). 131072 is gpt-oss's own trained/Ollama-enforced ceiling
    (values up to 1048576 had no further effect), so it is only reachable at
    OLLAMA_NUM_PARALLEL=1 - which is why the table pins 81920, not 131072.
    This is a DIFFERENT tag from "gpt-oss:20b" (whose entry is unchanged)."""
    assert b._LOCAL_MODEL_TUNING["gpt-oss-20b-high:latest"] == {"num_ctx": 81920}


def test_gpt_oss_20b_high_entry_value_is_not_the_host_wide_131072():
    """Negative control: 131072 is host-wide only at OLLAMA_NUM_PARALLEL=1,
    so it must not be what the shipped table pins."""
    entry = b._LOCAL_MODEL_TUNING["gpt-oss-20b-high:latest"]
    assert entry.get("num_ctx") != 131072


def test_gpt_oss_20b_high_entry_comment_documents_the_per_slot_budget():
    """The entry carries a provenance comment in the same style as the
    2026-07-03 / 2026-08-14 entries: date, machine, the per-slot/total
    relation, the measured ceiling and the over-ceiling CPU fallback, and
    the Ollama-side clamp finding - plus the invariant that an operator-set
    PIPELINE_LOCAL_NUM_CTX still overrides the table."""
    lines = Path(bo_tuning.__file__).read_text().splitlines()
    matches = [i for i, ln in enumerate(lines) if '"gpt-oss-20b-high:latest"' in ln]
    assert matches, "no gpt-oss-20b-high:latest entry found in ollama_prompt_utils.py"
    entry_idx = matches[0]
    comment_lines = []
    j = entry_idx - 1
    while j >= 0 and lines[j].lstrip().startswith("#"):
        comment_lines.append(lines[j])
        j -= 1
    comment = "\n".join(reversed(comment_lines))
    assert comment, "no comment block directly above the gpt-oss-20b-high:latest entry"

    assert "2026-09-18" in comment
    assert "Apple M4" in comment
    assert "24GB" in comment.replace(" ", "")
    # num_ctx is PER SLOT, and the total is the product with the slot count.
    assert "per slot" in comment.lower()
    assert "OLLAMA_NUM_PARALLEL" in comment
    # The measured 100%-GPU total-context ceiling, and the value that fits it.
    assert "81920" in comment
    assert "163840" in comment
    assert "100%" in comment
    assert "GPU" in comment
    assert "13GB" in comment.replace(" ", "")
    assert "resident" in comment.lower()
    assert "swap" in comment.lower()
    # Over the ceiling: the CPU fallback, with the measured configurations.
    assert "CPU_REPACK" in comment or "CPU" in comment
    assert "524288" in comment
    # Ollama's own hard clamp for this model, confirmed above 131072.
    assert "clamp" in comment.lower()
    assert "131072" in comment
    assert "1048576" in comment
    # The concurrency cap must be paired with the slot count.
    assert "PIPELINE_MAX_CONCURRENT_AGENTS" in comment
    assert "plist" in comment.lower()


def test_gptoss_20b_entry_unchanged_by_gpt_oss_20b_high_story():
    """Regression guard: the new gpt-oss-20b-high:latest entry must not
    disturb the existing, separately-provenanced "gpt-oss:20b" entry."""
    assert b._LOCAL_MODEL_TUNING["gpt-oss:20b"] == {"temperature": 0.3}


def test_tuned_num_ctx_uses_table_value_for_gpt_oss_20b_high_with_no_env_override(
    monkeypatch,
):
    """With PIPELINE_LOCAL_NUM_CTX unset, the table entry supplies
    num_ctx=81920 (per slot) for gpt-oss-20b-high:latest instead of the
    constructor fallback (16384) - i.e. num_ctx for this tag is decided by
    the table, not by the plist/constructor default."""
    monkeypatch.delenv("PIPELINE_LOCAL_NUM_CTX", raising=False)
    assert bo_tuning._tuned_num_ctx("gpt-oss-20b-high:latest", 16384) == 81920


