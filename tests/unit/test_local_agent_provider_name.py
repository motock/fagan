"""The local agent must ask for the provider the harness launched it with.

app/harness.py passes the resolved provider to the agent subprocess as
LOCAL_AGENT_PROVIDER, which scripts/local_agent_config.py reads into
PROVIDER. Before SG-02 the provider chat turn called
get_local_provider() with no name, so app/inference_providers resolved the
provider from PIPELINE_LOCAL_PROVIDER instead (default "ollama") and the
agent talked to the Ollama endpoint of an OpenAI-compatible server
(observed: provider=lmstudio, then 404 on http://model:8080/api/chat).

Only get_local_provider is mocked here: it is the external
provider-selection boundary. Everything else (the chat turn, the Ollama
branch of chat()) runs for real.
"""
import pytest

from tests.unit._local_agent_oracle_test_helpers import lao
from tests.unit._local_agent_test_helpers import la

_MESSAGES = [{"role": "user", "content": "hi"}]


class _FakeProvider:
    """Minimal provider whose chat() returns a bare envelope, no I/O."""

    def __init__(self):
        self.chat_calls = 0

    def chat(self, messages, **kwargs):
        self.chat_calls += 1
        return {"message": {"role": "assistant", "content": "hi"}}


def _install_recording_provider(monkeypatch, module):
    """Patch the provider-selection boundary to record the requested name."""
    names = []
    provider = _FakeProvider()

    def _get_local_provider(name=None):
        names.append(name)
        return provider

    monkeypatch.setattr(
        module.inference_providers, "get_local_provider", _get_local_provider,
    )
    return names


# ---------- local_agent (scripts/local_agent.py) ----------

def test_provider_chat_turn_asks_for_harness_provider(monkeypatch):
    """PROVIDER=lmstudio with no PIPELINE_LOCAL_PROVIDER -> request "lmstudio"."""
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)
    monkeypatch.setattr(la, "PROVIDER", "lmstudio")
    names = _install_recording_provider(monkeypatch, la)

    la._provider_chat_turn(_MESSAGES)

    assert names == ["lmstudio"]


def test_provider_chat_turn_ignores_pipeline_local_provider_env(monkeypatch):
    """The harness's PROVIDER wins over a PIPELINE_LOCAL_PROVIDER=ollama env."""
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    monkeypatch.setattr(la, "PROVIDER", "mlx")
    names = _install_recording_provider(monkeypatch, la)

    la._provider_chat_turn(_MESSAGES)
    la._provider_chat_turn(_MESSAGES)

    assert names == ["mlx", "mlx"]


# ---------- local_agent_oracle (scripts/local_agent_oracle.py) ----------

def test_oracle_provider_chat_turn_asks_for_harness_provider(monkeypatch):
    """PROVIDER=lmstudio with no PIPELINE_LOCAL_PROVIDER -> request "lmstudio"."""
    monkeypatch.delenv("PIPELINE_LOCAL_PROVIDER", raising=False)
    monkeypatch.setattr(lao, "PROVIDER", "lmstudio")
    names = _install_recording_provider(monkeypatch, lao)

    lao._provider_chat_turn(_MESSAGES)

    assert names == ["lmstudio"]


def test_oracle_provider_chat_turn_ignores_pipeline_local_provider_env(monkeypatch):
    """The harness's PROVIDER wins over a PIPELINE_LOCAL_PROVIDER=ollama env."""
    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    monkeypatch.setattr(lao, "PROVIDER", "mlx")
    names = _install_recording_provider(monkeypatch, lao)

    lao._provider_chat_turn(_MESSAGES)
    lao._provider_chat_turn(_MESSAGES)

    assert names == ["mlx", "mlx"]


# ---------- negative: the Ollama path must never select a provider ----------

@pytest.mark.parametrize("module", [la, lao], ids=["local_agent", "local_agent_oracle"])
def test_ollama_chat_never_calls_get_local_provider(monkeypatch, module):
    """PROVIDER=ollama streams; provider selection must stay untouched."""
    streamed = []

    def _stream_one_turn(payload):
        streamed.append(payload)
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setenv("PIPELINE_LOCAL_PROVIDER", "ollama")
    monkeypatch.setattr(module, "PROVIDER", "ollama")
    monkeypatch.setattr(module, "_stream_one_turn", _stream_one_turn)
    names = _install_recording_provider(monkeypatch, module)

    first = module.chat(_MESSAGES)
    second = module.chat(_MESSAGES)

    assert first["content"] == "ok"
    assert second["content"] == "ok"
    assert len(streamed) == 2, "the ollama streaming path must be used"
    assert names == [], "the ollama path must not select a provider"
