"""The file store's layout resolvers live in :mod:`pipeline.persistence`.

The audit stream's two path builders and the legacy outbox directory are file
store implementation, so they are resolved here, at call time, through the
module-level ``PLAN_DIR`` binding. ``pipeline.audit`` keeps its public names and
delegates, so existing callers and the documented call-time resolution are
unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import audit, notification_outbox, persistence


@pytest.fixture
def plans(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def test_should_resolve_the_plan_audit_file_under_the_monkeypatched_plan_dir(plans: Path) -> None:
    assert persistence.audit_file_path("alpha") == plans / "alpha.audit.jsonl"


def test_should_resolve_the_host_audit_file_under_the_monkeypatched_plan_dir(plans: Path) -> None:
    assert persistence.host_audit_file_path() == plans / ".audit.host.jsonl"


def test_should_delegate_audit_path_to_the_persistence_resolver(plans: Path) -> None:
    assert audit.audit_path("alpha") == persistence.audit_file_path("alpha")


def test_should_delegate_host_audit_path_to_the_persistence_resolver(plans: Path) -> None:
    assert audit.host_audit_path() == persistence.host_audit_file_path()


def test_should_keep_the_host_audit_file_name_on_the_audit_facade() -> None:
    assert audit.HOST_AUDIT_FILE == ".audit.host.jsonl"


def test_should_not_refuse_an_odd_plan_name_at_the_path_builder(plans: Path) -> None:
    # Refusal of an invalid plan name happens in the file log, as it always has;
    # the builder only names the file, exactly as ``audit_path`` did before.
    assert audit.audit_path("bad/name") == plans / "bad/name.audit.jsonl"


def test_should_resolve_the_legacy_outbox_dir_through_persistence_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    legacy_dir, _instance_dir = persistence._outbox_dirs()
    assert legacy_dir == tmp_path


def test_should_leave_notification_outbox_without_a_plan_dir_binding() -> None:
    assert not hasattr(notification_outbox, "PLAN_DIR")
