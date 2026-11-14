"""Resolve the effective ``test_scope`` for one dispatch from the policy layers.

No I/O, no environment reads, no logging: the caller reads the tenant settings
and passes them in. The policy layers (deployment, org, team, team_repo) are
combined by ``pipeline.settings_resolve``; this module adds the plan/story
request on top and keeps the result shape dispatch reports.
Layers only tighten, except that a layer may loosen an org ``full`` policy when
the org opts in.
"""

from pipeline.policy_settings import SETTINGS_ERROR_KEY, SETTINGS_PROVIDER_ERROR

TEST_SCOPE_FULL = "full"
TEST_SCOPE_SCOPED = "scoped"
TEST_SCOPE_VALUES = (TEST_SCOPE_FULL, TEST_SCOPE_SCOPED)

TEST_SCOPE_KEY = "test_scope"
_REQUEST_LAYERS = ("plan", "story", "env")
_POLICY_LAYERS = ("deployment", "org", "team", "team_repo")
_DEFAULT_SOURCE = "default"


def _read_layer(layer, value, invalid_layers):
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped in TEST_SCOPE_VALUES:
            return stripped
    invalid_layers.append(layer)
    return TEST_SCOPE_FULL


def _tenant_layer(settings, key):
    if not isinstance(settings, dict):
        return {}
    layer = settings.get(key)
    return layer if isinstance(layer, dict) else {}


def _allowed_loosen(name, layer):
    allowed = list(layer["allow_loosen"]) if isinstance(layer.get("allow_loosen"), list) else []
    # Legacy spelling: the org's boolean opt-in lets lower layers loosen.
    if name == "org" and layer.get("allow_team_loosen") is True:
        allowed.append(TEST_SCOPE_KEY)
    return allowed


def _policy_layers(settings, invalid_layers):
    """The resolver's layers mapping, with invalid values fail-closed to full."""
    layers = {}
    for name in _POLICY_LAYERS:
        layer = _tenant_layer(settings, name)
        entry = {}
        scope = _read_layer(name, layer.get("test_scope"), invalid_layers)
        if scope is not None:
            entry["test_scope"] = scope
        allowed = _allowed_loosen(name, layer)
        if allowed:
            entry["allow_loosen"] = allowed
        if entry:
            layers[name] = entry
    return layers


def _result(test_scope, source, lock_rejections, invalid_layers):
    return {
        "test_scope": test_scope,
        "source": source,
        "lock_rejections": lock_rejections,
        "invalid_layers": invalid_layers,
    }


def resolve_test_scope(*, settings, plan_value=None, story_value=None, env_value=None):
    # settings_registry imports TEST_SCOPE_VALUES from this module, so the
    # resolver modules are imported here, after this module has loaded.
    from pipeline.settings_registry import REQUEST_LAYER
    from pipeline.settings_resolve import SAFE_DEFAULT_SOURCE, resolve

    invalid_layers = []
    requested = {
        "plan": _read_layer("plan", plan_value, invalid_layers),
        "story": _read_layer("story", story_value, invalid_layers),
        "env": _read_layer("env", env_value, invalid_layers),
    }
    request = next(
        (
            (layer, requested[layer])
            for layer in _REQUEST_LAYERS
            if requested[layer] is not None
        ),
        None,
    )

    if isinstance(settings, dict) and SETTINGS_ERROR_KEY in settings:
        rejections = []
        if request is not None and request[1] == TEST_SCOPE_SCOPED:
            rejections.append(
                {
                    "layer": request[0],
                    "requested": TEST_SCOPE_SCOPED,
                    "by": SETTINGS_PROVIDER_ERROR,
                }
            )
        return _result(
            TEST_SCOPE_FULL, SETTINGS_PROVIDER_ERROR, rejections, invalid_layers
        )

    layers = _policy_layers(settings, invalid_layers)
    request_layer, request_scope = request or (REQUEST_LAYER, None)
    resolved = resolve(
        TEST_SCOPE_KEY,
        layers,
        request=request_scope,
        request_layer=request_layer,
    )
    source = _DEFAULT_SOURCE if resolved["source"] == SAFE_DEFAULT_SOURCE else resolved["source"]
    return _result(
        resolved["value"], source, resolved["lock_rejections"], invalid_layers
    )
