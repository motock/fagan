"""Unit tests for the scoped -> full degrade in pipeline.test_scope_dispatch.

A ``scoped`` resolution runs as ``full`` when the CI merge gate is off, the
ci_rework loop is off, or the repo declares no CI. The CI gate flag, the
environment, the forge adapter and the notification sink are the external
boundaries stubbed here; the resolver itself runs for real.
"""

import logging
from pathlib import Path

import pytest

from pipeline import server as p
from pipeline import settings_store
from pipeline import test_scope_dispatch as tsd
from pipeline.ci import context_from_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO = "/srv/repo"
# A repo_url gives the merge settings a valid repo identity; a bare repo_root
# path is not a Store key, so the settings read would fall back to safe values.
SCOPED_MANIFEST = {
    "test_scope": "scoped",
    "repo_root": REPO,
    "repo_url": "https://github.com/acme/app.git",
}


class _ForgeStub:
    def __init__(self):
        self.has_ci = True
        self.error = None
        self.factory_roots = []
        self.ci_checks = []

    def get_forge(self, repo_root):
        self.factory_roots.append(repo_root)
        return self

    def has_ci_config(self, repo_root):
        self.ci_checks.append(repo_root)
        if self.error is not None:
            raise self.error
        return self.has_ci


@pytest.fixture(autouse=True)
def _scoped_survives_by_default(monkeypatch, seed_merge_gate):
    seed_merge_gate(rework_on_ci_fail=True)
    monkeypatch.setattr(tsd, "policy_settings", lambda namespace, team: {})
    monkeypatch.delenv(tsd.TEST_SCOPE_ENV, raising=False)


@pytest.fixture
def forge(monkeypatch):
    stub = _ForgeStub()
    monkeypatch.setattr(tsd, "get_forge", stub.get_forge)
    return stub


@pytest.fixture
def notifications(monkeypatch):
    sent = []

    def _fake(plan_name, message, **kwargs):
        sent.append((plan_name, message, kwargs))

    monkeypatch.setattr(p, "_notify_user", _fake)
    return sent


def _stamp(manifest=None, story=None):
    return tsd.stamp_test_scope(
        "plan-a", "S1", manifest if manifest is not None else {}, story if story is not None else {}
    )


def _degraded_events(notifications):
    return [sent for sent in notifications if sent[2]["event"] == "test_scope_degraded"]


def _test_scope_section():
    text = (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")
    start = text.index("## Test scope (`test_scope`)")
    end = text.index("\n## ", start + 1)
    return text[start:end]


# ---------- gate on, rework on, CI declared: scoped survives ----------
def test_keeps_scoped_when_gate_rework_and_ci_are_all_on(forge, notifications):
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert (result["test_scope"], "degraded_from" in result) == ("scoped", False)


def test_sends_no_notification_when_scoped_survives(forge, notifications):
    _stamp(manifest=dict(SCOPED_MANIFEST))
    assert notifications == []


def test_checks_ci_config_of_manifest_repo_when_scoped(forge, notifications):
    _stamp(manifest=dict(SCOPED_MANIFEST))
    assert forge.ci_checks == [REPO]


# ---------- CI merge gate disabled ----------
def test_degrades_to_full_when_ci_gate_is_disabled(forge, notifications, monkeypatch, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert (result["test_scope"], result["degraded_reason"]) == ("full", "ci_gate_disabled")


def test_records_degraded_from_scoped_when_ci_gate_is_disabled(forge, notifications, monkeypatch, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False)
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert result["degraded_from"] == "scoped"


def test_notifies_one_warning_degraded_event_when_ci_gate_is_disabled(
    forge, notifications, monkeypatch,
    seed_merge_gate,
):
    seed_merge_gate(merge_ci_gate=False)
    _stamp(manifest=dict(SCOPED_MANIFEST))
    (sent,) = _degraded_events(notifications)
    assert (sent[2]["severity"], sent[2]["story_key"]) == ("warning", "S1")


# ---------- ci_rework loop: the resolved rework_on_ci_fail setting ----------
def test_degrades_when_rework_on_ci_fail_is_off(forge, notifications, seed_merge_gate):
    seed_merge_gate(rework_on_ci_fail=False)
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert (result["test_scope"], result["degraded_reason"]) == ("full", "rework_on_ci_fail_off")


def test_notifies_degraded_event_when_rework_on_ci_fail_is_off(
    forge, notifications, seed_merge_gate
):
    seed_merge_gate(rework_on_ci_fail=False)
    _stamp(manifest=dict(SCOPED_MANIFEST))
    (sent,) = _degraded_events(notifications)
    assert "rework_on_ci_fail_off" in sent[1]


def test_keeps_scoped_when_rework_on_ci_fail_is_on(forge, notifications):
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert result["test_scope"] == "scoped"


@pytest.mark.parametrize("stored, expected", [(True, True), (False, False)])
def test_rework_on_ci_fail_enabled_follows_the_stored_setting(
    seed_merge_gate, stored, expected
):
    seed_merge_gate(rework_on_ci_fail=stored)
    settings = context_from_manifest(dict(SCOPED_MANIFEST))
    assert tsd.rework_on_ci_fail_enabled(settings) is expected


def test_rework_on_ci_fail_is_resolved_per_repo(seed_merge_gate):
    seed_merge_gate(rework_on_ci_fail=False)
    on = context_from_manifest(dict(SCOPED_MANIFEST))
    off = context_from_manifest({"repo_url": "https://github.com/acme/other.git"})
    p._store.records().put_settings(
        "default", "team_repo", on.team, on.repo,
        {"rework_on_ci_fail": True}, [], "ops", None,
    )
    assert tsd.rework_on_ci_fail_enabled(on) is True
    assert tsd.rework_on_ci_fail_enabled(off) is False


def test_rework_on_ci_fail_is_off_when_the_store_is_unreadable(monkeypatch):
    class _UnreadableStore:
        def records(self):
            raise OSError("settings store unreadable")

    monkeypatch.setattr(settings_store, "_store", _UnreadableStore())
    settings = context_from_manifest(dict(SCOPED_MANIFEST))
    assert tsd.rework_on_ci_fail_enabled(settings) is False


# ---------- reason precedence ----------
def test_reports_ci_gate_disabled_before_rework_off(forge, notifications, monkeypatch, seed_merge_gate):
    seed_merge_gate(merge_ci_gate=False, rework_on_ci_fail=False)
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert result["degraded_reason"] == "ci_gate_disabled"


def test_reports_rework_off_before_missing_ci_without_forge_call(
    forge, notifications, seed_merge_gate
):
    seed_merge_gate(rework_on_ci_fail=False)
    forge.has_ci = False
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert (result["degraded_reason"], forge.factory_roots, forge.ci_checks) == (
        "rework_on_ci_fail_off",
        [],
        [],
    )


# ---------- repo CI config ----------
def test_degrades_to_full_when_repo_has_no_ci_config(forge, notifications):
    forge.has_ci = False
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert (result["test_scope"], result["degraded_reason"]) == ("full", "no_ci_config")


def test_notifies_degraded_event_when_repo_has_no_ci_config(forge, notifications):
    forge.has_ci = False
    _stamp(manifest=dict(SCOPED_MANIFEST))
    (sent,) = _degraded_events(notifications)
    assert "no_ci_config" in sent[1]


def test_degrades_to_full_when_ci_config_check_raises(forge, notifications):
    forge.error = RuntimeError("unreadable")
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert (result["test_scope"], result["degraded_reason"]) == ("full", "no_ci_config")


def test_logs_warning_with_error_type_when_ci_config_check_raises(
    forge, notifications, caplog
):
    forge.error = RuntimeError("unreadable")
    with caplog.at_level(logging.WARNING, logger="pipeline"):
        _stamp(manifest=dict(SCOPED_MANIFEST))
    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert (record.getMessage(), record.error_type) == (
        "CI configuration unreadable; treating test_scope as full",
        "RuntimeError",
    )


@pytest.mark.parametrize("manifest", [{"test_scope": "scoped"}, {"test_scope": "scoped", "repo_root": ""}])
def test_degrades_when_manifest_has_no_usable_repo_root(forge, notifications, manifest):
    result = _stamp(manifest=dict(manifest))
    assert (result["test_scope"], result["degraded_reason"]) == ("full", "no_ci_config")


def test_does_not_consult_forge_when_manifest_has_no_repo_root(forge, notifications):
    _stamp(manifest={"test_scope": "scoped"})
    assert forge.ci_checks == []


# ---------- full mode touches no forge and no CI gate ----------
def test_full_resolution_never_calls_forge(forge, notifications):
    _stamp(manifest={"repo_root": REPO})
    assert forge.factory_roots == []


def test_full_resolution_never_reads_ci_config(forge, notifications):
    _stamp(manifest={"test_scope": "full", "repo_root": REPO})
    assert forge.ci_checks == []


def test_full_resolution_sends_no_degraded_event(forge, notifications):
    forge.has_ci = False
    _stamp(manifest={"test_scope": "full", "repo_root": REPO})
    assert notifications == []


# ---------- resolver fields survive a degrade ----------
def test_preserves_source_and_lock_rejections_on_degraded_result(
    forge, notifications, monkeypatch
):
    rejection = {"layer": "story", "requested": "scoped", "by": "org"}
    resolved = {
        "test_scope": "scoped",
        "source": "team",
        "lock_rejections": [rejection],
        "invalid_layers": [],
    }
    monkeypatch.setattr(tsd, "resolve_test_scope", lambda **kwargs: dict(resolved))
    forge.has_ci = False
    result = _stamp(manifest=dict(SCOPED_MANIFEST))
    assert (result["source"], result["lock_rejections"], result["test_scope"]) == (
        "team",
        [rejection],
        "full",
    )


# ---------- docs pin ----------
@pytest.mark.parametrize(
    "literal",
    [
        "- `ci_gate_disabled` —",
        "- `rework_on_ci_fail_off` —",
        "- `no_ci_config` —",
    ],
)
def test_reference_documents_each_degrade_reason_in_test_scope_section(literal):
    assert literal in _test_scope_section()
