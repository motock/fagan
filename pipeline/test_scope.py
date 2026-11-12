"""Resolve the effective ``test_scope`` for one dispatch from the policy layers.

Pure: no I/O, no environment reads, no logging. Layers only tighten, except
that a team may loosen an org ``full`` policy when the org opts in.
"""

from pipeline.policy_settings import SETTINGS_ERROR_KEY, SETTINGS_PROVIDER_ERROR

TEST_SCOPE_FULL = "full"
TEST_SCOPE_SCOPED = "scoped"
TEST_SCOPE_VALUES = (TEST_SCOPE_FULL, TEST_SCOPE_SCOPED)

_REQUEST_LAYERS = ("plan", "story", "env")


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


def _result(test_scope, source, lock_rejections, invalid_layers):
    return {
        "test_scope": test_scope,
        "source": source,
        "lock_rejections": lock_rejections,
        "invalid_layers": invalid_layers,
    }


def resolve_test_scope(*, settings, plan_value=None, story_value=None, env_value=None):
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

    org = _tenant_layer(settings, "org")
    team = _tenant_layer(settings, "team")
    org_scope = _read_layer("org", org.get("test_scope"), invalid_layers)
    team_scope = _read_layer("team", team.get("test_scope"), invalid_layers)
    lock = org.get("allow_team_loosen") is True

    rejections = []
    if team_scope is not None:
        if (
            team_scope == TEST_SCOPE_SCOPED
            and org_scope == TEST_SCOPE_FULL
            and not lock
        ):
            rejections.append(
                {"layer": "team", "requested": TEST_SCOPE_SCOPED, "by": "org"}
            )
            policy = (TEST_SCOPE_FULL, "org")
        else:
            policy = (team_scope, "team")
    elif org_scope is not None:
        policy = (org_scope, "org")
    else:
        policy = None

    if policy is None and request is None:
        return _result(TEST_SCOPE_FULL, "default", rejections, invalid_layers)
    if policy is None:
        return _result(request[1], request[0], rejections, invalid_layers)
    if request is None:
        return _result(policy[0], policy[1], rejections, invalid_layers)

    request_layer, request_scope = request
    policy_scope, policy_layer = policy
    if request_scope == TEST_SCOPE_FULL:
        return _result(TEST_SCOPE_FULL, request_layer, rejections, invalid_layers)
    if policy_scope == TEST_SCOPE_FULL:
        rejections.append(
            {"layer": request_layer, "requested": TEST_SCOPE_SCOPED, "by": policy_layer}
        )
        return _result(TEST_SCOPE_FULL, policy_layer, rejections, invalid_layers)
    return _result(TEST_SCOPE_SCOPED, policy_layer, rejections, invalid_layers)
