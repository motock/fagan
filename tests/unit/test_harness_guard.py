import app
import pytest
from pathlib import Path

from app.backend_claude import ClaudeCliDriver
from app.backend_ollama import OllamaDriver
from pipeline import execution

# Helper to patch get_harness and spawn_harness
class DummyHarness:
    def build_agent_command(self, request):
        class DummyCommand:
            argv = ["dummy"]
        return DummyCommand()

class DummyHandle:
    def __init__(self):
        self.model = None

@pytest.fixture
def patch_get_harness(monkeypatch):
    monkeypatch.setattr("app.backend_claude.get_harness", lambda name: DummyHarness())
    monkeypatch.setattr("app.backend_ollama.get_harness", lambda name: DummyHarness())
    monkeypatch.setattr(app.backend_ollama, "_resolve_local_model", lambda model, provider=None: model)

def test_claude_cross_harness_raises(monkeypatch, patch_get_harness, tmp_path):
    monkeypatch.setenv("PIPELINE_AGENT_HARNESS", "local")
    driver = ClaudeCliDriver()
    with pytest.raises(NotImplementedError) as exc:
        driver.dispatch("prompt", system=None, model="claude", allowed_tools=None, cwd=tmp_path, log_path=tmp_path, append=False)
    assert "PIPELINE_AGENT_HARNESS" in str(exc.value)
    assert "local" in str(exc.value)

def test_ollama_cross_harness_raises(monkeypatch, patch_get_harness, tmp_path):
    monkeypatch.setenv("PIPELINE_AGENT_HARNESS", "claude")
    driver = OllamaDriver()
    with pytest.raises(NotImplementedError) as exc:
        driver.dispatch(
            prompt="prompt",
            system=None,
            model="model",
            allowed_tools=None,
            cwd=tmp_path,
            log_path=tmp_path,
            append=False,
            acceptance=[],
            resume_transcript_path=None,
            resume_append_content=None,
            rework_full_suite=False,
        )
    assert "PIPELINE_AGENT_HARNESS" in str(exc.value)
    assert "claude" in str(exc.value)

def test_claude_matching_harness_passes(monkeypatch, patch_get_harness, tmp_path):
    monkeypatch.setenv("PIPELINE_AGENT_HARNESS", "claude")
    driver = ClaudeCliDriver()
    handle = driver.dispatch("prompt", system=None, model="claude", allowed_tools=None, cwd=tmp_path, log_path=tmp_path, append=False)
    assert isinstance(handle, DummyHandle)

def test_ollama_matching_harness_passes(monkeypatch, patch_get_harness, tmp_path):
    monkeypatch.setenv("PIPELINE_AGENT_HARNESS", "local")
    driver = OllamaDriver()
    handle = driver.dispatch(
        prompt="prompt",
        system=None,
        model="model",
        allowed_tools=None,
        cwd=tmp_path,
        log_path=tmp_path,
        append=False,
        acceptance=[],
        resume_transcript_path=None,
        resume_append_content=None,
        rework_full_suite=False,
    )
    assert isinstance(handle, DummyHandle)

def test_unset_harness_passes(monkeypatch, patch_get_harness, tmp_path):
    monkeypatch.delenv("PIPELINE_AGENT_HARNESS", raising=False)
    driver = ClaudeCliDriver()
    handle = driver.dispatch("prompt", system=None, model="claude", allowed_tools=None, cwd=tmp_path, log_path=tmp_path, append=False)
    assert isinstance(handle, DummyHandle)

