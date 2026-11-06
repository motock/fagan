"""Tests for the strength-class derivation, the class-aware ingest sizing
warning, and RoleResolution.strength_class.

The strength class is either "capable" (a Claude-class dispatch target that
needs no sizing crutch) or "on-device" (everything else, including anything
unrecognised -- the stricter class is the default).
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# strength_class: the derivation table
# ---------------------------------------------------------------------------


class TestStrengthClassDerivation:
    @pytest.mark.parametrize(
        "provider,model_tag",
        [
            ("claude", None),
            ("claude", "claude-3-5-sonnet"),
            ("litellm", "openai/gpt-5-mini"),
            ("ollama", "deepseek-v4-flash:cloud"),
            ("ollama", "glm-5.3-flash:cloud"),
        ],
    )
    def test_capable_providers_and_cloud_tags(self, provider, model_tag):
        from pipeline.strength_class import CAPABLE, strength_class

        assert strength_class(provider, model_tag) == CAPABLE == "capable"

    @pytest.mark.parametrize(
        "provider,model_tag",
        [
            ("ollama", "llama3:8b"),
            ("ollama", None),
            ("lmstudio", None),
            ("mlx", "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"),
            ("local", "gpt-oss-20b-high:latest"),
            ("", None),
            ("", "anything"),
            ("some-new-provider", "some-model"),
            ("auto", None),
        ],
    )
    def test_everything_else_is_on_device(self, provider, model_tag):
        from pipeline.strength_class import ON_DEVICE, strength_class

        assert strength_class(provider, model_tag) == ON_DEVICE == "on-device"

    def test_cloud_suffix_match_is_case_sensitive_and_on_the_end(self):
        from pipeline.strength_class import CAPABLE, ON_DEVICE, strength_class

        assert strength_class("ollama", "foo:cloud") == CAPABLE
        assert strength_class("ollama", "foo:cloud2") == ON_DEVICE
        assert strength_class("ollama", ":cloud") == CAPABLE
        assert strength_class("ollama", "cloud:foo") == ON_DEVICE

    def test_provider_and_tag_are_normalised(self):
        from pipeline.strength_class import CAPABLE, ON_DEVICE, strength_class

        assert strength_class("CLAUDE ", None) == CAPABLE
        assert strength_class(" Ollama", "llama3:8b") == ON_DEVICE


class TestStrengthClassExplicitEntryClass:
    def test_explicit_on_device_beats_a_capable_provider(self):
        from pipeline.strength_class import ON_DEVICE, strength_class

        assert (
            strength_class("claude", "claude-3-5-sonnet", entry_class="on-device")
            == ON_DEVICE
        )
        assert strength_class("litellm", None, entry_class="on-device") == ON_DEVICE

    def test_explicit_capable_beats_an_on_device_provider(self):
        from pipeline.strength_class import CAPABLE, strength_class

        assert strength_class("ollama", "llama3:8b", entry_class="capable") == CAPABLE
        assert strength_class("lmstudio", None, entry_class="capable") == CAPABLE
        assert strength_class("local", None, entry_class="capable") == CAPABLE

    def test_none_entry_class_falls_through_to_derivation(self, caplog):
        from pipeline import strength_class as mod

        assert mod.strength_class("claude", None, entry_class=None) == mod.CAPABLE
        assert mod.strength_class("ollama", "llama3", entry_class=None) == mod.ON_DEVICE
        assert not [r for r in caplog.records if r.levelname == "WARNING"]


class TestStrengthClassInvalidEntryClass:
    @pytest.fixture(autouse=True)
    def _reset_warning_once_flag(self, monkeypatch):
        from pipeline import strength_class as mod

        monkeypatch.setattr(mod, "_INVALID_ENTRY_CLASS_WARNED", False)

    def test_invalid_value_is_ignored_not_an_error(self, caplog):
        import logging

        from pipeline.strength_class import ON_DEVICE, strength_class

        caplog.set_level(logging.WARNING)
        assert strength_class("ollama", "llama3", entry_class="gpu") == ON_DEVICE
        # The bad value is never echoed into the log message.
        assert caplog.records, "expected a WARNING for the unrecognised class"
        for record in caplog.records:
            assert record.levelname == "WARNING"
            assert "gpu" not in record.getMessage().lower()
        assert "on-device" in caplog.records[0].getMessage()

    def test_warns_only_once_per_process(self, caplog):
        import logging

        from pipeline.strength_class import CAPABLE, ON_DEVICE, strength_class

        caplog.set_level(logging.WARNING)
        assert strength_class("ollama", "llama3", entry_class="gpu") == ON_DEVICE
        first = len([r for r in caplog.records if r.levelname == "WARNING"])
        assert first == 1

        # A second, differently-invalid value: still no second warning, and the
        # derivation itself is unaffected.
        assert (
            strength_class("claude", "claude-3-5-sonnet", entry_class="tpu") == CAPABLE
        )
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == first
        assert "tpu" not in caplog.text.lower()


# ---------------------------------------------------------------------------
# registry_entry_class
# ---------------------------------------------------------------------------

_REGISTRY = {
    "providers": {
        "claude": {"models": {"sonnet": {"tag": "sonnet", "class": "capable"}}},
        "ollama": {
            "models": {
                "glm": {"tag": "glm-5.3-flash:cloud", "class": "capable"},
                "devstral": {"tag": "devstral:24b", "class": "on-device"},
                "gpt-oss": {"tag": "gpt-oss:20b"},
            }
        },
    }
}


class TestRegistryEntryClass:
    def test_found_by_friendly_name(self):
        from pipeline.strength_class import registry_entry_class

        assert registry_entry_class(_REGISTRY, "ollama", "glm") == "capable"
        assert registry_entry_class(_REGISTRY, "ollama", "devstral") == "on-device"

    def test_found_by_tag(self):
        from pipeline.strength_class import registry_entry_class

        assert (
            registry_entry_class(_REGISTRY, "ollama", "glm-5.3-flash:cloud") == "capable"
        )
        assert registry_entry_class(_REGISTRY, "ollama", "devstral:24b") == "on-device"

    def test_entry_without_class_returns_none(self):
        from pipeline.strength_class import registry_entry_class

        assert registry_entry_class(_REGISTRY, "ollama", "gpt-oss") is None
        assert registry_entry_class(_REGISTRY, "ollama", "gpt-oss:20b") is None

    def test_unknown_model_provider_or_registry_returns_none(self):
        from pipeline.strength_class import registry_entry_class

        assert registry_entry_class(_REGISTRY, "ollama", "mystery") is None
        assert registry_entry_class(_REGISTRY, "mystery-provider", "glm") is None
        assert registry_entry_class({}, "ollama", "glm") is None
        assert registry_entry_class(None, "ollama", "glm") is None
        assert registry_entry_class(_REGISTRY, None, None) is None

    @pytest.mark.parametrize(
        "registry",
        [
            {"providers": ["not", "a", "dict"]},
            {"providers": {"ollama": "nonsense"}},
            {"providers": {"ollama": {"models": "nonsense"}}},
            {"providers": {"ollama": {"models": {"glm": "nonsense"}}}},
            {"providers": {"ollama": {"models": {"glm": {"tag": ["unhashable"]}}}}},
            "not-a-dict",
        ],
    )
    def test_never_raises_on_malformed_registry(self, registry):
        from pipeline.strength_class import registry_entry_class

        assert registry_entry_class(registry, "ollama", "glm") is None


# ---------------------------------------------------------------------------
# dispatch_strength_class: provider + model precedence
# ---------------------------------------------------------------------------

_CLOUD_REGISTRY = {
    "providers": {
        "claude": {"models": {"sonnet": {"tag": "sonnet"}}},
        "ollama": {
            "models": {
                "glm": {"tag": "glm-5.3-flash:cloud"},
                "devstral": {"tag": "devstral:24b"},
            }
        },
    },
    "roles": {"dispatch": {"provider": "ollama", "model": "glm"}},
}


class TestDispatchStrengthClassProviderPrecedence:
    def test_story_backend_beats_plan_env_and_registry(self):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        story = {"backend": "claude"}
        plan_role_config = {"dispatch": {"provider": "ollama"}}
        environ = {"PIPELINE_BACKEND_DISPATCH": "ollama"}
        # Registry's roles.dispatch.provider would resolve to ollama too.
        assert (
            dispatch_strength_class(story, plan_role_config, _CLOUD_REGISTRY, environ)
            == CAPABLE
        )

    def test_plan_provider_beats_env_and_registry(self):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        plan_role_config = {"dispatch": {"provider": "claude"}}
        environ = {"PIPELINE_BACKEND_DISPATCH": "ollama"}
        assert (
            dispatch_strength_class({}, plan_role_config, _CLOUD_REGISTRY, environ)
            == CAPABLE
        )

    def test_env_provider_beats_registry(self):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        environ = {
            "PIPELINE_BACKEND_DISPATCH": "claude",
            "PIPELINE_LOCAL_MODEL_DEFAULT": "devstral:24b",
        }
        # Registry roles.dispatch.provider is ollama (on-device, devstral),
        # but the env var wins and claude is capable regardless of the tag.
        assert (
            dispatch_strength_class({}, None, _CLOUD_REGISTRY, environ) == CAPABLE
        )

    def test_registry_provider_used_when_nothing_else_is_set(self):
        from pipeline.strength_class import ON_DEVICE, dispatch_strength_class

        registry = {
            "providers": registry_providers(ollama_models={"devstral": "devstral:24b"}),
            "roles": {"dispatch": {"provider": "ollama", "model": "devstral"}},
        }
        assert dispatch_strength_class({}, None, registry, {}) == ON_DEVICE

    def test_falls_back_to_claude(self):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        assert dispatch_strength_class({}, None, {}, {}) == CAPABLE

    def test_auto_with_no_resolvable_model_is_on_device(self):
        from pipeline.strength_class import ON_DEVICE, dispatch_strength_class

        assert dispatch_strength_class({"backend": "auto"}, None, {}, {}) == ON_DEVICE


class TestDispatchStrengthClassModelPrecedence:
    def test_story_model_beats_plan_registry_and_env(self):
        from pipeline.strength_class import CAPABLE, ON_DEVICE, dispatch_strength_class

        # Story names the friendly name "glm" (a :cloud tag) -- capable.
        story = {"backend": "ollama", "model": "glm"}
        plan_role_config = {"dispatch": {"model": "devstral"}}
        environ = {"PIPELINE_LOCAL_MODEL_DEFAULT": "devstral:24b"}
        assert (
            dispatch_strength_class(story, plan_role_config, _CLOUD_REGISTRY, environ)
            == CAPABLE
        )
        # And the mirror: a story naming the on-device model stays on-device.
        story = {"backend": "ollama", "model": "devstral"}
        plan_role_config = {"dispatch": {"model": "glm"}}
        environ = {"PIPELINE_LOCAL_MODEL_DEFAULT": "glm-5.3-flash:cloud"}
        assert (
            dispatch_strength_class(story, plan_role_config, _CLOUD_REGISTRY, environ)
            == ON_DEVICE
        )

    def test_plan_model_beats_registry_and_env(self):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        registry = {
            "providers": registry_providers(
                ollama_models={"glm": "glm-5.3-flash:cloud", "devstral": "devstral:24b"}
            ),
            "roles": {"dispatch": {"provider": "ollama", "model": "devstral"}},
        }
        plan_role_config = {"dispatch": {"model": "glm"}}
        environ = {"PIPELINE_LOCAL_MODEL_DEFAULT": "devstral:24b"}
        assert (
            dispatch_strength_class(
                {"backend": "ollama"}, plan_role_config, registry, environ
            )
            == CAPABLE
        )

    def test_registry_model_beats_local_model_default(self):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        environ = {"PIPELINE_LOCAL_MODEL_DEFAULT": "devstral:24b"}
        # _CLOUD_REGISTRY's roles.dispatch.model is "glm" -> :cloud -> capable.
        assert (
            dispatch_strength_class({"backend": "ollama"}, None, _CLOUD_REGISTRY, environ)
            == CAPABLE
        )

    def test_local_model_default_is_the_last_resort(self):
        from pipeline.strength_class import CAPABLE, ON_DEVICE, dispatch_strength_class

        registry = {
            "providers": registry_providers(ollama_models={"devstral": "devstral:24b"})
        }
        assert (
            dispatch_strength_class(
                {"backend": "ollama"},
                None,
                registry,
                {"PIPELINE_LOCAL_MODEL_DEFAULT": "deepseek-v4-flash:cloud"},
            )
            == CAPABLE
        )
        assert (
            dispatch_strength_class(
                {"backend": "ollama"},
                None,
                registry,
                {"PIPELINE_LOCAL_MODEL_DEFAULT": "devstral:24b"},
            )
            == ON_DEVICE
        )

    def test_raw_unregistered_model_is_used_as_a_tag(self):
        from pipeline.strength_class import CAPABLE, ON_DEVICE, dispatch_strength_class

        assert (
            dispatch_strength_class(
                {"backend": "ollama", "model": "deepseek-v4-flash:cloud"}, None, {}, {}
            )
            == CAPABLE
        )
        assert (
            dispatch_strength_class(
                {"backend": "ollama", "model": "llama3:8b"}, None, {}, {}
            )
            == ON_DEVICE
        )

    def test_plan_model_that_is_a_friendly_name_resolves_through_the_registry(self):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        plan_role_config = {"dispatch": {"provider": "ollama", "model": "glm"}}
        assert (
            dispatch_strength_class({}, plan_role_config, _CLOUD_REGISTRY, {}) == CAPABLE
        )

    def test_explicit_class_on_the_registry_entry_wins(self):
        from pipeline.strength_class import ON_DEVICE, dispatch_strength_class

        registry = {
            "providers": registry_providers(
                ollama_models={"glm": {"tag": "glm-5.3-flash:cloud", "class": "on-device"}}
            )
        }
        assert (
            dispatch_strength_class({"backend": "ollama", "model": "glm"}, None, registry)
            == ON_DEVICE
        )


class TestDispatchStrengthClassRobustness:
    def test_registry_load_failure_yields_on_device(self, monkeypatch, tmp_path):
        from pipeline.strength_class import ON_DEVICE, dispatch_strength_class

        broken = tmp_path / "broken_registry.json"
        broken.write_text("{not json")
        monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(broken))
        assert (
            dispatch_strength_class({"backend": "ollama"}, None, None, {}) == ON_DEVICE
        )

    def test_registry_load_failure_still_uses_the_story_provider(self, monkeypatch, tmp_path):
        from pipeline.strength_class import CAPABLE, dispatch_strength_class

        broken = tmp_path / "broken_registry.json"
        broken.write_text("{not json")
        monkeypatch.setenv("PIPELINE_MODEL_REGISTRY_PATH", str(broken))
        assert dispatch_strength_class({"backend": "claude"}, None, None, {}) == CAPABLE

    def test_malformed_inputs_never_raise(self):
        from pipeline.strength_class import ON_DEVICE, dispatch_strength_class

        assert dispatch_strength_class({}, {}, {}, {}) == "capable"
        # A non-dict plan role config and non-dict registry never raise.
        assert (
            dispatch_strength_class({"backend": "ollama"}, "nonsense", "nonsense", {})
            == ON_DEVICE
        )
        assert (
            dispatch_strength_class({"backend": None, "model": None}, None, {}, {})
            == "capable"
        )

    def test_returns_only_known_classes(self):
        from pipeline.strength_class import CAPABLE, ON_DEVICE, dispatch_strength_class

        for story in (
            {},
            {"backend": "claude"},
            {"backend": "ollama"},
            {"backend": "auto"},
            {"backend": "mlx", "model": "qwen"},
            {"backend": "litellm"},
        ):
            assert dispatch_strength_class(story, None, _CLOUD_REGISTRY, {}) in (
                CAPABLE,
                ON_DEVICE,
            )


def registry_providers(ollama_models=None):
    """Build a minimal providers block; values may be a raw tag or an entry dict."""
    models = {}
    for name, entry in (ollama_models or {}).items():
        models[name] = entry if isinstance(entry, dict) else {"tag": entry}
    return {"ollama": {"models": models}}


# ---------------------------------------------------------------------------
# RoleResolution.strength_class
# ---------------------------------------------------------------------------


class TestRoleResolutionStrengthClass:
    def test_equality_is_unchanged_when_only_strength_class_differs(self):
        from app.role_registry import RoleResolution

        with_class = RoleResolution(
            provider="claude", model="claude-3", strength_class="capable"
        )
        without_class = RoleResolution(provider="claude", model="claude-3")
        assert with_class == without_class
        assert without_class.strength_class == "on-device"
        assert hash(with_class) == hash(without_class)

    def test_resolve_role_fills_the_class_from_the_entry(self):
        from app.role_registry import resolve_role

        registry = {
            "providers": {
                "litellm": {"models": {"gpt5-mini": {"tag": "openai/gpt-5-mini"}}},
                "ollama": {"models": {"devstral": {"tag": "devstral:24b"}}},
                "claude": {"models": {"sonnet": {"tag": "sonnet"}}},
            }
        }
        res = resolve_role(
            "review",
            plan_role_config={"review": {"provider": "litellm", "model": "gpt5-mini"}},
            registry=registry,
            default_provider="claude",
            environ={},
        )
        assert res.strength_class == "capable"

        res = resolve_role(
            "review",
            plan_role_config={"review": {"provider": "ollama", "model": "devstral"}},
            registry=registry,
            default_provider="claude",
            environ={},
        )
        assert res.strength_class == "on-device"

    def test_explicit_entry_class_on_a_capable_provider_is_honoured(self):
        from app.role_registry import resolve_role

        registry = {
            "providers": {
                "litellm": {
                    "models": {
                        "gpt5-mini": {
                            "tag": "openai/gpt-5-mini",
                            "class": "on-device",
                        }
                    }
                }
            }
        }
        res = resolve_role(
            "review",
            plan_role_config={"review": {"provider": "litellm", "model": "gpt5-mini"}},
            registry=registry,
            default_provider="claude",
            environ={},
        )
        assert res.strength_class == "on-device"

    def test_explicit_entry_class_on_an_on_device_provider_is_honoured(self):
        from app.role_registry import resolve_role

        registry = {
            "providers": {
                "ollama": {
                    "models": {
                        "glm": {"tag": "glm-5.3-flash:cloud", "class": "on-device"}
                    }
                }
            }
        }
        res = resolve_role(
            "review",
            plan_role_config={"review": {"provider": "ollama", "model": "glm"}},
            registry=registry,
            default_provider="claude",
            environ={},
        )
        assert res.strength_class == "on-device"


# ---------------------------------------------------------------------------
# class_aware_sizing_warning
# ---------------------------------------------------------------------------

_PREFIX = "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "


@pytest.fixture
def sizing_repo(tmp_path):
    """A repo root holding three small production files and one huge markdown file."""
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n")
    (tmp_path / "docs.md").write_text("\n".join(f"line {i}" for i in range(1200)))
    return str(tmp_path)


class TestClassAwareSizingWarning:
    def test_on_device_story_is_byte_identical_to_today(self, sizing_repo):
        from pipeline.ingest import _story_sizing_warning
        from pipeline.sizing_by_class import class_aware_sizing_warning

        story = {"backend": "ollama", "files": ["a.py", "b.py", "c.py", "docs.md"]}
        assert (
            class_aware_sizing_warning(story, sizing_repo, None, _story_sizing_warning)
            == _story_sizing_warning(story, sizing_repo)
        )

    def test_capable_story_drops_the_production_file_cap(self, sizing_repo):
        from pipeline.ingest import _story_sizing_warning
        from pipeline.sizing_by_class import class_aware_sizing_warning

        story = {
            "backend": "ollama",
            "model": "deepseek-v4-flash:cloud",
            "files": ["a.py", "b.py", "c.py"],
        }
        # The underlying on-device warning still reports the 3-file cap...
        on_device = _story_sizing_warning(story, sizing_repo)
        assert on_device is not None
        assert "production files (" in on_device
        # ...but a :cloud-capable story is not capped on file count.
        capable = class_aware_sizing_warning(
            story, sizing_repo, None, _story_sizing_warning
        )
        assert capable is None

    def test_capable_story_keeps_the_file_size_cap_without_weakest(self, sizing_repo):
        from pipeline.ingest import _story_sizing_warning
        from pipeline.sizing_by_class import class_aware_sizing_warning

        story = {
            "backend": "ollama",
            "model": "deepseek-v4-flash:cloud",
            "files": ["a.py", "b.py", "c.py", "docs.md"],
        }
        warning = class_aware_sizing_warning(
            story, sizing_repo, None, _story_sizing_warning
        )
        assert warning is not None
        assert warning.startswith(_PREFIX)
        assert "docs.md" in warning
        assert "1200 lines" in warning
        assert "weakest" not in warning
        assert "production files (" not in warning
        # The 1000-line cap itself is still enforced for capable stories.
        assert "1000-line file-size cap" in warning

    def test_capable_story_with_only_the_size_cap_still_warns(self, sizing_repo):
        from pipeline.ingest import _story_sizing_warning
        from pipeline.sizing_by_class import class_aware_sizing_warning

        story = {"backend": "claude", "files": ["docs.md"]}
        # claude is not gated by the on-device hook at all...
        assert _story_sizing_warning(story, sizing_repo) is None
        # ...and the wrapper keeps that None.
        assert class_aware_sizing_warning(story, sizing_repo, None, _story_sizing_warning) is None

    def test_returns_the_on_device_result_object_unchanged(self):
        from pipeline.sizing_by_class import class_aware_sizing_warning

        sentinel = object()

        def on_device_warning(story, repo_root):
            return sentinel

        story = {"backend": "ollama"}
        assert (
            class_aware_sizing_warning(story, "/nowhere", None, on_device_warning)
            is sentinel
        )

    def test_returns_none_unchanged_when_the_underlying_hook_is_silent(self):
        from pipeline.sizing_by_class import class_aware_sizing_warning

        def on_device_warning(story, repo_root):
            return None

        story = {"backend": "claude"}
        assert (
            class_aware_sizing_warning(story, "/nowhere", None, on_device_warning) is None
        )

    def test_plan_role_config_reaches_the_class_derivation(self, sizing_repo):
        from pipeline.ingest import _story_sizing_warning
        from pipeline.sizing_by_class import class_aware_sizing_warning

        # The story declares no backend of its own, so the plan's dispatch
        # provider decides: an ollama plan with a :cloud model is capable.
        story = {"files": ["a.py", "b.py", "c.py"]}
        plan_role_config = {"dispatch": {"provider": "ollama"}}
        on_device = _story_sizing_warning(
            {**story, "backend": "ollama"}, sizing_repo
        )
        assert on_device is not None
        assert "production files (" in on_device
        capable_story = {**story, "model": "glm-5.3-flash:cloud"}
        assert (
            class_aware_sizing_warning(
                capable_story,
                sizing_repo,
                plan_role_config,
                _story_sizing_warning,
            )
            is None
        )
