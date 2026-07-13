"""Tests for tests/benchmark/models.py per-model temperature/num_ctx overrides."""
from models import MODELS, _local, _LOCAL_AGENT_ENV


def test_devstral_retains_tuned_temperature_and_num_ctx():
    env = MODELS["devstral"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == "0.3"
    assert env["PIPELINE_LOCAL_NUM_CTX"] == "16384"


def test_minimax_entry_unchanged():
    env = MODELS["minimax"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_LOCAL_ENDPOINT"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_ENDPOINT"]
    assert (
        env["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"]
        == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"]
    )
    assert env["PIPELINE_LOCAL_MAX_STEPS"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_MAX_STEPS"]


def test_local_with_overrides_replaces_temperature_and_num_ctx():
    result = _local("some:tag", temperature=1.0, num_ctx=32768)
    assert result["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "1.0"
    assert result["env"]["PIPELINE_LOCAL_NUM_CTX"] == "32768"


def test_local_without_overrides_falls_back_to_shared_defaults():
    result = _local("some:tag")
    assert result["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "0.3"
    assert result["env"]["PIPELINE_LOCAL_NUM_CTX"] == "16384"


def test_gptoss_entry_uses_tuned_temperature_and_num_ctx():
    env = MODELS["gptoss"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == "1.0"
    assert env["PIPELINE_LOCAL_NUM_CTX"] == "32768"
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == "gpt-oss:20b"
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_gptoss_temp03_entry_uses_low_temperature_and_matches_gptoss_ctx():
    gptoss_env = MODELS["gptoss"]["env"]
    env = MODELS["gptoss_temp03"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == "0.3"
    assert env["PIPELINE_LOCAL_NUM_CTX"] == gptoss_env["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == "32768"
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == gptoss_env["PIPELINE_LOCAL_MODEL_DEFAULT"]
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"
    assert env["PIPELINE_LOCAL_TEMPERATURE"] != gptoss_env["PIPELINE_LOCAL_TEMPERATURE"]


def test_gptoss_devstral_review_matches_gptoss_temp03_dispatch_settings():
    """Same dispatch config as gptoss_temp03 - only the review model/backend
    differ - so this is a controlled asymmetric-review experiment, not a
    confound of two changes at once."""
    baseline = MODELS["gptoss_temp03"]["env"]
    env = MODELS["gptoss_devstral_review"]["env"]
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == baseline["PIPELINE_LOCAL_MODEL_DEFAULT"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == baseline["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == baseline["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_gptoss_temp03_auto_matches_gptoss_temp03_except_dispatch_backend():
    """Same dispatch/review settings as gptoss_temp03 - only
    PIPELINE_BACKEND_DISPATCH differs - so this isolates the
    escalate-to-Claude mechanism as the sole variable, not a confound with
    a different temperature/ctx/model."""
    baseline = MODELS["gptoss_temp03"]["env"]
    env = MODELS["gptoss_temp03_auto"]["env"]
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == baseline["PIPELINE_LOCAL_MODEL_DEFAULT"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == baseline["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == baseline["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_BACKEND_DISPATCH"] == "auto"
    assert baseline["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_gptoss_devstral_review_routes_review_to_devstral_locally():
    env = MODELS["gptoss_devstral_review"]["env"]
    assert env["PIPELINE_BACKEND_REVIEW"] == "local"
    assert env["PIPELINE_LOCAL_REVIEW_MODEL"] == "devstral:24b"
    # Dispatch model must NOT equal the review model - otherwise this isn't
    # actually testing asymmetric review.
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] != env["PIPELINE_LOCAL_REVIEW_MODEL"]


def test_qwen36_cell_disables_thinking_and_inherits_shared_defaults():
    """The qwen36 cell runs a Qwen3 hybrid model (batiai/qwen3.6-27b:q3) with
    thinking suppressed via PIPELINE_LOCAL_THINK=false, and otherwise inherits
    the shared local-agent defaults. Guards against accidental removal of the
    think flag (which would silently re-break the tool-calling loop) and
    against drift in the model tag."""
    env = MODELS["qwen36"]["env"]
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == "batiai/qwen3.6-27b:q3"
    assert env["PIPELINE_LOCAL_THINK"] == "false"
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"
    # Inherits the shared defaults (same Ollama endpoint/ctx/steps as devstral).
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_LOCAL_MAX_STEPS"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_MAX_STEPS"]
    assert env["PIPELINE_LOCAL_ENDPOINT"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_ENDPOINT"]


def test_gptoss_qwen36coder_review_routes_review_to_qwen36coder_locally():
    env = MODELS["gptoss_qwen36coder_review"]["env"]
    assert env["PIPELINE_BACKEND_REVIEW"] == "local"
    assert env["PIPELINE_LOCAL_REVIEW_MODEL"] == "qwen3-coder:30b"
    # Dispatch model must NOT equal the review model - otherwise this isn't
    # actually testing asymmetric review.
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] != env["PIPELINE_LOCAL_REVIEW_MODEL"]
