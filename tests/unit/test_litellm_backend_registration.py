"""litellm backend registration (RELIABILITY_PLAN b2).

litellm is registered as a Backend driver name by composing OllamaDriver with
provider_name="litellm" (the same pattern as "ollama"/"lmstudio"/"mlx"):
OllamaDriver owns the harness mechanics; the provider owns only the wire
protocol. This file asserts MEMBERSHIP only on the cumulative artifacts
(_DRIVERS, both _VALID_STORY_BACKENDS sets, model_registry.json providers) —
never exact equality/length/hash, since later stories extend them.

Per .claude/rules/testing-config-gates.md, the resolution test (test 4) uses a
STUBBED registry dict, not the live model_registry.json; only test 3 reads the
live file.
"""

import pytest

from app import backend, role_registry
from app.backend import get_backend
from app.backend_ollama import OllamaDriver
from pipeline import server as psrv
from pipeline import service as psvc


# --------------------------------------------------------------------------- #
# 1. get_backend composition
# --------------------------------------------------------------------------- #
def test_get_backend_litellm_returns_ollama_driver_with_litellm_provider():
    driver = get_backend("dispatch", name="litellm")
    assert isinstance(driver, OllamaDriver)
    assert driver.provider.name == "litellm"


# --------------------------------------------------------------------------- #
# 2. Membership in the three cumulative allowlists (no equality assertions)
# --------------------------------------------------------------------------- #
def test_litellm_is_member_of_all_three_allowlists():
    assert "litellm" in backend._DRIVERS
    assert "litellm" in psvc._VALID_STORY_BACKENDS
    assert "litellm" in psrv._VALID_STORY_BACKENDS


# --------------------------------------------------------------------------- #
# 3. Live model_registry.json declares providers.litellm with >=1 model
#    (the only test here that reads the real repo file)
# --------------------------------------------------------------------------- #
def test_live_model_registry_declares_litellm_provider_with_models():
    registry = role_registry.load_registry()
    models = registry["providers"]["litellm"]["models"]
    assert isinstance(models, dict)
    assert models, "providers.litellm.models must declare at least one model"


# --------------------------------------------------------------------------- #
# 4. Stubbed resolution (testing-config-gates: never the live file)
# --------------------------------------------------------------------------- #
def test_resolve_role_litellm_with_stubbed_registry():
    registry = {
        "providers": {
            "litellm": {"models": {"foo": {"tag": "test/gpt5-mini"}}},
        },
        "roles": {},
    }
    resolution = role_registry.resolve_role(
        "dispatch",
        registry=registry,
        plan_role_config={"dispatch": {"provider": "litellm", "model": "foo"}},
        environ={},
    )
    assert resolution.provider == "litellm"
    assert resolution.model == "test/gpt5-mini"


# --------------------------------------------------------------------------- #
# 5. NEGATIVE: unknown driver name still fails closed, message lists litellm
# --------------------------------------------------------------------------- #
def test_litellm_typo_raises_not_implementederror_naming_env_var():
    with pytest.raises(
        NotImplementedError, match="PIPELINE_BACKEND_DISPATCH"
    ) as excinfo:
        get_backend("dispatch", name="litellm-typo")
    # The driver list in the message is cumulative — assert membership only.
    # Quoted form: matches the sorted-list repr element, not the 'litellm-typo'
    # value echoed earlier in the message.
    assert "'litellm'" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 6. NEGATIVE: undeclared model under providers.litellm.models fails closed
# --------------------------------------------------------------------------- #
def test_undeclared_litellm_model_raises_role_registry_error():
    registry = {
        "providers": {
            "litellm": {"models": {"foo": {"tag": "test/gpt5-mini"}}},
        },
        "roles": {},
    }
    with pytest.raises(role_registry.RoleRegistryError):
        role_registry.resolve_role(
            "dispatch",
            registry=registry,
            plan_role_config={"dispatch": {"provider": "litellm", "model": "bar"}},
            environ={},
        )
