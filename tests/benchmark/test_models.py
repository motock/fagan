"""Tests for tests/benchmark/models.py per-model temperature/num_ctx overrides."""
from models import MODELS, _local, _LOCAL_AGENT_ENV


def test_devstral_retains_tuned_temperature_and_num_ctx():
    env = MODELS["devstral"]["env"]
    assert env["LOCAL_AGENT_TEMPERATURE"] == "0.3"
    assert env["LOCAL_AGENT_NUM_CTX"] == "16384"


def test_minimax_entry_unchanged():
    env = MODELS["minimax"]["env"]
    assert env["LOCAL_AGENT_TEMPERATURE"] == _LOCAL_AGENT_ENV["LOCAL_AGENT_TEMPERATURE"]
    assert env["LOCAL_AGENT_NUM_CTX"] == _LOCAL_AGENT_ENV["LOCAL_AGENT_NUM_CTX"]
    assert env["LOCAL_AGENT_ENDPOINT"] == _LOCAL_AGENT_ENV["LOCAL_AGENT_ENDPOINT"]
    assert env["LOCAL_AGENT_TIMEOUT"] == _LOCAL_AGENT_ENV["LOCAL_AGENT_TIMEOUT"]
    assert env["PIPELINE_LOCAL_MAX_STEPS"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_MAX_STEPS"]


def test_local_with_overrides_replaces_temperature_and_num_ctx():
    result = _local("some:tag", temperature=1.0, num_ctx=32768)
    assert result["env"]["LOCAL_AGENT_TEMPERATURE"] == "1.0"
    assert result["env"]["LOCAL_AGENT_NUM_CTX"] == "32768"


def test_local_without_overrides_falls_back_to_shared_defaults():
    result = _local("some:tag")
    assert result["env"]["LOCAL_AGENT_TEMPERATURE"] == "0.3"
    assert result["env"]["LOCAL_AGENT_NUM_CTX"] == "16384"
