"""The deployment ``dispatch_hold`` lock and the running-agents read (pipeline.dispatch_hold).

Graded here:

* the key is a deployment-only bool lock, safe value off, and nothing else;
* ``dispatch_held`` reads the deployment record: a stored True holds, False and
  an absent record or key do not;
* a store with no ``get_settings`` capability cannot have a hold set, so it is
  not held and logs nothing;
* a store that raises, or stores a non-bool, IS held and logs one WARNING;
* ``running_agents`` counts stories whose process is alive, across every
  namespace when the store lists them, else the default namespace only;
* setting and clearing the hold through ``settings_tools.set_setting`` writes
  one audit record each, with the old and new values.
"""

import logging
import os
import subprocess
import sys

import pytest

from app.identity import Principal
from pipeline import authorization, settings_tools
from pipeline import dispatch_hold as dh
from pipeline.actor import bind_principal
from pipeline.settings_registry import REGISTRY
from pipeline.store_records import DEFAULT_NAMESPACE

KEY = "dispatch_hold"
HUMAN = Principal("user:alice", auth_method="oidc")
OTHER_NAMESPACE = "env-b"


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


LIVE_PID = os.getpid()


class _Records:
    """A record store offering settings and manifests, but no namespace listing."""

    def __init__(self, settings=None, error=None, manifests=None):
        self._settings = settings
        self._error = error
        self._manifests = manifests or {}

    def get_settings(self, namespace, scope, scope_id, repo):
        if self._error is not None:
            raise self._error
        if scope != "deployment" or namespace != DEFAULT_NAMESPACE:
            return None
        return self._settings

    def get_manifests(self, namespace):
        return self._manifests.get(namespace, {})


class _NamespacedRecords(_Records):
    """The same store, also offering ``list_namespaces``."""

    def list_namespaces(self):
        return sorted(self._manifests)


class _Store:
    def __init__(self, records):
        self._records = records

    def records(self):
        return self._records


def _record(**values):
    return {"values": values, "allow_loosen": [], "version": 1}


def _story(status, pid=None):
    story = {"status": status}
    if pid is not None:
        story["pid"] = pid
    return story


# --- registry ------------------------------------------------------------------


def test_the_key_is_a_deployment_only_bool_lock_with_safe_value_off():
    key = REGISTRY[KEY]
    assert (key.kind, key.type, key.safe_value, key.allowed_layers) == (
        "lock",
        "bool",
        False,
        ("deployment",),
    )


def test_the_key_accepts_only_a_bool():
    key = REGISTRY[KEY]
    assert key.accepts(True) is True
    assert key.accepts(False) is True
    assert key.accepts("true") is False
    assert key.accepts(1) is False


# --- dispatch_held -------------------------------------------------------------


def test_a_stored_true_hold_is_held():
    assert dh.dispatch_held(_Store(_Records(_record(**{KEY: True})))) is True


def test_a_stored_false_hold_is_not_held():
    assert dh.dispatch_held(_Store(_Records(_record(**{KEY: False})))) is False


def test_no_deployment_record_is_not_held():
    assert dh.dispatch_held(_Store(_Records(settings=None))) is False


def test_a_record_without_the_key_is_not_held():
    assert dh.dispatch_held(_Store(_Records(_record(host_tests_skipped=True)))) is False


def test_a_store_without_settings_capability_is_not_held_and_logs_nothing(caplog):
    class _NoSettings:
        def get_manifests(self, namespace):
            return {}

    with caplog.at_level(logging.WARNING, logger="pipeline.dispatch_hold"):
        assert dh.dispatch_held(_Store(_NoSettings())) is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_store_with_no_records_accessor_is_not_held_and_logs_nothing(caplog):
    class _BareStore:
        pass

    with caplog.at_level(logging.WARNING, logger="pipeline.dispatch_hold"):
        assert dh.dispatch_held(_BareStore()) is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_store_that_raises_on_read_is_held_and_warns_once(caplog):
    store = _Store(_Records(error=OSError("disk gone")))
    with caplog.at_level(logging.WARNING, logger="pipeline.dispatch_hold"):
        assert dh.dispatch_held(store) is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.parametrize("stored", ["yes", 1, None, ["true"]])
def test_a_non_bool_stored_value_is_held_and_warns(caplog, stored):
    store = _Store(_Records(_record(**{KEY: stored})))
    with caplog.at_level(logging.WARNING, logger="pipeline.dispatch_hold"):
        assert dh.dispatch_held(store) is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_the_warning_does_not_echo_the_stored_value(caplog):
    store = _Store(_Records(_record(**{KEY: "secret-looking-value"})))
    with caplog.at_level(logging.WARNING, logger="pipeline.dispatch_hold"):
        dh.dispatch_held(store)
    assert "secret-looking-value" not in caplog.text


# --- filter_ready ---------------------------------------------------------------


def test_filter_ready_passes_the_list_through_when_not_held():
    assert dh.filter_ready(["s1", "s2"], held=False) == ["s1", "s2"]


def test_filter_ready_empties_the_list_when_held():
    assert dh.filter_ready(["s1", "s2"], held=True) == []


# --- running_agents -------------------------------------------------------------


def test_running_count_lists_only_live_in_progress_stories_across_namespaces():
    dead = _dead_pid()
    manifests = {
        DEFAULT_NAMESPACE: {
            "plan-a": {"stories": {"S1": _story("in_progress", LIVE_PID), "S2": _story("in_progress", dead)}},
        },
        OTHER_NAMESPACE: {
            "plan-b": {"stories": {"S9": _story("in_progress", LIVE_PID), "S10": _story("todo")}},
        },
    }
    result = dh.running_agents(_Store(_NamespacedRecords(manifests=manifests)))
    assert result["scope"] == "all_namespaces"
    assert sorted(result["stories"], key=lambda s: (s["plan"], s["story"])) == [
        {"plan": "plan-a", "story": "S1"},
        {"plan": "plan-b", "story": "S9"},
    ]


def test_running_count_excludes_in_progress_without_a_pid():
    manifests = {DEFAULT_NAMESPACE: {"plan-a": {"stories": {"S1": _story("in_progress")}}}}
    result = dh.running_agents(_Store(_NamespacedRecords(manifests=manifests)))
    assert result["stories"] == []


def test_running_count_is_default_namespace_only_without_namespace_listing():
    manifests = {
        DEFAULT_NAMESPACE: {"plan-a": {"stories": {"S1": _story("in_progress", LIVE_PID)}}},
        OTHER_NAMESPACE: {"plan-b": {"stories": {"S9": _story("in_progress", LIVE_PID)}}},
    }
    result = dh.running_agents(_Store(_Records(manifests=manifests)))
    assert result["scope"] == "default_namespace"
    assert result["stories"] == [{"plan": "plan-a", "story": "S1"}]


def test_running_count_is_zero_when_nothing_runs():
    result = dh.running_agents(_Store(_NamespacedRecords(manifests={})))
    assert result == {"scope": "all_namespaces", "stories": []}


# --- set / clear are audited -----------------------------------------------------


class _AuditingRecords:
    """A deployment settings record store that versions every write."""

    def __init__(self):
        self.record = None

    def get_settings(self, namespace, scope, scope_id, repo):
        return self.record

    def put_settings(self, namespace, scope, scope_id, repo, values, allow_loosen, actor, expected):
        version = (self.record["version"] if self.record else 0) + 1
        self.record = {"values": dict(values), "allow_loosen": list(allow_loosen), "version": version}
        return {"version": version}


class _Allow:
    def allow(self, principal, action, resource=None):
        return True


@pytest.fixture
def audited(monkeypatch):
    records = _AuditingRecords()
    audit_calls = []
    monkeypatch.setattr(settings_tools, "_store", _Store(records))
    monkeypatch.setattr(settings_tools, "provider_configured", lambda: False)
    monkeypatch.setattr(settings_tools, "append_host_audit", lambda **kw: audit_calls.append(kw))
    monkeypatch.setattr(authorization, "default_authorizer", lambda: _Allow())
    monkeypatch.delenv(authorization.ENFORCE_ENV, raising=False)
    return audit_calls


def _write(value, expected_version):
    with bind_principal(HUMAN):
        return settings_tools.set_setting(
            scope="deployment",
            scope_id="",
            repo="",
            key=KEY,
            value=value,
            expected_version=expected_version,
        )


def test_setting_the_hold_writes_one_allowed_audit_record_with_old_and_new(audited):
    result = _write(True, None)
    assert result["ok"] is True
    assert len(audited) == 1
    assert audited[0]["outcome"] == "allowed"
    assert audited[0]["detail"]["old"] is None
    assert audited[0]["detail"]["new"] is True


def test_clearing_the_hold_writes_one_audit_record_with_old_and_new(audited):
    first = _write(True, None)
    _write(False, first["new_version"])
    assert len(audited) == 2
    assert audited[1]["outcome"] == "allowed"
    assert audited[1]["detail"]["old"] is True
    assert audited[1]["detail"]["new"] is False
