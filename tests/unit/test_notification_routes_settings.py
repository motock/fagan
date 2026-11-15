"""The notification_routes settings key and the namespace-merged route config.

A non-deployment layer may carry extra channel instances and routes. The
deployment config file still decides the host's routes, and deployment routes
come first. A namespace only ever sees its own layer's routes.
"""

import json

import pytest

from pipeline.actor import bind_namespace, current_namespace
from pipeline.notification_routing import (
    NotificationConfigError,
    load_notification_config,
    namespace_notification_config,
)
from pipeline.settings_registry import LAYERS, REGISTRY

KEY = "notification_routes"
NAMESPACE = "a/b"
OTHER_NAMESPACE = "c/d"
UNSET_NAMESPACE = "e/f"
SECRET = "xoxb-test-secret-value-not-real"

DEPLOYMENT = {
    "version": 1,
    "instances": {"host_chat": {"type": "slack", "webhook_url_env": "HOST_HOOK"}},
    "routes": [
        {
            "name": "host",
            "match": {"plan": "deploy-*"},
            "deliver": [{"events": ["*"], "to": ["host_chat"]}],
        }
    ],
}
LAYER = {
    "instances": {"ns_chat": {"type": "slack", "webhook_url_ref": "notify.ns"}},
    "routes": [
        {
            "name": "ns",
            "match": {"plan": "*"},
            "deliver": [{"events": ["*"], "to": ["ns_chat"]}],
        }
    ],
}
OTHER_LAYER = {
    "instances": {"other_chat": {"type": "slack", "webhook_url_ref": "notify.other"}},
    "routes": [
        {
            "name": "other",
            "match": {"plan": "*"},
            "deliver": [{"events": ["*"], "to": ["other_chat"]}],
        }
    ],
}
ENV_LAYER = {
    "instances": {"ns_chat": {"type": "slack", "webhook_url_env": "NS_HOOK"}},
    "routes": LAYER["routes"],
}
INLINE_SECRET_LAYER = {
    "instances": {"ns_chat": {"type": "slack", "webhook_url": SECRET}},
    "routes": LAYER["routes"],
}


def _deployment_file(tmp_path, data=DEPLOYMENT):
    path = tmp_path / "notifications.json"
    path.write_text(json.dumps(data))
    return path


def _config(path, layers, namespace):
    return namespace_notification_config(
        lambda name: layers.get(name), namespace, path=path, env={}
    )


# --- the settings key -------------------------------------------------------


def test_notification_routes_is_a_pref_key_with_an_empty_default():
    key = REGISTRY[KEY]
    assert (key.kind, key.safe_value) == ("pref", {})


def test_notification_routes_is_read_from_the_non_deployment_layers_only():
    assert REGISTRY[KEY].allowed_layers == LAYERS[1:]


def test_accepts_a_reference_only_value_in_the_default_namespace():
    assert current_namespace() == "default"
    assert REGISTRY[KEY].accepts(LAYER)


def test_accepts_the_empty_value_as_the_safe_default():
    assert REGISTRY[KEY].accepts({})


@pytest.mark.parametrize("value", [None, [], "routes", 1, True])
def test_refuses_a_value_that_is_not_an_object(value):
    assert not REGISTRY[KEY].accepts(value)


def test_refuses_an_inline_secret_at_write():
    assert not REGISTRY[KEY].accepts(INLINE_SECRET_LAYER)


def test_refuses_an_environment_name_outside_the_default_namespace():
    with bind_namespace(NAMESPACE):
        assert not REGISTRY[KEY].accepts(ENV_LAYER)


def test_accepts_an_environment_name_in_the_default_namespace():
    assert current_namespace() == "default"
    assert REGISTRY[KEY].accepts(ENV_LAYER)


def test_refuses_a_top_level_key_outside_the_schema():
    assert not REGISTRY[KEY].accepts({**LAYER, "version": 1})


# --- merged route config ----------------------------------------------------


def test_layer_route_is_applied_for_its_own_namespace(tmp_path):
    config = _config(_deployment_file(tmp_path), {NAMESPACE: LAYER}, NAMESPACE)

    assert config.resolve("release", "evt") == ["ns_chat"]
    assert set(config.instances) == {"host_chat", "ns_chat"}


def test_deployment_route_takes_precedence_over_a_layer_route(tmp_path):
    config = _config(_deployment_file(tmp_path), {NAMESPACE: LAYER}, NAMESPACE)

    assert config.resolve("deploy-1", "evt") == ["host_chat"]


def test_a_namespace_never_receives_another_namespaces_routes(tmp_path):
    layers = {NAMESPACE: LAYER, OTHER_NAMESPACE: OTHER_LAYER}
    path = _deployment_file(tmp_path)

    mine = _config(path, layers, NAMESPACE)
    theirs = _config(path, layers, OTHER_NAMESPACE)
    unset = _config(path, layers, UNSET_NAMESPACE)

    assert "other_chat" not in mine.instances
    assert mine.resolve("release", "evt") == ["ns_chat"]
    assert theirs.resolve("release", "evt") == ["other_chat"]
    assert "ns_chat" not in theirs.instances
    assert unset.resolve("release", "evt") == []


def test_namespace_defaults_to_the_bound_namespace(tmp_path):
    path = _deployment_file(tmp_path)
    with bind_namespace(NAMESPACE):
        config = namespace_notification_config(
            lambda name: {NAMESPACE: LAYER}.get(name), path=path, env={}
        )

    assert config.resolve("release", "evt") == ["ns_chat"]


def _observable(config):
    # Channel instances have no equality; their names and routes are the contract.
    return (sorted(config.instances), config.routes)


def test_unset_layer_value_leaves_the_deployment_config_unchanged(tmp_path):
    path = _deployment_file(tmp_path)
    expected = _observable(load_notification_config(path, env={}))

    assert _observable(_config(path, {}, NAMESPACE)) == expected
    assert _observable(_config(path, {NAMESPACE: {}}, NAMESPACE)) == expected


def test_no_file_and_no_layer_value_stays_in_legacy_mode(tmp_path):
    missing = tmp_path / "absent.json"

    assert _config(missing, {}, NAMESPACE) is None


def test_layer_routes_apply_without_a_deployment_file(tmp_path):
    missing = tmp_path / "absent.json"
    config = _config(missing, {NAMESPACE: LAYER}, NAMESPACE)

    assert config.resolve("release", "evt") == ["ns_chat"]


def test_layer_route_cannot_deliver_to_a_deployment_instance(tmp_path):
    routes = [
        {
            "name": "leak",
            "match": {"plan": "*"},
            "deliver": [{"events": ["*"], "to": ["host_chat"]}],
        }
    ]
    layer = {"instances": {}, "routes": routes}

    with pytest.raises(NotificationConfigError, match="unknown instance"):
        _config(_deployment_file(tmp_path), {NAMESPACE: layer}, NAMESPACE)


def test_layer_instance_cannot_shadow_a_deployment_instance(tmp_path):
    layer = {
        "instances": {"host_chat": {"type": "slack", "webhook_url_ref": "notify.x"}},
        "routes": [],
    }

    with pytest.raises(NotificationConfigError, match="collides"):
        _config(_deployment_file(tmp_path), {NAMESPACE: layer}, NAMESPACE)


def test_inline_secret_in_a_layer_instance_is_refused_without_echoing_it(tmp_path):
    with pytest.raises(NotificationConfigError) as exc:
        _config(_deployment_file(tmp_path), {NAMESPACE: INLINE_SECRET_LAYER}, NAMESPACE)

    assert "must not hold a secret inline" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_environment_name_outside_the_default_namespace_is_refused(tmp_path):
    with pytest.raises(NotificationConfigError) as exc:
        _config(_deployment_file(tmp_path), {NAMESPACE: ENV_LAYER}, NAMESPACE)

    assert "outside the default namespace" in str(exc.value)
    assert "NS_HOOK" not in str(exc.value)


def test_environment_name_in_the_default_namespace_is_applied(tmp_path):
    config = _config(_deployment_file(tmp_path), {"default": ENV_LAYER}, "default")

    assert config.resolve("release", "evt") == ["ns_chat"]


@pytest.mark.parametrize("value", [[], "routes", {"version": 1}])
def test_a_malformed_layer_value_is_refused(tmp_path, value):
    with pytest.raises(NotificationConfigError):
        _config(_deployment_file(tmp_path), {NAMESPACE: value}, NAMESPACE)
