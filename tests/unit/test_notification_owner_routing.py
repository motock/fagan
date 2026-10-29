"""RT-03: notification routes may filter on a plan's ``owner``.

A route's ``match`` object now accepts an optional ``owner`` glob next to
``plan``.  A route carrying an ``owner`` glob matches only when BOTH globs
match, and an EMPTY owner never matches an owner glob -- deny by default, so a
plan whose manifest names no owner is never delivered to somebody else's
channel.  Routes without ``owner`` keep their exact pre-existing behaviour, and
first-match-wins is unchanged.

These tests also pin the spool side: ``pipeline.notification_outbox`` reads the
plan manifest's ``owner`` once per spool call (any read error means "no owner",
which falls through to plan-only routes).
"""

from __future__ import annotations

import json

import pytest

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server  # noqa: F401  - breaks a pre-existing import cycle
from pipeline import notification_channels as nc
from pipeline import notification_outbox as ob
from pipeline import paths, persistence, store
from pipeline.notification_routing import (
    NotificationConfigError,
    load_notification_config,
)


class _OwnerChannel:
    type_name = "fake_rt03"

    def __init__(self, name):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"

    @classmethod
    def from_settings(cls, name, settings, env):
        return cls(name)


@pytest.fixture(autouse=True)
def _register_owner_channel():
    if "fake_rt03" not in nc.registered_types():
        nc.register_channel_type(_OwnerChannel)
    yield


def _write(tmp_path, data):
    path = tmp_path / "notifications.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def _load(tmp_path, data):
    return load_notification_config(_write(tmp_path, data), env={})


def _cfg(routes=None):
    return {
        "version": 1,
        "instances": {
            "alice-mail": {"type": "fake_rt03"},
            "bob-mail": {"type": "fake_rt03"},
            "catch-all": {"type": "fake_rt03"},
        },
        "routes": routes
        if routes is not None
        else [
            {
                "name": "alice-owned",
                "match": {"plan": "team-*", "owner": "alice@example.com"},
                "deliver": [{"events": ["*"], "to": ["alice-mail"]}],
            },
            {
                "name": "everyone-else",
                "match": {"plan": "team-*"},
                "deliver": [{"events": ["*"], "to": ["catch-all"]}],
            },
        ],
    }


# --------------------------------------------------------------------------
# resolve(): owner glob semantics
# --------------------------------------------------------------------------


def test_owner_and_plan_globs_both_match(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("team-build", "decision", owner="alice@example.com") == [
        "alice-mail"
    ]


def test_owner_route_skipped_for_a_different_owner(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("team-build", "decision", owner="bob@example.com") == [
        "catch-all"
    ]


def test_owner_glob_is_a_glob(tmp_path):
    cfg = _load(
        tmp_path,
        _cfg(
            routes=[
                {
                    "name": "alice-domain",
                    "match": {"plan": "*", "owner": "alice@*"},
                    "deliver": [{"events": ["*"], "to": ["alice-mail"]}],
                }
            ]
        ),
    )
    assert cfg.resolve("any-plan", "overlord_failure", "alice@example.com") == [
        "alice-mail"
    ]


def test_plan_glob_still_has_to_match_when_owner_matches(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("other-plan", "decision", owner="alice@example.com") == []


def test_plan_only_route_unchanged_without_owner(tmp_path):
    """Regression: a route with no ``owner`` key matches any owner."""
    cfg = _load(
        tmp_path,
        _cfg(
            routes=[
                {
                    "name": "plan-only",
                    "match": {"plan": "team-*"},
                    "deliver": [{"events": ["*"], "to": ["bob-mail"]}],
                }
            ]
        ),
    )
    assert cfg.resolve("team-x", "decision") == ["bob-mail"]
    assert cfg.resolve("team-x", "decision", owner="") == ["bob-mail"]
    assert cfg.resolve("team-x", "decision", owner="anyone@example.com") == [
        "bob-mail"
    ]


@pytest.mark.parametrize("owner", ["", "unknown@example.com", "*"])
def test_empty_or_unknown_owner_does_not_match_owner_route(tmp_path, owner):
    """An unknown owner falls through; an empty owner never matches a glob."""
    cfg = _load(
        tmp_path,
        _cfg(
            routes=[
                {
                    "name": "owned-only",
                    "match": {"plan": "*", "owner": "alice@example.com"},
                    "deliver": [{"events": ["*"], "to": ["alice-mail"]}],
                }
            ]
        ),
    )
    assert cfg.resolve("team-x", "decision", owner=owner) == []


def test_empty_owner_never_matches_wildcard_owner_glob(tmp_path):
    """Deny by default: ``owner: "*"`` still excludes a plan with no owner."""
    cfg = _load(
        tmp_path,
        _cfg(
            routes=[
                {
                    "name": "any-owner",
                    "match": {"plan": "*", "owner": "*"},
                    "deliver": [{"events": ["*"], "to": ["alice-mail"]}],
                }
            ]
        ),
    )
    assert cfg.resolve("team-x", "decision", owner="") == []
    assert cfg.resolve("team-x", "decision", owner="bob@example.com") == ["alice-mail"]


def test_first_match_wins_when_owner_route_precedes_catch_all(tmp_path):
    cfg = _load(tmp_path, _cfg())
    assert cfg.resolve("team-x", "story_parked", owner="alice@example.com") == [
        "alice-mail"
    ]
    assert cfg.resolve("team-x", "story_parked", owner="bob@example.com") == [
        "catch-all"
    ]


def test_owner_route_does_not_list_the_event_goes_nowhere(tmp_path):
    cfg = _load(
        tmp_path,
        _cfg(
            routes=[
                {
                    "name": "alice-owned",
                    "match": {"plan": "*", "owner": "alice@example.com"},
                    "deliver": [{"events": ["story_parked"], "to": ["alice-mail"]}],
                },
                {
                    "name": "catch-all",
                    "match": {"plan": "*"},
                    "deliver": [{"events": ["*"], "to": ["catch-all"]}],
                },
            ]
        ),
    )
    # The owning route wins the plan and then drops the unlisted event.
    assert cfg.resolve("team-x", "decision", owner="alice@example.com") == []


# --------------------------------------------------------------------------
# config validation of ``owner``
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "owner",
    [None, 7, True, ["alice@example.com"], {"email": "alice@example.com"}, ""],
)
def test_invalid_owner_is_rejected_naming_the_key_path(tmp_path, owner):
    cfg = _cfg()
    cfg["routes"][0]["match"]["owner"] = owner
    with pytest.raises(NotificationConfigError) as excinfo:
        _load(tmp_path, cfg)
    message = str(excinfo.value)
    assert "routes[0].match.owner" in message
    assert "must be a non-empty string" in message


def test_invalid_owner_does_not_echo_the_value(tmp_path):
    cfg = _cfg()
    cfg["routes"][0]["match"]["owner"] = {"token": "hunter2"}
    with pytest.raises(NotificationConfigError) as excinfo:
        _load(tmp_path, cfg)
    assert "hunter2" not in str(excinfo.value)


def test_unknown_match_key_still_rejected_with_not_supported_yet(tmp_path):
    cfg = _cfg()
    cfg["routes"][0]["match"]["other"] = "x"
    with pytest.raises(NotificationConfigError) as excinfo:
        _load(tmp_path, cfg)
    message = str(excinfo.value)
    assert "routes[0].match.other" in message
    assert "not supported yet" in message
    assert "'plan' and 'owner'" in message


# --------------------------------------------------------------------------
# spool side: the plan manifest's owner decides the route
# --------------------------------------------------------------------------


class _SpoolEnv:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def config(self, data):
        (self.tmp_path / "cfg" / "notifications.json").write_text(json.dumps(data))

    def manifest(self, plan, owner):
        data = {"plan": plan}
        if owner is not None:
            data["owner"] = owner
        (self.tmp_path / f"{plan}.manifest.json").write_text(json.dumps(data))


@pytest.fixture
def spool_env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(store, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(ob, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(nc, "_REGISTRY", {"fake_rt03": _OwnerChannel})
    monkeypatch.delenv(ob.OUTBOX_ENABLED_ENV, raising=False)
    cfg_path = tmp_path / "cfg" / "notifications.json"
    cfg_path.parent.mkdir()
    monkeypatch.setenv("PIPELINE_NOTIFY_CONFIG_PATH", str(cfg_path))
    return _SpoolEnv(tmp_path, monkeypatch)


_TWO_ROUTES = {
    "version": 1,
    "instances": {
        "alice-mail": {"type": "fake_rt03"},
        "catch-all": {"type": "fake_rt03"},
    },
    "routes": [
        {
            "name": "alice-owned",
            "match": {"plan": "*", "owner": "alice@example.com"},
            "deliver": [{"events": ["*"], "to": ["alice-mail"]}],
        },
        {
            "name": "catch-all",
            "match": {"plan": "*"},
            "deliver": [{"events": ["*"], "to": ["catch-all"]}],
        },
    ],
}


def _emit(plan, event):
    ob.outbox_sink({"type": "notification", "plan": plan, "payload": {"event": event}})


def _outbox_lines(root, plan, instance):
    path = root / f"{plan}.outbox.{instance}.jsonl"
    return path.read_text().splitlines() if path.exists() else []


def test_owned_plan_event_spools_to_the_owner_instance(spool_env):
    spool_env.config(_TWO_ROUTES)
    spool_env.manifest("team-x", "alice@example.com")

    _emit("team-x", "decision")

    assert len(_outbox_lines(spool_env.tmp_path, "team-x", "alice-mail")) == 1
    assert _outbox_lines(spool_env.tmp_path, "team-x", "catch-all") == []


def test_plan_owned_by_somebody_else_spools_to_the_catch_all(spool_env):
    spool_env.config(_TWO_ROUTES)
    spool_env.manifest("team-x", "bob@example.com")

    _emit("team-x", "decision")

    assert _outbox_lines(spool_env.tmp_path, "team-x", "alice-mail") == []
    assert len(_outbox_lines(spool_env.tmp_path, "team-x", "catch-all")) == 1


def test_unreadable_manifest_falls_through_to_plan_only_routes(spool_env):
    spool_env.config(_TWO_ROUTES)  # no manifest written at all

    _emit("team-x", "decision")

    assert _outbox_lines(spool_env.tmp_path, "team-x", "alice-mail") == []
    assert len(_outbox_lines(spool_env.tmp_path, "team-x", "catch-all")) == 1


def test_malformed_manifest_falls_through_to_plan_only_routes(spool_env):
    spool_env.config(_TWO_ROUTES)
    (spool_env.tmp_path / "team-x.manifest.json").write_text("{not json")

    _emit("team-x", "decision")

    assert _outbox_lines(spool_env.tmp_path, "team-x", "alice-mail") == []
    assert len(_outbox_lines(spool_env.tmp_path, "team-x", "catch-all")) == 1


def test_manifest_is_read_once_per_spool_call(spool_env):
    spool_env.config(_TWO_ROUTES)
    spool_env.manifest("team-x", "alice@example.com")
    reads = []
    original = store.FileStore.get_manifest

    def _counting(self, plan_name):
        reads.append(plan_name)
        return original(self, plan_name)

    spool_env.monkeypatch.setattr(store.FileStore, "get_manifest", _counting)

    _emit("team-x", "decision")

    assert reads == ["team-x"]
