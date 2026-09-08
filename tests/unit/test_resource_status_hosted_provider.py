"""Hosted-provider skip for OllamaDriver.resource_status()'s free-memory floor.

A hosted provider (e.g. litellm) serves its models off-host: the model has no
local VRAM/RAM footprint, so a free-memory reading is not evidence about
whether it can serve. The floor must therefore be skipped for hosted providers
(the same way it already is for :cloud tags) while reachability — the real
precondition, SDK importability — still applies.

These tests stub both `_free_memory_mb` and the provider's `reachable` so no
test depends on the real host's free memory or installed packages.
"""
from app import backend_ollama as bo


def _make_driver(provider_name):
    driver = bo.OllamaDriver(provider_name=provider_name)
    # Stub reachability on the exact provider object resource_status()
    # delegates to - never depend on installed SDKs or a live server.
    monkeypatch_target = driver.provider
    return driver, monkeypatch_target


def test_hosted_provider_skips_memory_floor(monkeypatch):
    """litellm (hosted) with 10mb free - far under any floor - must report ok:
    the model is served off-host, so free memory says nothing about serving."""
    driver = bo.OllamaDriver(provider_name="litellm")
    monkeypatch.setattr(driver.provider, "reachable", lambda endpoint: (True, ""))
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 10)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_LITELLM", raising=False)

    status = driver.resource_status(model_tag="glm-4.7")

    assert status["ok"] is True


def test_ondevice_provider_still_enforces_memory_floor(monkeypatch):
    """Regression guard: the skip is provider-scoped, not a blanket weakening.
    Identical stubs with provider ollama must still trip the floor."""
    driver = bo.OllamaDriver(provider_name="ollama")
    monkeypatch.setattr(driver.provider, "reachable", lambda endpoint: (True, ""))
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 10)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_OLLAMA", raising=False)

    status = driver.resource_status(model_tag="llama3")

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_hosted_provider_still_requires_reachable(monkeypatch):
    """The hosted skip must NOT bypass reachability - SDK importability is a
    real precondition even for a hosted model."""
    driver = bo.OllamaDriver(provider_name="litellm")
    monkeypatch.setattr(
        driver.provider,
        "reachable",
        lambda endpoint: (False, "litellm is not installed"),
    )
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 10)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_LITELLM", raising=False)

    status = driver.resource_status(model_tag="glm-4.7")

    assert status["ok"] is False
    assert "litellm is not installed" in status["reason"]


def test_hosted_provider_unimplemented_reachable_fails_closed(monkeypatch):
    """An unimplemented stub provider's NotImplementedError must be converted
    into the {ok, reason} dict, never propagated out of resource_status()."""
    driver = bo.OllamaDriver(provider_name="litellm")

    def _unimplemented(endpoint):
        raise NotImplementedError("litellm provider is a stub")

    monkeypatch.setattr(driver.provider, "reachable", _unimplemented)
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 10)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.delenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB_LITELLM", raising=False)

    status = driver.resource_status(model_tag="glm-4.7")

    assert status["ok"] is False
    assert status["reason"] == "litellm provider is a stub"


def test_hosted_providers_registry_membership():
    """_HOSTED_PROVIDERS is a registry future providers extend - assert
    membership only, never the frozenset's exact contents or length."""
    assert "litellm" in bo._HOSTED_PROVIDERS
    assert "ollama" not in bo._HOSTED_PROVIDERS