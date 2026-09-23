"""TDD suite: a story's bare registry model NAME must be translated to its
concrete tag before dispatch hands it to a driver.

ROOT CAUSE
----------
``pipeline/dispatch_routing.py::_resolve_dispatch_target`` returns
``story["model"]`` verbatim. A value like ``"deepseek-v4.1-flash"`` (no ``:``
and no ``/``) is later passed to ``app/ollama_prompt_utils.py::
_resolve_local_model``, which only knows the tiers ``opus|sonnet|haiku`` and
returns the DEFAULT local model for anything else. The registry
(``role_registry.load_registry()["providers"][<provider>]["models"][<name>]
["tag"]``) maps ``deepseek-v4.1-flash`` -> ``deepseek-v4.1-flash:cloud``, so
the pin silently degrades to the driver default.

CONTRACT PINNED BY THIS FILE (the implementation must provide it)
-----------------------------------------------------------------
1. ``pipeline.dispatch_routing._registry_tag_for(provider, model) -> str``,
   defined immediately BEFORE ``_resolve_dispatch_target``:

   * a bare name declared under ``providers.<provider>.models`` returns that
     entry's ``tag``;
   * a value that already looks like a tag (contains ``:`` or ``/``) is
     returned unchanged and the registry is NOT consulted;
   * a bare name that is neither a declared registry name nor one of the tier
     names ``opus``/``sonnet``/``haiku`` is returned unchanged AND logged as a
     warning on the ``"pipeline"`` logger naming the model and the provider;
   * a tier name with no registry entry is returned unchanged with NO warning;
   * a provider missing from the registry, an empty registry, a malformed
     registry (``KeyError``/``TypeError``) and a ``RoleRegistryError`` from
     ``load_registry`` all fail OPEN to returning ``model`` unchanged - the
     helper never raises.

2. ``_resolve_dispatch_target`` runs ``story["model"]`` through
   ``_registry_tag_for`` for the provider the story actually dispatches on,
   while a story with no model still gets the model ``resolve_role`` resolved.
   The ``except role_registry.RoleRegistryError`` fail-open branch is
   untouched: it still returns no model at all.

3. ``REFERENCE.md`` gains a NEW ``## Story model pins`` section immediately
   before ``## Per-role provider/model configuration`` - outside the pinned
   ``## Plan / story schema`` section, whose body
   ``tests/unit/test_readme_reference_split.py::test_moved_section_body_is_verbatim``
   pins verbatim.

The registry is stubbed at its boundary (``role_registry.load_registry`` is
monkeypatched to return a synthetic dict); no test here reads the real
``model_registry*.json``.
"""

from __future__ import annotations

import copy
import inspect
import logging
from pathlib import Path

import pytest

from app import role_registry
from pipeline import dispatch_routing

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Synthetic registry: never the real model_registry*.json.
_REGISTRY = {
    "providers": {
        "ollama": {
            "models": {
                "deepseek-v4.1-flash": {"tag": "deepseek-v4.1-flash:cloud"},
                "glm-5.3-flash": {"tag": "glm-5.3-flash:cloud"},
            }
        },
        "mlx": {"models": {"qwen": {"tag": "qwen:latest"}}},
    },
    "roles": {},
}

_BARE_NAME = "deepseek-v4.1-flash"
_BARE_TAG = "deepseek-v4.1-flash:cloud"


@pytest.fixture
def registry_calls(monkeypatch):
    """Stub ``load_registry`` at the boundary; return the list of calls."""
    calls: list[object] = []

    def fake_load_registry(path=None):
        calls.append(path)
        return copy.deepcopy(_REGISTRY)

    monkeypatch.setattr(
        dispatch_routing.role_registry, "load_registry", fake_load_registry
    )
    return calls


@pytest.fixture
def dispatch_env(monkeypatch, registry_calls):
    """Stub the dispatch seam: resolve_role -> ollama, safety refs -> False."""
    monkeypatch.setattr(
        "pipeline.dispatch._persona_requires_claude", lambda story: False
    )
    monkeypatch.setattr(
        "pipeline.dispatch._story_has_unwinnable_local_scope", lambda story: False
    )

    def fake_resolve_role(
        role,
        plan_role_config=None,
        registry=None,
        model_fallback=None,
        environ=None,
        default_provider="claude",
    ):
        return role_registry.RoleResolution(
            provider="ollama", model="glm-5.3-flash:cloud"
        )

    monkeypatch.setattr(
        dispatch_routing.role_registry, "resolve_role", fake_resolve_role
    )
    return registry_calls


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# --------------------------------------------------------------------------
# _registry_tag_for: shape / placement
# --------------------------------------------------------------------------


def test_helper_exists_as_a_module_level_function():
    assert hasattr(dispatch_routing, "_registry_tag_for"), (
        "pipeline.dispatch_routing._registry_tag_for is missing"
    )
    assert inspect.isfunction(dispatch_routing._registry_tag_for)


def test_helper_is_defined_immediately_before_resolve_dispatch_target():
    src = (_REPO_ROOT / "pipeline" / "dispatch_routing.py").read_text()
    helper_at = src.index("def _registry_tag_for(")
    target_at = src.index("def _resolve_dispatch_target(")
    assert helper_at < target_at, (
        "_registry_tag_for must be defined BEFORE _resolve_dispatch_target"
    )
    between = src[helper_at:target_at]
    assert "\ndef " not in between, (
        "no other top-level def may sit between _registry_tag_for and "
        "_resolve_dispatch_target"
    )


def test_helper_docstring_documents_tag_and_warning():
    doc = dispatch_routing._registry_tag_for.__doc__ or ""
    assert doc.strip(), "_registry_tag_for must have a docstring"
    assert "tag" in doc
    assert "warning" in doc.lower()


# --------------------------------------------------------------------------
# _registry_tag_for: happy path
# --------------------------------------------------------------------------


def test_bare_declared_name_resolves_to_its_tag(registry_calls):
    assert dispatch_routing._registry_tag_for("ollama", _BARE_NAME) == _BARE_TAG
    assert registry_calls, "the registry must be consulted for a bare name"


def test_bare_declared_name_resolves_for_a_second_provider(registry_calls):
    assert dispatch_routing._registry_tag_for("mlx", "qwen") == "qwen:latest"


def test_return_value_is_a_string(registry_calls):
    assert isinstance(dispatch_routing._registry_tag_for("ollama", _BARE_NAME), str)


# --------------------------------------------------------------------------
# _registry_tag_for: already-a-tag values are returned untouched
# --------------------------------------------------------------------------


def test_colon_tag_returned_unchanged_and_registry_not_consulted(registry_calls):
    assert dispatch_routing._registry_tag_for("ollama", "x:cloud") == "x:cloud"
    assert registry_calls == [], (
        "a value that already looks like a tag must not consult the registry"
    )


def test_slash_tag_returned_unchanged_and_registry_not_consulted(registry_calls):
    assert dispatch_routing._registry_tag_for("ollama", "openai/gpt") == "openai/gpt"
    assert registry_calls == [], (
        "a value that already looks like a tag must not consult the registry"
    )


def test_registry_tag_returned_unchanged_and_registry_not_consulted(registry_calls):
    assert dispatch_routing._registry_tag_for("ollama", _BARE_TAG) == _BARE_TAG
    assert registry_calls == []


# --------------------------------------------------------------------------
# _registry_tag_for: unknown bare names warn, tiers do not
# --------------------------------------------------------------------------


def test_unknown_bare_name_unchanged_and_warns(registry_calls, caplog):
    caplog.set_level(logging.WARNING, logger="pipeline")
    assert (
        dispatch_routing._registry_tag_for("ollama", "totally-unknown-model")
        == "totally-unknown-model"
    )
    records = _warnings(caplog)
    assert records, "an unknown bare name must be logged as a warning"
    assert any(
        r.name == "pipeline"
        and "totally-unknown-model" in r.getMessage()
        and "ollama" in r.getMessage()
        for r in records
    ), (
        "the warning must be on the 'pipeline' logger and name both the model "
        f"and the provider; got {[r.getMessage() for r in records]!r}"
    )


@pytest.mark.parametrize("tier", ["opus", "sonnet", "haiku"])
def test_tier_name_without_registry_entry_unchanged_and_no_warning(
    registry_calls, caplog, tier
):
    caplog.set_level(logging.WARNING, logger="pipeline")
    assert dispatch_routing._registry_tag_for("ollama", tier) == tier
    assert _warnings(caplog) == [], f"the tier name {tier!r} must not be warned about"


def test_name_declared_under_a_different_provider_is_not_translated(registry_calls):
    # deepseek-v4.1-flash is declared under ollama, not mlx: the lookup is
    # scoped to the provider the story dispatches on.
    assert dispatch_routing._registry_tag_for("mlx", _BARE_NAME) == _BARE_NAME


# --------------------------------------------------------------------------
# _registry_tag_for: fail-open paths - never raise
# --------------------------------------------------------------------------


def test_provider_missing_from_registry_returns_model_unchanged(registry_calls):
    assert dispatch_routing._registry_tag_for("litellm", _BARE_NAME) == _BARE_NAME


def test_empty_registry_returns_model_unchanged(monkeypatch):
    monkeypatch.setattr(
        dispatch_routing.role_registry, "load_registry", lambda path=None: {}
    )
    assert dispatch_routing._registry_tag_for("ollama", _BARE_NAME) == _BARE_NAME


def test_load_registry_role_registry_error_fails_open(monkeypatch):
    def boom(path=None):
        raise role_registry.RoleRegistryError("malformed registry")

    monkeypatch.setattr(dispatch_routing.role_registry, "load_registry", boom)
    assert dispatch_routing._registry_tag_for("ollama", _BARE_NAME) == _BARE_NAME


@pytest.mark.parametrize(
    "payload",
    [
        {"providers": {}},
        {"providers": {"ollama": {}}},
        {"providers": {"ollama": {"models": None}}},
        {"providers": {"ollama": None}},
        {"providers": {"ollama": {"models": {}}}},
    ],
    ids=[
        "no-providers",
        "provider-without-models-key",
        "models-is-none",
        "provider-is-none",
        "models-empty",
    ],
)
def test_malformed_registry_fails_open(monkeypatch, payload):
    monkeypatch.setattr(
        dispatch_routing.role_registry, "load_registry", lambda path=None: payload
    )
    # KeyError / TypeError shapes must be swallowed: never raise, return model.
    assert dispatch_routing._registry_tag_for("ollama", _BARE_NAME) == _BARE_NAME


# --------------------------------------------------------------------------
# _resolve_dispatch_target: the story pin is translated
# --------------------------------------------------------------------------


def test_story_bare_registry_name_is_translated_to_its_tag(dispatch_env):
    assert dispatch_routing._resolve_dispatch_target({"model": _BARE_NAME}) == (
        "ollama",
        _BARE_TAG,
    )


def test_story_bare_name_is_translated_for_the_provider_it_dispatches_on(
    dispatch_env,
):
    # story["backend"] wins over the resolved provider, so the lookup must be
    # scoped to "mlx" (which does not declare the name) - not to "ollama".
    assert dispatch_routing._resolve_dispatch_target(
        {"backend": "mlx", "model": _BARE_NAME}
    ) == ("mlx", _BARE_NAME)


def test_story_bare_name_declared_under_the_story_backend_is_translated(
    dispatch_env,
):
    assert dispatch_routing._resolve_dispatch_target(
        {"backend": "mlx", "model": "qwen"}
    ) == ("mlx", "qwen:latest")


def test_story_concrete_tag_is_returned_unchanged(dispatch_env):
    assert dispatch_routing._resolve_dispatch_target(
        {"model": "glm-5.3-flash:cloud"}
    ) == ("ollama", "glm-5.3-flash:cloud")


def test_story_slash_tag_is_returned_unchanged(dispatch_env):
    assert dispatch_routing._resolve_dispatch_target({"model": "openai/gpt"}) == (
        "ollama",
        "openai/gpt",
    )


def test_story_unknown_bare_name_unchanged_and_warns(dispatch_env, caplog):
    caplog.set_level(logging.WARNING, logger="pipeline")
    assert dispatch_routing._resolve_dispatch_target(
        {"model": "totally-unknown-model"}
    ) == ("ollama", "totally-unknown-model")
    assert any(
        r.name == "pipeline"
        and "totally-unknown-model" in r.getMessage()
        and "ollama" in r.getMessage()
        for r in _warnings(caplog)
    )


def test_story_tier_name_unchanged_and_no_warning(dispatch_env, caplog):
    caplog.set_level(logging.WARNING, logger="pipeline")
    assert dispatch_routing._resolve_dispatch_target({"model": "sonnet"}) == (
        "ollama",
        "sonnet",
    )
    assert _warnings(caplog) == []


def test_story_without_model_gets_the_resolved_role_model(dispatch_env):
    assert dispatch_routing._resolve_dispatch_target({}) == (
        "ollama",
        "glm-5.3-flash:cloud",
    )


def test_story_with_empty_model_gets_the_resolved_role_model(dispatch_env):
    # "" is falsy: it must not be treated as a pin (and must not be warned
    # about as an unknown bare name).
    assert dispatch_routing._resolve_dispatch_target({"model": ""}) == (
        "ollama",
        "glm-5.3-flash:cloud",
    )


def test_story_without_model_does_not_consult_the_registry_for_a_tag(
    dispatch_env, caplog
):
    caplog.set_level(logging.WARNING, logger="pipeline")
    dispatch_routing._resolve_dispatch_target({})
    assert dispatch_env == [], "no story model pin means no registry tag lookup"
    assert _warnings(caplog) == []


def test_fail_open_branch_still_returns_no_model(monkeypatch, registry_calls):
    """The except RoleRegistryError branch is untouched: it returns no model."""

    def fake_resolve_role(
        role,
        plan_role_config=None,
        registry=None,
        model_fallback=None,
        environ=None,
        default_provider="claude",
    ):
        if registry is None:
            raise role_registry.RoleRegistryError("no roles.dispatch entry")
        return role_registry.RoleResolution(provider="claude", model="unpinned")

    monkeypatch.setattr(
        dispatch_routing.role_registry, "resolve_role", fake_resolve_role
    )
    monkeypatch.setattr(
        "pipeline.dispatch._persona_requires_claude", lambda story: False
    )
    monkeypatch.setattr(
        "pipeline.dispatch._story_has_unwinnable_local_scope", lambda story: False
    )
    assert dispatch_routing._resolve_dispatch_target({"model": _BARE_NAME}) == (
        "claude",
        None,
    )


# --------------------------------------------------------------------------
# REFERENCE.md: a NEW section, outside the pinned one
# --------------------------------------------------------------------------

# The heading LINE, not the bare phrase: "Per-role provider/model
# configuration" is also mentioned in prose (line 714) before the heading.
_ANCHOR = "\n## Per-role provider/model configuration\n"
_PINNED = "## Plan / story schema"
_NEW_HEADING = "## Story model pins"

_EXPECTED_SECTION = """## Story model pins

A story's `model` may be a Claude tier (`opus | sonnet | haiku`), a
concrete model tag (e.g. `glm-5.3-flash:cloud`), or a model name declared
in the live registry's `providers.<provider>.models` (e.g.
`deepseek-v4.1-flash`). Dispatch resolves a registry name to that entry's
`tag` for the provider the story dispatches on. A bare name that is none of
these is logged as a warning, because the local driver would otherwise run
its default model instead of the pin.
"""


def _reference() -> str:
    return (_REPO_ROOT / "REFERENCE.md").read_text()


def _norm(text: str) -> str:
    return " ".join(text.split())


def test_reference_has_the_story_model_pins_section():
    doc = _reference()
    assert _NEW_HEADING in doc, (
        "REFERENCE.md is missing the '## Story model pins' section"
    )
    assert doc.count(_NEW_HEADING) == 1


def test_story_model_pins_section_body_matches_the_brief():
    doc = _reference()
    assert _norm(_EXPECTED_SECTION) in _norm(doc), (
        "the '## Story model pins' section body does not match the brief"
    )


def test_story_model_pins_section_is_immediately_before_the_anchor():
    doc = _reference()
    assert _ANCHOR in doc, f"REFERENCE.md has no {_ANCHOR.strip()!r} heading line"
    before = doc.split(_ANCHOR, 1)[0]
    assert before.rstrip().endswith("its default model instead of the pin."), (
        f"the new section must sit immediately before {_ANCHOR.strip()!r}"
    )


def test_story_model_pins_section_sits_after_the_pinned_schema_section():
    doc = _reference()
    assert doc.index(_PINNED) < doc.index(_NEW_HEADING) < doc.index(_ANCHOR)


def test_story_model_pins_section_is_outside_the_pinned_schema_section():
    doc = _reference()
    start = doc.index(_PINNED) + len(_PINNED)
    rest = doc[start:]
    next_heading = rest.index("\n## ")
    pinned_body = rest[:next_heading]
    assert _NEW_HEADING not in pinned_body, (
        "the new section must NOT be inserted into the pinned "
        "'## Plan / story schema' section"
    )
    # ...and the pinned section's own model bullet is still there, untouched.
    assert '"model": "sonnet"' in pinned_body
