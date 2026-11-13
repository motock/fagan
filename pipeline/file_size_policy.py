"""Resolve the effective ``file_size_policy`` for one dispatch from the policy layers.

Pure: no I/O, no environment reads, no logging. Layers only tighten, except
that a team may loosen an org ``enforced`` policy when the org opts in. An
unknown value falls back to the default (``advisory``), not to the restrictive
value, and is recorded in ``invalid_layers``.
"""

from pipeline.policy_settings import SETTINGS_ERROR_KEY, SETTINGS_PROVIDER_ERROR

FILE_SIZE_POLICY_ADVISORY = "advisory"
FILE_SIZE_POLICY_ENFORCED = "enforced"
FILE_SIZE_POLICY_VALUES = (FILE_SIZE_POLICY_ADVISORY, FILE_SIZE_POLICY_ENFORCED)


def _read_layer(layer, value, invalid_layers):
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped in FILE_SIZE_POLICY_VALUES:
            return stripped
    invalid_layers.append(layer)
    return FILE_SIZE_POLICY_ADVISORY


def _tenant_layer(settings, key):
    if not isinstance(settings, dict):
        return {}
    layer = settings.get(key)
    return layer if isinstance(layer, dict) else {}


def _result(file_size_policy, source, lock_rejections, invalid_layers):
    return {
        "file_size_policy": file_size_policy,
        "source": source,
        "lock_rejections": lock_rejections,
        "invalid_layers": invalid_layers,
    }


def resolve_file_size_policy(*, settings, env_value=None):
    invalid_layers = []
    request = _read_layer("env", env_value, invalid_layers)

    if isinstance(settings, dict) and SETTINGS_ERROR_KEY in settings:
        rejections = []
        if request == FILE_SIZE_POLICY_ADVISORY:
            rejections.append(
                {
                    "layer": "env",
                    "requested": FILE_SIZE_POLICY_ADVISORY,
                    "by": SETTINGS_PROVIDER_ERROR,
                }
            )
        return _result(
            FILE_SIZE_POLICY_ENFORCED,
            SETTINGS_PROVIDER_ERROR,
            rejections,
            invalid_layers,
        )

    org = _tenant_layer(settings, "org")
    team = _tenant_layer(settings, "team")
    org_value = _read_layer("org", org.get("file_size_policy"), invalid_layers)
    team_value = _read_layer("team", team.get("file_size_policy"), invalid_layers)
    lock = org.get("allow_team_loosen") is True

    rejections = []
    if team_value is not None:
        if (
            team_value == FILE_SIZE_POLICY_ADVISORY
            and org_value == FILE_SIZE_POLICY_ENFORCED
            and not lock
        ):
            rejections.append(
                {
                    "layer": "team",
                    "requested": FILE_SIZE_POLICY_ADVISORY,
                    "by": "org",
                }
            )
            policy = (FILE_SIZE_POLICY_ENFORCED, "org")
        else:
            policy = (team_value, "team")
    elif org_value is not None:
        policy = (org_value, "org")
    else:
        policy = None

    if request is None:
        if policy is None:
            return _result(
                FILE_SIZE_POLICY_ADVISORY, "default", rejections, invalid_layers
            )
        return _result(policy[0], policy[1], rejections, invalid_layers)
    if request == FILE_SIZE_POLICY_ENFORCED or policy is None:
        return _result(request, "env", rejections, invalid_layers)

    policy_value, policy_layer = policy
    if policy_value == FILE_SIZE_POLICY_ENFORCED:
        rejections.append(
            {
                "layer": "env",
                "requested": FILE_SIZE_POLICY_ADVISORY,
                "by": policy_layer,
            }
        )
        return _result(
            FILE_SIZE_POLICY_ENFORCED, policy_layer, rejections, invalid_layers
        )
    return _result(FILE_SIZE_POLICY_ADVISORY, policy_layer, rejections, invalid_layers)
