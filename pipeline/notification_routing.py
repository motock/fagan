"""Notification config loading, validation and route resolution (stdlib only).

The config file is JSON (version 1)::

    {"version": 1,
     "instances": {"<name>": {"type": "<type_name>", ...settings}},
     "routes": [{"name": str, "match": {"plan": "<glob>"},
                 "deliver": [{"events": ["<event>" | "*"], "to": ["<instance>"]}]}]}

An ABSENT file means legacy mode: ``load_notification_config`` returns None.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


class NotificationConfigError(ValueError):
    """Invalid notification config; names the problem and key path, never a secret."""


@dataclass(frozen=True)
class _Route:
    name: str
    plan_glob: str
    deliver: tuple[tuple[frozenset[str], tuple[str, ...]], ...]


@dataclass(frozen=True)
class NotificationConfig:
    instances: dict[str, Any] = field(default_factory=dict)
    routes: tuple[_Route, ...] = ()

    def resolve(self, plan: str, event: str | None) -> list[str]:
        """Return the instance names an event of ``plan`` is delivered to.

        The FIRST route whose ``match.plan`` glob (``fnmatch.fnmatchcase``)
        matches ``plan`` wins. Within it, the result is the union of the ``to``
        lists of every ``deliver`` entry listing ``event`` or ``*``. An event the
        winning route does not list goes nowhere (deny by default), and so does
        any plan no route matches.
        """
        for route in self.routes:
            if not fnmatch.fnmatchcase(plan, route.plan_glob):
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
        if key != "plan":
            raise _fail(f"{path}.match.{key}", "not supported yet; only 'plan' is allowed")
    plan_glob = match.get("plan")
    if not isinstance(plan_glob, str) or not plan_glob:
        raise _fail(f"{path}.match.plan", "must be a non-empty string")
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
    return _Route(name=name, plan_glob=plan_glob, deliver=tuple(deliver))


def _config_path(path: Path | str | None, env: Mapping[str, str]) -> Path:
    if path is not None:
        return Path(path)
    override = env.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return paths.PLAN_DIR / "notifications.json"


def load_notification_config(
    path: Path | str | None = None, env: Mapping[str, str] = os.environ
) -> NotificationConfig | None:
    """Load and validate the config; None when the file is absent (legacy mode).

    Raises NotificationConfigError on any invalid file.
    """
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
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
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
