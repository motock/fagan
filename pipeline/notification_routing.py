"""Notification config loading, validation and route resolution (stdlib only).

The config file is JSON (version 1)::

    {"version": 1,
     "instances": {"<name>": {"type": "<type_name>", ...settings}},
     "routes": [{"name": str, "match": {"plan": "<glob>", "owner": "<glob>"},
                 "deliver": [{"events": ["<event>" | "*"], "to": ["<instance>"]}]}]}

``match.owner`` is optional: a route carrying it matches only when both the
plan and the plan owner globs match, and a plan with no owner never matches it.

An ABSENT file means legacy mode: ``load_notification_config`` returns None.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.plugin_loader import load_plugin_list
from pipeline import paths
from pipeline.notification_channels import (
    ChannelConfigError,
    channel_type,
    reject_inline_secrets,
    validate_instance_name,
)

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV = "PIPELINE_NOTIFY_CONFIG_PATH"
MAX_CONFIG_BYTES = 256 * 1024
# A layer's ``notification_routes`` value holds the file's instances and routes
# and nothing else (no ``version``: the deployment file owns that).
_LAYER_KEYS = ("instances", "routes")
_ENV_KEY_SUFFIX = "_env"
# The store's default namespace name. Only this namespace may read environment
# variables. A literal, not the store constant: importing the store here would
# grow the default-namespace ratchet.
_ENV_NAMESPACE = "default"


class NotificationConfigError(ValueError):
    """Invalid notification config; names the problem and key path, never a secret."""


@dataclass(frozen=True)
class _Route:
    name: str
    plan_glob: str
    deliver: tuple[tuple[frozenset[str], tuple[str, ...]], ...]
    owner_glob: str | None = None


@dataclass(frozen=True)
class NotificationConfig:
    instances: dict[str, Any] = field(default_factory=dict)
    routes: tuple[_Route, ...] = ()

    def resolve(self, plan: str, event: str | None, owner: str = "") -> list[str]:
        """Return the instance names an event of ``plan`` is delivered to.

        The FIRST route whose ``match.plan`` glob (``fnmatch.fnmatchcase``)
        matches ``plan`` -- and, when the route carries an ``owner`` glob, whose
        ``owner`` glob also matches ``owner`` -- wins. Within it, the result is
        the union of the ``to`` lists of every ``deliver`` entry listing
        ``event`` or ``*``. An event the winning route does not list goes
        nowhere (deny by default), and so does any plan no route matches.

        An empty ``owner`` never matches an ``owner`` glob, so a plan whose
        manifest names no owner is never delivered to an owner-scoped route.
        Routes without ``owner`` match every owner, exactly as before.
        """
        for route in self.routes:
            if not fnmatch.fnmatchcase(plan, route.plan_glob):
                continue
            if route.owner_glob is not None and (
                not owner or not fnmatch.fnmatchcase(owner, route.owner_glob)
            ):
                continue
            result: list[str] = []
            for events, targets in route.deliver:
                if "*" in events or (event is not None and event in events):
                    result.extend(t for t in targets if t not in result)
            return result
        return []


def _fail(path: str, problem: str) -> NotificationConfigError:
    return NotificationConfigError(f"{path}: {problem}")


def _require_object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise _fail(path, "must be an object")
    return value


def _require_str_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise _fail(path, "must be a list")
    if not value:
        raise _fail(path, "must not be empty")
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise _fail(f"{path}[{i}]", "must be a non-empty string")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # json.loads would silently keep the last of two identical keys, dropping
    # the first definition; a typo must not quietly reroute messages.
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise NotificationConfigError(f"duplicate key {key!r} in config")
        seen[key] = value
    return seen


def _build_instances(raw: Any, env: Mapping[str, str]) -> dict[str, Any]:
    raw = _require_object(raw, "instances")
    instances: dict[str, Any] = {}
    for name, settings in raw.items():
        path = f"instances.{name}"
        try:
            validate_instance_name(name)
        except ChannelConfigError as exc:
            raise _fail(path, "invalid instance name") from exc
        settings = _require_object(settings, path)
        type_name = settings.get("type")
        if not isinstance(type_name, str):
            raise _fail(f"{path}.type", "must be a string")
        try:
            cls = channel_type(type_name)
        except KeyError:
            raise _fail(f"{path}.type", f"unknown channel type {type_name!r}") from None
        try:
            reject_inline_secrets(name, settings)
            instances[name] = cls.from_settings(name, settings, env)
        except ChannelConfigError as exc:
            raise _fail(path, str(exc)) from exc
    return instances


def _build_route(raw: Any, index: int, instances: Mapping[str, Any]) -> _Route:
    path = f"routes[{index}]"
    raw = _require_object(raw, path)
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise _fail(f"{path}.name", "must be a non-empty string")
    match = _require_object(raw.get("match"), f"{path}.match")
    for key in match:
        if key not in ("plan", "owner"):
            raise _fail(
                f"{path}.match.{key}",
                "not supported yet; only 'plan' and 'owner' are allowed",
            )
    plan_glob = match.get("plan")
    if not isinstance(plan_glob, str) or not plan_glob:
        raise _fail(f"{path}.match.plan", "must be a non-empty string")
    owner_glob = match.get("owner")
    if "owner" in match and (not isinstance(owner_glob, str) or not owner_glob):
        # The value may carry a credential-shaped string; the message names
        # only the key path.
        raise _fail(f"{path}.match.owner", "must be a non-empty string")
    deliver_raw = raw.get("deliver")
    if not isinstance(deliver_raw, list):
        raise _fail(f"{path}.deliver", "must be a list")
    deliver = []
    for j, entry in enumerate(deliver_raw):
        epath = f"{path}.deliver[{j}]"
        entry = _require_object(entry, epath)
        events = _require_str_list(entry.get("events"), f"{epath}.events")
        targets = _require_str_list(entry.get("to"), f"{epath}.to")
        for k, target in enumerate(targets):
            if target not in instances:
                raise _fail(f"{epath}.to[{k}]", f"unknown instance {target!r}")
        deliver.append((frozenset(events), tuple(targets)))
    return _Route(
        name=name, plan_glob=plan_glob, deliver=tuple(deliver), owner_glob=owner_glob
    )


def _config_path(path: Path | str | None, env: Mapping[str, str]) -> Path:
    if path is not None:
        return Path(path)
    override = env.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return paths.default_notification_config_path()


_REGISTERED_CHANNEL_PLUGINS: set[Callable] = set()


def _register_channel_types() -> None:
    # Channel types register on import; config load must not depend on who
    # imported what first (the spool side never imports these modules).
    from . import notification_email, notification_slack  # noqa: F401
    from .notification_channels import register_channel_type

    # Operator-selected channel plugins: PIPELINE_CHANNEL_PLUGINS lists
    # module:attr registrar callables, each handed register_channel_type.
    # Registration happens once per process because register_channel_type
    # raises on a duplicate type name while this loader runs many times. A
    # registrar that raises propagates (config load must fail, not skip it)
    # and is only recorded once it returned.
    for registrar in load_plugin_list("channel"):
        if registrar not in _REGISTERED_CHANNEL_PLUGINS:
            registrar(register_channel_type)
            _REGISTERED_CHANNEL_PLUGINS.add(registrar)


def load_notification_config(
    path: Path | str | None = None, env: Mapping[str, str] = os.environ
) -> NotificationConfig | None:
    """Load and validate the config; None when the file is absent (legacy mode).

    Raises NotificationConfigError on any invalid file.
    """
    _register_channel_types()

    cfg_path = _config_path(path, env)
    try:
        size = cfg_path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NotificationConfigError(f"config file unreadable ({type(exc).__name__})") from exc
    if size > MAX_CONFIG_BYTES:
        raise NotificationConfigError(f"config file exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        data = json.loads(
            cfg_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise NotificationConfigError(f"config file unreadable ({type(exc).__name__})") from exc
    except json.JSONDecodeError as exc:
        raise NotificationConfigError(
            f"config is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from exc
    data = _require_object(data, "config")
    if data.get("version") != 1 or isinstance(data.get("version"), bool):
        raise _fail("version", "must be 1")
    instances = _build_instances(data.get("instances"), env)
    routes_raw = data.get("routes")
    if not isinstance(routes_raw, list):
        raise _fail("routes", "must be a list")
    routes = tuple(_build_route(r, i, instances) for i, r in enumerate(routes_raw))
    return NotificationConfig(instances=instances, routes=routes)


def safe_load_notification_config(
    path: Path | str | None = None, env: Mapping[str, str] = os.environ
) -> NotificationConfig | None:
    """Like ``load_notification_config`` but fails closed on an invalid file.

    A broken file yields an EMPTY config (no instances, nothing delivered) and
    an ERROR log; it never falls back to legacy mode.
    """
    try:
        return load_notification_config(path, env)
    except NotificationConfigError as exc:
        logger.error("notification config invalid; delivering nothing: error=%s", exc)
        return NotificationConfig()


def _refuse_env_names_outside_default(instances: Any) -> None:
    """Refuse a ``*_env`` setting: it reads the process environment, which only
    the default namespace may use. The message names the key path only."""
    if not isinstance(instances, dict):
        return
    for name, settings in instances.items():
        if not isinstance(settings, dict):
            continue
        for key in settings:
            if isinstance(key, str) and key.endswith(_ENV_KEY_SUFFIX):
                raise _fail(
                    f"instances.{name}.{key}",
                    "environment names are refused outside the default namespace; "
                    "use a *_ref name",
                )


def parse_layer_routes(value: Any, namespace: str) -> NotificationConfig:
    """Validate a non-deployment layer's ``notification_routes`` value.

    The value is ``{"instances": {...}, "routes": [...]}``, both optional, with
    the file's per-instance and per-route schema. Its instances form a table of
    their own: a layer route may name only the layer's instances, never a
    deployment instance. A ``*_env`` setting is refused unless *namespace* is
    the default namespace. Every deny path raises NotificationConfigError, which
    names the key path and never a value.
    """
    _register_channel_types()
    value = _require_object(value, "notification_routes")
    if set(value) - set(_LAYER_KEYS):
        raise _fail("notification_routes", "only 'instances' and 'routes' are allowed")
    raw_instances = value.get("instances", {})
    if namespace != _ENV_NAMESPACE:
        _refuse_env_names_outside_default(raw_instances)
    # Instance settings are validated for shape only; the env argument is
    # never read at build time, so an empty mapping is correct for both cases.
    instances = _build_instances(raw_instances, {})
    routes_raw = value.get("routes", [])
    if not isinstance(routes_raw, list):
        raise _fail("notification_routes.routes", "must be a list")
    routes = tuple(_build_route(r, i, instances) for i, r in enumerate(routes_raw))
    return NotificationConfig(instances=instances, routes=routes)


def namespace_notification_config(
    read_layer_routes: Callable[[str], Any],
    namespace: str | None = None,
    path: Path | str | None = None,
    env: Mapping[str, str] = os.environ,
) -> NotificationConfig | None:
    """Return the config that routes *namespace*'s events.

    The deployment file is merged with the namespace's own layer routes.
    ``read_layer_routes(namespace)`` returns that namespace's ``notification_routes``
    value, or None when unset. The namespace defaults to the bound request
    namespace. When the value is unset or empty the result is exactly
    ``load_notification_config``'s, so an unset layer changes nothing.

    Deployment routes come first and the first matching route wins, so a host
    route is never shadowed by a layer route. Layer instance names must not
    collide with deployment instance names. Raises NotificationConfigError on an
    invalid deployment file or layer value.
    """
    if namespace is None:
        from pipeline.actor import current_namespace

        namespace = current_namespace()
    base = load_notification_config(path, env)
    raw = read_layer_routes(namespace)
    if raw is None or raw == {}:
        return base
    layer = parse_layer_routes(raw, namespace)
    deployment = base if base is not None else NotificationConfig()
    if set(layer.instances) & set(deployment.instances):
        raise _fail(
            "notification_routes.instances", "name collides with a deployment instance"
        )
    return NotificationConfig(
        instances={**deployment.instances, **layer.instances},
        routes=deployment.routes + layer.routes,
    )
